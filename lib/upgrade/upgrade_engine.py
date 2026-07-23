"""
lib/upgrade/upgrade_engine.py

UPDATED per team design review (2026-07-22):
  - No more hop-file lookup. lib/upgrade/hops/ is no longer imported or
    referenced anywhere -- confirmed via repo-wide grep that get_hop()
    had no callers outside this file and its own package. The hops/
    directory can be deleted entirely.
  - pre_commands / after_radm_dependency_commands / after_radm_application_commands
    are now passed in DIRECTLY as constructor arguments -- sourced from
    the three Jenkins textboxes (base64-decoded in conftest.py before
    reaching here), not looked up from any file.
  - src_package_url / dst_package_url are now the primary inputs (full
    URLs, passed directly from Jenkins) -- package "name" is derived via
    a simple URL basename split (url.rsplit('/', 1)[-1]), which is 100%
    reliable since it's just splitting a path, not parsing a version out
    of a filename.
  - src_version / dst_version are now PURELY COSMETIC -- best-effort
    labels for print statements/logging only. No behavior in this class
    depends on them being correct, since there is no more per-version
    hop lookup to get wrong.

Phases, in the order they run:
  1.  pre_commands             — passed in directly (from Jenkins textbox)
  2.  download                 — new package via aria2c
  3.  extract                  — new package via pigz/tar
  4.  create_config            — copy template, patch fields from old config
  5.  copy_radm                — new radm → /usr/bin/
  6.  radm dependency          — always same, NEW package only
      after_radm_dependency_commands — passed in directly (from Jenkins textbox)
  7.  wait elasticsearch       — wait for green
  8.  radm application         — always same, NEW package only
      after_radm_application_commands — passed in directly (from Jenkins textbox)
  9.  radm cluster             — NEW package only (single pass)
"""

import re
import time

# ── Wait policies ─────────────────────────────────────────────────────────────
PHASE_WAIT = {
    "radm_dependency":  {"interval": 20, "max_wait": 600},
    "radm_application": {"interval": 20, "max_wait": 800},
    "radm_cluster":     {"interval": 20, "max_wait": 800},
    "elasticsearch":    {"interval": 30, "max_wait": 600},
}


