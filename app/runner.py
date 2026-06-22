"""Runs vdirsyncer (sync/discover), parses the output, and persists results."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from typing import Any

import alerts
import clear as clear_mod
import confgen
import enrich
import parser
from db import Database
from store import ConfigStore

log = logging.getLogger("cacs.runner")

CONFIG_FILE = "/config/vdirsyncer.conf"
DISCOVER_CONF = "/config/.discover.conf"
DRY_STATUS_DIR = "/data/status_dryrun"
ROLLING_LOG = "/logs/vdirsyncer.log"


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

    # --- subprocess --------------------------------------------------------
    async def _exec(self, cmd: list[str], secret_env: dict[str, str],
                    run_id: int, timeout: int = 180) -> tuple[int, list[str]]:
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

            async def _drain() -> None:
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    line = _redact(raw.decode("utf-8", "replace").rstrip("\n"))
                    lines.append(line)
                    rl.write(f"{_now()} {line}\n")
                await proc.wait()

            try:
                # Bounds any hang (e.g. vdirsyncer trying its own OAuth browser flow).
                await asyncio.wait_for(_drain(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                msg = (f"[CaCs] aborted: timed out after {timeout}s "
                       "(is the account connected and reachable?)")
                lines.append(msg)
                rl.write(f"{_now()} {msg}\n")
                try:
                    await proc.wait()
                except Exception:
                    pass
        return (proc.returncode if proc.returncode is not None else -1), lines

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
            try:
                return await self._run_sync_inner(pair, collection, trigger)
            finally:
                self.busy = False

    async def _run_sync_inner(self, pair: str | None, collection: str | None,
                              trigger: str) -> dict[str, Any]:
        cfg = self.store.get()
        prev = self.db.last_run("sync")
        prev_status = prev["status"] if prev else None
        dry = bool(cfg.get("dry_run", False))

        trig = f"{trigger}·dry" if dry else trigger
        run_id = self.db.start_run("sync", pair, trig, _now(), collection=collection)
        log.info("sync run #%s started (pair=%s, collection=%s, trigger=%s, dry=%s)",
                 run_id, pair, collection, trigger, dry)
        try:
            return await self._do_sync(run_id, cfg, prev_status, pair, dry, collection)
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
                       pair: str | None, dry: bool = False,
                       collection: str | None = None) -> dict[str, Any]:
        conf, secret_env = confgen.generate(cfg, only_pair=pair, dry_run=dry)
        self._write_conf(CONFIG_FILE, conf)

        if dry:
            # isolated, throwaway status dir so the real sync state is never touched;
            # discover into it first to populate the collection cache.
            shutil.rmtree(DRY_STATUS_DIR, ignore_errors=True)
            os.makedirs(DRY_STATUS_DIR, exist_ok=True)
            dcmd = ["vdirsyncer", "-v", "INFO", "-c", CONFIG_FILE, "discover"]
            if pair:
                dcmd.append(pair)
            await self._exec(dcmd, secret_env, run_id)

        cmd = ["vdirsyncer", "-v", "INFO", "-c", CONFIG_FILE, "sync"]
        if pair and collection:
            cmd.append(f"{pair}/{collection}")   # single mapping
        elif pair:
            cmd.append(pair)                      # whole pair

        rc, lines = await self._exec(cmd, secret_env, run_id)
        parsed = parser.parse_sync_output(lines)

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
            run_id = None
            try:
                if execute:
                    run_id = self.db.start_run("clear", pair, "manual", _now())
                res = await clear_mod.run_clear(self.store, pair, side, months, execute, collection)
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

    # --- discover ----------------------------------------------------------
    async def discover(self, pair: str, list_all: bool = True) -> dict[str, Any]:
        async with self._lock:
            self.busy = True
            try:
                return await self._discover_inner(pair, list_all)
            finally:
                self.busy = False

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
