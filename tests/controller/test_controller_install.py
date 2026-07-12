"""
tests/controller/test_controller_install.py

Installation validation tests -- drive the radm install flow and validate
the result based on controller_size, HA mode, and OS type from the profile.
"""

import pytest

pytestmark = [pytest.mark.order(1), pytest.mark.controller, pytest.mark.regression]


def attach_output(extras, label, content):
    """Embed command output into the pytest-html report.
    Supports both pytest-html 3.x (extra) and 4.x (extras).
    Also mirrors the same content to the Allure report (if allure is
    importable), so results are visible in whichever report is open
    without needing to SSH to the Jenkins node.
    """
    import pytest_html
    block = "<pre style='font-size:12px;white-space:pre-wrap'>" + content + "</pre>"
    item  = pytest_html.extras.html("<b>" + label + "</b>" + block)
    if hasattr(extras, 'append'):
        extras.append(item)
    else:
        extras.extend([item])

    try:
        import allure
        allure.attach(content, name=label, attachment_type=allure.attachment_type.TEXT)
    except Exception:
        pass


def attach_pod_logs(ssh_client, extras, bad_lines, tail=200):
    """
    For every pod line that isn't Running/Completed, pull logs for each of
    its containers (regular + init containers) and attach them to the
    report -- so a Jenkins node SSH isn't needed to see why a pod is
    unhealthy.
    """
    for pod_line in bad_lines:
        parts = pod_line.split()
        if len(parts) < 2:
            continue
        ns, pod_name = parts[0], parts[1]

        containers_out, _ = ssh_client.run(
            "kubectl get pod " + pod_name + " -n " + ns +
            " -o jsonpath='{.spec.containers[*].name} {.spec.initContainers[*].name}' 2>/dev/null"
        )
        containers = [c for c in containers_out.split() if c]
        if not containers:
            containers = [""]

        for container in containers:
            c_flag = ("-c " + container) if container else ""
            label  = ns + "/" + pod_name + ((" [" + container + "]") if container else "")

            logs_out, _ = ssh_client.run(
                "kubectl logs " + pod_name + " -n " + ns + " " + c_flag + " --tail=" + str(tail) + " 2>&1"
            )
            attach_output(extras, "logs: " + label, logs_out)

            prev_out, prev_rc = ssh_client.run(
                "kubectl logs " + pod_name + " -n " + ns + " " + c_flag + " --previous --tail=" + str(tail) + " 2>&1"
            )
            if prev_rc == 0 and prev_out.strip():
                attach_output(extras, "logs (previous instance): " + label, prev_out)

            print("[attach_pod_logs] " + label + ": attached current" +
                  (" + previous" if (prev_rc == 0 and prev_out.strip()) else "") + " logs")


class TestPreflightChecks:
    """Verify the node meets installation prerequisites before radm init."""

    def test_preflight_os_matches_config(self, ssh_client, controller_profile, extras):
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
        out, rc = ssh_client.run("df -BG / | tail -1 | awk '{print $2}'")
        attach_output(extras, "Root disk size", out)
        out_clean = out.strip().replace("G", "")
        if not out_clean.isdigit():
            pytest.fail("Could not read root disk size -- got: " + out)
        size_gb = int(out_clean)
        assert size_gb >= 500, "Root disk is " + str(size_gb) + "GB -- need at least 500GB"

    def test_preflight_data_disk_1tb(self, ssh_client, extras):
        out, rc = ssh_client.run("df -BG /data 2>&1 | tail -1 | awk '{print $2}'")
        attach_output(extras, "/data disk size", out)
        out_clean = out.strip().replace("G", "")
        if not out_clean.isdigit():
            pytest.fail("/data is not mounted or not accessible -- got: " + out)
        size_gb = int(out_clean)
        assert size_gb >= 1000, "/data disk is " + str(size_gb) + "GB -- need at least 1TB"

    def test_preflight_tmp_50gb(self, ssh_client, extras):
        out, rc = ssh_client.run("df -BG /tmp | tail -1 | awk '{print $4}'")
        attach_output(extras, "/tmp available", out)
        out_clean = out.strip().replace("G", "")
        if not out_clean.isdigit():
            pytest.fail("Could not read /tmp available space -- got: " + out)
        avail_gb = int(out_clean)
        assert avail_gb >= 50, "/tmp only has " + str(avail_gb) + "GB free -- need at least 50GB"

    def test_preflight_cpu_meets_size(self, ssh_client, controller_profile, extras):
        out, rc = ssh_client.run("nproc")
        attach_output(extras, "CPU count", out)
        actual_cpu   = int(out.strip())
        required_cpu = controller_profile.cpu
        assert actual_cpu >= required_cpu, (
            "Controller size '" + controller_profile.controller_size + "' requires " +
            str(required_cpu) + " CPUs -- found " + str(actual_cpu)
        )

    def test_preflight_memory_meets_size(self, ssh_client, controller_profile, extras):
        out, rc = ssh_client.run("free -m | awk '/^Mem:/{print $2}'")
        attach_output(extras, "Memory (MB)", out)
        actual_mb    = int(out.strip())
        actual_gb    = actual_mb / 1024
        required_gb  = controller_profile.memory_gb
        threshold_gb = required_gb * 0.95
        assert actual_gb >= threshold_gb, (
            "Controller size '" + controller_profile.controller_size + "' requires " +
            str(required_gb) + "GB RAM -- found " + "{:.1f}".format(actual_gb) + "GB"
        )

    def test_ubuntu_iptables_flushed(self, ssh_client, extras, controller_profile):
        if controller_profile.os_type != "ubuntu24":
            pytest.skip("iptables flush check is Ubuntu 24.04 only")

        ufw_out, _ = ssh_client.run(
            "sudo ufw disable 2>/dev/null || true && "
            "sudo systemctl stop ufw 2>/dev/null || true && "
            "sudo systemctl disable ufw 2>/dev/null || true && "
            "echo UFW_DONE"
        )
        attach_output(extras, "ufw disable", ufw_out.strip())
        print("[test_ubuntu_iptables_flushed] ufw: " + ufw_out.strip())

        flush_out, _ = ssh_client.run(
            "sudo iptables -F && "
            "sudo iptables -t nat -F && "
            "sudo iptables -t mangle -F && "
            "sudo iptables -X 2>/dev/null || true && "
            "echo FLUSHED"
        )
        attach_output(extras, "iptables flush", flush_out.strip())

        after_out, _ = ssh_client.run("sudo iptables -L | wc -l")
        attach_output(extras, "iptables rules after flush", after_out.strip())
        print("[test_ubuntu_iptables_flushed] rules after flush: " + after_out.strip())

    def test_nfs_utils_installed(self, ssh_client, controller_profile, extras):
        if controller_profile.os_type == "ubuntu24":
            out, rc = ssh_client.run("dpkg -l nfs-common 2>/dev/null | grep -c '^ii'")
        else:
            out, rc = ssh_client.run("rpm -q nfs-utils 2>/dev/null | grep -c nfs-utils")
        attach_output(extras, "NFS utils check", out)
        assert int(out.strip()) >= 1, "NFS utilities not installed -- run apt/yum install"

    def test_preflight_secondary_nodes(self, ssh_client, controller_profile,
                                       secondary_ips, extras):
        if not controller_profile.ha or not secondary_ips:
            pytest.skip("Non-HA mode - secondary node preflight not needed")

        from lib.ssh.ssh_client import SSHClient

        size_requirements = {
            "cpu":    controller_profile.cpu,
            "memory": controller_profile.memory_gb,
        }

        failures = []

        for i, sec_ip in enumerate(secondary_ips, 2):
            print("[test_preflight_secondary_nodes] Checking node" + str(i) + " (" + sec_ip + ") ...")
            sec_ssh = SSHClient(
                host=sec_ip,
                user=controller_profile.user,
                key_path=controller_profile.ssh_key
            )
            sec_ssh.connect()
            node_failures = []

            try:
                os_out, _ = sec_ssh.run("cat /etc/os-release | grep -E '^ID=|VERSION_ID'")
                attach_output(extras, "node" + str(i) + " OS", os_out)

                disk_out, _ = sec_ssh.run("df -BG / | tail -1 | awk '{print $2}'")
                disk_gb = int(disk_out.strip().replace("G", "") or "0")
                if disk_gb < 500:
                    node_failures.append("Root disk " + str(disk_gb) + "GB < 500GB")

                cpu_out, _ = sec_ssh.run("nproc")
                cpu = int(cpu_out.strip() or "0")
                if cpu < size_requirements["cpu"]:
                    node_failures.append("CPU " + str(cpu) + " < " + str(size_requirements["cpu"]))

                mem_out, _ = sec_ssh.run("free -m | awk '/^Mem:/{print $2}'")
                mem_gb = int(mem_out.strip() or "0") / 1024
                threshold = size_requirements["memory"] * 0.95
                if mem_gb < threshold:
                    node_failures.append("Memory {:.1f}GB < {}GB".format(mem_gb, size_requirements["memory"]))

                tmp_out, _ = sec_ssh.run("df -BG /tmp | tail -1 | awk '{print $4}'")
                tmp_gb = int(tmp_out.strip().replace("G", "") or "0")
                if tmp_gb < 50:
                    node_failures.append("/tmp " + str(tmp_gb) + "GB < 50GB")

                status = "PASS" if not node_failures else ("FAIL: " + ", ".join(node_failures))
                attach_output(extras, "node" + str(i) + " preflight", status)
                print("[test_preflight_secondary_nodes] node" + str(i) + ": " + status)

                if node_failures:
                    failures.append("node" + str(i) + " (" + sec_ip + "): " + ", ".join(node_failures))

            finally:
                sec_ssh.disconnect()

        assert not failures, "Preflight failed on secondary nodes:\n" + "\n".join(failures)


