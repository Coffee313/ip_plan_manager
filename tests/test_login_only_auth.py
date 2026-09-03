from __future__ import annotations

import json
from pathlib import Path

from app import create_app
from project_store import ProjectStore

ROOT = Path(__file__).resolve().parents[1]


def test_registration_and_login_require_only_corporate_login(tmp_path):
    store = ProjectStore(tmp_path / "data")
    client = create_app(store).test_client()

    registered = client.post(
        "/api/auth/register",
        json={"login": "Ivanov.II"},
    )

    assert registered.status_code == 200
    registration = registered.get_json()["data"]
    assert registration["user"]["login"] == "ivanov.ii"
    assert registration["user"]["name"] == "ivanov.ii"
    users = json.loads(store.users_path.read_text(encoding="utf-8"))["users"]
    assert "password_hash" not in users[0]

    logged_in = client.post(
        "/api/auth/login",
        json={"login": "IVANOV.II"},
    )

    assert logged_in.status_code == 200
    assert logged_in.get_json()["data"]["user"]["id"] == registration["user"]["id"]
    assert logged_in.get_json()["data"]["access_token"]


def test_unknown_corporate_login_cannot_enter_personal_cabinet(tmp_path):
    client = create_app(ProjectStore(tmp_path / "data")).test_client()

    response = client.post("/api/auth/login", json={"login": "unknown.user"})

    assert response.status_code == 401
    assert "логин" in response.get_json()["error"].lower()


def test_existing_session_migrates_password_era_user_to_login_only(tmp_path):
    store = ProjectStore(tmp_path / "data")
    user, token = store.create_user("legacy.user")
    payload = json.loads(store.users_path.read_text(encoding="utf-8"))
    payload["users"][0]["name"] = "Старое имя"
    payload["users"][0]["password_hash"] = "obsolete"
    store.users_path.write_text(json.dumps(payload), encoding="utf-8")
    client = create_app(store).test_client()

    response = client.get("/api/users/me", headers={"X-User-Token": token})

    assert response.status_code == 200
    assert response.get_json()["data"]["name"] == user["login"]
    migrated = json.loads(store.users_path.read_text(encoding="utf-8"))["users"][0]
    assert migrated["name"] == user["login"]
    assert "password_hash" not in migrated


def test_user_can_change_login_and_it_persists_across_restart(tmp_path):
    data_root = tmp_path / "data"
    store = ProjectStore(data_root)
    client = create_app(store).test_client()
    registration = client.post(
        "/api/auth/register", json={"login": "old.login"}
    ).get_json()["data"]
    headers = {"X-User-Token": registration["access_token"]}

    changed = client.put(
        "/api/users/me", json={"login": "New.Login"}, headers=headers
    )

    assert changed.status_code == 200
    assert changed.get_json()["data"]["id"] == registration["user"]["id"]
    assert changed.get_json()["data"]["login"] == "new.login"
    assert changed.get_json()["data"]["name"] == "new.login"
    assert client.post("/api/auth/login", json={"login": "old.login"}).status_code == 401
    restarted = create_app(ProjectStore(data_root)).test_client()
    assert restarted.post(
        "/api/auth/login", json={"login": "NEW.LOGIN"}
    ).status_code == 200
    assert restarted.get("/api/users/me", headers=headers).get_json()["data"][
        "login"
    ] == "new.login"


def test_user_cannot_change_login_to_an_existing_login(tmp_path):
    client = create_app(ProjectStore(tmp_path / "data")).test_client()
    first = client.post(
        "/api/auth/register", json={"login": "first.user"}
    ).get_json()["data"]
    client.post("/api/auth/register", json={"login": "second.user"})

    response = client.put(
        "/api/users/me",
        json={"login": "SECOND.USER"},
        headers={"X-User-Token": first["access_token"]},
    )

    assert response.status_code == 400
    assert "зарегистрирован" in response.get_json()["error"].lower()


def test_login_registration_and_profile_use_separate_dialogs_without_password():
    html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static/app.js").read_text(encoding="utf-8")

    assert 'id="loginDialog"' in html
    assert 'id="loginForm"' in html
    assert 'id="loginLogin"' in html
    assert 'id="openRegistrationBtn"' in html
    assert 'id="registrationDialog"' in html
    assert 'id="registrationForm"' in html
    assert 'id="registrationLogin"' in html
    assert 'id="openLoginBtn"' in html
    assert 'id="profileDialog"' in html
    assert 'id="profileForm"' in html
    assert 'id="profileLogin"' in html
    assert 'id="saveProfileBtn"' in html
    assert 'id="userPassword"' not in html
    assert 'id="authModeButtons"' not in html
    assert html.count('placeholder="IPetrov"') == 3
    assert "Например: IPetrov" in html
    assert 'href="/static/style.css?v=18.5"' in html
    assert 'src="/static/app.js?v=18.5"' in html
    assert "async function submitLogin" in javascript
    assert "async function submitRegistration" in javascript
    assert "async function submitProfile" in javascript
    assert '"/api/users/me"' in javascript
    assert "name.textContent = member.login || member.name;" in javascript
    assert '"/api/auth/login"' in javascript
    assert '"/api/auth/register"' in javascript
