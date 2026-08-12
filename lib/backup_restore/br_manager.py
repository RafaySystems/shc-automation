"""
lib/backup_restore/br_manager.py

Backup & restore enablement logic, refactored out of the original
standalone enable_backup_restore.py script.

build_patch_script() and build_remote_script() are pure string builders
with zero AWS calls -> fully unit testable without credentials.

launch() and stream_and_poll() talk to AWS via an injected SSMManager
and RETURN results instead of calling sys.exit(), so pytest can assert
on them directly.
"""

import base64
import time

from lib.aws.ssm_manager import SSMManager, log_info, log_error, log_poll

SSM_USER = "ubuntu"
UBUNTU_PATH = (
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:"
    "/snap/bin:/opt/rafay/home/ubuntu/oracle-cli/bin:/usr/local/go/bin"
)

REMOTE_LOG = "/tmp/br_setup_output.log"
REMOTE_SCRIPT = "/tmp/br_setup_run.sh"
REMOTE_PID_FILE = "/tmp/br_setup.pid"
REMOTE_DONE = "/tmp/br_setup.done"

SUCCESS_MSG = "[DONE] Backup & Restore setup completed successfully!"
FATAL_PATTERNS = [
    "radm dependency failed",
    "TIMEOUT: Velero pods",
    "config.yaml not found",
    "TIMEOUT: BSL not Available",
    "TIMEOUT: No new backup",
    "TIMEOUT after",
    "Backup phase: Failed",
    "Backup phase: PartiallyFailed",
    "Could not resolve hostname",   # new
    "Permission denied (publickey",  # new — same class of unrecoverable SSH failure
    "Connection refused",            # new
]


