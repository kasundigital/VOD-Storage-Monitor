#!/usr/bin/env python3
"""Manually capture SnapRAID status/diff on the HOST for a lock-free dashboard.

This is intentionally not a scheduled task. The dashboard never invokes it.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

CONFIG = os.environ.get("SNAPRAID_CONFIG", "/etc/snapraid.conf")
OUTPUT = Path(os.environ.get(
    "SNAPRAID_SNAPSHOT_OUTPUT", "/docker/vod-monitor/data/snapraid-snapshot.json"
))


def active_snapraid():
    result = subprocess.run(["pgrep", "-x", "snapraid"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def execute(operation):
    result = subprocess.run(
        ["snapraid", "-c", CONFIG, operation],
        capture_output=True, text=True, timeout=300, check=False
    )
    return result.returncode, result.stdout + result.stderr


def main():
    if active_snapraid():
        print("Refusing snapshot: SnapRAID is already running.", file=sys.stderr)
        return 2
    try:
        status_rc, status = execute("status")
        if active_snapraid():
            print("Refusing diff: another SnapRAID operation is running.", file=sys.stderr)
            return 2
        diff_rc, diff = execute("diff")
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Snapshot failed: {exc}", file=sys.stderr)
        return 1

    payload = {
        "schema": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "status_rc": status_rc,
        "diff_rc": diff_rc,
        "status": status,
        "diff": diff,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".snapraid-snapshot-", suffix=".tmp", dir=OUTPUT.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, OUTPUT)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    print(f"Saved snapshot: {OUTPUT} (status={status_rc}, diff={diff_rc})")
    return 0 if status_rc == 0 and diff_rc in (0, 2) else 1


if __name__ == "__main__":
    sys.exit(main())
