# VOD Storage Monitor — lock-free, monitoring-only dashboard

Monitor mergerfs capacity, mdadm RAID arrays, HDD/NVMe SMART health, and external SnapRAID jobs without interrupting recovery or scrubbing.

**The web application never invokes the `snapraid` executable — not even `status` or `diff`.** SnapRAID uses an exclusive content lock for those commands, and a 60-second dashboard poll previously raced with manually launched scrubs. This release removes that source of contention entirely.

## What it shows

- VOD and Docker storage utilization, mdadm array state and progress, and SMART health/temperatures
- Running SnapRAID jobs detected from host `/proc`, with start time, elapsed time and progress if the log has it
- **Last run** (operation, OK / Warning / Error, when it ended, how long it took) and **last failure**
- **Last verified sync** age, including successful host systemd-run sync units; highlighted when older than `SYNC_STALE_HOURS` (default 26h)
- Last existing sync log outcome (`summary:exit:ok` means **the observed log** ended OK)
- From the host snapshot: scrub age (newest/oldest block), % never scrubbed, array errors and interrupted syncs (highlighted when the newest scrub is older than `SCRUB_STALE_DAYS`, default 14)
- SMART reallocated/pending/uncorrectable sectors; sleeping disks are not woken up (`smartctl -n standby`)
- SnapRAID disk/parity/content configuration parsed from the mounted configuration
- Optional **host-captured** SnapRAID status/diff snapshot with capture time and scrub coverage
- **Run history** of host jobs, optional Docker listing, responsive authenticated UI

**Important accuracy rule:** The monitor does not call a historical sync log a live health check. It displays `Last log OK` instead of `Protected`. Without a snapshot, pending changes, status, diff and scrub coverage display **Unknown / Not collected**, never zero or failed. A snapshot is a point-in-time reading, not continuously verified protection.

## Missing HDD / parity disk detection (v0.6.1)

This dashboard now checks the **expected** disks from the configured `snapraid.conf` against the **host's** live mount table (`/host/proc/1/mountinfo`) and block devices (`lsblk`). Missing disks are listed even if they have vanished from `lsblk`.

- A configured `data` or `parity` location without its own filesystem mount is **Missing mount** (critical); this may be an unmount, a failed device, or a boot/mount configuration problem — not necessarily a failed HDD.
- A location still in the host mount table but with a major:minor block device not found by `lsblk` is **Device missing** (critical). Inspect the host before making repairs.
- If physical drive identity cannot be verified, the monitor says **Unverified**, never falsely "Healthy".
- After **all** expected slots are online and their serial numbers are available, the app automatically captures a reference to `/data/disk-baseline.json`. The baseline is not created from a degraded configuration. It survives monitor container upgrades under `/docker/vod-monitor/data`.
- Subsequent scans can identify the previous serial of a missing drive and flag a change of serial while avoiding false alarms when a Linux device name such as `sdb` changes.
- If a SnapRAID disk entry disappears from the config after the baseline was saved, it remains flagged instead of silently reducing the expected number.

The table lists configured **data and parity mounts**, not total physical drives including the Ubuntu boot disks or NVMe RAID. There is no physical "failed disk" verdict from a mount alert; always confirm via host diagnostics. If the host mount table is unavailable, inventory state is **Unknown**, not a fabricated all-good or all-missing count.

The checker only **reads** `snapraid.conf`, `/proc`, and `lsblk`. It never mounts/unmounts disks, runs SnapRAID, scrubs parity, or modifies data drives. Only the monitor's internal healthy-inventory snapshot is written under `/data`. Current alerts appear after the next monitor collection (normally within 60 seconds).

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

## Run history (last run / failures)

A background watcher looks for `snapraid` processes on the host every 10 seconds (via `/proc:/host/proc:ro`). It only reads `/proc`; it never runs SnapRAID. When a sync, scrub, check, fix or touch ends, it records start time, end time and duration in `./data`. The result (OK / Warning / Error, plus file/io/data error counts) is read from the job's log (`SNAPRAID_LOG_PATH`, or `SNAPRAID_SCRUB_LOG_PATH` for scrubs) if that log was written during the run:

- best: run SnapRAID with `-l <file>` (its `summary:exit:ok|warning|error` lines are used), or
- redirect console output to the log file (`Everything OK`, `DANGER!`, `WARNING!` lines are used).

Log paths may be globs such as `/docker/snapraid/logs/*.log`; the newest matching file is used. If no log was written during the run, it is recorded as **Finished** (result unknown). Jobs that start and end while the monitor is stopped are not recorded.

"Last sync" uses the **newest verified successful sync** from run history or host systemd journal. The dashboard reads dedicated `snapraid-*sync*.service: Deactivated successfully.` completions, including units created with `systemd-run --unit=snapraid-sync-resume-20261016 ...`. A failed or incomplete sync is never counted. The journal is read-only and requires the two host journal bind mounts in the updated Compose file. If host journals are unavailable and no successful sync was seen by the monitor, the last successful sync is **Unknown**, not the misleading mtime of `snapraid.content` (which also changes during scrub/fix and failed syncs).

**Important:** The "Scrub" box reports the age of blocks last **scrubbed**, not the age of the last **sync**. A new successful sync does not reset that scrub age. A manually captured status/diff snapshot can remain old until the host snapshot script is run again when no SnapRAID process is active.

### Journal-backed successful sync dates (v0.6.2)

The Docker image includes `journalctl` and the Compose file mounts both `/var/log/journal` and `/run/log/journal` **read-only**, because Ubuntu may use persistent or volatile journals. The monitor queries at most the latest 100 matching records in each location, limited to the last 180 days. It does not execute `snapraid`, modify host logs, or run any maintenance. Only dedicated sync services that systemd reports as successfully deactivated are treated as verified. Ordinary syncs seen by the process watcher and confirmed by a contemporaneous SnapRAID log still count.

After updating the Compose file and image, verify the source beneath "Last sync" reads `host systemd journal` if a matching completion exists; it may otherwise show `run history` or `Unknown`. The prior fixed `SNAPRAID_LOG_PATH` can still display historical recovery logs and need not be the most recent run. The dashboard's cache normally refreshes every 60 seconds. A dashboard update does **not** run a new parity sync.

Existing installations must use the updated Compose file to add the new read-only journal mounts (updating only the image is insufficient). Keep `./data` and `.env` intact.

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
