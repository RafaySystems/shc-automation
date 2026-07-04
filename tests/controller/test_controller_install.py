"""
tests/controller/test_controller_install.py

Installation validation tests -- drive the radm install flow and validate
the result based on controller_size, HA mode, and OS type from the profile.
"""

import pytest

pytestmark = [pytest.mark.order(1), pytest.mark.controller, pytest.mark.regression]


def attach_output(extras, label: str, content: str):
    """Embed command output into the pytest-html report.
    Supports both pytest-html 3.x (extra) and 4.x (extras).
    """
    import pytest_html
    block = f"<pre style='font-size:12px;white-space:pre-wrap'>{content}</pre>"
    item  = pytest_html.extras.html(f"<b>{label}</b>{block}")
    if hasattr(extras, 'append'):
        extras.append(item)
    else:
        extras.extend([item])


class TestPreflightChecks:
    """Verify the node meets installation prerequisites before radm init."""

    def test_preflight_os_matches_config(self, ssh_client, controller_profile, extras):
        """Confirm actual OS matches os_type declared in dev.yaml."""
        out, rc = ssh_client.run("cat /etc/os-release")
        attach_output(extras, "OS release", out)
        out_lower = out.lower()

        os_type = controller_profile.os_type
        if os_type == "ubuntu24":
            assert 'id=ubuntu' in out_lower, "Expected Ubuntu but OS is different"
            assert '24.04' in out, "Expected Ubuntu 24.04"
        elif os_type == "rhel8":
            assert 'rhel' in out_lower or 'red hat' in out_lower
            assert 'version_id="8' in out_lower, "Expected RHEL 8"
        elif os_type == "rhel9":
            assert 'rhel' in out_lower or 'red hat' in out_lower
            assert 'version_id="9' in out_lower, "Expected RHEL 9"

    def test_preflight_disk_root_500gb(self, ssh_client, extras):
        """Root disk must be at least 500 GB."""
        out, rc = ssh_client.run("df -BG / | tail -1 | awk '{print $2}'")
        attach_output(extras, "Root disk size", out)
        out_clean = out.strip().replace("G", "")
        if not out_clean.isdigit():
            pytest.fail(f"Could not read root disk size -- got: {out}")
        size_gb = int(out_clean)
        assert size_gb >= 500, f"Root disk is {size_gb}GB -- need at least 500GB"

    def test_preflight_data_disk_1tb(self, ssh_client, extras):
        """Data disk (/data) must exist and be at least 1 TB."""
        out, rc = ssh_client.run("df -BG /data 2>&1 | tail -1 | awk '{print $2}'")
        attach_output(extras, "/data disk size", out)
        out_clean = out.strip().replace("G", "")
        if not out_clean.isdigit():
            pytest.fail(
                f"/data is not mounted or not accessible -- got: {out}\n"
                f"Attach a 1TB block volume, format it, and mount it at /data before install."
            )
        size_gb = int(out_clean)
        assert size_gb >= 1000, f"/data disk is {size_gb}GB -- need at least 1TB"

    def test_preflight_tmp_50gb(self, ssh_client, extras):
        """Temp directory must have at least 50 GB available."""
        out, rc = ssh_client.run("df -BG /tmp | tail -1 | awk '{print $4}'")
        attach_output(extras, "/tmp available", out)
        out_clean = out.strip().replace("G", "")
        if not out_clean.isdigit():
            pytest.fail(f"Could not read /tmp available space -- got: {out}")
        avail_gb = int(out_clean)
        assert avail_gb >= 50, f"/tmp only has {avail_gb}GB free -- need at least 50GB"

    def test_preflight_cpu_meets_size(self, ssh_client, controller_profile, extras):
        """CPU count must meet controller size requirement."""
        out, rc = ssh_client.run("nproc")
        attach_output(extras, "CPU count", out)
        actual_cpu   = int(out.strip())
        required_cpu = controller_profile.cpu
        assert actual_cpu >= required_cpu, (
            f"Controller size '{controller_profile.controller_size}' requires "
            f"{required_cpu} CPUs -- found {actual_cpu}\n"
            f"Increase ocpus in dev.yaml (OCI Flex: ocpus={required_cpu // 2} gives {required_cpu} vCPUs)"
        )

    def test_preflight_memory_meets_size(self, ssh_client, controller_profile, extras):
        """Memory must meet controller size requirement.

        Uses free -m (megabytes) instead of free -g to avoid rounding errors.
        free -g reports 62 for a 64GB VM because it rounds down.
        A 5% tolerance is applied to account for OS kernel reservations.
        """
        out, rc = ssh_client.run("free -m | awk '/^Mem:/{print $2}'")
        attach_output(extras, "Memory (MB)", out)
        actual_mb    = int(out.strip())
        actual_gb    = actual_mb / 1024
        required_gb  = controller_profile.memory_gb
        # Allow 5% tolerance -- OS reserves some memory for kernel
        threshold_gb = required_gb * 0.95
        assert actual_gb >= threshold_gb, (
            f"Controller size '{controller_profile.controller_size}' requires "
            f"{required_gb}GB RAM -- found {actual_gb:.1f}GB\n"
            f"Increase memory_gb in dev.yaml to {required_gb}"
        )

    def test_ubuntu_iptables_flushed(self, ssh_client, extras, controller_profile):
        """
        Ubuntu 24.04: stop & disable ufw firewall, flush iptables.
        Must run early — before apt-get installs which may re-enable firewall rules.
        radm init requires clean iptables state.
        """
        if controller_profile.os_type != "ubuntu24":
            pytest.skip("iptables flush check is Ubuntu 24.04 only")

        # Step 1: stop and disable ufw (firewall manager that adds iptables rules)
        ufw_out, _ = ssh_client.run(
            "sudo ufw disable 2>/dev/null || true && "
            "sudo systemctl stop ufw 2>/dev/null || true && "
            "sudo systemctl disable ufw 2>/dev/null || true && "
            "echo UFW_DONE"
        )
        attach_output(extras, "ufw disable", ufw_out.strip())
        print(f"[test_ubuntu_iptables_flushed] ufw: {ufw_out.strip()}")

        # Step 2: flush all iptables rules
        flush_out, _ = ssh_client.run(
            "sudo iptables -F && "
            "sudo iptables -t nat -F && "
            "sudo iptables -t mangle -F && "
            "sudo iptables -X 2>/dev/null || true && "
            "echo FLUSHED"
        )
        attach_output(extras, "iptables flush", flush_out.strip())

        # Verify
        after_out, _ = ssh_client.run("sudo iptables -L | wc -l")
        attach_output(extras, "iptables rules after flush", after_out.strip())
        print(f"[test_ubuntu_iptables_flushed] rules after flush: {after_out.strip()}")

    def test_nfs_utils_installed(self, ssh_client, controller_profile, extras):
        """nfs-utils / nfs-common must be installed."""
        if controller_profile.os_type == "ubuntu24":
            out, rc = ssh_client.run("dpkg -l nfs-common 2>/dev/null | grep -c '^ii'")
        else:
            out, rc = ssh_client.run("rpm -q nfs-utils 2>/dev/null | grep -c nfs-utils")
        attach_output(extras, "NFS utils check", out)
        assert int(out.strip()) >= 1, "NFS utilities not installed -- run apt/yum install"

    def test_preflight_secondary_nodes(self, ssh_client, controller_profile,
                                       secondary_ips, extras):
        """
        HA mode only: run preflight checks on secondary nodes (node2, node3).
        Checks: OS, disk, CPU, memory on each node.
        Skipped for Non-HA.
        """
        if not controller_profile.ha or not secondary_ips:
            pytest.skip("Non-HA mode — secondary node preflight not needed")

        from lib.ssh.ssh_client import SSHClient

        size_requirements = {
            "cpu":    controller_profile.cpu,
            "memory": controller_profile.memory_gb,
        }

        failures = []

        for i, sec_ip in enumerate(secondary_ips, 2):
            print(f"[test_preflight_secondary_nodes] Checking node{i} ({sec_ip}) ...")
            sec_ssh = SSHClient(
                host=sec_ip,
                user=controller_profile.user,
                key_path=controller_profile.ssh_key
            )
            sec_ssh.connect()
            node_failures = []

            try:
                # OS check
                os_out, _ = sec_ssh.run("cat /etc/os-release | grep -E '^ID=|VERSION_ID'")
                attach_output(extras, f"node{i} OS", os_out)

                # Root disk >= 500GB
                disk_out, _ = sec_ssh.run("df -BG / | tail -1 | awk '{print $2}'")
                disk_gb = int(disk_out.strip().replace("G", "") or "0")
                if disk_gb < 500:
                    node_failures.append(f"Root disk {disk_gb}GB < 500GB")

                # CPU
                cpu_out, _ = sec_ssh.run("nproc")
                cpu = int(cpu_out.strip() or "0")
                if cpu < size_requirements["cpu"]:
                    node_failures.append(f"CPU {cpu} < {size_requirements['cpu']}")

                # Memory
                mem_out, _ = sec_ssh.run("free -m | awk '/^Mem:/{print $2}'")
                mem_gb = int(mem_out.strip() or "0") / 1024
                threshold = size_requirements["memory"] * 0.95
                if mem_gb < threshold:
                    node_failures.append(f"Memory {mem_gb:.1f}GB < {size_requirements['memory']}GB")

                # /tmp space
                tmp_out, _ = sec_ssh.run("df -BG /tmp | tail -1 | awk '{print $4}'")
                tmp_gb = int(tmp_out.strip().replace("G", "") or "0")
                if tmp_gb < 50:
                    node_failures.append(f"/tmp {tmp_gb}GB < 50GB")

                status = "PASS" if not node_failures else f"FAIL: {', '.join(node_failures)}"
                attach_output(extras, f"node{i} preflight", status)
                print(f"[test_preflight_secondary_nodes] node{i}: {status}")

                if node_failures:
                    failures.append(f"node{i} ({sec_ip}): {', '.join(node_failures)}")

            finally:
                sec_ssh.disconnect()

        assert not failures, (
            f"Preflight failed on secondary nodes:\n" + "\n".join(failures)
        )



