from __future__ import annotations

import json

from app import create_app
from project_store import ProjectStore, token_hash


def register(client, name: str, login: str) -> dict:
    response = client.post(
        "/api/auth/register",
        json={"login": login},
    )
    assert response.status_code == 200
    return response.get_json()["data"]


def auth(token: str, **extra: str) -> dict[str, str]:
    return {"X-User-Token": token, **extra}


def test_registration_requires_unique_corporate_login_and_login_survives_restart(tmp_path):
    data_root = tmp_path / "data"
    client = create_app(ProjectStore(data_root)).test_client()

    registered = register(client, "Анна Петрова", "a.petrova")
    assert registered["user"]["login"] == "a.petrova"
    assert registered["user"]["name"] == "a.petrova"

    duplicate = client.post(
        "/api/auth/register",
        json={"login": "A.PETROVA"},
    )
    assert duplicate.status_code == 400
    assert "логин" in duplicate.get_json()["error"].lower()

    restarted = create_app(ProjectStore(data_root)).test_client()
    logged_in = restarted.post(
        "/api/auth/login",
        json={"login": "A.PETROVA"},
    )
    assert logged_in.status_code == 200
    assert logged_in.get_json()["data"]["user"]["id"] == registered["user"]["id"]


def test_old_name_only_registration_endpoint_is_not_available(tmp_path):
    client = create_app(ProjectStore(tmp_path / "data")).test_client()
    response = client.post("/api/users", json={"name": "Без пароля"})
    assert response.status_code in {404, 405}


def test_legacy_browser_profile_can_add_credentials_without_losing_owned_projects(tmp_path):
    store = ProjectStore(tmp_path / "data")
    user, old_token = store.create_user("temporary")
    users_payload = json.loads(store.users_path.read_text(encoding="utf-8"))
    legacy_user = users_payload["users"][0]
    legacy_user.pop("login")
    legacy_user.pop("password_hash", None)
    legacy_user.pop("session_token_hashes")
    legacy_user["token_hash"] = token_hash(old_token)
    store.users_path.write_text(json.dumps(users_payload), encoding="utf-8")
    project, _ = store.create_project("Старый проект", "1234", user["id"], user["name"])
    client = create_app(store).test_client()

    upgraded = client.post(
        "/api/auth/register",
        json={"login": "legacy.owner"},
        headers=auth(old_token),
    )

    assert upgraded.status_code == 200
    data = upgraded.get_json()["data"]
    assert data["user"]["id"] == user["id"]
    projects = client.get("/api/projects", headers=auth(data["access_token"])).get_json()["data"]
    assert [item["id"] for item in projects] == [project["id"]]


def test_invite_link_and_pin_add_project_to_colleagues_personal_project_list(tmp_path):
    client = create_app(ProjectStore(tmp_path / "data")).test_client()
    owner = register(client, "Владелец", "owner")
    colleague = register(client, "Коллега", "colleague")
    created = client.post(
        "/api/projects",
        json={"name": "Закрытый проект", "pin": "2468"},
        headers=auth(owner["access_token"]),
    ).get_json()["data"]["project"]

    assert client.get(
        "/api/projects", headers=auth(colleague["access_token"])
    ).get_json()["data"] == []

    invite = client.post(
        f"/api/projects/{created['id']}/invite",
        headers=auth(owner["access_token"]),
    )
    assert invite.status_code == 200
    invite_token = invite.get_json()["data"]["token"]

    wrong = client.post(
        f"/api/invitations/{invite_token}/accept",
        json={"pin": "0000"},
        headers=auth(colleague["access_token"]),
    )
    assert wrong.status_code == 403

    accepted = client.post(
        f"/api/invitations/{invite_token}/accept",
        json={"pin": "2468"},
        headers=auth(colleague["access_token"]),
    )
    assert accepted.status_code == 200
    assert accepted.get_json()["data"]["access_level"] == "reduced"

    projects = client.get(
        "/api/projects", headers=auth(colleague["access_token"])
    ).get_json()["data"]
    assert [(project["id"], project["access_level"]) for project in projects] == [
        (created["id"], "reduced")
    ]


