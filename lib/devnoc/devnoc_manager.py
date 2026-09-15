"""
lib/devnoc/devnoc_manager.py

Configures/cleans up dev-noc jump-host access for a controller build:
  - SSH alias `ssh shc-<build_no>` -- a system-wide ssh_config.d snippet +
    key file on dev-noc, usable by any user logged into dev-noc (not just
    whoever ran the automation).
  - A kubeconfig context named `shc-<build_no>` merged into ONE shared
    kubeconfig file all users read, so `kubectl --context shc-<build_no>`
    works too.

Reuses lib.aws.ssm_manager.SSMManager for all execution against dev-noc --
same access pattern (SSM Session Manager, not direct SSH) already used by
the existing backup-restore integration elsewhere in this repo.

Instance ID (i-0962c62be5a94d5eb, us-west-1) confirmed directly from the
actual interactive connection command used to reach dev-noc:
    aws ssm start-session --target i-0962c62be5a94d5eb --region us-west-1 \
        --profile dev --document-name AWS-StartInteractiveCommand \
        --parameters command='cd && bash -l'
NOTE: this differs from the instance ID associated with the existing
backup-restore SSM integration elsewhere in shc-automation
(i-0e38286eeff640732) -- worth confirming whether that's a genuinely
different jump host, or a stale/incorrect reference, before assuming the
two features share infrastructure beyond the SSMManager code path itself.

ASSUMPTION TO VERIFY: this relies on `kubectl` being installed on dev-noc
itself (used for `config rename-context` / `config view --flatten` when
merging into the shared file). If it isn't, the kubeconfig-merge step will
fail cleanly with a DevNocError -- the SSH-alias step is independent and
will still succeed either way.
"""

import base64

from lib.aws.ssm_manager import SSMManager, log_info, log_warn

DEVNOC_INSTANCE_ID = "i-0962c62be5a94d5eb"
DEVNOC_REGION = "us-west-1"

DEVNOC_KEY_DIR = "/opt/shc-keys"
DEVNOC_SSH_CONFIG_DIR = "/etc/ssh/ssh_config.d"
# Shared file all users read -- e.g. via `export KUBECONFIG=/opt/shc-kubeconfig/config`
# in a shell profile snippet, or `kubectl --kubeconfig=/opt/shc-kubeconfig/config`.
DEVNOC_SHARED_KUBECONFIG = "/opt/shc-kubeconfig/config"


class DevNocError(Exception):
    """Raised when dev-noc configuration/cleanup fails."""


