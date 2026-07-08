"""
tests/controller/test_console_login.py

Console login + dashboard validation + org/user creation tests.

dev.yaml required:
    console:
      email: "admin@rafay.co"
      password: "change123"
      mfa_secret: ""          # auto-populated after first QR scan
      partner_id: "4qkolkn"   # from browser DevTools on signup request
      test_org:
        name: "onprem-qa"
        email: "onprem@rafay.co"
        password: "changeplz"
        first_name: "onprem"
        last_name: "qa"

Run standalone:
    pytest tests/controller/test_console_login.py \
        --skip-bringup --controller-ip=X.X.X.X --build-no=3 --keep-vm \
        --html=report.html --self-contained-html -s
"""

import re
import time
import pytest
import yaml
import requests
from pathlib import Path

pytestmark = [pytest.mark.order(2), pytest.mark.controller, pytest.mark.console]


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def attach_output(extras, label: str, content: str):
    """Attach text block to pytest-html report."""
    try:
        import pytest_html
        block = f"<pre style='font-size:12px;white-space:pre-wrap'>{content}</pre>"
        item  = pytest_html.extras.html(f"<b>{label}</b>{block}")
        extras.append(item)
    except Exception as e:
        print(f"[attach_output] failed: {e}")


def attach_screenshot(extras, label: str, screenshot_bytes: bytes):
    """Attach PNG screenshot to pytest-html report."""
    try:
        import pytest_html
        import base64
        b64  = base64.b64encode(screenshot_bytes).decode()
        item = pytest_html.extras.html(
            f"<b>{label}</b><br>"
            f"<img src='data:image/png;base64,{b64}' "
            f"style='max-width:900px;border:1px solid #ccc;margin-top:6px'/>"
        )
        extras.append(item)
        print(f"[attach_screenshot] ✓ attached '{label}' ({len(screenshot_bytes)} bytes)")
    except Exception as e:
        print(f"[attach_screenshot] failed: {e}")


def _save_secret_to_config(env: str, secret: str):
    """Save TOTP secret to dev.yaml after first QR scan."""
    config_path = Path(__file__).parent.parent.parent / "config" / f"{env}.yaml"
    if not config_path.exists():
        print(f"[console_login] config not found: {config_path}")
        return
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        cfg.setdefault("console", {})["mfa_secret"] = secret
        with open(config_path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        print(f"[console_login] TOTP secret saved → {config_path}")
    except Exception as e:
        print(f"[console_login] Could not save secret: {e}")


def _get_star_domain(request, controller_fqdn, raw_config) -> str:
    """Resolve star_domain from controller_fqdn or --build-no + base_domain."""
    if controller_fqdn:
        return controller_fqdn.lstrip("*.")
    base   = raw_config.get("dns", {}).get("base_domain", "")
    build  = request.config.getoption("--build-no") or ""
    return f"shc-{build}.{base}" if base and build else ""


time.sleep(3)
def _browser_login_and_screenshot(url: str, email: str, password: str) -> tuple:
    """
    Login to a Rafay console URL via playwright browser.
    Returns: (screenshot_bytes, dashboard_url)
    No MFA expected for newly created org users.
    """
    from playwright.sync_api import sync_playwright

    screenshot_bytes = None
    dashboard_url    = ""

    pw      = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"]
    )
    ctx  = browser.new_context(
        ignore_https_errors=True,
        viewport={"width": 1920, "height": 1080}
    )
    page = ctx.new_page()

    try:
        print(f"[browser_login] Navigating to {url} ...")
        page.goto(url + "/", wait_until="networkidle")
        time.sleep(2)

        # Email
        email_loc = page.locator(
            "input[type='email'], input[name*='email'], "
            "input[placeholder*='email' i], input[placeholder*='username' i]"
        ).first
        email_loc.wait_for(state="visible", timeout=15000)
        email_loc.fill(email)
        page.keyboard.press("Enter")
        print(f"[browser_login] Email: {email}")

        # Password
        page.locator("input[type='password']").first.wait_for(state="visible", timeout=10000)
        page.locator("input[type='password']").first.fill(password)
        page.locator("button[type='submit']").first.click()
        print(f"[browser_login] Password entered")

        # Wait for dashboard
        page.wait_for_url(
            lambda u: "login" not in u and "mfa" not in u,
            timeout=15000
        )
        time.sleep(2)

        dashboard_url    = page.url
        screenshot_bytes = page.screenshot(full_page=False)
        print(f"[browser_login] ✓ Dashboard: {dashboard_url} ({len(screenshot_bytes)} bytes)")

    except Exception as e:
        print(f"[browser_login] Error: {e}")
        screenshot_bytes = page.screenshot(full_page=False)
        dashboard_url    = page.url
    finally:
        browser.close()
        pw.stop()

    return screenshot_bytes, dashboard_url


