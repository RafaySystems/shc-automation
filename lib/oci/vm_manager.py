"""
lib/oci/vm_manager.py

OCI VM management:
  - OCIProfile      : dataclass sourced from dev.yaml
  - OCIVMManager    : create/terminate instances
  - OCINSGManager   : attach/detach NSG to VM VNIC for temporary internet access
"""

import time
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import oci


# ── Profile dataclass ─────────────────────────────────────────────────────────

@dataclass
class OCIProfile:
    config_file: str
    profile: str
    compartment_id: str
    availability_domain: str
    vcn_id: str
    subnet_id: str
    image_id: str
    shape: str
    ocpus: float
    memory_gb: float
    ssh_public_key: str
    display_name: str
    nsg_id: str = ""
    boot_volume_gb: int = 500
    data_volume_gb: int = 1024
    tags: dict = field(default_factory=dict)
    boot_timeout: int = 300
    ha: bool = False               # HA mode — 3 nodes when True

    def __post_init__(self):
        # os.path.expandvars resolves ${RAUTO_OCI_SSH_PUBLIC_KEY}-style
        # placeholders in dev.yaml before ~ expansion.
        self.config_file    = str(Path(os.path.expandvars(self.config_file)).expanduser())
        self.ssh_public_key = str(Path(os.path.expandvars(self.ssh_public_key)).expanduser())

    def resolve_display_name(self, build_no: Optional[str] = None) -> str:
        if build_no:
            return self.display_name.format(build_no=build_no)
        return self.display_name

    def validate(self):
        print("\n[OCIProfile] ── Resolved config ──────────────────────────────")
        print(f"  config_file         : {self.config_file}")
        print(f"  profile             : {self.profile}")
        print(f"  compartment_id      : {self.compartment_id}")
        print(f"  availability_domain : {self.availability_domain}")
        print(f"  vcn_id              : {self.vcn_id}")
        print(f"  subnet_id           : {self.subnet_id}")
        print(f"  nsg_id              : {self.nsg_id or 'NOT SET'}")
        print(f"  image_id            : {self.image_id}")
        print(f"  shape               : {self.shape}")
        print(f"  ocpus               : {self.ocpus}")
        print(f"  memory_gb           : {self.memory_gb}")
        print(f"  boot_volume_gb      : {self.boot_volume_gb}")
        print(f"  data_volume_gb      : {self.data_volume_gb}")
        print(f"  ssh_public_key      : {self.ssh_public_key}")
        print(f"  display_name        : {self.display_name}")
        print(f"  tags                : {self.tags}")
        print(f"  boot_timeout        : {self.boot_timeout}")
        print("[OCIProfile] ───────────────────────────────────────────────────\n")

        bad = [k for k, v in {
            "compartment_id": self.compartment_id,
            "subnet_id":      self.subnet_id,
            "image_id":       self.image_id,
        }.items() if "XXXX" in str(v) or not v]
        if bad:
            raise ValueError(
                f"These fields still have placeholder values in dev.yaml: {bad}\n"
                f"Fill in real OCIDs before running."
            )
        if self.availability_domain in ("AD-1", "AD-2", "AD-3"):
            raise ValueError(
                f"availability_domain '{self.availability_domain}' is not valid.\n"
                f"OCI requires the full name e.g. 'PaOl:PHX-AD-1'.\n"
                f"Run: oci iam availability-domain list --compartment-id <id> --query \"data[*].name\""
            )


def load_oci_profile(cfg: dict, build_no: Optional[str] = None) -> OCIProfile:
    """Build OCIProfile from dev.yaml. Env vars override YAML values."""
    oci_cfg = cfg.get("oci", {})

    def resolve(yaml_val, env_var):
        return os.environ.get(env_var, yaml_val)

    # ssh_public_key: check RAUTO_OCI_SSH_PUBLIC_KEY (set by Jenkins
    # credentials at pipeline runtime) first, then fall back to the
    # OCI-namespaced override, then finally dev.yaml's own value.
    resolved_ssh_public_key = (
        os.environ.get("RAUTO_OCI_SSH_PUBLIC_KEY")
        or os.environ.get("OCI_SSH_PUBLIC_KEY")
        or oci_cfg["ssh_public_key"]
    )

    return OCIProfile(
        config_file=resolve(oci_cfg.get("config_file", "~/.oci/config"), "OCI_CONFIG_FILE"),
        profile=resolve(oci_cfg.get("profile", "DEFAULT"), "OCI_PROFILE"),
        compartment_id=resolve(oci_cfg["compartment_id"], "OCI_COMPARTMENT_ID"),
        availability_domain=resolve(oci_cfg["availability_domain"], "OCI_AD"),
        vcn_id=resolve(oci_cfg.get("vcn_id", ""), "OCI_VCN_ID"),
        subnet_id=resolve(oci_cfg["subnet_id"], "OCI_SUBNET_ID"),
        nsg_id=resolve(oci_cfg.get("nsg_id", ""), "OCI_NSG_ID"),
        image_id=resolve(oci_cfg["image_id"], "OCI_IMAGE_ID"),
        shape=resolve(oci_cfg.get("shape", "VM.Standard.E5.Flex"), "OCI_SHAPE"),
        ocpus=float(resolve(oci_cfg.get("ocpus", 16), "OCI_OCPUS")),
        memory_gb=float(resolve(oci_cfg.get("memory_gb", 64), "OCI_MEMORY_GB")),
        boot_volume_gb=int(resolve(oci_cfg.get("boot_volume_gb", 500), "OCI_BOOT_VOLUME_GB")),
        data_volume_gb=int(resolve(oci_cfg.get("data_volume_gb", 1024), "OCI_DATA_VOLUME_GB")),
        ssh_public_key=resolved_ssh_public_key,
        display_name=resolve(oci_cfg.get("display_name", "rafay-controller"), "OCI_DISPLAY_NAME"),
        ha=bool(cfg.get("controller", {}).get("ha", False)),
        tags=oci_cfg.get("tags") or {},
        boot_timeout=int(resolve(oci_cfg.get("boot_timeout", 300), "OCI_BOOT_TIMEOUT")),
    )


