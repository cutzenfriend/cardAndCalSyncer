"""Notifications via Apprise (email, Telegram, ntfy, Discord, ...)."""
from __future__ import annotations

import logging

log = logging.getLogger("cacs.alerts")

try:
    import apprise  # type: ignore
except Exception:  # pragma: no cover
    apprise = None


def notify(urls: list[str], title: str, body: str, *, kind: str = "info") -> bool:
    """Send a message to all configured Apprise URLs.

    kind: info | success | warning | failure  (mapped to Apprise NotifyType)
    """
    if not urls:
        return False
    if apprise is None:
        log.warning("apprise not installed – alert suppressed: %s", title)
        return False

    notify_type = {
        "success": "success", "warning": "warning",
        "failure": "failure", "info": "info",
    }.get(kind, "info")

    ap = apprise.Apprise()
    added = 0
    for url in urls:
        if url and ap.add(url):
            added += 1
    if not added:
        log.warning("no valid Apprise URL configured")
        return False
    try:
        return bool(ap.notify(
            title=title, body=body,
            notify_type=getattr(apprise.NotifyType, notify_type.upper(),
                                apprise.NotifyType.INFO),
        ))
    except Exception as exc:  # pragma: no cover
        log.exception("alert failed: %s", exc)
        return False