def _get_authenticated_session(ops_url: str, email: str,
                                password: str, mfa_secret: str) -> tuple:
    """
    Login to ops-console via playwright to get csrftoken + authenticated cookies.
    Returns: (csrftoken, requests.Session)
    """
    from lib.console.mfa_login import ConsoleLogin
    from playwright.sync_api import sync_playwright

    print(f"[auth_session] Logging in to get CSRF cookies ...")
    console = ConsoleLogin(url=ops_url, email=email,
                           password=password, mfa_secret=mfa_secret or None)
    result  = console.login()
    if not result.success:
        raise RuntimeError(f"Admin login failed: {result.error}")

    # Re-login via playwright to grab all cookies including csrftoken
    pw      = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
    )
    ctx  = browser.new_context(ignore_https_errors=True)
    page = ctx.new_page()

    try:
        page.goto(ops_url + "/", wait_until="networkidle")

        email_loc = page.locator(
            "input[type='email'], input[name*='email'], input[placeholder*='email' i]"
        ).first
        email_loc.wait_for(state="visible", timeout=15000)
        email_loc.fill(email)
        page.keyboard.press("Enter")

        page.locator("input[type='password']").first.wait_for(state="visible", timeout=10000)
        page.locator("input[type='password']").first.fill(password)
        page.locator("button[type='submit']").first.click()

        # Handle MFA
        try:
            page.wait_for_selector(
                "input[name='verify_token'], input[placeholder='Enter 6-digit code']",
                timeout=10000
            )
            if mfa_secret:
                import pyotp
                totp = pyotp.TOTP(mfa_secret)
                remaining = totp.interval - (int(time.time()) % totp.interval)
                if remaining < 5:
                    time.sleep(remaining + 1)
                page.locator(
                    "input[name='verify_token'], input[placeholder='Enter 6-digit code']"
                ).first.fill(totp.now())
                page.locator("button:visible").first.click()
        except Exception:
            pass

        page.wait_for_url(lambda u: "login" not in u, timeout=15000)
        time.sleep(2)

        all_cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        csrftoken   = all_cookies.get("csrftoken", "")
        print(f"[auth_session] cookies: {list(all_cookies.keys())}")
        print(f"[auth_session] csrftoken: {csrftoken[:10]}..." if csrftoken else "[auth_session] csrftoken: EMPTY")

    finally:
        browser.close()
        pw.stop()

    session = requests.Session()
    session.verify = False
    for name, value in all_cookies.items():
        session.cookies.set(name, value)

    return csrftoken, session


# ─────────────────────────────────────────────────────────────────────────────
# TestConsoleLogin
# ─────────────────────────────────────────────────────────────────────────────

