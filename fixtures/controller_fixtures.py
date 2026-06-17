"""
fixtures/controller_fixtures.py

Additional controller fixtures beyond what's in root conftest.py.
Import these in tests that need module-scoped or function-scoped SSH connections.
"""

import pytest
from lib.ssh.ssh_client import SSHClient
from lib.kubectl.kubectl_client import KubectlClient


@pytest.fixture(scope="module")
def ssh_client_module(controller_profile):
    """Module-scoped SSH client — one connection per test file."""
    client = SSHClient(
        host=controller_profile.ip,
        user=controller_profile.user,
        key_path=controller_profile.ssh_key,
    )
    client.connect()
    yield client
    client.disconnect()


@pytest.fixture(scope="function")
def ssh_client_fn(controller_profile):
    """Function-scoped SSH client — fresh connection per test (slower, more isolated)."""
    client = SSHClient(
        host=controller_profile.ip,
        user=controller_profile.user,
        key_path=controller_profile.ssh_key,
    )
    client.connect()
    yield client
    client.disconnect()
