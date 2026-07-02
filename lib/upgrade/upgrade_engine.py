"""
lib/upgrade/upgrade_engine.py

Written ONCE — never changes when new versions are added.
No hooks file needed — commands live directly in upgrade_registry.py.

Phases per hop:
  1. pre_commands       — version-specific shell commands (warn on failure)
  2. download           — new package via aria2c
  3. extract            — new package via pigz/tar
  4. create_config      — copy template, patch fields from old config
  5. copy_radm          — new radm → /usr/bin/
  6. radm dependency    — always same
  7. wait elasticsearch — wait for green
  8. radm application   — always same
  9. radm cluster old   — from source package dir
  10. post_commands     — version-specific shell commands (warn on failure)
  11. radm cluster new  — from dest package dir
"""

import re
import time
from lib.upgrade.upgrade_registry import get_hop

# ── Wait policies ─────────────────────────────────────────────────────────────
PHASE_WAIT = {
    "radm_dependency":  {"interval": 20, "max_wait": 1500},
    "radm_application": {"interval": 20, "max_wait": 2400},
    "radm_cluster":     {"interval": 20, "max_wait": 1200},
    "elasticsearch":    {"interval": 30, "max_wait": 600},
}

S3_BASE = "https://rafay-airgap-controller.s3.us-west-2.amazonaws.com"