class TestConsoleLogin:
    """
    Login to ops-console via MFA and validate dashboard.
    First run  → scans QR, extracts secret, saves to dev.yaml
    Subsequent → reads secret from dev.yaml or --mfa-secret CLI flag
    """

    @pytest.fixture(autouse=True)
    def skip_if_bringup_failed(self, request):
        if getattr(request.session, "_bringup_failed", False):
            pytest.skip("Bringup did not complete — skipping console login tests")

    def test_console_url_reachable(self, request, controller_fqdn,
                                    raw_config, ssh_client, extras):
        """ops-console must return HTTP 200 before login attempt."""
        star_domain = _get_star_domain(request, controller_fqdn, raw_config)
        if not star_domain:
            pytest.skip("star_domain not set — skipping")

        url = f"https://ops-console.{star_domain}"
        out, rc = ssh_client.run(
            f"curl -sk -o /dev/null -w '%{{http_code}}' {url}/ || echo FAILED"
        )
        attach_output(extras, f"curl {url}", out.strip())
        assert out.strip() not in ("000", "FAILED"), \
            f"ops-console not reachable — got: {out.strip()}"
        print(f"[console_url] HTTP {out.strip()}")

    def test_console_login_and_dashboard(self, request, controller_fqdn,
                                          raw_config, extras):
        """Login to ops-console via MFA, capture dashboard screenshot."""
        from lib.console.mfa_login import ConsoleLogin

        star_domain = _get_star_domain(request, controller_fqdn, raw_config)
        assert star_domain, "Cannot determine console URL — pass --build-no"

        console_url = f"https://ops-console.{star_domain}"
        console_cfg = raw_config.get("console", {})
        email       = console_cfg.get("email",    "admin@rafay.co")
        password    = console_cfg.get("password", "change123")
        mfa_secret  = (
            request.config.getoption("--mfa-secret", default=None)
            or console_cfg.get("mfa_secret")
            or None
        )

        attach_output(extras, "Console URL", console_url)
        attach_output(extras, "Email", email)
        attach_output(extras, "MFA secret source",
                      "CLI" if request.config.getoption("--mfa-secret", default=None)
                      else "dev.yaml" if console_cfg.get("mfa_secret")
                      else "QR scan")

        print(f"\n[console_login] Logging in to {console_url} ...")
        result = ConsoleLogin(url=console_url, email=email,
                              password=password, mfa_secret=mfa_secret).login()

        if result.screenshot:
            attach_screenshot(extras, "ops-console dashboard screenshot", result.screenshot)

        if result.success and result.secret and result.secret != mfa_secret:
            _save_secret_to_config(
                request.config.getoption("--env", default="dev"),
                result.secret
            )
            # Stash on the session too — raw_config is session-scoped and was
            # already loaded from disk, so a later test in this same run would
            # otherwise keep reading the stale value even after the YAML write.
            request.session._fresh_mfa_secret = result.secret
            attach_output(extras, "TOTP secret", "Saved to dev.yaml (fresh secret)")

        assert result.success, f"Login failed: {result.error}"
        attach_output(extras, "Dashboard URL", result.url)

        elements = result.dashboard.get("elements", [])
        attach_output(extras, "Dashboard elements", "\n".join(elements) or "none")
        assert len(elements) > 0, f"Dashboard empty after login — URL: {result.url}"
        print(f"[console_login] Dashboard: {elements}")


# ─────────────────────────────────────────────────────────────────────────────
# TestOrgAndUser
# ─────────────────────────────────────────────────────────────────────────────

