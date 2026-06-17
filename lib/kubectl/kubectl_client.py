"""kubectl client — thin subprocess wrapper used by kubectl-layer tests."""

import subprocess
import shlex
from pathlib import Path


class KubectlClient:
    def __init__(self, kubeconfig_path: str = "~/.kube/config", namespace: str = "rafay-system"):
        self.kubeconfig = str(Path(kubeconfig_path).expanduser())
        self.namespace = namespace

    def _run(self, args: list[str], timeout: int = 60) -> tuple[str, int]:
        cmd = ["kubectl", "--kubeconfig", self.kubeconfig] + args
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        output = result.stdout.strip()
        if result.stderr.strip():
            output += "\n" + result.stderr.strip()
        return output, result.returncode

    def get_pods(self, namespace: str = None) -> tuple[str, int]:
        ns = namespace or self.namespace
        return self._run(["get", "pods", "-n", ns, "--no-headers"])

    def get_pods_all(self) -> tuple[str, int]:
        return self._run(["get", "pods", "-A", "--no-headers"])

    def get_nodes(self) -> tuple[str, int]:
        return self._run(["get", "nodes", "--no-headers"])

    def cluster_info(self) -> tuple[str, int]:
        return self._run(["cluster-info"])

    def apply(self, manifest_path: str) -> tuple[str, int]:
        return self._run(["apply", "-f", manifest_path])

    def wait(self, resource: str, condition: str, timeout: int = 120) -> tuple[str, int]:
        return self._run(
            ["wait", resource, f"--for=condition={condition}", f"--timeout={timeout}s"],
            timeout=timeout + 10,
        )
