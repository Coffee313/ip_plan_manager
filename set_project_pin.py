from __future__ import annotations

import argparse
import getpass
from collections.abc import Callable, Sequence

from project_store import ProjectStore


def main(
    argv: Sequence[str] | None = None,
    prompt: Callable[[str], str] = getpass.getpass,
) -> int:
    parser = argparse.ArgumentParser(
        description="Set or reset a project's four-digit PIN on the server."
    )
    parser.add_argument("project_id", help="Project ID from the project list")
    args = parser.parse_args(argv)

    pin = prompt("Новый PIN (4 цифры): ")
    confirmation = prompt("Повторите PIN: ")
    if pin != confirmation:
        parser.error("PIN-коды не совпадают")

    store = ProjectStore()
    store.set_project_pin(args.project_id, pin)
    project = store.get_meta(args.project_id)
    print(f"PIN проекта «{project['name']}» установлен. Старые токены отозваны.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
