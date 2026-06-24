"""Probe what a target server's CardDAV will actually accept.

Google's CardDAV often rejects vCard *creates* with a bare 400 INVALID_ARGUMENT
even for minimal valid cards (SyncGene works because it uses Google's People
API, not CardDAV). This tries a handful of create variations against the target
using vdirsyncer's own storage (correct OAuth + resolved collection URL) and
reports which, if any, the server accepts. Any probe item that gets created is
deleted again.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import aiohttp

import enrich

log = logging.getLogger("cacs.diagnose")


def _vcard(uid: str, lines: list[str]) -> str:
    body = "".join(l + "\r\n" for l in lines)
    return f"BEGIN:VCARD\r\nVERSION:3.0\r\n{body}UID:{uid}\r\nEND:VCARD\r\n"


def _contact_variations(uid_prefix: str) -> list[tuple[str, str]]:
    def u(n):
        return f"{uid_prefix}-{n}"
    return [
        ("as Apple sends it (PRODID + TEL type=pref)",
         _vcard(u(1), ["PRODID:-//Apple Inc.//iOS 26.0//EN", "N:Probe;CaCs;;;",
                       "FN:CaCs Probe", "TEL;type=CELL;type=VOICE;type=pref:0155 0000000",
                       "REV:2025-01-01T00:00:00Z"])),
        ("barest (N + FN only)",
         _vcard(u(2), ["N:Probe;CaCs;;;", "FN:CaCs Probe"])),
        ("RFC 3.0 TEL (TYPE=CELL,VOICE,PREF)",
         _vcard(u(3), ["N:Probe;CaCs;;;", "FN:CaCs Probe",
                       "TEL;TYPE=CELL,VOICE,PREF:+490000000"])),
        ("simple TEL, no PRODID/REV",
         _vcard(u(4), ["N:Probe;CaCs;;;", "FN:CaCs Probe", "TEL:+490000000"])),
        ("with EMAIL + ORG",
         _vcard(u(5), ["N:Probe;CaCs;;;", "FN:CaCs Probe", "ORG:CaCs;",
                       "EMAIL;TYPE=INTERNET:probe@example.com"])),
    ]


async def probe_target(store, pair_id: str, collection: str) -> dict[str, Any]:
    cfg = store.get()
    pair = cfg["pairs"].get(pair_id)
    if not pair:
        return {"error": "unknown pair"}
    svc = pair.get("service", "contacts")
    if svc != "contacts":
        return {"error": "probe currently only diagnoses contact (CardDAV) targets"}
    accs = cfg["accounts"]

    # probe the Google side — that's where creates are rejected; else side B
    side = next((k for k in ("a", "b")
                 if accs.get(pair.get(k), {}).get("kind") == "google"), "b")
    acc_id = pair[side]
    acc = accs.get(acc_id, {})
    if acc.get("kind") == "google":
        if not os.path.exists(os.path.join(enrich.TOKEN_DIR, f"acc_{acc_id}_card.token")):
            return {"error": "Connect Google first"}
        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

    deltas = enrich._collection_deltas(pair_id)
    if not deltas:
        return {"error": "Run 'Load collections' first (no discovery cache)"}
    delta = deltas.get(collection, ({}, {}))[0 if side == "a" else 1]
    conf = enrich.build_collection_config(acc_id, acc, svc, delta)
    if not conf:
        return {"error": "no resolved collection URL — load collections again"}

    try:
        from vdirsyncer.cli.utils import storage_instance_from_config
        from vdirsyncer.vobject import Item
    except Exception:
        return {"error": "vdirsyncer API unavailable"}

    results: list[dict[str, Any]] = []
    async with aiohttp.TCPConnector(limit_per_host=2) as conn:
        try:
            storage = await storage_instance_from_config(conf, create=False, connector=conn)
        except Exception as exc:
            return {"error": f"could not open target storage: {exc}"}
        for label, vcard in _contact_variations("cacs-probe-" + uuid.uuid4().hex[:10]):
            try:
                href, etag = await storage.upload(Item(vcard))
                entry = {"label": label, "ok": True, "detail": "accepted ✓ (created)"}
                try:
                    await storage.delete(href, etag or "")
                except Exception:
                    entry["detail"] = "accepted ✓ (created; could not auto-delete — remove 'CaCs Probe' manually)"
                results.append(entry)
            except Exception as exc:
                results.append({"label": label, "ok": False, "detail": str(exc)[:200]})
    return {"target": acc.get("name", acc_id), "side": side, "results": results}
