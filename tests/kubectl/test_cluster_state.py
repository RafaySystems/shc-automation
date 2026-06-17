"""
tests/kubectl/test_cluster_state.py

Cluster state validation using kubectl.
Requires kubeconfig to be set up on the machine running pytest.
"""

import pytest

pytestmark = [pytest.mark.kubectl, pytest.mark.regression]


def attach_output(extra, label: str, content: str):
    import pytest_html
    block = f"<pre style='font-size:12px;white-space:pre-wrap'>{content}</pre>"
    extra.append(pytest_html.extras.html(f"<b>{label}</b>{block}"))


class TestClusterState:

    def test_nodes_all_ready(self, ssh_client, extra):
        """Every node in the cluster must be in Ready state."""
        out, rc = ssh_client.run("kubectl get nodes --no-headers")
        attach_output(extra, "Nodes", out)
        assert rc == 0
        not_ready = [l for l in out.splitlines() if "NotReady" in l]
        assert not not_ready, "Some nodes are NotReady:\n" + "\n".join(not_ready)

    def test_no_crashloop_pods(self, ssh_client, extra):
        """No pod should be stuck in CrashLoopBackOff."""
        out, rc = ssh_client.run("kubectl get pods -A --no-headers")
        attach_output(extra, "All pods", out)
        crash = [l for l in out.splitlines() if "CrashLoop" in l]
        assert not crash, f"{len(crash)} pod(s) in CrashLoopBackOff:\n" + "\n".join(crash)

    def test_rafay_system_pods_running(self, ssh_client, extra):
        """All pods in rafay-system namespace must be Running or Completed."""
        out, rc = ssh_client.run("kubectl get pods -n rafay-system --no-headers")
        attach_output(extra, "rafay-system pods", out)
        assert rc == 0, "Could not list rafay-system pods"
        bad = [l for l in out.splitlines()
               if any(s in l for s in ("Pending", "Error", "CrashLoop", "Init:"))]
        assert not bad, "Unhealthy pods in rafay-system:\n" + "\n".join(bad)

    def test_kube_system_pods_running(self, ssh_client, extra):
        """All pods in kube-system must be Running or Completed."""
        out, rc = ssh_client.run("kubectl get pods -n kube-system --no-headers")
        attach_output(extra, "kube-system pods", out)
        bad = [l for l in out.splitlines()
               if any(s in l for s in ("Pending", "Error", "CrashLoop"))]
        assert not bad, "Unhealthy pods in kube-system:\n" + "\n".join(bad)

    def test_persistent_volumes_bound(self, ssh_client, extra):
        """All PersistentVolumes must be in Bound state."""
        out, rc = ssh_client.run("kubectl get pv --no-headers 2>&1")
        attach_output(extra, "PersistentVolumes", out)
        if "No resources found" in out:
            pytest.skip("No PersistentVolumes found — skipping")
        unbound = [l for l in out.splitlines() if "Bound" not in l and l.strip()]
        assert not unbound, "Unbound PVs:\n" + "\n".join(unbound)
