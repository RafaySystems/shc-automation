"""Load and validate YAML config, providing a typed ControllerProfile."""

import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


VALID_SIZES   = {"S", "M", "L"}
VALID_OS      = {"ubuntu24", "rhel8", "rhel9"}
# Size S is non-HA only; Size L is HA only
SIZE_HA_RULES = {
    "S": [True],  # S → both
    "M": [True], # M → both
    "L": [True],        # L → HA only
}


@dataclass
class ControllerProfile:
    ip: str
    user: str
    ssh_key: str
    controller_size: str   # S | M | L
    ha: bool               # True = HA, False = single node
    os_type: str           # ubuntu24 | rhel8 | rhel9
    ssh_public_key: str = ""   # used by Terraform/cloud-init to authorize the VM at creation time

    def __post_init__(self):
        # Validate size
        if self.controller_size not in VALID_SIZES:
            raise ValueError(
                f"controller_size '{self.controller_size}' is invalid. "
                f"Choose from: {sorted(VALID_SIZES)}"
            )
        # Validate OS
        if self.os_type not in VALID_OS:
            raise ValueError(
                f"os_type '{self.os_type}' is invalid. "
                f"Choose from: {sorted(VALID_OS)}"
            )
        # Validate size/HA combination
        allowed_ha = SIZE_HA_RULES[self.controller_size]
        if self.ha not in allowed_ha:
            mode = "HA" if self.ha else "Non-HA"
            raise ValueError(
                f"Size '{self.controller_size}' does not support {mode} mode. "
                f"Size S = Non-HA only | Size M = both | Size L = HA only."
            )
        # Expand ~ and any ${ENV_VAR} references in key paths
        self.ssh_key = str(Path(os.path.expandvars(self.ssh_key)).expanduser())
        if self.ssh_public_key:
            self.ssh_public_key = str(Path(os.path.expandvars(self.ssh_public_key)).expanduser())

    @property
    def mode_label(self) -> str:
        return "HA" if self.ha else "Non-HA"

    @property
    def cpu(self) -> int:
        return {"S": 16, "M": 24, "L": 48}[self.controller_size]

    @property
    def memory_gb(self) -> int:
        return {"S": 64, "M": 64, "L": 192}[self.controller_size]

    def summary(self) -> str:
        return (
            f"Controller Profile | "
            f"Size={self.controller_size} ({self.cpu}CPU/{self.memory_gb}GB) | "
            f"Mode={self.mode_label} | "
            f"OS={self.os_type} | "
            f"IP={self.ip}"
        )


def load_config(env: str = "dev") -> dict:
    """Load raw YAML config for the given env (e.g. 'dev', 'staging')."""
    config_path = Path(__file__).parent.parent / "config" / f"{env}.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path) as f:
        return yaml.safe_load(f)


def load_controller_profile(
    env: str = "dev",
    # CLI overrides — any of these can be passed directly from conftest.py
    controller_ip: Optional[str] = None,
    ssh_key: Optional[str] = None,
    ssh_public_key: Optional[str] = None,
    ssh_user: Optional[str] = None,
    controller_size: Optional[str] = None,
    ha: Optional[bool] = None,
    os_type: Optional[str] = None,
) -> ControllerProfile:
    """
    Build a ControllerProfile.

    Priority: CLI arg > env var > dev.yaml value.
    """
    cfg = load_config(env)
    ctrl = cfg.get("controller", {})

    def resolve(cli_val, env_var, yaml_val):
        """CLI → env var → YAML."""
        if cli_val is not None:
            return cli_val
        env_val = os.environ.get(env_var)
        if env_val is not None:
            return env_val
        return yaml_val

    resolved_ha = ha
    if resolved_ha is None:
        env_ha = os.environ.get("CONTROLLER_HA")
        if env_ha is not None:
            resolved_ha = env_ha.lower() in ("true", "1", "yes")
        else:
            resolved_ha = ctrl.get("ha", False)

    return ControllerProfile(
        ip=resolve(controller_ip, "CONTROLLER_IP", ctrl["ip"]),
        user=resolve(ssh_user, "CONTROLLER_USER", ctrl.get("user", "ubuntu")),
        ssh_key=resolve(ssh_key, "CONTROLLER_SSH_KEY", ctrl["ssh_key"]),
        ssh_public_key=resolve(
            ssh_public_key, "CONTROLLER_SSH_PUBLIC_KEY", ctrl.get("ssh_public_key", "")
        ),
        controller_size=resolve(controller_size, "CONTROLLER_SIZE", ctrl.get("controller_size", "S")),
        ha=resolved_ha,
        os_type=resolve(os_type, "CONTROLLER_OS_TYPE", ctrl.get("os_type", "ubuntu24")),
    )


# ── OCI profile helpers ───────────────────────────────────────────────────────

