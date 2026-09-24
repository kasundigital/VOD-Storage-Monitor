# VOD Storage Monitor

Purpose-built dashboard for a mergerFS + SnapRAID + mdadm VOD server.

## Features

- `/mnt/vod` total / used / free space and utilization
- `/docker` NVMe RAID storage usage
- mdadm arrays and resync/recovery progress
- SnapRAID status, last sync estimate, warnings, and pending diff counts
- SMART health, temperature, power-on hours, pending/reallocated sectors
- Maintenance history stored in SQLite
- SnapRAID Check Now, Status, Diff, Sync, 10% Scrub, Full Scrub, live job output, cancel control, and maintenance history
- Optional Docker container list
- Responsive desktop/mobile dashboard
- Password login

## Quick install

Create the app folder and Compose file:

```bash
mkdir -p /docker/vod-monitor/data
cd /docker/vod-monitor

curl -fsSL https://raw.githubusercontent.com/kasundigital/VOD-Storage-Monitor/main/docker-compose.yml -o docker-compose.yml
```

Edit your password and secret:

```bash
nano docker-compose.yml
```

Generate a strong secret if needed:

```bash
openssl rand -hex 32
```

Pull and start the prebuilt image:

```bash
docker compose pull
docker compose up -d
```

Open:

```text
http://SERVER-IP:8099
```

Docker image:

```text
ghcr.io/kasundigital/vod-storage-monitor:latest
```

## Update

```bash
cd /docker/vod-monitor
curl -fsSL https://raw.githubusercontent.com/kasundigital/VOD-Storage-Monitor/main/docker-compose.yml -o docker-compose.yml
docker compose pull
docker compose up -d
docker image prune -f
```

## SnapRAID actions

Monitoring is read-only by default.

To enable the authenticated SnapRAID maintenance controls, set:

```env
ENABLE_ACTIONS=true
```

The supplied Compose file mounts `/mnt` read/write so Sync and Scrub can update parity when actions are enabled. Keep `ENABLE_ACTIONS=false` if you only want monitoring.

Available controls:

- **Check Now** — runs SnapRAID status then diff
- **Status** — current SnapRAID array status
- **Diff** — pending added/updated/removed/moved files
- **Start Sync** — updates parity for pending changes
- **Scrub 10%** — routine partial scrub
- **Full Scrub** — verifies the entire protected set
- **Cancel Job** — requests termination of the currently running dashboard job
- Live output, elapsed time, result state, and SQLite maintenance history

Only enable actions on a trusted/private network and use a strong administrator password.

## Docker monitoring

Optional. To show containers, set:

```yaml
ENABLE_DOCKER_MONITOR=true
```

and add:

```yaml
- /var/run/docker.sock:/var/run/docker.sock
```

Access to the Docker socket is highly privileged. Leave this disabled unless you specifically need it.

## Last Sync

The Last Sync field is derived from the newest SnapRAID `content` file modification time. Jobs launched through the dashboard are additionally recorded in SQLite with start/result/duration.
