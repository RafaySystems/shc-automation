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
    controller_upgrade   -- session-scoped (NOT autouse -- see conftest.py's
                           comment on this fixture for why). Runs
                           engine.run() the first time anything requests
                           it as a parameter, and sets
                           package_profile._actual_extract_dir to the NEW
                           (dst) extract dir on success. Cached afterward
                           for the rest of the session.

IMPORTANT — fixture timing / ordering: controller_upgrade is no longer
autouse, so it does NOT fire before every other test in the session. It
only runs the first time a test explicitly requests it as a parameter --
which TestUpgradeExecution.test_upgrade_completes below does. Because
test_console_login.py collects alphabetically before this file, the
intended sequence is:

    controller_bringup (autouse, always first)
      -> test_console_login.py: signup + login (creates the org/user)
      -> this file's tests run, in order:
           TestUpgradeReportedState  -- genuinely pre-upgrade now (the
                                        upgrade hasn't fired yet at this
                                        point in a normal full-suite run)
           TestUpgradeExecution      -- REQUESTS controller_upgrade,
                                        which is what actually triggers
                                        the upgrade to run
           TestPostUpgradeHealth     -- runs after, sees the upgraded state
           TestPostUpgradeLogin      -- confirms the org/user created
                                        BEFORE the upgrade can still log
                                        in AFTER it
"""

import re
import pytest

pytestmark = [pytest.mark.order(2), pytest.mark.controller, pytest.mark.upgrade]


def _extract_version(pkg: str) -> str:
    """Same pattern as conftest.py's extract_version() -- 'v' prefix optional."""
    m = re.search(r'v?([\d.]+-\d+)\.tar\.gz', pkg or "")
    return m.group(1) if m else ""


def attach_output(extras, label: str, content: str):
    """
    Embed command output into the pytest-html report (3.x/4.x compatible).
    Also mirrors the same content to the Allure report (if allure is
    importable), so results are visible in whichever report is open
    without needing to SSH to the Jenkins node.
    """
    import pytest_html
    block = f"<pre style='font-size:12px;white-space:pre-wrap'>{content}</pre>"
    item = pytest_html.extras.html(f"<b>{label}</b>{block}")
    if hasattr(extras, "append"):
        extras.append(item)
    else:
        extras.extend([item])

    try:
        import allure
        allure.attach(content, name=label, attachment_type=allure.attachment_type.TEXT)
    except Exception:
        # Allure not installed / not active in this run -- pytest-html
        # attachment above still succeeded, so don't fail the test over this.
        pass


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


def _resolve_console_url(request, raw_config) -> str:
    """
    Resolve the org/user-facing console URL (console.{star_domain}, NOT
    ops-console.{star_domain}) from --build-no + dns.base_domain in
    dev.yaml. Same convention test_console_login.py's TestOrgAndUser uses
    for the same purpose. Returns "" if it can't be determined.
    """
    base_domain  = raw_config.get("dns", {}).get("base_domain", "")
    build_no_val = request.config.getoption("--build-no") or ""
    if not (base_domain and build_no_val):
        return ""
    star_domain = f"shc-{build_no_val}.{base_domain}"
    return f"https://console.{star_domain}"


