from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from project_store import ProjectStore


def _validated_members(archive: zipfile.ZipFile) -> tuple[dict, list[zipfile.ZipInfo]]:
    members = archive.infolist()
    for member in members:
        path = PurePosixPath(member.filename)
        if path.is_absolute() or ".." in path.parts or "\\" in member.filename:
            raise ValueError(f"Архив содержит недопустимый путь: {member.filename}")
        if path.parts and path.parts[0] not in {"manifest.json", "projects"}:
            raise ValueError(f"Архив содержит недопустимый путь: {member.filename}")

    try:
        manifest = json.loads(archive.read("manifest.json"))
    except (KeyError, json.JSONDecodeError) as exc:
        raise ValueError("Некорректный manifest.json") from exc

    if "complete" not in manifest and "project_ids" not in manifest:
        # v15 backups predate the explicit completeness marker. Their manifest
        # listed every intended project, so derive the v16 fields and still run
        # the same archive-content validation below.
        legacy_projects = manifest.get("projects", [])
        if not isinstance(legacy_projects, list):
            raise ValueError("Некорректный legacy manifest.json")
        legacy_ids = [
            project.get("id") for project in legacy_projects if isinstance(project, dict)
        ]
        if len(legacy_ids) != int(manifest.get("project_count", -1)):
            raise ValueError("Некорректный legacy manifest.json")
        manifest["project_ids"] = legacy_ids
        manifest["complete"] = True
        manifest["format_version"] = 1

    if manifest.get("complete") is not True:
        raise ValueError("Нельзя восстановить неполный бэкап")

    project_ids = manifest.get("project_ids")
    if not isinstance(project_ids, list) or any(
        not isinstance(project_id, str) for project_id in project_ids
    ):
        raise ValueError("Некорректный список проектов в manifest.json")

    names = {member.filename for member in members if not member.is_dir()}
    archived_project_ids = {
        parts[1]
        for name in names
        if len(parts := PurePosixPath(name).parts) == 3
        and parts[0] == "projects"
        and parts[2] == "project.json"
    }
    if archived_project_ids != set(project_ids):
        raise ValueError("Состав проектов не совпадает с manifest.json")

    for project_id in project_ids:
        if not project_id or any(ch not in "0123456789abcdef" for ch in project_id.lower()):
            raise ValueError("Некорректный идентификатор проекта в бэкапе")
        project_meta_name = f"projects/{project_id}/project.json"
        workspace_name = f"projects/{project_id}/workspace.json"
        if workspace_name not in names:
            raise ValueError(f"В проекте {project_id} отсутствует workspace.json")
        try:
            project_meta = json.loads(archive.read(project_meta_name))
            workspace = json.loads(archive.read(workspace_name))
        except (KeyError, json.JSONDecodeError) as exc:
            raise ValueError(f"Повреждены данные проекта {project_id}") from exc
        if project_meta.get("id") != project_id:
            raise ValueError(f"ID проекта {project_id} не совпадает с project.json")
        if not isinstance(workspace.get("sites"), list):
            raise ValueError(f"Некорректный workspace.json проекта {project_id}")

    return manifest, members


def restore_backup(backup: Path, store: ProjectStore | None = None) -> int:
    store = store or ProjectStore()
    backup = Path(backup).resolve()
    if not backup.is_file():
        raise ValueError(f"Backup not found: {backup}")

    store.data_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(backup) as archive:
        manifest, members = _validated_members(archive)

        with tempfile.TemporaryDirectory(
            prefix="ipplan-restore-", dir=store.data_root
        ) as tmp:
            staged_root = Path(tmp) / "projects"
            staged_root.mkdir()
            for member in members:
                parts = PurePosixPath(member.filename).parts
                if member.is_dir() or not parts or parts[0] != "projects":
                    continue
                destination = Path(tmp).joinpath(*parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)

            rollback = store.data_root / f".projects-rollback-{uuid.uuid4().hex}"
            with store.registry_lock:
                moved_old = False
                try:
                    if store.projects_root.exists():
                        store.projects_root.rename(rollback)
                        moved_old = True
                    staged_root.rename(store.projects_root)
                except Exception:
                    if moved_old and rollback.exists() and not store.projects_root.exists():
                        rollback.rename(store.projects_root)
                    raise
                else:
                    if rollback.exists():
                        shutil.rmtree(rollback)

    return int(manifest.get("project_count", len(manifest["project_ids"])))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore IP Plan Manager projects from a backup ZIP."
    )
    parser.add_argument("backup", type=Path)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation.",
    )
    args = parser.parse_args()

    store = ProjectStore()
    if not args.yes:
        print("This will replace all current projects in:")
        print(store.projects_root)
        answer = input("Type RESTORE to continue: ")
        if answer != "RESTORE":
            print("Cancelled.")
            return 1

    try:
        count = restore_backup(args.backup, store)
    except Exception as exc:
        print(f"Restore failed: {exc}", file=sys.stderr)
        return 2

    print(f"Restored projects: {count}")
    print(f"Restored from: {args.backup.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
