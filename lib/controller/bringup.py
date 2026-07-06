"""
lib/controller/bringup.py

ControllerBringup — single function to bring up a Rafay airgapped controller.
Called as a pytest fixture before validation test classes run.

Flow:
    1.  patch_hosts()          — fix OCI self-referential IP issue
    2.  setup_install_dir()    — mkdir /opt/rafay
    3.  fix_dns()              — ensure /etc/resolv.conf is valid
    4.  disable_firewall()     — ufw + iptables flush
    5.  install_aria2c()       — apt install aria2 (NSG attached/detached)
    6.  download_package()     — aria2c -x 16 from S3
    7.  extract_package()      — tar -xf to /opt/rafay
    8.  copy_radm_binary()     — cp radm /usr/bin/
    9.  create_config_yaml()   — patch size/ha/type/archive-dir/star-domain
    10. setup_secondary_nodes()— HA only: repeat steps 3-9 on node2+node3
    11. radm_init()            — radm init + kubeconfig + HA join
                                 → polls node Ready every 30s for 5 min
    12. radm_dependency()      — radm dependency
                                 → polls pods every 20s for 20 min
    13. radm_application()     — radm application
                                 → polls pods every 20s for 35 min
    14. radm_cluster()         — radm cluster (streaming output, 3-retry on 502)
                                 → polls pods every 20s for 15 min

Jenkins params:
    --controller-size   S | M | L | POC
    --package-name      rafay-airgapped-controller-v3.1-39.tar.gz
"""

import time
import subprocess
import base64
from typing import Optional, List


# ── Retry wait policies per phase ─────────────────────────────────────────────
PHASE_WAIT = {
    "radm_init":        {"interval": 30, "max_wait": 300,   "label": "nodes Ready"},
    "radm_dependency":  {"interval": 20, "max_wait": 1200,  "label": "pods Running"},
    "radm_application": {"interval": 20, "max_wait": 2100,  "label": "pods Running"},
    "radm_cluster":     {"interval": 20, "max_wait": 2500,  "label": "pods Running"},
}


class BringupError(Exception):
    """Raised when a bringup phase fails after retries."""
    def __init__(self, phase: str, message: str):
        self.phase = phase
        super().__init__(f"[{phase}] {message}")


