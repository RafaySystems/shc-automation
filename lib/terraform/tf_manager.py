"""
lib/terraform/tf_manager.py

Reads OCIProfile + config from dev.yaml → generates terraform.tfvars → runs Terraform.

HA mode (ha: true in dev.yaml):
  - Creates 3 OCI instances + 3 data volumes
  - Returns (instance_ids, public_ips, fqdn)
  - instance_ids[0] / public_ips[0] = primary node (radm init)
  - instance_ids[1:] / public_ips[1:] = secondary nodes (radm join)

Non-HA mode (ha: false):
  - Creates 1 OCI instance + 1 data volume
  - Returns ([instance_id], [public_ip], fqdn)
"""

import os
import json
import subprocess
import shutil
from pathlib import Path
from typing import Optional

from lib.oci.vm_manager import OCIProfile

TERRAFORM_DIR = Path(__file__).parent.parent.parent / "terraform"


class TerraformManager:

    def __init__(self, profile: OCIProfile, terraform_dir: Optional[Path] = None):
        self.profile       = profile
        self.terraform_dir = Path(terraform_dir) if terraform_dir else TERRAFORM_DIR
        self.tfvars_path   = self.terraform_dir / "terraform.tfvars"
        self._aws_profile  = ""

        if not shutil.which("terraform"):
            raise EnvironmentError(
                "terraform binary not found in PATH.\n"
                "Install: brew install terraform"
            )

    def apply(self, build_no: Optional[str] = None, dns_cfg: Optional[dict] = None,
              controller_profile=None) -> tuple:
        """
        Generate tfvars → terraform init → apply.

        Args:
            controller_profile: optional ControllerProfile — if provided, its cpu/memory_gb
                                 override the raw oci.ocpus/memory_gb from dev.yaml so that
                                 controller_size: "M" in dev.yaml automatically provisions
                                 the correct VM hardware size.

        Returns:
            (instance_ids, public_ips, fqdn)
            instance_ids : list of instance OCIDs  [node1, node2, node3] or [node1]
            public_ips   : list of public IPs       [ip1, ip2, ip3]       or [ip1]
            fqdn         : wildcard DNS string or ""
        """
        self._aws_profile = (dns_cfg or {}).get("aws_profile", "")
        display_name      = self.profile.resolve_display_name(build_no)

        print(f"\n[TerraformManager] Generating terraform.tfvars from dev.yaml ...")
        self._write_tfvars(display_name, dns_cfg, controller_profile)

        print(f"[TerraformManager] Running terraform init ...")
        self._run(["terraform", "init", "-upgrade"], "terraform init")

        print(f"[TerraformManager] Running terraform apply ...")
        self._run(
            ["terraform", "apply", "-auto-approve", "-var-file=terraform.tfvars"],
            "terraform apply"
        )

        instance_ids, public_ips, fqdn = self._parse_outputs()

        ha_label = f"HA ({len(public_ips)} nodes)" if len(public_ips) > 1 else "Non-HA"
        print(f"[TerraformManager] {ha_label} — VMs ready:")
        for i, (iid, ip) in enumerate(zip(instance_ids, public_ips), 1):
            role = "primary" if i == 1 else f"node{i}"
            print(f"  node{i} ({role}): {ip}  ({iid})")

        return instance_ids, public_ips, fqdn

    def destroy(self):
        """terraform destroy — removes all instances, volumes."""
        print(f"\n[TerraformManager] Running terraform destroy ...")
        self._run(
            ["terraform", "destroy", "-auto-approve", "-var-file=terraform.tfvars"],
            "terraform destroy"
        )
        print(f"[TerraformManager] All resources destroyed.")

    def output(self) -> dict:
        result = self._run(
            ["terraform", "output", "-json"],
            "terraform output",
            capture=True
        )
        raw = json.loads(result.stdout)
        return {k: v["value"] for k, v in raw.items()}

    # ── private ───────────────────────────────────────────────────────────────

    def _write_tfvars(self, display_name: str, dns_cfg: Optional[dict],
                      controller_profile=None):
        p  = self.profile
        ha = getattr(p, 'ha', False)

        if not Path(p.ssh_public_key).exists():
            raise FileNotFoundError(f"SSH public key not found: {p.ssh_public_key}")

        # Use controller_profile cpu/memory if provided — this ensures the VM
        # hardware matches controller_size (S/M/L) defined in dev.yaml.
        # Falls back to raw oci.ocpus / oci.memory_gb if no profile passed.
        if controller_profile:
            ocpus     = controller_profile.cpu
            memory_gb = controller_profile.memory_gb
            print(f"[TerraformManager] VM sizing from controller_size="
                  f"{controller_profile.controller_size}: "
                  f"{ocpus} vCPUs / {memory_gb}GB RAM")
        else:
            ocpus     = p.ocpus
            memory_gb = p.memory_gb

        tags_hcl = "{\n" + "".join(
            f'  "{k}" = "{v}"\n' for k, v in (p.tags or {}).items()
        ) + "}"

        content = f"""\
# Auto-generated from dev.yaml by lib/terraform/tf_manager.py
# DO NOT EDIT MANUALLY — edit dev.yaml instead

# OCI
oci_profile         = "{p.profile}"
compartment_id      = "{p.compartment_id}"
availability_domain = "{p.availability_domain}"
subnet_id           = "{p.subnet_id}"
image_id            = "{p.image_id}"
shape               = "{p.shape}"
ocpus               = {ocpus}
memory_gb           = {memory_gb}
boot_volume_gb      = {p.boot_volume_gb}
data_volume_gb      = {p.data_volume_gb}
ssh_public_key_path = "{p.ssh_public_key}"
display_name        = "{display_name}"
ha                  = {"true" if ha else "false"}
tags                = {tags_hcl}
"""
        if dns_cfg and dns_cfg.get("hosted_zone_id"):
            content += f"""
# Route53 DNS (managed by boto3 in conftest.py)
# aws_profile = "{dns_cfg.get('aws_profile', 'default')}"
"""

        self.tfvars_path.write_text(content)
        print(f"[TerraformManager] tfvars written (ha={ha}, ocpus={ocpus}, memory_gb={memory_gb})")

    def _parse_outputs(self) -> tuple:
        outputs = self.output()

        # HA mode returns lists, non-HA also returns lists (count=1)
        instance_ids = outputs.get("instance_ids", [])
        public_ips   = outputs.get("public_ips", [])
        fqdn         = outputs.get("star_domain", "")

        # Fallback to single-value outputs if lists empty
        if not instance_ids:
            single_id = outputs.get("instance_id", "")
            if single_id:
                instance_ids = [single_id]
        if not public_ips:
            single_ip = outputs.get("public_ip", "")
            if single_ip:
                public_ips = [single_ip]

        if not instance_ids:
            raise RuntimeError("terraform output 'instance_ids' is empty")
        if not public_ips:
            raise RuntimeError("terraform output 'public_ips' is empty")

        return instance_ids, public_ips, fqdn

    def _run(self, cmd: list, label: str, capture: bool = False) -> subprocess.CompletedProcess:
        print(f"[TerraformManager] $ {' '.join(cmd)}")

        import copy
        env = copy.copy(os.environ)
        if self._aws_profile:
            env["AWS_PROFILE"] = self._aws_profile

        result = subprocess.run(
            cmd,
            cwd=str(self.terraform_dir),
            capture_output=capture,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"{label} failed (exit {result.returncode}).\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result