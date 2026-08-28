from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pin_dialog_and_project_creation_pin_field_exist():
    html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
    assert 'id="projectPIN"' in html
    assert 'id="unlockDialog"' in html
    assert 'id="unlockPIN"' in html
    assert 'id="exportBtn"' in html and '<button id="exportBtn"' in html


def test_frontend_persists_tokens_and_authenticates_every_project_request():
    javascript = (ROOT / "static/app.js").read_text(encoding="utf-8")
    assert 'ipPlanManager.projectTokens' in javascript
    assert 'headers.set("X-Project-Token"' in javascript
    assert '/unlock`' in javascript
    assert 'async function exportExcel()' in javascript
    assert "if (!currentProjectId || !currentProjectToken() || document.hidden) return;" in javascript


def test_legacy_project_unlock_is_disabled_until_server_setup():
    javascript = (ROOT / "static/app.js").read_text(encoding="utf-8")
    assert "Администратор должен задать PIN на сервере" in javascript
    assert '$("unlockPIN").disabled = !project.pin_set;' in javascript
