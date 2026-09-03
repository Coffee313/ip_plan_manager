from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_project_creation_and_invitation_pin_fields_exist():
    html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
    assert 'id="projectPIN"' in html
    assert 'id="inviteDialog"' in html
    assert 'id="invitePIN"' in html
    assert 'id="exportBtn"' in html and '<button id="exportBtn"' in html


def test_frontend_authenticates_by_registered_user_membership():
    javascript = (ROOT / "static/app.js").read_text(encoding="utf-8")
    assert 'ipPlanManager.userToken' in javascript
    assert 'headers.set("X-User-Token"' in javascript
    assert '"/api/auth/register"' in javascript
    assert '"/api/auth/login"' in javascript
    assert "X-Project-Token" not in javascript
    assert 'async function exportExcel()' in javascript
    assert "if (!currentProjectId || document.hidden) return;" in javascript


def test_user_registration_project_sharing_and_audit_drawer_exist():
    html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "static/style.css").read_text(encoding="utf-8")

    for element_id in (
        "loginDialog",
        "loginForm",
        "loginLogin",
        "registrationDialog",
        "registrationForm",
        "registrationLogin",
        "profileDialog",
        "profileLogin",
        "userProfileBtn",
        "manageAccessBtn",
        "accessDialog",
        "membersList",
        "auditBtn",
        "auditPanel",
        "auditList",
    ):
        assert f'id="{element_id}"' in html
    assert "ipPlanManager.userToken" in javascript
    assert 'headers.set("X-User-Token"' in javascript
    assert "async function ensureUserProfile()" in javascript
    assert "async function loadAuditLog()" in javascript
    assert "project?.can_manage_project" in javascript
    assert "project?.can_manage_access" in javascript
    assert "project?.can_invite" in javascript
    assert 'id="membersSection"' in html
    assert '$("membersSection").hidden = !project?.can_manage_access' in javascript
    assert "async function createInviteLink()" in javascript
    assert "async function loadProjectMembers()" in javascript
    assert "/api/invitations/${encodeURIComponent(pendingInviteToken)}/accept" in javascript
    assert "row-${node.id}" in javascript
    assert "row-${host.id}" in javascript
    assert ".audit-panel" in css and "position: fixed" in css
    assert "body.audit-open .app-layout" in css


def test_invite_link_has_automatic_and_explicit_clipboard_copy():
    html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static/app.js").read_text(encoding="utf-8")

    assert 'id="copyInviteBtn"' in html
    assert "async function copyTextToClipboard" in javascript
    assert 'document.execCommand("copy")' in javascript
    assert '$("copyInviteBtn").onclick' in javascript


def test_backup_dialog_is_wide_and_has_inner_spacing():
    html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
    css = (ROOT / "static/style.css").read_text(encoding="utf-8")

    assert 'id="backupsDialog" class="modal backup-modal"' in html
    assert ".backup-modal" in css and "width: min(760px" in css
    assert ".backup-toolbar" in css and "padding-inline: 18px" in css
    assert ".backups-list" in css and "margin-inline: 18px" in css
