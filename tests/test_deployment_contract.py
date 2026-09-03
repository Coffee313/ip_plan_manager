from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_systemd_service_uses_restrictive_file_permissions_and_hardening():
    installer = (ROOT / "deploy/install_debian.sh").read_text(encoding="utf-8")
    assert "UMask=0077" in installer
    assert "PrivateDevices=true" in installer
    assert "ProtectSystem=strict" in installer
    assert "ReadWritePaths=$APP_DIR/data" in installer
    assert "systemctl restart ip-plan-manager.service" in installer


def test_nginx_example_documents_https_and_security_headers():
    nginx = (ROOT / "deploy/nginx.conf.example").read_text(encoding="utf-8")
    assert "listen 443 ssl" in nginx
    assert "Strict-Transport-Security" in nginx
    assert "X-Content-Type-Options" in nginx
