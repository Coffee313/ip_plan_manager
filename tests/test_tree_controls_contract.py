from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_subnets_are_collapsed_by_default_and_collapse_all_control_exists():
    html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static/app.js").read_text(encoding="utf-8")

    assert 'id="collapseAllBtn"' in html
    assert "function collapseAllSubnets" in javascript
    assert '$("collapseAllBtn").onclick = collapseAllSubnets' in javascript
    assert "collapseAllSubnets();" in javascript


def test_deleted_audit_events_use_distinct_location_highlight():
    javascript = (ROOT / "static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "static/style.css").read_text(encoding="utf-8")

    assert "focusAuditTarget(anchor, event.action)" in javascript
    assert "audit-deletion-target-flash" in javascript
    assert ".audit-deletion-target-flash" in css