class TestUpgradeReportedState:
    """
    Informational checks recorded for the report. In a full-suite run,
    these now genuinely run BEFORE the upgrade fixture has fired (since
    controller_upgrade is no longer autouse) -- see module docstring.
    They are still not gates; their value is making it easy to see, from
    the pytest-html/Allure report, what version was installed going in
    and whether the box was already healthy before this file touched it.
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
                f"check Jenkins parameters if this looks wrong."
            )


class TestUpgradeExecution:
    """
    Drives the actual upgrade via the controller_upgrade fixture.

    Requesting the controller_upgrade fixture here is what actually TRIGGERS
    it to run -- it's session-scoped but no longer autouse, so nothing
    upgrades the controller until this test (or another test requesting the
    same fixture) executes. This is the deliberate hook point that lets
    signup/login in test_console_login.py run beforehand.
    """

    def test_upgrade_completes(self, controller_upgrade, package_profile, extras):
        """
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
        """
        NOTE: this only checks that SOMETHING responds over HTTP -- it does
        NOT confirm that a real user can actually authenticate. For that
        guarantee, see TestPostUpgradeLogin.test_existing_user_can_still_login
        below, which performs a genuine login using credentials created
        before the upgrade.
        """
        out, rc = ssh_client.run(
            "curl -sk -o /dev/null -w '%{http_code}' https://localhost/ || echo FAILED"
        )
        attach_output(extras, "console HTTP status (post-upgrade)", out)
        assert out.strip() not in ("000", "FAILED"), (
            "Console endpoint not responding after upgrade"
        )


class TestPostUpgradeLogin:
    """
    Confirm the org/user created BEFORE the upgrade (in
    test_console_login.py::TestOrgAndUser.test_create_org_and_user) can
    still authenticate AFTER the upgrade completes.

    This is a materially different guarantee than
    TestPostUpgradeHealth.test_console_endpoint_reachable: that test only
    confirms the console PROCESS is up and responding to HTTP. This test
    confirms that auth/session/user-store data actually survived the
    upgrade intact -- i.e. a real person who signed up before the upgrade
    can still get into their account afterward, which is the thing an
    end user actually cares about.

    Deliberately reuses the SAME credentials test_console_login.py's
    TestOrgAndUser created (console.test_org in dev.yaml) rather than
    creating a new user here -- the whole point is to verify the
    PRE-EXISTING user survived, not to test signup again.
    """

    def test_existing_user_can_still_login(self, controller_upgrade, request,
                                            raw_config, extras):
        """
        Requesting controller_upgrade here (same as TestUpgradeExecution)
        guarantees the upgrade has actually run before this test's body
        executes, regardless of what order pytest happens to collect
        files/classes in -- it's session-scoped, so if it already ran
        (the normal case, since TestUpgradeExecution runs first in this
        file), this just reuses the cached result at no extra cost.
        """
        import requests

        console_url = _resolve_console_url(request, raw_config)
        assert console_url, "Cannot determine console URL -- pass --build-no"

        org_cfg  = raw_config.get("console", {}).get("test_org", {})
        username = org_cfg.get("email",    "onprem@rafay.co")
        password = org_cfg.get("password", "changeplz")

        attach_output(extras, "Console URL", console_url)
        attach_output(extras, "Username (created pre-upgrade)", username)

        session = requests.Session()
        session.verify = False

        print(f"[test_existing_user_can_still_login] Logging in as pre-upgrade "
              f"user '{username}' post-upgrade ...")
        resp = session.post(
            f"{console_url}/auth/v1/login/",
            json={
                "username": username,
                "password": password,
                "organization": "",
                "usertype": "internal",
            },
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": console_url,
                "referer": console_url + "/",
            },
            timeout=15,
        )

        attach_output(extras, "Post-upgrade login status", str(resp.status_code))
        attach_output(extras, "Post-upgrade login response", resp.text[:300])
        assert resp.status_code == 200, (
            f"User '{username}' (created before the upgrade) could not log in "
            f"after the upgrade ({resp.status_code}): {resp.text[:200]}\n"
            f"This means user/session/auth data did not survive the upgrade "
            f"intact -- a real customer would be locked out of their account."
        )

        rsid = session.cookies.get("rsid", "")
        attach_output(extras, "Session cookie (rsid)",
                      rsid[:20] + "..." if rsid else "MISSING")
        assert rsid, (
            "Login returned 200 but no rsid cookie was set -- "
            "session was not actually established"
        )
        print(f"[test_existing_user_can_still_login] ✓ pre-upgrade user "
              f"'{username}' logged in successfully post-upgrade — rsid: {rsid[:10]}...")