class UpgradeEngine:

    def __init__(
        self,
        ssh_client,
        src_package_url: str,
        dst_package_url: str,
        install_dir: str = "/opt/rafay",
        star_domain: str = "",
        nsg_manager=None,
        pre_commands: list = None,
        after_radm_dependency_commands: list = None,
        after_radm_application_commands: list = None,
    ):
        self.ssh         = ssh_client
        self.install_dir = install_dir
        self.star_domain = star_domain
        self.nsg         = nsg_manager

        self.pre_commands = pre_commands or []
        self.after_radm_dependency_commands = after_radm_dependency_commands or []
        self.after_radm_application_commands = after_radm_application_commands or []

        # Package name is JUST the URL's last path segment -- a plain
        # string split, not a version-parsing regex. This is safe
        # regardless of naming convention (RC suffixes, dash-vs-dot,
        # differing prefixes -- none of it matters here).
        self.src_package_url = src_package_url
        self.dst_package_url = dst_package_url
        self.src_package = src_package_url.rsplit("/", 1)[-1]
        self.dst_package = dst_package_url.rsplit("/", 1)[-1]

        self.src_extract_dir = f"{install_dir}/{self.src_package.replace('.tar.gz', '')}"
        self.dst_extract_dir = f"{install_dir}/{self.dst_package.replace('.tar.gz', '')}"

        # Cosmetic only -- best-effort, used solely in print()/log output.
        # No decision in this class reads these values.
        self.src_version = self._cosmetic_version_label(self.src_package)
        self.dst_version = self._cosmetic_version_label(self.dst_package)

    @staticmethod
    def _cosmetic_version_label(package_name: str) -> str:
        """
        Best-effort label for logs only -- e.g. "rafay-airgapped-controller-
        v3.1-40-1.tar.gz" -> "3.1-40-1". Falls back to the full package
        name if nothing matches. Never used for any control-flow decision,
        so a wrong/partial match here has zero functional impact -- it
        only affects how readable the console log is.
        """
        m = re.search(r'v?([\d.]+(?:-\d+)*)\.tar\.gz', package_name)
        return m.group(1) if m else package_name

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self):
        print("\n" + "═" * 60)
        print(f"[upgrade] Starting controller upgrade")
        print(f"[upgrade]   src : {self.src_package} ({self.src_version})")
        print(f"[upgrade]   dst : {self.dst_package} ({self.dst_version})")
        print("═" * 60 + "\n")

        self._run_commands("pre", self.pre_commands)

        self._phase("download_new_package",  self._download_new_package)
        self._phase("extract_new_package",   self._extract_new_package)
        self._phase("create_upgrade_config", self._create_upgrade_config)
        self._phase("copy_new_radm",         self._copy_new_radm)

        self._phase("radm_dependency", self._radm_dependency)
        self._run_commands("after_radm_dependency", self.after_radm_dependency_commands)

        self._phase("wait_elasticsearch", self._wait_elasticsearch)

        self._phase("radm_application", self._radm_application)
        self._run_commands("after_radm_application", self.after_radm_application_commands)

        self._phase("radm_cluster", self._radm_cluster)

        print("\n" + "═" * 60)
        print(f"[upgrade] ✅ Upgrade complete: {self.src_version} → {self.dst_version}")
        print("═" * 60 + "\n")

    # ── Runners ───────────────────────────────────────────────────────────────

    def _phase(self, name: str, fn):
        print(f"\n[upgrade] ── Phase: {name} " + "─" * max(0, 40 - len(name)))
        try:
            fn()
            print(f"[upgrade] ✓ {name}")
        except Exception as e:
            raise RuntimeError(f"Upgrade failed at [{name}]: {e}") from e

    def _run_commands(self, cmd_type: str, commands: list):
        """
        Warns on failure -- never stops the upgrade. These commands come
        straight from a Jenkins textbox with no review, so treating any
        one line's failure as fatal would make one typo abort an entire
        real upgrade run -- warn-and-continue matches how pre/post
        commands have always behaved in this engine.
        """
        if not commands:
            print(f"[upgrade] No {cmd_type} commands provided")
            return
        print(f"\n[upgrade] ── {cmd_type.upper()} COMMANDS ({len(commands)} total) " + "─" * 20)
        for i, cmd in enumerate(commands, 1):
            label = cmd.strip()[:60] + ("..." if len(cmd.strip()) > 60 else "")
            print(f"[upgrade] [{i}/{len(commands)}] {label}")
            try:
                out, rc = self.ssh.run(cmd, timeout=120)
                print(f"[upgrade] ✓" if rc == 0 else f"[upgrade] ⚠ WARNING (exit {rc}): {out[-150:]} — continuing")
            except Exception as e:
                print(f"[upgrade] ⚠ WARNING: {e} — continuing")

    # ── Phase implementations (unchanged from previous version) ──────────────

    def _download_new_package(self):
        tar_path = f"{self.install_dir}/{self.dst_package}"
        check, _ = self.ssh.run(f"test -f {tar_path} && test ! -f {tar_path}.aria2 && echo COMPLETE || echo MISSING")
        if "COMPLETE" in check:
            print(f"[download_new_package] already downloaded — skipping")
            return

        if self.nsg:
            self.nsg.attach()
            print("[download_new_package] NSG attached — waiting 30s ...")
            time.sleep(30)

        aria2c_out, _ = self.ssh.run("which aria2c")
        aria2c_bin = aria2c_out.strip() or "/usr/bin/aria2c"

        print(f"[download_new_package] Downloading: {self.dst_package_url}")
        out, rc = self.ssh.run(
            f"sudo {aria2c_bin} -x 16 -s 16 --max-tries=3 --retry-wait=10 "
            f"--connect-timeout=30 -d {self.install_dir} {self.dst_package_url} 2>&1",
            timeout=1800
        )
        if self.nsg:
            self.nsg.detach()
            print("[download_new_package] NSG detached ✓")

        assert rc == 0, f"aria2c download failed (exit {rc}): {out[-300:]}"
        print(f"[download_new_package] Downloaded ✓")

    def _extract_new_package(self):
        check, _ = self.ssh.run(f"test -d {self.dst_extract_dir} && echo EXISTS || echo MISSING")
        if "EXISTS" in check:
            print(f"[extract_new_package] already extracted: {self.dst_extract_dir}")
            return

        tar_path = f"{self.install_dir}/{self.dst_package}"
        _, pigz_rc = self.ssh.run("which pigz 2>/dev/null")
        cmd = (f"cd {self.install_dir} && sudo tar -I pigz -xf {tar_path} 2>&1 && echo EXTRACTED" if pigz_rc == 0
               else f"cd {self.install_dir} && sudo tar -xf {tar_path} 2>&1 && echo EXTRACTED")

        out, rc = self.ssh.run(cmd, timeout=3600)
        assert rc == 0 and "EXTRACTED" in out, f"Extraction failed: {out[-300:]}"
        print(f"[extract_new_package] Extracted to {self.dst_extract_dir} ✓")

    def _create_upgrade_config(self):
        old_config = f"{self.src_extract_dir}/config.yaml"
        new_config = f"{self.dst_extract_dir}/config.yaml"

        self.ssh.run(f"sudo cp {old_config} {new_config}")
        self.ssh.run(f"sudo sed -i 's|archive-directory:.*|archive-directory: RAFAY_PH|' {new_config}")
        self.ssh.run(f"sudo sed -i 's|archive-directory: RAFAY_PH|archive-directory: {self.dst_extract_dir}|' {new_config}")
        print(f"[create_upgrade_config] ✓ archive-directory: {self.dst_extract_dir}")

    def _copy_new_radm(self):
        out, rc = self.ssh.run(
            f"sudo cp {self.dst_extract_dir}/radm /usr/bin/radm && sudo chmod +x /usr/bin/radm && echo OK"
        )
        assert rc == 0 and "OK" in out, f"radm copy failed: {out}"
        print("[copy_new_radm] new radm → /usr/bin/radm ✓")

    def _radm_dependency(self):
        print("[radm_dependency] Running ...")
        out, rc = self.ssh.run(
            f"cd {self.dst_extract_dir} && sudo ./radm dependency --config config.yaml 2>&1",
            timeout=1800,
        )
        assert rc == 0, f"radm dependency failed (exit {rc}): {out[-300:]}"
        self._poll_pods(PHASE_WAIT["radm_dependency"], "after radm dependency")

    def _wait_elasticsearch(self):
        cfg = PHASE_WAIT["elasticsearch"]
        deadline = time.time() + cfg["max_wait"]
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            out, rc = self.ssh.run("kubectl get es,kibana -A --no-headers 2>/dev/null || echo NOT_READY")
            if rc == 0 and "NOT_READY" not in out and out.strip():
                lines = [l for l in out.splitlines() if l.strip()]
                not_green = [l for l in lines if "green" not in l.lower()]
                if not not_green:
                    print("[wait_elasticsearch] all green ✓")
                    return
            time.sleep(cfg["interval"])
        print(f"[wait_elasticsearch] ⚠ Timeout — continuing anyway")

    def _radm_application(self):
        print("[radm_application] Running ...")
        out, rc = self.ssh.run(
            f"cd {self.dst_extract_dir} && sudo ./radm application --config config.yaml 2>&1",
            timeout=2400,
        )
        assert rc == 0, f"radm application failed (exit {rc}): {out[-300:]}"
        self._poll_pods(PHASE_WAIT["radm_application"], "after radm application")

    def _radm_cluster(self):
        print(f"[radm_cluster] Running from {self.dst_extract_dir} ...")
        out, rc = self.ssh.run_stream(
            f"cd {self.dst_extract_dir} && sudo ./radm cluster --config config.yaml 2>&1",
            timeout=2400, prefix="[radm cluster]",
        )
        assert rc == 0, f"radm cluster failed (exit {rc}): {out[-300:]}"
        self._poll_pods(PHASE_WAIT["radm_cluster"], "after radm cluster")

    def _poll_pods(self, cfg: dict, label: str):
        deadline = time.time() + cfg["max_wait"]
        attempt = 0
        stable_count = 0
        prev_total = 0
        while time.time() < deadline:
            attempt += 1
            out, rc = self.ssh.run("kubectl get pods -A --no-headers 2>/dev/null || /usr/local/bin/kubectl get pods -A --no-headers 2>&1")
            if rc != 0:
                time.sleep(cfg["interval"])
                continue
            lines = [l for l in out.splitlines() if l.strip()]
            total = len(lines)
            not_ready = [l for l in lines if "Running" not in l and "Completed" not in l]
            print(f"[poll_pods][{label}] attempt {attempt}: {total - len(not_ready)}/{total} Running")
            if not not_ready and total > 0:
                stable_count = stable_count + 1 if total == prev_total else 0
                if stable_count >= 2:
                    print(f"[poll_pods][{label}] All {total} pods Running ✓")
                    return
            else:
                stable_count = 0
            prev_total = total
            time.sleep(cfg["interval"])
        print(f"[poll_pods][{label}] Timeout after {cfg['max_wait']}s")