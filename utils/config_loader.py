"""Load and validate YAML config, providing a typed ControllerProfile."""

import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


VALID_SIZES   = {"S", "M", "L", "POC"}
VALID_OS      = {"ubuntu24", "rhel8", "rhel9", "rhel10"}
# S/M/L are HA-only; POC is the only Non-HA option (per Jenkinsfile's
# controller_size description: "POC → Non-HA | S/M/L → HA only", added
# 2026-07-22). NOTE: POC's actual cpu/memory values below are a PLACEHOLDER
# -- not yet confirmed with the team, see resolve_size_profile() and the
# cpu/memory_gb properties below.
SIZE_HA_RULES = {
    "S":   [True],
    "M":   [True],
    "L":   [True],
    "POC": [False],
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
        # POC value is a PLACEHOLDER (4 CPU) -- not yet confirmed with the
        # team. Update once the real POC spec is known.
        return {"S": 16, "M": 24, "L": 48, "POC": 4}[self.controller_size]

    @property
    def memory_gb(self) -> int:
        # POC value is a PLACEHOLDER (16GB) -- not yet confirmed with the
        # team. Update once the real POC spec is known.
        return {"S": 64, "M": 64, "L": 192, "POC": 16}[self.controller_size]

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
      S    →  16 OCPUs / 64GB   — HA only
      M    →  24 OCPUs / 64GB   — HA only
      L    →  48 OCPUs / 192GB  — HA only
      POC  →  4 OCPUs / 16GB    — Non-HA only (PLACEHOLDER -- not yet confirmed)

    Returns:
        (ocpus: float, memory_gb: float)
    """
    # POC value is a PLACEHOLDER -- not yet confirmed with the team.
    SIZE_PROFILES = {
        "S":   (16.0,  64.0),
        "M":   (24.0,  64.0),
        "L":   (48.0, 192.0),
        "POC": (4.0,   16.0),
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


@dataclass
class PackageProfile:
    """
    Represents a controller installation/upgrade package.

    UPDATED per team design review (2026-07-22): package_url is now the
    ONLY input -- no more --package-name fallback, no PACKAGE_PATTERN
    regex-based version/build extraction, no RC_PATTERN/dev-vs-prod bucket
    guessing (PROD_S3_BASE/DEV_S3_BASE routing is gone entirely). Every
    one of those mechanisms existed to compensate for NOT having a full
    URL up front; now that Jenkins always supplies one directly
    (src_package_url / dst_package_url on the two upgrade-capable
    run_modes), none of that guessing is needed -- or wanted, since it's
    exactly what caused the earlier bugs where "-2"/"-1" suffixes got
    silently dropped, and where a real dev-bucket package with no "-RC"
    marker got misrouted to the prod bucket.

    `name` is derived purely as the URL's last path segment -- a plain
    string split, not a version-parsing regex -- so it's reliable
    regardless of naming convention (RC suffixes, dash-vs-dot, differing
    prefixes like "rafay-v3-airgap-controller-" vs
    "rafay-airgapped-controller-" -- none of it matters here, since
    nothing is extracted FROM the name; it's just used verbatim for
    tar_path/extract_dir).

    `version` is a best-effort COSMETIC label only, used purely to make
    print()/report output readable -- e.g.
    "rafay-airgapped-controller-v3.1-40-1.tar.gz" -> "3.1-40-1". Nothing
    in this class or its callers makes any decision based on this value;
    a wrong or empty label has zero functional impact. This mirrors
    lib/upgrade/upgrade_engine.py's UpgradeEngine._cosmetic_version_label,
    for the same reason.
    """
    url: str
    install_dir: str

    def __post_init__(self):
        if not self.url:
            raise ValueError("PackageProfile requires a non-empty url.")
        if not self.url.endswith(".tar.gz"):
            raise ValueError(f"Package URL '{self.url}' must end with .tar.gz")

        self.name = self.url.rsplit("/", 1)[-1]
        self.version = self._cosmetic_version_label(self.name)

    @staticmethod
    def _cosmetic_version_label(package_name: str) -> str:
        """Best-effort label for logs/reports only -- see class docstring."""
        m = _re.search(r'v?([\d.]+(?:-\d+)*)\.tar\.gz', package_name)
        return m.group(1) if m else ""

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
            f"version={self.version or 'unknown'} | "
            f"url={self.url}"
        )


def load_package_profile(cfg: dict, package_url: str) -> PackageProfile:
    """
    Build a PackageProfile from a full package URL.

    UPDATED per team design review (2026-07-22): package_url is now
    REQUIRED -- no --package-name fallback, no dev.yaml package.name
    fallback, no CONTROLLER_PACKAGE env var fallback. Jenkins always
    supplies a full URL directly now (conftest.py's package_profile
    fixture raises before even calling this if --package-url wasn't
    passed at all).
    """
    if not package_url:
        raise ValueError(
            "package_url is required.\n"
            "Pass the full package URL, e.g.:\n"
            "  https://.../rafay-airgapped-controller-v3.1-39.tar.gz"
        )

    pkg_cfg = cfg.get("package", {})
    return PackageProfile(
        url=package_url,
        install_dir=pkg_cfg.get("install_dir", "/opt/rafay"),
    )