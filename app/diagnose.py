"""Probe what a target server's CardDAV will actually accept.

The first probe proved Google CardDAV *can* create contacts (every synthetic
variation was accepted) — so the real sync's 400s aren't about vCard content.
This probe takes one of the user's *real* source contacts and tries it on the
target (a) exactly as-is (real UID) and (b) with a fresh UID, plus a synthetic
control. If as-is is rejected but the fresh-UID copy is accepted, the failure is
a UID collision (the contact already exists on the target; Google returns 400
instead of 412). Any probe item created is cleaned up.
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


def _verdict(results: list[dict]) -> tuple[str, str]:
    real = next((r for r in results if r["label"].startswith("real")), None)
    fresh = next((r for r in results if "fresh UID" in r["label"]), None)
    if real and fresh and not real["ok"] and fresh["ok"]:
        return ("collision",
                "Your real contact is rejected, but the SAME contact with a fresh "
                "UID is accepted — so the data is fine. It's a UID collision: that "
                "UID still exists on Google, almost certainly in Google Contacts' "
                "Trash (deleted contacts linger ~30 days and keep their UIDs; Google "
                "then returns 400 for re-creating that UID). Fix: Google Contacts → "
                "Trash → Delete forever (empty the trash), then sync.")
    if real and real["ok"]:
        return ("works",
                "Your real contact uploaded fine on its own, so Google accepts it. "
                "The bulk failure is a UID collision — the contacts still exist on "
                "Google (very likely in Contacts' Trash). Empty Google Contacts' "
                "Trash (Delete forever), then sync.")
    if results and all(not r["ok"] for r in results):
        return ("blocked", "Even a fresh contact was rejected — create is blocked "
                           "for this account/collection.")
    return ("mixed", "Mixed results — see the details below.")


async def probe_target(store, pair_id: str, collection: str) -> dict[str, Any]:
    cfg = store.get()
    pair = cfg["pairs"].get(pair_id)
    if not pair:
        return {"error": "unknown pair"}
    svc = pair.get("service", "contacts")
    if svc != "contacts":
        return {"error": "probe currently only diagnoses contact (CardDAV) targets"}
    accs = cfg["accounts"]

    # target = the Google side (where creates are rejected); source = the other
    tkey = next((k for k in ("a", "b")
                 if accs.get(pair.get(k), {}).get("kind") == "google"), "b")
    skey = "a" if tkey == "b" else "b"
    tgt_id, src_id = pair[tkey], pair[skey]
    tgt, src = accs.get(tgt_id, {}), accs.get(src_id, {})

    if tgt.get("kind") == "google":
        if not os.path.exists(os.path.join(enrich.TOKEN_DIR, f"acc_{tgt_id}_card.token")):
            return {"error": "Connect Google first"}
        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

    deltas = enrich._collection_deltas(pair_id)
    if not deltas:
        return {"error": "Run 'Load collections' first (no discovery cache)"}
    pair_delta = deltas.get(collection, ({}, {}))
    tconf = enrich.build_collection_config(tgt_id, tgt, svc, pair_delta[0 if tkey == "a" else 1])
    sconf = enrich.build_collection_config(src_id, src, svc, pair_delta[0 if skey == "a" else 1])
    if not tconf:
        return {"error": "no resolved target collection URL — load collections again"}

    try:
        from vdirsyncer.cli.utils import storage_instance_from_config
        from vdirsyncer.vobject import Item
    except Exception:
        return {"error": "vdirsyncer API unavailable"}

    results: list[dict[str, Any]] = []
    sample = None
    async with aiohttp.TCPConnector(limit_per_host=2) as conn:
        try:
            tstor = await storage_instance_from_config(tconf, create=False, connector=conn)
        except Exception as exc:
            return {"error": f"could not open target storage: {exc}"}

        tests: list[tuple[str, Any]] = []
        # one real source contact, as-is and with a fresh UID
        if sconf:
            try:
                sstor = await storage_instance_from_config(sconf, create=False, connector=conn)
                async for href, _etag in sstor.list():
                    real, _ = await sstor.get(href)
                    sample = {"uid": real.uid,
                              "title": (enrich.parse_item(real.raw)[0] or "")}
                    tests.append(("real contact, exactly as the source has it (real UID)", real))
                    tests.append(("same real contact, fresh UID", real.with_uid(
                        "cacs-probe-" + uuid.uuid4().hex[:10])))
                    break
            except Exception as exc:
                results.append({"label": "read one source contact", "ok": False,
                                "detail": str(exc)[:200]})
        # synthetic control
        tests.append(("synthetic minimal contact (control)", Item(_vcard(
            "cacs-probe-" + uuid.uuid4().hex[:10], ["N:Probe;CaCs;;;", "FN:CaCs Probe"]))))

        for label, item in tests:
            try:
                href, etag = await tstor.upload(item)
                entry = {"label": label, "ok": True, "detail": "accepted ✓"}
                try:                                   # cleanup: need a real etag
                    if not etag:
                        _, etag = await tstor.get(href)
                    await tstor.delete(href, etag)
                except Exception as de:
                    entry["detail"] = f"accepted ✓ (left on target — couldn't delete: {str(de)[:80]})"
                results.append(entry)
            except Exception as exc:
                results.append({"label": label, "ok": False, "detail": str(exc)[:200]})

    verdict, explain = _verdict(results)
    return {"target": tgt.get("name", tgt_id), "verdict": verdict,
            "explain": explain, "sample": sample, "results": results}