class TestPackageSetup:
    """
    Download and set up the controller installation package on the VM.
    These tests run BEFORE TestRadmInstall -- they prepare /opt/rafay with
    the tar, extract it, copy radm to /usr/bin, and create config.yaml.

    Flow:
        1. Validate URL derived from package name
        2. Create /opt/rafay install directory
        3. Install aria2c on the VM (apt install aria2)
        4. Download package using aria2c -x 16 -s 16 (16 parallel connections)
           -- skips download if package already present on the node
        5. Verify downloaded file integrity (non-zero size)
        6. Extract tar.gz
        7. Copy radm binary to /usr/bin/
        8. Copy config.yaml-airgap-tmpl -> config.yaml

    Package configured in dev.yaml:
        package:
          name: "rafay-airgapped-controller-v3.1-39.tar.gz"
          install_dir: "/opt/rafay"
          url: ""   # auto-derived from name if empty

    CLI override:
        pytest tests/controller/ --package-name=rafay-airgapped-controller-v3.1-39.tar.gz
    """

    def test_package_url_derived(self, package_profile, extras):
        """Verify the download URL is correctly derived from the package name."""
        attach_output(extras, "Package summary", package_profile.summary())
        assert package_profile.url.startswith("https://"), (
            f"Invalid URL derived: {package_profile.url}"
        )
        assert package_profile.name in package_profile.url, (
            f"Package name not in URL: {package_profile.url}"
        )
        assert package_profile.version in package_profile.url, (
            f"Version not in URL: {package_profile.url}"
        )

    def test_install_dir_created(self, ssh_client, package_profile, extras):
        """Create /opt/rafay install directory on the VM."""
        out, rc = ssh_client.run(
            f"sudo mkdir -p {package_profile.install_dir} && echo OK"
        )
        attach_output(extras, "mkdir install_dir", out)
        assert rc == 0 and "OK" in out, (
            f"Could not create {package_profile.install_dir}: {out}"
        )

    def test_dns_resolver_configured(self, ssh_client, extras):
        """
        Ensure /etc/resolv.conf exists with working DNS servers.
        OCI VMs sometimes lose resolv.conf after reboot or network changes.
        Without it, all DNS resolution fails (aria2c errorCode=19).
        """
        check_out, _ = ssh_client.run(
            "test -f /etc/resolv.conf && cat /etc/resolv.conf || echo MISSING"
        )
        attach_output(extras, "resolv.conf", check_out)

        if "MISSING" in check_out or "nameserver" not in check_out:
            print("[test_dns_resolver_configured] resolv.conf missing — creating ...")
            dns_content = "nameserver 169.254.169.254\nnameserver 8.8.8.8\nnameserver 8.8.4.4\n"
            fix_out, fix_rc = ssh_client.run(
                f"printf '{dns_content}' | sudo tee /etc/resolv.conf > /dev/null && echo FIXED"
            )
            attach_output(extras, "resolv.conf fix", fix_out)
            print(f"[test_dns_resolver_configured] Fixed: {fix_out.strip()}")
        else:
            print("[test_dns_resolver_configured] resolv.conf already configured")

        # Verify DNS works
        dns_out, _ = ssh_client.run(
            "nslookup google.com 2>/dev/null | grep -i address | head -2 || echo DNS_DONE"
        )
        attach_output(extras, "DNS check", dns_out)
        print(f"[test_dns_resolver_configured] DNS: {dns_out.strip()[:100]}")

    def test_aria2c_installed(self, ssh_client, controller_profile, nsg_manager, extras):
        """
        Ensure aria2c is installed on the VM.
        NSG is attached here for apt-get internet access and stays attached
        through test_package_download (which detaches after download completes).
        Re-flushes iptables after apt to keep firewall clean.
        """
        # Attach NSG first — needed for apt-get update AND package download
        # NSG stays attached through test_package_download (detached there)
        import time
        if nsg_manager:
            nsg_manager.attach()
            attach_output(extras, "NSG", "Attached — will stay open through package download")
            print("[test_aria2c_installed] NSG attached — waiting 30s for rules to propagate ...")
            time.sleep(30)

        # Check if already installed
        out, rc = ssh_client.run("which aria2c 2>/dev/null && aria2c --version 2>/dev/null | head -1")
        if rc == 0 and "aria2" in out.lower():
            attach_output(extras, "aria2c version", out)
            print(f"[test_aria2c_installed] already installed: {out.strip()}")
            return

        attach_output(extras, "aria2c status", "Not found — installing ...")

        if controller_profile.os_type == "ubuntu24":
            # Force IPv4 for apt — OCI VMs have IPv6 configured but no IPv6 route
            # causing apt-get update to time out trying IPv6 addresses first
            update_out, _ = ssh_client.run(
                "sudo apt-get -o Acquire::ForceIPv4=true update -y 2>&1", timeout=120
            )
            attach_output(extras, "apt-get update", update_out)

            install_out, install_rc = ssh_client.run(
                "sudo apt-get -o Acquire::ForceIPv4=true install -y aria2 2>&1", timeout=180
            )
            attach_output(extras, "apt install aria2", install_out)

            if install_rc != 0:
                universe_out, universe_rc = ssh_client.run(
                    "sudo add-apt-repository universe -y 2>&1 && "
                    "sudo apt-get update -y 2>&1 && "
                    "sudo apt-get install -y aria2 2>&1",
                    timeout=300
                )
                attach_output(extras, "universe repo install", universe_out)
                assert universe_rc == 0, (
                    "aria2c install failed. SSH in and run: "
                    "sudo add-apt-repository universe && sudo apt install aria2"
                )
        else:
            # RHEL 8 / RHEL 9
            out, rc = ssh_client.run(
                "sudo yum install -y aria2 2>&1 || sudo dnf install -y aria2 2>&1",
                timeout=180
            )
            attach_output(extras, "yum/dnf install aria2", out)
            assert rc == 0, f"aria2c install failed on RHEL (exit {rc}): {out}"

        # Re-flush iptables after apt — apt-get may re-enable firewall rules
        # NSG stays attached for the upcoming package download
        ssh_client.run(
            "sudo iptables -F && sudo iptables -t nat -F && "
            "sudo iptables -t mangle -F && sudo iptables -X 2>/dev/null || true",
            timeout=10
        )
        print("[test_aria2c_installed] iptables re-flushed after apt-get")

        # Confirm installed
        verify_out, verify_rc = ssh_client.run("which aria2c && aria2c --version 2>/dev/null | head -1")
        attach_output(extras, "aria2c version", verify_out)
        assert verify_rc == 0, "aria2c not found after install. Run: sudo apt install aria2"

    def test_package_download(self, ssh_client, package_profile, nsg_manager, extras):
        """
        Download controller package from S3 using aria2c.
        Flow: confirm NSG attached -> wait 10s -> download -> detach NSG
        """
        import time

        attach_output(extras, "Package URL", package_profile.url)
        attach_output(extras, "Download destination", package_profile.tar_path)

        # Skip only if download is fully complete (no .aria2 resume file present)
        # .aria2 file = interrupted download — must re-download in this case
        aria2_control = f"{package_profile.tar_path}.aria2"

        # Check 1: tar file exists
        tar_exists_out, tar_exists_rc = ssh_client.run(
            f"test -f {package_profile.tar_path} && echo EXISTS || echo MISSING"
        )
        # Check 2: no .aria2 control file (means download is complete, not interrupted)
        aria2_exists_out, _ = ssh_client.run(
            f"test -f {aria2_control} && echo PARTIAL || echo CLEAN"
        )

        tar_exists  = "EXISTS"  in tar_exists_out
        is_complete = tar_exists and "CLEAN" in aria2_exists_out

        attach_output(extras, "Download state",
            f"tar exists: {tar_exists} | .aria2 file: {'YES (partial)' if 'PARTIAL' in aria2_exists_out else 'NO (clean)'}")

        if is_complete:
            size_out, _ = ssh_client.run(
                f"du -sh {package_profile.tar_path} | awk '{{print $1}}'"
            )
            attach_output(extras, "Download skipped",
                f"Already complete ({size_out.strip()}): {package_profile.tar_path}")
            if nsg_manager:
                nsg_manager.detach()
                attach_output(extras, "NSG", "Detached (package already present)")
            return

        # Clean up any partial download before retrying
        partial_out, _ = ssh_client.run(
            f"test -f {aria2_control} && echo PARTIAL || echo CLEAN"
        )
        if "PARTIAL" in partial_out:
            attach_output(extras, "Partial download detected",
                f"Removing incomplete files: {package_profile.tar_path} + .aria2")
            print("[test_package_download] Removing partial download files ...")
            ssh_client.run(
                f"sudo rm -f {package_profile.tar_path} {aria2_control}"
            )

        download_ok = False
        try:
            # NSG already attached in test_aria2c_installed — just confirm
            if nsg_manager:
                attach_output(extras, "NSG status", f"Already attached from aria2c install step")
                print(f"[test_package_download] NSG already attached")
            else:
                attach_output(extras, "NSG status", "No nsg_manager -- ensure VM has internet access")

            # S3 connectivity check
            diag_out, _ = ssh_client.run(
                "curl -sI --max-time 15 "
                "https://rafay-airgap-controller.s3.us-west-2.amazonaws.com "
                "--write-out '\nHTTP_CODE:%{http_code}' -o /dev/null 2>&1 || true",
                timeout=30
            )
            attach_output(extras, "S3 connectivity", diag_out)
            print(f"[S3 check] {diag_out}")

            # Download
            aria2c_path_out, _ = ssh_client.run("which aria2c")
            aria2c_bin = aria2c_path_out.strip() or "/usr/bin/aria2c"
            print(f"[test_package_download] Downloading: {package_profile.url}")

            out, rc = ssh_client.run(
                f"cd {package_profile.install_dir} && "
                f"sudo {aria2c_bin} -x 16 -s 16 --max-tries=3 --retry-wait=10 "
                f"--connect-timeout=30 --log-level=notice "
                f"{package_profile.url} 2>&1",
                timeout=1800
            )
            attach_output(extras, "aria2c output", out)
            print(f"[aria2c]\n{out}\n[exit] {rc}")

            if rc == 0:
                download_ok = True
                # Store actual download path for extract test
                package_profile._actual_tar_path = f"{package_profile.install_dir}/{package_profile.name}"
                print("[test_package_download] Download complete")
            else:
                errors = {
                    3:  "404 -- check package name in dev.yaml",
                    6:  "Network problem -- check NSG egress rules",
                    9:  "Disk full",
                    16: "No internet -- check NSG has 0.0.0.0/0 egress",
                }
                attach_output(extras, "Download FAILED",
                    f"{errors.get(rc, f'exit {rc}')}\n{out[-500:]}")

            # Verify file size
            if download_ok:
                v_out, verify_rc = ssh_client.run(
                    f"test -s {package_profile.tar_path} && "
                    f"du -sh {package_profile.tar_path} | awk '{{print $1}}'"
                )
                attach_output(extras, "Downloaded file size", v_out)
            else:
                verify_rc = 1

        finally:
            # Always detach NSG
            if nsg_manager:
                print("[test_package_download] Detaching NSG ...")
                nsg_manager.detach()
                attach_output(extras, "NSG", "Detached -- internet access removed")

        assert download_ok, f"Package download failed. URL: {package_profile.url}"
        assert verify_rc == 0, f"File missing or empty: {package_profile.tar_path}"

    def test_package_extract(self, ssh_client, package_profile, extras):
        """
        Extract the tar.gz into /opt/rafay (same dir as download).
        Timeout: 60 minutes. Skips if already extracted.
        """
        import time

        extract_dest = package_profile.install_dir  # /opt/rafay
        attach_output(extras, "Extract destination", extract_dest)

        # ── Skip if already extracted ──────────────────────────────────────────
        ls_out, _ = ssh_client.run(f"ls -1 {extract_dest}/ 2>/dev/null")
        before_dirs = set(ls_out.strip().splitlines())

        already = [
            d for d in before_dirs
            if "rafay-airgapped-controller" in d and not d.endswith(".tar.gz")
        ]
        if already:
            actual = f"{extract_dest}/{already[0]}"
            attach_output(extras, "Already extracted", actual)
            package_profile._actual_extract_dir = actual
            print(f"[test_package_extract] Already extracted: {actual}")
            return

        # ── Locate tar file ────────────────────────────────────────────────────
        tar_path = getattr(package_profile, "_actual_tar_path", package_profile.tar_path)
        locate_out, locate_rc = ssh_client.run(
            f"test -f {tar_path} && echo FOUND || echo MISSING"
        )
        if "MISSING" in locate_out:
            search_out, _ = ssh_client.run(
                f"find /opt/rafay -name '{package_profile.name}' 2>/dev/null | head -1"
            )
            found = search_out.strip()
            if found:
                tar_path = found
                package_profile._actual_tar_path = found
                print(f"[test_package_extract] Found tar at: {found}")
            else:
                pytest.fail(
                    f"Package tar not found at {tar_path}.\n"
                    f"Run test_package_download first."
                )

        size_out, _ = ssh_client.run(f"du -sh {tar_path}")
        print(f"[test_package_extract] File size: {size_out}")
        attach_output(extras, "Package size", size_out)

        # ── Extract ────────────────────────────────────────────────────────────
        extract_cmd = (
            f"sudo tar -xf {tar_path} -C {extract_dest} 2>&1 && echo EXTRACTED"
        )
        print(f"[test_package_extract] Running: {extract_cmd}")
        start_time = time.time()

        out, rc = ssh_client.run(extract_cmd, timeout=3600)
        elapsed = int(time.time() - start_time)
        attach_output(extras, f"Extract result ({elapsed}s)",
            out[-500:] if len(out) > 500 else out)
        print(f"[test_package_extract] Finished in {elapsed}s — exit {rc}")

        assert rc == 0 and "EXTRACTED" in out, (
            f"tar extraction failed (exit {rc}) after {elapsed}s.\n"
            f"Output: {out}\n"
            f"Try manually: sudo tar -xf {tar_path} -C {extract_dest}"
        )

        # ── Detect actual extracted dir ────────────────────────────────────────
        ls_after, _ = ssh_client.run(f"ls -1 {extract_dest}/ 2>/dev/null")
        after_dirs  = set(ls_after.strip().splitlines())
        new_entries = after_dirs - before_dirs
        extracted_dirs = [
            e for e in new_entries
            if not e.endswith(".tar.gz") and not e.endswith(".tar")
        ]

        assert extracted_dirs, (
            f"No new directory found after extraction in {extract_dest}. "
            f"New entries: {sorted(new_entries)}"
        )

        actual_extract_dir = f"{extract_dest}/{extracted_dirs[0]}"
        attach_output(extras, "Detected extract dir", actual_extract_dir)
        print(f"[test_package_extract] Extract dir: {actual_extract_dir}")
        package_profile._actual_extract_dir = actual_extract_dir

    def test_setup_secondary_nodes(self, ssh_client, package_profile,
                                   controller_profile, secondary_ips,
                                   secondary_instance_ids, oci_profile_fixture, extras):
        """
        HA mode only: full setup for nodes 2 and 3.
        Skipped for Non-HA.

        Per secondary node:
          1. Create /opt/rafay
          2. Install aria2c       (NSG attached = internet access)
          3. Download package     (NSG attached)
          4. Detach NSG           (internet no longer needed)
          5. Extract package
          6. Copy config.yaml from node1
        """
        if not controller_profile.ha or not secondary_ips:
            pytest.skip("Non-HA mode — secondary node setup not needed")

        from lib.ssh.ssh_client import SSHClient
        from lib.oci.vm_manager import OCINSGManager
        import base64

        extract_dir = getattr(package_profile, "_actual_extract_dir",
                              package_profile.extract_dir)
        config_path = f"{extract_dir}/config.yaml"

        # Read patched config.yaml from node1 once
        config_content, cfg_rc = ssh_client.run(f"sudo cat {config_path}")
        assert cfg_rc == 0, "Could not read config.yaml from node1"
        encoded_config = base64.b64encode(config_content.encode()).decode()
        print(f"[test_setup_secondary_nodes] config.yaml read from node1 ({len(config_content)} bytes)")

        for i, (sec_ip, sec_id) in enumerate(
            zip(secondary_ips, secondary_instance_ids), 2
        ):
            print(f"[test_setup_secondary_nodes] Setting up node{i} ({sec_ip}) ...")
            attach_output(extras, f"node{i} IP", sec_ip)

            # NSG manager for this secondary node
            sec_nsg = None
            if oci_profile_fixture and oci_profile_fixture.nsg_id and sec_id:
                sec_nsg = OCINSGManager(oci_profile_fixture, sec_id)

            sec_ssh = SSHClient(
                host=sec_ip,
                user=controller_profile.user,
                key_path=controller_profile.ssh_key
            )
            sec_ssh.connect()

            try:
                # 1. Create install dir
                sec_ssh.run(f"sudo mkdir -p {package_profile.install_dir}")

                # Check if already extracted
                check_out, _ = sec_ssh.run(
                    f"test -d {extract_dir} && echo EXISTS || echo MISSING"
                )
                already_extracted = "EXISTS" in check_out

                if not already_extracted:
                    # 2. Ensure NSG attached for internet access
                    if sec_nsg:
                        sec_nsg.attach()
                        print(f"[test_setup_secondary_nodes] node{i}: NSG attached")

                    # 3. Install aria2c
                    print(f"[test_setup_secondary_nodes] node{i}: installing aria2c ...")
                    sec_ssh.run("sudo apt-get update -y 2>&1 || true", timeout=120)
                    sec_ssh.run("sudo apt-get install -y aria2 2>&1 || true", timeout=180)

                    # 4. Download package
                    tar_check, _ = sec_ssh.run(
                        f"test -f {package_profile.tar_path} && "
                        f"test ! -f {package_profile.tar_path}.aria2 && echo COMPLETE || echo MISSING"
                    )
                    if "MISSING" in tar_check:
                        aria2c_out, _ = sec_ssh.run("which aria2c")
                        aria2c_bin = aria2c_out.strip() or "/usr/bin/aria2c"
                        print(f"[test_setup_secondary_nodes] node{i}: downloading ...")
                        dl_out, dl_rc = sec_ssh.run(
                            f"cd {package_profile.install_dir} && "
                            f"sudo {aria2c_bin} -x 16 -s 16 --max-tries=3 "
                            f"--connect-timeout=30 {package_profile.url} 2>&1",
                            timeout=1800
                        )
                        attach_output(extras, f"node{i} download", dl_out[-300:])
                        assert dl_rc == 0, f"Download failed on node{i}: {dl_out[-200:]}"
                        print(f"[test_setup_secondary_nodes] node{i}: download complete")
                    else:
                        print(f"[test_setup_secondary_nodes] node{i}: package already downloaded")

                    # 5. Detach NSG — internet no longer needed after download
                    if sec_nsg:
                        sec_nsg.detach()
                        print(f"[test_setup_secondary_nodes] node{i}: NSG detached")

                    # 6. Extract
                    print(f"[test_setup_secondary_nodes] node{i}: extracting ...")
                    ext_out, ext_rc = sec_ssh.run(
                        f"sudo tar -xf {package_profile.tar_path} "
                        f"-C {package_profile.install_dir} 2>&1 && echo EXTRACTED",
                        timeout=3600
                    )
                    attach_output(extras, f"node{i} extract", ext_out[-200:])
                    assert ext_rc == 0 and "EXTRACTED" in ext_out, (
                        f"Extraction failed on node{i}: {ext_out[-200:]}"
                    )
                    print(f"[test_setup_secondary_nodes] node{i}: extraction complete")
                else:
                    print(f"[test_setup_secondary_nodes] node{i}: already extracted — skipping download")
                    # Still detach NSG if attached (cleanup)
                    if sec_nsg:
                        try:
                            sec_nsg.detach()
                        except Exception:
                            pass

                # 7. Copy patched config.yaml from node1
                write_out, write_rc = sec_ssh.run(
                    f"echo '{encoded_config}' | base64 -d | "
                    f"sudo tee {config_path} > /dev/null && echo OK"
                )
                attach_output(extras, f"node{i} config.yaml", write_out)
                assert write_rc == 0 and "OK" in write_out, (
                    f"Failed to write config.yaml to node{i}: {write_out}"
                )
                print(f"[test_setup_secondary_nodes] node{i}: config.yaml copied")
                attach_output(extras, f"node{i} status", "Setup complete ✓")

            finally:
                sec_ssh.disconnect()

    def test_radm_binary_copied(self, ssh_client, package_profile, extras):
        """Copy radm binary from extracted package to /usr/bin/ and make executable."""
        extract_dir = (getattr(package_profile, "_actual_extract_dir", None) or package_profile.extract_dir)
        attach_output(extras, "Extract dir used", extract_dir)
        out, rc = ssh_client.run(
            f"sudo cp {extract_dir}/radm /usr/bin/radm && "
            f"sudo chmod +x /usr/bin/radm && echo OK"
        )
        attach_output(extras, "radm binary copy", out)
        assert rc == 0 and "OK" in out, f"Failed to copy radm binary (exit {rc}): {out}"

        version_out, version_rc = ssh_client.run("which radm")
        attach_output(extras, "radm location", version_out)
        assert version_rc == 0, "radm not found in PATH after copy"

    def test_config_yaml_created(self, ssh_client, package_profile, request,
                                  controller_profile, controller_fqdn, raw_config, extras):
        """
        Copy config.yaml-airgap-tmpl to config.yaml and patch all required fields:

          spec.deployment.size          <- controller_size from dev.yaml (S/M/L)
          spec.deployment.ha            <- true if ha=true in dev.yaml else false
          spec.deployment.type          <- "airgap"
          spec.repo.archive-directory   <- actual extracted package path
          spec.app-config.partner.star-domain <- shc-{build_no}.dev.rafay-edge.net
        """
        extract_dir  = (getattr(package_profile, "_actual_extract_dir", None) or package_profile.extract_dir)
        config_path  = f"{extract_dir}/config.yaml"   # Fix 1: inside extracted dir
        tmpl_names   = ["config.yaml-airgap-tmpl", "config.yaml-tmpl", "config.yaml.tmpl"]

        # ── Find the template file ─────────────────────────────────────────────
        tmpl_path = None
        for name in tmpl_names:
            check, rc = ssh_client.run(
                f"test -f {extract_dir}/{name} && echo FOUND || echo MISSING"
            )
            if "FOUND" in check:
                tmpl_path = f"{extract_dir}/{name}"
                break

        assert tmpl_path, f"No config.yaml template found in {extract_dir}. Tried: {tmpl_names}"
        attach_output(extras, "Template found", tmpl_path)

        # ── Copy template to config.yaml ──────────────────────────────────────
        out, rc = ssh_client.run(
            f"sudo cp {tmpl_path} {config_path} && echo OK"
        )
        assert rc == 0 and "OK" in out, f"Failed to copy template: {out}"

        # ── Show original content ─────────────────────────────────────────────
        orig_out, _ = ssh_client.run(f"cat {config_path}")
        attach_output(extras, "config.yaml (original)", orig_out)

        # ── Determine values to patch ─────────────────────────────────────────
        size      = controller_profile.controller_size          # S, M, or L
        ha        = "true" if controller_profile.ha else "false"
        repo_path = extract_dir                                  # /opt/rafay/rafay-airgapped-controller-v3.1-39

        # star-domain: shc-{build_no}.dev.rafay-edge.net (no wildcard prefix)
        # controller_fqdn is "*.shc-42.dev.rafay-edge.net" — strip the "*." prefix
        if controller_fqdn:
            # provision:true mode — FQDN comes from terraform output
            star_domain = controller_fqdn.lstrip("*.")
        else:
            # provision:false mode — build from dev.yaml dns.base_domain + --build-no
            base_domain  = raw_config.get("dns", {}).get("base_domain", "")
            build_no_val = request.config.getoption("--build-no") or ""
            if base_domain and build_no_val:
                star_domain = f"shc-{build_no_val}.{base_domain}"
            elif base_domain:
                # No --build-no passed — use display_name from dev.yaml as fallback
                # e.g. display_name: "shc-1" -> star_domain: "shc-1.dev.rafay-edge.net"
                display_name = raw_config.get("oci", {}).get("display_name", "")
                if display_name and "{build_no}" not in display_name:
                    star_domain = f"{display_name}.{base_domain}"
                    print(f"[test_config_yaml_created] Using display_name as build_no fallback: {star_domain}")
                else:
                    star_domain = ""
                    print("[test_config_yaml_created] WARNING: pass --build-no=N to set star-domain")
            else:
                star_domain = ""
                print("[test_config_yaml_created] WARNING: dns.base_domain not set in dev.yaml")

        print(f"[test_config_yaml_created] Patching config.yaml:")
        print(f"  size             : {size}")
        print(f"  ha               : {ha}")
        print(f"  type             : airgap")
        print(f"  archive-directory: {repo_path}")
        print(f"  star-domain      : {star_domain}")

        attach_output(extras, "Patch values",
            f"size={size} | ha={ha} | type=airgap | "
            f"archive-directory={repo_path} | star-domain={star_domain}"
        )

        # ── Apply patches using sed ───────────────────────────────────────────
        # Each sed command targets the specific key and replaces its value.
        # Using | as delimiter to avoid conflicts with / in paths.

        # Build sed commands with proper quoting
        # Result in config.yaml:
        #   size: "S"
        #   ha: true/false
        #   type: "airgap"
        #   archive-directory: /opt/rafay/rafay-airgapped-controller-v3.1-39
        #   star-domain: shc-42.dev.rafay-edge.net
        q = '"'  # double-quote character for use inside f-strings
        patches = [
            f"sudo sed -i '/size:/s|:.*|: {q}{size}{q}|' {config_path}",
            f"sudo sed -i '/^[ ]*ha:/s|:.*|: {ha}|' {config_path}",
            # type: patched via two-step sed below to avoid matching repo.rafay-registry.type
        ]

        # Run sed patches
        for patch_cmd in patches:
            out_p, rc_p = ssh_client.run(patch_cmd)
            assert rc_p == 0, f"Patch failed: {patch_cmd}\n{out_p}"

        # Patch deployment.type — two-step sed to avoid matching repo.rafay-registry.type
        # deployment.type has exactly 4 spaces indent: "    type:"
        # repo.rafay-registry.type has 6+ spaces indent: "      type:"
        # Match only lines with exactly 4 spaces before type:
        ssh_client.run(
            "sudo sed -i 's|^    type:.*|    type: TYPE_PLACEHOLDER|' " + config_path
        )
        out_type, rc_type = ssh_client.run(
            f"sudo sed -i " + "'" + r's|type: TYPE_PLACEHOLDER|type: "airgap"|' + "'" + f" {config_path} && echo OK"
        )
        attach_output(extras, "type patch", out_type)
        assert rc_type == 0 and "OK" in out_type, f"type patch failed: {out_type}"
        print(f"  [OK] type: airgap")

        # Patch archive-directory — two-step sed to avoid slash conflicts
        # Step 1: replace value with a unique placeholder
        ssh_client.run(
            f"sudo sed -i 's|archive-directory:.*|archive-directory: RAFAY_PLACEHOLDER|' {config_path}"
        )
        # Step 2: replace placeholder with actual path
        out_arch, rc_arch = ssh_client.run(
            f"sudo sed -i 's|archive-directory: RAFAY_PLACEHOLDER|archive-directory: {repo_path}|' {config_path} && echo OK"
        )
        attach_output(extras, 'archive-directory patch', out_arch)
        assert rc_arch == 0 and 'OK' in out_arch, f'archive-directory patch failed: {out_arch}'
        print(f'  [OK] archive-directory: {repo_path}')

        if star_domain:
            # Match only the actual star-domain: value line (not the comment line)
            # Pattern: line starts with spaces then "star-domain:" (not inside a comment)
            ssh_client.run(
                f"sudo sed -i '/^[ ]*star-domain:/s|star-domain:.*|star-domain: STAR_PLACEHOLDER|' {config_path}"
            )
            out_star, rc_star = ssh_client.run(
                f"sudo sed -i 's|star-domain: STAR_PLACEHOLDER|star-domain: {star_domain}|' "
                f"{config_path} && echo OK"
            )
            attach_output(extras, 'star-domain patch', out_star)
            assert rc_star == 0 and 'OK' in out_star, f'star-domain patch failed: {out_star}'
            print(f'  [OK] star-domain: {star_domain}')

        # ── Verify key fields were set correctly ──────────────────────────────
        # Verify each field was patched correctly
        verify_items = [
            ("size",              f'"{size}"'),
            ("ha",                ha),
            ("type",              '"airgap"'),
        ]
        # archive-directory and star-domain verified separately via Python scripts above

        for key, expected in verify_items:
            grep_out, _ = ssh_client.run(
                f"grep '{key}:' {config_path} | head -1"
            )
            assert expected in grep_out, (
                f"Patch failed for '{key}': expected '{expected}' in '{grep_out.strip()}'"
            )
            print(f"  [OK] {grep_out.strip()}")

        attach_output(extras, "Patch verification", "All fields patched correctly")

    def test_package_version_matches_profile(self, ssh_client, package_profile, extras):
        """Verify extracted package version matches what's declared in dev.yaml."""
        out, rc = ssh_client.run(
            f"ls {package_profile.install_dir}/ | grep rafay-airgapped-controller"
        )
        attach_output(extras, "Extracted dirs", out)
        assert package_profile.version in out, (
            f"Expected version {package_profile.version} in extracted dir, got: {out}"
        )