class TestOrgAndUser:
    """
    Create org + user via signup API, then verify login and capture screenshot.

    Flow:
      1. test_create_org_and_user → POST /auth/v1/signup/organization/
      2. test_prelogin_check      → POST /auth/v1/prelogin/
      3. test_user_login          → POST /auth/v1/login/ + browser screenshot
    """

    @pytest.fixture(autouse=True)
    def skip_if_bringup_failed(self, request):
        if getattr(request.session, "_bringup_failed", False):
            pytest.skip("Bringup did not complete — skipping org/user tests")

    def _resolve(self, request, controller_fqdn, raw_config):
        star_domain = _get_star_domain(request, controller_fqdn, raw_config)
        assert star_domain, "Cannot determine console URL — pass --build-no"
        console_cfg = raw_config.get("console", {})
        org_cfg     = console_cfg.get("test_org", {})
        partner_id  = console_cfg.get("partner_id", "")
        assert partner_id, (
            "x-rafay-partner not set.\n"
            "Add to dev.yaml:\n  console:\n    partner_id: '4qkolkn'\n"
            "Find it: DevTools → Network → signup POST → x-rafay-partner header"
        )
        return (
            star_domain,
            f"https://ops-console.{star_domain}",
            f"https://console.{star_domain}",
            console_cfg,
            org_cfg,
            partner_id,
        )

    def test_create_org_and_user(self, request, controller_fqdn,
                                  raw_config, extras):
        """Create org + admin user via POST /auth/v1/signup/organization/"""
        star_domain, ops_url, _, console_cfg, org_cfg, partner_id = \
            self._resolve(request, controller_fqdn, raw_config)

        org_name   = org_cfg.get("name",       "onprem-qa")
        username   = org_cfg.get("email",      "onprem@rafay.co")
        password   = org_cfg.get("password",   "changeplz")
        first_name = org_cfg.get("first_name", "onprem")
        last_name  = org_cfg.get("last_name",  "qa")

        attach_output(extras, "Ops URL",    ops_url)
        attach_output(extras, "Org name",   org_name)
        attach_output(extras, "Username",   username)
        attach_output(extras, "Partner ID", partner_id)

        # Need authenticated session for signup API
        admin_email    = console_cfg.get("email",    "admin@rafay.co")
        admin_password = console_cfg.get("password", "change123")
        # Prefer the secret refreshed earlier this session (handles the
        # controller-re-provisioned / fresh-QR-scan case) over the
        # session-cached raw_config value, which may now be stale even
        # though dev.yaml on disk was updated.
        admin_secret   = getattr(request.session, "_fresh_mfa_secret", None) \
                          or console_cfg.get("mfa_secret") or ""

        csrftoken, session = _get_authenticated_session(
            ops_url, admin_email, admin_password, admin_secret
        )
        attach_output(extras, "CSRF token", csrftoken[:15] + "..." if csrftoken else "EMPTY")

        headers = {
            "accept":          "application/json, text/plain, */*",
            "content-type":    "application/json",
            "x-csrftoken":     csrftoken,
            "x-rafay-partner": partner_id,
            "origin":          ops_url,
            "referer":         ops_url + "/",
        }
        payload = {
            "username":          username,
            "password":          password,
            "organization_name": org_name,
            "first_name":        first_name,
            "last_name":         last_name,
            "role":              "ADMIN",
        }

        print(f"[org_user] Creating org '{org_name}' + user '{username}' ...")
        resp = session.post(
            f"{ops_url}/auth/v1/signup/organization/",
            json=payload, headers=headers, timeout=30,
        )

        attach_output(extras, "Signup status",   str(resp.status_code))
        attach_output(extras, "Signup response", resp.text[:500])

        # 409 = org already exists from previous run — that's fine
        if resp.status_code == 409:
            print(f"[org_user] Org '{org_name}' already exists — continuing")
        else:
            assert resp.status_code in (200, 201), (
                f"Signup failed ({resp.status_code}): {resp.text[:300]}"
            )
            print(f"[org_user] ✓ Org + user created")

        request.session._test_username = username
        request.session._test_password = password
        request.session._star_domain   = star_domain

    def test_prelogin_check(self, request, controller_fqdn,
                             raw_config, extras):
        """POST /auth/v1/prelogin/ — verify user exists in system."""
        _, _, console_url, _, org_cfg, _ = \
            self._resolve(request, controller_fqdn, raw_config)

        username = org_cfg.get("email", "onprem@rafay.co")
        session  = requests.Session()
        session.verify = False

        print(f"[org_user] Pre-login check for '{username}' ...")
        resp = session.post(
            f"{console_url}/auth/v1/prelogin/",
            json={"username": username, "organization": ""},
            headers={"accept": "application/json", "content-type": "application/json",
                     "origin": console_url, "referer": console_url + "/"},
            timeout=15,
        )

        attach_output(extras, "Pre-login status",   str(resp.status_code))
        attach_output(extras, "Pre-login response", resp.text[:300])
        assert resp.status_code == 200, \
            f"Pre-login failed ({resp.status_code}): {resp.text[:200]}"
        print(f"[org_user] ✓ Pre-login passed")

    def test_user_login(self, request, controller_fqdn,
                         raw_config, extras):
        """
        Login as org user via POST /auth/v1/login/.
        Verifies session cookie returned, then captures browser screenshot.
        """
        _, _, console_url, _, org_cfg, _ = \
            self._resolve(request, controller_fqdn, raw_config)

        username = org_cfg.get("email",    "onprem@rafay.co")
        password = org_cfg.get("password", "changeplz")

        # ── Step 1: API login to verify credentials ───────────────────────────
        session = requests.Session()
        session.verify = False

        print(f"[org_user] API login as '{username}' ...")
        resp = session.post(
            f"{console_url}/auth/v1/login/",
            json={"username": username, "password": password,
                  "organization": "", "usertype": "internal"},
            headers={"accept": "application/json", "content-type": "application/json",
                     "origin": console_url, "referer": console_url + "/"},
            timeout=15,
        )

        attach_output(extras, "Login status",   str(resp.status_code))
        attach_output(extras, "Login response", resp.text[:300])
        assert resp.status_code == 200, \
            f"User login failed ({resp.status_code}): {resp.text[:200]}"

        rsid = session.cookies.get("rsid", "")
        attach_output(extras, "Session cookie (rsid)",
                      rsid[:20] + "..." if rsid else "MISSING")
        assert rsid, "Login OK but no rsid cookie"
        print(f"[org_user] ✓ API login successful — rsid: {rsid[:10]}...")

        # ── Step 2: Browser login + screenshot ───────────────────────────────
        print(f"[org_user] Opening browser for dashboard screenshot ...")
        screenshot_bytes, dashboard_url = _browser_login_and_screenshot(
            console_url, username, password
        )

        if screenshot_bytes:
            # Save to disk
            debug_path = "/tmp/user_dashboard.png"
            with open(debug_path, "wb") as f:
                f.write(screenshot_bytes)
            print(f"[org_user] Screenshot saved: {debug_path}")

            # Attach to report
            attach_screenshot(extras, "console dashboard screenshot", screenshot_bytes)
            attach_output(extras, "console dashboard URL", dashboard_url)
        else:
            print(f"[org_user] No screenshot captured")