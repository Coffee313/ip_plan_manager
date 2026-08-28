from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from filelock import Timeout

from project_store import ProjectStore


def create_backup(store: ProjectStore | None = None) -> Path:
    store = store or ProjectStore()
    store.backups_root.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    backup_path = store.backups_root / f"ipplan-backup-{now:%Y%m%d-%H%M%S-%f}.zip"
    # A backup must fail closed: the regular project list intentionally hides
    # unreadable entries from the UI, but silently omitting one from a backup
    # would produce a plausible-looking archive with data loss.
    projects = store.list_projects(strict=True)

    included: list[dict[str, object]] = []
    try:
        with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for project in projects:
                project_id = project["id"]
                with store.project_lock(project_id):
                    project_dir = store.project_dir(project_id)
                    for path in project_dir.rglob("*"):
                        if not path.is_file():
                            continue
                        if path.name in {".lock"} or path.suffix == ".tmp":
                            continue
                        rel = path.relative_to(store.data_root)
                        archive.write(path, arcname=str(rel))
                    included.append(
                        {
                            "id": project_id,
                            "name": project["name"],
                            "revision": project.get("revision", 0),
                            "updated_at": project.get("updated_at"),
                        }
                    )

            with store.users_lock:
                users = store._read_users_unlocked()
                users_bytes = json.dumps(
                    {"users": users}, ensure_ascii=False, indent=2
                ).encode("utf-8")
                archive.writestr("users.json", users_bytes)

            manifest = {
                "format_version": 3,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "complete": True,
                "users_included": True,
                "users_sha256": hashlib.sha256(users_bytes).hexdigest(),
                "project_count": len(included),
                "project_ids": [project["id"] for project in included],
                "projects": included,
            }
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
    except (Timeout, FileNotFoundError, ValueError, OSError):
        backup_path.unlink(missing_ok=True)
        raise

    copy_dir = os.environ.get("IP_PLAN_BACKUP_COPY_DIR", "").strip()
    if copy_dir:
        external_root = Path(copy_dir).expanduser()
        external_root.mkdir(parents=True, exist_ok=True)
        external_tmp = external_root / f".{backup_path.name}.tmp"
        external_path = external_root / backup_path.name
        shutil.copy2(backup_path, external_tmp)
        external_tmp.replace(external_path)

    return backup_path


def cleanup_old_backups(store: ProjectStore) -> int:
    keep_days = int(os.environ.get("IP_PLAN_BACKUP_KEEP_DAYS", "30"))
    if keep_days <= 0:
        return 0

    threshold = datetime.now(timezone.utc) - timedelta(days=keep_days)
    removed = 0
    roots = [store.backups_root]
    copy_dir = os.environ.get("IP_PLAN_BACKUP_COPY_DIR", "").strip()
    if copy_dir:
        external_root = Path(copy_dir).expanduser()
        if external_root.resolve() != store.backups_root.resolve():
            roots.append(external_root)

    for root in roots:
        for path in root.glob("ipplan-backup-*.zip"):
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                if modified < threshold:
                    path.unlink()
                    removed += 1
            except FileNotFoundError:
                continue
    return removed


def main() -> int:
    try:
        store = ProjectStore()
        path = create_backup(store)
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
