"""Paramiko SSH wrapper used by all controller tests."""

import time
import socket
import paramiko
from pathlib import Path


class SSHClient:
    def __init__(self, host: str, user: str, key_path: str, port: int = 22, timeout: int = 30):
        self.host     = host
        self.user     = user
        self.key_path = str(Path(key_path).expanduser())
        self.port     = port
        self.timeout  = timeout
        self._client  = None

    def connect(self, retries: int = 10, retry_interval: int = 15):
        """
        Connect with retries — OCI VMs take 30-60s after RUNNING
        before SSH daemon is ready to accept connections.

        retries       : how many attempts before giving up (default 10 = ~2.5 min)
        retry_interval: seconds between attempts (default 15)
        """
        key_path = Path(self.key_path)
        if not key_path.exists():
            raise FileNotFoundError(
                f"SSH private key not found: {key_path}\n"
                f"Check the ssh_key path in dev.yaml."
            )

        # Load key — try RSA first, then Ed25519, then ECDSA
        pkey = self._load_private_key(key_path)

        last_error = None
        for attempt in range(1, retries + 1):
            try:
                print(f"[SSHClient] Connecting to {self.user}@{self.host} "
                      f"(attempt {attempt}/{retries}) ...")
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(
                    hostname=self.host,
                    username=self.user,
                    pkey=pkey,
                    port=self.port,
                    timeout=self.timeout,
                    look_for_keys=False,   # only use the key we explicitly loaded
                    allow_agent=False,     # don't use ssh-agent
                )
                self._client = client
                print(f"[SSHClient] Connected to {self.host} ✓")
                return

            except (paramiko.ssh_exception.NoValidConnectionsError,
                    paramiko.ssh_exception.SSHException,
                    socket.timeout,
                    socket.error,
                    OSError) as e:
                last_error = e
                print(f"[SSHClient] Attempt {attempt} failed: {e}")
                if attempt < retries:
                    print(f"[SSHClient] Retrying in {retry_interval}s ...")
                    time.sleep(retry_interval)

        raise ConnectionError(
            f"Could not SSH to {self.host} after {retries} attempts.\n"
            f"Last error: {last_error}\n"
            f"Check:\n"
            f"  1. Security list / firewall allows port 22 from your IP\n"
            f"  2. SSH key matches the public key injected into the VM\n"
            f"  3. Username is correct (ubuntu for Ubuntu, opc for Oracle Linux)"
        )

    def run(self, command: str, timeout: int = 120):
        """Run a command and return (stdout_str, exit_code)."""
        if not self._client:
            raise RuntimeError("SSH not connected — call connect() first")
        stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace").strip()
        err = stderr.read().decode("utf-8", errors="replace").strip()
        return (out + ("\n" + err if err else "")), exit_code

    def disconnect(self):
        if self._client:
            self._client.close()
            self._client = None

    # ── private ───────────────────────────────────────────────────────────────

    def _load_private_key(self, key_path: Path) -> paramiko.PKey:
        """
        Try loading the private key as RSA, Ed25519, then ECDSA.
        Raises a clear error if none work.
        """
        key_types = [
            ("RSA",     paramiko.RSAKey),
            ("Ed25519", paramiko.Ed25519Key),
            ("ECDSA",   paramiko.ECDSAKey),
        ]
        for name, key_class in key_types:
            try:
                key = key_class.from_private_key_file(str(key_path))
                print(f"[SSHClient] Loaded {name} private key: {key_path}")
                return key
            except paramiko.ssh_exception.SSHException:
                continue
            except Exception:
                continue

        raise ValueError(
            f"Could not load private key from {key_path}.\n"
            f"Tried RSA, Ed25519, ECDSA — none matched.\n"
            f"Make sure ssh_key in dev.yaml points to the private key file (not .pub)."
        )