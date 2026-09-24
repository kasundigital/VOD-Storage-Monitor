# VOD Storage Monitor

Purpose-built dashboard for a mergerFS + SnapRAID + mdadm VOD server.

## Features

- `/mnt/vod` total / used / free space and utilization
- `/docker` NVMe RAID storage usage
- mdadm arrays and resync/recovery progress
- SnapRAID status, last sync estimate, warnings, and pending diff counts
- SMART health, temperature, power-on hours, pending/reallocated sectors
- Maintenance history stored in SQLite
- Optional SnapRAID Sync / Scrub buttons
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

To enable Sync / Scrub from the UI, set:

```yaml
ENABLE_ACTIONS=true
```

and change the `/mnt` mount from `:ro` to `:rw`.

This gives the dashboard write access to your storage. Only enable it on a trusted/private network and use a strong password.

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