class UpgradeEngine:

    def __init__(
        self,
        ssh_client,
        src_version:     str,
        dst_version:     str,
        src_package:     str,
        dst_package:     str,
        dst_package_url: str = "",
        install_dir:     str = "/opt/rafay",
        star_domain:     str = "",
        nsg_manager=None,
    ):
        self.ssh         = ssh_client
        self.src_version = src_version
        self.dst_version = dst_version
        self.src_package = src_package
        self.dst_package = dst_package
        self.install_dir = install_dir
        self.star_domain = star_domain
        self.nsg         = nsg_manager

        self.src_extract_dir = f"{install_dir}/{src_package.replace('.tar.gz', '')}"
        self.dst_extract_dir = f"{install_dir}/{dst_package.replace('.tar.gz', '')}"
        self.dst_package_url = dst_package_url or self._build_url(dst_package)

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self):
        hop = get_hop(self.src_version, self.dst_version)

        print("\n" + "═" * 60)
        print(f"[upgrade] Starting controller upgrade")
        print(f"[upgrade]   {self.src_version} → {self.dst_version}")
        print(f"[upgrade]   src : {self.src_extract_dir}")
        print(f"[upgrade]   dst : {self.dst_extract_dir}")
        print(f"[upgrade]   url : {self.dst_package_url}")
        print("═" * 60 + "\n")

        # 1. Pre-commands (warn on failure)
        self._run_commands("pre", hop.get("pre_commands", []))

        # 2. Download + setup (fatal on failure)
        self._phase("download_new_package",  self._download_new_package)
        self._phase("extract_new_package",   self._extract_new_package)
        self._phase("create_upgrade_config", self._create_upgrade_config)
        self._phase("copy_new_radm",         self._copy_new_radm)

        # 3. radm phases (fatal on failure)
        self._phase("radm_dependency",    self._radm_dependency)
        self._phase("wait_elasticsearch", self._wait_elasticsearch)
        self._phase("radm_application",   self._radm_application)
        self._phase("radm_cluster_old",   self._radm_cluster_old)

        # 4. Post-commands (warn on failure)
        self._run_commands("post", hop.get("post_commands", []))

        # 5. Final radm cluster (fatal on failure)
        self._phase("radm_cluster_new", self._radm_cluster_new)

        print("\n" + "═" * 60)
        print(f"[upgrade] ✅ Upgrade complete: {self.src_version} → {self.dst_version}")
        print("═" * 60 + "\n")

    # ── Runners ───────────────────────────────────────────────────────────────

    def _phase(self, name: str, fn):
        """Run a phase — stops upgrade on failure."""
        print(f"\n[upgrade] ── Phase: {name} " + "─" * max(0, 40 - len(name)))
        try:
            fn()
            print(f"[upgrade] ✓ {name}")
        except Exception as e:
            raise RuntimeError(f"Upgrade failed at [{name}]: {e}") from e

    def _run_commands(self, cmd_type: str, commands: list):
        """
        Run pre or post shell commands.
        Warns on failure — never stops the upgrade.
        Commands ending with || true never fail by design.
        """
        if not commands:
            print(f"[upgrade] No {cmd_type}_commands for this hop")
            return

        print(f"\n[upgrade] ── {cmd_type.upper()}_COMMANDS ({len(commands)} total) " + "─" * 20)
        for i, cmd in enumerate(commands, 1):
            # Show a short label (first 60 chars of command)
            label = cmd.strip()[:60] + ("..." if len(cmd.strip()) > 60 else "")
            print(f"[upgrade] [{i}/{len(commands)}] {label}")
            try:
                out, rc = self.ssh.run(cmd, timeout=120)
                if rc == 0:
                    print(f"[upgrade] ✓")
                else:
                    print(f"[upgrade] ⚠ WARNING (exit {rc}): {out[-150:]} — continuing")
            except Exception as e:
                print(f"[upgrade] ⚠ WARNING: {e} — continuing")

    # ── Phase implementations ─────────────────────────────────────────────────

    def _download_new_package(self):
        tar_path = f"{self.install_dir}/{self.dst_package}"

        # Skip if already downloaded
        check, _ = self.ssh.run(
            f"test -f {tar_path} && test ! -f {tar_path}.aria2 && echo COMPLETE || echo MISSING"
        )
        if "COMPLETE" in check:
            size_out, _ = self.ssh.run(f"du -sh {tar_path} | awk '{{print $1}}'")
            print(f"[download_new_package] already downloaded ({size_out.strip()}) — skipping")
            return

        if self.nsg:
            self.nsg.attach()
            print("[download_new_package] NSG attached — waiting 30s ...")
            time.sleep(30)

        aria2c_out, _ = self.ssh.run("which aria2c")
        aria2c_bin    = aria2c_out.strip() or "/usr/bin/aria2c"

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
        size_out, _ = self.ssh.run(f"du -sh {tar_path} | awk '{{print $1}}'")
        print(f"[download_new_package] Downloaded {size_out.strip()} ✓")

    def _extract_new_package(self):
        # Skip if already extracted
        check, _ = self.ssh.run(
            f"test -d {self.dst_extract_dir} && echo EXISTS || echo MISSING"
        )
        if "EXISTS" in check:
            print(f"[extract_new_package] already extracted: {self.dst_extract_dir}")
            return

        tar_path = f"{self.install_dir}/{self.dst_package}"
        _, pigz_rc = self.ssh.run("which pigz 2>/dev/null")

        if pigz_rc == 0:
            cmd = f"cd {self.install_dir} && sudo tar -I pigz -xf {tar_path} 2>&1 && echo EXTRACTED"
            print("[extract_new_package] Extracting with pigz ...")
        else:
            cmd = f"cd {self.install_dir} && sudo tar -xf {tar_path} 2>&1 && echo EXTRACTED"
            print("[extract_new_package] Extracting with tar ...")

        out, rc = self.ssh.run(cmd, timeout=3600)
        assert rc == 0 and "EXTRACTED" in out, f"Extraction failed: {out[-300:]}"
        print(f"[extract_new_package] Extracted to {self.dst_extract_dir} ✓")

    def _create_upgrade_config(self):
        """
        Build new config.yaml by copying old config.yaml entirely,
        then updating only archive-directory to new package path.

        Everything else — star-domain, ha, size, type, storageClass etc
        stays exactly as in the old config.
        """
        old_config = f"{self.src_extract_dir}/config.yaml"
        new_config = f"{self.dst_extract_dir}/config.yaml"

        # Copy old config.yaml as base — retains ALL fields including storageClass
        self.ssh.run(f"sudo cp {old_config} {new_config}")

        # Update only archive-directory → new package path
        self.ssh.run(f"sudo sed -i 's|archive-directory:.*|archive-directory: RAFAY_PH|' {new_config}")
        self.ssh.run(f"sudo sed -i 's|archive-directory: RAFAY_PH|archive-directory: {self.dst_extract_dir}|' {new_config}")

        print(f"[create_upgrade_config] ✓")
        print(f"  base              : copied from {self.src_version}/config.yaml")
        print(f"  archive-directory : {self.dst_extract_dir}")
        print(f"  storageClass      : unchanged from {self.src_version}")

    def _copy_new_radm(self):
        out, rc = self.ssh.run(
            f"sudo cp {self.dst_extract_dir}/radm /usr/bin/radm && "
            f"sudo chmod +x /usr/bin/radm && echo OK"
        )
        assert rc == 0 and "OK" in out, f"radm copy failed: {out}"
        print("[copy_new_radm] new radm → /usr/bin/radm ✓")

    def _radm_dependency(self):
        print("[radm_dependency] Running ...")
        out, rc = self.ssh.run(
            f"cd {self.dst_extract_dir} && sudo ./radm dependency --config config.yaml 2>&1",
            timeout=600,
        )
        assert rc == 0, f"radm dependency failed (exit {rc}): {out[-300:]}"
        self._poll_pods(PHASE_WAIT["radm_dependency"], "after radm dependency")

    def _wait_elasticsearch(self):
        cfg      = PHASE_WAIT["elasticsearch"]
        deadline = time.time() + cfg["max_wait"]
        attempt  = 0
        print(f"[wait_elasticsearch] Waiting for es+kibana green ...")
        while time.time() < deadline:
            attempt += 1
            out, rc = self.ssh.run("kubectl get es,kibana -A --no-headers 2>/dev/null || echo NOT_READY")
            if rc == 0 and "NOT_READY" not in out and out.strip():
                lines     = [l for l in out.splitlines() if l.strip()]
                not_green = [l for l in lines if "green" not in l.lower()]
                print(f"[wait_elasticsearch] attempt {attempt}: {len(lines)-len(not_green)}/{len(lines)} green")
                if not not_green:
                    print("[wait_elasticsearch] all green ✓")
                    return
            else:
                print(f"[wait_elasticsearch] attempt {attempt}: not ready ...")
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

    def _radm_cluster_old(self):
        print(f"[radm_cluster_old] Running from {self.src_version} dir ...")
        out, rc = self.ssh.run_stream(
            f"cd {self.src_extract_dir} && sudo ./radm cluster --config config.yaml 2>&1",
            timeout=1200, prefix="[radm cluster old]",
        )
        assert rc == 0, f"radm cluster (old) failed (exit {rc}): {out[-300:]}"
        self._poll_pods(PHASE_WAIT["radm_cluster"], "after radm cluster old")

    def _radm_cluster_new(self):
        print(f"[radm_cluster_new] Running from {self.dst_version} dir ...")
        out, rc = self.ssh.run_stream(
            f"cd {self.dst_extract_dir} && sudo ./radm cluster --config config.yaml 2>&1",
            timeout=2400, prefix="[radm cluster new]",
        )
        assert rc == 0, f"radm cluster (new) failed (exit {rc}): {out[-300:]}"
        self._poll_pods(PHASE_WAIT["radm_cluster"], "after radm cluster new")

    # ── Poll helper ───────────────────────────────────────────────────────────

    def _poll_pods(self, cfg: dict, label: str):
        deadline     = time.time() + cfg["max_wait"]
        attempt      = 0
        stable_count = 0
        prev_total   = 0

        while time.time() < deadline:
            attempt += 1
            out, rc = self.ssh.run(
                "kubectl get pods -A --no-headers 2>/dev/null || "
                "/usr/local/bin/kubectl get pods -A --no-headers 2>&1"
            )
            if rc != 0:
                print(f"[poll_pods][{label}] kubectl not ready (attempt {attempt}) ...")
                time.sleep(cfg["interval"])
                continue

            lines     = [l for l in out.splitlines() if l.strip()]
            total     = len(lines)
            not_ready = [l for l in lines if "Running" not in l and "Completed" not in l]
            print(f"[poll_pods][{label}] attempt {attempt}: {total-len(not_ready)}/{total} Running")

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

    # ── Helper ────────────────────────────────────────────────────────────────

    def _build_url(self, package_name: str) -> str:
        match = re.search(r'v([\d.]+)-\d+\.tar\.gz', package_name)
        if match:
            return f"{S3_BASE}/{match.group(1)}/{package_name}"
        raise ValueError(
            f"Cannot auto-build URL from: {package_name}\n"
            f"Pass --dst-package-url for dev/custom packages."
        )