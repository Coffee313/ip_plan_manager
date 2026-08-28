from __future__ import annotations

import json

from project_store import ProjectStore, verify_pin
from set_project_pin import main


def test_set_project_pin_cli_prompts_twice_and_updates_legacy_project(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    store = ProjectStore(data_root)
    project, _ = store.create_project("Legacy", "1234")
    meta_path = store.project_dir(project["id"]) / "project.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.pop("pin_hash")
    meta.pop("access_token_hashes")
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setenv("IP_PLAN_DATA_DIR", str(data_root))
    answers = iter(["7777", "7777"])

    exit_code = main([project["id"]], prompt=lambda _: next(answers))

    assert exit_code == 0
    updated = store.get_meta(project["id"])
    assert verify_pin("7777", updated["pin_hash"])
