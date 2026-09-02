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
    assert 'id="profileLogin"' in html
    assert 'id="userPassword"' not in html
    assert 'id="authModeButtons"' not in html
    assert 'href="/static/style.css?v=18.1"' in html
    assert 'src="/static/app.js?v=18.1"' in html
    assert "async function submitLogin" in javascript
    assert "async function submitRegistration" in javascript
    assert "name.textContent = member.login || member.name;" in javascript
    assert '"/api/auth/login"' in javascript
    assert '"/api/auth/register"' in javascript
