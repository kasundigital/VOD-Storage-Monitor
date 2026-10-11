# VOD Storage Monitor — lock-free, monitoring-only dashboard

Monitor mergerfs capacity, mdadm RAID arrays, HDD/NVMe SMART health, and external SnapRAID jobs without interrupting recovery or scrubbing.

**The web application never invokes the `snapraid` executable — not even `status` or `diff`.** SnapRAID uses an exclusive content lock for those commands, and a 60-second dashboard poll previously raced with manually launched scrubs. This release removes that source of contention entirely.

## What it shows

- VOD and Docker storage utilization, mdadm array state and progress, and SMART health/temperatures
- Running SnapRAID jobs detected from host `/proc`, plus external log tail and progress if present
- Last existing sync log outcome (`summary:exit:ok` means **the observed log** ended OK)
- SnapRAID disk/parity/content configuration parsed from the mounted configuration
- Optional **host-captured** SnapRAID status/diff snapshot with capture time and scrub coverage
- Historical maintenance records, optional Docker listing, responsive authenticated UI

**Important accuracy rule:** The monitor does not call a historical sync log a live health check. It displays `Last log OK` instead of `Protected`. Without a snapshot, pending changes, status, diff and scrub coverage display **Unknown / Not collected**, never zero or failed. A snapshot is a point-in-time reading, not continuously verified protection.

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

Use strong `ADMIN_PASSWORD` and `SECRET_KEY`; generate a secret with `openssl rand -hex 32`. Open `http://SERVER-IP:8099` only on a trusted network or behind an authenticated HTTPS/VPN proxy.

Image: `ghcr.io/kasundigital/vod-storage-monitor:latest`.

## Lock-free SnapRAID monitoring

The default Compose file intentionally keeps `/var/lib/snapraid:/var/lib/snapraid:ro`. This is safe because the monitor **never calls SnapRAID**, and therefore never tries to create `snapraid.content.lock`. The earlier workaround of switching this mount to `:rw` is **not needed and should be reverted** when deploying this release.

The app reads the configured external sync log (`SNAPRAID_LOG_PATH`, default `/docker/snapraid-recovery-backup/resync-final.log`). It reads existing files through the read-only `/docker:/docker:ro` bind mount. Set `SNAPRAID_SCRUB_LOG_PATH` to the actual existing scrub log if you want that log displayed during a running scrub. Logs may be historical; the process indicator is independent.

### Optional accurate status/diff snapshot (manual, host only)

SnapRAID's `status` and `diff` themselves take the SnapRAID content lock. To avoid a monitoring race, **there is no automatic timer, cron job, or background status/diff collector**. If you want status/diff/pending counts and scrub coverage on the dashboard, capture them **manually on the Ubuntu host after maintenance has finished**, while no other SnapRAID process is running:

```bash
curl -fsSL https://raw.githubusercontent.com/kasundigital/VOD-Storage-Monitor/main/scripts/capture-snapraid-snapshot.py \
  -o /tmp/capture-snapraid-snapshot.py
python3 /tmp/capture-snapraid-snapshot.py
```

The script checks for a running SnapRAID process, runs `snapraid status` then `snapraid diff` on the host, and atomically saves a timestamped JSON snapshot to `/docker/vod-monitor/data/snapraid-snapshot.json`. It does not modify parity or media files; the commands acquire their normal temporary content lock. **Do not run it during sync/scrub/fix**, and remember that a new maintenance operation could theoretically start between its checks unless host maintenance is coordinated.

The dashboard reads this JSON file through `/docker:/docker:ro`. The snapshot timestamp is displayed prominently. Snapshot data becomes historical as soon as files change; no live pending-change guarantee is implied. If no snapshot exists, the monitor still provides full disk, RAID, process, and log monitoring.

For a running scrub, you can keep the monitor online throughout the operation: it does not acquire a SnapRAID lock.

## Updating an existing server

1. **Stop the monitor only** while changing its Compose configuration: `docker stop vod-monitor`.
2. Back up `/docker/vod-monitor/docker-compose.yml` and `.env`.
3. After this change is merged and a new GHCR image is published, run the repository `update.sh` or manually download the updated Compose file and pull/recreate **only** `vod-monitor`.
4. Confirm `/var/lib/snapraid:/var/lib/snapraid:ro` and that the dashboard shows `Not collected` (not `Failed`) before the first manual snapshot.
5. Optional: capture a host snapshot when SnapRAID is idle, then verify the snapshot time and status/diff fields.

To update using the published release:

```bash
curl -fsSL https://raw.githubusercontent.com/kasundigital/VOD-Storage-Monitor/main/update.sh | bash
```

The updater backs up the local Compose file before replacing it, preserves `.env` and `./data`, and recreates only `vod-monitor`. Do **not** run the updater until the change is merged and the new image is available.

## Security

The web app exposes no Sync/Scrub/Fix/Cancel/scheduling routes. Host disks, SnapRAID metadata, and logs remain mounted read-only; only `./data:/data` is writable. The sample Compose deployment still uses `privileged: true` for low-level SMART compatibility, so it is **not** a strong security boundary. Restrict dashboard access, never expose it unprotected on a public IP, and consider a more narrowly privileged collector in the future.

Docker listing is disabled by default (`ENABLE_DOCKER_MONITOR=false`); exposing `docker.sock` to a container is highly privileged even with a read-only bind mount.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The regression suite asserts that status refresh never invokes SnapRAID, even when idle, and that unavailable snapshot data is reported as unknown.
