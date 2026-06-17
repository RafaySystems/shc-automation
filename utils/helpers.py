"""Shared helpers — wait_until poller, assert utilities."""

import time
from typing import Callable


def wait_until(
    condition: Callable[[], bool],
    timeout: int = 300,
    interval: int = 15,
    description: str = "condition",
) -> bool:
    """
    Poll condition() every `interval` seconds until it returns True
    or `timeout` seconds elapse.

    Returns True if condition was met, False if timed out.

    Example:
        wait_until(lambda: "Running" in ssh.run("kubectl get pods -A")[0],
                   timeout=300, description="all pods Running")
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False


def assert_exit_code(rc: int, expected: int = 0, cmd: str = ""):
    """Assert that a command exited with the expected code."""
    assert rc == expected, (
        f"Command {'`' + cmd + '`' if cmd else ''} "
        f"exited with {rc}, expected {expected}"
    )


def assert_in_output(needle: str, haystack: str, label: str = "output"):
    """Assert that `needle` appears in command output."""
    assert needle in haystack, (
        f"Expected '{needle}' not found in {label}:\n{haystack}"
    )