class BackupRestoreManager:
    def __init__(self, ssm_manager: SSMManager, instance_id: str):
        self.ssm = ssm_manager
        self.instance_id = instance_id

    # -------------------------------------------------------------------
    # Pure string builders — no AWS calls, no side effects.
    # These are the pieces covered by tests/test_br_manager_unit.py
    # -------------------------------------------------------------------
    @staticmethod
    def build_patch_script(bucket_name, s3_region, b64_access_key, b64_secret_key):
        """
        Returns Python source that patches config.yaml in-place.
        backup_restore is matched at ANY indent (it lives under spec: in the
        real config). Only backup_restore -> externalBlobStorage ->
        username/password are changed. smtp / jfrog / super-user credentials
        are never touched.
        """
        lines = [
            "import re",
            "p      = open('/tmp/config_path.txt').read().strip()",
            "lines  = open(p).readlines()",
            "out    = []",
            "in_br      = False",
            "in_ebs     = False",
            "br_indent  = -1",
            "ebs_indent = -1",
            "bucket    = " + repr(bucket_name),
            "s3_region = " + repr(s3_region),
            "ak        = " + repr(b64_access_key) + "  # base64-encoded",
            "sk        = " + repr(b64_secret_key) + "  # base64-encoded",
            "for line in lines:",
            "    s   = line.lstrip()",
            "    ind = len(line) - len(s)",
            r"    if re.match(r'\s*backup_restore\s*:', line):",
            "        in_br = True; in_ebs = False; br_indent = ind; ebs_indent = -1",
            "        out.append(line); continue",
            "    elif in_br and s and not s.startswith('#') and ind <= br_indent:",
            "        in_br = False; in_ebs = False",
            "    if in_br:",
            r"        if re.match(r'\s+enabled\s*:\s*false', line):",
            r"            line = re.sub(r'(enabled\s*:)\s*false', r'\1 true', line)",
            r"        elif re.match(r'\s+schedule\s*:', line):",
            "            cron = '*/10 * * * *'\n",
            "            line = re.sub(r'(schedule\\s*:).*', lambda m: m.group(1) + ' \"' + cron + '\"', line).rstrip() + '\\n'",
            r"        elif re.match(r'\s+bucketName\s*:', line):",
            "            line = re.sub(r'(bucketName\\s*:)\\s*\"[^\"]*\"', r'\\1 \"' + bucket + r'\"', line)",
            r"        elif re.match(r'\s+region\s*:', line) and not in_ebs:",
            "            line = re.sub(r'(region\\s*:)\\s*\"[^\"]*\"', r'\\1 \"' + s3_region + r'\"', line)",
            r"        elif re.match(r'\s+externalBlobStorage\s*:', line):",
            "            in_ebs = True; ebs_indent = ind",
            "        elif in_ebs:",
            "            if s and not s.startswith('#') and ind <= ebs_indent:",
            "                in_ebs = False",
            r"            elif re.match(r'\s+username\s*:', line):",
            "                line = re.sub(r'(username\\s*:)\\s*\"[^\"]*\"', r'\\1 \"' + ak + r'\"', line)",
            r"            elif re.match(r'\s+password\s*:', line):",
            "                line = re.sub(r'(password\\s*:)\\s*\"[^\"]*\"', r'\\1 \"' + sk + r'\"', line)",
            "    out.append(line)",
            "open(p, 'w').write(''.join(out))",
            "print('[PATCH] Done — only externalBlobStorage username/password changed')",
        ]
        return "\n".join(lines) + "\n"

    @classmethod
    def build_remote_script(cls, build_no, aws_access_key, aws_secret_key, s3_region, testbed_host=None):
        bucket_name = f"shc-{build_no}-s3"
        testbed_host = testbed_host or f"shc-{build_no}"

        b64_access_key = base64.b64encode(aws_access_key.encode()).decode()
        b64_secret_key = base64.b64encode(aws_secret_key.encode()).decode()

        patch_src = cls.build_patch_script(bucket_name, s3_region, b64_access_key, b64_secret_key)
        patch_b64 = base64.b64encode(patch_src.encode()).decode()

        script = f"""#!/bin/bash
set -e
export TERM=xterm
export HOME=/home/{SSM_USER}
export PATH={UBUNTU_PATH}
cd /home/{SSM_USER}

TESTBED="{testbed_host}"

echo "[STEP 1] Testbed: $TESTBED"
echo "[INFO]   Bucket : {bucket_name}"
echo "[INFO]   Region : {s3_region}"
echo "[INFO]   B64 AK : {b64_access_key[:8]}... ({len(b64_access_key)} chars)"
echo "[INFO]   B64 SK : **************** ({len(b64_secret_key)} chars)"
echo "============================================"

# ── Step 2: Locate config.yaml on testbed ─────────────────────────────────
echo "[STEP 2] Locating config.yaml"
ssh -o StrictHostKeyChecking=no -o BatchMode=yes "$TESTBED" bash <<'REMOTE_EOF'
CONFIG_PATH=$(ls /home/ubuntu/rafay-airgapped-controller-*/config.yaml 2>/dev/null | head -1)
if [ -z "$CONFIG_PATH" ]; then
    echo "[ERROR] config.yaml not found under /home/ubuntu/rafay-airgapped-controller-*"
    exit 1
fi
echo "[INFO] Found: $CONFIG_PATH"
echo "$CONFIG_PATH" > /tmp/config_path.txt
REMOTE_EOF
echo "[STEP 2] config.yaml located ✅"

# ── Step 3: Patch config.yaml on testbed ──────────────────────────────────
echo "[STEP 3] Patching config.yaml (scoped to backup_restore->externalBlobStorage)"

python3 -c "import base64; open('/tmp/patch_config_local.py','wb').write(base64.b64decode('{patch_b64}'))"
echo "[DEBUG] patch_config_local.py written: $(wc -c < /tmp/patch_config_local.py) bytes"

ssh -o StrictHostKeyChecking=no -o BatchMode=yes "$TESTBED" python3 - < /tmp/patch_config_local.py

echo "[DEBUG] Verifying externalBlobStorage credentials in config.yaml..."
ssh -o StrictHostKeyChecking=no -o BatchMode=yes "$TESTBED" bash <<'VERIFY_EOF'
CONFIG_PATH=$(cat /tmp/config_path.txt)
USERNAME_LINE=$(grep "username:" "$CONFIG_PATH" | grep -A0 "username" | tail -1)
PASSWORD_LINE=$(grep "password:" "$CONFIG_PATH" | grep -A0 "password" | tail -1)
echo "[DEBUG] username line: $USERNAME_LINE"
echo "[DEBUG] password line: $PASSWORD_LINE"
VERIFY_EOF
echo "[STEP 3] config.yaml patched ✅"

# ── Step 4: Display updated config.yaml ────────────────────────────────────
echo "[STEP 4] Displaying updated config.yaml:"
echo "============================================"
ssh -o StrictHostKeyChecking=no -o BatchMode=yes "$TESTBED" bash <<'REMOTE_EOF'
cat $(cat /tmp/config_path.txt)
REMOTE_EOF
echo "============================================"

# ── Step 5: Run radm dependency ────────────────────────────────────────────
echo "[STEP 5] Running: sudo ./radm dependency --config config.yaml"
echo "============================================"
ssh -o StrictHostKeyChecking=no -o BatchMode=yes "$TESTBED" bash <<'REMOTE_EOF'
CONFIG_PATH=$(cat /tmp/config_path.txt)
CONTROLLER_DIR=$(dirname "$CONFIG_PATH")
cd "$CONTROLLER_DIR"
echo "[INFO] Working dir: $(pwd)"
echo "[INFO] Starting radm dependency at: $(date)"
sudo ./radm dependency --config config.yaml
RADM_EXIT=$?
if [ "$RADM_EXIT" -ne 0 ]; then
    echo "[ERROR] radm dependency failed with exit code: $RADM_EXIT"
    exit $RADM_EXIT
fi
echo "[INFO] radm dependency completed successfully ✅"
REMOTE_EOF
echo "[STEP 5] radm dependency completed ✅"

# ── Step 6: Wait for all Velero pods to be Running ─────────────────────────
echo "[STEP 6] Waiting for all pods in velero namespace to be Running..."
ssh -o StrictHostKeyChecking=no -o BatchMode=yes "$TESTBED" bash <<'REMOTE_EOF'
MAX_WAIT=600; INTERVAL=15; ELAPSED=0
while [ "$ELAPSED" -lt "$MAX_WAIT" ]; do
    POD_STATUS=$(kubectl get pods -n velero --no-headers 2>/dev/null || echo "")
    if [ -z "$POD_STATUS" ]; then
        echo "[POLL] (${{ELAPSED}}s) No velero pods yet..."; sleep $INTERVAL; ELAPSED=$((ELAPSED+INTERVAL)); continue
    fi
    NOT_READY=$(echo "$POD_STATUS" | awk '$3 != "Running" && $3 != "Completed" && $3 != "Succeeded" {{print}}' | wc -l)
    TOTAL=$(echo "$POD_STATUS" | wc -l)
    echo "--- velero pods (${{ELAPSED}}s) ---"; echo "$POD_STATUS"
    if [ "$NOT_READY" -eq 0 ] && [ "$TOTAL" -gt 0 ]; then
        echo "[INFO] ✅ All ${{TOTAL}} velero pod(s) Running!"; exit 0
    fi
    echo "[POLL] (${{ELAPSED}}s) ${{NOT_READY}}/${{TOTAL}} not ready yet"
    sleep $INTERVAL; ELAPSED=$((ELAPSED+INTERVAL))
done
echo "[ERROR] ❌ TIMEOUT: Velero pods not Running after ${{MAX_WAIT}}s"; exit 1
REMOTE_EOF
echo "[STEP 6] All Velero pods Running ✅"

# ── Step 7: Wait 1 minute before checking BSL ──────────────────────────────
echo "[STEP 7] Waiting 60 seconds before checking BSL..."
sleep 60

# ── Step 8: Check BSL is Available ─────────────────────────────────────────
echo "[STEP 8] Checking Backup Storage Location (BSL)..."
ssh -o StrictHostKeyChecking=no -o BatchMode=yes "$TESTBED" bash <<'REMOTE_EOF'
MAX_WAIT=300; INTERVAL=15; ELAPSED=0
while [ "$ELAPSED" -lt "$MAX_WAIT" ]; do
    BSL_OUTPUT=$(kubectl get bsl -A --no-headers 2>/dev/null || echo "")
    if [ -z "$BSL_OUTPUT" ]; then
        echo "[POLL] (${{ELAPSED}}s) No BSL yet..."; sleep $INTERVAL; ELAPSED=$((ELAPSED+INTERVAL)); continue
    fi
    echo "--- BSL (${{ELAPSED}}s) ---"; echo "$BSL_OUTPUT"
    AVAIL=$(echo "$BSL_OUTPUT" | awk '$3 == "Available" {{print}}' | wc -l)
    if [ "$AVAIL" -gt 0 ]; then echo "[INFO] ✅ BSL Available!"; exit 0; fi
    echo "[POLL] (${{ELAPSED}}s) BSL not Available yet"
    sleep $INTERVAL; ELAPSED=$((ELAPSED+INTERVAL))
done
echo "[ERROR] ❌ TIMEOUT: BSL not Available after ${{MAX_WAIT}}s"; exit 1
REMOTE_EOF
echo "[STEP 8] BSL Available ✅"

# ── Step 9: Wait for a new backup to appear ────────────────────────────────
echo "[STEP 9] Waiting for a new scheduled backup..."
ssh -o StrictHostKeyChecking=no -o BatchMode=yes "$TESTBED" bash <<'REMOTE_EOF'
MAX_WAIT=1500; INTERVAL=30; ELAPSED=0
START_TS=$(date +%Y%m%d%H%M%S)
echo "[INFO] Waiting for backup newer than ${{START_TS}} (max ${{MAX_WAIT}}s)"
while [ "$ELAPSED" -lt "$MAX_WAIT" ]; do
    BACKUP_LIST=$(kubectl get backup -A --no-headers 2>/dev/null || echo "")
    if [ -z "$BACKUP_LIST" ]; then
        echo "[POLL] (${{ELAPSED}}s) No backups yet"; sleep $INTERVAL; ELAPSED=$((ELAPSED+INTERVAL)); continue
    fi
    echo "--- backups (${{ELAPSED}}s) ---"; echo "$BACKUP_LIST"
    LATEST=$(echo "$BACKUP_LIST" | awk '{{print $2}}' | grep -E 'velero-rafay-core-backup-[0-9]{{14}}' | sort | tail -1)
    if [ -z "$LATEST" ]; then
        echo "[POLL] (${{ELAPSED}}s) No matching backup yet"; sleep $INTERVAL; ELAPSED=$((ELAPSED+INTERVAL)); continue
    fi
    BACKUP_TS=$(echo "$LATEST" | grep -oE '[0-9]{{14}}$')
    if [ "$BACKUP_TS" -ge "$START_TS" ]; then
        echo "[INFO] ✅ New backup: ${{LATEST}}"
        echo "$LATEST" > /tmp/latest_backup_name.txt; exit 0
    fi
    echo "[POLL] (${{ELAPSED}}s) Latest backup ${{LATEST}} older than start"
    sleep $INTERVAL; ELAPSED=$((ELAPSED+INTERVAL))
done
echo "[ERROR] ❌ TIMEOUT: No new backup in ${{MAX_WAIT}}s"; exit 1
REMOTE_EOF
echo "[STEP 9] New backup detected ✅"

# ── Step 10: Poll backup describe until Phase=Completed ────────────────────
echo "[STEP 10] Monitoring backup until Phase=Completed..."
ssh -o StrictHostKeyChecking=no -o BatchMode=yes "$TESTBED" bash <<'REMOTE_EOF'
MAX_WAIT=10800; INTERVAL=300; ELAPSED=0
BACKUP_NAME=$(cat /tmp/latest_backup_name.txt 2>/dev/null)
[ -z "$BACKUP_NAME" ] && echo "[ERROR] No backup name found" && exit 1
echo "[INFO] Monitoring: ${{BACKUP_NAME}} (timeout 3h)"
while [ "$ELAPSED" -lt "$MAX_WAIT" ]; do
    DESCRIBE=$(kubectl describe backup -n velero "$BACKUP_NAME" 2>/dev/null || echo "")
    [ -z "$DESCRIBE" ] && echo "[POLL] (${{ELAPSED}}s) Cannot describe yet" && sleep $INTERVAL && ELAPSED=$((ELAPSED+INTERVAL)) && continue
    PHASE=$(echo "$DESCRIBE" | grep -E '^\\s+Phase:' | head -1 | awk '{{print $2}}')
    BACKED=$(echo "$DESCRIBE" | grep 'Items Backed Up:' | head -1 | awk '{{print $NF}}')
    TOTAL=$(echo "$DESCRIBE"  | grep 'Total Items:'     | head -1 | awk '{{print $NF}}')
    echo "--- (${{ELAPSED}}s) Phase=${{PHASE}} Backed=${{BACKED}} Total=${{TOTAL}} ---"
    if [ "$PHASE" = "Completed" ] && [ "$BACKED" = "$TOTAL" ]; then
        echo "[INFO] ✅ Backup COMPLETED: ${{BACKED}}/${{TOTAL}} items"; exit 0
    fi
    if [ "$PHASE" = "Failed" ] || [ "$PHASE" = "PartiallyFailed" ]; then
        echo "[ERROR] ❌ Backup phase: ${{PHASE}}"; echo "$DESCRIBE"; exit 1
    fi
    if [ "$PHASE" = "InProgress" ] && [ -n "$TOTAL" ] && [ "$TOTAL" -gt 0 ]; then
        PCT=$(( BACKED * 100 / TOTAL ))
        echo "[POLL] (${{ELAPSED}}s) ${{BACKED}}/${{TOTAL}} (${{PCT}}%) — next check in ${{INTERVAL}}s"
    else
        echo "[POLL] (${{ELAPSED}}s) Phase=${{PHASE}} — waiting ${{INTERVAL}}s"
    fi
    sleep $INTERVAL; ELAPSED=$((ELAPSED+INTERVAL))
done
echo "[ERROR] ❌ TIMEOUT after ${{MAX_WAIT}}s"; exit 1
REMOTE_EOF

echo "[STEP 10] Backup completed ✅"
echo "============================================"
echo "[DONE] Backup & Restore setup completed successfully!"
echo "[DONE] Time: $(date)"
"""
        return script

    # -------------------------------------------------------------------
    # AWS-calling methods — these need a live SSMManager + online instance.
    # -------------------------------------------------------------------
    def launch(self, build_no, aws_access_key, aws_secret_key, s3_region="us-west-2", testbed_host=None):
        log_info("Launching backup-restore setup script in background on dev-noc...")
        remote_script_content = self.build_remote_script(
            build_no, aws_access_key, aws_secret_key, s3_region, testbed_host=testbed_host
        )

        launch_commands = [
            f"rm -f {REMOTE_LOG} {REMOTE_SCRIPT} {REMOTE_DONE} {REMOTE_PID_FILE}",
            "rm -f /tmp/patch_config_local.py /tmp/patch_config.py /tmp/config_path.txt",
            f"cat > {REMOTE_SCRIPT} << 'ENDOFSCRIPT'\n{remote_script_content}\nENDOFSCRIPT",
            f"chmod +x {REMOTE_SCRIPT}",
            f"sudo -u {SSM_USER} nohup bash {REMOTE_SCRIPT} > {REMOTE_LOG} 2>&1 &",
            f"echo $! > {REMOTE_PID_FILE}",
            f"echo 'Background PID: '$(cat {REMOTE_PID_FILE})",
            f"echo 'Log file: {REMOTE_LOG}'",
            "echo 'Backup-restore script launched successfully'",
        ]

        cmd_id = self.ssm.send_command(
            self.instance_id, launch_commands, timeout=120,
            comment="pytest: launch backup-restore setup",
        )
        if not cmd_id:
            raise RuntimeError("Failed to submit launch command to SSM")

        result = self.ssm.wait_for_command(self.instance_id, cmd_id, timeout=60)
        if result["status"] != "Success":
            raise RuntimeError(f"Failed to launch script: {result}")
        log_info("✅ Backup-restore setup script launched in background on dev-noc")

    def _read_remote_log(self, offset=0):
        commands = [
            f"if [ -f {REMOTE_LOG} ]; then",
            f"    tail -c +{offset + 1} {REMOTE_LOG}",
            "else",
            "    echo ''",
            "fi",
        ]
        return self.ssm.send_command(
            self.instance_id, commands, timeout=30, comment="pytest: read br log"
        )

    def _check_done(self):
        commands = [
            f"if [ -f {REMOTE_DONE} ]; then",
            f"    echo 'DONE:'$(cat {REMOTE_DONE})",
            "else",
            "    echo 'RUNNING'",
            "fi",
        ]
        return self.ssm.send_command(
            self.instance_id, commands, timeout=30, comment="pytest: check br done"
        )

    def stream_and_poll(self, poll_interval=10, max_wait=7200):
        """
        Returns (exit_code: int, full_log: str) instead of calling sys.exit(),
        so pytest can assert on the result directly and attach the log to
        Allure regardless of pass/fail.
        """
        elapsed = 0
        log_offset = 0
        full_output = ""

        log_info("Streaming output from dev-noc...")
        log_info(f"Polling every {poll_interval}s | Max wait: {max_wait}s")
        time.sleep(5)

        while elapsed < max_wait:
            read_cmd_id = self._read_remote_log(log_offset)
            if read_cmd_id:
                result = self.ssm.wait_for_command(self.instance_id, read_cmd_id, timeout=30)
                new_output = result.get("stdout", "")
                if new_output and new_output.strip():
                    print(new_output, end="", flush=True)
                    full_output += new_output
                    log_offset += len(new_output)

            done_cmd_id = self._check_done()
            if done_cmd_id:
                done_result = self.ssm.wait_for_command(self.instance_id, done_cmd_id, timeout=30)
                done_output = done_result.get("stdout", "").strip()
                if done_output.startswith("DONE:"):
                    exit_code_str = done_output.replace("DONE:", "").strip()
                    log_info(f"Script finished with exit code: {exit_code_str}")
                    if SUCCESS_MSG in full_output:
                        log_info("✅ SUCCESS: Backup & Restore setup completed!")
                        return 0, full_output
                    log_error("❌ Script finished but success message not found.")
                    return 1, full_output

            for pattern in FATAL_PATTERNS:
                if pattern in full_output:
                    log_error(f"❌ Fatal failure detected: '{pattern}'")
                    return 1, full_output

            if SUCCESS_MSG in full_output:
                log_info("✅ SUCCESS: Backup & Restore setup completed!")
                return 0, full_output

            log_poll(f"Elapsed: {elapsed}s | Log offset: {log_offset} bytes | Still running...")
            time.sleep(poll_interval)
            elapsed += poll_interval

        log_error(f"❌ TIMEOUT after {max_wait}s")
        return 1, full_output