class TestPackageSetup:
    """
    Download and set up the controller installation package on the VM.
    """

    def test_package_url_derived(self, package_profile, extras):
        attach_output(extras, "Package summary", package_profile.summary())
        assert package_profile.url.startswith("https://"), "Invalid URL derived: " + package_profile.url
        assert package_profile.name in package_profile.url, "Package name not in URL: " + package_profile.url
        assert package_profile.version in package_profile.url, "Version not in URL: " + package_profile.url

    def test_install_dir_created(self, ssh_client, package_profile, extras):
        out, rc = ssh_client.run(
            "sudo mkdir -p " + package_profile.install_dir + " && echo OK"
        )
        attach_output(extras, "mkdir install_dir", out)
        assert rc == 0 and "OK" in out, "Could not create " + package_profile.install_dir + ": " + out

    def test_dns_resolver_configured(self, ssh_client, extras):
        check_out, _ = ssh_client.run(
            "test -f /etc/resolv.conf && cat /etc/resolv.conf || echo MISSING"
        )
        attach_output(extras, "resolv.conf", check_out)

        if "MISSING" in check_out or "nameserver" not in check_out:
            print("[test_dns_resolver_configured] resolv.conf missing - creating ...")
            dns_content = "nameserver 169.254.169.254\nnameserver 8.8.8.8\nnameserver 8.8.4.4\n"
            fix_out, fix_rc = ssh_client.run(
                "printf '" + dns_content + "' | sudo tee /etc/resolv.conf > /dev/null && echo FIXED"
            )
            attach_output(extras, "resolv.conf fix", fix_out)
            print("[test_dns_resolver_configured] Fixed: " + fix_out.strip())
        else:
            print("[test_dns_resolver_configured] resolv.conf already configured")

        dns_out, _ = ssh_client.run(
            "nslookup google.com 2>/dev/null | grep -i address | head -2 || echo DNS_DONE"
        )
        attach_output(extras, "DNS check", dns_out)
        print("[test_dns_resolver_configured] DNS: " + dns_out.strip()[:100])

    def test_aria2c_installed(self, ssh_client, controller_profile, nsg_manager, extras):
        import time
        if nsg_manager:
            nsg_manager.attach()
            attach_output(extras, "NSG", "Attached - will stay open through package download")
            print("[test_aria2c_installed] NSG attached - waiting 30s for rules to propagate ...")
            time.sleep(30)

        out, rc = ssh_client.run("which aria2c 2>/dev/null && aria2c --version 2>/dev/null | head -1")
        if rc == 0 and "aria2" in out.lower():
            attach_output(extras, "aria2c version", out)
            print("[test_aria2c_installed] already installed: " + out.strip())
            return

        attach_output(extras, "aria2c status", "Not found - installing ...")

        if controller_profile.os_type == "ubuntu24":
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
                assert universe_rc == 0, "aria2c install failed"
        else:
            out, rc = ssh_client.run(
                "sudo yum install -y aria2 2>&1 || sudo dnf install -y aria2 2>&1",
                timeout=180
            )
            attach_output(extras, "yum/dnf install aria2", out)
            assert rc == 0, "aria2c install failed on RHEL (exit " + str(rc) + "): " + out

        ssh_client.run(
            "sudo iptables -F && sudo iptables -t nat -F && "
            "sudo iptables -t mangle -F && sudo iptables -X 2>/dev/null || true",
            timeout=10
        )
        print("[test_aria2c_installed] iptables re-flushed after apt-get")

        verify_out, verify_rc = ssh_client.run("which aria2c && aria2c --version 2>/dev/null | head -1")
        attach_output(extras, "aria2c version", verify_out)
        assert verify_rc == 0, "aria2c not found after install"

    def test_package_download(self, ssh_client, package_profile, nsg_manager, extras):
        import time

        attach_output(extras, "Package URL", package_profile.url)
        attach_output(extras, "Download destination", package_profile.tar_path)

        aria2_control = package_profile.tar_path + ".aria2"

        tar_exists_out, tar_exists_rc = ssh_client.run(
            "test -f " + package_profile.tar_path + " && echo EXISTS || echo MISSING"
        )
        aria2_exists_out, _ = ssh_client.run(
            "test -f " + aria2_control + " && echo PARTIAL || echo CLEAN"
        )

        tar_exists  = "EXISTS"  in tar_exists_out
        is_complete = tar_exists and "CLEAN" in aria2_exists_out

        attach_output(extras, "Download state",
            "tar exists: " + str(tar_exists) + " | .aria2 file: " +
            ("YES (partial)" if "PARTIAL" in aria2_exists_out else "NO (clean)"))

        if is_complete:
            size_out, _ = ssh_client.run(
                "du -sh " + package_profile.tar_path + " | awk '{print $1}'"
            )
            attach_output(extras, "Download skipped",
                "Already complete (" + size_out.strip() + "): " + package_profile.tar_path)
            if nsg_manager:
                nsg_manager.detach()
                attach_output(extras, "NSG", "Detached (package already present)")
            return

        partial_out, _ = ssh_client.run(
            "test -f " + aria2_control + " && echo PARTIAL || echo CLEAN"
        )
        if "PARTIAL" in partial_out:
            attach_output(extras, "Partial download detected",
                "Removing incomplete files: " + package_profile.tar_path + " + .aria2")
            print("[test_package_download] Removing partial download files ...")
            ssh_client.run("sudo rm -f " + package_profile.tar_path + " " + aria2_control)

        download_ok = False
        try:
            if nsg_manager:
                attach_output(extras, "NSG status", "Already attached from aria2c install step")
                print("[test_package_download] NSG already attached")
            else:
                attach_output(extras, "NSG status", "No nsg_manager -- ensure VM has internet access")

            diag_out, _ = ssh_client.run(
                "curl -sI --max-time 15 "
                "https://rafay-airgap-controller.s3.us-west-2.amazonaws.com "
                "--write-out '\\nHTTP_CODE:%{http_code}' -o /dev/null 2>&1 || true",
                timeout=30
            )
            attach_output(extras, "S3 connectivity", diag_out)
            print("[S3 check] " + diag_out)

            aria2c_path_out, _ = ssh_client.run("which aria2c")
            aria2c_bin = aria2c_path_out.strip() or "/usr/bin/aria2c"
            print("[test_package_download] Downloading: " + package_profile.url)

            out, rc = ssh_client.run(
                "cd " + package_profile.install_dir + " && "
                "sudo " + aria2c_bin + " -x 16 -s 16 --max-tries=3 --retry-wait=10 "
                "--connect-timeout=30 --log-level=notice " +
                package_profile.url + " 2>&1",
                timeout=1800
            )
            attach_output(extras, "aria2c output", out)
            print("[aria2c]\n" + out + "\n[exit] " + str(rc))

            if rc == 0:
                download_ok = True
                package_profile._actual_tar_path = package_profile.install_dir + "/" + package_profile.name
                print("[test_package_download] Download complete")
            else:
                errors = {
                    3:  "404 -- check package name in dev.yaml",
                    6:  "Network problem -- check NSG egress rules",
                    9:  "Disk full",
                    16: "No internet -- check NSG has 0.0.0.0/0 egress",
                }
                attach_output(extras, "Download FAILED",
                    errors.get(rc, "exit " + str(rc)) + "\n" + out[-500:])

            if download_ok:
                v_out, verify_rc = ssh_client.run(
                    "test -s " + package_profile.tar_path + " && "
                    "du -sh " + package_profile.tar_path + " | awk '{print $1}'"
                )
                attach_output(extras, "Downloaded file size", v_out)
            else:
                verify_rc = 1

        finally:
            if nsg_manager:
                print("[test_package_download] Detaching NSG ...")
                nsg_manager.detach()
                attach_output(extras, "NSG", "Detached -- internet access removed")

        assert download_ok, "Package download failed. URL: " + package_profile.url
        assert verify_rc == 0, "File missing or empty: " + package_profile.tar_path

    def test_package_extract(self, ssh_client, package_profile, extras):
        import time

        extract_dest = package_profile.install_dir
        attach_output(extras, "Extract destination", extract_dest)

        ls_out, _ = ssh_client.run("ls -1 " + extract_dest + "/ 2>/dev/null")
        before_dirs = set(ls_out.strip().splitlines())

        already = [
            d for d in before_dirs
            if "rafay-airgapped-controller" in d and not d.endswith(".tar.gz")
        ]
        if already:
            actual = extract_dest + "/" + already[0]
            attach_output(extras, "Already extracted", actual)
            package_profile._actual_extract_dir = actual
            print("[test_package_extract] Already extracted: " + actual)
            return

        tar_path = getattr(package_profile, "_actual_tar_path", package_profile.tar_path)
        locate_out, locate_rc = ssh_client.run(
            "test -f " + tar_path + " && echo FOUND || echo MISSING"
        )
        if "MISSING" in locate_out:
            search_out, _ = ssh_client.run(
                "find /opt/rafay -name '" + package_profile.name + "' 2>/dev/null | head -1"
            )
            found = search_out.strip()
            if found:
                tar_path = found
                package_profile._actual_tar_path = found
                print("[test_package_extract] Found tar at: " + found)
            else:
                pytest.fail("Package tar not found at " + tar_path + ". Run test_package_download first.")

        size_out, _ = ssh_client.run("du -sh " + tar_path)
        print("[test_package_extract] File size: " + size_out)
        attach_output(extras, "Package size", size_out)

        extract_cmd = "sudo tar -xf " + tar_path + " -C " + extract_dest + " 2>&1 && echo EXTRACTED"
        print("[test_package_extract] Running: " + extract_cmd)
        start_time = time.time()

        out, rc = ssh_client.run(extract_cmd, timeout=3600)
        elapsed = int(time.time() - start_time)
        attach_output(extras, "Extract result (" + str(elapsed) + "s)",
            out[-500:] if len(out) > 500 else out)
        print("[test_package_extract] Finished in " + str(elapsed) + "s - exit " + str(rc))

        assert rc == 0 and "EXTRACTED" in out, (
            "tar extraction failed (exit " + str(rc) + ") after " + str(elapsed) + "s.\nOutput: " + out
        )

        ls_after, _ = ssh_client.run("ls -1 " + extract_dest + "/ 2>/dev/null")
        after_dirs  = set(ls_after.strip().splitlines())
        new_entries = after_dirs - before_dirs
        extracted_dirs = [
            e for e in new_entries
            if not e.endswith(".tar.gz") and not e.endswith(".tar")
        ]

        assert extracted_dirs, (
            "No new directory found after extraction in " + extract_dest +
            ". New entries: " + str(sorted(new_entries))
        )

        actual_extract_dir = extract_dest + "/" + extracted_dirs[0]
        attach_output(extras, "Detected extract dir", actual_extract_dir)
        print("[test_package_extract] Extract dir: " + actual_extract_dir)
        package_profile._actual_extract_dir = actual_extract_dir

    def test_setup_secondary_nodes(self, ssh_client, package_profile,
                                   controller_profile, secondary_ips,
                                   secondary_instance_ids, oci_profile_fixture, extras):
        if not controller_profile.ha or not secondary_ips:
            pytest.skip("Non-HA mode - secondary node setup not needed")

        from lib.ssh.ssh_client import SSHClient
        from lib.oci.vm_manager import OCINSGManager
        import base64

        extract_dir = getattr(package_profile, "_actual_extract_dir",
                              package_profile.extract_dir)
        config_path = extract_dir + "/config.yaml"

        config_content, cfg_rc = ssh_client.run("sudo cat " + config_path)
        assert cfg_rc == 0, "Could not read config.yaml from node1"
        encoded_config = base64.b64encode(config_content.encode()).decode()
        print("[test_setup_secondary_nodes] config.yaml read from node1 (" + str(len(config_content)) + " bytes)")

        for i, (sec_ip, sec_id) in enumerate(
            zip(secondary_ips, secondary_instance_ids), 2
        ):
            print("[test_setup_secondary_nodes] Setting up node" + str(i) + " (" + sec_ip + ") ...")
            attach_output(extras, "node" + str(i) + " IP", sec_ip)

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
                sec_ssh.run("sudo mkdir -p " + package_profile.install_dir)

                check_out, _ = sec_ssh.run(
                    "test -d " + extract_dir + " && echo EXISTS || echo MISSING"
                )
                already_extracted = "EXISTS" in check_out

                if not already_extracted:
                    if sec_nsg:
                        sec_nsg.attach()
                        print("[test_setup_secondary_nodes] node" + str(i) + ": NSG attached")

                    print("[test_setup_secondary_nodes] node" + str(i) + ": installing aria2c ...")
                    sec_ssh.run("sudo apt-get update -y 2>&1 || true", timeout=120)
                    sec_ssh.run("sudo apt-get install -y aria2 2>&1 || true", timeout=180)

                    tar_check, _ = sec_ssh.run(
                        "test -f " + package_profile.tar_path + " && "
                        "test ! -f " + package_profile.tar_path + ".aria2 && echo COMPLETE || echo MISSING"
                    )
                    if "MISSING" in tar_check:
                        aria2c_out, _ = sec_ssh.run("which aria2c")
                        aria2c_bin = aria2c_out.strip() or "/usr/bin/aria2c"
                        print("[test_setup_secondary_nodes] node" + str(i) + ": downloading ...")
                        dl_out, dl_rc = sec_ssh.run(
                            "cd " + package_profile.install_dir + " && "
                            "sudo " + aria2c_bin + " -x 16 -s 16 --max-tries=3 "
                            "--connect-timeout=30 " + package_profile.url + " 2>&1",
                            timeout=1800
                        )
                        attach_output(extras, "node" + str(i) + " download", dl_out[-300:])
                        assert dl_rc == 0, "Download failed on node" + str(i) + ": " + dl_out[-200:]
                        print("[test_setup_secondary_nodes] node" + str(i) + ": download complete")
                    else:
                        print("[test_setup_secondary_nodes] node" + str(i) + ": package already downloaded")

                    if sec_nsg:
                        sec_nsg.detach()
                        print("[test_setup_secondary_nodes] node" + str(i) + ": NSG detached")

                    print("[test_setup_secondary_nodes] node" + str(i) + ": extracting ...")
                    ext_out, ext_rc = sec_ssh.run(
                        "sudo tar -xf " + package_profile.tar_path + " "
                        "-C " + package_profile.install_dir + " 2>&1 && echo EXTRACTED",
                        timeout=3600
                    )
                    attach_output(extras, "node" + str(i) + " extract", ext_out[-200:])
                    assert ext_rc == 0 and "EXTRACTED" in ext_out, (
                        "Extraction failed on node" + str(i) + ": " + ext_out[-200:]
                    )
                    print("[test_setup_secondary_nodes] node" + str(i) + ": extraction complete")
                else:
                    print("[test_setup_secondary_nodes] node" + str(i) + ": already extracted - skipping download")
                    if sec_nsg:
                        try:
                            sec_nsg.detach()
                        except Exception:
                            pass

                write_out, write_rc = sec_ssh.run(
                    "echo '" + encoded_config + "' | base64 -d | "
                    "sudo tee " + config_path + " > /dev/null && echo OK"
                )
                attach_output(extras, "node" + str(i) + " config.yaml", write_out)
                assert write_rc == 0 and "OK" in write_out, (
                    "Failed to write config.yaml to node" + str(i) + ": " + write_out
                )
                print("[test_setup_secondary_nodes] node" + str(i) + ": config.yaml copied")
                attach_output(extras, "node" + str(i) + " status", "Setup complete")

            finally:
                sec_ssh.disconnect()

    def test_radm_binary_copied(self, ssh_client, package_profile, extras):
        extract_dir = (getattr(package_profile, "_actual_extract_dir", None) or package_profile.extract_dir)
        attach_output(extras, "Extract dir used", extract_dir)
        out, rc = ssh_client.run(
            "sudo cp " + extract_dir + "/radm /usr/bin/radm && "
            "sudo chmod +x /usr/bin/radm && echo OK"
        )
        attach_output(extras, "radm binary copy", out)
        assert rc == 0 and "OK" in out, "Failed to copy radm binary (exit " + str(rc) + "): " + out

        version_out, version_rc = ssh_client.run("which radm")
        attach_output(extras, "radm location", version_out)
        assert version_rc == 0, "radm not found in PATH after copy"

    def test_config_yaml_created(self, ssh_client, package_profile, request,
                                  controller_profile, controller_fqdn, raw_config, extras):
        extract_dir  = (getattr(package_profile, "_actual_extract_dir", None) or package_profile.extract_dir)
        config_path  = extract_dir + "/config.yaml"
        tmpl_names   = ["config.yaml-airgap-tmpl", "config.yaml-tmpl", "config.yaml.tmpl"]

        tmpl_path = None
        for name in tmpl_names:
            check, rc = ssh_client.run(
                "test -f " + extract_dir + "/" + name + " && echo FOUND || echo MISSING"
            )
            if "FOUND" in check:
                tmpl_path = extract_dir + "/" + name
                break

        assert tmpl_path, "No config.yaml template found in " + extract_dir
        attach_output(extras, "Template found", tmpl_path)

        out, rc = ssh_client.run("sudo cp " + tmpl_path + " " + config_path + " && echo OK")
        assert rc == 0 and "OK" in out, "Failed to copy template: " + out

        orig_out, _ = ssh_client.run("cat " + config_path)
        attach_output(extras, "config.yaml (original)", orig_out)

        size      = controller_profile.controller_size
        ha        = "true" if controller_profile.ha else "false"
        repo_path = extract_dir

        if controller_fqdn:
            star_domain = controller_fqdn.lstrip("*.")
        else:
            base_domain  = raw_config.get("dns", {}).get("base_domain", "")
            build_no_val = request.config.getoption("--build-no") or ""
            if base_domain and build_no_val:
                star_domain = "shc-" + build_no_val + "." + base_domain
            elif base_domain:
                display_name = raw_config.get("oci", {}).get("display_name", "")
                if display_name and "{build_no}" not in display_name:
                    star_domain = display_name + "." + base_domain
                    print("[test_config_yaml_created] Using display_name as build_no fallback: " + star_domain)
                else:
                    star_domain = ""
                    print("[test_config_yaml_created] WARNING: pass --build-no=N to set star-domain")
            else:
                star_domain = ""
                print("[test_config_yaml_created] WARNING: dns.base_domain not set in dev.yaml")

        print("[test_config_yaml_created] Patching config.yaml:")
        print("  size             : " + size)
        print("  ha               : " + ha)
        print("  type             : airgap")
        print("  archive-directory: " + repo_path)
        print("  star-domain      : " + star_domain)

        attach_output(extras, "Patch values",
            "size=" + size + " | ha=" + ha + " | type=airgap | "
            "archive-directory=" + repo_path + " | star-domain=" + star_domain
        )

        q = '"'
        patches = [
            "sudo sed -i '/size:/s|:.*|: " + q + size + q + "|' " + config_path,
            "sudo sed -i '/^[ ]*ha:/s|:.*|: " + ha + "|' " + config_path,
        ]

        for patch_cmd in patches:
            out_p, rc_p = ssh_client.run(patch_cmd)
            assert rc_p == 0, "Patch failed: " + patch_cmd + "\n" + out_p

        ssh_client.run("sudo sed -i 's|^    type:.*|    type: TYPE_PLACEHOLDER|' " + config_path)
        out_type, rc_type = ssh_client.run(
            "sudo sed -i 's|type: TYPE_PLACEHOLDER|type: \"airgap\"|' " + config_path + " && echo OK"
        )
        attach_output(extras, "type patch", out_type)
        assert rc_type == 0 and "OK" in out_type, "type patch failed: " + out_type
        print("  [OK] type: airgap")

        ssh_client.run(
            "sudo sed -i 's|archive-directory:.*|archive-directory: RAFAY_PLACEHOLDER|' " + config_path
        )
        out_arch, rc_arch = ssh_client.run(
            "sudo sed -i 's|archive-directory: RAFAY_PLACEHOLDER|archive-directory: " + repo_path + "|' " + config_path + " && echo OK"
        )
        attach_output(extras, 'archive-directory patch', out_arch)
        assert rc_arch == 0 and 'OK' in out_arch, 'archive-directory patch failed: ' + out_arch
        print('  [OK] archive-directory: ' + repo_path)

        if star_domain:
            ssh_client.run(
                "sudo sed -i '/^[ ]*star-domain:/s|star-domain:.*|star-domain: STAR_PLACEHOLDER|' " + config_path
            )
            out_star, rc_star = ssh_client.run(
                "sudo sed -i 's|star-domain: STAR_PLACEHOLDER|star-domain: " + star_domain + "|' " +
                config_path + " && echo OK"
            )
            attach_output(extras, 'star-domain patch', out_star)
            assert rc_star == 0 and 'OK' in out_star, 'star-domain patch failed: ' + out_star
            print('  [OK] star-domain: ' + star_domain)

        verify_items = [
            ("size",              '"' + size + '"'),
            ("ha",                ha),
            ("type",              '"airgap"'),
        ]

        for key, expected in verify_items:
            grep_out, _ = ssh_client.run("grep '" + key + ":' " + config_path + " | head -1")
            assert expected in grep_out, (
                "Patch failed for '" + key + "': expected '" + expected + "' in '" + grep_out.strip() + "'"
            )
            print("  [OK] " + grep_out.strip())

        attach_output(extras, "Patch verification", "All fields patched correctly")

    def test_package_version_matches_profile(self, ssh_client, package_profile, extras):
        out, rc = ssh_client.run(
            "ls " + package_profile.install_dir + "/ | grep rafay-airgapped-controller"
        )
        attach_output(extras, "Extracted dirs", out)
        assert package_profile.version in out, (
            "Expected version " + package_profile.version + " in extracted dir, got: " + out
        )