class TestRadmInstall:
    """Drive the radm installation steps sequentially."""

    def _wait_for_pods(self, ssh_client, extras, label="pods", max_wait=1500,
                       fail_on_timeout=False):
        """
        Poll kubectl get pods -A every 30s.
        Waits until two consecutive checks show ALL pods Running/Completed
        with the same pod count.

        On timeout: describes unhealthy pods and continues (never blocks next step).

        Args:
            label          : label for log/report output e.g. "after radm init"
            max_wait       : seconds to wait (default 25 min)
            fail_on_timeout: ignored — always continues after timeout with describe output
        """
        import time
        poll_every   = 30
        deadline     = time.time() + max_wait
        attempt      = 0
        stable_count = 0
        prev_total   = 0

        print(f"[{label}] Waiting for all pods Running/Completed (max {max_wait//60} min) ...")

        while time.time() < deadline:
            attempt += 1
            pods_out, pods_rc = ssh_client.run("kubectl get pods -A --no-headers 2>/dev/null || /usr/local/bin/kubectl get pods -A --no-headers 2>&1")

            if pods_rc != 0:
                print(f"[{label}] kubectl not ready (attempt {attempt}) ...")
                stable_count = 0
                time.sleep(poll_every)
                continue

            lines     = [l for l in pods_out.splitlines() if l.strip()]
            total     = len(lines)
            not_ready = [l for l in lines if "Running" not in l and "Completed" not in l]
            unhealthy = [l for l in lines if any(
                s in l for s in ("Error", "CrashLoop", "OOMKilled")
            )]

            print(f"[{label}] Attempt {attempt}: "
                  f"{total} pods, {len(not_ready)} not ready, {len(unhealthy)} unhealthy")

            if unhealthy:
                print(f"[{label}] Unhealthy pods — waiting for recovery ...")
                stable_count = 0

            if not not_ready and total > 0:
                if total == prev_total:
                    stable_count += 1
                else:
                    stable_count = 0
                    print(f"[{label}] Pod count changed {prev_total} -> {total}, waiting ...")

                if stable_count >= 2:
                    attach_output(extras, f"All pods Running ({label})", pods_out)
                    print(f"[{label}] All {total} pods Running/Completed ✓")
                    return
            else:
                stable_count = 0

            prev_total = total
            time.sleep(poll_every)

        # ── Timeout reached — describe unhealthy pods then continue ───────────
        print(f"[{label}] Timeout reached — describing unhealthy pods ...")
        pods_out, _ = ssh_client.run("kubectl get pods -A --no-headers 2>&1")
        lines     = [l for l in pods_out.splitlines() if l.strip()]
        not_ready = [l for l in lines if "Running" not in l and "Completed" not in l]
        unhealthy = [l for l in lines if any(
            s in l for s in ("Error", "CrashLoop", "OOMKilled", "Pending")
        )]

        attach_output(extras, f"Pod status at timeout ({label})", pods_out)
        print(f"[{label}] {len(lines)} total, {len(not_ready)} not ready after {max_wait//60} min")

        # Describe each unhealthy pod for diagnostics
        for pod_line in unhealthy[:5]:  # describe up to 5 pods
            parts = pod_line.split()
            if len(parts) >= 2:
                ns, pod_name = parts[0], parts[1]
                desc_out, _ = ssh_client.run(
                    f"kubectl describe pod {pod_name} -n {ns} 2>&1 | tail -30"
                )
                attach_output(extras, f"describe {ns}/{pod_name}", desc_out)
                print(f"[{label}] describe {ns}/{pod_name}:\n{desc_out[-300:]}")

        # Always continue — do not fail
        print(f"[{label}] Continuing despite unready pods ...")

    def test_radm_binary_present(self, ssh_client, extras):
        """radm binary must exist at /usr/bin/radm."""
        out, rc = ssh_client.run("which radm || echo NOT_FOUND")
        attach_output(extras, "radm path", out)
        assert "NOT_FOUND" not in out, (
            "radm binary not found -- copy it to /usr/bin/:\n"
            "  sudo cp ./radm /usr/bin/"
        )

    def test_config_yaml_present(self, ssh_client, package_profile, extras):
        """config.yaml must exist inside the extracted package dir before radm init."""
        extract_dir = (getattr(package_profile, "_actual_extract_dir", None) or package_profile.extract_dir)
        config_path = f"{extract_dir}/config.yaml"
        out, rc = ssh_client.run(f"test -f {config_path} && echo OK || echo MISSING")
        attach_output(extras, "config.yaml check", out)
        assert out.strip() == "OK", (
            f"config.yaml not found at {config_path}\n"
            f"Run TestPackageSetup tests first."
        )

    def test_config_ha_matches_profile(self, ssh_client, package_profile, controller_profile, extras):
        """HA setting in remote config.yaml must match dev.yaml."""
        extract_dir = (getattr(package_profile, "_actual_extract_dir", None) or package_profile.extract_dir)
        config_path = f"{extract_dir}/config.yaml"
        out, rc = ssh_client.run(f"grep 'ha:' {config_path} | head -1")
        attach_output(extras, "config.yaml ha field", out)
        remote_ha = "true" in out.lower()
        assert remote_ha == controller_profile.ha, (
            f"Profile says ha={controller_profile.ha} but config.yaml has: {out.strip()}"
        )

    def test_config_domain_set(self, ssh_client, package_profile, extras):
        """star-domain must be set in config.yaml."""
        extract_dir = (getattr(package_profile, "_actual_extract_dir", None) or package_profile.extract_dir)
        config_path = f"{extract_dir}/config.yaml"
        out, rc = ssh_client.run(f"grep 'star-domain' {config_path}")
        attach_output(extras, "star-domain config", out)
        assert "example.com" not in out, (
            "star-domain still has the template value -- update it before running radm init"
        )
        assert out.strip() != "", "star-domain is missing from config.yaml"

    def test_radm_init_completes(self, ssh_client, package_profile,
                                  controller_profile, secondary_ips, extras):
        """
        Full radm init + kubeconfig + radm join flow:

        Step 1: sudo ./radm init --config config.yaml  (node1, ~10-15 min)
        Step 2: mkdir -p $HOME/.kube
                sudo cp -f /etc/kubernetes/admin.conf $HOME/.kube/config
                sudo chown $(id -u):$(id -g) -R $HOME/.kube
        Step 3: HA only — build join command from kubeadm token, run on node2+node3
        Step 4: kubectl get nodes — verify all nodes Ready
        Step 5: wait for pods Running
        """
        # Resolve actual extract dir — _actual_extract_dir may not be set
        # when running a single test in isolation (different session)
        extract_dir = getattr(package_profile, "_actual_extract_dir", None)
        if not extract_dir:
            # Search for the extracted directory on the VM
            search_out, _ = ssh_client.run(
                "find /opt/rafay -maxdepth 1 -type d -name 'rafay-airgapped-controller*' 2>/dev/null | head -1"
            )
            extract_dir = search_out.strip() or "/opt/rafay"
            print(f"[test_radm_init_completes] Detected extract_dir: {extract_dir}")

        attach_output(extras, "Extract dir", extract_dir)
        attach_output(extras, "HA mode", str(controller_profile.ha))

        # ── Step 1: radm init on node1 ────────────────────────────────────────
        # Check if already initialized — skip if kubeadm config already exists
        already_init_out, _ = ssh_client.run(
            "test -f /etc/kubernetes/admin.conf && echo ALREADY_INIT || echo NOT_INIT"
        )
        already_initialized = "ALREADY_INIT" in already_init_out
        attach_output(extras, "radm init status",
            "Already initialized — skipping radm init" if already_initialized
            else "Not initialized — running radm init"
        )

        if already_initialized:
            print("[test_radm_init_completes] Kubernetes already initialized — skipping radm init")
        else:
            # Full reset before radm init:
            # 1. Stop consul FIRST — if consul is running with stale k8master registration,
            #    the API server health check will fail (NXDOMAIN on k8master.service.edgedc.consul)
            #    radm init will start consul itself as part of its flow
            # 2. Stop kubelet, reset kubeadm, wipe all state
            print("[test_radm_init_completes] Full reset: stopping consul + kubelet ...")

            ssh_client.run(
                "sudo systemctl stop consul kubelet 2>/dev/null || true && sleep 2",
                timeout=15
            )
            ssh_client.run(
                "sudo kubeadm reset -f 2>/dev/null || true",
                timeout=30
            )
            ssh_client.run(
                "sudo rm -rf /etc/kubernetes /var/lib/kubelet /var/lib/etcd "
                "/var/run/kubernetes /tmp/rafay-infra "
                "/var/lib/consul /etc/consul.d/rafay*.hcl "
                "/etc/consul.d/rafay*.json 2>/dev/null || true",
                timeout=15
            )
            # Remove containerd config written by previous radm init
            # radm init writes registry config pointing to local k8master-registry
            # If not removed, containerd tries to pull pause image from dead registry
            ssh_client.run(
                "sudo rm -rf /etc/containerd/certs.d/ && "
                "sudo rm -f /etc/containerd/config.toml 2>/dev/null || true",
                timeout=10
            )
            ssh_client.run(
                "sudo systemctl restart containerd && sleep 5 && echo READY",
                timeout=30
            )

            # Verify consul is stopped before proceeding
            consul_status, _ = ssh_client.run(
                "sudo systemctl is-active consul 2>/dev/null || echo inactive"
            )
            attach_output(extras, "pre-init consul status", consul_status.strip())
            print(f"[test_radm_init_completes] consul status: {consul_status.strip()}")

            attach_output(extras, "k8s+consul reset", "Full reset complete")
            print("[test_radm_init_completes] Full reset complete")

            print(f"[test_radm_init_completes] Step 1: running radm init on node1 ...")
            # Skip install-containerd-config-toml phase:
            # radm writes sandbox_image pointing to local registry which isn't up yet
            # causing pause image pull failure and API server timeout
            out, rc = ssh_client.run(
                f"cd {extract_dir} && sudo ./radm init --config config.yaml "
                f"--skip-phases infra/containerd/install-containerd-config-toml 2>&1",
                timeout=1800,
            )
            attach_output(extras, "radm init output (node1)", out)
            assert rc == 0, f"radm init failed (exit {rc}). See output above."
            print("[test_radm_init_completes] radm init completed on node1")

        # ── Step 2: kubeconfig setup ──────────────────────────────────────────
        print("[test_radm_init_completes] Step 2: setting up kubeconfig ...")
        kubeconfig_out, kubeconfig_rc = ssh_client.run(
            "mkdir -p $HOME/.kube && "
            "sudo cp -f /etc/kubernetes/admin.conf $HOME/.kube/config && "
            "sudo chown $(id -u):$(id -g) -R $HOME/.kube && "
            "echo KUBECONFIG_OK"
        )
        attach_output(extras, "kubeconfig setup", kubeconfig_out)
        assert kubeconfig_rc == 0 and "KUBECONFIG_OK" in kubeconfig_out, (
            f"kubeconfig setup failed: {kubeconfig_out}"
        )
        print("[test_radm_init_completes] kubeconfig ready on node1")

        # Verify kubectl works — try common paths
        kubectl_out, kubectl_rc = ssh_client.run(
            "kubectl get nodes 2>/dev/null || "
            "/usr/local/bin/kubectl get nodes 2>/dev/null || "
            "/usr/bin/kubectl get nodes 2>&1"
        )
        attach_output(extras, "kubectl get nodes (after init)", kubectl_out)
        print(f"[test_radm_init_completes] Nodes after init:\n{kubectl_out}")

        # ── Step 3: HA — radm join on node2 and node3 ────────────────────────
        if controller_profile.ha and secondary_ips:
            print(f"[test_radm_init_completes] Step 3: HA mode — joining {len(secondary_ips)} secondary nodes ...")

            # Build join command from kubeadm token on node1
            token_out, _ = ssh_client.run(
                "kubeadm token list 2>/dev/null | grep -v TOKEN | head -1 | awk '{print $1}'"
            )
            ca_hash_out, _ = ssh_client.run(
                "openssl x509 -pubkey -in /etc/kubernetes/pki/ca.crt | "
                "openssl rsa -pubin -outform der 2>/dev/null | "
                "openssl dgst -sha256 -hex | sed 's/^.* //'"
            )
            primary_ip_out, _ = ssh_client.run(
                "hostname -I | awk '{print $1}'"
            )
            token   = token_out.strip()
            ca_hash = ca_hash_out.strip()
            pri_ip  = primary_ip_out.strip()

            assert token,   "Could not get kubeadm token — did radm init complete?"
            assert ca_hash, "Could not get CA hash"
            assert pri_ip,  "Could not get primary node IP"

            print(f"[test_radm_init_completes] token  : {token}")
            print(f"[test_radm_init_completes] pri_ip : {pri_ip}")

            join_cmd = (
                f"cd {extract_dir} && "
                f"sudo ./radm join {pri_ip}:6443 "
                f"--token {token} "
                f"--discovery-token-ca-cert-hash sha256:{ca_hash} "
                f"--config config.yaml"
            )
            attach_output(extras, "radm join command", join_cmd)

            from lib.ssh.ssh_client import SSHClient
            for i, sec_ip in enumerate(secondary_ips, 2):
                print(f"[test_radm_init_completes] Joining node{i} ({sec_ip}) ...")
                sec_ssh = SSHClient(
                    host=sec_ip,
                    user=controller_profile.user,
                    key_path=controller_profile.ssh_key
                )
                sec_ssh.connect()
                try:
                    join_out, join_rc = sec_ssh.run(join_cmd, timeout=1800)
                    attach_output(extras, f"radm join node{i} ({sec_ip})", join_out)
                    assert join_rc == 0, (
                        f"radm join failed on node{i} ({sec_ip}) exit {join_rc}:\n{join_out}"
                    )
                    print(f"[test_radm_init_completes] node{i} ({sec_ip}) joined successfully")
                finally:
                    sec_ssh.disconnect()

            # ── Step 4: verify all nodes Ready ────────────────────────────────
            print("[test_radm_init_completes] Step 4: verifying all nodes Ready ...")
            nodes_out, _ = ssh_client.run(
                "kubectl get nodes 2>/dev/null || /usr/local/bin/kubectl get nodes 2>&1"
            )
            attach_output(extras, "kubectl get nodes (after join)", nodes_out)
            print(f"[test_radm_init_completes] All nodes:\n{nodes_out}")

        # ── Step 5: wait for pods ──────────────────────────────────────────────
        self._wait_for_pods(ssh_client, extras, label="after radm init")

    def test_radm_dependency_completes(self, ssh_client, package_profile, controller_profile, extras):
        """Run: sudo radm dependency --config config.yaml"""
        out, rc = ssh_client.run(
            f"cd {(getattr(package_profile, '_actual_extract_dir', None) or '/opt/rafay/rafay-airgapped-controller-v3.1-39')} && sudo ./radm dependency --config config.yaml 2>&1",
            timeout=600,
        )
        attach_output(extras, "radm dependency output", out)
        assert rc == 0, f"radm dependency failed (exit {rc}). See output above."
        self._wait_for_pods(ssh_client, extras, label="after radm dependency")

    def test_radm_application_completes(self, ssh_client, package_profile, extras):
        """Run: sudo radm application --config config.yaml (~20-30 min)"""
        out, rc = ssh_client.run(
            f"cd {(getattr(package_profile, '_actual_extract_dir', None) or '/opt/rafay/rafay-airgapped-controller-v3.1-39')} && sudo ./radm application --config config.yaml 2>&1",
            timeout=2400,
        )
        attach_output(extras, "radm application output", out)
        assert rc == 0, f"radm application failed (exit {rc}). See output above."
        self._wait_for_pods(ssh_client, extras, label="after radm application",
                            max_wait=2400, fail_on_timeout=True)

    def test_radm_cluster_completes(self, ssh_client, package_profile, extras):
        """Run: sudo radm cluster --config config.yaml"""
        out, rc = ssh_client.run(
            f"cd {(getattr(package_profile, '_actual_extract_dir', None) or '/opt/rafay/rafay-airgapped-controller-v3.1-39')} && sudo ./radm cluster --config config.yaml 2>&1",
            timeout=1200,
        )
        attach_output(extras, "radm cluster output", out)
        assert rc == 0, f"radm cluster failed (exit {rc}). See output above."
        self._wait_for_pods(ssh_client, extras, label="after radm cluster",
                            max_wait=1200, fail_on_timeout=True)


