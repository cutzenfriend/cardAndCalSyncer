# CaCs · cardAndCalSyncer

Selbst gehosteter, bidirektionaler Sync für **Kalender und Kontakte** zwischen
beliebigen Anbietern – als ein einziger Docker-Container mit **Web-UI**,
Aktivitäts-Feed, Logs und Alerts. Eine quelloffene Alternative zu Diensten wie
SyncGene, unabhängig von externen Anbietern.

Unter der Haube läuft [vdirsyncer](https://github.com/pimutils/vdirsyncer); ein
FastAPI-Dienst plant die Läufe, generiert die vdirsyncer-Konfiguration, parst
deren Ausgabe in eine SQLite-DB und zeigt alles im Browser.

## Features

- **Bidirektional**, beliebig viele Kalender/Adressbücher, im UI mappbar
- Unterstützte Anbieter:
  - **iCloud** (CalDAV/CardDAV, App-spezifisches Passwort → voller Schreibzugriff)
  - **Google** (offizielle Calendar API + People API, OAuth2)
  - **Microsoft / Outlook** sowie **jeder CalDAV/CardDAV-Server** (Nextcloud, Fastmail, mailbox.org, eigener Server …)
- **Web-UI** (Port 8080): Dashboard, Aktivität (was/wann/wo/Quelle→Ziel), Läufe, Live-Logs
- **Komplett im UI konfigurierbar**: Accounts, Paare, Discover, Mappings, Intervall
- **Login** mit „angemeldet bleiben" und geführter Ersteinrichtung
- **Alerts** via [Apprise](https://github.com/caronc/apprise) (E-Mail, ntfy, Telegram, Discord …)
- **Healthcheck**, Logs in `docker logs` und Datei, Bind-Mounts, non-root Container

## Schnellstart

```sh
git clone https://github.com/cutzenfriend/cardAndCalSyncer.git
cd cardAndCalSyncer
cp .env.example .env            # optional; alles geht auch im UI

sudo mkdir -p /opt/docker-volumes/cacs/{config,data,logs}
sudo chown -R 1000:1000 /opt/docker-volumes/cacs

docker compose up -d --build
```

Dann **http://<host>:8080** öffnen → bei der Ersteinrichtung ein Admin-Konto
anlegen → Accounts hinzufügen → Paare anlegen → „Collections laden" → mappen →
„Paar speichern". Fertig.

### Beispiel `docker-compose.yml`

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
      SYNC_INTERVAL: "300"        # Sekunden, im UI änderbar
      # ADMIN_USERNAME / ADMIN_PASSWORD optional für headless-Bootstrap
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

## Anbieter einrichten

| Anbieter | Was du brauchst |
|---|---|
| **iCloud** | Apple-ID + **App-spezifisches Passwort** (appleid.apple.com → Anmeldung & Sicherheit; 2FA nötig) |
| **Google** | OAuth-Client **Typ „Desktop"** aus der Google Cloud Console; APIs „Calendar" + „People" aktivieren. OAuth einmalig autorisieren (siehe unten) |
| **Microsoft/Outlook** | Nur falls der Account CalDAV/CardDAV erlaubt – Server-URL + Zugangsdaten. **Microsoft 365 / Graph wird nicht unterstützt** (vdirsyncer spricht kein Graph) |
| **CalDAV/CardDAV** (Nextcloud, Fastmail, eigener Server) | Server-URL(s) + Benutzer/Passwort (ggf. App-Passwort) |

> **Google-OAuth (einmalig):** Der Flow erwartet einen `localhost`-Redirect, was
> headless im Container umständlich ist. Einfachster Weg: vdirsyncer einmal auf
> einem Desktop mit Browser autorisieren (`pipx install "vdirsyncer[google]"`,
> gleiche Client-ID), das erzeugte Token nach `/opt/docker-volumes/cacs/data/`
> kopieren (als `acc_<id>_cal.token` / `acc_<id>_card.token`, Eigentümer uid 1000).

## Oberfläche

| Seite | Inhalt |
|---|---|
| **Dashboard** | Status, nächster Lauf, Summen, letzte Läufe & Aktivität, manueller Sync, Pausieren |
| **Aktivität** | jedes Objekt: Zeit · Aktion (erstellt/geändert/gelöscht) · Paar · Kalender · **Quelle → Ziel** · UID |
| **Läufe** | jeder Sync/Discover mit Counts, Status, rc und vollem Log |
| **Logs** | Live-Tail der `vdirsyncer.log` |
| **Konfiguration** | Accounts, Sync-Paare, Discover/Mappings, Intervall, Alerts, Login |

## Hinweise & Grenzen

- **Kein echtes Realtime** – iCloud-CalDAV bietet kein Push. CaCs pollt im
  Intervall (Standard 300 s, ab 30 s) und überträgt per Sync-Token nur Deltas.
- **Erst-Sync/Duplikate:** vdirsyncer matcht per UID. Existieren dieselben
  Termine schon auf beiden Seiten, erst mit **einem** Kalender testen.
- **Objekt-Titel:** Der Feed zeigt die UID (mehr loggt vdirsyncer nicht), dazu
  Richtung, Kalender und Zeitpunkt.
- **Beide Seiten müssen existieren** – CaCs legt keine neuen Kalender an, es mappt vorhandene.
- **Secrets** liegen ausschließlich in `/data/cacs.json` (0600) und gehen per
  Umgebungsvariable an den Subprozess – **nie** in die generierte `vdirsyncer.conf`.

## Umgebungsvariablen

| Variable | Zweck |
|---|---|
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | optionales Admin-Bootstrap (sonst `/setup`) |
| `APPRISE_URLS` | komma-getrennte Alert-Ziele |
| `SYNC_INTERVAL` | Start-Intervall in Sekunden (im UI änderbar) |
| `LOG_LEVEL` | `INFO` (Standard) / `DEBUG` |

## Architektur

```
┌────────────────────────── cacs (1 Container) ───────────────────────────┐
│  FastAPI ──Scheduler──▶ vdirsyncer (Subprozess) ──CalDAV/CardDAV/API──▶  │
│   │  │                       │ stdout/stderr        iCloud · Google ·     │
│   │  └─ Web-UI :8080         ▼                       Microsoft · DAV …    │
│   └─ SQLite (Läufe/Aktivität) ◀─ Log-Parser ─ generierte vdirsyncer.conf  │
└───────────────────────────────────────────────────────────────────────────┘
   /config  generierte conf     /data  DB · Config · OAuth-Token · Status     /logs
```

## Lizenz

MIT
