"""Benachrichtigungen via Apprise (E-Mail, Telegram, ntfy, Discord, ...)."""
from __future__ import annotations

import logging

log = logging.getLogger("calsync.alerts")

try:
    import apprise  # type: ignore
except Exception:  # pragma: no cover
    apprise = None


def notify(urls: list[str], title: str, body: str, *, kind: str = "info") -> bool:
    """Schickt eine Nachricht an alle konfigurierten Apprise-URLs.

    kind: info | success | warning | failure  (Mapping auf Apprise NotifyType)
    """
    if not urls:
        return False
    if apprise is None:
        log.warning("apprise nicht installiert – Alert unterdrueckt: %s", title)
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
        log.warning("keine gueltige Apprise-URL konfiguriert")
        return False
    try:
        return bool(ap.notify(
            title=title, body=body,
            notify_type=getattr(apprise.NotifyType, notify_type.upper(),
                                apprise.NotifyType.INFO),
        ))
    except Exception as exc:  # pragma: no cover
        log.exception("Alert fehlgeschlagen: %s", exc)
        return False
