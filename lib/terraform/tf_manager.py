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

        Returns:
            (instance_ids, public_ips, fqdn)
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

    def read_state(self) -> tuple:
        """
        Read instance_ids and public_ips from existing terraform.tfstate
        WITHOUT running terraform apply.

        Used when provision: false but VMs were originally created by Terraform
        — avoids manual copy-paste of instance OCIDs into dev.yaml.

        Priority:
          1. terraform output -json  (fast, uses cached state)
          2. parse terraform.tfstate directly (fallback)
          3. return ([], [], "") silently if no state exists — caller handles gracefully

        Returns:
            (instance_ids, public_ips, fqdn)
            Returns ([], [], "") if no tfstate found — no crash, caller skips NSG.

        Example:
            ids, ips, fqdn = tf_manager.read_state()
            # ids = ["ocid1.instance...node1", "ocid1.instance...node2", "ocid1.instance...node3"]
            # ips = ["144.24.53.28", "144.24.51.116", "132.226.74.178"]
        """
        tfstate_path = self.terraform_dir / "terraform.tfstate"

        # ── Method 1: terraform output -json (uses .terraform/ cache) ─────────
        # Works if terraform init was previously run in this directory
        try:
            result = self._run(
                ["terraform", "output", "-json"],
                "terraform output (read_state)",
                capture=True
            )
            raw = json.loads(result.stdout)
            if raw:
                outputs = {k: v["value"] for k, v in raw.items()}
                instance_ids, public_ips, fqdn = self._parse_outputs_from_dict(outputs)
                if instance_ids and public_ips:
                    print(f"[TerraformManager] read_state: found {len(instance_ids)} node(s) via terraform output")
                    for i, (iid, ip) in enumerate(zip(instance_ids, public_ips), 1):
                        print(f"  node{i}: {ip}  ({iid})")
                    return instance_ids, public_ips, fqdn
        except Exception as e:
            print(f"[TerraformManager] read_state: terraform output failed ({e}) — trying tfstate file ...")

        # ── Method 2: parse terraform.tfstate directly ────────────────────────
        if tfstate_path.exists():
            try:
                state = json.loads(tfstate_path.read_text())
                instance_ids, public_ips = self._extract_from_tfstate(state)
                if instance_ids and public_ips:
                    print(f"[TerraformManager] read_state: found {len(instance_ids)} node(s) via tfstate file")
                    for i, (iid, ip) in enumerate(zip(instance_ids, public_ips), 1):
                        print(f"  node{i}: {ip}  ({iid})")
                    return instance_ids, public_ips, ""
            except Exception as e:
                print(f"[TerraformManager] read_state: tfstate parse failed ({e})")

        # ── Method 3: no state found — return empty, caller handles ──────────
        print(f"[TerraformManager] read_state: no tfstate found at {tfstate_path} — skipping NSG for secondary nodes")
        return [], [], ""

    # ── private ───────────────────────────────────────────────────────────────

    def _write_tfvars(self, display_name: str, dns_cfg: Optional[dict],
                      controller_profile=None):
        p  = self.profile
        ha = getattr(p, 'ha', False)

        if not Path(p.ssh_public_key).exists():
            raise FileNotFoundError(f"SSH public key not found: {p.ssh_public_key}")

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
        return self._parse_outputs_from_dict(outputs)

    def _parse_outputs_from_dict(self, outputs: dict) -> tuple:
        """Parse instance_ids, public_ips, fqdn from terraform output dict."""
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

    def _extract_from_tfstate(self, state: dict) -> tuple:
        """
        Extract instance OCIDs and public IPs directly from tfstate JSON.
        Handles both terraform 0.13+ (resources array) format.
        """
        instance_ids = []
        public_ips   = []

        resources = state.get("resources", [])
        for resource in resources:
            # Only look at OCI compute instances
            if resource.get("type") != "oci_core_instance":
                continue
            for instance in resource.get("instances", []):
                attrs = instance.get("attributes", {})
                ocid  = attrs.get("id", "")
                ip    = attrs.get("public_ip", "") or ""
                if ocid:
                    instance_ids.append(ocid)
                if ip:
                    public_ips.append(ip)

        # Sort by display_name so node1/node2/node3 order is consistent
        # tfstate doesn't guarantee order — zip by index after sorting
        return instance_ids, public_ips

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