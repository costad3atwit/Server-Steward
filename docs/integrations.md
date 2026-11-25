# Integrations

Each integration is off until you set `enabled: true` under `integrations:` in `config/steward.yaml`. When one is enabled, the matching tool or topic is offered to the model; when it's off, the model never hears of it.

Most integrations need two more things:

1. **Network access.** The executor sits on an internal-only network, so it must join each app's Docker network. Copy `compose.local.example.yaml` to `compose.local.yaml`, keep the networks you use, and add it to `COMPOSE_FILE` in `.env`:

   ```
   COMPOSE_FILE=compose.yaml:compose.local.yaml        # plus compose.gpu.yaml if you use it
   ```

   Find an app's network with `docker network ls` or `docker inspect <container> --format '{{json .NetworkSettings.Networks}}'`. Inside a shared network, use the app's container name and its *internal* port, not the port published on the host.

2. **A credential** in `.env`, if the app needs one. Only the executor receives these.

Then run `docker compose up -d`.

| Integration | What you can ask | Network | Credential |
| --- | --- | --- | --- |
| [`immich`](#immich) | Job queue status, pause/resume (✅), storage per user | Immich's | `IMMICH_API_KEY` (admin) |
| [`nextcloud`](#nextcloud) | Storage per user | none | none |
| [`jellyfin`](#jellyfin) | Who's watching | Jellyfin's | `JELLYFIN_API_KEY` |
| [`audiobookshelf`](#audiobookshelf) | Who's listening | Audiobookshelf's | `AUDIOBOOKSHELF_TOKEN` (admin) |
| [`transmission`](#transmission) | Download progress | Transmission's (or its VPN's) | `TRANSMISSION_USER`/`_PASS` if it has a login |
| [`vpn`](#vpn) | VPN health, public IP, location | none | none |
| [`backup_log`](#backup-log) | Last backup result, last good backup | none (a file mount) | none |

## Testing an integration without Discord

You can call the executor directly from a throwaway container on its internal network. This bypasses the model, so it shows whether the integration itself works:

```bash
TOKEN=$(grep ^EXECUTOR_TOKEN= .env | cut -d= -f2)
act() { docker run --rm --network server-steward_steward-internal curlimages/curl -s \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "$1" \
  http://steward-executor:8080/v1/action; echo; }

act '{"verb":"status"}'
act '{"verb":"storage_usage","args":{"app":"immich"}}'
```

The network name is `<compose project>_steward-internal`; the project is `server-steward` unless you renamed the folder or set `COMPOSE_PROJECT_NAME`. Each section below lists its verb.

## Immich

Photo library. Needs an **admin** user's API key (Immich: **Account Settings → API Keys**); per-user storage needs the server statistics permission, which admin keys have.

```yaml
integrations:
  immich:
    enabled: true
    url: http://immich_server:2283      # Immich's container name and internal port
    api_style: jobs
    queues: [videoConversion, thumbnailGeneration, smartSearch, faceDetection]
    max_pause_hours: 48
```

- **Network:** Immich's compose network (often `immich_default`).
- **Queues** are Immich job names; only the ones listed can be paused. Pausing stops new jobs from starting; running jobs finish.
- **Pause with hours** schedules an automatic resume, kept across restarts. Requests longer than `max_pause_hours` are refused.
- `api_style: jobs` uses `PUT /api/jobs/{queue}`. If pausing returns HTTP 404, your Immich version uses a different jobs API: check `<your-immich>/api/docs` and open an issue.
- **Verbs:** `immich_jobs`, `immich_queue` (`action`, `queue`, `hours`), `storage_usage` with `app: immich`.

## Nextcloud

Per-user storage, read with Nextcloud's `occ` command inside its container (as `www-data`), so no admin password is needed.

```yaml
  nextcloud:
    enabled: true
    container: nextcloud-app-1           # the container that runs Nextcloud's PHP app
```

- **Network:** none; it uses the Docker API.
- Only `occ user:list` and `occ user:info <id>` are run, and a requested user is matched against the real user list first.
- **Verb:** `storage_usage` with `app: nextcloud`.

## Jellyfin

Active playback sessions: user, title, device, whether they're at home, on Tailscale or on the internet, paused, transcoding.

1. Jellyfin: **Dashboard → API Keys → +**, name it "server-steward".
2. `.env`: `JELLYFIN_API_KEY=...`

```yaml
  jellyfin:
    enabled: true
    url: http://jellyfin:8096
```

- **Network:** Jellyfin's.
- **Verb:** `streams` (shared with Audiobookshelf). The model reaches it as "get_status, topic streaming".

## Audiobookshelf

Open listening sessions, with progress through the book.

1. Audiobookshelf: copy an **admin** user's API token (Settings → Users → the user; newer versions: Settings → API Keys).
2. `.env`: `AUDIOBOOKSHELF_TOKEN=...`

```yaml
  audiobookshelf:
    enabled: true
    url: http://audiobookshelf:80        # internal port 80, whatever you publish on the host
```

- **Network:** Audiobookshelf's.
- Books played from a phone's downloaded copy don't create a server session, so they don't show.
- **Verb:** `streams`.

## Transmission

Torrent progress, speed and time left. Read only.

```yaml
  transmission:
    enabled: true
    url: http://transmission:9091/transmission/rpc
```

- **Network:** the network Transmission's web port is on. If Transmission shares a VPN container's network (`network_mode: service:gluetun`), use the VPN container's name: `http://gluetun:9091/transmission/rpc`, and join *its* network.
- **Login:** if the web UI asks for one, set `TRANSMISSION_USER` and `TRANSMISSION_PASS`. Quick check from the host: `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9091/transmission/rpc` gives 409 without a login and 401 with one.
- **Verb:** `torrent_list`.

## VPN

Health, public IP and location of a [Gluetun](https://github.com/qdm12/gluetun) VPN container, read from its own log (Gluetun's control server requires authentication in recent versions, so the log is simpler).

```yaml
  vpn:
    enabled: true
    container: gluetun
```

- **Network:** none.
- It looks for Gluetun's `Public IP address is ...` log line. Other VPN containers would need their own reader (see [adding-an-integration.md](adding-an-integration.md)).
- **Verb:** `vpn_status`.

## Backup log

The result of your backup script, read from its log file: the last run, and if it failed, when the last successful backup was, plus when each repository or copy last succeeded.

Mount the log read-only in `compose.local.yaml`:

```yaml
    volumes:
      - /var/log/my-backup.log:/host/backup/backup.log:ro
```

Every line the integration reads must start with a timestamp, `YYYY-MM-DD HH:MM:SS`. Four regular expressions say what the lines mean; adjust them to your script:

```yaml
  backup_log:
    enabled: true
    path: /host/backup/backup.log
    started_pattern: "===== backup started"
    finished_pattern: "===== backup finished"
    failed_pattern: "FAILED: (?P<message>.*)"          # the message group is shown to you
    repo_ok_pattern: "(?P<repo>[\\w-]+): backup OK"    # optional, one line per copy
```

A log these defaults understand:

```
2026-01-10 03:00:01 ===== backup started
2026-01-10 03:41:12 offsite: backup OK
2026-01-10 03:52:40 local: backup OK
2026-01-10 03:52:41 ===== backup finished
2026-01-11 03:00:00 FAILED: /mnt/backup is not mounted
```

Here Steward would report that the Jan 11 run failed, that the last good backup finished Jan 10 at 03:52, and when `offsite` and `local` last succeeded.

- A bind-mounted single file stops updating in the container if your script replaces the file instead of appending to it (log rotation). Mount the log's folder instead and adjust `path`.
- **Verb:** `backup_status`.

## Integrations that aren't built in

Steward is meant to be extended. If your app has an HTTP API or a CLI inside its container, adding it is a reader function, a verb, a tool or topic, and a formatter: see [adding-an-integration.md](adding-an-integration.md).
