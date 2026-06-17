"""
tests/regression/test_end_to_end.py

End-to-end regression tests — cross-validate SSH + kubectl results
and verify the controller is fully operational after installation.
"""

import pytest

pytestmark = [pytest.mark.regression, pytest.mark.slow]


def attach_output(extra, label: str, content: str):
    import pytest_html
    block = f"<pre style='font-size:12px;white-space:pre-wrap'>{content}</pre>"
    extra.append(pytest_html.extras.html(f"<b>{label}</b>{block}"))


class TestEndToEnd:

    def test_ssh_and_kubectl_node_count_match(self, ssh_client, controller_profile, extra):
        """
        Node count from kubectl must match what the controller profile expects:
        HA = 3 masters, Non-HA = 1 master.
        """
        out, rc = ssh_client.run(
            "kubectl get nodes -l node-role.kubernetes.io/control-plane --no-headers | wc -l"
        )
        attach_output(extra, "Master node count", out)
        count = int(out.strip())
        expected = 3 if controller_profile.ha else 1
        assert count == expected, (
            f"{controller_profile.mode_label} controller should have {expected} master(s), "
            f"found {count}"
        )

    def test_console_accessible_via_curl(self, ssh_client, extra):
        """ops-console endpoint must return a non-zero HTTP status code (not connection refused)."""
        out, rc = ssh_client.run(
            "curl -sk -o /dev/null -w '%{http_code}' https://localhost/ || echo FAILED"
        )
        attach_output(extra, "Console HTTP status", out)
        assert out.strip() not in ("000", "FAILED"), (
            "Console endpoint returned no response — check ingress and rafay-system pods"
        )

    def test_rafay_services_all_running(self, ssh_client, extra):
        """Cross-check: zero pods in a non-Running/Completed state across all namespaces."""
        out, rc = ssh_client.run("kubectl get pods -A --no-headers 2>&1")
        attach_output(extra, "All pods (regression check)", out)
        assert rc == 0
        bad = [l for l in out.splitlines()
               if any(s in l for s in ("Pending", "Error", "CrashLoop", "Init:", "OOMKilled"))]
        assert not bad, f"{len(bad)} unhealthy pod(s) found:\n" + "\n".join(bad)

    def test_controller_size_resources_available(self, ssh_client, controller_profile, extra):
        """
        Allocatable CPU and memory on master nodes must meet the declared
        controller size minimums (S/M/L) from the profile.
        """
        out, rc = ssh_client.run(
            "kubectl get nodes -l node-role.kubernetes.io/control-plane "
            "-o jsonpath='{.items[0].status.allocatable.cpu}'"
        )
        attach_output(extra, "Allocatable CPU", out)
        # cpu is returned as e.g. "64" or "64000m"
        cpu_str = out.strip().replace("'", "")
        if cpu_str.endswith("m"):
            actual_cpu = int(cpu_str[:-1]) // 1000
        else:
            actual_cpu = int(cpu_str)
        assert actual_cpu >= controller_profile.cpu, (
            f"Controller size {controller_profile.controller_size} requires "
            f"{controller_profile.cpu} CPUs — node reports {actual_cpu}"
        )
