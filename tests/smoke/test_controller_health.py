"""
tests/smoke/test_controller_health.py

Fast sanity checks — SSH reachability, service status, disk space.
Run these first before any heavier test suite.
"""

import pytest

pytestmark = [pytest.mark.smoke, pytest.mark.controller]


def attach_output(extra, label: str, content: str):
    import pytest_html
    block = f"<pre style='font-size:12px;white-space:pre-wrap'>{content}</pre>"
    extra.append(pytest_html.extras.html(f"<b>{label}</b>{block}"))


class TestControllerHealth:

    def test_ssh_reachable(self, ssh_client, extra):
        """Controller must respond to SSH and execute a basic command."""
        out, rc = ssh_client.run("echo ALIVE")
        attach_output(extra, "SSH echo", out)
        assert rc == 0 and "ALIVE" in out

    def test_all_pods_running(self, ssh_client, extra):
        """No pods should be in Pending / Error / CrashLoopBackOff state."""
        out, rc = ssh_client.run("kubectl get pods -A --no-headers 2>&1")
        attach_output(extra, "kubectl get pods -A", out)
        assert rc == 0, "kubectl get pods failed"
        bad = [l for l in out.splitlines()
               if any(s in l for s in ("Pending", "Error", "CrashLoop", "Init:", "OOMKilled"))]
        assert not bad, f"{len(bad)} unhealthy pod(s):\n" + "\n".join(bad)

    def test_disk_space_root(self, ssh_client, extra):
        """Root filesystem must have at least 20% free."""
        out, rc = ssh_client.run("df / | tail -1 | awk '{print $5}' | tr -d '%'")
        attach_output(extra, "Root disk usage %", out)
        used_pct = int(out.strip())
        assert used_pct < 80, f"Root disk is {used_pct}% full — less than 20% free"

    def test_node_ready(self, ssh_client, extra):
        """All Kubernetes nodes must show Ready status."""
        out, rc = ssh_client.run("kubectl get nodes --no-headers 2>&1")
        attach_output(extra, "kubectl get nodes", out)
        assert rc == 0
        not_ready = [l for l in out.splitlines() if "NotReady" in l]
        assert not not_ready, f"Nodes not ready:\n" + "\n".join(not_ready)

    def test_controller_profile_summary(self, controller_profile):
        """Smoke-check that controller_profile loaded correctly from config."""
        s = controller_profile.summary()
        assert controller_profile.ip in s
        assert controller_profile.controller_size in s
        assert controller_profile.os_type in s
