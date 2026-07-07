"""
lib/certs/cert_manager.py

Generates a Let's Encrypt wildcard certificate for the controller's
Route53-backed DNS domain, using certbot's automated dns-route53 plugin
(NOT --manual — this runs fully unattended in CI, no human TXT-record step).

Requires: pip install certbot certbot-dns-route53 (see Dockerfile)
Requires: AWS credentials in the environment with Route53 change permissions
          on the relevant hosted zone (same credentials Route53Manager uses).

Usage:
    from lib.certs.cert_manager import generate_signed_cert

    cert_b64, key_b64 = generate_signed_cert(
        star_domain="shc-34.dev.rafay-edge.net",
        email="admin@rafay.co",
    )
"""

import base64
import subprocess
from pathlib import Path
from typing import Tuple


class CertGenerationError(Exception):
    pass


def generate_signed_cert(
    star_domain: str,
    email: str,
    config_dir: str = "/tmp/letsencrypt/config",
    work_dir: str = "/tmp/letsencrypt/work",
    logs_dir: str = "/tmp/letsencrypt/logs",
) -> Tuple[str, str]:
    """
    Requests a wildcard cert for *.{star_domain} via certbot's automated
    Route53 DNS-01 challenge, then returns (cert_b64, key_b64) — the
    base64-encoded fullchain.pem and privkey.pem contents, ready to drop
    straight into config.yaml's `certificate:` / `key:` fields.

    Raises CertGenerationError with certbot's stderr on failure.
    """
    domain_arg = f"*.{star_domain}"

    # Isolated dirs so this doesn't collide with any system-wide certbot
    # state, and so repeated CI runs don't accumulate renewal config.
    for d in (config_dir, work_dir, logs_dir):
        Path(d).mkdir(parents=True, exist_ok=True)

    cmd = [
        "certbot", "certonly",
        "--non-interactive",
        "--agree-tos",
        "-m", email,
        "--dns-route53",
        "-d", domain_arg,
        "--config-dir", config_dir,
        "--work-dir", work_dir,
        "--logs-dir", logs_dir,
    ]

    print(f"[CertManager] Requesting wildcard cert for {domain_arg} ...")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            raise CertGenerationError(
                f"certbot failed (exit {result.returncode}):\n"
                f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
            )

        # certbot names the live dir after the first -d domain, stripping the
        # leading "*." wildcard marker.
        live_dir = Path(config_dir) / "live" / star_domain
        fullchain_path = live_dir / "fullchain.pem"
        privkey_path = live_dir / "privkey.pem"

        if not fullchain_path.exists() or not privkey_path.exists():
            raise CertGenerationError(
                f"certbot reported success but expected files are missing:\n"
                f"  {fullchain_path} (exists={fullchain_path.exists()})\n"
                f"  {privkey_path} (exists={privkey_path.exists()})"
            )

        cert_b64 = base64.b64encode(fullchain_path.read_bytes()).decode("ascii")
        key_b64 = base64.b64encode(privkey_path.read_bytes()).decode("ascii")

        print(f"[CertManager] Cert issued ✓ — fullchain {len(cert_b64)}b64 chars, "
              f"key {len(key_b64)}b64 chars")

        return cert_b64, key_b64

    finally:
        # config_dir/work_dir/logs_dir default to paths under /tmp, which the
        # Jenkinsfile bind-mounts from the HOST (`-v /tmp:/tmp`) — NOT ephemeral
        # container storage. Without explicit cleanup, private keys (or partial
        # certbot state from a failed run) would persist in plaintext on the
        # Jenkins host indefinitely across every signed-cert build. This runs
        # on every exit path — success, certbot failure, or missing files —
        # since the cert/key (if any) are already captured above by this point.
        import shutil
        for d in (config_dir, work_dir, logs_dir):
            shutil.rmtree(d, ignore_errors=True)
        print(f"[CertManager] Cleaned up local certbot state (config/work/logs dirs)")