"""Configuration store: the web UI is the source of truth.

Model:
  accounts[<id>]  = { name, kind, ... }        # kind: icloud | google | caldav
  pairs[<id>]     = { name, service, a, b, conflict_resolution, collections }
  auth            = { username, pw_salt, pw_hash, secret }
  alerts          = { apprise_urls, on_failure, on_recovery }

A storage is generated per (account, service); its name is deterministic (see
storage_name) and is exactly what shows up in the vdirsyncer logs.
"""
from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from typing import Any

# kind -> human-friendly default label (UI presets provide the rest)
KIND_LABEL = {"icloud": "iCloud", "google": "Google", "caldav": "CalDAV/CardDAV"}

# fields treated as secrets (never written to vdirsyncer.conf, only via env)
SECRET_FIELDS = {"password", "client_secret", "client_id"}

DEFAULT_CONFIG: dict[str, Any] = {
    "interval_seconds": 300,
    "sync_enabled": True,
    "dry_run": False,
    "base_url": "",  # optional public URL for OAuth redirects (e.g. https://cacs.example.com)
    "auth": {"username": "", "pw_salt": "", "pw_hash": "", "secret": ""},
    "accounts": {},
    "pairs": {},
    "alerts": {"apprise_urls": [], "on_failure": True, "on_recovery": True},
}


def storage_name(account_id: str, service: str) -> str:
    """Deterministic vdirsyncer storage name for (account, service)."""
    suf = "cal" if service == "calendar" else "card"
    return f"acc_{account_id}_{suf}"


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class ConfigStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._cfg = self._load()

    def _load(self) -> dict[str, Any]:
        cfg = deepcopy(DEFAULT_CONFIG)
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                cfg = _deep_merge(cfg, json.load(f))
        if os.environ.get("SYNC_INTERVAL"):
            try:
                cfg["interval_seconds"] = int(os.environ["SYNC_INTERVAL"])
            except ValueError:
                pass
        if not cfg["alerts"]["apprise_urls"] and os.environ.get("APPRISE_URLS"):
            cfg["alerts"]["apprise_urls"] = [
                u.strip() for u in os.environ["APPRISE_URLS"].split(",") if u.strip()
            ]
        return cfg

    def get(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._cfg)

    def save(self, new_cfg: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._cfg = _deep_merge(self._cfg, new_cfg)
            self._persist()
            return deepcopy(self._cfg)

    def replace(self, key: str, value: Any) -> None:
        """Replace a whole top-level key (e.g. accounts/pairs)."""
        with self._lock:
            self._cfg[key] = value
            self._persist()

    def _persist(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # --- derived helpers ---------------------------------------------------
    def public_view(self) -> dict[str, Any]:
        """Config for the UI: secrets masked, auth/secret removed."""
        cfg = self.get()
        for acc in cfg["accounts"].values():
            for fld in list(acc):
                if fld in SECRET_FIELDS:
                    acc[fld] = "__SET__" if acc[fld] else ""
        cfg.pop("auth", None)
        return cfg

    def account(self, acc_id: str) -> dict[str, Any] | None:
        return self.get()["accounts"].get(acc_id)

    def pair_storages(self, pair: dict[str, Any]) -> tuple[str, str]:
        svc = pair.get("service", "calendar")
        return storage_name(pair["a"], svc), storage_name(pair["b"], svc)

    def resolve_dest(self, dest_storage: str, collection: str | None = None) -> dict[str, Any] | None:
        """From an activity's destination storage, derive pair + source/target account.

        If `collection` (the mapping short name) is given, also resolve the real
        calendar/address-book display name from the pair's stored labels.
        """
        cfg = self.get()
        accs = cfg["accounts"]
        for pid, p in cfg["pairs"].items():
            sa, sb = self.pair_storages(p)
            if dest_storage == sa:
                dst, src = p["a"], p["b"]
            elif dest_storage == sb:
                dst, src = p["b"], p["a"]
            else:
                continue
            label = (p.get("labels", {}) or {}).get(collection) if collection else None
            return {
                "pair": p.get("name") or pid,
                "collection_label": label or collection,
                "dst_name": accs.get(dst, {}).get("name", dst),
                "dst_kind": accs.get(dst, {}).get("kind", "caldav"),
                "src_name": accs.get(src, {}).get("name", src),
                "src_kind": accs.get(src, {}).get("kind", "caldav"),
            }
        return None
