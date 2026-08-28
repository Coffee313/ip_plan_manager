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


def test_legacy_project_unlock_uses_default_pin():
    javascript = (ROOT / "static/app.js").read_text(encoding="utf-8")
    assert "PIN по умолчанию: 1111" in javascript


def test_user_registration_profile_and_audit_drawer_exist():
    html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "static/style.css").read_text(encoding="utf-8")

    for element_id in (
        "userDialog",
        "userForm",
        "userName",
        "userProfileBtn",
        "auditBtn",
        "auditPanel",
        "auditList",
    ):
        assert f'id="{element_id}"' in html
    assert "ipPlanManager.userToken" in javascript
    assert 'headers.set("X-User-Token"' in javascript
    assert "async function ensureUserProfile()" in javascript
    assert "async function loadAuditLog()" in javascript
    assert '$("deleteProjectBtn").disabled = !hasAccess || !project?.can_delete;' in javascript
    assert "row-${node.id}" in javascript
    assert "row-${host.id}" in javascript
    assert ".audit-panel" in css and "position: fixed" in css
    assert "body.audit-open .app-layout" in css
