"""
lib/upgrade/upgrade_engine.py

UPDATED (2026-08-12): patch-command sourcing moved fully to
utils/helpers.py's load_canned_patch_commands() + config/patches/,
called from conftest.py -- NOT looked up inside this class. This engine
just takes six plain command lists as constructor args and runs them at
the right point. lib/upgrade/hops/ is not used by this file (superseded
by config/patches/ -- see config/patches/README.md for the "add a new
version pair" workflow). controller_type/src_version/dst_version are
NOT needed by this class for lookup purposes -- src_version/dst_version
remain cosmetic log labels only, matching the pre-hop-experiment design.

Three of the six command lists are typically "canned (from
config/patches/) + Jenkins textbox" merges, done by the CALLER
(conftest.py) before construction -- this class doesn't know or care
where a given list's commands came from, it just runs them in order:
  pre_commands, after_radm_dependency_commands, after_radm_application_commands

The other three have no Jenkins textbox equivalent, so the caller passes
canned-only lists (or [] if nothing canned exists for that pair):
  config_patches, post_commands, after_radm_cluster_commands

Phases, in the order they run:
  1.  pre_commands             — from conftest.py (canned + Jenkins)
  2.  download                 — new package via aria2c
  3.  extract                  — new package via pigz/tar
  4.  create_config            — copy template, patch archive-directory,
                                  then apply config_patches
  5.  copy_radm                — new radm → /usr/bin/
  6.  radm dependency          — always same, NEW package only
      after_radm_dependency_commands — from conftest.py (canned + Jenkins)
  7.  wait elasticsearch       — wait for green
  8.  radm application         — always same, NEW package only
      after_radm_application_commands — from conftest.py (canned + Jenkins)
  9.  post_commands            — canned only, right before radm cluster
  10. radm cluster              — NEW package only (single pass)
      after_radm_cluster_commands — canned only, before final pod polling
  11. final pod-health polling
"""

import re
import time

