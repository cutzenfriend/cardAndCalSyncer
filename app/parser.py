"""Parser for vdirsyncer output (v0.20, click_log).

vdirsyncer logs INFO lines without a prefix to stderr, e.g.:
    Syncing calendars/work
    Copying (uploading) item <uid> to google_calendar/work
    Copying (updating) item <uid> to icloud_calendar/work
    Deleting item <uid> from google_calendar/work
Errors/warnings get a level prefix from click_log:
    error: ...
    warning: ...

Discover prints one block per storage:
    icloud_calendar:
      - "11111111-..." ("Work")
      - "domenik@gmail.com" ("Personal")
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# --- sync ------------------------------------------------------------------

_COPY = re.compile(
    r"Copying \((?P<op>uploading|updating)\) item (?P<ident>.+?) to (?P<storage>\S+)\s*$"
)
_DELETE = re.compile(r"Deleting item (?P<ident>.+?) from (?P<storage>\S+)\s*$")
_SYNCING = re.compile(r"Syncing (?P<status>\S+)\s*$")
_LEVEL = re.compile(r"^(?P<level>error|warning|critical):\s*(?P<msg>.*)$", re.I)

_OP_TO_ACTION = {"uploading": "create", "updating": "update"}


@dataclass
class Activity:
    action: str          # create | update | delete
    ident: str           # item UID
    dest_storage: str     # instance_name of the target side, e.g. "google_calendar"
    collection: str       # short name, e.g. "work"
    raw: str


@dataclass
class ParsedRun:
    activities: list[Activity] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    synced_collections: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        c = {"create": 0, "update": 0, "delete": 0}
        for a in self.activities:
            c[a.action] = c.get(a.action, 0) + 1
        return c


def _activity_from_storage(action: str, ident: str, storage: str, raw: str) -> Activity:
    if "/" in storage:
        name, _, coll = storage.partition("/")
    else:
        name, coll = storage, ""
    return Activity(action=action, ident=ident, dest_storage=name, collection=coll, raw=raw)


def parse_sync_line(line: str) -> Activity | None:
    """Check a single log line for a sync activity."""
    line = line.rstrip("\n")
    m = _COPY.search(line)
    if m:
        return _activity_from_storage(
            _OP_TO_ACTION[m.group("op")], m.group("ident"), m.group("storage"), line
        )
    m = _DELETE.search(line)
    if m:
        return _activity_from_storage("delete", m.group("ident"), m.group("storage"), line)
    return None


def classify_line(line: str) -> tuple[str, str] | None:
    """Detect error/warning lines -> ('error'|'warning', msg)."""
    m = _LEVEL.match(line.strip())
    if m:
        lvl = m.group("level").lower()
        lvl = "error" if lvl == "critical" else lvl
        return lvl, m.group("msg")
    return None


def parse_sync_output(lines: list[str]) -> ParsedRun:
    run = ParsedRun()
    for line in lines:
        act = parse_sync_line(line)
        if act:
            run.activities.append(act)
            continue
        m = _SYNCING.search(line)
        if m:
            run.synced_collections.append(m.group("status"))
            continue
        lvl = classify_line(line)
        if lvl:
            (run.errors if lvl[0] == "error" else run.warnings).append(lvl[1])
    return run


# --- discover --------------------------------------------------------------

_DISC_HEADER = re.compile(r"^(?P<name>[A-Za-z0-9_.\-]+):\s*$")
_DISC_ITEM = re.compile(
    r'^\s*-\s+(?P<id>"(?:[^"\\]|\\.)*"|\S+?)(?:\s+\("(?P<name>.*)"\))?\s*$'
)


@dataclass
class DiscoveredCollection:
    ident: str
    displayname: str = ""


def parse_discover_output(lines: list[str], known_storages: set[str]) -> dict[str, list[DiscoveredCollection]]:
    """Discover output -> {storage_name: [DiscoveredCollection, ...]}.

    Only blocks whose header is a known storage name are kept (so any pair
    heading is ignored).
    """
    result: dict[str, list[DiscoveredCollection]] = {}
    current: str | None = None
    for raw in lines:
        line = raw.rstrip("\n")
        h = _DISC_HEADER.match(line)
        if h and h.group("name") in known_storages:
            current = h.group("name")
            result.setdefault(current, [])
            continue
        if current is None:
            continue
        m = _DISC_ITEM.match(line)
        if m:
            ident = m.group("id")
            if ident.startswith('"'):
                try:
                    ident = json.loads(ident)
                except ValueError:
                    ident = ident.strip('"')
            result[current].append(
                DiscoveredCollection(ident=ident, displayname=m.group("name") or "")
            )
        else:
            # a non-indented line ends the current block
            if line and not line.startswith(" "):
                current = None
    return result
