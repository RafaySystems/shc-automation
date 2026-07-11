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

NOTE on --src-package: package_profile is built by conftest.py from
--src-package (falling back to --package-name). If left blank, the
engine still runs off whatever src_version/src_package the fixture
resolves internally -- this file's own detection of "installed version"
is purely for the report, not for deciding what to upgrade from.

Requires fixtures already defined in conftest.py:
    ssh_client          -- connects to --controller-ip when --skip-bringup
                           is set, bypassing Terraform provisioning entirely
    controller_profile   -- size/ha/os_type profile
    package_profile      -- src package info (name/version/url/tar_path),
                           built from --src-package (falls back to
                           --package-name if that's unset)
    controller_upgrade   -- autouse, session-scoped. Wraps UpgradeEngine;
                           runs engine.run() and sets
                           package_profile._actual_extract_dir to the NEW
                           (dst) extract dir on success

IMPORTANT — fixture timing: controller_upgrade is autouse + session-scoped
and depends on controller_bringup (also autouse). Both run during the
SETUP of the first test in this file that pytest executes -- i.e. by the
time ANY test body below runs, the upgrade has already happened. There is
no way to write a true "before upgrade" check as a test in this file; the
classes below are therefore purely POST-HOC validation, same pattern as
TestRadmInstall in test_controller_install.py. Nothing here gates or
delays the upgrade itself -- if you need genuine preconditions enforced
before upgrading, those belong inside UpgradeEngine.run() itself, or in a
separate earlier pytest session (e.g. a smoke-test file run first in the
Jenkins pipeline).
"""

import re
import pytest

pytestmark = [pytest.mark.order(2), pytest.mark.controller, pytest.mark.upgrade]


def _extract_version(pkg: str) -> str:
    """Same pattern as conftest.py's extract_version() -- 'v' prefix optional."""
    m = re.search(r'v?([\d.]+-\d+)\.tar\.gz', pkg or "")
    return m.group(1) if m else ""


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
    reading the extracted package directory name under /opt/rafay. Used
    purely for report/diagnostic purposes in this file -- see module
    docstring on why these checks can't gate the upgrade (autouse fixture
    timing). Falls back to empty string if nothing is found.
    """
    out, rc = ssh_client.run(
        "ls -1 /opt/rafay/ 2>/dev/null | grep '^rafay-airgapped-controller' | head -1"
    )
    if rc != 0 or not out.strip():
        return ""
    m = re.search(r"v?([\d.]+-\d+)", out.strip())
    return m.group(1) if m else ""


class TestUpgradeReportedState:
    """
    Informational checks recorded for the report. These run AFTER the
    upgrade already happened (see module docstring on fixture timing) --
    they are not gates. Their main value is making it easy to see, from
    the pytest-html/Allure report, what version was installed going in
    and whether the box was already healthy before this run touched it.
    """

    def test_record_pre_upgrade_context(self, ssh_client, package_profile, extras):
        """Record src version + pod state for the report (informational only)."""
        installed = _detect_installed_version(ssh_client)
        attach_output(extras, "installed version (detected, post-hoc)", installed or "UNKNOWN")

        declared = getattr(package_profile, "version", "") or ""
        attach_output(extras, "--src-package declared version", declared or "(not supplied)")

        if declared and installed and declared not in installed and installed not in declared:
            print(
                f"[test_record_pre_upgrade_context] NOTE: --src-package declared "
                f"'{declared}' but detected extract dir suggests '{installed}' -- "
                f"by this point the upgrade has already run, so this is informational "
                f"only, not a gate. Check Jenkins parameters if this looks wrong."
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

    def test_dst_version_installed(self, ssh_client, package_profile, request, extras):
        """
        Confirm the NEW (dst) version is what's actually active post-upgrade.

        NOTE: this deliberately does NOT use _detect_installed_version()'s
        `ls /opt/rafay/ | grep ... | head -1` approach. The upgrade never
        deletes the old (src) extract directory, so /opt/rafay/ contains
        BOTH rafay-airgapped-controller-v3.1-39/ and .../v3.1-40/ side by
        side after a successful upgrade. `ls` sorts alphabetically, and
        "39" < "40" character-by-character, so `head -1` deterministically
        picks the OLD directory regardless of which version actually ran --
        this caused a false failure even on a fully successful upgrade.

        Instead, use package_profile._actual_extract_dir, which the
        controller_upgrade fixture in conftest.py explicitly sets to
        engine.dst_extract_dir on success -- that's the actual source of
        truth for "what did the upgrade just point the cluster at",
        rather than re-deriving it from a directory listing.
        """
        actual_extract_dir = getattr(package_profile, "_actual_extract_dir", "") or ""
        attach_output(extras, "post-upgrade active extract dir", actual_extract_dir or "UNKNOWN")

        installed = _extract_version(actual_extract_dir + ".tar.gz") or actual_extract_dir
        attach_output(extras, "post-upgrade installed version (from active extract dir)", installed or "UNKNOWN")

        dst_package = request.config.getoption("--dst-package") or ""
        dst_version = (
            request.config.getoption("--dst-version") or _extract_version(dst_package)
        )
        attach_output(extras, "--dst-package declared version", dst_version or "UNKNOWN")

        assert actual_extract_dir, (
            "package_profile._actual_extract_dir was never set -- "
            "controller_upgrade fixture may not have completed successfully"
        )
        if dst_version:
            assert dst_version in installed or installed in dst_version, (
                f"Expected dst version '{dst_version}' installed, found '{installed}' "
                f"(active extract dir: {actual_extract_dir})"
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