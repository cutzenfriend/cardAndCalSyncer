# CaCs · cardAndCalSyncer

Self-hosted, bidirectional sync for **calendars and contacts** between arbitrary
providers — a single Docker container with a **web UI**, an activity feed, logs
and alerts. An open-source alternative to services like SyncGene, with no
dependency on an external provider.

Under the hood it runs [vdirsyncer](https://github.com/pimutils/vdirsyncer); a
FastAPI service schedules the runs, generates the vdirsyncer configuration,
parses its output into a SQLite DB and shows everything in the browser.

## Features

- **Bidirectional or one-way** per pair (A→B / B→A, where the source side is kept read-only), any number of calendars/address books, mappable in the UI
- Supported providers:
  - **iCloud** (CalDAV/CardDAV, app-specific password → full write access)
  - **Google** (Google's CalDAV & CardDAV APIs, OAuth2)
  - **Microsoft / Outlook** and **any CalDAV/CardDAV server** (Nextcloud, Fastmail, mailbox.org, your own server …)
- **Web UI** (port 8080): dashboard, activity (what/when/where/source→target), runs, live logs
- **Fully configurable in the UI**: accounts, pairs, discovery, mappings, interval
- **Login** with "stay signed in" and a guided first-time setup
- **Alerts** via [Apprise](https://github.com/caronc/apprise) (email, ntfy, Telegram, Discord …)
- **Healthcheck**, logs in `docker logs` and a file, bind mounts, non-root container

## Quick start

The image is published on Docker Hub as
[`cutzenfriend/cardandcalsyncer`](https://hub.docker.com/r/cutzenfriend/cardandcalsyncer).
No `.env` file is needed — all settings live directly in the compose file.

```sh
# grab the compose file
curl -O https://raw.githubusercontent.com/cutzenfriend/cardAndCalSyncer/main/docker-compose.yml

# create the bind-mount directories (container runs as uid 1000)
sudo mkdir -p /opt/docker-volumes/cacs/{config,data,logs}
sudo chown -R 1000:1000 /opt/docker-volumes/cacs

docker compose up -d
```

Then open **http://<host>:8080** → create an admin account during first-time
setup → add accounts → create pairs → "Load collections" → map them →
"Save pair". Done.

### `docker-compose.yml`

```yaml
services:
  cacs:
    image: cutzenfriend/cardandcalsyncer:latest
    container_name: cacs
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      SYNC_INTERVAL: "300"        # seconds, changeable in the UI
      LOG_LEVEL: "INFO"
      # Optional admin bootstrap (otherwise first-time setup at /setup):
      # ADMIN_USERNAME: "admin"
      # ADMIN_PASSWORD: "change-me"
      # Optional alerts (comma-separated Apprise URLs):
      # APPRISE_URLS: "ntfy://ntfy.sh/my-topic"
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

To update: `docker compose pull && docker compose up -d`.

## Setting up providers

| Provider | What you need |
|---|---|
| **iCloud** | Apple ID + **app-specific password** (appleid.apple.com → Sign-In & Security; 2FA required) |
| **Google** | OAuth client of type **"Web application"** from the Google Cloud Console; paste Client ID + secret, then click **"Connect Google"** — one browser consent, no CLI (see below) |
| **Microsoft/Outlook** | Only if the account allows CalDAV/CardDAV — server URL + credentials. **Microsoft 365 / Graph is not supported** (vdirsyncer doesn't speak Graph) |
| **CalDAV/CardDAV** (Nextcloud, Fastmail, your own server) | Server URL(s) + username/password (app password if applicable) |

### Connecting Google (one click)

1. In the [Google Cloud Console](https://console.cloud.google.com), **enable the
   APIs vdirsyncer uses** (these are *not* the "Calendar API" / "People API"):
   - **CalDAV API** — <https://console.cloud.google.com/apis/library/caldav.googleapis.com>
   - **CardDAV API** (only for contacts) — <https://console.cloud.google.com/apis/library/carddav.googleapis.com>
2. Create an OAuth client of type **Web application**.
3. Add an **Authorized redirect URI** — CaCs shows the exact value to use in the
   account dialog. It is `<base-url>/oauth/google/callback`.
4. In CaCs: add a Google account, paste **Client ID** + **Client secret**, save,
   reopen it and click **Connect Google**. Approve the consent screen — done.
   CaCs stores the token itself; no CLI, no copying files.

> If discovery returns **0 Google collections** with `accessNotConfigured` in the
> details, you skipped step 1 — enable the CalDAV (and CardDAV) API, wait a minute,
> and retry.

> **Redirect URI rule (Google):** Google only accepts redirect URIs that are
> `https://…` or `http://localhost` / `http://127.0.0.1`. A **LAN IP**
> (`http://192.168.x.x:port`) is **always rejected** — so don't register that.

**Local / LAN setup (no domain, no HTTPS, no tunnel):**

1. In *Configuration → General*, set **Public base URL** to
   `http://localhost:<the host port you published>` (e.g. `http://localhost:9119`) and save.
2. Register exactly `http://localhost:9119/oauth/google/callback` in your Google
   "Web application" OAuth client (the account dialog shows the exact URI).
3. Add the Google account, paste Client ID + secret, save, reopen, click **Connect Google**.
   A new tab opens Google's consent screen.
4. After approving, your browser is redirected to that `localhost` URL. If CaCs
   isn't reachable at `localhost` from that browser, the tab shows a "can't
   connect" page — that's expected. **Copy the full address from the address bar**
   and paste it into the **Finish connection** box. Done.

(If you browse CaCs from the same host, or via an HTTPS reverse proxy whose URL
you put in Public base URL, the redirect completes automatically and you can skip
the copy-paste.) The token is refreshed automatically afterwards — one-time step.

## Interface

| Page | Content |
|---|---|
| **Dashboard** | status, next run, totals, recent runs & activity, manual sync, pause |
| **Activity** | each object: time · action (created/updated/deleted) · pair · **calendar name** · **source → target** · item (title + date when resolvable, else UID). Filter by action/pair/time window, and clear the log (all or older than N days) |
| **Runs** | every sync/discover with counts, status, rc and the full log |
| **Logs** | live tail of `vdirsyncer.log` |
| **Configuration** | accounts, sync pairs, discovery/mappings, interval, alerts, login |

## Notes & limitations

- **No true real-time** — iCloud CalDAV offers no push. CaCs polls on an
  interval (default 300 s, down to 30 s) and transfers only deltas via sync token.
- **Initial sync / duplicates:** vdirsyncer matches by UID, so duplicates only
  happen when the *same* event has *different* UIDs on each side. If you do get
  duplicates, the reliable fix is to set the pair to **one-way (A→B)**, use
  **Configuration → pair → Clear B…** to empty the target side (preview shows the
  count; optional "only items newer than N months"), then sync once so B mirrors
  A. Clearing a CalDAV side (iCloud/Nextcloud) is solid; clearing **Google** is
  best-effort. To just validate a setup without changing anything, use **Load
  collections** (discovery is read-only).
- **No date filter during sync:** vdirsyncer syncs whole collections, so an
  ongoing "ignore events older than X months" is **not possible**. The only
  date-based control is the one-time "newer than N months" option in the Clear tool.
- **Object titles:** vdirsyncer only logs an item's UID, so CaCs fetches the
  changed item's content from a server after each sync (best-effort) to show the
  real title + date. Deletes (the item is gone) and anything it can't fetch fall
  back to showing the UID.
- **Both sides must exist** — CaCs does not create new calendars, it maps existing ones.
- **Secrets** live only in `/data/cacs.json` (0600) and are passed to the
  subprocess via environment variables — **never** in the generated `vdirsyncer.conf`.

## Environment variables

Set these in the `environment:` block of your compose file (all optional):

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

## Build from source

```sh
git clone https://github.com/cutzenfriend/cardAndCalSyncer.git
cd cardAndCalSyncer
docker build -t cutzenfriend/cardandcalsyncer:latest .
# maintainer: publish to Docker Hub
docker push cutzenfriend/cardandcalsyncer:latest
```

A multi-arch build (amd64/arm64):

```sh
docker buildx build --platform linux/amd64,linux/arm64 \
  -t cutzenfriend/cardandcalsyncer:latest --push .
```

## License

MIT

## A note on AI

This project was built with the help of AI. If that bothers you, feel free not
to use it. :)
