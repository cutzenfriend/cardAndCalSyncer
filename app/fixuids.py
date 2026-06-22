"""Rewrite over-long item UIDs that iCloud (and some servers) reject.

iCloud returns 404 on PUT for events whose UID is too long (e.g. 64-char
Outlook/Exchange GlobalObjectIds), and vdirsyncer aborts the whole collection
on the first such failure. vdirsyncer's own `repair --repair-unsafe-uid` only
checks for unsafe *characters*, not length, so it doesn't help here.

This rewrites UIDs longer than a threshold on a chosen side to a short, safe
UID (vdirsyncer's generate_href) — delete old + upload new, exactly like
vdirsyncer's repair. A following sync then uploads them with iCloud-safe UIDs.
Read-only preview (execute=False) just counts + samples. Reuses the same
storage machinery as enrich/clear. Reliable for CalDAV; best-effort for Google.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

import enrich

log = logging.getLogger("cacs.fixuids")

DEFAULT_THRESHOLD = 50   # rewrite UIDs strictly longer than this
MAX_ITEMS = 10000


async def run_fix(store, pair_id: str, side: str, collection: str | None,
                  threshold: int, execute: bool) -> dict[str, Any]:
    cfg = store.get()
    pair = cfg["pairs"].get(pair_id)
    if not pair:
        raise ValueError("Unknown pair")
    svc = pair.get("service", "calendar")
    accs = cfg["accounts"]
    acc_id = pair["a"] if side == "a" else pair["b"]
    acc = accs.get(acc_id, {})
    if not pair.get("collections"):
        raise ValueError("No mappings yet — add mappings and load collections first")
    deltas = enrich._collection_deltas(pair_id)
    if not deltas:
        raise ValueError("Run 'Load collections' first (no discovery cache)")
    if acc.get("kind") == "google":
        suf = "cal" if svc == "calendar" else "card"
        if not os.path.exists(os.path.join(enrich.TOKEN_DIR, f"acc_{acc_id}_{suf}.token")):
            raise ValueError("Connect Google first")
        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

    threshold = int(threshold) if threshold else DEFAULT_THRESHOLD

    from vdirsyncer.cli.utils import storage_instance_from_config
    from vdirsyncer.utils import generate_href

    count = fixed = processed = 0
    samples: list[str] = []
    errors: list[str] = []

    async with aiohttp.TCPConnector(limit_per_host=4) as conn:
        for mapping in pair["collections"]:
            short = mapping[0]
            if collection and short != collection:
                continue
            delta = deltas.get(short, ({}, {}))[0 if side == "a" else 1]
            conf = enrich.build_collection_config(acc_id, acc, svc, delta)
            if not conf:
                errors.append(f"{short}: no resolved collection URL — load collections again")
                continue
            try:
                storage = await storage_instance_from_config(conf, create=False, connector=conn)
                async for href, _etag in storage.list():
                    if processed >= MAX_ITEMS:
                        break
                    processed += 1
                    try:
                        item, etag = await storage.get(href)
                    except Exception as exc:
                        errors.append(f"{short}: read failed: {exc}")
                        continue
                    uid = item.uid or ""
                    if len(uid) <= threshold:
                        continue
                    count += 1
                    if len(samples) < 8:
                        title, _ = enrich.parse_item(item.raw)
                        samples.append((title or uid)[:60])
                    if execute:
                        try:
                            new_item = item.with_uid(generate_href())
                            await storage.upload(new_item)
                            await storage.delete(href, etag)
                            fixed += 1
                        except Exception as exc:
                            errors.append(f"{short}: rewrite failed ({uid[:16]}…): {exc}")
            except Exception as exc:
                errors.append(f"{short}: {exc}")
                continue

    return {"count": count, "fixed": fixed, "samples": samples, "errors": errors,
            "account": acc.get("name", acc_id), "side": side,
            "collection": collection or "all", "threshold": threshold}
