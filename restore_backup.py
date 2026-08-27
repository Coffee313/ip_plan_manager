from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from project_store import ProjectStore


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

    backup = args.backup.resolve()
    if not backup.is_file():
        print(f"Backup not found: {backup}", file=sys.stderr)
        return 2

    store = ProjectStore()

    if not args.yes:
        print("This will replace all current projects in:")
        print(store.projects_root)
        answer = input("Type RESTORE to continue: ")
        if answer != "RESTORE":
            print("Cancelled.")
            return 1

    with tempfile.TemporaryDirectory(prefix="ipplan-restore-") as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(backup) as archive:
            archive.extractall(tmp_path)

        restored_projects = tmp_path / "projects"
        if not restored_projects.is_dir():
            print("Backup does not contain projects/", file=sys.stderr)
            return 3

        if store.projects_root.exists():
            shutil.rmtree(store.projects_root)
        shutil.copytree(restored_projects, store.projects_root)

    print(f"Restored from: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