def test_project_id_and_pin_cannot_bypass_required_invite_link(tmp_path):
    client = create_app(ProjectStore(tmp_path / "data")).test_client()
    owner = register(client, "Владелец", "owner")
    colleague = register(client, "Коллега", "colleague")
    project = client.post(
        "/api/projects", json={"name": "Проект", "pin": "1234"},
        headers=auth(owner["access_token"]),
    ).get_json()["data"]["project"]

    bypass = client.post(
        f"/api/projects/{project['id']}/unlock",
        json={"pin": "1234"},
        headers=auth(colleague["access_token"]),
    )

    assert bypass.status_code == 403
    assert client.get(
        "/api/projects", headers=auth(colleague["access_token"])
    ).get_json()["data"] == []


def test_saved_legacy_project_token_migrates_to_user_membership(tmp_path):
    store = ProjectStore(tmp_path / "data")
    owner, _ = store.create_user("owner")
    project, legacy_project_token = store.create_project(
        "Старый общий проект", "1234", owner["id"], owner["name"]
    )
    client = create_app(store).test_client()
    colleague = register(client, "Старый участник", "legacy.member")

    migrated = client.post(
        "/api/migrations/legacy-project-access",
        json={"tokens": {project["id"]: legacy_project_token}},
        headers=auth(colleague["access_token"]),
    )

    assert migrated.status_code == 200
    assert migrated.get_json()["data"]["migrated"] == 1
    projects = client.get(
        "/api/projects", headers=auth(colleague["access_token"])
    ).get_json()["data"]
    assert [(item["id"], item["access_level"]) for item in projects] == [
        (project["id"], "reduced")
    ]

    another = register(client, "Другой пользователь", "another.user")
    reused = client.post(
        "/api/migrations/legacy-project-access",
        json={"tokens": {project["id"]: legacy_project_token}},
        headers=auth(another["access_token"]),
    )
    assert reused.get_json()["data"]["migrated"] == 0
    assert client.get(
        "/api/projects", headers=auth(another["access_token"])
    ).get_json()["data"] == []


def test_owner_can_promote_reduced_member_to_full_and_revoke_access(tmp_path):
    client = create_app(ProjectStore(tmp_path / "data")).test_client()
    owner = register(client, "Владелец", "owner")
    colleague = register(client, "Коллега", "colleague")
    created = client.post(
        "/api/projects", json={"name": "Проект", "pin": "1234"},
        headers=auth(owner["access_token"]),
    ).get_json()["data"]["project"]
    pid = created["id"]
    invite_token = client.post(
        f"/api/projects/{pid}/invite", headers=auth(owner["access_token"])
    ).get_json()["data"]["token"]
    client.post(
        f"/api/invitations/{invite_token}/accept",
        json={"pin": "1234"},
        headers=auth(colleague["access_token"]),
    )

    members = client.get(
        f"/api/projects/{pid}/members", headers=auth(owner["access_token"])
    ).get_json()["data"]
    colleague_member = next(member for member in members if member["login"] == "colleague")

    reduced_rename = client.put(
        f"/api/projects/{pid}", json={"name": "Запрещено"},
        headers=auth(colleague["access_token"]),
    )
    assert reduced_rename.status_code == 403

    promoted = client.put(
        f"/api/projects/{pid}/members/{colleague_member['id']}",
        json={"access_level": "full"},
        headers=auth(owner["access_token"]),
    )
    assert promoted.status_code == 200

    full_rename = client.put(
        f"/api/projects/{pid}", json={"name": "Разрешено"},
        headers=auth(colleague["access_token"]),
    )
    assert full_rename.status_code == 200
    assert client.get(
        f"/api/projects/{pid}/backups", headers=auth(colleague["access_token"])
    ).status_code == 200

    assert client.delete(
        f"/api/projects/{pid}", headers=auth(colleague["access_token"])
    ).status_code == 403
    assert client.get(
        f"/api/projects/{pid}/members", headers=auth(colleague["access_token"])
    ).status_code == 403

    revoked = client.delete(
        f"/api/projects/{pid}/members/{colleague_member['id']}",
        headers=auth(owner["access_token"]),
    )
    assert revoked.status_code == 200
    assert client.get(
        "/api/state", headers=auth(colleague["access_token"], **{"X-Project-ID": pid})
    ).status_code == 403