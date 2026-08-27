from __future__ import annotations

import json
import os
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from filelock import Timeout

from project_store import ProjectStore


def create_backup() -> Path:
    store = ProjectStore()
    store.backups_root.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    backup_path = store.backups_root / f"ipplan-backup-{now:%Y%m%d-%H%M%S}.zip"
    projects = store.list_projects()

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project_count": len(projects),
        "projects": [
            {
                "id": p["id"],
                "name": p["name"],
                "revision": p.get("revision", 0),
                "updated_at": p.get("updated_at"),
            }
            for p in projects
        ],
    }

    with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )

        for project in projects:
            project_id = project["id"]
            try:
                with store.project_lock(project_id):
                    project_dir = store.project_dir(project_id)
                    for path in project_dir.rglob("*"):
                        if not path.is_file():
                            continue
                        if path.name in {".lock"} or path.suffix == ".tmp":
                            continue
                        rel = path.relative_to(store.data_root)
                        archive.write(path, arcname=str(rel))
            except (Timeout, FileNotFoundError, ValueError):
                # A project may be deleted while the backup is running.
                continue

    return backup_path


def cleanup_old_backups(store: ProjectStore) -> int:
    keep_days = int(os.environ.get("IP_PLAN_BACKUP_KEEP_DAYS", "30"))
    if keep_days <= 0:
        return 0

    threshold = datetime.now() - timedelta(days=keep_days)
    removed = 0
    for path in store.backups_root.glob("ipplan-backup-*.zip"):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime)
            if modified < threshold:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            pass
    return removed


def main() -> int:
    try:
        store = ProjectStore()
        path = create_backup()
        removed = cleanup_old_backups(store)
        print(f"Backup created: {path}")
        if removed:
            print(f"Old backups removed: {removed}")
        return 0
    except Exception as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
