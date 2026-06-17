"""
fixtures/oci_fixtures.py

Exposes the Terraform-provisioned VM to infra tests via oci_instance fixture.
VM provisioning and NSG attachment are handled by ssh_client in conftest.py.
"""

import pytest
from lib.oci.vm_manager import load_oci_profile


@pytest.fixture(scope="session")
def oci_profile(raw_config):
    return load_oci_profile(raw_config)


@pytest.fixture(scope="session")
def oci_vm_manager(oci_profile):
    from lib.oci.vm_manager import OCIVMManager
    return OCIVMManager(oci_profile)


@pytest.fixture(scope="session")
def oci_instance(request, ssh_client, oci_profile):
    """
    Expose the Terraform-provisioned VM to infra tests.
    ssh_client dependency ensures Terraform runs first.
    Reads instance_id and public_ip stored on session by conftest.py.
    """
    instance_id = getattr(request.session, "_tf_instance_id", None)
    public_ip   = getattr(request.session, "_tf_public_ip", None)

    if instance_id is None:
        static_ip = public_ip or request.session._raw_config.get(
            "controller", {}
        ).get("ip", "")
        yield {"id": None, "public_ip": static_ip, "profile": oci_profile}
        return

    yield {"id": instance_id, "public_ip": public_ip, "profile": oci_profile}
    # Teardown handled by ssh_client → terraform destroy
