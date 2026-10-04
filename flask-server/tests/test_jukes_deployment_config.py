"""Deployment invariants for the JUKES Compose and proxy boundary."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class JukesDeploymentConfigTests(unittest.TestCase):
    def test_compose_preserves_external_vpn_and_persistent_storage(self):
        compose = (ROOT / "docker-compose.yml").read_text()

        self.assertIn('network_mode: "container:${VPN_CONTAINER_NAME:-gluetun}"', compose)
        self.assertEqual(compose.count('network_mode: "container:${VPN_CONTAINER_NAME:-gluetun}"'), 2)
        self.assertIn("ytmusic_data:/data", compose)
        self.assertIn("ytmusic_cache:/tmp/ytm_audio_cache", compose)
        self.assertIn("ytmusic_chromium_profile:/profile", compose)
        self.assertRegex(compose, r"(?m)^  web:\n    external: true\n    name: web$")
        self.assertNotIn("ytmusic_alexa_cookies", compose)
        self.assertNotIn("cookies.txt:/app/cookies.txt", compose)

    def test_private_routes_use_verified_gluetun_alias_and_never_publish_sidecars(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        caddy = (ROOT / "Caddyfile").read_text()

        self.assertIn("GLUETUN_ALIAS", compose)
        self.assertIn("{$GLUETUN_ALIAS:gluetun}:5000", caddy)
        self.assertIn("{$GLUETUN_ALIAS:gluetun}:6080", caddy)
        self.assertIn("{$CADDY_TRUSTED_PROXY_CONFIG}", caddy)
        self.assertNotIn("trusted_proxies static", caddy)
        self.assertGreaterEqual(caddy.count("header_up X-Forwarded-For {client_ip}"), 2)
        self.assertGreaterEqual(caddy.count("header_up X-Real-IP {client_ip}"), 2)
        self.assertGreaterEqual(caddy.count("header_up -CF-Connecting-IP"), 2)
        self.assertIn("/admin/youtube/browser/authorize", caddy)
        self.assertIn("127.0.0.1:8765", compose)
        self.assertNotIn("8765:", compose)
        self.assertNotIn("9222:", compose)
        self.assertNotIn("5900:", compose)
        self.assertNotIn("6080:", compose)
        self.assertNotIn("/alexa/", caddy)

    def test_compose_exports_the_approved_jukes_environment_contract(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        env_example = (ROOT / ".env.example").read_text()

        for name in (
            "JUKES_DB_PATH",
            "JUKES_AUDIO_DIR",
            "JUKES_REQUESTED_CACHE_LIMIT_BYTES",
            "JUKES_WARMUP_CACHE_LIMIT_BYTES",
            "JUKES_WARMUP_TTL_SECONDS",
            "JUKES_MIN_FREE_DISK_BYTES",
            "JUKES_ADMIN_PASSWORD_HASH",
            "JUKES_ADMIN_SESSION_KEY",
            "JUKES_CREDENTIAL_ENCRYPTION_KEY",
            "JUKES_TRUSTED_PROXY_CIDRS",
            "CADDY_TRUSTED_PROXY_CONFIG",
            "PUBLIC_BASE_URL",
            "YT_BROWSER_CONTROL_TOKEN",
            "YT_BROWSER_SERVICE_URL",
            "YTDLP_BGUTIL_BASE_URL",
            "YTDLP_JS_RUNTIME",
        ):
            self.assertIn(name, compose)
            self.assertIn(name, env_example)
        self.assertIn("/data/jukes.sqlite3", env_example)
        self.assertIn("/tmp/ytm_audio_cache", env_example)
        for name in (
            "JUKES_ADMIN_PASSWORD_HASH",
            "JUKES_ADMIN_SESSION_KEY",
            "JUKES_CREDENTIAL_ENCRYPTION_KEY",
            "YT_BROWSER_CONTROL_TOKEN",
            "JUKES_TRUSTED_PROXY_CIDRS",
            "CADDY_TRUSTED_PROXY_CONFIG",
        ):
            self.assertRegex(env_example, rf"(?m)^{name}=$")

    def test_images_install_python_requirements_with_uv_and_use_local_liveness(self):
        app = (ROOT / "flask-server/Dockerfile").read_text()
        browser = (ROOT / "browser-auth/Dockerfile").read_text()

        for dockerfile in (app, browser):
            self.assertIn("uv pip install", dockerfile)
            self.assertNotRegex(dockerfile, r"(?m)^RUN pip install")
        self.assertIn("/health/live", app)
        self.assertIn("/health", browser)

    def test_bgutil_helper_retries_when_external_network_is_not_ready(self):
        script = ROOT / "scripts/ensure-bgutil-network.sh"
        with tempfile.TemporaryDirectory() as temp_dir:
            bin_dir = Path(temp_dir)
            docker = bin_dir / "docker"
            docker.write_text(
                "#!/bin/sh\n"
                "if [ \"$1 $2\" = 'inspect bgutil-provider' ]; then exit 0; fi\n"
                "if [ \"$1 $2\" = 'network inspect' ]; then exit 1; fi\n"
                "exit 0\n"
            )
            docker.chmod(0o755)
            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")
            result = subprocess.run(
                ["bash", str(script)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 75, result.stderr)
        unit = (ROOT / "scripts/ensure-bgutil-network.service").read_text()
        self.assertIn("RemainAfterExit=no", unit)
        self.assertIn("Restart=on-failure", unit)


if __name__ == "__main__":
    unittest.main()
