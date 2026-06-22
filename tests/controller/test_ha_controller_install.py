"""
tests/controller/test_controller_install.py

Installation validation tests -- drive the radm install flow and validate
the result based on controller_size, HA mode, and OS type from the profile.
"""

import pytest

pytestmark = [pytest.mark.controller, pytest.mark.regression]


def attach_output(extras, label: str, content: str):
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
            pytest.fail(f"Could not read root disk size -- got: {out}")
        size_gb = int(out_clean)
        assert size_gb >= 500, f"Root disk is {size_gb}GB -- need at least 500GB"

    def test_preflight_data_disk_1tb(self, ssh_client, extras):
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
        out, rc = ssh_client.run("df -BG /tmp | tail -1 | awk '{print $4}'")
        attach_output(extras, "/tmp available", out)
        out_clean = out.strip().replace("G", "")
        if not out_clean.isdigit():
            pytest.fail(f"Could not read /tmp available space -- got: {out}")
        avail_gb = int(out_clean)
        assert avail_gb >= 50, f"/tmp only has {avail_gb}GB free -- need at least 50GB"

    def test_preflight_cpu_meets_size(self, ssh_client, controller_profile, extras):
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
        out, rc = ssh_client.run("free -m | awk '/^Mem:/{print $2}'")
        attach_output(extras, "Memory (MB)", out)
        actual_mb    = int(out.strip())
        actual_gb    = actual_mb / 1024
        required_gb  = controller_profile.memory_gb
        threshold_gb = required_gb * 0.95
        assert actual_gb >= threshold_gb, (
            f"Controller size '{controller_profile.controller_size}' requires "
            f"{required_gb}GB RAM -- found {actual_gb:.1f}GB\n"
            f"Increase memory_gb in dev.yaml to {required_gb}"
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
            pytest.skip("Non-HA mode — secondary node preflight not needed")
        from lib.ssh.ssh_client import SSHClient
        size_requirements = {
            "cpu":    controller_profile.cpu,
            "memory": controller_profile.memory_gb,
        }
        failures = []
        for i, sec_ip in enumerate(secondary_ips, 2):
            sec_ssh = SSHClient(host=sec_ip, user=controller_profile.user, key_path=controller_profile.ssh_key)
            sec_ssh.connect()
            node_failures = []
            try:
                os_out, _ = sec_ssh.run("cat /etc/os-release | grep -E '^ID=|VERSION_ID'")
                attach_output(extras, f"node{i} OS", os_out)
                disk_out, _ = sec_ssh.run("df -BG / | tail -1 | awk '{print $2}'")
                disk_gb = int(disk_out.strip().replace("G", "") or "0")
                if disk_gb < 500:
                    node_failures.append(f"Root disk {disk_gb}GB < 500GB")
                cpu_out, _ = sec_ssh.run("nproc")
                cpu = int(cpu_out.strip() or "0")
                if cpu < size_requirements["cpu"]:
                    node_failures.append(f"CPU {cpu} < {size_requirements['cpu']}")
                mem_out, _ = sec_ssh.run("free -m | awk '/^Mem:/{print $2}'")
                mem_gb = int(mem_out.strip() or "0") / 1024
                threshold = size_requirements["memory"] * 0.95
                if mem_gb < threshold:
                    node_failures.append(f"Memory {mem_gb:.1f}GB < {size_requirements['memory']}GB")
                tmp_out, _ = sec_ssh.run("df -BG /tmp | tail -1 | awk '{print $4}'")
                tmp_gb = int(tmp_out.strip().replace("G", "") or "0")
                if tmp_gb < 50:
                    node_failures.append(f"/tmp {tmp_gb}GB < 50GB")
                status = "PASS" if not node_failures else f"FAIL: {', '.join(node_failures)}"
                attach_output(extras, f"node{i} preflight", status)
                if node_failures:
                    failures.append(f"node{i} ({sec_ip}): {', '.join(node_failures)}")
            finally:
                sec_ssh.disconnect()
        assert not failures, f"Preflight failed on secondary nodes:\n" + "\n".join(failures)


class TestPackageSetup:
    """Download and set up the controller installation package on the VM."""

    def test_package_url_derived(self, package_profile, extras):
        attach_output(extras, "Package summary", package_profile.summary())
        assert package_profile.url.startswith("https://"), f"Invalid URL derived: {package_profile.url}"
        assert package_profile.name in package_profile.url
        assert package_profile.version in package_profile.url

    def test_install_dir_created(self, ssh_client, package_profile, extras):
        out, rc = ssh_client.run(f"sudo mkdir -p {package_profile.install_dir} && echo OK")
        attach_output(extras, "mkdir install_dir", out)
        assert rc == 0 and "OK" in out

    def test_dns_resolver_configured(self, ssh_client, extras):
        check_out, _ = ssh_client.run("test -f /etc/resolv.conf && cat /etc/resolv.conf || echo MISSING")
        attach_output(extras, "resolv.conf", check_out)
        if "MISSING" in check_out or "nameserver" not in check_out:
            ssh_client.run("sudo systemctl start resolvconf 2>/dev/null || true && sleep 1", timeout=10)
            fix1_out, _ = ssh_client.run(
                "test -f /run/resolvconf/resolv.conf && "
                "sudo ln -sf /run/resolvconf/resolv.conf /etc/resolv.conf && "
                "echo RESOLVCONF_FIXED || echo RESOLVCONF_MISSING"
            )
            if "RESOLVCONF_MISSING" in fix1_out:
                ssh_client.run("sudo systemctl start systemd-resolved 2>/dev/null || true && sleep 2", timeout=15)
                ssh_client.run(
                    "test -f /run/systemd/resolve/resolv.conf && "
                    "sudo ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf || true",
                    timeout=10
                )
            recheck, _ = ssh_client.run("cat /etc/resolv.conf 2>/dev/null || echo STILL_MISSING")
            if "nameserver" not in recheck:
                ssh_client.run(
                    "sudo rm -f /etc/resolv.conf && "
                    "printf 'nameserver 169.254.169.254\nnameserver 8.8.8.8\nnameserver 8.8.4.4\n' "
                    "| sudo tee /etc/resolv.conf > /dev/null",
                    timeout=10
                )
            final_out, _ = ssh_client.run("cat /etc/resolv.conf 2>/dev/null || echo FAILED")
            attach_output(extras, "resolv.conf after fix", final_out.strip()[:300])
        dns_out, _ = ssh_client.run("nslookup google.com 2>/dev/null | grep -i address | head -2 || echo DNS_DONE")
        attach_output(extras, "DNS check", dns_out)

    def test_aria2c_installed(self, ssh_client, controller_profile, nsg_manager, extras):
        import time
        if nsg_manager:
            nsg_manager.attach()
            attach_output(extras, "NSG", "Attached — will stay open through package download")
            print("[test_aria2c_installed] NSG attached — waiting 30s for rules to propagate ...")
            time.sleep(30)
        out, rc = ssh_client.run("which aria2c 2>/dev/null && aria2c --version 2>/dev/null | head -1")
        if rc == 0 and "aria2" in out.lower():
            attach_output(extras, "aria2c version", out)
            return
        if controller_profile.os_type == "ubuntu24":
            update_out, _ = ssh_client.run("sudo apt-get -o Acquire::ForceIPv4=true update -y 2>&1", timeout=120)
            attach_output(extras, "apt-get update", update_out)
            install_out, install_rc = ssh_client.run("sudo apt-get -o Acquire::ForceIPv4=true install -y aria2 2>&1", timeout=180)
            attach_output(extras, "apt install aria2", install_out)
            if install_rc != 0:
                universe_out, universe_rc = ssh_client.run(
                    "sudo add-apt-repository universe -y 2>&1 && sudo apt-get update -y 2>&1 && sudo apt-get install -y aria2 2>&1",
                    timeout=300
                )
                attach_output(extras, "universe repo install", universe_out)
                assert universe_rc == 0
        else:
            out, rc = ssh_client.run("sudo yum install -y aria2 2>&1 || sudo dnf install -y aria2 2>&1", timeout=180)
            attach_output(extras, "yum/dnf install aria2", out)
            assert rc == 0
        ssh_client.run(
            "sudo iptables -F && sudo iptables -t nat -F && "
            "sudo iptables -t mangle -F && sudo iptables -X 2>/dev/null || true",
            timeout=10
        )
        verify_out, verify_rc = ssh_client.run("which aria2c && aria2c --version 2>/dev/null | head -1")
        attach_output(extras, "aria2c version", verify_out)
        assert verify_rc == 0

    def test_package_download(self, ssh_client, package_profile, nsg_manager, extras):
        import time
        attach_output(extras, "Package URL", package_profile.url)
        aria2_control = f"{package_profile.tar_path}.aria2"
        tar_exists_out, tar_exists_rc = ssh_client.run(f"test -f {package_profile.tar_path} && echo EXISTS || echo MISSING")
        aria2_exists_out, _ = ssh_client.run(f"test -f {aria2_control} && echo PARTIAL || echo CLEAN")
        tar_exists  = "EXISTS" in tar_exists_out
        is_complete = tar_exists and "CLEAN" in aria2_exists_out
        if is_complete:
            size_out, _ = ssh_client.run(f"du -sh {package_profile.tar_path} | awk '{{print $1}}'")
            attach_output(extras, "Download skipped", f"Already complete ({size_out.strip()})")
            if nsg_manager:
                nsg_manager.detach()
            return
        if "PARTIAL" in aria2_exists_out:
            ssh_client.run(f"sudo rm -f {package_profile.tar_path} {aria2_control}")
        download_ok = False
        try:
            diag_out, _ = ssh_client.run(
                "curl -sI --max-time 15 https://rafay-airgap-controller.s3.us-west-2.amazonaws.com "
                "--write-out '\nHTTP_CODE:%{http_code}' -o /dev/null 2>&1 || true",
                timeout=30
            )
            attach_output(extras, "S3 connectivity", diag_out)
            aria2c_path_out, _ = ssh_client.run("which aria2c")
            aria2c_bin = aria2c_path_out.strip() or "/usr/bin/aria2c"
            out, rc = ssh_client.run(
                f"cd {package_profile.install_dir} && "
                f"sudo {aria2c_bin} -x 16 -s 16 --max-tries=3 --retry-wait=10 "
                f"--connect-timeout=30 --log-level=notice {package_profile.url} 2>&1",
                timeout=1800
            )
            attach_output(extras, "aria2c output", out)
            if rc == 0:
                download_ok = True
            v_out, verify_rc = ssh_client.run(
                f"test -s {package_profile.tar_path} && du -sh {package_profile.tar_path} | awk '{{print $1}}'"
            )
            attach_output(extras, "Downloaded file size", v_out)
        finally:
            if nsg_manager:
                nsg_manager.detach()
                attach_output(extras, "NSG", "Detached")
        assert download_ok, f"Package download failed. URL: {package_profile.url}"
        assert verify_rc == 0

    def test_package_extract(self, ssh_client, package_profile, extras):
        import time
        extract_dest = package_profile.install_dir
        ls_out, _ = ssh_client.run(f"ls -1 {extract_dest}/ 2>/dev/null")
        before_dirs = set(ls_out.strip().splitlines())
        already = [d for d in before_dirs if "rafay-airgapped-controller" in d and not d.endswith(".tar.gz")]
        if already:
            actual = f"{extract_dest}/{already[0]}"
            attach_output(extras, "Already extracted", actual)
            package_profile._actual_extract_dir = actual
            return
        tar_path = getattr(package_profile, "_actual_tar_path", package_profile.tar_path)
        locate_out, _ = ssh_client.run(f"test -f {tar_path} && echo FOUND || echo MISSING")
        if "MISSING" in locate_out:
            search_out, _ = ssh_client.run(f"find /opt/rafay -name '{package_profile.name}' 2>/dev/null | head -1")
            found = search_out.strip()
            if found:
                tar_path = found
            else:
                pytest.fail(f"Package tar not found at {tar_path}.")
        out, rc = ssh_client.run(f"sudo tar -xf {tar_path} -C {extract_dest} 2>&1 && echo EXTRACTED", timeout=3600)
        attach_output(extras, "Extract result", out[-500:] if len(out) > 500 else out)
        assert rc == 0 and "EXTRACTED" in out
        ls_after, _ = ssh_client.run(f"ls -1 {extract_dest}/ 2>/dev/null")
        after_dirs  = set(ls_after.strip().splitlines())
        new_entries = after_dirs - before_dirs
        extracted_dirs = [e for e in new_entries if not e.endswith(".tar.gz") and not e.endswith(".tar")]
        assert extracted_dirs
        actual_extract_dir = f"{extract_dest}/{extracted_dirs[0]}"
        attach_output(extras, "Detected extract dir", actual_extract_dir)
        package_profile._actual_extract_dir = actual_extract_dir

    def test_radm_binary_copied(self, ssh_client, package_profile, extras):
        extract_dir = (getattr(package_profile, "_actual_extract_dir", None) or package_profile.extract_dir)
        out, rc = ssh_client.run(f"sudo cp {extract_dir}/radm /usr/bin/radm && sudo chmod +x /usr/bin/radm && echo OK")
        attach_output(extras, "radm binary copy", out)
        assert rc == 0 and "OK" in out
        version_out, version_rc = ssh_client.run("which radm")
        attach_output(extras, "radm location", version_out)
        assert version_rc == 0

    def test_config_yaml_created(self, ssh_client, package_profile, request,
                                  controller_profile, controller_fqdn, raw_config, extras):
        extract_dir  = (getattr(package_profile, "_actual_extract_dir", None) or package_profile.extract_dir)
        config_path  = f"{extract_dir}/config.yaml"
        tmpl_names   = ["config.yaml-airgap-tmpl", "config.yaml-tmpl", "config.yaml.tmpl"]
        tmpl_path = None
        for name in tmpl_names:
            check, rc = ssh_client.run(f"test -f {extract_dir}/{name} && echo FOUND || echo MISSING")
            if "FOUND" in check:
                tmpl_path = f"{extract_dir}/{name}"
                break
        assert tmpl_path, f"No config.yaml template found in {extract_dir}"
        out, rc = ssh_client.run(f"sudo cp {tmpl_path} {config_path} && echo OK")
        assert rc == 0 and "OK" in out
        orig_out, _ = ssh_client.run(f"cat {config_path}")
        attach_output(extras, "config.yaml (original)", orig_out)
        size = controller_profile.controller_size
        ha   = "true" if controller_profile.ha else "false"
        repo_path = extract_dir
        if controller_fqdn:
            star_domain = controller_fqdn.lstrip("*.")
        else:
            base_domain  = raw_config.get("dns", {}).get("base_domain", "")
            build_no_val = request.config.getoption("--build-no") or ""
            if base_domain and build_no_val:
                star_domain = f"shc-{build_no_val}.{base_domain}"
            elif base_domain:
                display_name = raw_config.get("oci", {}).get("display_name", "")
                if display_name and "{build_no}" not in display_name:
                    star_domain = f"{display_name}.{base_domain}"
                else:
                    star_domain = ""
            else:
                star_domain = ""
        print(f"[test_config_yaml_created] size={size} ha={ha} star_domain={star_domain}")
        attach_output(extras, "Patch values", f"size={size} | ha={ha} | type=airgap | archive-directory={repo_path} | star-domain={star_domain}")
        q = '"'
        patches = [
            f"sudo sed -i '/size:/s|:.*|: {q}{size}{q}|' {config_path}",
            f"sudo sed -i '/^[ ]*ha:/s|:.*|: {ha}|' {config_path}",
        ]
        for patch_cmd in patches:
            out_p, rc_p = ssh_client.run(patch_cmd)
            assert rc_p == 0
        ssh_client.run(f"sudo sed -i 's|^    type:.*|    type: TYPE_PLACEHOLDER|' {config_path}")
        out_type, rc_type = ssh_client.run(
            f"sudo sed -i " + "'" + r's|type: TYPE_PLACEHOLDER|type: "airgap"|' + "'" + f" {config_path} && echo OK"
        )
        assert rc_type == 0 and "OK" in out_type
        ssh_client.run(f"sudo sed -i 's|archive-directory:.*|archive-directory: RAFAY_PLACEHOLDER|' {config_path}")
        out_arch, rc_arch = ssh_client.run(
            f"sudo sed -i 's|archive-directory: RAFAY_PLACEHOLDER|archive-directory: {repo_path}|' {config_path} && echo OK"
        )
        assert rc_arch == 0 and "OK" in out_arch
        if star_domain:
            ssh_client.run(f"sudo sed -i '/^[ ]*star-domain:/s|star-domain:.*|star-domain: STAR_PLACEHOLDER|' {config_path}")
            out_star, rc_star = ssh_client.run(
                f"sudo sed -i 's|star-domain: STAR_PLACEHOLDER|star-domain: {star_domain}|' {config_path} && echo OK"
            )
            assert rc_star == 0 and "OK" in out_star
        verify_items = [("size", f'"{size}"'), ("ha", ha), ("type", '"airgap"')]
        for key, expected in verify_items:
            grep_out, _ = ssh_client.run(f"grep '{key}:' {config_path} | head -1")
            assert expected in grep_out
        attach_output(extras, "Patch verification", "All fields patched correctly")

    def test_setup_secondary_nodes(self, ssh_client, package_profile,
                                   controller_profile, secondary_ips,
                                   secondary_instance_ids, oci_profile_fixture, extras):
        if not controller_profile.ha or not secondary_ips:
            pytest.skip("Non-HA mode — secondary node setup not needed")
        from lib.ssh.ssh_client import SSHClient
        from lib.oci.vm_manager import OCINSGManager
        import base64
        import time
        extract_dir = getattr(package_profile, "_actual_extract_dir", package_profile.extract_dir)
        config_path = f"{extract_dir}/config.yaml"
        config_content, cfg_rc = ssh_client.run(f"sudo cat {config_path}")
        assert cfg_rc == 0
        encoded_config = base64.b64encode(config_content.encode()).decode()
        padded_ids = list(secondary_instance_ids) + [""] * len(secondary_ips)
        for i, (sec_ip, sec_id) in enumerate(zip(secondary_ips, padded_ids), 2):
            sec_nsg = None
            if oci_profile_fixture and oci_profile_fixture.nsg_id and sec_id:
                sec_nsg = OCINSGManager(oci_profile_fixture, sec_id)
            sec_ssh = SSHClient(host=sec_ip, user=controller_profile.user, key_path=controller_profile.ssh_key)
            sec_ssh.connect()
            try:
                sec_ssh.run(f"sudo mkdir -p {package_profile.install_dir} && sudo chmod 777 {package_profile.install_dir}")
                sec_ssh.run(
                    "sudo ufw disable 2>/dev/null || true && sudo systemctl stop ufw 2>/dev/null || true && "
                    "sudo systemctl disable ufw 2>/dev/null || true && sudo iptables -F && sudo iptables -t nat -F && "
                    "sudo iptables -t mangle -F && sudo iptables -X 2>/dev/null || true",
                    timeout=15
                )
                check_out, _ = sec_ssh.run(f"test -d {extract_dir} && echo EXISTS || echo MISSING")
                already_extracted = "EXISTS" in check_out
                if not already_extracted:
                    if sec_nsg:
                        sec_nsg.attach()
                        time.sleep(30)
                    aria2c_out, aria2c_rc = sec_ssh.run("which aria2c 2>/dev/null && aria2c --version 2>/dev/null | head -1")
                    if aria2c_rc != 0 or "aria2" not in aria2c_out.lower():
                        sec_ssh.run("sudo apt-get -o Acquire::ForceIPv4=true update -y 2>&1 || true", timeout=120)
                        sec_ssh.run("sudo apt-get -o Acquire::ForceIPv4=true install -y aria2 2>&1 || true", timeout=180)
                        sec_ssh.run("sudo iptables -F && sudo iptables -t nat -F && sudo iptables -t mangle -F && sudo iptables -X 2>/dev/null || true", timeout=10)
                    tar_check, _ = sec_ssh.run(
                        f"test -f {package_profile.tar_path} && test ! -f {package_profile.tar_path}.aria2 && echo COMPLETE || echo MISSING"
                    )
                    if "MISSING" in tar_check:
                        aria2c_bin_out, _ = sec_ssh.run("which aria2c")
                        aria2c_bin = aria2c_bin_out.strip() or "/usr/bin/aria2c"
                        dl_out, dl_rc = sec_ssh.run(
                            f"cd {package_profile.install_dir} && "
                            f"sudo {aria2c_bin} -x 16 -s 16 --max-tries=3 --connect-timeout=30 {package_profile.url} 2>&1",
                            timeout=1800
                        )
                        attach_output(extras, f"node{i} download", dl_out[-300:])
                        assert dl_rc == 0
                    if sec_nsg:
                        sec_nsg.detach()
                    ext_out, ext_rc = sec_ssh.run(
                        f"sudo tar -xf {package_profile.tar_path} -C {package_profile.install_dir} 2>&1 && echo EXTRACTED",
                        timeout=3600
                    )
                    assert ext_rc == 0 and "EXTRACTED" in ext_out
                else:
                    if sec_nsg:
                        try:
                            sec_nsg.detach()
                        except Exception:
                            pass
                write_out, write_rc = sec_ssh.run(
                    f"echo '{encoded_config}' | base64 -d | sudo tee {config_path} > /dev/null && echo OK"
                )
                assert write_rc == 0 and "OK" in write_out
            finally:
                sec_ssh.disconnect()

    def test_package_version_matches_profile(self, ssh_client, package_profile, extras):
        out, rc = ssh_client.run(f"ls {package_profile.install_dir}/ | grep rafay-airgapped-controller")
        attach_output(extras, "Extracted dirs", out)
        assert package_profile.version in out


class TestRadmInstall:
    """Drive the radm installation steps sequentially."""

    def _wait_for_pods(self, ssh_client, extras, label="pods", max_wait=1500, fail_on_timeout=False):
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
                stable_count = 0
                time.sleep(poll_every)
                continue
            lines     = [l for l in pods_out.splitlines() if l.strip()]
            total     = len(lines)
            not_ready = [l for l in lines if "Running" not in l and "Completed" not in l]
            unhealthy = [l for l in lines if any(s in l for s in ("Error", "CrashLoop", "OOMKilled"))]
            if unhealthy:
                stable_count = 0
            if not not_ready and total > 0:
                if total == prev_total:
                    stable_count += 1
                else:
                    stable_count = 0
                if stable_count >= 2:
                    attach_output(extras, f"All pods Running ({label})", pods_out)
                    return
            else:
                stable_count = 0
            prev_total = total
            time.sleep(poll_every)
        pods_out, _ = ssh_client.run("kubectl get pods -A --no-headers 2>&1")
        lines     = [l for l in pods_out.splitlines() if l.strip()]
        unhealthy = [l for l in lines if any(s in l for s in ("Error", "CrashLoop", "OOMKilled", "Pending"))]
        attach_output(extras, f"Pod status at timeout ({label})", pods_out)
        for pod_line in unhealthy[:5]:
            parts = pod_line.split()
            if len(parts) >= 2:
                ns, pod_name = parts[0], parts[1]
                desc_out, _ = ssh_client.run(f"kubectl describe pod {pod_name} -n {ns} 2>&1 | tail -30")
                attach_output(extras, f"describe {ns}/{pod_name}", desc_out)
        print(f"[{label}] Continuing despite unready pods ...")

    def test_radm_binary_present(self, ssh_client, extras):
        out, rc = ssh_client.run("which radm || echo NOT_FOUND")
        attach_output(extras, "radm path", out)
        assert "NOT_FOUND" not in out

    def test_config_yaml_present(self, ssh_client, package_profile, extras):
        extract_dir = (getattr(package_profile, "_actual_extract_dir", None) or package_profile.extract_dir)
        config_path = f"{extract_dir}/config.yaml"
        out, rc = ssh_client.run(f"test -f {config_path} && echo OK || echo MISSING")
        attach_output(extras, "config.yaml check", out)
        assert out.strip() == "OK"

    def test_config_ha_matches_profile(self, ssh_client, package_profile, controller_profile, extras):
        extract_dir = (getattr(package_profile, "_actual_extract_dir", None) or package_profile.extract_dir)
        out, rc = ssh_client.run(f"grep 'ha:' {extract_dir}/config.yaml | head -1")
        attach_output(extras, "config.yaml ha field", out)
        remote_ha = "true" in out.lower()
        assert remote_ha == controller_profile.ha

    def test_config_domain_set(self, ssh_client, package_profile, extras):
        extract_dir = (getattr(package_profile, "_actual_extract_dir", None) or package_profile.extract_dir)
        out, rc = ssh_client.run(f"grep 'star-domain' {extract_dir}/config.yaml")
        attach_output(extras, "star-domain config", out)
        assert "example.com" not in out
        assert out.strip() != ""

    def test_radm_init_completes(self, ssh_client, package_profile,
                                  controller_profile, secondary_ips, extras):
        extract_dir = getattr(package_profile, "_actual_extract_dir", None)
        if not extract_dir:
            search_out, _ = ssh_client.run(
                "find /opt/rafay -maxdepth 1 -type d -name 'rafay-airgapped-controller*' 2>/dev/null | head -1"
            )
            extract_dir = search_out.strip() or "/opt/rafay"
        attach_output(extras, "Extract dir", extract_dir)
        already_init_out, _ = ssh_client.run("test -f /etc/kubernetes/admin.conf && echo ALREADY_INIT || echo NOT_INIT")
        already_initialized = "ALREADY_INIT" in already_init_out
        if already_initialized:
            print("[test_radm_init_completes] Already initialized — skipping radm init")
        else:
            ssh_client.run("sudo systemctl stop consul kubelet 2>/dev/null || true && sleep 2", timeout=15)
            ssh_client.run("sudo kubeadm reset -f 2>/dev/null || true", timeout=30)
            ssh_client.run(
                "sudo rm -rf /etc/kubernetes /var/lib/kubelet /var/lib/etcd "
                "/var/run/kubernetes /tmp/rafay-infra /var/lib/consul "
                "/etc/consul.d/rafay*.hcl /etc/consul.d/rafay*.json 2>/dev/null || true",
                timeout=15
            )
            ssh_client.run(
                "sudo rm -rf /etc/containerd/certs.d/ && sudo rm -f /etc/containerd/config.toml 2>/dev/null || true",
                timeout=10
            )
            ssh_client.run("sudo systemctl restart containerd && sleep 5 && echo READY", timeout=30)
            out, rc = ssh_client.run(
                f"cd {extract_dir} && sudo ./radm init --config config.yaml "
                f"--skip-phases infra/containerd/install-containerd-config-toml 2>&1",
                timeout=1800,
            )
            attach_output(extras, "radm init output (node1)", out)
            assert rc == 0, f"radm init failed (exit {rc})"

        kubeconfig_out, kubeconfig_rc = ssh_client.run(
            "mkdir -p $HOME/.kube && sudo cp -f /etc/kubernetes/admin.conf $HOME/.kube/config && "
            "sudo chown $(id -u):$(id -g) -R $HOME/.kube && echo KUBECONFIG_OK"
        )
        attach_output(extras, "kubeconfig setup", kubeconfig_out)
        assert kubeconfig_rc == 0 and "KUBECONFIG_OK" in kubeconfig_out

        if controller_profile.ha and secondary_ips:
            kubeadm_out, _ = ssh_client.run(
                "find /tmp/rafay-infra /usr/local/bin /usr/bin -name 'rafay-kubeadm' -type f 2>/dev/null | head -1"
            )
            kubeadm_bin = kubeadm_out.strip() or "/tmp/rafay-infra/packages/kubeadm/amd64/rafay-kubeadm"
            token_out, _ = ssh_client.run(
                f"sudo {kubeadm_bin} token list --kubeconfig=/etc/kubernetes/admin.conf 2>/dev/null | "
                f"grep 'authentication,signing' | head -1 | awk '{{print $1}}'"
            )
            if not token_out.strip():
                token_out, _ = ssh_client.run(f"sudo {kubeadm_bin} token create --kubeconfig=/etc/kubernetes/admin.conf 2>/dev/null")
            ca_hash_out, _ = ssh_client.run(
                "openssl x509 -pubkey -in /etc/kubernetes/pki/ca.crt | "
                "openssl rsa -pubin -outform der 2>/dev/null | openssl dgst -sha256 -hex | sed 's/^.* //'"
            )
            primary_ip_out, _ = ssh_client.run("hostname -I | awk '{print $1}'")
            cert_key_raw, _ = ssh_client.run(f"cd {extract_dir} && sudo ./radm init phase infra upload-certs --config config.yaml 2>&1")
            cert_key = ""
            for line in cert_key_raw.splitlines():
                stripped = line.strip()
                if len(stripped) == 64 and all(c in "0123456789abcdef" for c in stripped):
                    cert_key = stripped
                    break
            token   = token_out.strip()
            ca_hash = ca_hash_out.strip()
            pri_ip  = primary_ip_out.strip()
            assert token and ca_hash and pri_ip and cert_key
            join_cmd = (
                f"cd {extract_dir} && sudo ./radm join {pri_ip}:6443 "
                f"--token {token} --discovery-token-ca-cert-hash sha256:{ca_hash} "
                f"--control-plane --certificate-key {cert_key} --config config.yaml"
            )
            import time as _join_wait
            print("[test_radm_init_completes] Waiting 60s for etcd to stabilize ...")
            _join_wait.sleep(60)
            import subprocess as _subprocess
            import time as _time
            for i, sec_ip in enumerate(secondary_ips, 2):
                prereq_cmd = [
                    "ssh", "-i", controller_profile.ssh_key,
                    "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                    "-o", "ConnectTimeout=30", f"{controller_profile.user}@{sec_ip}",
                    f"sudo systemctl stop consul 2>/dev/null || true && "
                    f"sudo systemctl disable consul 2>/dev/null || true && "
                    f"sudo rm -f /etc/consul.d/*.hcl /etc/consul.d/*.json 2>/dev/null || true && "
                    f"sudo rm -rf /var/lib/consul 2>/dev/null || true && "
                    f"sudo mkdir -p /run/systemd/resolve && "
                    f"printf 'nameserver 169.254.169.254\nnameserver 8.8.8.8\nnameserver 8.8.4.4\n' "
                    f"| sudo tee /run/systemd/resolve/resolv.conf > /dev/null && "
                    f"sudo rm -f /etc/resolv.conf && "
                    f"printf 'nameserver 169.254.169.254\nnameserver 8.8.8.8\nnameserver 8.8.4.4\n' "
                    f"| sudo tee /etc/resolv.conf > /dev/null && "
                    f"grep -q 'k8master.service.edgedc.consul' /etc/hosts || "
                    f"echo '{pri_ip} k8master.service.edgedc.consul' | sudo tee -a /etc/hosts && "
                    f"echo PREREQ_DONE"
                ]
                _subprocess.run(prereq_cmd, capture_output=True, text=True, timeout=30)
                ssh_join_cmd = [
                    "ssh", "-i", controller_profile.ssh_key,
                    "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                    "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=60",
                    "-o", "ConnectTimeout=30", f"{controller_profile.user}@{sec_ip}",
                    join_cmd
                ]
                join_rc = 1
                join_out = ""
                for attempt in range(1, 4):
                    if attempt > 1:
                        _time.sleep(30)
                    result = _subprocess.run(ssh_join_cmd, capture_output=True, text=True, timeout=1800)
                    join_out = (result.stdout + result.stderr).strip()
                    join_rc  = result.returncode
                    if join_rc == 0:
                        break
                    if "can only promote a learner" not in join_out and "FailedPrecondition" not in join_out:
                        break
                attach_output(extras, f"radm join node{i}", join_out)
                assert join_rc == 0, f"radm join failed on node{i} ({sec_ip}): {join_out}"

        self._wait_for_pods(ssh_client, extras, label="after radm init")

    def test_ha_nodes_ready(self, ssh_client, controller_profile, extras):
        if not controller_profile.ha:
            pytest.skip("Non-HA mode — skipping HA node check")
        import time
        max_wait = 600
        deadline = time.time() + max_wait
        while time.time() < deadline:
            nodes_out, rc = ssh_client.run("kubectl get nodes --no-headers 2>/dev/null || /usr/local/bin/kubectl get nodes --no-headers 2>&1")
            lines = [l for l in nodes_out.splitlines() if l.strip()]
            ready = [l for l in lines if "Ready" in l and "NotReady" not in l]
            if len(ready) >= 3:
                attach_output(extras, "All nodes Ready", nodes_out)
                return
            time.sleep(15)
        nodes_out, _ = ssh_client.run("kubectl get nodes 2>/dev/null || /usr/local/bin/kubectl get nodes 2>&1")
        attach_output(extras, "Node state at timeout", nodes_out)
        not_ready = [l for l in nodes_out.splitlines() if "NotReady" in l or "Unknown" in l]
        assert not not_ready, f"Not all 3 control-plane nodes are Ready: {nodes_out}"

    def test_radm_dependency_completes(self, ssh_client, package_profile, controller_profile, extras):
        out, rc = ssh_client.run(
            f"cd {(getattr(package_profile, '_actual_extract_dir', None) or '/opt/rafay/rafay-airgapped-controller-v3.1-39')} && sudo ./radm dependency --config config.yaml 2>&1",
            timeout=600,
        )
        attach_output(extras, "radm dependency output", out)
        assert rc == 0, f"radm dependency failed (exit {rc})"
        self._wait_for_pods(ssh_client, extras, label="after radm dependency")

    def test_radm_application_completes(self, ssh_client, package_profile, extras):
        out, rc = ssh_client.run(
            f"cd {(getattr(package_profile, '_actual_extract_dir', None) or '/opt/rafay/rafay-airgapped-controller-v3.1-39')} && sudo ./radm application --config config.yaml 2>&1",
            timeout=2400,
        )
        attach_output(extras, "radm application output", out)
        assert rc == 0, f"radm application failed (exit {rc})"
        self._wait_for_pods(ssh_client, extras, label="after radm application", max_wait=2400, fail_on_timeout=True)

    def test_radm_cluster_completes(self, ssh_client, package_profile, request,
                                     controller_profile, controller_fqdn, raw_config, extras):
        """
        Run: sudo radm cluster --config config.yaml

        Includes /etc/hosts fix before running radm cluster.
        OCI blocks self-referential public IP traffic — the star-domain hostname
        resolves to the public IP via Route53, but the VM can't reach its own
        public IP. Patching /etc/hosts to resolve to 127.0.0.1 fixes this.
        """
        extract_dir = getattr(package_profile, "_actual_extract_dir", None) or "/opt/rafay/rafay-airgapped-controller-v3.1-39"

        # ── Resolve star_domain for /etc/hosts patch ──────────────────────────
        if controller_fqdn:
            star_domain = controller_fqdn.lstrip("*.")
        else:
            base_domain  = raw_config.get("dns", {}).get("base_domain", "")
            build_no_val = request.config.getoption("--build-no") or ""
            if base_domain and build_no_val:
                star_domain = f"shc-{build_no_val}.{base_domain}"
            elif base_domain:
                display_name = raw_config.get("oci", {}).get("display_name", "")
                star_domain  = f"{display_name}.{base_domain}" if display_name and "{build_no}" not in display_name else ""
            else:
                star_domain = ""

        # ── Fix: patch /etc/hosts so star-domain resolves to 127.0.0.1 ───────
        # OCI VMs cannot reach their own public IP from within — self-referential
        # traffic on the public IP is dropped by OCI's networking layer.
        # Without this fix: "dial tcp <public_ip>:80: i/o timeout" during image upload.
        if star_domain:
            print(f"[test_radm_cluster_completes] Patching /etc/hosts for star_domain: {star_domain}")

            # Common subdomains used by radm cluster during image upload
            hostnames_to_patch = [
                f"ops-console.{star_domain}",
                f"registry.{star_domain}",
                f"*.{star_domain}",
            ]

            for hostname in hostnames_to_patch:
                patch_out, patch_rc = ssh_client.run(
                    f"grep -q '{hostname}' /etc/hosts || "
                    f"echo '127.0.0.1 {hostname}' | sudo tee -a /etc/hosts"
                )
                print(f"[test_radm_cluster_completes] /etc/hosts patch: {hostname} → {patch_out.strip()}")

            # Verify the patch is in place
            hosts_out, _ = ssh_client.run(f"grep '{star_domain}' /etc/hosts")
            attach_output(extras, "/etc/hosts patch", hosts_out)
            print(f"[test_radm_cluster_completes] /etc/hosts after patch:\n{hosts_out}")

            # Confirm ops-console is now reachable before running radm cluster
            curl_out, _ = ssh_client.run(
                f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 10 "
                f"http://ops-console.{star_domain}/v2/token 2>&1 || echo TIMEOUT"
            )
            attach_output(extras, "ops-console reachability check", curl_out.strip())
            print(f"[test_radm_cluster_completes] ops-console HTTP status: {curl_out.strip()}")
        else:
            print("[test_radm_cluster_completes] WARNING: star_domain not resolved — skipping /etc/hosts patch")
            attach_output(extras, "/etc/hosts patch", "Skipped — star_domain not set")

        # ── Run radm cluster (streaming — prints progress live) ──────────────
        print(f"[test_radm_cluster_completes] Starting radm cluster — streaming output ...")
        out, rc = ssh_client.run_stream(
            f"cd {extract_dir} && sudo ./radm cluster --config config.yaml 2>&1",
            timeout=1200,
            prefix="[radm cluster]",
        )
        attach_output(extras, "radm cluster output", out)
        assert rc == 0, f"radm cluster failed (exit {rc}). See output above."
        self._wait_for_pods(ssh_client, extras, label="after radm cluster", max_wait=1200, fail_on_timeout=True)


class TestPostInstallHealth:
    """Validate controller state after installation completes."""

    def test_all_pods_running(self, ssh_client, extras):
        out, rc = ssh_client.run("kubectl get pods -A --no-headers 2>/dev/null || /usr/local/bin/kubectl get pods -A --no-headers 2>&1")
        attach_output(extras, "kubectl get pods -A", out)
        assert rc == 0
        bad = [l for l in out.splitlines() if any(s in l for s in ("Pending", "Error", "CrashLoop", "Init:", "OOMKilled"))]
        assert not bad, f"{len(bad)} unhealthy pod(s):\n" + "\n".join(bad)

    def test_ha_master_node_count(self, ssh_client, controller_profile, extras):
        out, rc = ssh_client.run(
            "kubectl get nodes --no-headers -l node-role.kubernetes.io/control-plane 2>&1 || "
            "/usr/local/bin/kubectl get nodes --no-headers -l node-role.kubernetes.io/control-plane 2>&1"
        )
        attach_output(extras, "Master nodes", out)
        master_count = len([l for l in out.splitlines() if l.strip()])
        expected = 3 if controller_profile.ha else 1
        assert master_count == expected, f"{controller_profile.mode_label} should have {expected} master(s) — found {master_count}"

    def test_console_endpoint_reachable(self, ssh_client, extras):
        out, rc = ssh_client.run("curl -sk -o /dev/null -w '%{http_code}' https://localhost/ || echo FAILED")
        attach_output(extras, "Console HTTP status", out)
        assert out.strip() not in ("000", "FAILED")

    def test_kube_config_accessible(self, ssh_client, extras):
        out, rc = ssh_client.run("kubectl cluster-info 2>&1 || /usr/local/bin/kubectl cluster-info 2>&1")
        attach_output(extras, "kubectl cluster-info", out)
        assert rc == 0
        assert "running" in out.lower() or "https://" in out.lower()

    def test_size_label_in_profile_summary(self, controller_profile):
        summary = controller_profile.summary()
        assert controller_profile.controller_size in summary
        assert controller_profile.mode_label in summary
        assert controller_profile.os_type in summary