#!/usr/bin/env python3
"""Nightly backup of everything the dashboard can't rebuild from Garmin:
data/ (accounts, Garmin tokens, races, plans, notes, coach notes) and .env.

- Each run writes data-YYYYmmdd-HHMMSS.tar.gz into LOCAL_DIR and keeps the
  newest LOCAL_KEEP of them.
- SQLite databases are copied with SQLite's own online-backup API, so a
  backup taken while the app is writing is still a consistent database.
- If a USB stick labelled TRAINBACKUP is plugged in, every local archive it
  doesn't have yet is copied to <stick>/trainstate-backups/ (newest USB_KEEP
  kept there). If this script had to mount the stick itself, it unmounts it
  again afterwards, so the stick is safe to pull out between runs.

Run by trainstate-backup.service: nightly via its timer, and immediately
whenever the stick is plugged in (udev rule). Runs as root so it can mount
the stick; files it writes are handed back to the dashboard's user.

Restore: stop the services, extract an archive into the project folder
(`tar xzf data-....tar.gz -C /home/aleppi/GarminDashboard`), start them.
"""

from __future__ import annotations

import os
import pwd
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
OWNER = os.environ.get("BACKUP_OWNER", "aleppi")
LOCAL_DIR = Path(os.environ.get("BACKUP_LOCAL_DIR", "/home/aleppi/GarminDashboard-backups/nightly"))
LOCAL_KEEP = int(os.environ.get("BACKUP_LOCAL_KEEP", "30"))
USB_LABEL = os.environ.get("BACKUP_USB_LABEL", "TRAINBACKUP")
USB_MOUNTPOINT = Path(os.environ.get("BACKUP_USB_MOUNTPOINT", "/mnt/trainbackup"))
USB_SUBDIR = "trainstate-backups"
USB_KEEP = int(os.environ.get("BACKUP_USB_KEEP", "120"))


def log(msg: str) -> None:
    print(msg, flush=True)


def owner_ids() -> tuple[int, int]:
    pw = pwd.getpwnam(OWNER)
    return pw.pw_uid, pw.pw_gid


def chown(path: Path) -> None:
    if os.geteuid() == 0:
        uid, gid = owner_ids()
        os.chown(path, uid, gid)


def snapshot(dest: Path) -> None:
    """Copy data/ and .env into dest, SQLite files via the backup API."""
    data = PROJECT / "data"
    for src in data.rglob("*"):
        rel = src.relative_to(PROJECT)
        target = dest / rel
        if src.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix == ".db":
            with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as live, sqlite3.connect(target) as copy:
                live.backup(copy)
        else:
            shutil.copy2(src, target)
    env = PROJECT / ".env"
    if env.exists():
        shutil.copy2(env, dest / ".env")


def make_archive() -> Path:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    chown(LOCAL_DIR)
    name = f"data-{datetime.now():%Y%m%d-%H%M%S}.tar.gz"
    archive = LOCAL_DIR / name
    with tempfile.TemporaryDirectory() as tmp:
        snapshot(Path(tmp))
        with tarfile.open(archive, "w:gz") as tar:
            for item in sorted(Path(tmp).iterdir()):
                tar.add(item, arcname=item.name)
    os.chmod(archive, 0o600)  # holds Garmin tokens and the session secret
    chown(archive)
    # Prove it reads back before trusting it.
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getnames()
    log(f"wrote {archive} ({archive.stat().st_size // 1024} KB, {len(members)} entries)")
    return archive


def prune(folder: Path, keep: int) -> None:
    archives = sorted(folder.glob("data-*.tar.gz"))
    for old in archives[:-keep] if keep > 0 else []:
        old.unlink()
        log(f"pruned {old}")


def find_usb() -> tuple[Path | None, bool]:
    """(mountpoint, mounted_by_us). Uses an existing mount if the desktop
    already automounted the stick; otherwise mounts it here."""
    dev = subprocess.run(["blkid", "-L", USB_LABEL], capture_output=True, text=True).stdout.strip()
    if not dev:
        return None, False
    current = subprocess.run(["findmnt", "-n", "-o", "TARGET", "--source", dev], capture_output=True, text=True).stdout.strip()
    if current:
        return Path(current.splitlines()[0]), False
    if os.geteuid() != 0:
        log(f"found {USB_LABEL} at {dev} but not root, can't mount it")
        return None, False
    fstype = subprocess.run(["blkid", "-o", "value", "-s", "TYPE", dev], capture_output=True, text=True).stdout.strip()
    USB_MOUNTPOINT.mkdir(parents=True, exist_ok=True)
    cmd = ["mount", dev, str(USB_MOUNTPOINT)]
    if fstype in ("vfat", "exfat", "ntfs", "ntfs3"):
        uid, gid = owner_ids()
        cmd[1:1] = ["-o", f"uid={uid},gid={gid},umask=077"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"could not mount {dev} ({fstype}): {result.stderr.strip()}")
        return None, False
    return USB_MOUNTPOINT, True


def copy_to_usb() -> None:
    mount, mounted_here = find_usb()
    if mount is None:
        log(f"no USB stick labelled {USB_LABEL} — local backup only")
        return
    try:
        target = mount / USB_SUBDIR
        target.mkdir(exist_ok=True)
        have = {p.name for p in target.glob("data-*.tar.gz")}
        copied = 0
        for archive in sorted(LOCAL_DIR.glob("data-*.tar.gz")):
            if archive.name not in have:
                shutil.copyfile(archive, target / (archive.name + ".part"))
                os.replace(target / (archive.name + ".part"), target / archive.name)
                copied += 1
        prune(target, USB_KEEP)
        os.sync()
        log(f"USB {mount}: copied {copied} new archive(s), {len(list(target.glob('data-*.tar.gz')))} on stick")
    finally:
        if mounted_here:
            subprocess.run(["umount", str(mount)], check=False)


def main() -> int:
    make_archive()
    prune(LOCAL_DIR, LOCAL_KEEP)
    copy_to_usb()
    return 0


if __name__ == "__main__":
    sys.exit(main())
