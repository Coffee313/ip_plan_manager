from __future__ import annotations

import json

from app import create_app
from project_store import ProjectStore


def register(client, name: str = "Тестовый пользователь") -> tuple[dict, dict[str, str]]:
    data = client.post(
        "/api/auth/register",
        json={"login": f"user-{abs(hash(name))}"},
    ).get_json()["data"]
    return data, {"X-User-Token": data["access_token"]}


def test_project_membership_survives_store_restart(tmp_path):
    data_root = tmp_path / "data"
    store = ProjectStore(data_root)
    app = create_app(store)
    client = app.test_client()
    user, user_headers = register(client)

    created = client.post(
        "/api/projects",
        json={"name": "Заказчик", "pin": "1234"},
        headers=user_headers,
    )
    assert created.status_code == 200
    payload = created.get_json()["data"]
    project_id = payload["project"]["id"]
    token = payload["access_token"]
    assert token

    public_projects = client.get("/api/projects", headers=user_headers).get_json()["data"]
    assert public_projects == [
        {
            "id": project_id,
            "name": "Заказчик",
            "created_at": payload["project"]["created_at"],
            "updated_at": payload["project"]["updated_at"],
            "revision": 0,
            "pin_set": True,
            "can_delete": True,
            "access_level": "owner",
            "can_manage_access": True,
            "can_manage_project": True,
        }
    ]

    assert client.get("/api/state", headers={"X-Project-ID": project_id}).status_code == 401
    assert client.get(
        "/api/state",
        headers={"X-Project-ID": project_id, "X-Project-Token": "wrong"},
    ).status_code == 401
    assert client.get(
        "/api/state",
        headers={**user_headers, "X-Project-ID": project_id},
    ).status_code == 200

    restarted_app = create_app(ProjectStore(data_root))
    restarted_client = restarted_app.test_client()
    assert restarted_client.get(
        "/api/state",
        headers={**user_headers, "X-Project-ID": project_id},
    ).status_code == 200


def test_unlock_is_not_available_for_new_owned_projects(tmp_path):
    store = ProjectStore(tmp_path / "data")
    client = create_app(store).test_client()
    user, user_headers = register(client)
    created = client.post(
        "/api/projects", json={"name": "P", "pin": "9876"}, headers=user_headers
    )
    project_id = created.get_json()["data"]["project"]["id"]

    denied = client.post(
        f"/api/projects/{project_id}/unlock", json={"pin": "0000"}, headers=user_headers
    )
    assert denied.status_code == 403

    unlocked = client.post(
        f"/api/projects/{project_id}/unlock", json={"pin": "9876"}, headers=user_headers
    )
    assert unlocked.status_code == 403
    assert client.get(
        "/api/state",
        headers={**user_headers, "X-Project-ID": project_id},
    ).status_code == 200


def test_pin_must_contain_exactly_four_digits(tmp_path):
    client = create_app(ProjectStore(tmp_path / "data")).test_client()
    user, user_headers = register(client)
    for pin in ("", "123", "12345", "12ab"):
        response = client.post(
            "/api/projects", json={"name": f"P-{pin}", "pin": pin}, headers=user_headers
        )
        assert response.status_code == 400
        assert "четыр" in response.get_json()["error"].lower()


def test_legacy_project_uses_default_pin_and_first_user_becomes_owner(tmp_path):
    store = ProjectStore(tmp_path / "data")
    project, _ = store.create_project("Legacy", "1234")
    meta_path = store.project_dir(project["id"]) / "project.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.pop("pin_hash")
    meta.pop("access_token_hashes")
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    client = create_app(store).test_client()
    user, user_headers = register(client, "Первый пользователь")

    denied = client.post(
        f"/api/projects/{project['id']}/unlock",
        json={"pin": "5555"},
        headers=user_headers,
    )
    assert denied.status_code == 403

    unlocked = client.post(
        f"/api/projects/{project['id']}/unlock",
        json={"pin": "1111"},
        headers=user_headers,
    )
    assert unlocked.status_code == 200
    assert store.list_projects()[0]["pin_set"] is True
    assert store.is_creator(project["id"], user["user"]["id"])


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
    user, user_headers = register(client)
    assert client.post(
        f"/api/projects/{project['id']}/unlock",
        json={"pin": "0000"},
        headers=user_headers,
    ).status_code == 403
    assert client.post(
        f"/api/projects/{project['id']}/unlock",
        json={"pin": "7777"},
        headers=user_headers,
    ).status_code == 200


def test_first_authenticated_user_claims_existing_project_with_legacy_pin(tmp_path):
    store = ProjectStore(tmp_path / "data")
    project, _project_token = store.create_project("Existing", "1234")
    client = create_app(store).test_client()
    user, user_headers = register(client, "Старый владелец")

    unlocked = client.post(
        f"/api/projects/{project['id']}/unlock",
        json={"pin": "1234"},
        headers=user_headers,
    )
    assert unlocked.status_code == 200
    response = client.get(
        "/api/state", headers={**user_headers, "X-Project-ID": project["id"]}
    )
    assert response.status_code == 200
    assert store.is_creator(project["id"], user["user"]["id"])