# ── VM manager ────────────────────────────────────────────────────────────────

class OCIVMManager:

    def __init__(self, profile: OCIProfile):
        self.profile = profile
        self._oci_config = oci.config.from_file(
            file_location=profile.config_file,
            profile_name=profile.profile,
        )
        self.compute_client      = oci.core.ComputeClient(self._oci_config)
        self.network_client      = oci.core.VirtualNetworkClient(self._oci_config)
        self.blockstorage_client = oci.core.BlockstorageClient(self._oci_config)

    def create_instance(self, build_no: Optional[str] = None) -> tuple:
        p = self.profile
        p.validate()
        display_name = p.resolve_display_name(build_no)
        pub_key      = self._read_public_key()

        shape_config = None
        if "flex" in p.shape.lower():
            shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=p.ocpus,
                memory_in_gbs=p.memory_gb,
            )

        details = oci.core.models.LaunchInstanceDetails(
            compartment_id=p.compartment_id,
            availability_domain=p.availability_domain,
            display_name=display_name,
            image_id=p.image_id,
            shape=p.shape,
            shape_config=shape_config,
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                image_id=p.image_id,
                boot_volume_size_in_gbs=p.boot_volume_gb,
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=p.subnet_id,
                assign_public_ip=True,
            ),
            metadata={"ssh_authorized_keys": pub_key},
            freeform_tags=p.tags if p.tags else {},
        )

        response    = self.compute_client.launch_instance(details)
        instance_id = response.data.id
        print(f"[OCIVMManager] Launched: {display_name} ({instance_id})")

        self._wait_for_running(instance_id)
        self._attach_data_volume(instance_id, display_name)
        public_ip = self._get_public_ip(instance_id)
        print(f"[OCIVMManager] RUNNING — public IP: {public_ip}")
        return instance_id, public_ip

    def terminate_instance(self, instance_id: str, wait: bool = True):
        self.compute_client.terminate_instance(instance_id, preserve_boot_volume=False)
        print(f"[OCIVMManager] Termination requested: {instance_id}")
        if wait:
            self._wait_for_state(instance_id, "TERMINATED", timeout=300)
            print(f"[OCIVMManager] Terminated: {instance_id}")

    def get_instance_state(self, instance_id: str) -> str:
        return self.compute_client.get_instance(instance_id).data.lifecycle_state

    def list_instances(self, display_name_prefix: str = "") -> list:
        instances = oci.pagination.list_call_get_all_results(
            self.compute_client.list_instances,
            compartment_id=self.profile.compartment_id,
        ).data
        results = []
        for inst in instances:
            if inst.lifecycle_state == "TERMINATED":
                continue
            if display_name_prefix and not inst.display_name.startswith(display_name_prefix):
                continue
            results.append({
                "id":           inst.id,
                "display_name": inst.display_name,
                "state":        inst.lifecycle_state,
                "public_ip":    self._get_public_ip(inst.id, silent=True),
            })
        return results

    def _attach_data_volume(self, instance_id: str, display_name: str):
        p = self.profile
        print(f"[OCIVMManager] Creating {p.data_volume_gb}GB data volume ...")
        vol = self.blockstorage_client.create_volume(
            oci.core.models.CreateVolumeDetails(
                compartment_id=p.compartment_id,
                availability_domain=p.availability_domain,
                display_name=f"{display_name}-data",
                size_in_gbs=p.data_volume_gb,
                freeform_tags=p.tags if p.tags else {},
            )
        ).data.id
        deadline = time.time() + 120
        while time.time() < deadline:
            state = self.blockstorage_client.get_volume(vol).data.lifecycle_state
            if state == "AVAILABLE":
                break
            time.sleep(10)
        self.compute_client.attach_volume(
            oci.core.models.AttachParavirtualizedVolumeDetails(
                instance_id=instance_id, volume_id=vol,
                display_name=f"{display_name}-data", is_read_only=False,
            )
        )
        print(f"[OCIVMManager] Data volume attached: {vol}")

    def _read_public_key(self) -> str:
        key_path = Path(self.profile.ssh_public_key)
        if not key_path.exists():
            raise FileNotFoundError(f"SSH public key not found: {key_path}")
        content = key_path.read_text().strip()
        if not content.startswith("ssh-"):
            raise ValueError(f"File at {key_path} does not look like an SSH public key.")
        return content

    def _wait_for_running(self, instance_id: str) -> str:
        deadline = time.time() + self.profile.boot_timeout
        while time.time() < deadline:
            state = self.get_instance_state(instance_id)
            print(f"[OCIVMManager] State: {state} — waiting for RUNNING ...")
            if state == "RUNNING":
                return instance_id
            if state in ("TERMINATED", "TERMINATING"):
                raise RuntimeError(f"Instance {instance_id} moved to {state} unexpectedly")
            time.sleep(15)
        raise TimeoutError(f"Instance did not reach RUNNING within {self.profile.boot_timeout}s")

    def _wait_for_state(self, instance_id: str, target_state: str, timeout: int = 300):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.get_instance_state(instance_id) == target_state:
                return
            time.sleep(10)
        raise TimeoutError(f"Instance did not reach {target_state} within {timeout}s")

    def _get_public_ip(self, instance_id: str, silent: bool = False) -> Optional[str]:
        attachments = oci.pagination.list_call_get_all_results(
            self.compute_client.list_vnic_attachments,
            compartment_id=self.profile.compartment_id,
            instance_id=instance_id,
        ).data
        for att in attachments:
            if att.lifecycle_state != "ATTACHED":
                continue
            vnic = self.network_client.get_vnic(att.vnic_id).data
            if vnic.public_ip:
                return vnic.public_ip
        if not silent:
            raise RuntimeError(f"No public IP found for instance {instance_id}")
        return None


