"""
lib/console/mfa_login.py

Wraps rafay_mfa_login_playwright.py as a reusable class.
Handles both first-run (QR scan) and subsequent runs (saved secret).
Includes OTP retry logic to handle TOTP window timing issues.
"""

import urllib.parse
import base64
import io
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LoginResult:
    success:    bool
    url:        str
    secret:     str
    screenshot: bytes
    dashboard:  dict = field(default_factory=dict)
    error:      str = ""


class ConsoleLogin:

    def __init__(
        self,
        url:        str,
        email:      str     = "admin@rafay.co",
        password:   str     = "change123",
        mfa_secret: Optional[str] = None,
    ):
        self.url        = url.rstrip("/")
        self.email      = email
        self.password   = password
        self.mfa_secret = mfa_secret

    def login(self) -> LoginResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise ImportError(
                "playwright not installed.\n"
                "Run: pip install playwright && playwright install chromium"
            )

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            context = browser.new_context(
                ignore_https_errors=True,
                viewport={"width": 1920, "height": 1080}
            )
            page = context.new_page()

            try:
                secret    = self._do_login(page)
                dashboard = self._capture_dashboard(page)
                screenshot = page.screenshot(full_page=False)
                return LoginResult(
                    success=True,
                    url=page.url,
                    secret=secret,
                    screenshot=screenshot,
                    dashboard=dashboard,
                )
            except Exception as e:
                screenshot = page.screenshot(full_page=False)
                return LoginResult(
                    success=False,
                    url=page.url,
                    secret=self.mfa_secret or "",
                    screenshot=screenshot,
                    error=str(e),
                )
            finally:
                browser.close()

    # ── Login flow ────────────────────────────────────────────────────────────

    def _do_login(self, page) -> str:
        import pyotp

        print(f"[console_login] Navigating to {self.url}")
        page.goto(self.url, wait_until="networkidle")
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)

        print(f"[console_login] Page URL: {page.url}")
        print(f"[console_login] Page title: {page.title()}")

        # Step 1: Email
        print("[console_login] Entering email ...")
        email_input = page.locator(
            "input[type='email'], "
            "input[name*='email'], "
            "input[placeholder*='email' i], "
            "input[placeholder*='username' i], "
            "input[autocomplete='email'], "
            "input[autocomplete='username']"
        ).first
        email_input.wait_for(state="visible", timeout=30000)
        email_input.fill(self.email)
        page.keyboard.press("Enter")

        # Step 2: Password
        print("[console_login] Entering password ...")
        pwd_input = page.locator("input[type='password']").first
        pwd_input.wait_for(state="visible", timeout=15000)
        pwd_input.fill(self.password)

        submit = page.locator("button[type='submit']")
        if submit.count() == 0:
            submit = page.locator("button").filter(has_text="Login").or_(
                     page.locator("button").filter(has_text="Sign in")).or_(
                     page.locator("button").filter(has_text="Continue"))
        submit.first.click()

        # Step 3: MFA page
        print("[console_login] Waiting for MFA page ...")
        page.wait_for_selector(
            "input[name='verify_token'], input[placeholder='Enter 6-digit code']",
            timeout=15000
        )

        mfa_type = self._detect_mfa_page(page)
        print(f"[console_login] MFA type: {mfa_type}")

        secret = self.mfa_secret

        if mfa_type == "enrollment" and not secret:
            # First run — scan QR and extract secret
            secret = self._scan_qr(page)
            print(f"[console_login] TOTP secret extracted from QR: {secret}")

        elif mfa_type == "enrollment" and secret:
            # Secret provided but enrollment page shown — controller re-brough up
            # Old secret is stale — scan fresh QR instead
            print(f"[console_login] Enrollment page shown but secret exists "
                  f"— controller may have been re-deployed, scanning fresh QR ...")
            secret = self._scan_qr(page)
            print(f"[console_login] Fresh TOTP secret extracted: {secret}")

        elif mfa_type == "otp" and not secret:
            # OTP page + no secret — check if QR is hidden in page
            has_qr = page.locator("canvas, img[src*='qr']").count() > 0
            if has_qr:
                print("[console_login] OTP detected but QR visible — scanning QR ...")
                secret = self._scan_qr(page)
            else:
                raise ValueError(
                    "OTP page shown but no mfa_secret provided.\n"
                    "Pass --mfa-secret on CLI or set console.mfa_secret in dev.yaml"
                )

        elif mfa_type == "otp" and secret:
            # Normal subsequent run — use saved secret
            print(f"[console_login] Using saved TOTP secret for OTP login")

        else:
            page.screenshot(path="/tmp/debug_mfa_unknown.png")
            raise RuntimeError(
                f"Unknown MFA page state: {mfa_type}. "
                f"Screenshot saved to /tmp/debug_mfa_unknown.png"
            )

        # Step 4: Enter OTP with retry on timing failure
        # ── Key fix: TOTP codes are only valid for 30s.
        # If we generate the code and then the window expires before
        # the server validates it, we get "Could not validate" error.
        # Solution: wait until start of a new 30s window before generating,
        # and retry up to 3 times if validation fails.
        totp         = pyotp.TOTP(secret)
        max_attempts = 3

        for attempt in range(1, max_attempts + 1):

            # Wait if we're near the END of a TOTP window (< 5s remaining)
            # to avoid submitting a code that expires mid-validation
            remaining = totp.interval - (int(time.time()) % totp.interval)
            if remaining < 5:
                print(f"[console_login] OTP window ending in {remaining}s "
                      f"— waiting for fresh window ...")
                time.sleep(remaining + 1)
                remaining = totp.interval  # reset after wait

            otp_code = totp.now()
            print(f"[console_login] Attempt {attempt}/{max_attempts} "
                  f"— OTP: {otp_code} "
                  f"(~{totp.interval - (int(time.time()) % totp.interval)}s remaining)")

            # Fill OTP input
            otp_input = page.locator(
                "input[name='verify_token'], "
                "input[placeholder='Enter 6-digit code']"
            ).first
            otp_input.fill(otp_code)
            self._click_submit(page)

            # Check if we navigated to dashboard (success)
            try:
                page.wait_for_url(
                    lambda u: "mfa" not in u and "login" not in u,
                    timeout=8000
                )
                print(f"[console_login] ✓ Login successful — {page.url}")
                return secret

            except Exception:
                # Still on login/mfa page
                if attempt < max_attempts:
                    print(f"[console_login] ⚠ OTP attempt {attempt} failed "
                          f"— waiting for next TOTP window ...")
                    # Wait for full fresh window
                    wait_sec = totp.interval - (int(time.time()) % totp.interval) + 1
                    print(f"[console_login] Waiting {wait_sec}s for fresh TOTP window ...")
                    time.sleep(wait_sec)

                    # Check what page we are on now
                    current_url = page.url
                    print(f"[console_login] Current URL after wait: {current_url}")

                    # Check if MFA input is still visible — reuse it
                    mfa_still_visible = page.locator(
                        "input[name='verify_token'], input[placeholder='Enter 6-digit code']"
                    ).count() > 0

                    if mfa_still_visible:
                        # MFA page still showing — just retry OTP directly
                        print("[console_login] MFA page still active — retrying OTP ...")
                        continue

                    # Otherwise full re-login needed
                    print("[console_login] Re-doing full login flow ...")
                    page.goto(self.url, wait_until="networkidle")
                    time.sleep(3)

                    # Email — try multiple selectors
                    email_loc = page.locator(
                        "input[type='email'], input[name*='email'], "
                        "input[placeholder*='email' i], input[placeholder*='username' i]"
                    ).first
                    email_loc.wait_for(state="visible", timeout=20000)
                    email_loc.fill(self.email)
                    page.keyboard.press("Enter")

                    # Password
                    pwd_loc = page.locator("input[type='password']").first
                    pwd_loc.wait_for(state="visible", timeout=10000)
                    pwd_loc.fill(self.password)

                    # Submit
                    submit = page.locator("button[type='submit']")
                    if submit.count() == 0:
                        submit = page.locator("button:visible").first
                    submit.first.click()

                    # Wait for MFA page
                    page.wait_for_selector(
                        "input[name='verify_token'], input[placeholder='Enter 6-digit code']",
                        timeout=15000
                    )
                else:
                    raise RuntimeError(
                        f"MFA login failed after {max_attempts} OTP attempts.\n"
                        f"The secret in dev.yaml may be incorrect for this controller.\n"
                        f"Try resetting MFA or passing --mfa-secret with the correct secret."
                    )

    def _detect_mfa_page(self, page) -> str:
        has_canvas   = page.locator("canvas").count() > 0
        has_img_qr   = page.locator("img[src*='qr'], img[alt*='QR' i]").count() > 0
        has_verify   = page.locator("input[name='verify_token']").count() > 0
        has_sixdigit = page.locator("input[placeholder='Enter 6-digit code']").count() > 0

        if (has_canvas or has_img_qr) and has_verify:
            return "enrollment"
        if has_sixdigit:
            return "otp"
        if has_verify:
            return "otp"
        return "unknown"

    def _scan_qr(self, page) -> str:
        """Extract TOTP secret from QR canvas."""
        try:
            from pyzbar.pyzbar import decode
            from PIL import Image
        except ImportError:
            raise ImportError(
                "pyzbar and Pillow required.\n"
                "Run: pip install pyzbar Pillow"
            )

        b64_data = page.evaluate("""() => {
            const canvas = document.querySelector('canvas');
            if (!canvas) return null;
            return canvas.toDataURL('image/png').split(',')[1];
        }""")

        if not b64_data:
            raise ValueError("Canvas found but toDataURL returned nothing")

        image   = Image.open(io.BytesIO(base64.b64decode(b64_data)))
        decoded = decode(image)

        if not decoded:
            raise ValueError("pyzbar could not decode QR code from canvas")

        uri    = decoded[0].data.decode("utf-8")
        params = urllib.parse.parse_qs(urllib.parse.urlparse(uri).query)
        secret = params.get("secret", [None])[0]

        if not secret:
            raise ValueError(f"No secret found in OTP URI: {uri}")
        return secret

    def _click_submit(self, page):
        for label in ["Verify Token", "Verify", "Submit", "Confirm", "Continue", "Sign in"]:
            btn = page.get_by_role("button", name=label, exact=False)
            if btn.count() > 0:
                btn.first.click()
                return
        all_buttons = page.locator("button:visible")
        if all_buttons.count() > 0:
            all_buttons.first.click()
            return
        raise RuntimeError("Could not find submit button on MFA page")

    # ── Dashboard capture ─────────────────────────────────────────────────────

    def _capture_dashboard(self, page) -> dict:
        """Capture what is visible on the dashboard after login."""
        print("[console_login] Capturing dashboard state ...")
        time.sleep(2)

        dashboard = {
            "url":   page.url,
            "title": page.title(),
            "elements": [],
        }

        checks = {
            "Projects":     "text=Projects",
            "Clusters":     "text=Clusters",
            "Workloads":    "text=Workloads",
            "Blueprints":   "text=Blueprints",
            "Repositories": "text=Repositories",
            "Organization": "text=Organization",
            "Users":        "text=Users",
            "Audit Logs":   "text=Audit Logs",
            "Nav sidebar":  "nav, [role='navigation']",
            "User menu":    "[aria-label*='user' i], [aria-label*='account' i]",
        }

        visible = []
        for label, selector in checks.items():
            try:
                if page.locator(selector).first.is_visible(timeout=2000):
                    visible.append(label)
            except Exception:
                pass

        dashboard["elements"] = visible
        print(f"[console_login] Dashboard elements: {visible}")

        try:
            dashboard["page_text_preview"] = page.locator("body").inner_text()[:500].strip()
        except Exception:
            dashboard["page_text_preview"] = ""

        return dashboard

    def _detect_mfa_page(self, page) -> str:
        has_canvas   = page.locator("canvas").count() > 0
        has_img_qr   = page.locator("img[src*='qr'], img[alt*='QR' i]").count() > 0
        has_verify   = page.locator("input[name='verify_token']").count() > 0
        has_sixdigit = page.locator("input[placeholder='Enter 6-digit code']").count() > 0

        # ← ADD THIS DEBUG
        print(f"[console_login] MFA page debug:")
        print(f"  has_canvas   : {has_canvas}")
        print(f"  has_img_qr   : {has_img_qr}")
        print(f"  has_verify   : {has_verify}")
        print(f"  has_sixdigit : {has_sixdigit}")
        # Save screenshot for inspection
        page.screenshot(path="/tmp/mfa_page_debug.png")
        print(f"  screenshot   : /tmp/mfa_page_debug.png")

        if (has_canvas or has_img_qr) and has_verify:
            return "enrollment"
        if has_sixdigit:
            return "otp"
        if has_verify:
            return "otp"
        return "unknown"