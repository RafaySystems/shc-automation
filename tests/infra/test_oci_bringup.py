"""
tests/infra/test_oci_bringup.py

Tests for OCI VM provisioning and readiness.
These run BEFORE controller installation tests — they validate that the VM
was launched correctly and is reachable over SSH.

Markers: infra, smoke
"""

import time
import pytest
from fixtures.oci_fixtures import oci_profile, oci_vm_manager, oci_instance

pytestmark = [pytest.mark.infra, pytest.mark.smoke]


def attach_output(extra, label: str, content: str):
    import pytest_html
    block = f"<pre style='font-size:12px;white-space:pre-wrap'>{content}</pre>"
    extra.append(pytest_html.extras.html(f"<b>{label}</b>{block}"))


# ── OCI provisioning checks ───────────────────────────────────────────────────

class TestOCIProvisioning:
    """Validate that the OCI VM was launched with the correct configuration."""

    def test_instance_is_running(self, oci_instance, oci_vm_manager, extra):
        """Instance must be in RUNNING state after fixture setup."""
        if oci_instance["id"] is None:
            pytest.skip("SKIP_OCI_CREATE=1 — no instance to check")

        state = oci_vm_manager.get_instance_state(oci_instance["id"])
        attach_output(extra, "Instance state", state)
        assert state == "RUNNING", f"Expected RUNNING, got {state}"

    def test_public_ip_assigned(self, oci_instance, extra):
        """Instance must have a public IP address."""
        ip = oci_instance["public_ip"]
        attach_output(extra, "Public IP", ip or "NONE")
        assert ip, "No public IP assigned to the instance"
        # Basic IP format sanity check
        parts = ip.split(".")
        assert len(parts) == 4, f"IP '{ip}' does not look like a valid IPv4 address"

    def test_display_name_matches_profile(self, oci_instance, oci_vm_manager, extra):
        """Instance display name in OCI must match the profile display_name."""
        if oci_instance["id"] is None:
            pytest.skip("SKIP_OCI_CREATE=1 — skipping name check")

        resp = oci_vm_manager.compute_client.get_instance(oci_instance["id"])
        actual_name = resp.data.display_name
        attach_output(extra, "Display name", actual_name)
        profile_name_prefix = oci_instance["profile"].display_name.split("{")[0]
        assert actual_name.startswith(profile_name_prefix), (
            f"Display name '{actual_name}' does not start with '{profile_name_prefix}'"
        )

    def test_tags_applied(self, oci_instance, oci_vm_manager, extra):
        """Freeform tags from dev.yaml must be present on the instance."""
        if oci_instance["id"] is None:
            pytest.skip("SKIP_OCI_CREATE=1 — skipping tag check")

        expected_tags = oci_instance["profile"].tags
        if not expected_tags:
            pytest.skip("No custom tags configured in dev.yaml — skipping")

        resp = oci_vm_manager.compute_client.get_instance(oci_instance["id"])
        actual_tags = resp.data.freeform_tags or {}
        attach_output(extra, "Applied tags", str(actual_tags))

        for key, value in expected_tags.items():
            assert key in actual_tags, f"Tag key '{key}' missing from instance"
            assert actual_tags[key] == str(value), (
                f"Tag '{key}': expected '{value}', got '{actual_tags[key]}'"
            )

    def test_shape_matches_profile(self, oci_instance, oci_vm_manager, extra):
        """Instance shape must match what was requested in dev.yaml."""
        if oci_instance["id"] is None:
            pytest.skip("SKIP_OCI_CREATE=1 — skipping shape check")

        resp = oci_vm_manager.compute_client.get_instance(oci_instance["id"])
        actual_shape = resp.data.shape
        expected_shape = oci_instance["profile"].shape
        attach_output(extra, "Shape", actual_shape)
        assert actual_shape == expected_shape, (
            f"Shape mismatch: expected '{expected_shape}', got '{actual_shape}'"
        )

    def test_subnet_matches_profile(self, oci_instance, oci_vm_manager, extra):
        """The instance VNIC must be attached to the subnet declared in dev.yaml."""
        if oci_instance["id"] is None:
            pytest.skip("SKIP_OCI_CREATE=1 — skipping subnet check")

        import oci as oci_sdk
        profile = oci_instance["profile"]
        attachments = oci_sdk.pagination.list_call_get_all_results(
            oci_vm_manager.compute_client.list_vnic_attachments,
            compartment_id=profile.compartment_id,
            instance_id=oci_instance["id"],
        ).data

        assert attachments, "No VNIC attachments found"
        vnic = oci_vm_manager.network_client.get_vnic(attachments[0].vnic_id).data
        attach_output(extra, "VNIC subnet OCID", vnic.subnet_id)
        assert vnic.subnet_id == profile.subnet_id, (
            f"Subnet mismatch: expected {profile.subnet_id}, got {vnic.subnet_id}"
        )


# ── SSH reachability checks ───────────────────────────────────────────────────

class TestVMSSHReachability:
    """
    Validate that the VM is reachable over SSH after bringup.
    These tests create a fresh SSH connection using the oci_instance IP,
    independent of the main ssh_client fixture, so they can run standalone.
    """

    def _get_ssh(self, oci_instance, controller_profile):
        """Open a one-off SSH connection to the newly provisioned VM."""
        from lib.ssh.ssh_client import SSHClient
        client = SSHClient(
            host=oci_instance["public_ip"],
            user=controller_profile.user,
            key_path=controller_profile.ssh_key,
        )
        client.connect()
        return client

    def test_ssh_login_succeeds(self, oci_instance, controller_profile, extra):
        """Must be able to SSH into the VM using the key in dev.yaml."""
        client = self._get_ssh(oci_instance, controller_profile)
        out, rc = client.run("echo SSH_OK")
        client.disconnect()
        attach_output(extra, "SSH echo test", out)
        assert rc == 0 and "SSH_OK" in out, f"SSH login failed: {out}"

    def test_os_type_matches_image(self, oci_instance, controller_profile, extra):
        """
        Confirm that the OS running on the VM matches the os_type declared
        in dev.yaml. Catches image/config mismatches early.
        """
        client = self._get_ssh(oci_instance, controller_profile)
        out, rc = client.run("cat /etc/os-release")
        client.disconnect()
        attach_output(extra, "/etc/os-release", out)

        os_type = controller_profile.os_type
        if os_type == "ubuntu24":
            assert "ubuntu" in out.lower()
            assert "24.04" in out
        elif os_type == "rhel8":
            assert "rhel" in out.lower() or "red hat" in out.lower()
            assert 'VERSION_ID="8' in out
        elif os_type == "rhel9":
            assert "rhel" in out.lower() or "red hat" in out.lower()
            assert 'VERSION_ID="9' in out

    def test_hostname_reachable(self, oci_instance, controller_profile, extra):
        """hostname command must return a non-empty value — confirms basic shell access."""
        client = self._get_ssh(oci_instance, controller_profile)
        out, rc = client.run("hostname")
        client.disconnect()
        attach_output(extra, "Hostname", out)
        assert rc == 0 and out.strip() != "", "Could not read hostname from VM"

    def test_data_disk_attached(self, oci_instance, controller_profile, extra):
        """
        /data mount must exist on the VM.
        Installation prerequisite: 1 TB data disk attached as /data volume.
        """
        client = self._get_ssh(oci_instance, controller_profile)
        out, rc = client.run("mountpoint /data 2>&1 || df /data 2>&1")
        client.disconnect()
        attach_output(extra, "/data mountpoint check", out)
        assert rc == 0, "/data is not mounted — attach and format the data disk before install"
