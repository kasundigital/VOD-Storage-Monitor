# VOD Storage Monitor

Purpose-built dashboard for a mergerFS + SnapRAID + mdadm VOD server.

## Features
- `/mnt/vod` total / used / free space and utilization
- `/docker` NVMe RAID storage usage
- mdadm arrays and resync/recovery progress
- SnapRAID status, last sync estimate (latest content-file timestamp), warnings, and pending diff counts
- SMART health, temperature, power-on hours, pending/reallocated sectors
- Maintenance history stored in SQLite
- Optional SnapRAID Sync / Scrub buttons
- Optional Docker container list
- Responsive dark dashboard
- Password login

## Install

Copy this folder to `/docker/vod-monitor`, then:

```bash
cd /docker/vod-monitor
nano docker-compose.yml
```

Change at least:

```yaml
ADMIN_PASSWORD=CHANGE_ME_NOW
SECRET_KEY=CHANGE_TO_A_LONG_RANDOM_SECRET
```

Start:

```bash
docker compose up -d --build
```

Open:

`http://SERVER-IP:8099`

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

Note: access to the Docker socket is highly privileged. Leave this disabled unless you specifically need it.

## Important

The "Last Sync" field is derived from the newest SnapRAID `content` file modification time. This is a reliable indication that SnapRAID saved state, including when sync is run outside this dashboard, but it is not a full job history. Jobs launched through this dashboard are additionally recorded in SQLite with start/result/duration.
