# CaCs · cardAndCalSyncer

Self-hosted, bidirectional sync for **calendars and contacts** between arbitrary
providers — a single Docker container with a **web UI**, an activity feed, logs
and alerts. An open-source alternative to services like SyncGene, with no
dependency on an external provider.

Under the hood it runs [vdirsyncer](https://github.com/pimutils/vdirsyncer); a
FastAPI service schedules the runs, generates the vdirsyncer configuration,
parses its output into a SQLite DB and shows everything in the browser.

## Features

- **Bidirectional**, any number of calendars/address books, mappable in the UI
- Supported providers:
  - **iCloud** (CalDAV/CardDAV, app-specific password → full write access)
  - **Google** (official Calendar API + People API, OAuth2)
  - **Microsoft / Outlook** and **any CalDAV/CardDAV server** (Nextcloud, Fastmail, mailbox.org, your own server …)
- **Web UI** (port 8080): dashboard, activity (what/when/where/source→target), runs, live logs
- **Fully configurable in the UI**: accounts, pairs, discovery, mappings, interval
- **Login** with "stay signed in" and a guided first-time setup
- **Alerts** via [Apprise](https://github.com/caronc/apprise) (email, ntfy, Telegram, Discord …)
- **Healthcheck**, logs in `docker logs` and a file, bind mounts, non-root container

## Quick start

```sh
git clone https://github.com/cutzenfriend/cardAndCalSyncer.git
cd cardAndCalSyncer
cp .env.example .env            # optional; everything also works in the UI

sudo mkdir -p /opt/docker-volumes/cacs/{config,data,logs}
sudo chown -R 1000:1000 /opt/docker-volumes/cacs

docker compose up -d --build
```

Then open **http://<host>:8080** → create an admin account during first-time
setup → add accounts → create pairs → "Load collections" → map them →
"Save pair". Done.

### Example `docker-compose.yml`

```yaml
services:
  cacs:
    image: cacs:local
    build: .
    container_name: cacs
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      SYNC_INTERVAL: "300"        # seconds, changeable in the UI
      # ADMIN_USERNAME / ADMIN_PASSWORD optional for a headless bootstrap
    volumes:
      - /opt/docker-volumes/cacs/config:/config
      - /opt/docker-volumes/cacs/data:/data
      - /opt/docker-volumes/cacs/logs:/logs
    healthcheck:
      test: ["CMD","python","-c","import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health',timeout=3).status==200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 3
```

## Setting up providers

| Provider | What you need |
|---|---|
| **iCloud** | Apple ID + **app-specific password** (appleid.apple.com → Sign-In & Security; 2FA required) |
| **Google** | OAuth client of type **"Desktop"** from the Google Cloud Console; enable the "Calendar" and "People" APIs. Authorize OAuth once (see below) |
| **Microsoft/Outlook** | Only if the account allows CalDAV/CardDAV — server URL + credentials. **Microsoft 365 / Graph is not supported** (vdirsyncer doesn't speak Graph) |
| **CalDAV/CardDAV** (Nextcloud, Fastmail, your own server) | Server URL(s) + username/password (app password if applicable) |

> **Google OAuth (one-time):** the flow expects a `localhost` redirect, which is
> awkward headless inside a container. Easiest path: authorize vdirsyncer once on
> a desktop with a browser (`pipx install "vdirsyncer[google]"`, same client ID),
> then copy the generated token into `/opt/docker-volumes/cacs/data/`
> (as `acc_<id>_cal.token` / `acc_<id>_card.token`, owner uid 1000).

## Interface

| Page | Content |
|---|---|
| **Dashboard** | status, next run, totals, recent runs & activity, manual sync, pause |
| **Activity** | each object: time · action (created/updated/deleted) · pair · collection · **source → target** · UID |
| **Runs** | every sync/discover with counts, status, rc and the full log |
| **Logs** | live tail of `vdirsyncer.log` |
| **Configuration** | accounts, sync pairs, discovery/mappings, interval, alerts, login |

## Notes & limitations

- **No true real-time** — iCloud CalDAV offers no push. CaCs polls on an
  interval (default 300 s, down to 30 s) and transfers only deltas via sync token.
- **Initial sync / duplicates:** vdirsyncer matches by UID. If the same events
  already exist on both sides, test with **one** calendar first.
- **Object titles:** the feed shows the UID (that's all vdirsyncer logs), plus
  direction, collection and timestamp.
- **Both sides must exist** — CaCs does not create new calendars, it maps existing ones.
- **Secrets** live only in `/data/cacs.json` (0600) and are passed to the
  subprocess via environment variables — **never** in the generated `vdirsyncer.conf`.

## Environment variables

| Variable | Purpose |
|---|---|
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | optional admin bootstrap (otherwise `/setup`) |
| `APPRISE_URLS` | comma-separated alert targets |
| `SYNC_INTERVAL` | start interval in seconds (changeable in the UI) |
| `LOG_LEVEL` | `INFO` (default) / `DEBUG` |

## Architecture

```
┌────────────────────────── cacs (1 container) ───────────────────────────┐
│  FastAPI ──scheduler──▶ vdirsyncer (subprocess) ──CalDAV/CardDAV/API──▶  │
│   │  │                       │ stdout/stderr        iCloud · Google ·     │
│   │  └─ web UI :8080         ▼                       Microsoft · DAV …    │
│   └─ SQLite (runs/activity) ◀─ log parser ─ generated vdirsyncer.conf     │
└───────────────────────────────────────────────────────────────────────────┘
   /config  generated conf      /data  DB · config · OAuth tokens · status     /logs
```

## License

MIT
