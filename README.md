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
- Most host mounts remain read-only (`/docker`, `/mnt`, `/dev`). **`/var/lib/snapraid` is mounted read-write** because SnapRAID creates `/var/lib/snapraid/snapraid.content.lock` even when running `snapraid status` or `snapraid diff`. The application keeps its own SQLite database in `./data:/data`
- The browser can view process and log output, disk SMART, storage utilization, and existing maintenance history

**Important:** `privileged: true` remains in the example Compose deployment for compatibility with low-level SMART access across many HDDs. This privilege means the *container itself is not a strong OS-level security boundary*, even though the application exposes no maintenance commands and most bind mounts are set `:ro`. The SnapRAID content directory is writable from this privileged container; a compromised monitor could damage critical SnapRAID metadata. Back up SnapRAID content files and restrict access accordingly. Deploy only on trusted networks and keep login credentials private. A future hardened deployment should use only the exact device/capability permissions required by the target host.

### SnapRAID content lock permissions

SnapRAID creates a `.snapraid.content.lock` file beside its content file even for `snapraid status` and `snapraid diff`. A read-only bind mount at `/var/lib/snapraid` causes the error:

```text
Failed to create the lock file '/var/lib/snapraid/snapraid.content.lock'. Read-only file system.
```

The supplied Compose file uses `/var/lib/snapraid:/var/lib/snapraid:rw` to let these monitoring queries acquire their lock. **This is a functional workaround, not a strictly read-only security design:** the privileged monitor gains write access to content metadata, although its application exposes no sync/scrub/fix actions. Deploy only on a trusted or VPN-restricted network. A host-side collector that exports status results without handing the dashboard writable SnapRAID metadata would provide stronger isolation.

If updating an existing installation, also update the local `/docker/vod-monitor/docker-compose.yml` mount from `:ro` to `:rw`, then run `docker compose up -d --no-deps --force-recreate vod-monitor`. Updating the image alone cannot change an existing local Compose mount.

Verify with:

```bash
docker exec vod-monitor snapraid -c /etc/snapraid.conf status
```

If an external SnapRAID job owns the lock, wait until it finishes. The monitor pauses its own status/diff queries while it detects host maintenance.

Optional Docker listing requires `ENABLE_DOCKER_MONITOR=true` and a manually added `/var/run/docker.sock` mount. Docker socket access is highly privileged even when mounted read-only and is **not** enabled by default.

## Updating

```bash
curl -fsSL https://raw.githubusercontent.com/kasundigital/VOD-Storage-Monitor/main/update.sh | bash
```

The updater recreates only the *monitor* container. Existing `.env` and `./data` are preserved. Host SnapRAID syncs launched outside the dashboard remain independent of the monitor.

To upgrade a deployment with old schedules enabled, updating to this version makes those settings inert: no scheduler is started and the API cannot save or execute them.
