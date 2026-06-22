"""Erzeugt vdirsyncer.conf aus den Accounts/Paaren der UI-Config.

Pro (Account, Service) wird ein Storage generiert. Secrets landen NICHT in der
Datei, sondern gehen per Umgebungsvariable an den vdirsyncer-Subprozess.
"""
from __future__ import annotations

import json
import re
from typing import Any

from store import storage_name

STATUS_PATH = "/data/status/"
TOKEN_DIR = "/data"

ICLOUD_CAL_URL = "https://caldav.icloud.com/"
ICLOUD_CARD_URL = "https://contacts.icloud.com/"


def _val(v: Any) -> str:
    return json.dumps(v)


def _sec_var(account_id: str, field: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]", "_", account_id).upper()
    return f"CACS_SEC_{safe}_{field.upper()}"


def _storage_block(account_id: str, acc: dict[str, Any], service: str,
                   env: dict[str, str]) -> list[str]:
    name = storage_name(account_id, service)
    kind = acc.get("kind", "caldav")
    is_cal = service == "calendar"
    out = [f"[storage {name}]"]

    if kind == "google":
        out.append(f'type = {_val("google_calendar" if is_cal else "google_contacts")}')
        out.append(f'token_file = {_val(f"{TOKEN_DIR}/{name}.token")}')
        for fld in ("client_id", "client_secret"):
            var = _sec_var(account_id, fld)
            env[var] = acc.get(fld, "") or ""
            out.append(f'{fld}.fetch = ["command", "printenv", "{var}"]')
    else:
        out.append(f'type = {_val("caldav" if is_cal else "carddav")}')
        if kind == "icloud":
            url = ICLOUD_CAL_URL if is_cal else ICLOUD_CARD_URL
        else:  # generic caldav/carddav (inkl. Microsoft, Nextcloud, ...)
            url = acc.get("cal_url" if is_cal else "card_url", "") or ""
        out.append(f"url = {_val(url)}")
        out.append(f'username = {_val(acc.get("username", ""))}')
        var = _sec_var(account_id, "password")
        env[var] = acc.get("password", "") or ""
        out.append(f'password.fetch = ["command", "printenv", "{var}"]')
    out.append("")
    return out


def generate(cfg: dict[str, Any], *, only_pair: str | None = None,
             list_all: bool = False) -> tuple[str, dict[str, str]]:
    env: dict[str, str] = {}
    lines = ["# AUTOMATISCH GENERIERT von CaCs – nicht von Hand editieren.",
             "[general]", f'status_path = {_val(STATUS_PATH)}', ""]

    pairs = cfg["pairs"]
    if only_pair:
        pairs = {only_pair: pairs[only_pair]}
    accounts = cfg["accounts"]

    # benötigte (Account, Service)-Kombinationen einsammeln
    needed: dict[str, tuple[str, str]] = {}  # storage_name -> (account_id, service)
    for p in pairs.values():
        svc = p.get("service", "calendar")
        for aid in (p["a"], p["b"]):
            needed[storage_name(aid, svc)] = (aid, svc)

    for sname, (aid, svc) in needed.items():
        acc = accounts.get(aid)
        if acc:
            lines += _storage_block(aid, acc, svc, env)

    for pid, p in pairs.items():
        svc = p.get("service", "calendar")
        sa, sb = storage_name(p["a"], svc), storage_name(p["b"], svc)
        if list_all:
            colls: list[Any] = []
        else:
            colls = [[c[0], c[1], c[2]] for c in p.get("collections", []) if len(c) >= 3]
        lines += [
            f"[pair {pid}]",
            f"a = {_val(sa)}",
            f"b = {_val(sb)}",
            "collections = [" + ", ".join(_val(c) for c in colls) + "]",
            f'conflict_resolution = {_val(p.get("conflict_resolution", "a wins"))}',
            'metadata = ["displayname", "color"]' if svc == "calendar"
            else 'metadata = ["displayname"]',
            "",
        ]

    return "\n".join(lines) + "\n", env
