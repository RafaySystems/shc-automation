"""
lib/console/mfa_login.py

Wraps rafay_mfa_login_playwright.py as a reusable class.
Handles both first-run (QR scan) and subsequent runs (saved secret).
Includes OTP retry logic to handle TOTP window timing issues.
"""

import re
import urllib.parse
import base64
import io
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LoginResult:
    success:      bool
    url:          str
    secret:       str
    screenshot:   bytes = field(repr=False)
    dashboard:    dict = field(default_factory=dict)
    error:        str = ""
    qr_screenshot: bytes = field(default=b"", repr=False)
    mfa_type:     str = ""


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
        self._qr_bytes  = b""
        self._mfa_type  = ""

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
            self._attach_auth_logger(page)

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
                    qr_screenshot=self._qr_bytes,
                    mfa_type=self._mfa_type,
                )
            except Exception as e:
                screenshot = page.screenshot(full_page=False)
                return LoginResult(
                    success=False,
                    url=page.url,
                    secret=self.mfa_secret or "",
                    screenshot=screenshot,
                    error=str(e),
                    qr_screenshot=self._qr_bytes,
                    mfa_type=self._mfa_type,
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

        # Step 1+2: Email + password (handles single-form and two-step pages)
        print("[console_login] Entering email ...")
        self._submit_credentials(page)

        # Step 3: MFA page
        print("[console_login] Waiting for MFA page ...")
        page.wait_for_selector(
            "input[name='verify_token'], input[placeholder='Enter 6-digit code'], input[name='totp'], input[name*='otp' i]",
            timeout=15000
        )

        mfa_type = self._detect_mfa_page(page)
        print(f"[console_login] MFA type: {mfa_type}")
        self._mfa_type = mfa_type

        secret = self.mfa_secret

        if mfa_type == "enrollment" and not secret:
            # First run — scan QR and extract secret
            secret = self._enrollment_secret(page)

        elif mfa_type == "enrollment" and secret:
            # Secret provided but enrollment page shown — controller re-brough up
            # Old secret is stale — scan fresh QR instead
            print(f"[console_login] Enrollment page shown but secret exists "
                  f"— controller may have been re-deployed, scanning fresh QR ...")
            secret = self._enrollment_secret(page)

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
            # Only a VISIBLE OTP field -- a hidden input matching the same
            # selector would take the code while the real field stays empty.
            otp_input = page.locator(", ".join(
                f"{sel.strip()}:visible" for sel in self._MFA_SELECTOR.split(",")
            )).first
            otp_input.fill(otp_code)
            print(f"[console_login] OTP field: name={otp_input.get_attribute('name')!r} "
                  f"value now={otp_input.input_value()!r}")
            seen = len(self._auth_log)
            self._click_submit(page, fallback_input=otp_input)

            # Check if we navigated to dashboard (success)
            try:
                page.wait_for_url(
                    lambda u: "mfa" not in u and "login" not in u,
                    timeout=8000
                )
                print(f"[console_login] ✓ Login successful — {page.url}")
                return secret

            except Exception:
                # Still on login/mfa page -- show what the controller answered
                self._print_auth_log_since(seen, attempt)
                last = " ".join(self._auth_log[seen:])
                if "AUTH079" in last:
                    raise RuntimeError(
                        "Admin account is LOCKED OUT (AUTH079: too many incorrect attempts) — "
                        "wait 15 minutes before retrying. Stopped to avoid extending the lockout.")
                if "AUTH002" in last and attempt >= 2:
                    # Code was generated at the start of a fresh window and actually
                    # submitted, and rejected twice: the secret is wrong. More
                    # attempts only trigger the controller's lockout (AUTH079).
                    raise RuntimeError(
                        f"OTP rejected twice (AUTH002) with the code actually submitted — the TOTP "
                        f"secret {secret} does not match this controller's enrollment. "
                        f"Stopped before the account gets locked out.")
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
                        "input[name='verify_token'], input[placeholder='Enter 6-digit code'], input[name='totp'], input[name*='otp' i]"
                    ).count() > 0

                    if mfa_still_visible:
                        # MFA page still showing — just retry OTP directly
                        print("[console_login] MFA page still active — retrying OTP ...")
                        continue

                    # Otherwise full re-login needed
                    print("[console_login] Re-doing full login flow ...")
                    page.goto(self.url, wait_until="networkidle")
                    time.sleep(3)

                    self._submit_credentials(page)

                    # Wait for MFA page
                    page.wait_for_selector(
                        "input[name='verify_token'], input[placeholder='Enter 6-digit code'], input[name='totp'], input[name*='otp' i]",
                        timeout=15000
                    )
                else:
                    self._dump_mfa_controls(page)
                    raise RuntimeError(
                        f"MFA login failed after {max_attempts} OTP attempts.\n"
                        f"The secret in dev.yaml may be incorrect for this controller.\n"
                        f"Try resetting MFA or passing --mfa-secret with the correct secret."
                    )

    _EMAIL_SELECTOR = (
        "input[type='email'], "
        "input[name*='email'], "
        "input[placeholder*='email' i], "
        "input[placeholder*='username' i], "
        "input[autocomplete='email'], "
        "input[autocomplete='username']"
    )
    # Matches the login form's submit control by accessible name, whatever
    # the element is (<button>, <input type=submit>, role=button) and whatever
    # the casing ("Sign In", "Login", ...). Anchored so it can't match
    # "Forgot your password" or the password show/hide toggle.
    _SIGNIN_NAME = re.compile(r"^\s*(sign\s*in|log\s*in|login|continue|next)\s*$", re.I)
    _MFA_SELECTOR = "input[name='verify_token'], input[placeholder='Enter 6-digit code'], input[name='totp'], input[name*='otp' i]"

    @staticmethod
    def _find_action(page, name):
        """
        First visible control with this accessible name, as a button OR a
        link. The 4.3 ops-console renders its actions as links -- Playwright
        codegen on shc-168: get_by_role("link", name="Sign In") and
        get_by_role("link", name="Continue") -- so button-only lookups found
        nothing and the OTP was never submitted.
        """
        for role in ("button", "link"):
            loc = page.get_by_role(role, name=name)
            if loc.count() > 0:
                return loc.first
        return None

    def _submit_credentials(self, page):
        """
        Fill email + password and submit, for BOTH login page layouts:
          - single form (4.3 UI): email and password on one page
          - two-step (older UI): email first, password appears after Enter

        The old code always pressed Enter after the email. On the single-form
        page that submits the form with an EMPTY password, so the controller
        answers AUTH002 "Could not validate your account" (the toast in the
        build #164 screenshot) -- then the password gets typed afterwards,
        which is why the screenshot showed both fields filled.
        """
        email_input = page.locator(self._EMAIL_SELECTOR).first
        email_input.wait_for(state="visible", timeout=30000)
        email_input.fill(self.email)

        pwd_input = page.locator("input[type='password']").first
        try:
            pwd_input.wait_for(state="visible", timeout=3000)
            print("[console_login] Single-form login page (email + password together)")
        except Exception:
            print("[console_login] Two-step login page — submitting email first ...")
            email_input.press("Enter")
            pwd_input.wait_for(state="visible", timeout=15000)

        print("[console_login] Entering password ...")
        pwd_input.fill(self.password)

        # Same order as the pre-4.3 code first (a real submit button, whatever
        # its label), so 3.x / GPU-PaaS pages behave exactly as before; then
        # the 4.3 link-styled "Sign In"; then Enter.
        typed_submit = page.locator("button[type='submit']:visible")
        submit = typed_submit.first if typed_submit.count() > 0 else self._find_action(page, self._SIGNIN_NAME)
        if submit is not None:
            submit.click()
        else:
            print("[console_login] No Sign In button matched — submitting with Enter")
            pwd_input.press("Enter")

        self._raise_if_login_rejected(page)

    def _raise_if_login_rejected(self, page, timeout: int = 15):
        """
        After submitting credentials, wait until one of: MFA page appears,
        we leave the login page, or the controller shows its rejection toast.
        On rejection, raise with the controller's own message immediately
        instead of timing out 30s later on an unrelated locator.
        """
        deadline = time.time() + timeout
        rejected = page.get_by_text(re.compile(r"could not validate|invalid (credentials|password)", re.I))
        while time.time() < deadline:
            if page.locator(self._MFA_SELECTOR).count() > 0:
                return
            if "login" not in page.url:
                return
            if rejected.count() > 0:
                msg = rejected.first.inner_text().strip()
                raise RuntimeError(f"Controller rejected the login: {msg}")
            time.sleep(0.5)

    def _enrollment_secret(self, page) -> str:
        """
        TOTP secret for enrollment. The controller returns it directly in the
        first POST /auth/v1/login/ response (account.totp_url ->
        otpauth://...?secret=...), so that is the source of truth. The QR is
        still decoded -- for the report attachment and as a fallback -- and a
        mismatch is logged.
        """
        api = getattr(self, "_api_totp_secret", "")
        qr = ""
        try:
            qr = self._scan_qr(page)
        except Exception as e:
            print(f"[console_login] QR decode failed ({e})")
        if api:
            print(f"[console_login] TOTP secret from login response (totp_url): {api}")
            if qr and qr != api:
                print(f"[console_login] ⚠ QR secret {qr} differs from totp_url — using totp_url")
            return api
        if qr:
            print(f"[console_login] TOTP secret extracted from QR: {qr}")
            return qr
        raise RuntimeError("No TOTP secret: login response had no totp_url and QR decode failed")

    def _detect_mfa_page(self, page) -> str:
        has_canvas   = page.locator("canvas").count() > 0
        has_img_qr   = page.locator("img[src*='qr'], img[alt*='QR' i]").count() > 0
        has_verify   = page.locator(self._MFA_SELECTOR).count() > 0
        has_sixdigit = page.locator("input[placeholder='Enter 6-digit code']").count() > 0

        if (has_canvas or has_img_qr) and has_verify:
            return "enrollment"
        if has_sixdigit:
            return "otp"
        if has_verify:
            return "otp"
        return "unknown"

    def _scan_qr(self, page) -> str:
        """Extract TOTP secret from QR canvas, and keep the raw QR image
        itself so it can be attached to the report -- only called on the
        enrollment branch, so self._qr_bytes here is always a real QR,
        never a plain OTP box."""
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

        # Save the raw QR image itself (the canvas IS the QR code) so it
        # can be attached to the report regardless of whether decoding or
        # login succeeds afterward.
        self._qr_bytes = base64.b64decode(b64_data)

        image   = Image.open(io.BytesIO(self._qr_bytes))
        decoded = decode(image)

        if not decoded:
            raise ValueError("pyzbar could not decode QR code from canvas")

        uri    = decoded[0].data.decode("utf-8")
        params = urllib.parse.parse_qs(urllib.parse.urlparse(uri).query)
        secret = params.get("secret", [None])[0]

        if not secret:
            raise ValueError(f"No secret found in OTP URI: {uri}")
        return secret

    _MFA_SUBMIT_TEXT = re.compile(
        r"^\s*(verify( token| code)?|submit|confirm|continue|enable( mfa)?|activate)\s*$", re.I)

    def _attach_auth_logger(self, page):
        """
        Record every /auth/ response (method, status, URL, short body), and
        the controller-vs-runner clock skew from the first response's Date
        header. TOTP codes are time-based: a skew of more than ~30s makes
        every code wrong even with the correct secret.
        """
        self._auth_log = []
        self._skew_reported = False
        self._api_totp_secret = ""

        def on_response(r):
            try:
                if not self._skew_reported:
                    date_hdr = r.headers.get("date")
                    if date_hdr:
                        from email.utils import parsedate_to_datetime
                        skew = time.time() - parsedate_to_datetime(date_hdr).timestamp()
                        self._skew_reported = True
                        print(f"[console_login] Clock skew runner - controller: {skew:+.1f}s"
                              + ("  ⚠ large enough to break TOTP" if abs(skew) > 25 else ""))
                if "/auth/" not in r.url:
                    return
                if "/auth/v1/login" in r.url:
                    try:
                        totp_url = (r.json().get("account") or {}).get("totp_url") or ""
                        if "secret=" in totp_url:
                            q = urllib.parse.parse_qs(urllib.parse.urlparse(totp_url).query)
                            self._api_totp_secret = q.get("secret", [""])[0]
                    except Exception:
                        pass
                body = ""
                try:
                    body = r.text()[:200]
                except Exception:
                    pass
                sent = ""
                if "/auth/v1/login" in r.url and r.request.method == "POST":
                    try:
                        import json as _json
                        pd = _json.loads(r.request.post_data or "{}")
                        sent = " | sent: " + ", ".join(
                            f"{k}={'***' if 'password' in k and v else v!r}"
                            for k, v in pd.items() if k in ("username", "password", "totp"))
                    except Exception:
                        pass
                self._auth_log.append(f"{r.request.method} {r.status} {r.url.split('?')[0]} {body}{sent}")
            except Exception:
                pass

        page.on("response", on_response)

    def _print_auth_log_since(self, start: int, attempt: int):
        new = self._auth_log[start:]
        if not new:
            print(f"[console_login]   attempt {attempt}: NO /auth/ request was sent after "
                  f"submitting — the OTP form was not submitted")
        for line in new:
            print(f"[console_login]   attempt {attempt}: {line}")

    def _dump_mfa_controls(self, page):
        """Print the MFA page's candidate submit controls so the next fix
        can target the real element instead of guessing."""
        try:
            info = page.evaluate("""() => {
                const out = [];
                const inp = document.querySelector("input[name='verify_token'], input[placeholder='Enter 6-digit code'], input[name='totp'], input[name*='otp' i]");
                out.push('otp input inside <form>: ' + !!(inp && inp.closest('form')));
                const cands = document.querySelectorAll("button, [role=button], input[type=submit], a, div, span");
                for (const el of cands) {
                    const t = (el.innerText || el.value || '').trim();
                    if (t && t.length < 40 && /verify|submit|confirm|continue|enable|activate/i.test(t)
                        && el.children.length < 3) {
                        out.push(el.outerHTML.slice(0, 250));
                    }
                    if (out.length > 12) break;
                }
                return out.join('\\n');
            }""")
            print(f"[console_login] MFA page controls:\n{info}")
        except Exception as e:
            print(f"[console_login] Could not inspect MFA page controls: {e}")

    def _click_submit(self, page, fallback_input=None):
        """
        Submit the MFA form. Tries a button by accessible name first; if the
        page has no matching button element, presses Enter in the OTP input.

        The 4.3 ops-console renders its actions without a button role -- the
        login page's Sign In matched no button either (build #166 log:
        "No Sign In button matched — submitting with Enter", which then
        worked) and the MFA page failed with "Could not find submit button".
        Enter submits the form the same way a click would.
        """
        action = self._find_action(page, self._MFA_SUBMIT_TEXT)
        if action is not None:
            print(f"[console_login] Clicking MFA submit ({action.inner_text().strip()!r})")
            action.click()
            return
        # Pre-4.3 behaviour, unchanged: button whose label CONTAINS one of these
        # (e.g. "Verify & Continue"), as the original code matched.
        for label in ["Verify Token", "Verify", "Submit", "Confirm", "Continue", "Sign in"]:
            btn = page.get_by_role("button", name=label, exact=False)
            if btn.count() > 0:
                btn.first.click()
                return
        # Clickable element with a submit label that has no button role
        # (div/span/a styled as a button).
        by_text = page.get_by_text(self._MFA_SUBMIT_TEXT)
        if by_text.count() > 0:
            print("[console_login] Clicking MFA submit control by its text "
                  f"({by_text.first.inner_text().strip()!r})")
            before = len(getattr(self, "_auth_log", []))
            by_text.first.click()
            # If that text was only a label (nothing got sent), fall back to Enter.
            time.sleep(1.5)
            if fallback_input is not None and len(getattr(self, "_auth_log", [])) == before:
                try:
                    if fallback_input.is_visible():
                        print("[console_login] Text click sent no request — submitting with Enter")
                        fallback_input.press("Enter")
                except Exception:
                    pass
            return
        if fallback_input is not None:
            print("[console_login] No MFA submit button matched — submitting with Enter")
            fallback_input.press("Enter")
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