"""
tests/controller/test_controller_upgrade.py

Drives an in-place controller upgrade (src_package -> dst_package) against
an ALREADY-PROVISIONED controller, using --skip-bringup + --controller-ip
+ --secondary-ips instead of Terraform-provisioning fresh nodes.

Jenkins usage (upgrade_only mode):
    pytest tests/controller/test_controller_upgrade.py \
        --skip-bringup \
        --controller-ip=141.148.166.161 \
        --secondary-ips=161.153.50.242,132.226.86.231 \
        --build-no=63 \
        --src-package=rafay-airgapped-controller-v3.1-39.tar.gz \
        --dst-package=rafay-airgapped-controller-v3.1-40.tar.gz \
        --os-type=ubuntu24 --controller-size=S --keep-vm

NOTE on --src-package: if left blank (as in the Jenkins form), the src
version is auto-detected from the live controller's installed config
(see _detect_installed_version) rather than assumed from CLI. This lets
the upgrade target an existing box without knowing its exact build number
in advance.

Requires fixtures already defined in conftest.py:
    ssh_client          -- connects to --controller-ip when --skip-bringup
                           is set, bypassing Terraform provisioning entirely
    controller_profile   -- size/ha/os_type profile
    package_profile      -- src package info (name/version/url/tar_path)
    secondary_ips        -- parsed from --secondary-ips
    controller_upgrade   -- wraps UpgradeEngine; runs engine.run() and sets
                           package_profile._actual_extract_dir to the NEW
                           (dst) extract dir on success

This file assumes those fixture names match conftest.py. If any fixture
is named differently, update the signatures below accordingly -- the
logic/assertions are what matters.
"""

import re
import pytest

pytestmark = [pytest.mark.order(2), pytest.mark.controller, pytest.mark.upgrade]


def attach_output(extras, label: str, content: str):
    """Embed command output into the pytest-html report (3.x/4.x compatible)."""
    import pytest_html
    block = f"<pre style='font-size:12px;white-space:pre-wrap'>{content}</pre>"
    item = pytest_html.extras.html(f"<b>{label}</b>{block}")
    if hasattr(extras, "append"):
        extras.append(item)
    else:
        extras.extend([item])


def _detect_installed_version(ssh_client) -> str:
    """
    Best-effort detection of the currently-installed controller version by
    reading the extracted package directory name under /opt/rafay, when
    --src-package is not supplied. Falls back to empty string if nothing
    is found (some validations below will then be skipped rather than
    failing on data we were never given).
    """
    out, rc = ssh_client.run(
        "ls -1 /opt/rafay/ 2>/dev/null | grep '^rafay-airgapped-controller' | head -1"
    )
    if rc != 0 or not out.strip():
        return ""
    m = re.search(r"v?([\d.]+-\d+)", out.strip())
    return m.group(1) if m else ""


class TestUpgradePreconditions:
    """Sanity checks before attempting the upgrade."""

    def test_ssh_reachable(self, ssh_client, extras):
        """Primary controller must be reachable over SSH before upgrading."""
        out, rc = ssh_client.run("hostname && uptime")
        attach_output(extras, "hostname/uptime", out)
        assert rc == 0, f"Could not reach controller over SSH: {out}"

    def test_existing_cluster_healthy_before_upgrade(self, ssh_client, extras):
        """
        Cluster should be fully healthy BEFORE we touch it. Upgrading on top
        of an already-unhealthy cluster makes root-causing any post-upgrade
        failure ambiguous (was it the upgrade, or a pre-existing problem?).
        """
        out, rc = ssh_client.run("kubectl get pods -A --no-headers 2>/dev/null")
        attach_output(extras, "pre-upgrade pod status", out)
        lines = [l for l in out.splitlines() if l.strip()]
        not_ready = [l for l in lines if "Running" not in l and "Completed" not in l]
        assert rc == 0 and lines, "kubectl get pods failed -- is kubeconfig set up on this box?"
        assert not not_ready, (
            f"{len(not_ready)} pod(s) not healthy BEFORE upgrade -- fix these first:\n"
            + "\n".join(not_ready[:10])
        )

    def test_src_version_matches_installed(self, ssh_client, package_profile, extras):
        """
        If --src-package was explicitly supplied, confirm it actually matches
        what's installed on the box -- catches a mismatched/stale Jenkins
        parameter before the upgrade engine does anything destructive.

        If --src-package was left blank (auto-detect mode), this just
        records what was detected for the report and does not assert.
        """
        installed = _detect_installed_version(ssh_client)
        attach_output(extras, "installed version (detected)", installed or "UNKNOWN")

        declared = getattr(package_profile, "version", "") or ""
        if not declared:
            print("[test_src_version_matches_installed] --src-package not set -- "
                  f"auto-detected installed version: {installed or 'UNKNOWN'}")
            pytest.skip("--src-package not supplied -- skipping explicit match check")

        assert installed, (
            "Could not detect any installed controller package under /opt/rafay -- "
            "is this actually a bringup'd controller?"
        )
        assert declared in installed or installed in declared, (
            f"--src-package declares version '{declared}' but the box has "
            f"'{installed}' installed -- check the Jenkins parameter"
        )