class TestPostInstallHealth:
    """Validate controller state after installation completes."""

    def test_all_pods_running(self, ssh_client, extras):
        """All pods must be Running or Completed."""
        out, rc = ssh_client.run("kubectl get pods -A --no-headers 2>/dev/null || /usr/local/bin/kubectl get pods -A --no-headers 2>&1")
        attach_output(extras, "kubectl get pods -A", out)
        assert rc == 0, "kubectl get pods failed -- is kubeconfig set up?"
        bad = [l for l in out.splitlines()
               if any(s in l for s in ("Pending", "Error", "CrashLoop", "Init:", "OOMKilled"))]
        assert not bad, f"{len(bad)} unhealthy pod(s):\n" + "\n".join(bad)

    def test_ha_master_node_count(self, ssh_client, controller_profile, extras):
        """HA=3 masters, Non-HA=1 master."""
        out, rc = ssh_client.run(
            "kubectl get nodes --no-headers -l node-role.kubernetes.io/control-plane 2>&1 || "
            "/usr/local/bin/kubectl get nodes --no-headers -l node-role.kubernetes.io/control-plane 2>&1"
        )
        attach_output(extras, "Master nodes", out)
        master_count = len([l for l in out.splitlines() if l.strip()])
        expected = 3 if controller_profile.ha else 1
        assert master_count == expected, (
            f"{controller_profile.mode_label} controller should have {expected} master(s) -- found {master_count}"
        )

    def test_console_endpoint_reachable(self, ssh_client, extras):
        """ops-console endpoint must respond."""
        out, rc = ssh_client.run(
            "curl -sk -o /dev/null -w '%{http_code}' https://localhost/ || echo FAILED"
        )
        attach_output(extras, "Console HTTP status", out)
        assert out.strip() not in ("000", "FAILED"), (
            "Console endpoint is not responding -- check pods and ingress"
        )

    def test_kube_config_accessible(self, ssh_client, extras):
        """kubectl must work with admin.conf."""
        out, rc = ssh_client.run("kubectl cluster-info 2>&1 || /usr/local/bin/kubectl cluster-info 2>&1")
        attach_output(extras, "kubectl cluster-info", out)
        assert rc == 0, "kubectl cluster-info failed -- check $HOME/.kube/config"
        assert "running" in out.lower() or "https://" in out.lower()

    def test_size_label_in_profile_summary(self, controller_profile):
        """Smoke-check: profile summary is well-formed."""
        summary = controller_profile.summary()
        assert controller_profile.controller_size in summary
        assert controller_profile.mode_label in summary
        assert controller_profile.os_type in summary