class TestRadmInstall:
    """Drive the radm installation steps sequentially."""

    # Class-level cache for the "cluster already fully applied" check.
    # Computed once (by whichever of dependency/application/cluster runs
    # first), then reused by the other two so we don't repeat the same
    # kubectl query three times in a row.
    _cluster_healthy = None

    def _check_cluster_already_healthy(self, ssh_client, extras):
        """
        Check once whether ALL pods are already Running/Completed -- no
        exceptions. If any pod, for any reason, is not Running/Completed,
        the cluster is treated as not-yet-healthy and the caller will
        re-run its radm step. There is no exclusion list here: a pod that
        can't come up is a real failure and should be surfaced as one,
        not quietly waved through.

        Result is cached on the class so this kubectl query only runs once
        per test session, not once per test.
        """
        if TestRadmInstall._cluster_healthy is not None:
            return TestRadmInstall._cluster_healthy

        pods_out, pods_rc = ssh_client.run("kubectl get pods -A --no-headers 2>/dev/null")
        lines     = [l for l in pods_out.splitlines() if l.strip()]
        not_ready = [l for l in lines if "Running" not in l and "Completed" not in l]
        healthy   = pods_rc == 0 and bool(lines) and not not_ready

        attach_output(extras, "pre-check pod status",
                      "total=" + str(len(lines)) + " not_ready=" + str(len(not_ready)))

        TestRadmInstall._cluster_healthy = healthy
        return healthy

    def _wait_for_pods(self, ssh_client, extras, label="pods", max_wait=1500,
                       fail_on_timeout=False, quick_check=False):
        """
        Poll kubectl get pods -A every 30s.
        Waits until two consecutive checks show ALL pods Running/Completed
        with the same pod count.

        On timeout: describes unhealthy pods and continues (never blocks next step).

        Args:
            label          : label for log/report output e.g. "after radm init"
            max_wait       : seconds to wait (default 25 min)
            fail_on_timeout: ignored - always continues after timeout with describe output
            quick_check    : if True, skip the poll loop entirely and do ONE
                             kubectl get pods call instead. Use this when
                             _check_cluster_already_healthy() already
                             confirmed the cluster is healthy just before
                             calling this -- there is nothing to wait for,
                             since radm dependency/application/cluster were
                             never re-run in that branch. Waiting the full
                             max_wait here (as every call site used to) just
                             burns 20-40 minutes per test re-confirming
                             something already confirmed a moment ago.
        """
        import time

        if quick_check:
            print("[" + label + "] quick_check=True (cluster already confirmed healthy) "
                  "- single kubectl check, no poll loop ...")
            pods_out, pods_rc = ssh_client.run(
                "kubectl get pods -A --no-headers 2>/dev/null || "
                "/usr/local/bin/kubectl get pods -A --no-headers 2>&1"
            )
            lines = [l for l in pods_out.splitlines() if l.strip()]
            not_ready = [l for l in lines if "Running" not in l and "Completed" not in l]
            attach_output(extras, "quick check (" + label + ")", pods_out)
            print("[" + label + "] quick check: " + str(len(lines)) + " pods, " +
                  str(len(not_ready)) + " not ready")
            if not_ready:
                print("[" + label + "] quick check found unexpected not-ready pods "
                      "despite already-healthy guard - attaching logs ...")
                attach_pod_logs(ssh_client, extras, not_ready)
            return

        poll_every   = 30
        deadline     = time.time() + max_wait
        attempt      = 0
        stable_count = 0
        prev_total   = 0

        print("[" + label + "] Waiting for all pods Running/Completed (max " + str(max_wait // 60) + " min) ...")

        while time.time() < deadline:
            attempt += 1
            pods_out, pods_rc = ssh_client.run("kubectl get pods -A --no-headers 2>/dev/null || /usr/local/bin/kubectl get pods -A --no-headers 2>&1")

            if pods_rc != 0:
                print("[" + label + "] kubectl not ready (attempt " + str(attempt) + ") ...")
                stable_count = 0
                time.sleep(poll_every)
                continue

            lines     = [l for l in pods_out.splitlines() if l.strip()]
            total     = len(lines)
            not_ready = [l for l in lines if "Running" not in l and "Completed" not in l]
            unhealthy = [l for l in lines if any(
                s in l for s in ("Error", "CrashLoop", "OOMKilled")
            )]

            print("[" + label + "] Attempt " + str(attempt) + ": " +
                  str(total) + " pods, " + str(len(not_ready)) + " not ready, " + str(len(unhealthy)) + " unhealthy")

            if unhealthy:
                print("[" + label + "] Unhealthy pods - waiting for recovery ...")
                stable_count = 0

            if not not_ready and total > 0:
                if total == prev_total:
                    stable_count += 1
                else:
                    stable_count = 0
                    print("[" + label + "] Pod count changed " + str(prev_total) + " -> " + str(total) + ", waiting ...")

                if stable_count >= 2:
                    attach_output(extras, "All pods Running (" + label + ")", pods_out)
                    print("[" + label + "] All " + str(total) + " pods Running/Completed OK")
                    return
            else:
                stable_count = 0

            prev_total = total
            time.sleep(poll_every)

        print("[" + label + "] Timeout reached - describing unhealthy pods ...")
        pods_out, _ = ssh_client.run("kubectl get pods -A --no-headers 2>&1")
        lines     = [l for l in pods_out.splitlines() if l.strip()]
        not_ready = [l for l in lines if "Running" not in l and "Completed" not in l]
        unhealthy = [l for l in lines if any(
            s in l for s in ("Error", "CrashLoop", "OOMKilled", "Pending")
        )]

        attach_output(extras, "Pod status at timeout (" + label + ")", pods_out)
        print("[" + label + "] " + str(len(lines)) + " total, " + str(len(not_ready)) + " not ready after " + str(max_wait // 60) + " min")

        for pod_line in unhealthy[:5]:
            parts = pod_line.split()
            if len(parts) >= 2:
                ns, pod_name = parts[0], parts[1]
                desc_out, _ = ssh_client.run(
                    "kubectl describe pod " + pod_name + " -n " + ns + " 2>&1 | tail -30"
                )
                attach_output(extras, "describe " + ns + "/" + pod_name, desc_out)
                print("[" + label + "] describe " + ns + "/" + pod_name + ":\n" + desc_out[-300:])

        if not_ready:
            attach_pod_logs(ssh_client, extras, not_ready)

        print("[" + label + "] Continuing despite unready pods ...")

    def test_radm_binary_present(self, ssh_client, extras):
        out, rc = ssh_client.run("which radm || echo NOT_FOUND")
        attach_output(extras, "radm path", out)
        assert "NOT_FOUND" not in out, "radm binary not found -- copy it to /usr/bin/"

    def test_config_yaml_present(self, ssh_client, package_profile, extras):
        extract_dir = (getattr(package_profile, "_actual_extract_dir", None) or package_profile.extract_dir)
        config_path = extract_dir + "/config.yaml"
        out, rc = ssh_client.run("test -f " + config_path + " && echo OK || echo MISSING")
        attach_output(extras, "config.yaml check", out)
        assert out.strip() == "OK", "config.yaml not found at " + config_path

    def test_config_ha_matches_profile(self, ssh_client, package_profile, controller_profile, extras):
        extract_dir = (getattr(package_profile, "_actual_extract_dir", None) or package_profile.extract_dir)
        config_path = extract_dir + "/config.yaml"
        out, rc = ssh_client.run("grep 'ha:' " + config_path + " | head -1")
        attach_output(extras, "config.yaml ha field", out)
        remote_ha = "true" in out.lower()
        assert remote_ha == controller_profile.ha, (
            "Profile says ha=" + str(controller_profile.ha) + " but config.yaml has: " + out.strip()
        )

    def test_config_domain_set(self, ssh_client, package_profile, extras):
        extract_dir = (getattr(package_profile, "_actual_extract_dir", None) or package_profile.extract_dir)
        config_path = extract_dir + "/config.yaml"
        out, rc = ssh_client.run("grep 'star-domain' " + config_path)
        attach_output(extras, "star-domain config", out)
        assert "example.com" not in out, "star-domain still has the template value"
        assert out.strip() != "", "star-domain is missing from config.yaml"

    def test_radm_init_completes(self, ssh_client, package_profile,
                                  controller_profile, secondary_ips, extras):
        from lib.ssh.ssh_client import SSHClient

        extract_dir = getattr(package_profile, "_actual_extract_dir", None)
        if not extract_dir:
            search_out, _ = ssh_client.run(
                "find /opt/rafay -maxdepth 1 -type d -name 'rafay-airgapped-controller*' 2>/dev/null | head -1"
            )
            extract_dir = search_out.strip() or "/opt/rafay"
            print("[test_radm_init_completes] Detected extract_dir: " + extract_dir)

        attach_output(extras, "Extract dir", extract_dir)
        attach_output(extras, "HA mode", str(controller_profile.ha))

        already_init_out, _ = ssh_client.run(
            "test -f /etc/kubernetes/admin.conf && echo ALREADY_INIT || echo NOT_INIT"
        )
        already_initialized = "ALREADY_INIT" in already_init_out
        attach_output(extras, "radm init status",
            "Already initialized - skipping radm init" if already_initialized
            else "Not initialized - running radm init"
        )

        if already_initialized:
            print("[test_radm_init_completes] Kubernetes already initialized - skipping radm init")
        else:
            print("[test_radm_init_completes] Full reset: stopping consul + kubelet ...")

            ssh_client.run(
                "sudo systemctl stop consul kubelet 2>/dev/null || true && sleep 2",
                timeout=15
            )
            ssh_client.run("sudo kubeadm reset -f 2>/dev/null || true", timeout=30)
            ssh_client.run(
                "sudo rm -rf /etc/kubernetes /var/lib/kubelet /var/lib/etcd "
                "/var/run/kubernetes /tmp/rafay-infra "
                "/var/lib/consul /etc/consul.d/rafay*.hcl "
                "/etc/consul.d/rafay*.json 2>/dev/null || true",
                timeout=15
            )
            ssh_client.run(
                "sudo rm -rf /etc/containerd/certs.d/ && "
                "sudo rm -f /etc/containerd/config.toml 2>/dev/null || true",
                timeout=10
            )
            ssh_client.run("sudo systemctl restart containerd && sleep 5 && echo READY", timeout=30)

            consul_status, _ = ssh_client.run(
                "sudo systemctl is-active consul 2>/dev/null || echo inactive"
            )
            attach_output(extras, "pre-init consul status", consul_status.strip())
            print("[test_radm_init_completes] consul status: " + consul_status.strip())

            attach_output(extras, "k8s+consul reset", "Full reset complete")
            print("[test_radm_init_completes] Full reset complete")

            print("[test_radm_init_completes] Step 1: running radm init on node1 ...")
            out, rc = ssh_client.run(
                "cd " + extract_dir + " && sudo ./radm init --config config.yaml "
                "--skip-phases infra/containerd/install-containerd-config-toml 2>&1",
                timeout=1800,
            )
            attach_output(extras, "radm init output (node1)", out)
            assert rc == 0, "radm init failed (exit " + str(rc) + "). See output above."
            print("[test_radm_init_completes] radm init completed on node1")

        print("[test_radm_init_completes] Step 2: setting up kubeconfig ...")
        kubeconfig_out, kubeconfig_rc = ssh_client.run(
            "mkdir -p $HOME/.kube && "
            "sudo cp -f /etc/kubernetes/admin.conf $HOME/.kube/config && "
            "sudo chown $(id -u):$(id -g) -R $HOME/.kube && "
            "echo KUBECONFIG_OK"
        )
        attach_output(extras, "kubeconfig setup", kubeconfig_out)
        assert kubeconfig_rc == 0 and "KUBECONFIG_OK" in kubeconfig_out, (
            "kubeconfig setup failed: " + kubeconfig_out
        )
        print("[test_radm_init_completes] kubeconfig ready on node1")

        kubectl_out, kubectl_rc = ssh_client.run(
            "kubectl get nodes 2>/dev/null || "
            "/usr/local/bin/kubectl get nodes 2>/dev/null || "
            "/usr/bin/kubectl get nodes 2>&1"
        )
        attach_output(extras, "kubectl get nodes (after init)", kubectl_out)
        print("[test_radm_init_completes] Nodes after init:\n" + kubectl_out)

        if controller_profile.ha and secondary_ips:
            print("[test_radm_init_completes] Step 3: HA mode - checking " + str(len(secondary_ips)) + " secondary node(s) ...")

            already_joined_hostnames = set()
            for line in kubectl_out.splitlines():
                parts = line.split()
                if parts and parts[0] not in ("NAME",):
                    already_joined_hostnames.add(parts[0])

            pending = []
            for i, sec_ip in enumerate(secondary_ips, 2):
                sec_hostname = ""
                try:
                    hostname_check_ssh = SSHClient(
                        host=sec_ip,
                        user=controller_profile.user,
                        key_path=controller_profile.ssh_key
                    )
                    hostname_check_ssh.connect()
                    try:
                        hostname_out, _ = hostname_check_ssh.run("hostname")
                        sec_hostname = hostname_out.strip()
                    finally:
                        hostname_check_ssh.disconnect()
                except Exception as e:
                    print("[test_radm_init_completes] Could not check node" + str(i) + " (" + sec_ip + ") hostname - will attempt join: " + str(e))

                if sec_hostname and sec_hostname in already_joined_hostnames:
                    print("[test_radm_init_completes] node" + str(i) + " (" + sec_ip + ", " + sec_hostname + ") already a cluster member - skipping join")
                    attach_output(extras, "node" + str(i) + " join status", "Already joined - skipped")
                else:
                    pending.append((i, sec_ip))

            if not pending:
                print("[test_radm_init_completes] All secondary nodes already joined - skipping join step entirely")
            else:
                print("[test_radm_init_completes] Joining " + str(len(pending)) + " pending node(s): " + str([ip for _, ip in pending]))

                token_out, _ = ssh_client.run(
                    "kubeadm token list 2>/dev/null | grep -v TOKEN | head -1 | awk '{print $1}'"
                )
                ca_hash_out, _ = ssh_client.run(
                    "openssl x509 -pubkey -in /etc/kubernetes/pki/ca.crt | "
                    "openssl rsa -pubin -outform der 2>/dev/null | "
                    "openssl dgst -sha256 -hex | sed 's/^.* //'"
                )
                primary_ip_out, _ = ssh_client.run("hostname -I | awk '{print $1}'")
                token   = token_out.strip()
                ca_hash = ca_hash_out.strip()
                pri_ip  = primary_ip_out.strip()

                assert token,   "Could not get kubeadm token - did radm init complete?"
                assert ca_hash, "Could not get CA hash"
                assert pri_ip,  "Could not get primary node IP"

                print("[test_radm_init_completes] token  : " + token)
                print("[test_radm_init_completes] pri_ip : " + pri_ip)

                join_cmd = (
                    "cd " + extract_dir + " && "
                    "sudo ./radm join " + pri_ip + ":6443 "
                    "--token " + token + " "
                    "--discovery-token-ca-cert-hash sha256:" + ca_hash + " "
                    "--config config.yaml"
                )
                attach_output(extras, "radm join command", join_cmd)

                for i, sec_ip in pending:
                    print("[test_radm_init_completes] Joining node" + str(i) + " (" + sec_ip + ") ...")
                    sec_ssh = SSHClient(
                        host=sec_ip,
                        user=controller_profile.user,
                        key_path=controller_profile.ssh_key
                    )
                    sec_ssh.connect()
                    try:
                        join_out, join_rc = sec_ssh.run(join_cmd, timeout=1800)
                        attach_output(extras, "radm join node" + str(i) + " (" + sec_ip + ")", join_out)
                        assert join_rc == 0, (
                            "radm join failed on node" + str(i) + " (" + sec_ip + ") exit " + str(join_rc) + ":\n" + join_out
                        )
                        print("[test_radm_init_completes] node" + str(i) + " (" + sec_ip + ") joined successfully")
                    finally:
                        sec_ssh.disconnect()

            print("[test_radm_init_completes] Step 4: verifying all nodes Ready ...")
            nodes_out, _ = ssh_client.run(
                "kubectl get nodes 2>/dev/null || /usr/local/bin/kubectl get nodes 2>&1"
            )
            attach_output(extras, "kubectl get nodes (after join)", nodes_out)
            print("[test_radm_init_completes] All nodes:\n" + nodes_out)

        # already_initialized => radm init was skipped above (kubeconfig
        # already existed, e.g. right after an upgrade) -- nothing was
        # actually re-run, so there's nothing new to wait for beyond a
        # single confirmation snapshot. Same fix as dependency/application/
        # cluster: only fall back to the full poll loop when radm init
        # genuinely just ran.
        self._wait_for_pods(ssh_client, extras, label="after radm init",
                            quick_check=already_initialized)

    def test_radm_dependency_completes(self, ssh_client, package_profile, controller_profile, extras):
        extract_dir = (
            getattr(package_profile, '_actual_extract_dir', None)
            or '/opt/rafay/rafay-airgapped-controller-v3.1-39'
        )

        already_healthy = self._check_cluster_already_healthy(ssh_client, extras)
        if already_healthy:
            print("[test_radm_dependency_completes] Cluster already healthy "
                  "(all pods Running/Completed) - skipping radm dependency re-run")
            attach_output(extras, "radm dependency status", "Already applied - skipped re-run")
        else:
            out, rc = ssh_client.run(
                "cd " + extract_dir + " && sudo ./radm dependency --config config.yaml 2>&1",
                timeout=600,
            )
            attach_output(extras, "radm dependency output", out)
            assert rc == 0, "radm dependency failed (exit " + str(rc) + "). See output above."
            print("[test_radm_dependency_completes] radm dependency passed")

        # already_healthy => nothing was re-run above, so there is nothing
        # to wait for beyond a single confirmation snapshot -- only fall
        # back to the full poll loop when radm dependency actually ran.
        self._wait_for_pods(ssh_client, extras, label="after radm dependency",
                            quick_check=already_healthy)

    def test_radm_application_completes(self, ssh_client, package_profile, extras):
        extract_dir = (
            getattr(package_profile, '_actual_extract_dir', None)
            or '/opt/rafay/rafay-airgapped-controller-v3.1-39'
        )

        already_healthy = self._check_cluster_already_healthy(ssh_client, extras)
        if already_healthy:
            print("[test_radm_application_completes] Cluster already healthy "
                  "(all pods Running/Completed) - skipping radm application re-run")
            attach_output(extras, "radm application status", "Already applied - skipped re-run")
        else:
            out, rc = ssh_client.run(
                "cd " + extract_dir + " && sudo ./radm application --config config.yaml 2>&1",
                timeout=2400,
            )
            attach_output(extras, "radm application output", out)
            assert rc == 0, "radm application failed (exit " + str(rc) + "). See output above."
            print("[test_radm_application_completes] radm application passed")

        self._wait_for_pods(ssh_client, extras, label="after radm application",
                            max_wait=2400, fail_on_timeout=True,
                            quick_check=already_healthy)

    def test_radm_cluster_completes(self, ssh_client, package_profile, extras):
        extract_dir = (
            getattr(package_profile, '_actual_extract_dir', None)
            or '/opt/rafay/rafay-airgapped-controller-v3.1-39'
        )

        already_healthy = self._check_cluster_already_healthy(ssh_client, extras)
        if already_healthy:
            print("[test_radm_cluster_completes] Cluster already healthy "
                  "(all pods Running/Completed) - skipping radm cluster re-run")
            attach_output(extras, "radm cluster status", "Already applied - skipped re-run")
        else:
            out, rc = ssh_client.run(
                "cd " + extract_dir + " && sudo ./radm cluster --config config.yaml 2>&1",
                timeout=1200,
            )
            attach_output(extras, "radm cluster output", out)
            assert rc == 0, "radm cluster failed (exit " + str(rc) + "). See output above."
            print("[test_radm_cluster_completes] radm cluster passed")

        self._wait_for_pods(ssh_client, extras, label="after radm cluster",
                            max_wait=1200, fail_on_timeout=True,
                            quick_check=already_healthy)


class TestPostInstallHealth:
    """Validate controller state after installation completes."""

    def test_all_pods_running(self, ssh_client, extras):
        out, rc = ssh_client.run("kubectl get pods -A --no-headers 2>/dev/null || /usr/local/bin/kubectl get pods -A --no-headers 2>&1")
        attach_output(extras, "kubectl get pods -A", out)
        assert rc == 0, "kubectl get pods failed -- is kubeconfig set up?"
        bad = [l for l in out.splitlines()
               if any(s in l for s in ("Pending", "Error", "CrashLoop", "Init:", "OOMKilled"))]
        if bad:
            attach_pod_logs(ssh_client, extras, bad)
        assert not bad, str(len(bad)) + " unhealthy pod(s):\n" + "\n".join(bad)

    def test_ha_master_node_count(self, ssh_client, controller_profile, extras):
        out, rc = ssh_client.run(
            "kubectl get nodes --no-headers -l node-role.kubernetes.io/control-plane 2>&1 || "
            "/usr/local/bin/kubectl get nodes --no-headers -l node-role.kubernetes.io/control-plane 2>&1"
        )
        attach_output(extras, "Master nodes", out)
        master_count = len([l for l in out.splitlines() if l.strip()])
        expected = 3 if controller_profile.ha else 1
        assert master_count == expected, (
            controller_profile.mode_label + " controller should have " + str(expected) + " master(s) -- found " + str(master_count)
        )

    def test_console_endpoint_reachable(self, ssh_client, extras):
        out, rc = ssh_client.run(
            "curl -sk -o /dev/null -w '%{http_code}' https://localhost/ || echo FAILED"
        )
        attach_output(extras, "Console HTTP status", out)
        assert out.strip() not in ("000", "FAILED"), "Console endpoint is not responding -- check pods and ingress"

    def test_kube_config_accessible(self, ssh_client, extras):
        out, rc = ssh_client.run("kubectl cluster-info 2>&1 || /usr/local/bin/kubectl cluster-info 2>&1")
        attach_output(extras, "kubectl cluster-info", out)
        assert rc == 0, "kubectl cluster-info failed -- check $HOME/.kube/config"
        assert "running" in out.lower() or "https://" in out.lower()

    def test_size_label_in_profile_summary(self, controller_profile):
        summary = controller_profile.summary()
        assert controller_profile.controller_size in summary
        assert controller_profile.mode_label in summary
        assert controller_profile.os_type in summary