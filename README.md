# VOD Storage Monitor — Read-only dashboard

A monitoring-only dashboard for Linux VOD servers using mergerFS, SnapRAID, and mdadm.

**The dashboard cannot start, schedule, cancel, or control a SnapRAID operation.** All SnapRAID work must be managed externally via SSH, cron, or systemd. Existing scheduled settings from older versions are ignored (the scheduler is removed).

## What you can view

- `/mnt/vod` total, used, free capacity and utilization
- `/docker` storage statistics
- mdadm RAID health and progress
- SnapRAID status/diff summaries and last sync content-file timestamp
- Active **host** SnapRAID process (for example, a sync started via SSH or systemd)
- Tail of an existing SnapRAID log with automatic refresh
- SMART disk health, temperatures, runtime, pending/reallocated sectors
- Existing historical maintenance records stored in the local SQLite database
- Optional Docker container listing
- Responsive dashboard and password login

The app performs read-only inspection commands such as `df`, `lsblk`, `smartctl -x`, `snapraid status`, and `snapraid diff` when no host SnapRAID process is active. These are not maintenance operations. While a host SnapRAID command owns the content lock, the dashboard pauses its own status/diff queries, shows the active process and available log output, and marks those readings as paused, **not failed**.

## Install

```bash
mkdir -p /docker/vod-monitor/data
cd /docker/vod-monitor
curl -fsSL https://raw.githubusercontent.com/kasundigital/VOD-Storage-Monitor/main/docker-compose.yml -o docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/kasundigital/VOD-Storage-Monitor/main/.env.example -o .env
nano .env
docker compose pull
docker compose up -d
```

Set strong `ADMIN_PASSWORD` and `SECRET_KEY`. Generate a secret with `openssl rand -hex 32`.

Open `http://SERVER-IP:8099`.

Container image: `ghcr.io/kasundigital/vod-storage-monitor:latest`.

## Existing SnapRAID log

The monitor **does not start SnapRAID to produce a log**. It reads the existing file specified by `SNAPRAID_LOG_PATH`. Default:

```env
SNAPRAID_LOG_PATH=/docker/snapraid-recovery-backup/resync-final.log
```

This should be the path *inside the container*, backed by the read-only `/docker:/docker:ro` host mount. Point it to the actual existing log file on your system. If your log is outside `/docker`, add an additional **read-only** bind mount to the Compose file and use its in-container path. The log viewer shows the latest 220 lines (up to ~96 KiB), not the whole file, and refreshes every 5 seconds.

For the server `den-sm5049p-1`, the active external recovery command previously used `/docker/snapraid-recovery-backup/resync-final.log`.

To see an active SnapRAID process that runs on the host, `/proc:/host/proc:ro` is mounted. The dashboard displays the operation and PID but has no way to send signals or start jobs. **Percentage progress only appears if the external log actually contains percentage lines; no progress estimate is fabricated.**

## Read-only operation and permissions

- No Sync, Scrub, Check, Cancel, or schedule buttons
- No write-action or schedule API routes
- No background scheduler; old persisted schedules cannot execute
- Data, content, and device host mounts are specified read-only (`/docker`, `/mnt`, `/var/lib/snapraid`, `/dev`); the application keeps its own SQLite database in `./data:/data`
- The browser can view process and log output, disk SMART, storage utilization, and existing maintenance history

**Important:** `privileged: true` remains in the example Compose deployment for compatibility with low-level SMART access across many HDDs. This privilege means the *container itself is not a strong OS-level security boundary*, even though the application exposes no maintenance commands and bind mounts are set `:ro`. Deploy only on trusted networks and keep login credentials private. A future hardened deployment should use only the exact device/capability permissions required by the target host.

SnapRAID `status` and `diff` may be unavailable on some read-only filesystem/locking configurations; when they are, the dashboard continues showing disks, process and logs instead of changing any data.

Optional Docker listing requires `ENABLE_DOCKER_MONITOR=true` and a manually added `/var/run/docker.sock` mount. Docker socket access is highly privileged even when mounted read-only and is **not** enabled by default.

## Updating

```bash
curl -fsSL https://raw.githubusercontent.com/kasundigital/VOD-Storage-Monitor/main/update.sh | bash
```

The updater recreates only the *monitor* container. Existing `.env` and `./data` are preserved. Host SnapRAID syncs launched outside the dashboard remain independent of the monitor.

To upgrade a deployment with old schedules enabled, updating to this version makes those settings inert: no scheduler is started and the API cannot save or execute them.
