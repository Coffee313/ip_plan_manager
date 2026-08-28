from __future__ import annotations

import json

from app import create_app
from project_store import ProjectStore


def test_project_requires_pin_and_permanent_token_survives_store_restart(tmp_path):
    data_root = tmp_path / "data"
    store = ProjectStore(data_root)
    app = create_app(store)
    client = app.test_client()

    created = client.post(
        "/api/projects",
        json={"name": "Заказчик", "pin": "1234"},
    )
    assert created.status_code == 200
    payload = created.get_json()["data"]
    project_id = payload["project"]["id"]
    token = payload["access_token"]
    assert token

    public_projects = client.get("/api/projects").get_json()["data"]
    assert public_projects == [
        {
            "id": project_id,
            "name": "Заказчик",
            "created_at": payload["project"]["created_at"],
            "updated_at": payload["project"]["updated_at"],
            "revision": 0,
            "pin_set": True,
        }
    ]

    assert client.get("/api/state", headers={"X-Project-ID": project_id}).status_code == 403
    assert client.get(
        "/api/state",
        headers={"X-Project-ID": project_id, "X-Project-Token": "wrong"},
    ).status_code == 403
    assert client.get(
        "/api/state",
        headers={"X-Project-ID": project_id, "X-Project-Token": token},
    ).status_code == 200

    restarted_app = create_app(ProjectStore(data_root))
    restarted_client = restarted_app.test_client()
    assert restarted_client.get(
        "/api/state",
        headers={"X-Project-ID": project_id, "X-Project-Token": token},
    ).status_code == 200


def test_unlock_rejects_wrong_pin_and_returns_another_permanent_token(tmp_path):
    store = ProjectStore(tmp_path / "data")
    client = create_app(store).test_client()
    created = client.post("/api/projects", json={"name": "P", "pin": "9876"})
    project_id = created.get_json()["data"]["project"]["id"]

    denied = client.post(f"/api/projects/{project_id}/unlock", json={"pin": "0000"})
    assert denied.status_code == 403

    unlocked = client.post(f"/api/projects/{project_id}/unlock", json={"pin": "9876"})
    assert unlocked.status_code == 200
    token = unlocked.get_json()["data"]["access_token"]
    assert client.get(
        "/api/state",
        headers={"X-Project-ID": project_id, "X-Project-Token": token},
    ).status_code == 200


def test_pin_must_contain_exactly_four_digits(tmp_path):
    client = create_app(ProjectStore(tmp_path / "data")).test_client()
    for pin in ("", "123", "12345", "12ab"):
        response = client.post("/api/projects", json={"name": f"P-{pin}", "pin": pin})
        assert response.status_code == 400
        assert "четыр" in response.get_json()["error"].lower()


def test_legacy_project_cannot_be_claimed_through_public_unlock(tmp_path):
    store = ProjectStore(tmp_path / "data")
    project, _ = store.create_project("Legacy", "1234")
    meta_path = store.project_dir(project["id"]) / "project.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.pop("pin_hash")
    meta.pop("access_token_hashes")
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    client = create_app(store).test_client()

    response = client.post(
        f"/api/projects/{project['id']}/unlock", json={"pin": "5555"}
    )

    assert response.status_code == 403
    assert "администратор" in response.get_json()["error"].lower()
    assert store.list_projects()[0]["pin_set"] is False


def test_administrator_can_set_pin_for_legacy_project_on_server(tmp_path):
    store = ProjectStore(tmp_path / "data")
    project, _ = store.create_project("Legacy", "1234")
    meta_path = store.project_dir(project["id"]) / "project.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.pop("pin_hash")
    meta.pop("access_token_hashes")
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    store.set_project_pin(project["id"], "7777")

    client = create_app(store).test_client()
    assert client.post(
        f"/api/projects/{project['id']}/unlock", json={"pin": "0000"}
    ).status_code == 403
    assert client.post(
        f"/api/projects/{project['id']}/unlock", json={"pin": "7777"}
    ).status_code == 200
