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
        """Connect with retries."""
        key_path = Path(self.key_path)
        if not key_path.exists():
            raise FileNotFoundError(
                f"SSH private key not found: {key_path}\n"
                f"Check the ssh_key path in dev.yaml."
            )

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
                    look_for_keys=False,
                    allow_agent=False,
                )
                # Keepalive every 30s — prevents connection drop during
                # long-running commands like radm init/join (~15 min)
                client.get_transport().set_keepalive(30)
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
        """
        Run a command and return (stdout_str, exit_code).

        Uses polling so long-running commands (radm init/join ~15 min)
        don't get killed by a socket read timeout.
        No PTY — avoids login shell hang on some OCI Ubuntu VMs.
        Sudo works without PTY since OCI ubuntu user has NOPASSWD sudoers.
        """
        if not self._client:
            raise RuntimeError("SSH not connected — call connect() first")

        transport = self._client.get_transport()
        if not transport or not transport.is_active():
            raise RuntimeError(f"SSH transport not active for {self.host}")

        channel = transport.open_session()
        channel.set_combine_stderr(True)
        channel.exec_command(command)

        poll_interval = 0.5
        deadline      = time.time() + timeout
        output_chunks = []

        while True:
            while channel.recv_ready():
                chunk = channel.recv(4096).decode("utf-8", errors="replace")
                output_chunks.append(chunk)

            if channel.exit_status_ready():
                while channel.recv_ready():
                    chunk = channel.recv(4096).decode("utf-8", errors="replace")
                    output_chunks.append(chunk)
                break

            if time.time() > deadline:
                channel.close()
                output = "".join(output_chunks)
                raise TimeoutError(
                    f"Command timed out after {timeout}s on {self.host}.\n"
                    f"Command: {command[:200]}\n"
                    f"Last output: {output[-500:]}"
                )

            time.sleep(poll_interval)

        exit_code = channel.recv_exit_status()
        channel.close()

        return "".join(output_chunks).strip(), exit_code

    def run_stream(self, command: str, timeout: int = 120, prefix: str = ""):
        """
        Run a command and stream output line by line to stdout in real time.
        Useful for long-running commands like radm cluster (317 image uploads)
        where you want to see progress as it happens instead of waiting for completion.

        Returns (full_output_str, exit_code) — same signature as run() so it's
        a drop-in replacement whenever you need live output.

        Args:
            command : shell command to run on the remote VM
            timeout : max seconds to wait (default 120 — increase for long commands)
            prefix  : optional label printed before each line e.g. "[radm cluster]"

        Usage:
            out, rc = ssh_client.run_stream(
                "cd /opt/rafay/... && sudo ./radm cluster --config config.yaml",
                timeout=1200,
                prefix="[radm cluster]"
            )

        Output example (printed live as it runs):
            [radm cluster] Uploading images...
            [radm cluster] [##                ] 5%   16/317
            [radm cluster] [####              ] 10%  32/317
            ...
        """
        if not self._client:
            raise RuntimeError("SSH not connected — call connect() first")

        transport = self._client.get_transport()
        if not transport or not transport.is_active():
            raise RuntimeError(f"SSH transport not active for {self.host}")

        channel = transport.open_session()
        channel.set_combine_stderr(True)
        channel.exec_command(command)

        poll_interval  = 0.1          # tighter poll for more responsive output
        deadline       = time.time() + timeout
        output_chunks  = []
        line_buffer    = ""           # incomplete line carried between chunks
        label          = f"{prefix} " if prefix else ""

        while True:
            # Drain all available data
            while channel.recv_ready():
                chunk = channel.recv(4096).decode("utf-8", errors="replace")
                output_chunks.append(chunk)

                # Split into lines and print each one as it arrives
                # Handles \r (progress bars) and \n (normal lines)
                data = line_buffer + chunk
                # Normalize \r\n → \n, then split on \r or \n
                lines = data.replace("\r\n", "\n").split("\r")

                for i, segment in enumerate(lines):
                    parts = segment.split("\n")
                    for j, part in enumerate(parts):
                        is_last = (i == len(lines) - 1) and (j == len(parts) - 1)
                        if is_last:
                            # Incomplete line — keep in buffer
                            line_buffer = part
                        else:
                            if part.strip():
                                print(f"{label}{part}", flush=True)

            if channel.exit_status_ready():
                # Drain remaining data
                while channel.recv_ready():
                    chunk = channel.recv(4096).decode("utf-8", errors="replace")
                    output_chunks.append(chunk)
                    data = line_buffer + chunk
                    lines = data.replace("\r\n", "\n").split("\r")
                    for i, segment in enumerate(lines):
                        parts = segment.split("\n")
                        for j, part in enumerate(parts):
                            is_last = (i == len(lines) - 1) and (j == len(parts) - 1)
                            if is_last:
                                line_buffer = part
                            else:
                                if part.strip():
                                    print(f"{label}{part}", flush=True)
                # Print any remaining buffered content
                if line_buffer.strip():
                    print(f"{label}{line_buffer}", flush=True)
                break

            if time.time() > deadline:
                channel.close()
                output = "".join(output_chunks)
                raise TimeoutError(
                    f"Command timed out after {timeout}s on {self.host}.\n"
                    f"Command: {command[:200]}\n"
                    f"Last output: {output[-500:]}"
                )

            time.sleep(poll_interval)

        exit_code = channel.recv_exit_status()
        channel.close()

        return "".join(output_chunks).strip(), exit_code

    def disconnect(self):
        if self._client:
            self._client.close()
            self._client = None

    # ── private ───────────────────────────────────────────────────────────────

    def _load_private_key(self, key_path: Path) -> paramiko.PKey:
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