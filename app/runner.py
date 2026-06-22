"""Runs vdirsyncer (sync/discover), parses the output, and persists results."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import datetime, timezone
from typing import Any

import alerts
import confgen
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


class Runner:
    def __init__(self, db: Database, store: ConfigStore):
        self.db = db
        self.store = store
        self._lock = asyncio.Lock()
        self.busy = False

    # --- subprocess --------------------------------------------------------
    async def _exec(self, cmd: list[str], secret_env: dict[str, str],
                    run_id: int) -> tuple[int, list[str]]:
        env = dict(os.environ)
        env.update(secret_env)
        env.setdefault("HOME", "/data")
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
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                lines.append(line)
                rl.write(f"{_now()} {line}\n")
            await proc.wait()
        return proc.returncode or 0, lines

    def _write_conf(self, path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    # --- sync --------------------------------------------------------------
    async def run_sync(self, pair: str | None = None,
                       trigger: str = "scheduled") -> dict[str, Any]:
        async with self._lock:
            self.busy = True
            try:
                return await self._run_sync_inner(pair, trigger)
            finally:
                self.busy = False

    async def _run_sync_inner(self, pair: str | None, trigger: str) -> dict[str, Any]:
        cfg = self.store.get()
        prev = self.db.last_run("sync")
        prev_status = prev["status"] if prev else None
        dry = bool(cfg.get("dry_run", False))

        trig = f"{trigger}·dry" if dry else trigger
        run_id = self.db.start_run("sync", pair, trig, _now())
        log.info("sync run #%s started (pair=%s, trigger=%s, dry=%s)", run_id, pair, trigger, dry)
        try:
            return await self._do_sync(run_id, cfg, prev_status, pair, dry)
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
                       pair: str | None, dry: bool = False) -> dict[str, Any]:
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
        if pair:
            cmd.append(pair)

        rc, lines = await self._exec(cmd, secret_env, run_id)
        parsed = parser.parse_sync_output(lines)

        for act in parsed.activities:
            info = self.store.resolve_dest(act.dest_storage) or {}
            self.db.add_activity(run_id, _now(), {
                "action": act.action,
                "ident": act.ident,
                "pair": info.get("pair"),
                "collection": act.collection,
                "src_name": info.get("src_name"),
                "src_kind": info.get("src_kind"),
                "dst_name": info.get("dst_name"),
                "dst_kind": info.get("dst_kind"),
            })

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
        conf, secret_env = confgen.generate(cfg, only_pair=pair, list_all=list_all)
        self._write_conf(DISCOVER_CONF, conf)

        cmd = ["vdirsyncer", "-v", "INFO", "-c", DISCOVER_CONF, "discover"]
        if list_all:
            cmd.append("--list")
        cmd.append(pair)

        rc, lines = await self._exec(cmd, secret_env, run_id)

        p = cfg["pairs"][pair]
        sa, sb = self.store.pair_storages(p)
        known = {sa, sb}
        discovered = parser.parse_discover_output(lines, known)

        # list-all prints the data before any possible abort -> consider it a
        # success as soon as at least one side was found.
        ok = bool(discovered) if list_all else (rc == 0)
        status = "success" if ok else "failed"
        self.db.finish_run(
            run_id, status=status, finished_at=_now(), rc=rc,
            n_create=0, n_update=0, n_delete=0, n_errors=0 if ok else 1,
            log="\n".join(lines),
        )

        accs = cfg["accounts"]

        def colls_for(sn: str) -> list[dict[str, str]]:
            return [{"id": c.ident, "name": c.displayname} for c in discovered.get(sn, [])]

        out: dict[str, Any] = {
            "run_id": run_id, "status": status, "rc": rc,
            "a": {"account": p["a"], "name": accs.get(p["a"], {}).get("name", p["a"]),
                  "collections": colls_for(sa)},
            "b": {"account": p["b"], "name": accs.get(p["b"], {}).get("name", p["b"]),
                  "collections": colls_for(sb)},
        }
        if not discovered:
            out["log"] = "\n".join(lines[-20:])
        return out