# ── NSG Manager ───────────────────────────────────────────────────────────────

class OCINSGManager:
    """
    Attach and detach a Network Security Group (NSG) to/from a VM's primary VNIC.

    The NSG must already have egress rules (0.0.0.0/0 TCP + UDP) configured
    in OCI Console. This class just plugs/unplugs it from the VM VNIC.

    Flow:
        bringup VM → attach NSG → apt install aria2 + S3 download → detach NSG

    Usage as context manager:
        with OCINSGManager(profile, instance_id) as mgr:
            ssh.run("apt install aria2")
            ssh.run("aria2c -x 16 ...")
        # NSG detached automatically

    Usage manual:
        mgr = OCINSGManager(profile, instance_id)
        mgr.attach()
        try:
            ...
        finally:
            mgr.detach()
    """

    def __init__(self, profile: OCIProfile, instance_id: str):
        if not profile.nsg_id:
            raise ValueError(
                "oci.nsg_id is not set in dev.yaml.\n"
                "Add the NSG OCID under the oci: section."
            )
        self.profile     = profile
        self.instance_id = instance_id
        self._vnic_id    = None

        self._oci_config = oci.config.from_file(
            file_location=profile.config_file,
            profile_name=profile.profile,
        )
        self.compute_client = oci.core.ComputeClient(self._oci_config)
        self.network_client = oci.core.VirtualNetworkClient(self._oci_config)

    def __enter__(self):
        self.attach()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.detach()
        return False

    def attach(self):
        """Attach the NSG to the instance's primary VNIC. Idempotent."""
        vnic_id       = self._get_primary_vnic_id()
        self._vnic_id = vnic_id
        vnic          = self.network_client.get_vnic(vnic_id).data
        current_nsgs  = list(vnic.nsg_ids or [])

        if self.profile.nsg_id in current_nsgs:
            print(f"[OCINSGManager] NSG already attached — skipping")
            return

        self.network_client.update_vnic(
            vnic_id,
            oci.core.models.UpdateVnicDetails(nsg_ids=current_nsgs + [self.profile.nsg_id]),
        )
        print(f"[OCINSGManager] NSG attached to VNIC {vnic_id}: {self.profile.nsg_id}")

    def detach(self):
        """Detach the NSG from the instance's primary VNIC. Idempotent."""
        vnic_id = self._vnic_id or self._get_primary_vnic_id()
        vnic    = self.network_client.get_vnic(vnic_id).data
        current_nsgs = list(vnic.nsg_ids or [])

        if self.profile.nsg_id not in current_nsgs:
            print(f"[OCINSGManager] NSG not attached — nothing to detach")
            return

        updated = [n for n in current_nsgs if n != self.profile.nsg_id]
        self.network_client.update_vnic(
            vnic_id,
            oci.core.models.UpdateVnicDetails(nsg_ids=updated),
        )
        print(f"[OCINSGManager] NSG detached from VNIC {vnic_id}: {self.profile.nsg_id}")

    def _get_primary_vnic_id(self) -> str:
        attachments = oci.pagination.list_call_get_all_results(
            self.compute_client.list_vnic_attachments,
            compartment_id=self.profile.compartment_id,
            instance_id=self.instance_id,
        ).data
        for att in attachments:
            if att.lifecycle_state == "ATTACHED":
                return att.vnic_id
        raise RuntimeError(f"No attached VNIC found for instance {self.instance_id}")