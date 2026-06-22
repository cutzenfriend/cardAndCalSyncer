"""Best-effort enrichment: fetch changed items' content after a sync and parse
a human title + date for the activity feed.

vdirsyncer only logs an item's UID, never its title. So we look up the UID's
href in vdirsyncer's status DB, fetch the item from a server (reusing
vdirsyncer's own storage), and parse SUMMARY/DTSTART (calendar) or FN (contacts).

We prefer fetching from a CalDAV/CardDAV side (e.g. iCloud/Nextcloud) and avoid
Google when possible — the content is identical on both sides after a sync.
Everything here is wrapped so it can never break a sync.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from typing import Any

import aiohttp

from store import storage_name

log = logging.getLogger("cacs.enrich")

STATUS_PATH = "/data/status"
MAX_ITEMS = 200          # cap work per run
ICLOUD_CAL_URL = "https://caldav.icloud.com/"
ICLOUD_CARD_URL = "https://contacts.icloud.com/"


# --- parsing (offline, fully testable) -------------------------------------
def _unfold(raw: str) -> str:
    return re.sub(r"\r?\n[ \t]", "", raw or "")


def _field(text: str, name: str) -> str | None:
    m = re.search(rf"(?im)^{name}(?:;[^:\r\n]*)?:(.*)$", text)
    return m.group(1).strip() if m else None


def _fmt_dt(v: str | None) -> str | None:
    if not v:
        return None
    m = re.match(r"(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2}))?", v)
    if not m:
        return v
    y, mo, d, hh, mi = m.groups()
    s = f"{y}-{mo}-{d}"
    return f"{s} {hh}:{mi}" if hh else s


def parse_item(raw: str) -> tuple[str | None, str | None]:
    """Return (title, subtitle) from an iCal/vCard payload."""
    text = _unfold(raw)
    if "BEGIN:VCARD" in text:
        return _field(text, "FN"), None
    title = _field(text, "SUMMARY")
    sub = _fmt_dt(_field(text, "DTSTART"))
    return title, sub


# --- status / cache readers ------------------------------------------------
def _hrefs(pair_id: str, collection: str, uid: str) -> tuple[str | None, str | None]:
    path = os.path.join(STATUS_PATH, pair_id, collection + ".items")
    if not os.path.exists(path):
        return (None, None)
    try:
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT href_a, href_b FROM status WHERE ident=?", (uid,)).fetchone()
        con.close()
        return (row["href_a"], row["href_b"]) if row else (None, None)
    except Exception:
        return (None, None)


def _collection_deltas(pair_id: str) -> dict[str, tuple[dict, dict]]:
    """{collection_short: (a_delta, b_delta)} from the collections cache."""
    path = os.path.join(STATUS_PATH, pair_id + ".collections")
    out: dict[str, tuple[dict, dict]] = {}
    try:
        with open(path) as f:
            data = json.load(f)
        for entry in data.get("collections", []):
            name, sides = entry[0], entry[1]
            out[name] = (sides[0] or {}, sides[1] or {})
    except Exception:
        pass
    return out


def build_collection_config(account_id: str, acc: dict[str, Any], service: str,
                            delta: dict) -> dict[str, Any] | None:
    """Reconstruct the resolved per-collection storage config (base + cache delta),
    mirroring what vdirsyncer built at discover time. Works for DAV and Google.
    """
    is_cal = service == "calendar"
    kind = acc.get("kind", "caldav")
    if kind == "google":
        base = {
            "type": "google_calendar" if is_cal else "google_contacts",
            "token_file": f"{TOKEN_DIR}/{storage_name(account_id, service)}.token",
            "client_id": acc.get("client_id", "") or "",
            "client_secret": acc.get("client_secret", "") or "",
        }
    elif kind == "icloud":
        base = {"type": "caldav" if is_cal else "carddav",
                "url": ICLOUD_CAL_URL if is_cal else ICLOUD_CARD_URL,
                "username": acc.get("username", "") or "",
                "password": acc.get("password", "") or ""}
    else:  # generic caldav/carddav (Microsoft, Nextcloud, ...)
        base = {"type": "caldav" if is_cal else "carddav",
                "url": acc.get("cal_url" if is_cal else "card_url", "") or "",
                "username": acc.get("username", "") or "",
                "password": acc.get("password", "") or ""}
    full = {**base, **(delta or {})}
    # need at least a resolved url (DAV) or a collection (Google) to address it
    if not full.get("url") and not full.get("collection"):
        return None
    return full


TOKEN_DIR = "/data"


def _dav_config(account_id: str, acc: dict[str, Any], service: str, delta: dict) -> dict[str, Any] | None:
    """Config for the title-fetch: DAV sides only (skip Google; same content)."""
    if acc.get("kind") == "google":
        return None
    return build_collection_config(account_id, acc, service, delta)


# --- main entry ------------------------------------------------------------
async def enrich(store, db, items: list[dict[str, Any]]) -> None:
    """items: [{activity_id, pair_id, collection, uid}]. Never raises."""
    if not items:
        return
    cfg = store.get()
    try:
        from vdirsyncer.cli.utils import storage_instance_from_config
    except Exception:
        log.warning("vdirsyncer storage API unavailable; skipping enrichment")
        return

    deltas_by_pair: dict[str, dict] = {}
    async with aiohttp.TCPConnector(limit_per_host=4) as conn:
        for it in items[:MAX_ITEMS]:
            try:
                pair_id = it["pair_id"]
                pair = cfg["pairs"].get(pair_id)
                if not pair:
                    continue
                svc = pair.get("service", "calendar")
                accs = cfg["accounts"]
                deltas = deltas_by_pair.setdefault(pair_id, _collection_deltas(pair_id))
                a_delta, b_delta = deltas.get(it["collection"], ({}, {}))
                href_a, href_b = _hrefs(pair_id, it["collection"], it["uid"])

                # prefer a DAV side (skip Google); content is identical post-sync
                candidates = [
                    (pair["a"], accs.get(pair["a"], {}), a_delta, href_a),
                    (pair["b"], accs.get(pair["b"], {}), b_delta, href_b),
                ]
                title = sub = None
                for acc_id, acc, delta, href in candidates:
                    sconf = _dav_config(acc_id, acc, svc, delta)
                    if not sconf or not href:
                        continue
                    storage = await storage_instance_from_config(sconf, create=False, connector=conn)
                    item, _etag = await storage.get(href)
                    title, sub = parse_item(item.raw)
                    if title:
                        break
                if title:
                    db.set_activity_detail(it["activity_id"], title, sub)
            except Exception:
                continue  # best-effort per item