class ControllerBringup:
    """
    Drives the full controller install flow on an already-provisioned OCI VM.

    Usage (in conftest.py fixture):
        bringup = ControllerBringup(
            ssh_client=ssh,
            controller_profile=profile,
            package_profile=package,
            star_domain="shc-22.dev.rafay-edge.net",
            secondary_ips=["10.0.0.2", "10.0.0.3"],   # HA only
            secondary_instance_ids=["ocid1...", "ocid1..."],
            oci_profile=oci_profile,
        )
        bringup.run()

    After run() completes, package_profile._actual_extract_dir is set
    so downstream pytest validation tests can find the install path.
    """

    def __init__(
        self,
        ssh_client,
        controller_profile,
        package_profile,
        star_domain:             str = "",
        secondary_ips:           Optional[List[str]] = None,
        secondary_instance_ids:  Optional[List[str]] = None,
        oci_profile=None,
        nsg_manager=None,
    ):
        self.ssh             = ssh_client
        self.profile         = controller_profile
        self.pkg             = package_profile
        self.star_domain     = star_domain
        self.secondary_ips   = secondary_ips or []
        self.secondary_ids   = secondary_instance_ids or []
        self.oci_profile     = oci_profile
        self.nsg             = nsg_manager
        self.extract_dir     = None   # set after extraction

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self):
        """
        Run the full bringup sequence.
        Raises BringupError on first phase that fails.
        """
        print("\n" + "═" * 60)
        print(f"[bringup] Starting controller bringup")
        print(f"[bringup]   size    : {self.profile.controller_size}")
        print(f"[bringup]   mode    : {self.profile.mode_label}")
        print(f"[bringup]   os      : {self.profile.os_type}")
        print(f"[bringup]   package : {self.pkg.name}")
        print(f"[bringup]   domain  : {self.star_domain or 'not set'}")
        print("═" * 60 + "\n")

        self._phase("setup_install_dir",    self._setup_install_dir)
        self._phase("fix_dns",              self._fix_dns)
        self._phase("disable_firewall",     self._disable_firewall)
        self._phase("install_aria2c",       self._install_aria2c)
        self._phase("download_package",     self._download_package)
        self._phase("extract_package",      self._extract_package)
        self._phase("copy_radm_binary",     self._copy_radm_binary)
        self._phase("create_config_yaml",   self._create_config_yaml)
        if self.profile.ha and self.secondary_ips:
            self._phase("setup_secondary_nodes", self._setup_secondary_nodes)
        self._phase("radm_init",            self._radm_init)
        self._phase("radm_dependency",      self._radm_dependency)
        self._phase("radm_application",     self._radm_application)
        self._phase("patch_hosts",          self._patch_hosts)
        self._phase("radm_cluster",         self._radm_cluster)

        print("\n" + "═" * 60)
        print(f"[bringup] ✅ Controller bringup complete")
        print(f"[bringup]   extract_dir: {self.extract_dir}")
        print("═" * 60 + "\n")

    # ── Phase runner ──────────────────────────────────────────────────────────

    def _phase(self, name: str, fn):
        """Run a phase, print header/footer, raise BringupError on failure."""
        print(f"\n[bringup] ── Phase: {name} " + "─" * max(0, 40 - len(name)))
        try:
            fn()
            print(f"[bringup] ✓ {name}")
        except BringupError:
            raise
        except Exception as e:
            raise BringupError(name, str(e)) from e

    # ── Phase implementations ─────────────────────────────────────────────────

    def _setup_install_dir(self):
        out, rc = self.ssh.run(
            f"sudo mkdir -p {self.pkg.install_dir} && "
            f"sudo chmod 777 {self.pkg.install_dir} && echo OK"
        )
        assert rc == 0 and "OK" in out, f"mkdir failed: {out}"
        print(f"[setup_install_dir] {self.pkg.install_dir} ready")

    def _fix_dns(self):
        """Ensure /etc/resolv.conf exists with working nameservers."""
        check, _ = self.ssh.run("cat /etc/resolv.conf 2>/dev/null || echo MISSING")
        if "nameserver" in check:
            print("[fix_dns] resolv.conf already configured")
            return
        print("[fix_dns] resolv.conf missing — fixing ...")
        self.ssh.run("sudo systemctl start resolvconf 2>/dev/null || true && sleep 1", timeout=10)
        fix_out, _ = self.ssh.run(
            "test -f /run/resolvconf/resolv.conf && "
            "sudo ln -sf /run/resolvconf/resolv.conf /etc/resolv.conf && "
            "echo FIXED || echo MISSING"
        )
        if "MISSING" in fix_out:
            self.ssh.run("sudo systemctl start systemd-resolved 2>/dev/null || true && sleep 2", timeout=15)
            self.ssh.run(
                "test -f /run/systemd/resolve/resolv.conf && "
                "sudo ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf || true",
                timeout=10
            )
        recheck, _ = self.ssh.run("cat /etc/resolv.conf 2>/dev/null || echo STILL_MISSING")
        if "nameserver" not in recheck:
            self.ssh.run(
                "sudo rm -f /etc/resolv.conf && "
                "printf 'nameserver 169.254.169.254\nnameserver 8.8.8.8\nnameserver 8.8.4.4\n' "
                "| sudo tee /etc/resolv.conf > /dev/null",
                timeout=10
            )
        final, _ = self.ssh.run("cat /etc/resolv.conf 2>/dev/null || echo FAILED")
        assert "nameserver" in final, f"Could not fix resolv.conf: {final}"
        print("[fix_dns] resolv.conf fixed ✓")

    def _disable_firewall(self):
        """Disable ufw and flush iptables."""
        self.ssh.run(
            "sudo ufw disable 2>/dev/null || true && "
            "sudo systemctl stop ufw 2>/dev/null || true && "
            "sudo systemctl disable ufw 2>/dev/null || true && "
            "sudo iptables -F && sudo iptables -t nat -F && "
            "sudo iptables -t mangle -F && sudo iptables -X 2>/dev/null || true",
            timeout=15
        )
        print("[disable_firewall] ufw disabled + iptables flushed ✓")

    def _install_aria2c(self):
        """Install aria2c. NSG attached for internet access, detached after download."""
        out, rc = self.ssh.run("which aria2c 2>/dev/null && aria2c --version 2>/dev/null | head -1")
        if rc == 0 and "aria2" in out.lower():
            print(f"[install_aria2c] already installed: {out.strip()}")
            return

        if self.nsg:
            self.nsg.attach()
            print("[install_aria2c] NSG attached — waiting 30s for rules to propagate ...")
            time.sleep(30)

        if self.profile.os_type == "ubuntu24":
            self.ssh.run("sudo apt-get -o Acquire::ForceIPv4=true update -y 2>&1", timeout=120)
            _, rc = self.ssh.run("sudo apt-get -o Acquire::ForceIPv4=true install -y aria2 2>&1", timeout=180)
            if rc != 0:
                _, rc = self.ssh.run(
                    "sudo add-apt-repository universe -y 2>&1 && "
                    "sudo apt-get update -y 2>&1 && sudo apt-get install -y aria2 2>&1",
                    timeout=300
                )
                assert rc == 0, "aria2c install failed"
        else:
            _, rc = self.ssh.run("sudo yum install -y aria2 2>&1 || sudo dnf install -y aria2 2>&1", timeout=180)
            assert rc == 0, "aria2c install failed on RHEL"

        self.ssh.run(
            "sudo iptables -F && sudo iptables -t nat -F && "
            "sudo iptables -t mangle -F && sudo iptables -X 2>/dev/null || true",
            timeout=10
        )
        verify_out, verify_rc = self.ssh.run("which aria2c")
        assert verify_rc == 0, "aria2c not found after install"
        print(f"[install_aria2c] installed at {verify_out.strip()} ✓")

    def _download_package(self):
        """Download controller package from S3 using aria2c."""
        tar_check, _ = self.ssh.run(
            f"test -f {self.pkg.tar_path} && "
            f"test ! -f {self.pkg.tar_path}.aria2 && echo COMPLETE || echo MISSING"
        )
        if "COMPLETE" in tar_check:
            size_out, _ = self.ssh.run(f"du -sh {self.pkg.tar_path} | awk '{{print $1}}'")
            print(f"[download_package] already downloaded ({size_out.strip()}) — skipping")
            if self.nsg:
                self.nsg.detach()
            return

        self.ssh.run(f"sudo rm -f {self.pkg.tar_path} {self.pkg.tar_path}.aria2 2>/dev/null || true")

        aria2c_bin_out, _ = self.ssh.run("which aria2c")
        aria2c_bin = aria2c_bin_out.strip() or "/usr/bin/aria2c"

        print(f"[download_package] Downloading: {self.pkg.url}")
        out, rc = self.ssh.run(
            f"cd {self.pkg.install_dir} && "
            f"sudo {aria2c_bin} -x 16 -s 16 --max-tries=3 --retry-wait=10 "
            f"--connect-timeout=30 --log-level=notice {self.pkg.url} 2>&1",
            timeout=1800
        )
        if self.nsg:
            self.nsg.detach()
            print("[download_package] NSG detached ✓")

        assert rc == 0, f"aria2c download failed (exit {rc}): {out[-300:]}"

        size_out, verify_rc = self.ssh.run(
            f"test -s {self.pkg.tar_path} && du -sh {self.pkg.tar_path} | awk '{{print $1}}'"
        )
        assert verify_rc == 0, f"Downloaded file missing or empty: {self.pkg.tar_path}"
        print(f"[download_package] Downloaded {size_out.strip()} ✓")

    def _extract_package(self):
        """Extract tar.gz to install_dir. Skips if already extracted."""
        ls_out, _ = self.ssh.run(f"ls -1 {self.pkg.install_dir}/ 2>/dev/null")
        before = set(ls_out.strip().splitlines())
        already = [d for d in before if "rafay-airgapped-controller" in d and not d.endswith(".tar.gz")]
        if already:
            self.extract_dir = f"{self.pkg.install_dir}/{already[0]}"
            self.pkg._actual_extract_dir = self.extract_dir
            print(f"[extract_package] already extracted: {self.extract_dir}")
            return

        print(f"[extract_package] Extracting {self.pkg.tar_path} ...")
        out, rc = self.ssh.run(
            f"sudo tar -xf {self.pkg.tar_path} -C {self.pkg.install_dir} 2>&1 && echo EXTRACTED",
            timeout=3600
        )
        assert rc == 0 and "EXTRACTED" in out, f"tar extraction failed: {out[-300:]}"

        ls_after, _ = self.ssh.run(f"ls -1 {self.pkg.install_dir}/ 2>/dev/null")
        after = set(ls_after.strip().splitlines())
        new   = [e for e in (after - before) if not e.endswith(".tar.gz")]
        assert new, f"No new directory found after extraction"

        self.extract_dir = f"{self.pkg.install_dir}/{new[0]}"
        self.pkg._actual_extract_dir = self.extract_dir
        print(f"[extract_package] Extracted to {self.extract_dir} ✓")

    def _copy_radm_binary(self):
        out, rc = self.ssh.run(
            f"sudo cp {self.extract_dir}/radm /usr/bin/radm && "
            f"sudo chmod +x /usr/bin/radm && echo OK"
        )
        assert rc == 0 and "OK" in out, f"radm binary copy failed: {out}"
        print("[copy_radm_binary] radm → /usr/bin/radm ✓")

    def _create_config_yaml(self):
        """Copy template and patch all required fields."""
        config_path = f"{self.extract_dir}/config.yaml"
        tmpl_names  = ["config.yaml-airgap-tmpl", "config.yaml-tmpl", "config.yaml.tmpl"]
        tmpl_path   = None
        for name in tmpl_names:
            check, _ = self.ssh.run(f"test -f {self.extract_dir}/{name} && echo FOUND || echo MISSING")
            if "FOUND" in check:
                tmpl_path = f"{self.extract_dir}/{name}"
                break
        assert tmpl_path, f"No config template found in {self.extract_dir}"

        self.ssh.run(f"sudo cp {tmpl_path} {config_path}")

        size      = self.profile.controller_size
        ha        = "true" if self.profile.ha else "false"
        repo_path = self.extract_dir
        q         = '"'

        for patch in [
            f"sudo sed -i '/size:/s|:.*|: {q}{size}{q}|' {config_path}",
            f"sudo sed -i '/^[ ]*ha:/s|:.*|: {ha}|' {config_path}",
        ]:
            _, rc = self.ssh.run(patch)
            assert rc == 0

        self.ssh.run(f"sudo sed -i 's|^    type:.*|    type: TYPE_PLACEHOLDER|' {config_path}")
        _, rc = self.ssh.run(
            f"sudo sed -i " + "'" + r's|type: TYPE_PLACEHOLDER|type: "airgap"|' + "'" + f" {config_path}"
        )
        assert rc == 0

        self.ssh.run(f"sudo sed -i 's|archive-directory:.*|archive-directory: RAFAY_PLACEHOLDER|' {config_path}")
        _, rc = self.ssh.run(f"sudo sed -i 's|archive-directory: RAFAY_PLACEHOLDER|archive-directory: {repo_path}|' {config_path}")
        assert rc == 0

        if self.star_domain:
            self.ssh.run(f"sudo sed -i '/^[ ]*star-domain:/s|star-domain:.*|star-domain: STAR_PLACEHOLDER|' {config_path}")
            _, rc = self.ssh.run(f"sudo sed -i 's|star-domain: STAR_PLACEHOLDER|star-domain: {self.star_domain}|' {config_path}")
            assert rc == 0

        # ── Signed cert (opt-in, everything above is completely unchanged) ────
        if getattr(self, "use_signed_cert", False):
            self._patch_signed_cert(config_path)

        print(f"[create_config_yaml] Patched: size={size} ha={ha} domain={self.star_domain} ✓")

    def _patch_signed_cert(self, config_path: str):
        """
        Replaces the controller's self-signed cert with a Let's Encrypt
        wildcard cert issued via Route53 DNS-01 (lib/certs/cert_manager.py).

        Only called when use_signed_cert=True. Does not touch config.yaml
        at all otherwise — self-signed behavior is fully preserved.
        """
        from lib.certs.cert_manager import generate_signed_cert, CertGenerationError

        assert self.star_domain, "star_domain is required to issue a signed cert"

        try:
            cert_b64, key_b64 = generate_signed_cert(
                star_domain=self.star_domain,
                email=self.cert_email,
            )
        except CertGenerationError as e:
            raise Exception(f"Signed cert generation failed: {e}") from e

        q = '"'

        # generate-self-signed-certs: false
        _, rc = self.ssh.run(
            f"sudo sed -i '/^[ ]*generate-self-signed-certs:/s|:.*|: false|' {config_path}"
        )
        assert rc == 0

        # certificate: "<base64 fullchain>"
        self.ssh.run(f"sudo sed -i '/^[ ]*certificate:/s|:.*|: CERT_PLACEHOLDER|' {config_path}")
        _, rc = self.ssh.run(f"sudo sed -i 's|CERT_PLACEHOLDER|{q}{cert_b64}{q}|' {config_path}")
        assert rc == 0

        # key: "<base64 privkey>"
        self.ssh.run(f"sudo sed -i '/^[ ]*key:/s|:.*|: KEY_PLACEHOLDER|' {config_path}")
        _, rc = self.ssh.run(f"sudo sed -i 's|KEY_PLACEHOLDER|{q}{key_b64}{q}|' {config_path}")
        assert rc == 0

        print(f"[create_config_yaml] Signed cert installed for *.{self.star_domain} ✓ "
              f"(generate-self-signed-certs=false)")

    def _patch_signed_cert(self, config_path: str):
        """
        Replaces the controller's self-signed cert with a Let's Encrypt
        wildcard cert issued via Route53 DNS-01 (lib/certs/cert_manager.py).

        Only called when use_signed_cert=True. Does not touch config.yaml
        at all otherwise — self-signed behavior is fully preserved.
        """
        from lib.certs.cert_manager import generate_signed_cert, CertGenerationError

        assert self.star_domain, "star_domain is required to issue a signed cert"

        try:
            cert_b64, key_b64 = generate_signed_cert(
                star_domain=self.star_domain,
                email=self.cert_email,
            )
        except CertGenerationError as e:
            raise Exception(f"Signed cert generation failed: {e}") from e

        q = '"'

        # generate-self-signed-certs: false
        _, rc = self.ssh.run(
            f"sudo sed -i '/^[ ]*generate-self-signed-certs:/s|:.*|: false|' {config_path}"
        )
        assert rc == 0

        # certificate: "<base64 fullchain>"
        self.ssh.run(f"sudo sed -i '/^[ ]*certificate:/s|:.*|: CERT_PLACEHOLDER|' {config_path}")
        _, rc = self.ssh.run(f"sudo sed -i 's|CERT_PLACEHOLDER|{q}{cert_b64}{q}|' {config_path}")
        assert rc == 0

        # key: "<base64 privkey>"
        self.ssh.run(f"sudo sed -i '/^[ ]*key:/s|:.*|: KEY_PLACEHOLDER|' {config_path}")
        _, rc = self.ssh.run(f"sudo sed -i 's|KEY_PLACEHOLDER|{q}{key_b64}{q}|' {config_path}")
        assert rc == 0

        print(f"[create_config_yaml] Signed cert installed for *.{self.star_domain} ✓ "
              f"(generate-self-signed-certs=false)")

    def _setup_secondary_nodes(self):
        """HA: full setup on node2 + node3 (download, extract, config.yaml copy)."""
        from lib.oci.vm_manager import OCINSGManager
        from lib.ssh.ssh_client import SSHClient

        config_path    = f"{self.extract_dir}/config.yaml"
        config_content, _ = self.ssh.run(f"sudo cat {config_path}")
        encoded_config = base64.b64encode(config_content.encode()).decode()
        padded_ids     = list(self.secondary_ids) + [""] * len(self.secondary_ips)

        for i, (sec_ip, sec_id) in enumerate(zip(self.secondary_ips, padded_ids), 2):
            print(f"[setup_secondary_nodes] Setting up node{i} ({sec_ip}) ...")
            sec_nsg = OCINSGManager(self.oci_profile, sec_id) if (self.oci_profile and self.oci_profile.nsg_id and sec_id) else None
            sec_ssh = SSHClient(host=sec_ip, user=self.profile.user, key_path=self.profile.ssh_key)
            sec_ssh.connect()
            try:
                sec_ssh.run(f"sudo mkdir -p {self.pkg.install_dir} && sudo chmod 777 {self.pkg.install_dir}")
                sec_ssh.run(
                    "sudo ufw disable 2>/dev/null || true && sudo iptables -F && "
                    "sudo iptables -t nat -F && sudo iptables -t mangle -F && "
                    "sudo iptables -X 2>/dev/null || true",
                    timeout=15
                )
                check_out, _ = sec_ssh.run(f"test -d {self.extract_dir} && echo EXISTS || echo MISSING")
                if "MISSING" in check_out:
                    if sec_nsg:
                        sec_nsg.attach()
                        time.sleep(30)
                    _, aria2c_rc = sec_ssh.run("which aria2c 2>/dev/null")
                    if aria2c_rc != 0:
                        sec_ssh.run("sudo apt-get -o Acquire::ForceIPv4=true update -y 2>&1 || true", timeout=120)
                        sec_ssh.run("sudo apt-get -o Acquire::ForceIPv4=true install -y aria2 2>&1 || true", timeout=180)
                        sec_ssh.run("sudo iptables -F && sudo iptables -t nat -F && sudo iptables -t mangle -F 2>/dev/null || true", timeout=10)
                    tar_check, _ = sec_ssh.run(
                        f"test -f {self.pkg.tar_path} && test ! -f {self.pkg.tar_path}.aria2 && echo COMPLETE || echo MISSING"
                    )
                    if "MISSING" in tar_check:
                        aria2c_bin_out, _ = sec_ssh.run("which aria2c")
                        aria2c_bin = aria2c_bin_out.strip() or "/usr/bin/aria2c"
                        dl_out, dl_rc = sec_ssh.run(
                            f"cd {self.pkg.install_dir} && "
                            f"sudo {aria2c_bin} -x 16 -s 16 --max-tries=3 --connect-timeout=30 {self.pkg.url} 2>&1",
                            timeout=1800
                        )
                        assert dl_rc == 0, f"Download failed on node{i}: {dl_out[-200:]}"
                    if sec_nsg:
                        sec_nsg.detach()
                    ext_out, ext_rc = sec_ssh.run(
                        f"sudo tar -xf {self.pkg.tar_path} -C {self.pkg.install_dir} 2>&1 && echo EXTRACTED",
                        timeout=3600
                    )
                    assert ext_rc == 0 and "EXTRACTED" in ext_out, f"Extraction failed on node{i}"
                else:
                    print(f"[setup_secondary_nodes] node{i}: already extracted — skipping")
                    if sec_nsg:
                        try: sec_nsg.detach()
                        except: pass

                write_out, write_rc = sec_ssh.run(
                    f"echo '{encoded_config}' | base64 -d | sudo tee {config_path} > /dev/null && echo OK"
                )
                assert write_rc == 0 and "OK" in write_out, f"config.yaml copy failed on node{i}"
                print(f"[setup_secondary_nodes] node{i} ({sec_ip}) ready ✓")
            finally:
                sec_ssh.disconnect()

    def _radm_init(self):
        """radm init on node1, kubeconfig setup, HA join on node2+3."""
        self.ssh.run("sudo systemctl stop consul kubelet 2>/dev/null || true && sleep 2", timeout=15)
        self.ssh.run("sudo kubeadm reset -f 2>/dev/null || true", timeout=30)
        self.ssh.run(
            "sudo rm -rf /etc/kubernetes /var/lib/kubelet /var/lib/etcd "
            "/var/run/kubernetes /tmp/rafay-infra /var/lib/consul "
            "/etc/consul.d/rafay*.hcl /etc/consul.d/rafay*.json 2>/dev/null || true",
            timeout=15
        )
        self.ssh.run(
            "sudo rm -rf /etc/containerd/certs.d/ && sudo rm -f /etc/containerd/config.toml 2>/dev/null || true",
            timeout=10
        )
        self.ssh.run("sudo systemctl restart containerd && sleep 5", timeout=30)

        print("[radm_init] Running radm init ...")
        out, rc = self.ssh.run(
            f"cd {self.extract_dir} && sudo ./radm init --config config.yaml "
            f"--skip-phases infra/containerd/install-containerd-config-toml 2>&1",
            timeout=1800,
        )
        assert rc == 0, f"radm init failed (exit {rc}): {out[-500:]}"
        print("[radm_init] radm init complete ✓")

        _, krc = self.ssh.run(
            "mkdir -p $HOME/.kube && sudo cp -f /etc/kubernetes/admin.conf $HOME/.kube/config && "
            "sudo chown $(id -u):$(id -g) -R $HOME/.kube && echo OK"
        )
        assert krc == 0, "kubeconfig setup failed"

        if self.profile.ha and self.secondary_ips:
            self._ha_join()

        expected = 3 if (self.profile.ha and self.secondary_ips) else 1
        print(f"[radm_init] Polling {expected} node(s) Ready every 30s for 5 min ...")
        self._poll_nodes_ready(
            interval=PHASE_WAIT["radm_init"]["interval"],
            max_wait=PHASE_WAIT["radm_init"]["max_wait"],
            expected_count=expected,
        )

    def _ha_join(self):
        """Build join command and run radm join on secondary nodes."""
        kubeadm_out, _ = self.ssh.run(
            "find /tmp/rafay-infra /usr/local/bin /usr/bin -name 'rafay-kubeadm' -type f 2>/dev/null | head -1"
        )
        kubeadm_bin = kubeadm_out.strip() or "/tmp/rafay-infra/packages/kubeadm/amd64/rafay-kubeadm"

        token_out, _ = self.ssh.run(
            f"sudo {kubeadm_bin} token list --kubeconfig=/etc/kubernetes/admin.conf 2>/dev/null | "
            f"grep 'authentication,signing' | head -1 | awk '{{print $1}}'"
        )
        if not token_out.strip():
            token_out, _ = self.ssh.run(f"sudo {kubeadm_bin} token create --kubeconfig=/etc/kubernetes/admin.conf 2>/dev/null")

        ca_hash_out, _ = self.ssh.run(
            "openssl x509 -pubkey -in /etc/kubernetes/pki/ca.crt | "
            "openssl rsa -pubin -outform der 2>/dev/null | openssl dgst -sha256 -hex | sed 's/^.* //'"
        )
        primary_ip_out, _ = self.ssh.run("hostname -I | awk '{print $1}'")
        cert_key_raw, _   = self.ssh.run(f"cd {self.extract_dir} && sudo ./radm init phase infra upload-certs --config config.yaml 2>&1")

        cert_key = ""
        for line in cert_key_raw.splitlines():
            stripped = line.strip()
            if len(stripped) == 64 and all(c in "0123456789abcdef" for c in stripped):
                cert_key = stripped
                break

        token  = token_out.strip()
        ca_hash = ca_hash_out.strip()
        pri_ip  = primary_ip_out.strip()
        assert token and ca_hash and pri_ip and cert_key, "Could not build join command — missing token/hash/cert_key"

        join_cmd = (
            f"cd {self.extract_dir} && sudo ./radm join {pri_ip}:6443 "
            f"--token {token} --discovery-token-ca-cert-hash sha256:{ca_hash} "
            f"--control-plane --certificate-key {cert_key} --config config.yaml"
        )

        print("[ha_join] Waiting 60s for etcd to stabilize ...")
        time.sleep(60)

        for i, sec_ip in enumerate(self.secondary_ips, 2):
            prereq = [
                "ssh", "-i", self.profile.ssh_key,
                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=30", f"{self.profile.user}@{sec_ip}",
                f"sudo systemctl stop consul 2>/dev/null || true && "
                f"sudo rm -rf /var/lib/consul /etc/consul.d/rafay*.hcl /etc/consul.d/rafay*.json 2>/dev/null || true && "
                f"sudo mkdir -p /run/systemd/resolve && "
                f"printf 'nameserver 169.254.169.254\nnameserver 8.8.8.8\nnameserver 8.8.4.4\n' "
                f"| sudo tee /run/systemd/resolve/resolv.conf > /dev/null && "
                f"sudo rm -f /etc/resolv.conf && "
                f"printf 'nameserver 169.254.169.254\nnameserver 8.8.8.8\nnameserver 8.8.4.4\n' "
                f"| sudo tee /etc/resolv.conf > /dev/null && "
                f"grep -q 'k8master.service.edgedc.consul' /etc/hosts || "
                f"echo '{pri_ip} k8master.service.edgedc.consul' | sudo tee -a /etc/hosts && echo PREREQ_DONE"
            ]
            subprocess.run(prereq, capture_output=True, text=True, timeout=30)

            ssh_join = [
                "ssh", "-i", self.profile.ssh_key,
                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=60",
                "-o", "ConnectTimeout=30", f"{self.profile.user}@{sec_ip}",
                join_cmd
            ]
            join_rc  = 1
            join_out = ""
            for attempt in range(1, 4):
                if attempt > 1:
                    print(f"[ha_join] node{i}: retry {attempt}/3 ...")
                    time.sleep(30)
                result   = subprocess.run(ssh_join, capture_output=True, text=True, timeout=1800)
                join_out = (result.stdout + result.stderr).strip()
                join_rc  = result.returncode
                if join_rc == 0:
                    break
                if "can only promote a learner" not in join_out and "FailedPrecondition" not in join_out:
                    break

            assert join_rc == 0, f"radm join failed on node{i} ({sec_ip}): {join_out[-300:]}"
            print(f"[ha_join] node{i} ({sec_ip}) joined ✓")

    def _radm_dependency(self):
        print("[radm_dependency] Running radm dependency ...")
        out, rc = self.ssh.run(
            f"cd {self.extract_dir} && sudo ./radm dependency --config config.yaml 2>&1",
            timeout=600,
        )
        assert rc == 0, f"radm dependency failed (exit {rc}): {out[-300:]}"
        print("[radm_dependency] complete ✓ — polling pods ...")
        self._poll_pods_running(
            interval=PHASE_WAIT["radm_dependency"]["interval"],
            max_wait=PHASE_WAIT["radm_dependency"]["max_wait"],
            label="after radm dependency",
        )

    def _radm_application(self):
        print("[radm_application] Running radm application ...")
        out, rc = self.ssh.run(
            f"cd {self.extract_dir} && sudo ./radm application --config config.yaml 2>&1",
            timeout=2400,
        )
        assert rc == 0, f"radm application failed (exit {rc}): {out[-300:]}"
        print("[radm_application] complete ✓ — polling pods ...")
        self._poll_pods_running(
            interval=PHASE_WAIT["radm_application"]["interval"],
            max_wait=PHASE_WAIT["radm_application"]["max_wait"],
            label="after radm application",
        )

    def _patch_hosts(self):
        """Patch /etc/hosts so star-domain resolves to 127.0.0.1."""
        if not self.star_domain:
            print("[patch_hosts] star_domain not set — skipping")
            return
        for hostname in [
            f"ops-console.{self.star_domain}",
            f"registry.{self.star_domain}",
            f"*.{self.star_domain}",
        ]:
            self.ssh.run(
                f"grep -q '{hostname}' /etc/hosts || "
                f"echo '127.0.0.1 {hostname}' | sudo tee -a /etc/hosts"
            )
        print(f"[patch_hosts] /etc/hosts patched for {self.star_domain} ✓")

    def _radm_cluster(self):
        """
        Run radm cluster with retry on 502 Bad Gateway errors.

        502 happens when the registry pod restarts mid-upload (large image
        sets — 300+ images — can trigger registry restarts under load).
        Retries from scratch after waiting for the registry to recover.
        """
        max_retries = 3
        retry_wait  = 60

        for attempt in range(1, max_retries + 1):
            print(f"[radm_cluster] attempt {attempt}/{max_retries} ...")
            out, rc = self.ssh.run_stream(
                f"cd {self.extract_dir} && sudo ./radm cluster --config config.yaml 2>&1",
                timeout=2400,
                prefix="[radm cluster]",
            )

            if rc == 0:
                print("[radm_cluster] complete ✓ — polling pods ...")
                self._poll_pods_running(
                    interval=PHASE_WAIT["radm_cluster"]["interval"],
                    max_wait=PHASE_WAIT["radm_cluster"]["max_wait"],
                    label="after radm cluster",
                )
                return

            # 502 Bad Gateway — registry pod restarted mid-upload
            if "502" in out or "Bad Gateway" in out:
                if attempt < max_retries:
                    print(f"[radm_cluster] ⚠ 502 Bad Gateway — registry pod may have restarted")
                    print(f"[radm_cluster] Waiting {retry_wait}s for registry to recover ...")
                    time.sleep(retry_wait)

                    # Wait for ops-console pod to be ready before retrying
                    deadline = time.time() + 300
                    while time.time() < deadline:
                        pod_out, _ = self.ssh.run(
                            "kubectl get pods -n rafay-core -l app=ops-console "
                            "--no-headers 2>/dev/null | grep -c Running || echo 0"
                        )
                        if pod_out.strip() != "0":
                            print(f"[radm_cluster] ops-console ready — retrying ...")
                            break
                        print(f"[radm_cluster] ops-console not ready — waiting 15s ...")
                        time.sleep(15)
                else:
                    raise Exception(
                        f"radm cluster failed with 502 Bad Gateway after {max_retries} attempts.\n"
                        f"Registry pod keeps restarting under image upload load.\n"
                        f"Check ops-console pod logs:\n"
                        f"  kubectl logs -n rafay-core -l app=ops-console --tail=50\n"
                        f"Last output: {out[-300:]}"
                    )
            else:
                # Non-502 failure — fail immediately, don't retry
                raise Exception(f"radm cluster failed (exit {rc}): {out[-300:]}")

    # ── Poll helpers ──────────────────────────────────────────────────────────

    def _poll_nodes_ready(self, interval: int, max_wait: int, expected_count: int = 1):
        """Poll kubectl get nodes every `interval` seconds until `expected_count` nodes Ready."""
        deadline = time.time() + max_wait
        attempt  = 0
        while time.time() < deadline:
            attempt += 1
            out, rc = self.ssh.run(
                "kubectl get nodes --no-headers 2>/dev/null || /usr/local/bin/kubectl get nodes --no-headers 2>&1"
            )
            lines = [l for l in out.splitlines() if l.strip()]
            ready = [l for l in lines if "Ready" in l and "NotReady" not in l]
            print(f"[poll_nodes] attempt {attempt}: {len(ready)}/{len(lines)} Ready (need {expected_count})")
            if len(ready) >= expected_count:
                print(f"[poll_nodes] {expected_count} node(s) Ready ✓")
                return
            time.sleep(interval)
        out, _ = self.ssh.run("kubectl get nodes 2>&1")
        print(f"[poll_nodes] Timeout after {max_wait}s — current state:\n{out}")

    def _poll_pods_running(self, interval: int, max_wait: int, label: str = ""):
        """Poll kubectl get pods -A every `interval` seconds until all Running/Completed."""
        deadline     = time.time() + max_wait
        attempt      = 0
        stable_count = 0
        prev_total   = 0

        while time.time() < deadline:
            attempt += 1
            out, rc = self.ssh.run(
                "kubectl get pods -A --no-headers 2>/dev/null || /usr/local/bin/kubectl get pods -A --no-headers 2>&1"
            )
            if rc != 0:
                print(f"[poll_pods][{label}] kubectl not ready (attempt {attempt}) ...")
                stable_count = 0
                time.sleep(interval)
                continue

            lines     = [l for l in out.splitlines() if l.strip()]
            total     = len(lines)
            not_ready = [l for l in lines if "Running" not in l and "Completed" not in l]
            print(f"[poll_pods][{label}] attempt {attempt}: {total - len(not_ready)}/{total} Running")

            if not not_ready and total > 0:
                stable_count = stable_count + 1 if total == prev_total else 0
                if stable_count >= 2:
                    print(f"[poll_pods][{label}] All {total} pods Running/Completed ✓")
                    return
            else:
                stable_count = 0

            prev_total = total
            time.sleep(interval)

        out, _ = self.ssh.run("kubectl get pods -A --no-headers 2>&1")
        lines     = [l for l in out.splitlines() if l.strip()]
        not_ready = [l for l in lines if "Running" not in l and "Completed" not in l]
        print(f"[poll_pods][{label}] Timeout after {max_wait}s — {len(not_ready)} pods not ready")
        print(out)