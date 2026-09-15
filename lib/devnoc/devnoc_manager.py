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

ASSUMPTION TO VERIFY: the kubeconfig-merge step relies on `kubectl` being
installed on dev-noc itself (used only for the final `config view
--flatten` merge -- cluster/context/user renaming now happens locally in
Python via PyYAML before anything is sent to dev-noc, see below). If
kubectl isn't there, the merge step will fail cleanly with a DevNocError
-- the SSH-alias step is independent and will still succeed either way.
"""

import base64

import yaml

from lib.aws.ssm_manager import SSMManager, log_info, log_warn

DEVNOC_INSTANCE_ID = "i-0962c62be5a94d5eb"
DEVNOC_REGION = "us-west-1"

# A structurally-valid, empty kubeconfig -- used to seed
# DEVNOC_SHARED_KUBECONFIG the first time it's created. A plain empty file
# (e.g. from `touch`) is NOT valid YAML and breaks `kubectl config view
# --flatten` outright (confirmed via dry run: it silently produced empty
# output, which then got written back over the shared file -- wiping out
# anything already merged into it). This skeleton is what a real, empty
# kubeconfig looks like, so the very first merge has something valid to
# merge into.
_EMPTY_KUBECONFIG_SKELETON = (
    "apiVersion: v1\nkind: Config\nclusters: []\ncontexts: []\n"
    "current-context: \"\"\npreferences: {}\nusers: []\n"
)

DEVNOC_KEY_DIR = "/opt/shc-keys"
DEVNOC_SSH_CONFIG_DIR = "/etc/ssh/ssh_config.d"
# Shared file all users read -- e.g. via `export KUBECONFIG=/opt/shc-kubeconfig/config`
# in a shell profile snippet, or `kubectl --kubeconfig=/opt/shc-kubeconfig/config`.
DEVNOC_SHARED_KUBECONFIG = "/opt/shc-kubeconfig/config"


def _rename_kubeconfig_entries(kubeconfig_content: str, host_alias: str) -> str:
    """
    Rename this kubeconfig's cluster/user/context entries to unique,
    build-specific names before it's ever sent to dev-noc.

    Two real bugs this fixes (both found via dry run against a fake
    kubeconfig, see devnoc_dry_run_full.py):

    1. The original approach did `kubectl config get-clusters` to find
       what to rename, then called `rename-context` on the result --
       but rename-context needs a CONTEXT name, not a cluster name,
       and admin.conf's context is typically named
       "kubernetes-admin@kubernetes", not "kubernetes". The rename
       silently no-opped (its stderr was suppressed), so nothing ever
       actually got renamed.

    2. Even fixed, renaming only the CONTEXT isn't enough: every real
       controller's admin.conf uses kubeadm's same fixed default names
       for the CLUSTER ("kubernetes") and USER ("kubernetes-admin")
       entries too. Merging a second build's kubeconfig into the shared
       file would silently overwrite the first build's cluster/user
       entries -- since they'd share the exact same name -- breaking
       the first build's context even though it looked untouched.

    Doing this in Python (not shell/kubectl calls) sidesteps both: full
    control over the YAML structure, no fragile text-parsing of kubectl's
    table output, and no risk of a wrong field being read as the "thing
    to rename."
    """
    data = yaml.safe_load(kubeconfig_content)
    cluster_name = f"{host_alias}-cluster"
    user_name = f"{host_alias}-user"

    for cluster in data.get("clusters", []) or []:
        cluster["name"] = cluster_name
    for user in data.get("users", []) or []:
        user["name"] = user_name
    for context in data.get("contexts", []) or []:
        context["name"] = host_alias
        context["context"]["cluster"] = cluster_name
        context["context"]["user"] = user_name
    data["current-context"] = host_alias

    return yaml.safe_dump(data, default_flow_style=False)


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
        log_info(f"[devnoc] ssh {host_alias} -> {controller_ip} configured OK")

        # 2) Rename this kubeconfig's cluster/user/context entries to
        #    build-specific names IN PYTHON (see _rename_kubeconfig_entries
        #    for why -- get-clusters/rename-context over SSM silently
        #    no-opped, and even fixed, only renaming the context wasn't
        #    enough to prevent cluster/user name collisions across builds),
        #    then merge into the one shared kubeconfig via `kubectl config
        #    view --flatten` -- the standard safe way to combine
        #    kubeconfigs without clobbering other builds' entries.
        renamed_kubeconfig = _rename_kubeconfig_entries(kubeconfig_content, host_alias)
        renamed_kubeconfig_b64 = base64.b64encode(renamed_kubeconfig.encode()).decode()
        empty_skeleton_b64 = base64.b64encode(_EMPTY_KUBECONFIG_SKELETON.encode()).decode()

        tmp_kubeconfig = f"/tmp/{host_alias}.kubeconfig"
        kube_setup_cmds = [
            f"echo {renamed_kubeconfig_b64} | base64 -d | sudo tee {tmp_kubeconfig} > /dev/null",
            f"sudo mkdir -p $(dirname {DEVNOC_SHARED_KUBECONFIG})",
            # Seed with a valid EMPTY kubeconfig, not a zero-byte file from
            # `touch` -- a truly empty file isn't valid YAML and broke
            # `kubectl config view --flatten` outright (confirmed via dry
            # run: it silently produced empty output, which then got
            # written back over the shared file, wiping out anything
            # already in it).
            (
                f"sudo test -f {DEVNOC_SHARED_KUBECONFIG} || "
                f"(echo {empty_skeleton_b64} | base64 -d | sudo tee {DEVNOC_SHARED_KUBECONFIG} > /dev/null)"
            ),
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
        log_info(f"[devnoc] kubeconfig context '{host_alias}' merged into {DEVNOC_SHARED_KUBECONFIG} OK")

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
        cluster_name = f"{host_alias}-cluster"
        user_name = f"{host_alias}-user"

        cleanup_cmds = [
            f"sudo rm -f {ssh_snippet_path}",
            f"sudo rm -f {key_path}",
            f"sudo kubectl --kubeconfig={DEVNOC_SHARED_KUBECONFIG} config unset contexts.{host_alias} 2>/dev/null || true",
            f"sudo kubectl --kubeconfig={DEVNOC_SHARED_KUBECONFIG} config unset clusters.{cluster_name} 2>/dev/null || true",
            f"sudo kubectl --kubeconfig={DEVNOC_SHARED_KUBECONFIG} config unset users.{user_name} 2>/dev/null || true",
            "echo DEVNOC_CLEANED",
        ]
        try:
            out = self._run(cleanup_cmds, comment=f"devnoc cleanup {host_alias}")
            if "DEVNOC_CLEANED" not in out:
                log_warn(f"[devnoc] cleanup for {host_alias} may not have fully completed: {out[-300:]}")
            else:
                log_info(f"[devnoc] {host_alias} cleaned up OK")
        except DevNocError as e:
            log_warn(f"[devnoc] cleanup for {host_alias} failed (non-fatal): {e}")