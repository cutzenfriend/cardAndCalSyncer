"""Runs vdirsyncer (sync/discover), parses the output, and persists results."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import alerts
import clear as clear_mod
import confgen
import enrich
import fixuids
import parser
from db import Database
from store import ConfigStore, vpair_name

log = logging.getLogger("cacs.runner")

CONFIG_FILE = "/config/vdirsyncer.conf"
DISCOVER_CONF = "/config/.discover.conf"
ROLLING_LOG = "/logs/vdirsyncer.log"
LINE_CAP = 16384  # max chars kept per output line (truncate huge base64 dumps)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


_REDACT = [
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(authorization\s*:\s*)\S+"),
    re.compile(r'(?i)("?(?:access_token|refresh_token|client_secret|client_id|password)"?\s*[:=]\s*)"?[^"&,\s]+'),
    re.compile(r"(?i)([?&](?:access_token|refresh_token|code|client_secret|client_id)=)[^&\s]+"),
]


def _redact(line: str) -> str:
    """Strip secrets (bearer tokens, OAuth fields) before logging/showing."""
    line = _REDACT[0].sub("Bearer «redacted»", line)
    line = _REDACT[1].sub(r"\1«redacted»", line)
    line = _REDACT[2].sub(r"\1«redacted»", line)
    line = _REDACT[3].sub(r"\1«redacted»", line)
    return line


class Runner:
    def __init__(self, db: Database, store: ConfigStore):
        self.db = db
        self.store = store
        self._lock = asyncio.Lock()
        self.busy = False
        self.busy_since: float | None = None
        self.busy_what: str | None = None
        self._op_task: asyncio.Task | None = None

    # --- subprocess --------------------------------------------------------
    async def _exec(self, cmd: list[str], secret_env: dict[str, str],
                    run_id: int, idle_timeout: int = 300) -> tuple[int, list[str]]:
        env = dict(os.environ)
        env.update(secret_env)
        env.setdefault("HOME", "/data")
        # Google returns the full granted scope on refresh while vdirsyncer's
        # per-storage session requests a single scope; relax oauthlib's scope
        # check so the refresh doesn't raise "Scope has changed".
        env["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
        lines: list[str] = []
        os.makedirs(os.path.dirname(ROLLING_LOG), exist_ok=True)
        with open(ROLLING_LOG, "a", encoding="utf-8") as rl:
            rl.write(f"\n===== run #{run_id} :: {' '.join(cmd[3:])} :: {_now()} =====\n")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )

            def _emit(raw: bytes) -> None:
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                if len(text) > LINE_CAP:           # e.g. an inline base64 PHOTO/ATTACH
                    text = text[:LINE_CAP] + " …[truncated]"
                line = _redact(text)
                lines.append(line)
                rl.write(f"{_now()} {line}\n")

            async def _drain() -> None:
                # Read in fixed-size chunks and split lines ourselves. Line-based
                # iteration over StreamReader enforces a 64 KB limit and raises
                # "Separator is found, but chunk is longer than limit" on a single
                # long line (vCard PHOTO, event ATTACH, big DEBUG dump) — read()
                # has no such limit.
                assert proc.stdout is not None
                buf = b""
                while True:
                    # Inactivity timeout: abort only if vdirsyncer prints NOTHING
                    # for idle_timeout s (a genuine hang). A large sync keeps
                    # emitting per-item lines, so it's never killed just for taking
                    # a while — only a hard total cap (the old bug) did that.
                    chunk = await asyncio.wait_for(proc.stdout.read(65536),
                                                   timeout=idle_timeout)
                    if not chunk:
                        break
                    buf += chunk
                    nl = buf.find(b"\n")
                    while nl >= 0:
                        _emit(buf[:nl])
                        buf = buf[nl + 1:]
                        nl = buf.find(b"\n")
                    if len(buf) > LINE_CAP * 4:     # no newline yet: flush, don't grow
                        _emit(buf)
                        buf = b""
                if buf:
                    _emit(buf)
                await proc.wait()

            try:
                await _drain()
            except asyncio.TimeoutError:
                msg = (f"[CaCs] aborted: no output for {idle_timeout}s "
                       "(stuck, or the account is unreachable?)")
                lines.append(msg)
                rl.write(f"{_now()} {msg}\n")
            except asyncio.CancelledError:
                # Stop button / cancellation: fall through to the finally so the
                # child is killed, then re-raise to unwind the operation.
                msg = "[CaCs] aborted: cancelled — terminating vdirsyncer"
                lines.append(msg)
                rl.write(f"{_now()} {msg}\n")
                raise
            finally:
                # Always make sure the vdirsyncer child is dead. If it survives
                # (timeout/cancel/error), it keeps holding its status-DB lock and
                # the next sync fails with "database is locked".
                await self._kill(proc)
        return (proc.returncode if proc.returncode is not None else -1), lines

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process) -> None:
        """Terminate a (possibly hung) child so it releases its file locks.
        SIGKILL releases POSIX/SQLite locks at the kernel level immediately;
        the await is only to reap the zombie and is best-effort."""
        if proc.returncode is not None:
            return
        try:
            proc.kill()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=5)
        except (Exception, asyncio.CancelledError):
            pass

    def _unconnected_google(self, cfg: dict[str, Any], pair: str) -> list[str]:
        """Names of Google accounts in this pair that have no OAuth token yet."""
        p = cfg["pairs"][pair]
        accs = cfg["accounts"]
        svc = "cal" if p.get("service", "calendar") == "calendar" else "card"
        missing = []
        for aid in (p["a"], p["b"]):
            a = accs.get(aid, {})
            if a.get("kind") == "google":
                tok = os.path.join(confgen.TOKEN_DIR, f"acc_{aid}_{svc}.token")
                if not os.path.exists(tok):
                    missing.append(a.get("name", aid))
        return missing

    def _write_conf(self, path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def cancel(self) -> bool:
        """Cancel the currently-running operation (frees the lock)."""
        t = self._op_task
        if t is not None and not t.done():
            t.cancel()
            return True
        return False

    # --- sync --------------------------------------------------------------
    async def run_sync_all(self, trigger: str = "scheduled") -> list[dict[str, Any]]:
        """Sync every configured pair as its own run (so runs are separated by pair)."""
        results = []
        for pid, p in self.store.get()["pairs"].items():
            if not p.get("collections"):
                continue
            try:
                results.append(await self.run_sync(pair=pid, trigger=trigger))
            except Exception:
                log.exception("sync of pair %s failed", pid)
        return results

    async def run_sync(self, pair: str | None = None, collection: str | None = None,
                       trigger: str = "scheduled") -> dict[str, Any]:
        async with self._lock:
            self.busy = True
            self.busy_since = time.time()
            self._op_task = asyncio.current_task()
            try:
                if collection is not None:
                    return await self._run_sync_inner(pair, collection, trigger)
                # whole pair: sync each mapping as its own run, because direction
                # is per-mapping now and a vdirsyncer pair can't mix directions.
                shorts = [c[0] for c in self.store.get()["pairs"].get(pair, {})
                          .get("collections", []) if c]
                if not shorts:
                    return {"run_id": None, "status": "success", "rc": 0,
                            "counts": {"create": 0, "update": 0, "delete": 0},
                            "errors": [], "warnings": []}
                agg = {"create": 0, "update": 0, "delete": 0}
                errors: list[str] = []
                warnings: list[str] = []
                failed = False
                last_id = None
                for short in shorts:
                    r = await self._run_sync_inner(pair, short, trigger)
                    last_id = r.get("run_id")
                    for k in agg:
                        agg[k] += r.get("counts", {}).get(k, 0)
                    errors += r.get("errors", [])
                    warnings += r.get("warnings", [])
                    failed = failed or r.get("status") == "failed"
                return {"run_id": last_id, "status": "failed" if failed else "success",
                        "rc": -1 if failed else 0, "counts": agg,
                        "errors": errors, "warnings": warnings}
            finally:
                self.busy = False
                self.busy_since = None
                self._op_task = None

    async def _run_sync_inner(self, pair: str | None, collection: str | None,
                              trigger: str) -> dict[str, Any]:
        cfg = self.store.get()
        prev = self.db.last_run("sync")
        prev_status = prev["status"] if prev else None

        run_id = self.db.start_run("sync", pair, trigger, _now(), collection=collection)
        log.info("sync run #%s started (pair=%s, collection=%s, trigger=%s)",
                 run_id, pair, collection, trigger)
        try:
            return await self._do_sync(run_id, cfg, prev_status, pair, collection)
        except Exception as exc:
            log.exception("sync run #%s aborted", run_id)
            self.db.finish_run(
                run_id, status="failed", finished_at=_now(), rc=-1,
                n_create=0, n_update=0, n_delete=0, n_errors=1,
                log=f"CaCs internal error: {exc}")
            self._maybe_alert(cfg, "failed", prev_status, run_id,
                              parser.ParsedRun(errors=[str(exc)]), -1)
            return {"run_id": run_id, "status": "failed", "rc": -1,
                    "counts": {"create": 0, "update": 0, "delete": 0},
                    "errors": [str(exc)], "warnings": []}

    async def _do_sync(self, run_id: int, cfg: dict[str, Any], prev_status: str | None,
                       pair: str | None, collection: str | None = None) -> dict[str, Any]:
        # Each mapping syncs as its own vdirsyncer pair so read_only matches this
        # mapping's direction (per-mapping bisync/one-way).
        vpair = vpair_name(pair, collection)
        conf, secret_env = confgen.generate(cfg, only_pair=pair, only_collection=collection)
        self._write_conf(CONFIG_FILE, conf)

        cmd = ["vdirsyncer", "-v", "INFO", "-c", CONFIG_FILE, "sync", vpair]
        rc, lines = await self._exec(cmd, secret_env, run_id)

        # First sync of this mapping (no cache) or after a direction change:
        # vdirsyncer keys its cache on collections + storage configs (incl.
        # read_only) and refuses to sync until re-discovered. Run discover once
        # for this scoped config, then retry. (Uses this mapping's own pair name,
        # so it never clobbers the shared `<pid>.collections` register cache.)
        if rc != 0 and any("vdirsyncer discover" in l for l in lines):
            await self._exec(["vdirsyncer", "-v", "INFO", "-c", CONFIG_FILE,
                              "discover", vpair], secret_env, run_id)
            rc, lines = await self._exec(cmd, secret_env, run_id)

        parsed = parser.parse_sync_output(lines)

        # vdirsyncer hides the cause behind "Unknown error … use -vdebug" (the
        # traceback is only logged at DEBUG). On failure, retry once at DEBUG
        # (secrets redacted) to capture it into the run log. Counts/status stay
        # from the first pass.
        if rc != 0 or parsed.errors:
            dbg = ["vdirsyncer", "-v", "DEBUG", "-c", CONFIG_FILE, "sync", vpair]
            _, dlines = await self._exec(dbg, secret_env, run_id)
            lines = lines + ["", "----- DEBUG retry (redacted) -----"] + dlines

        enrich_items: list[dict[str, Any]] = []
        for act in parsed.activities:
            info = self.store.resolve_dest(act.dest_storage, act.collection) or {}
            act_id = self.db.add_activity(run_id, _now(), {
                "action": act.action,
                "ident": act.ident,
                "pair": info.get("pair"),
                "collection": act.collection,
                "collection_label": info.get("collection_label") or act.collection,
                "src_name": info.get("src_name"),
                "src_kind": info.get("src_kind"),
                "dst_name": info.get("dst_name"),
                "dst_kind": info.get("dst_kind"),
            })
            # deletes can't be enriched (item is gone); only create/update
            if act.action != "delete" and info.get("pair_id"):
                enrich_items.append({"activity_id": act_id, "pair_id": info["pair_id"],
                                     "collection": act.collection, "uid": act.ident})

        counts = parsed.counts
        n_errors = len(parsed.errors)
        status = "success" if (rc == 0 and n_errors == 0) else "failed"
        self.db.finish_run(
            run_id, status=status, finished_at=_now(), rc=rc,
            n_create=counts["create"], n_update=counts["update"],
            n_delete=counts["delete"], n_errors=n_errors,
            log="\n".join(lines),
        )
        self.db.prune_runs()

        self._maybe_alert(cfg, status, prev_status, run_id, parsed, rc)

        # best-effort: fetch human titles/dates for changed items (never fatal)
        if enrich_items:
            try:
                await asyncio.wait_for(
                    enrich.enrich(self.store, self.db, enrich_items), timeout=90)
            except Exception:
                log.debug("enrichment skipped/failed", exc_info=True)

        log.info("sync run #%s done: %s (rc=%s, +%s ~%s -%s, %s errors)",
                 run_id, status, rc, counts["create"], counts["update"],
                 counts["delete"], n_errors)
        return {
            "run_id": run_id, "status": status, "rc": rc,
            "counts": counts, "errors": parsed.errors, "warnings": parsed.warnings,
        }

    def _maybe_alert(self, cfg: dict[str, Any], status: str, prev_status: str | None,
                     run_id: int, parsed: parser.ParsedRun, rc: int) -> None:
        al = cfg.get("alerts", {})
        urls = al.get("apprise_urls", [])
        if not urls:
            return
        if status == "failed" and al.get("on_failure", True):
            body = (f"CaCs run #{run_id} failed (rc={rc}).\n\n"
                    + "\n".join(parsed.errors[:10]) or "See logs.")
            alerts.notify(urls, "❌ CaCs: sync failed", body, kind="failure")
        elif status == "success" and prev_status == "failed" and al.get("on_recovery", True):
            alerts.notify(urls, "✅ CaCs: sync recovered",
                          f"Run #{run_id} succeeded.", kind="success")

    # --- clear (destructive) -----------------------------------------------
    async def clear(self, pair: str, side: str, months: int, execute: bool,
                    collection: str | None = None) -> dict[str, Any]:
        async with self._lock:
            self.busy = True
            self.busy_since = time.time()
            self._op_task = asyncio.current_task()
            run_id = None
            try:
                if execute:
                    run_id = self.db.start_run("clear", pair, "manual", _now())
                res = await asyncio.wait_for(
                    clear_mod.run_clear(self.store, pair, side, months, execute, collection),
                    timeout=900)
                if run_id is not None:
                    self.db.finish_run(
                        run_id, status="failed" if res["errors"] else "success",
                        finished_at=_now(), rc=0, n_create=0, n_update=0,
                        n_delete=res["deleted"], n_errors=len(res["errors"]),
                        log=(f"Cleared {res['deleted']} item(s) from {res['account']} "
                             f"(side {side}, calendar: {res['collection']}).\n"
                             + "\n".join(res["errors"])))
                    res["run_id"] = run_id
                return res
            except Exception as exc:
                if run_id is not None:
                    self.db.finish_run(run_id, status="failed", finished_at=_now(), rc=-1,
                                       n_create=0, n_update=0, n_delete=0, n_errors=1,
                                       log=f"Clear failed: {exc}")
                raise
            finally:
                self.busy = False
                self.busy_since = None
                self._op_task = None

    # --- resolve activity titles (read-only backfill) ----------------------
    async def resolve_names(self, pair: str | None = None,
                            collection: str | None = None) -> dict[str, Any]:
        async with self._lock:
            self.busy = True
            self.busy_since = time.time()
            self._op_task = asyncio.current_task()
            try:
                if pair:
                    return await asyncio.wait_for(
                        enrich.resolve(self.store, self.db, pair, collection), timeout=600)
                total, errs = 0, []
                for pid in self.store.get()["pairs"]:
                    try:
                        r = await asyncio.wait_for(
                            enrich.resolve(self.store, self.db, pid), timeout=600)
                        total += r["resolved"]
                        errs += r["errors"]
                    except asyncio.TimeoutError:
                        errs.append(f"{pid}: timed out")
                return {"resolved": total, "errors": errs}
            finally:
                self.busy = False
                self.busy_since = None
                self._op_task = None

    # --- fix long UIDs (rewrites items) ------------------------------------
    async def fix_uids(self, pair: str, side: str, collection: str | None,
                       threshold: int, execute: bool) -> dict[str, Any]:
        async with self._lock:
            self.busy = True
            self.busy_since = time.time()
            self._op_task = asyncio.current_task()
            run_id = None
            try:
                if execute:
                    run_id = self.db.start_run("repair", pair, "manual", _now(),
                                               collection=collection)
                res = await asyncio.wait_for(
                    fixuids.run_fix(self.store, pair, side, collection, threshold, execute),
                    timeout=900)
                if run_id is not None:
                    self.db.finish_run(
                        run_id, status="failed" if res["errors"] else "success",
                        finished_at=_now(), rc=0, n_create=0, n_update=res["fixed"],
                        n_delete=0, n_errors=len(res["errors"]),
                        log=(f"Rewrote {res['fixed']} over-long UID(s) on "
                             f"{res['account']} (>{res['threshold']} chars).\n"
                             + "\n".join(res["errors"])))
                    res["run_id"] = run_id
                return res
            except Exception as exc:
                if run_id is not None:
                    self.db.finish_run(run_id, status="failed", finished_at=_now(), rc=-1,
                                       n_create=0, n_update=0, n_delete=0, n_errors=1,
                                       log=f"Fix UIDs failed: {exc}")
                raise
            finally:
                self.busy = False
                self.busy_since = None
                self._op_task = None

    # --- discover ----------------------------------------------------------
    async def discover(self, pair: str, list_all: bool = True) -> dict[str, Any]:
        async with self._lock:
            self.busy = True
            self.busy_since = time.time()
            self._op_task = asyncio.current_task()
            try:
                return await self._discover_inner(pair, list_all)
            finally:
                self.busy = False
                self.busy_since = None
                self._op_task = None

    async def _discover_inner(self, pair: str, list_all: bool) -> dict[str, Any]:
        cfg = self.store.get()
        run_id = self.db.start_run("discover", pair, "manual", _now())
        try:
            return await self._do_discover(run_id, cfg, pair, list_all)
        except Exception as exc:
            log.exception("discover #%s aborted", run_id)
            self.db.finish_run(
                run_id, status="failed", finished_at=_now(), rc=-1,
                n_create=0, n_update=0, n_delete=0, n_errors=1,
                log=f"CaCs internal error: {exc}")
            return {"run_id": run_id, "status": "failed", "rc": -1,
                    "storages": {}, "error": str(exc)}

    async def _do_discover(self, run_id: int, cfg: dict[str, Any],
                           pair: str, list_all: bool) -> dict[str, Any]:
        p = cfg["pairs"][pair]
        accs = cfg["accounts"]

        def _side(aid: str, colls: list | None = None) -> dict[str, Any]:
            return {"account": aid, "name": accs.get(aid, {}).get("name", aid),
                    "collections": colls or []}

        # Fail fast (and clearly) if a Google account isn't connected yet —
        # otherwise vdirsyncer would try its own browser OAuth flow and hang.
        missing = self._unconnected_google(cfg, pair)
        if missing:
            msg = ("Not connected to Google: " + ", ".join(missing) +
                   ". Open the account and click 'Connect Google' first.")
            self.db.finish_run(run_id, status="failed", finished_at=_now(), rc=-1,
                               n_create=0, n_update=0, n_delete=0, n_errors=1, log=msg)
            return {"run_id": run_id, "status": "failed", "rc": -1, "error": msg,
                    "a": _side(p["a"]), "b": _side(p["b"])}

        conf, secret_env = confgen.generate(cfg, only_pair=pair, list_all=list_all)
        self._write_conf(DISCOVER_CONF, conf)

        cmd = ["vdirsyncer", "-v", "INFO", "-c", DISCOVER_CONF, "discover"]
        if list_all:
            cmd.append("--list")
        cmd.append(pair)

        rc, lines = await self._exec(cmd, secret_env, run_id)

        sa, sb = self.store.pair_storages(p)
        discovered = parser.parse_discover_output(lines, {sa, sb})
        diag = parser.parse_sync_output(lines)  # reuse error/warning classifier
        log_tail = "\n".join(lines[-30:])

        # If a side failed to discover, retry once at DEBUG (secrets redacted)
        # to capture the real traceback for the details panel.
        if list_all and (not discovered.get(sa) or not discovered.get(sb)):
            dbg = ["vdirsyncer", "-v", "DEBUG", "-c", DISCOVER_CONF, "discover", "--list", pair]
            _, dlines = await self._exec(dbg, secret_env, run_id)
            log_tail = "\n".join(dlines[-80:])
            if not diag.errors:
                diag = parser.parse_sync_output(dlines)

        # list-all prints the data before any possible abort -> consider it a
        # success as soon as at least one side was found.
        ok = bool(discovered) if list_all else (rc == 0)
        status = "success" if ok else "failed"
        self.db.finish_run(
            run_id, status=status, finished_at=_now(), rc=rc,
            n_create=0, n_update=0, n_delete=0, n_errors=0 if ok else 1,
            log="\n".join(lines),
        )

        def colls_for(sn: str) -> list[dict[str, str]]:
            return [{"id": c.ident, "name": c.displayname} for c in discovered.get(sn, [])]

        out: dict[str, Any] = {
            "run_id": run_id, "status": status, "rc": rc,
            "a": _side(p["a"], colls_for(sa)),
            "b": _side(p["b"], colls_for(sb)),
            "errors": diag.errors, "warnings": diag.warnings,
            "log_tail": log_tail,
        }
        return out
