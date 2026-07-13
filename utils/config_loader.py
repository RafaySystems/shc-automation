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

# Two separate S3 locations, two separate path conventions:
#
#   PROD (official releases):
#     https://rafay-airgap-controller.s3.us-west-2.amazonaws.com/{version}/{name}
#     e.g. .../3.1/rafay-airgapped-controller-v3.1-40.tar.gz
#
#   DEV / RC (release-candidate / automation builds):
#     https://dev-rafay-controller.s3.us-west-1.amazonaws.com/Automation/{name}
#     e.g. .../Automation/rafay-airgapped-controller-v3.1-40-RC48541-1-35.tar.gz
#     -- flat "Automation/" prefix, NO version subfolder.
#
# Which bucket a name belongs to is detected by the presence of an
# "-RC<digits>" segment (e.g. "-RC48541") -- that's the signal that
# distinguishes an RC/dev build from a prod release build.
PROD_S3_BASE = "https://rafay-airgap-controller.s3.us-west-2.amazonaws.com"
DEV_S3_BASE  = "https://dev-rafay-controller.s3.us-west-1.amazonaws.com/Automation"

RC_PATTERN = _re.compile(r"-RC\d+")

# Best-effort version/build extraction -- NOT a strict full-name validator.
# Uses .search() (not .match()/fullmatch()) so it finds "v{version}-{build}"
# wherever it appears in the name and ignores anything after it -- this is
# what lets RC/dev builds like:
#   rafay-airgapped-controller-v3.1-40-RC48541-1-35.tar.gz
# still resolve to version=3.1, build=40, rather than being rejected outright
# for having extra suffix content after the build number.
#
# The 'v' prefix on the version is optional -- all of these match:
#   rafay-airgapped-controller-v3.1-39.tar.gz          -> 3.1 / 39
#   rafay-airgapped-controller-4.2-1.tar.gz            -> 4.2 / 1
#   rafay-airgapped-controller-v3.1-40-RC48541-1-35... -> 3.1 / 40
PACKAGE_PATTERN = _re.compile(r"rafay-airgapped-controller-v?([\d.]+)-(\d+)")


@dataclass
class PackageProfile:
    """
    Represents a controller installation package.

    Built from the 'package:' section of dev.yaml.
    The download URL is derived automatically from the package name when it
    matches the standard rafay-airgapped-controller-v{version}-{build}...
    convention (RC/dev suffixes after the build number are fine). For any
    other .tar.gz package name -- one that doesn't match closely enough to
    extract a version -- pass --package-url (or --dst-package-url) explicitly
    so the download URL doesn't have to be guessed.

    The only HARD requirement on the name itself is that it ends in .tar.gz.
    Everything else (version, build) is best-effort metadata used for
    convenience (auto-deriving the S3 URL, populating reports) -- it is not
    a gate on whether the package can be used.

    Example package names (the 'v' prefix on the version is optional):
        rafay-airgapped-controller-v3.1-39.tar.gz
        → version = 3.1
        → build   = 39
        → url     = https://rafay-airgap-controller.s3.../3.1/rafay-airgapped-controller-v3.1-39.tar.gz

        rafay-airgapped-controller-4.2-1.tar.gz
        → version = 4.2
        → build   = 1

        rafay-airgapped-controller-v3.1-40-RC48541-1-35.tar.gz
        → version = 3.1
        → build   = 40
        → url     = https://dev-rafay-controller.s3.us-west-1.amazonaws.com/Automation/rafay-airgapped-controller-v3.1-40-RC48541-1-35.tar.gz
        (detected as an RC/dev build via the "-RC<digits>" segment -- routed
        to the dev bucket's flat Automation/ prefix, not the prod versioned
        path. RC suffix is ignored for version/build extraction, but is kept
        as-is in `name` / `tar_path` / `extract_dir` -- the RC identifier
        is part of the actual filename on disk and in S3, so it must not
        be stripped there)
    """
    name: str           # e.g. rafay-airgapped-controller-v3.1-39.tar.gz
    install_dir: str    # e.g. /opt/rafay
    url: str = ""       # auto-derived if possible, else must be passed in

    def __post_init__(self):
        # The only hard requirement: this has to be a .tar.gz file. Everything
        # else about the name is best-effort.
        if not self.name.endswith(".tar.gz"):
            raise ValueError(
                f"Package name '{self.name}' must end with .tar.gz"
            )

        m = PACKAGE_PATTERN.search(self.name)
        if m:
            self.version = m.group(1)   # e.g. "3.1"
            self.build   = m.group(2)   # e.g. "40"
        else:
            # Doesn't match the known naming convention closely enough to
            # extract a version -- that's fine, but we can no longer
            # auto-derive the S3 URL below, so an explicit url becomes
            # mandatory in that case.
            self.version = ""
            self.build   = ""

        # Derive URL from name if not explicitly set.
        # RC/dev builds go to the flat Automation/ prefix (no version
        # subfolder); prod releases go to the versioned prod path.
        if not self.url:
            if RC_PATTERN.search(self.name):
                self.url = f"{DEV_S3_BASE}/{self.name}"
            elif self.version:
                self.url = f"{PROD_S3_BASE}/{self.version}/{self.name}"
            else:
                raise ValueError(
                    f"Could not auto-derive a version from package name "
                    f"'{self.name}' (expected something containing "
                    f"'rafay-airgapped-controller-[v]{{version}}-{{build}}').\n"
                    f"Pass --package-url (or --dst-package-url) explicitly "
                    f"for package names that don't follow this convention."
                )

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
            f"version={self.version or 'unknown'} | build={self.build or 'unknown'} | "
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