class TestUpgradeExecution:
    """Drives the actual upgrade via the controller_upgrade fixture."""

    def test_upgrade_completes(self, controller_upgrade, package_profile, extras):
        """
        Requesting the controller_upgrade fixture is what actually runs
        UpgradeEngine.run() (pre_commands -> download/extract dst package ->
        radm dependency -> wait_elasticsearch -> radm application ->
        radm cluster_old -> post_commands -> radm cluster_new).

        By the time this test body runs, the fixture has already either
        succeeded (and this is just confirming the extract dir moved to the
        NEW package) or raised, which pytest surfaces as a fixture error
        rather than a test failure -- either way, nothing else in this file
        should run against a half-upgraded box if it failed.
        """
        extract_dir = getattr(package_profile, "_actual_extract_dir", None)
        attach_output(extras, "post-upgrade extract dir", extract_dir or "NOT SET")
        assert extract_dir, (
            "controller_upgrade fixture did not set _actual_extract_dir -- "
            "upgrade engine may not have completed successfully"
        )


class TestPostUpgradeHealth:
    """Validate cluster state after the upgrade completes."""

    def test_all_pods_running(self, ssh_client, extras):
        out, rc = ssh_client.run("kubectl get pods -A --no-headers 2>/dev/null")
        attach_output(extras, "post-upgrade pod status", out)
        assert rc == 0, "kubectl get pods failed after upgrade"
        bad = [
            l for l in out.splitlines()
            if any(s in l for s in ("Pending", "Error", "CrashLoop", "Init:", "OOMKilled"))
        ]
        assert not bad, f"{len(bad)} unhealthy pod(s) after upgrade:\n" + "\n".join(bad)

    def test_dst_version_installed(self, ssh_client, package_profile, extras):
        """Confirm the NEW (dst) version is what's actually on disk now."""
        installed = _detect_installed_version(ssh_client)
        attach_output(extras, "post-upgrade installed version", installed or "UNKNOWN")

        dst_version = getattr(package_profile, "_dst_version", None) or getattr(
            package_profile, "version", ""
        )
        assert installed, "Could not detect installed version after upgrade"
        if dst_version:
            assert dst_version in installed or installed in dst_version, (
                f"Expected dst version '{dst_version}' installed, found '{installed}'"
            )

    def test_ha_master_node_count(self, ssh_client, controller_profile, extras):
        """HA=3 masters, Non-HA=1 master -- upgrade must not have dropped a node."""
        out, rc = ssh_client.run(
            "kubectl get nodes --no-headers -l node-role.kubernetes.io/control-plane 2>&1"
        )
        attach_output(extras, "master nodes (post-upgrade)", out)
        master_count = len([l for l in out.splitlines() if l.strip()])
        expected = 3 if controller_profile.ha else 1
        assert master_count == expected, (
            f"Expected {expected} master(s) after upgrade -- found {master_count}"
        )

    def test_console_endpoint_reachable(self, ssh_client, extras):
        out, rc = ssh_client.run(
            "curl -sk -o /dev/null -w '%{http_code}' https://localhost/ || echo FAILED"
        )
        attach_output(extras, "console HTTP status (post-upgrade)", out)
        assert out.strip() not in ("000", "FAILED"), (
            "Console endpoint not responding after upgrade"
        )