class DevNocManager:
    def __init__(self, region=DEVNOC_REGION, instance_id=DEVNOC_INSTANCE_ID,
                 profile=None, access_key=None, secret_key=None):
        self.instance_id = instance_id
        # No profile/access_key/secret_key passed through -> SSMManager's
        # own boto3.client() call falls back to boto3's default credential
        # chain, which already picks up AWS_ACCESS_KEY_ID/
        # AWS_SECRET_ACCESS_KEY -- the same env vars Rauto.jenkinsfile's
        # existing `withCredentials([[$class: 'AmazonWebServicesCredentialsBinding', ...]])`
        # block already exports for S3/other AWS calls. No new credential
        # plumbing needed IF that credential's IAM permissions cover SSM
        # access to this specific dev-noc instance -- worth confirming,
        # since the known-working manual connection command uses a named
        # local profile (`--profile dev`), not env-var creds, so this is
        # an unverified assumption, not a confirmed fact.
        self.ssm = SSMManager(region=region, profile=profile,
                               access_key=access_key, secret_key=secret_key)

    def _run(self, commands, timeout=60, comment=""):
        """Run a list of shell commands on dev-noc via SSM, raise DevNocError on failure."""
        self.ssm.check_instance_online(self.instance_id)
        command_id = self.ssm.send_command(self.instance_id, commands, timeout=timeout, comment=comment)
        if command_id is None:
            raise DevNocError(f"Failed to submit SSM command ({comment or commands[0][:60]})")
        result = self.ssm.wait_for_command(self.instance_id, command_id, timeout=timeout + 30)
        if result["status"] != "Success":
            raise DevNocError(
                f"SSM command failed ({comment or commands[0][:60]}): "
                f"status={result['status']} stderr={result['stderr'][-500:]}"
            )
        return result["stdout"]

    def configure(self, build_no: str, controller_ip: str, ssh_key_content: str,
                  kubeconfig_content: str, user: str = "ubuntu"):
        """
        Configure dev-noc so `ssh shc-<build_no>` reaches controller_ip, and
        `kubectl --kubeconfig=/opt/shc-kubeconfig/config --context shc-<build_no> ...`
        reaches this build's cluster.
        """
        host_alias = f"shc-{build_no}"
        key_path = f"{DEVNOC_KEY_DIR}/{host_alias}.key"
        ssh_snippet_path = f"{DEVNOC_SSH_CONFIG_DIR}/{host_alias}.conf"

        key_b64 = base64.b64encode(ssh_key_content.encode()).decode()
        kubeconfig_b64 = base64.b64encode(kubeconfig_content.encode()).decode()

        log_info(f"[devnoc] Configuring dev-noc for {host_alias} ({controller_ip}) ...")

        # 1) SSH key + system-wide ssh_config.d snippet -- works for any
        #    user logged into dev-noc, not just whoever ran this.
        ssh_setup_cmds = [
            f"sudo mkdir -p {DEVNOC_KEY_DIR}",
            f"echo {key_b64} | base64 -d | sudo tee {key_path} > /dev/null",
            f"sudo chmod 600 {key_path}",
            f"sudo chown root:root {key_path}",
            f"sudo mkdir -p {DEVNOC_SSH_CONFIG_DIR}",
            (
                f"printf 'Host {host_alias}\\n"
                f"    HostName {controller_ip}\\n"
                f"    User {user}\\n"
                f"    IdentityFile {key_path}\\n"
                f"    StrictHostKeyChecking no\\n"
                f"    UserKnownHostsFile /dev/null\\n' "
                f"| sudo tee {ssh_snippet_path} > /dev/null"
            ),
            f"sudo chmod 644 {ssh_snippet_path}",
            "echo SSH_ALIAS_CONFIGURED",
        ]
        out = self._run(ssh_setup_cmds, comment=f"devnoc ssh alias {host_alias}")
        if "SSH_ALIAS_CONFIGURED" not in out:
            raise DevNocError(f"SSH alias setup for {host_alias} did not complete: {out[-300:]}")
        log_info(f"[devnoc] ssh {host_alias} -> {controller_ip} configured ✓")

        # 2) Rename this kubeconfig's cluster/user/context entries to the
        #    build alias (so multiple builds never collide in the shared
        #    file), then merge into the one shared kubeconfig via
        #    `kubectl config view --flatten` -- the standard safe way to
        #    combine kubeconfigs without clobbering other builds' entries.
        tmp_kubeconfig = f"/tmp/{host_alias}.kubeconfig"
        kube_setup_cmds = [
            f"echo {kubeconfig_b64} | base64 -d | sudo tee {tmp_kubeconfig} > /dev/null",
            # admin.conf typically has exactly one cluster/user/context,
            # all under kubeadm's default name -- rename whatever's there
            # to the build alias so it can't collide with another build's
            # entry once merged into the shared file.
            (
                f"sudo kubectl --kubeconfig={tmp_kubeconfig} config get-clusters | tail -n +2 | "
                f"while read c; do sudo kubectl --kubeconfig={tmp_kubeconfig} "
                f"config rename-context \"$c\" '{host_alias}' 2>/dev/null; done"
            ),
            f"sudo mkdir -p $(dirname {DEVNOC_SHARED_KUBECONFIG})",
            f"sudo test -f {DEVNOC_SHARED_KUBECONFIG} || sudo touch {DEVNOC_SHARED_KUBECONFIG}",
            (
                f"sudo KUBECONFIG={DEVNOC_SHARED_KUBECONFIG}:{tmp_kubeconfig} "
                f"kubectl config view --flatten | sudo tee /tmp/{host_alias}.merged > /dev/null"
            ),
            f"sudo mv /tmp/{host_alias}.merged {DEVNOC_SHARED_KUBECONFIG}",
            f"sudo chmod 644 {DEVNOC_SHARED_KUBECONFIG}",
            f"rm -f {tmp_kubeconfig}",
            "echo KUBECONFIG_MERGED",
        ]
        out = self._run(kube_setup_cmds, timeout=90, comment=f"devnoc kubeconfig merge {host_alias}")
        if "KUBECONFIG_MERGED" not in out:
            raise DevNocError(f"kubeconfig merge for {host_alias} did not complete: {out[-300:]}")
        log_info(f"[devnoc] kubeconfig context '{host_alias}' merged into {DEVNOC_SHARED_KUBECONFIG} ✓")

    def cleanup(self, build_no: str):
        """
        Remove the SSH alias + kubeconfig context for a build -- call this
        when its controller VM is destroyed (run_cleanup teardown).
        Non-fatal on failure: logs a warning rather than raising, since a
        leftover alias/context shouldn't block the rest of teardown.
        """
        host_alias = f"shc-{build_no}"
        key_path = f"{DEVNOC_KEY_DIR}/{host_alias}.key"
        ssh_snippet_path = f"{DEVNOC_SSH_CONFIG_DIR}/{host_alias}.conf"

        cleanup_cmds = [
            f"sudo rm -f {ssh_snippet_path}",
            f"sudo rm -f {key_path}",
            f"sudo kubectl --kubeconfig={DEVNOC_SHARED_KUBECONFIG} config unset contexts.{host_alias} 2>/dev/null || true",
            f"sudo kubectl --kubeconfig={DEVNOC_SHARED_KUBECONFIG} config unset clusters.{host_alias} 2>/dev/null || true",
            f"sudo kubectl --kubeconfig={DEVNOC_SHARED_KUBECONFIG} config unset users.{host_alias} 2>/dev/null || true",
            "echo DEVNOC_CLEANED",
        ]
        try:
            out = self._run(cleanup_cmds, comment=f"devnoc cleanup {host_alias}")
            if "DEVNOC_CLEANED" not in out:
                log_warn(f"[devnoc] cleanup for {host_alias} may not have fully completed: {out[-300:]}")
            else:
                log_info(f"[devnoc] {host_alias} cleaned up ✓")
        except DevNocError as e:
            log_warn(f"[devnoc] cleanup for {host_alias} failed (non-fatal): {e}")