def resolve_image_id(cfg: dict, os_type: str, override_image_id: str = "") -> str:
    """
    Resolve OCI image_id for a given os_type.

    Priority:
      1. Hard override (oci.image_id in dev.yaml or passed directly) — escape hatch for testing
      2. oci_images[os_type] in dev.yaml
      3. Raise — never silently use a wrong image

    Example dev.yaml:
        oci_images:
          ubuntu24: "ocid1.image.oc1.phx.aaa..."
          rhel8:    ""    # future
    """
    # Priority 1 — hard override
    if override_image_id:
        print(f"[config_loader] image_id: using hard override → {override_image_id}")
        return override_image_id

    # Priority 2 — oci_images map
    oci_images = cfg.get("oci_images", {})
    image_id = oci_images.get(os_type, "")
    if image_id:
        print(f"[config_loader] image_id: resolved from oci_images[{os_type}] → {image_id}")
        return image_id

    # Priority 3 — fail loudly
    available = [k for k, v in oci_images.items() if v]
    raise ValueError(
        f"No OCI image_id found for os_type='{os_type}'.\n"
        f"Add it under 'oci_images:' in dev.yaml.\n"
        f"Currently configured: {available or 'none'}"
    )


def resolve_size_profile(controller_size: str) -> tuple:
    """
    Resolve ocpus and memory_gb for a given controller_size.

    Values are owned here in config_loader.py — no dev.yaml entry needed.
    dev.yaml only needs to say controller_size: S | M | L

    Size rules:
      S  →  32 OCPUs / 64GB   — Non-HA only
      M  →  32 OCPUs / 64GB   — HA or Non-HA
      L  →  128 OCPUs / 192GB — HA only

    Returns:
        (ocpus: float, memory_gb: float)
    """
    SIZE_PROFILES = {
        "S": (16.0,  64.0),
        "M": (24.0,  64.0),
        "L": (48.0, 192.0),
    }

    if controller_size not in SIZE_PROFILES:
        raise ValueError(
            f"controller_size '{controller_size}' is invalid. "
            f"Choose from: {sorted(SIZE_PROFILES.keys())}"
        )

    ocpus, memory_gb = SIZE_PROFILES[controller_size]
    print(f"[config_loader] size_profile: {controller_size} → {ocpus} OCPUs / {memory_gb}GB RAM")
    return ocpus, memory_gb


# ── Package profile ────────────────────────────────────────────────────────────

import re as _re

S3_BASE = "https://rafay-airgap-controller.s3.us-west-2.amazonaws.com"
PACKAGE_PATTERN = _re.compile(r"rafay-airgapped-controller-v([\d.]+)-(\d+)\.tar\.gz")


@dataclass
class PackageProfile:
    """
    Represents a controller installation package.

    Built from the 'package:' section of dev.yaml.
    The download URL is derived automatically from the package name unless
    a custom url is provided.

    Example package name:
        rafay-airgapped-controller-v3.1-39.tar.gz
        → version = 3.1
        → build   = 39
        → url     = https://rafay-airgap-controller.s3.../3.1/rafay-airgapped-controller-v3.1-39.tar.gz
    """
    name: str           # e.g. rafay-airgapped-controller-v3.1-39.tar.gz
    install_dir: str    # e.g. /opt/rafay
    url: str = ""       # auto-derived if empty

    def __post_init__(self):
        m = PACKAGE_PATTERN.match(self.name)
        if not m:
            raise ValueError(
                f"Package name '{self.name}' does not match expected format.\n"
                f"Expected: rafay-airgapped-controller-v{{version}}-{{build}}.tar.gz\n"
                f"Example:  rafay-airgapped-controller-v3.1-39.tar.gz"
            )
        self.version = m.group(1)   # e.g. "3.1"
        self.build   = m.group(2)   # e.g. "39"

        # Derive URL from name if not explicitly set
        if not self.url:
            self.url = f"{S3_BASE}/{self.version}/{self.name}"

    @property
    def extract_dir(self) -> str:
        """The directory the tar extracts into, e.g. /opt/rafay/rafay-airgapped-controller-v3.1-39"""
        return f"{self.install_dir}/{self.name.replace('.tar.gz', '')}"

    @property
    def tar_path(self) -> str:
        """Full path to the downloaded tar on the remote VM."""
        return f"{self.install_dir}/{self.name}"

    def summary(self) -> str:
        return (
            f"Package | name={self.name} | "
            f"version={self.version} | build={self.build} | "
            f"url={self.url}"
        )


def load_package_profile(cfg: dict, package_name: Optional[str] = None,
                          package_url: Optional[str] = None) -> PackageProfile:
    """
    Build a PackageProfile from the 'package:' section of dev.yaml.

    Priority: CLI --package-url > CLI --package-name > env var > dev.yaml value.
    If package_url is provided, name is extracted from URL automatically.
    """
    pkg_cfg = cfg.get("package", {})

    # If URL provided, extract name from it
    if package_url and not package_name:
        package_name = package_url.split("/")[-1]

    name = (
        package_name
        or os.environ.get("CONTROLLER_PACKAGE")
        or pkg_cfg.get("name", "")
    )
    if not name:
        raise ValueError(
            "No package name set.\n"
            "Set 'package.name' in dev.yaml or pass --package-name on the CLI.\n"
            "Example: rafay-airgapped-controller-v3.1-39.tar.gz"
        )

    # URL priority: CLI --package-url > dev.yaml url > auto-derived from name
    url = package_url or pkg_cfg.get("url", "")

    return PackageProfile(
        name=name,
        install_dir=pkg_cfg.get("install_dir", "/opt/rafay"),
        url=url,
    )