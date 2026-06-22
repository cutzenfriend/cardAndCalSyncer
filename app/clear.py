"""Destructive helper: empty one side's mapped collections before a first sync,
to avoid duplicates (then mirror from the other side, e.g. with direction A->B).

Reuses vdirsyncer's storages (list/get/delete). Reliable for CalDAV/CardDAV;
best-effort for Google. Supports an optional 'only items newer than N months'
window. Preview (execute=False) counts + samples without deleting.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, timedelta
from typing import Any

import aiohttp

import enrich

log = logging.getLogger("cacs.clear")

MAX_ITEMS = 10000  # safety cap


def _item_date(raw: str) -> date | None:
    v = enrich._field(enrich._unfold(raw), "DTSTART")
    if not v:
        return None
    m = re.match(r"(\d{4})(\d{2})(\d{2})", v)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


async def run_clear(store, pair_id: str, side: str, months: int,
                    execute: bool, collection: str | None = None) -> dict[str, Any]:
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

    # the "newer than N months" window only makes sense for calendars (vCards
    # have no date) — ignore it for contacts and clear everything.
    cutoff = (date.today() - timedelta(days=30 * months)
              if months and months > 0 and svc == "calendar" else None)

    from vdirsyncer.cli.utils import storage_instance_from_config

    count = deleted = 0
    samples: list[str] = []
    errors: list[str] = []
    processed = 0

    async with aiohttp.TCPConnector(limit_per_host=4) as conn:
        for mapping in pair["collections"]:
            short = mapping[0]
            if collection and short != collection:
                continue  # only the chosen calendar/address book
            delta = deltas.get(short, ({}, {}))[0 if side == "a" else 1]
            conf = enrich.build_collection_config(acc_id, acc, svc, delta)
            if not conf:
                errors.append(f"{short}: no resolved collection URL — load collections again")
                continue
            try:
                storage = await storage_instance_from_config(conf, create=False, connector=conn)
                items = await storage.list()
            except Exception as exc:
                errors.append(f"{short}: {exc}")
                continue

            for href, etag in items:
                if processed >= MAX_ITEMS:
                    break
                processed += 1
                if cutoff is None:
                    # no date window: every item qualifies
                    count += 1
                    if not execute and len(samples) < 8:
                        try:
                            it, _ = await storage.get(href)
                            t, _ = enrich.parse_item(it.raw)
                            if t:
                                samples.append(t)
                        except Exception:
                            pass
                    if execute:
                        try:
                            await storage.delete(href, etag)
                            deleted += 1
                        except Exception as exc:
                            errors.append(f"{short}: delete failed: {exc}")
                else:
                    # date window: keep old/undateable, act on recent (>= cutoff)
                    try:
                        it, _ = await storage.get(href)
                        d = _item_date(it.raw)
                        t, _ = enrich.parse_item(it.raw)
                    except Exception:
                        continue  # can't evaluate -> keep (safe)
                    if d is None or d < cutoff:
                        continue
                    count += 1
                    if t and len(samples) < 8:
                        samples.append(t)
                    if execute:
                        try:
                            await storage.delete(href, etag)
                            deleted += 1
                        except Exception as exc:
                            errors.append(f"{short}: delete failed: {exc}")

    return {"count": count, "deleted": deleted, "samples": samples,
            "errors": errors, "account": acc.get("name", acc_id), "side": side,
            "collection": collection or "all"}