# ── Wait policies ─────────────────────────────────────────────────────────────
PHASE_WAIT = {
    "radm_dependency":  {"interval": 20, "max_wait": 600},
    "radm_application": {"interval": 20, "max_wait": 1000},
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
        config_patches: list = None,
        post_commands: list = None,
        after_radm_cluster_commands: list = None,
        expected_es_version: str = None,
    ):
        self.ssh         = ssh_client
        self.install_dir = install_dir
        self.star_domain = star_domain
        self.nsg         = nsg_manager

        # All six command lists are handed in as-is -- this class doesn't
        # know or care whether a given list came from config/patches/,
        # a Jenkins textbox, both merged, or neither. See conftest.py's
        # controller_upgrade fixture for where that merging happens.
        self.pre_commands = pre_commands or []
        self.after_radm_dependency_commands = after_radm_dependency_commands or []
        self.after_radm_application_commands = after_radm_application_commands or []
        self.config_patches = config_patches or []
        self.post_commands = post_commands or []
        self.expected_es_version = expected_es_version
        self.after_radm_cluster_commands = after_radm_cluster_commands or []

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
        name if nothing matches. Never used for any control-flow decision.
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

        self._phase("wait_elasticsearch", lambda: self._wait_elasticsearch(self.expected_es_version))

        self._phase("radm_application", self._radm_application)
        self._run_commands("after_radm_application", self.after_radm_application_commands)

        self._run_commands("post", self.post_commands)

        self._phase("radm_cluster", self._radm_cluster)
        self._run_commands("after_radm_cluster", self.after_radm_cluster_commands)
        self._phase("poll_after_radm_cluster",
                     lambda: self._poll_pods(PHASE_WAIT["radm_cluster"], "after radm cluster"))

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
        Warns on failure -- never stops the upgrade, matching how
        pre/post commands have always behaved in this engine (commands
        should end with `|| true` themselves if they're allowed to fail
        silently; this wrapper additionally never lets ANY single
        command's exception or non-zero exit abort the whole run).
        """
        if not commands:
            print(f"[upgrade] No {cmd_type} commands to run")
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

    # ── Phase implementations ─────────────────────────────────────────────────

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

        # Version-specific field edits from config/patches/.../config_patches.txt
        # (if one exists for this src->dst pair), run against the NEW
        # config.yaml right after it's created -- before any radm command
        # reads it.
        self._run_commands("config_patches", self.config_patches)

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

    def _wait_elasticsearch(self, expected_version: str = None):
        """
        Waits for the ECK Elasticsearch/Kibana CRDs to report PHASE=Ready.

        Only resources whose NAME ends in "-cluster" (e.g.
        rafay-es-cluster, rafay-kibana-cluster) are checked -- some
        environments also run legacy standalone instances (e.g.
        rafay-es, rafay-kibana) that intentionally stay on their own
        older version and are NOT part of this upgrade's rollout. Gating
        on ALL es/kibana resources would mean this phase can never
        succeed in an environment where those legacy instances exist,
        since they'll never reach expected_version -- it would just
        silently run out its full timeout on every single upgrade.

        If expected_version is given, ALSO requires every matched
        *-cluster resource's VERSION column to match it exactly. Leave
        it as None to accept any version, as long as PHASE=Ready.
        """
        cfg = PHASE_WAIT["elasticsearch"]
        deadline = time.time() + cfg["max_wait"]
        while time.time() < deadline:
            out, rc = self.ssh.run(
                "kubectl get es,kibana -A --no-headers "
                "-o custom-columns=KIND:.kind,NAME:.metadata.name,PHASE:.status.phase,VERSION:.status.version "
                "2>/dev/null || echo NOT_READY"
            )
            if rc == 0 and "NOT_READY" not in out and out.strip():
                all_lines = [l.split() for l in out.splitlines() if l.strip()]
                # KIND NAME PHASE VERSION -- only the -cluster resources
                # matter for this gate.
                lines = [l for l in all_lines if len(l) >= 2 and l[1].endswith("-cluster")]
                if not lines:
                    print("[wait_elasticsearch] no *-cluster ES/Kibana resources found yet ...")
                else:
                    not_ready = [l for l in lines if len(l) < 4 or l[2] != "Ready"]
                    wrong_version = (
                        [l for l in lines if len(l) >= 4 and l[3] != expected_version]
                        if expected_version else []
                    )
                    if not not_ready and not wrong_version:
                        versions = {l[3] for l in lines if len(l) >= 4}
                        print(f"[wait_elasticsearch] all *-cluster resources Ready ✓ (version: {', '.join(versions) or 'unknown'})")
                        return
                    if not_ready:
                        names = [l[1] for l in not_ready]
                        print(f"[wait_elasticsearch] waiting on not-yet-Ready: {names} ...")
                    elif wrong_version:
                        names_versions = {(l[1], l[3]) for l in wrong_version}
                        print(f"[wait_elasticsearch] Ready but version mismatch (want {expected_version}): {names_versions} ...")
            time.sleep(cfg["interval"])
        print(f"[wait_elasticsearch] ⚠ Timeout after {cfg['max_wait']}s — continuing anyway")

    def _radm_application(self):
        print("[radm_application] Running ...")
        out, rc = self.ssh.run(
            f"cd {self.dst_extract_dir} && sudo ./radm application --config config.yaml 2>&1",
            timeout=2400,
        )
        assert rc == 0, f"radm application failed (exit {rc}): {out[-300:]}"
        self._poll_pods(PHASE_WAIT["radm_application"], "after radm application")

    def _radm_cluster(self):
        """
        Runs `radm cluster` only. Pod-health polling for this phase is
        deliberately NOT done here -- it runs separately in run(), after
        after_radm_cluster_commands, so a canned
        config/patches/.../after_radm_cluster.txt (if one exists) gets to
        run before polling starts, not after.
        """
        print(f"[radm_cluster] Running from {self.dst_extract_dir} ...")
        out, rc = self.ssh.run_stream(
            f"cd {self.dst_extract_dir} && sudo ./radm cluster --config config.yaml 2>&1",
            timeout=2400, prefix="[radm cluster]",
        )
        assert rc == 0, f"radm cluster failed (exit {rc}): {out[-300:]}"

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