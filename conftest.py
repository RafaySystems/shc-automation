"""Root conftest — CLI options, session fixtures, controller profile injection."""

import os
import re
import pytest
from utils.config_loader import (
    load_controller_profile,
    load_config,
    load_package_profile,
    download_rauto_config_from_s3,
)


def pytest_addoption(parser):
    parser.addoption("--env",             default="dev",  help="Config env: dev | staging")
    parser.addoption("--controller-ip",   default=None,   help="Override controller IP (skips provisioning)")
    parser.addoption("--ssh-key",         default=None,   help="Path to SSH private key (.pem)")
    parser.addoption("--ssh-user",        default=None,   help="SSH username (default: ubuntu)")
    parser.addoption("--controller-size", default=None,   help="S | M | L")
    parser.addoption("--ha",              default=None,   help="true | false")
    parser.addoption("--os-type",         default=None,   help="ubuntu24 | rhel8 | rhel9")
    parser.addoption("--build-no",        default=None,   help="Build number -> VM display name e.g. 42")
    parser.addoption("--keep-vm",         action="store_true", default=False,
                     help="Skip terraform destroy after session")
    parser.addoption("--package-name",    default=None,   help="e.g. rafay-airgapped-controller-v3.1-39.tar.gz")
    parser.addoption("--package-url",     default=None,   help="Full S3 URL for package (optional — auto-built for prod)")
    parser.addoption("--secondary-ips",   default=None,   help="Comma-separated IPs of secondary nodes e.g. 1.2.3.4,5.6.7.8")
    parser.addoption("--secondary-ids",   default=None,   help="Comma-separated OCI instance IDs of secondary nodes")
    parser.addoption("--controller-instance-id", default=None, help="OCI instance ID of primary node (for NSG in static IP mode)")
    parser.addoption("--skip-bringup",    action="store_true", default=False,
                     help="Skip controller bringup — run validation tests only against existing install")
    # ── Upgrade flags ──────────────────────────────────────────────────────────
    parser.addoption("--src-package",     default=None,
                     help="Source package e.g. rafay-airgapped-controller-v3.1-39.tar.gz")
    parser.addoption("--dst-package",     default=None,
                     help="Dest package e.g. rafay-airgapped-controller-v3.1-40.tar.gz")
    parser.addoption("--dst-package-url", default=None,
                     help="Full URL for dest package (optional — auto-built for prod)")
    parser.addoption("--src-version",     default=None,
                     help="Source version e.g. 3.1-39 (auto-extracted from src-package if not set)")
    parser.addoption("--dst-version",     default=None,
                     help="Dest version e.g. 3.1-40 (auto-extracted from dst-package if not set)")
    parser.addoption("--skip-upgrade",    action="store_true", default=False,
                     help="Skip upgrade — run validation only")


# ── S3 config/keys fixture ──────────────────────────────────────────────────────
@pytest.fixture(scope="session", autouse=True)
def rauto_config_from_s3():
    """
    Pulls SSH keys and other infra config from S3 once per test session,
    before any fixture that needs oci_key / awstest.pem runs.

    Exposes the downloaded key paths via environment variables so dev.yaml
    (or any config file) can reference them with ${RAUTO_OCI_SSH_KEY} /
    ${RAUTO_AWS_SSH_KEY}, instead of hardcoding an absolute local path.
    """
    paths = download_rauto_config_from_s3()
    os.environ["RAUTO_OCI_SSH_KEY"]        = paths["ocipem"]   # private key — used by SSHClient to connect
    os.environ["RAUTO_OCI_SSH_PUBLIC_KEY"] = paths["ocipub"]   # public key  — used by Terraform/cloud-init at VM creation
    os.environ["RAUTO_AWS_SSH_KEY"]        = paths["awspem"]
    print(f"[conftest] SSH keys ready — oci={paths['ocipem']}  oci_pub={paths['ocipub']}  aws={paths['awspem']}")
    yield paths


@pytest.fixture(scope="session")
def raw_config(request, rauto_config_from_s3):
    cfg = load_config(request.config.getoption("--env"))
    request.session._raw_config = cfg
    return cfg


@pytest.fixture(scope="session")
def controller_profile(request, raw_config, rauto_config_from_s3):
    ha_raw = request.config.getoption("--ha")
    ha_override = None
    if ha_raw is not None:
        ha_override = ha_raw.lower() in ("true", "1", "yes")
    profile = load_controller_profile(
        env=request.config.getoption("--env"),
        controller_ip=request.config.getoption("--controller-ip"),
        ssh_key=request.config.getoption("--ssh-key") or os.environ.get("RAUTO_OCI_SSH_KEY"),
        ssh_public_key=os.environ.get("RAUTO_OCI_SSH_PUBLIC_KEY"),
        ssh_user=request.config.getoption("--ssh-user"),
        controller_size=request.config.getoption("--controller-size"),
        ha=ha_override,
        os_type=request.config.getoption("--os-type"),
    )
    print(f"\n[conftest] {profile.summary()}")
    return profile


@pytest.fixture(scope="session")
def ssh_client(request, raw_config, controller_profile):
    from lib.ssh.ssh_client import SSHClient

    cli_ip    = request.config.getoption("--controller-ip")
    provision = raw_config.get("controller", {}).get("provision", False)

    if cli_ip:
        print(f"\n[ssh_client] Using --controller-ip: {cli_ip}")
        ip = cli_ip

    elif provision:
        from lib.oci.vm_manager import load_oci_profile, OCINSGManager
        from lib.terraform.tf_manager import TerraformManager

        oci_profile = load_oci_profile(raw_config)
        dns_cfg     = raw_config.get("dns", {})
        tf_manager  = TerraformManager(oci_profile)
        build_no    = request.config.getoption("--build-no") or os.environ.get("BUILD_NUMBER")

        print("\n[ssh_client] Provisioning VM(s) via Terraform ...")
        instance_ids, public_ips, _ = tf_manager.apply(
            build_no=build_no,
            dns_cfg=dns_cfg,
            controller_profile=controller_profile,
        )
        instance_id = instance_ids[0]
        ip          = public_ips[0]
        print(f"[ssh_client] VM ready — id={instance_id}  ip={ip}")

        dns_mgr = None
        fqdn    = ""
        if dns_cfg.get("hosted_zone_id"):
            from lib.dns.route53_manager import Route53Manager
            display_name = oci_profile.resolve_display_name(build_no)
            dns_mgr = Route53Manager(dns_cfg, display_name)
            dns_mgr.create_record(ip)
            fqdn = dns_mgr.fqdn
            print(f"[ssh_client] DNS ready — {fqdn} → {ip}")
        else:
            print(f"[ssh_client] No dns.hosted_zone_id in dev.yaml — skipping DNS")

        nsg_mgr = None
        if oci_profile.nsg_id:
            print(f"[ssh_client] Attaching NSG to primary node ...")
            nsg_mgr = OCINSGManager(oci_profile, instance_id)
            nsg_mgr.attach()
            for sec_id in instance_ids[1:]:
                try:
                    sec_nsg = OCINSGManager(oci_profile, sec_id)
                    sec_nsg.attach()
                    print(f"[ssh_client] NSG attached to secondary node: {sec_id}")
                except Exception as e:
                    print(f"[ssh_client] NSG attach warning for {sec_id}: {e}")

        request.session._tf_instance_id  = instance_id
        request.session._tf_instance_ids = instance_ids
        request.session._tf_public_ip    = ip
        request.session._tf_public_ips   = public_ips
        request.session._tf_fqdn         = fqdn
        request.session._tf_manager      = tf_manager
        request.session._dns_manager     = dns_mgr
        request.session._nsg_manager     = nsg_mgr
        request.session._keep_vm         = request.config.getoption("--keep-vm")

    else:
        ip = controller_profile.ip
        if not ip:
            raise ValueError(
                "No IP available.\n"
                "Set controller.provision: true, controller.ip, or --controller-ip."
            )
        print(f"\n[ssh_client] Using static IP: {ip}")

    client = SSHClient(host=ip, user=controller_profile.user, key_path=controller_profile.ssh_key)
    client.connect()
    yield client

    # ── Teardown ──────────────────────────────────────────────────────────────
    client.disconnect()

    if provision and not cli_ip:
        nsg = getattr(request.session, "_nsg_manager", None)
        if nsg:
            try: nsg.detach()
            except Exception as e: print(f"[ssh_client] NSG detach warning: {e}")

        dns = getattr(request.session, "_dns_manager", None)
        ip  = getattr(request.session, "_tf_public_ip", "")
        if dns and ip:
            try: dns.delete_record(ip)
            except Exception as e: print(f"[ssh_client] DNS delete warning: {e}")

        tf   = getattr(request.session, "_tf_manager", None)
        keep = getattr(request.session, "_keep_vm", False)
        if tf:
            if keep:
                print(f"[ssh_client] --keep-vm set — skipping terraform destroy")
            else:
                print(f"[ssh_client] Running terraform destroy ...")
                tf.destroy()


@pytest.fixture(scope="session")
def secondary_ips(request, raw_config):
    cli_ips = request.config.getoption("--secondary-ips", default=None)
    if cli_ips:
        return [ip.strip() for ip in cli_ips.split(",") if ip.strip()]
    yaml_ips = raw_config.get("controller", {}).get("secondary_ips", [])
    yaml_ips_clean = [ip.strip() for ip in (yaml_ips or []) if ip and ip.strip()]
    if yaml_ips_clean:
        return yaml_ips_clean
    all_ips = getattr(request.session, "_tf_public_ips", [])
    return all_ips[1:] if len(all_ips) > 1 else []


@pytest.fixture(scope="session")
def secondary_instance_ids(request, raw_config):
    cli_ids = request.config.getoption("--secondary-ids", default=None)
    if cli_ids:
        return [i.strip() for i in cli_ids.split(",") if i.strip()]

    yaml_ids = raw_config.get("controller", {}).get("secondary_ids", [])
    yaml_ids_clean = [i.strip() for i in (yaml_ids or []) if i and i.strip()]
    if yaml_ids_clean:
        return yaml_ids_clean

    all_ids = getattr(request.session, "_tf_instance_ids", [])
    if len(all_ids) > 1:
        return all_ids[1:]

    provision = raw_config.get("controller", {}).get("provision", False)
    if not provision:
        try:
            from lib.oci.vm_manager import load_oci_profile
            from lib.terraform.tf_manager import TerraformManager
            oci_profile = load_oci_profile(raw_config)
            tf_manager  = TerraformManager(oci_profile)
            all_ids, _, _ = tf_manager.read_state()
            if len(all_ids) > 1:
                print(f"[secondary_instance_ids] read_state: found {len(all_ids)-1} secondary ID(s)")
                return all_ids[1:]
        except Exception as e:
            print(f"[secondary_instance_ids] read_state failed ({e}) — NSG skipped for secondary nodes")

    return []


@pytest.fixture(scope="session")
def oci_profile_fixture(request, raw_config):
    from lib.oci.vm_manager import load_oci_profile
    try:
        return load_oci_profile(raw_config)
    except Exception:
        return None


@pytest.fixture(scope="session")
def nsg_manager(request, raw_config):
    instance_id = getattr(request.session, "_tf_instance_id", None)
    if not instance_id:
        instance_id = request.config.getoption("--controller-instance-id", default=None)
    if not instance_id:
        instance_id = raw_config.get("controller", {}).get("instance_id", "") or None

    if instance_id and raw_config.get("oci", {}).get("nsg_id"):
        from lib.oci.vm_manager import OCINSGManager, load_oci_profile
        try:
            oci_profile = load_oci_profile(raw_config)
            mgr = OCINSGManager(oci_profile, instance_id)
            request.session._nsg_manager = mgr
            yield mgr
            return
        except Exception as e:
            print(f"[nsg_manager] Could not create NSG manager: {e}")

    yield getattr(request.session, "_nsg_manager", None)


@pytest.fixture(scope="session")
def _nsg_manager_orig(request):
    return getattr(request.session, "_nsg_manager", None)


@pytest.fixture(scope="session")
def controller_fqdn(request):
    return getattr(request.session, "_tf_fqdn", "")


@pytest.fixture(scope="session")
def package_profile(request, raw_config):
    # For upgrade runs — bringup uses src-package
    # For bringup-only runs — uses package-name
    package_name = (
        request.config.getoption("--src-package")
        or request.config.getoption("--package-name")
    )
    package_url = request.config.getoption("--package-url")

    # If URL passed — extract name from URL automatically
    if package_url and not package_name:
        package_name = package_url.split("/")[-1]
        print(f"[conftest] package name extracted from URL: {package_name}")

    profile = load_package_profile(
        cfg=raw_config,
        package_name=package_name,
        package_url=package_url,
    )
    print(f"\n[conftest] {profile.summary()}")
    return profile


# ── controller_bringup fixture ────────────────────────────────────────────────
@pytest.fixture(scope="session", autouse=True)
def controller_bringup(
    request,
    ssh_client,
    controller_profile,
    package_profile,
    controller_fqdn,
    secondary_ips,
    secondary_instance_ids,
    oci_profile_fixture,
    nsg_manager,
    raw_config,
):
    from lib.controller.bringup import ControllerBringup, BringupError

    if request.config.getoption("--skip-bringup"):
        print("[conftest] --skip-bringup — skipping install, running validation only")
        yield
        return

    if controller_fqdn:
        star_domain = controller_fqdn.lstrip("*.")
    else:
        base_domain  = raw_config.get("dns", {}).get("base_domain", "")
        build_no_val = request.config.getoption("--build-no") or ""
        star_domain  = f"shc-{build_no_val}.{base_domain}" if base_domain and build_no_val else ""

    bringup = ControllerBringup(
        ssh_client=ssh_client,
        controller_profile=controller_profile,
        package_profile=package_profile,
        star_domain=star_domain,
        secondary_ips=secondary_ips,
        secondary_instance_ids=secondary_instance_ids,
        oci_profile=oci_profile_fixture,
        nsg_manager=nsg_manager,
    )

    try:
        bringup.run()
    except BringupError as e:
        pytest.fail(f"Controller bringup failed at phase [{e.phase}]: {e}")

    yield


# ── controller_upgrade fixture ────────────────────────────────────────────────
@pytest.fixture(scope="session", autouse=True)
def controller_upgrade(
    request,
    ssh_client,
    controller_fqdn,
    nsg_manager,
    raw_config,
    controller_bringup,   # ensures bringup runs first
):
    from lib.upgrade.upgrade_engine import UpgradeEngine

    dst_package = request.config.getoption("--dst-package", default=None)
    src_package = request.config.getoption("--src-package", default=None)

    # Skip if no dst-package — bringup only run
    if not dst_package:
        print("[conftest] --dst-package not set — skipping upgrade")
        yield
        return

    # Skip if --skip-upgrade
    if request.config.getoption("--skip-upgrade"):
        print("[conftest] --skip-upgrade — skipping upgrade")
        yield
        return

    # Auto-extract versions from package names if not explicitly passed
    def extract_version(pkg):
        m = re.search(r'v([\d.]+-\d+)\.tar\.gz', pkg or "")
        return m.group(1) if m else ""

    src_version = request.config.getoption("--src-version") or extract_version(src_package)
    dst_version = request.config.getoption("--dst-version") or extract_version(dst_package)

    # Resolve star_domain
    if controller_fqdn:
        star_domain = controller_fqdn.lstrip("*.")
    else:
        base_domain  = raw_config.get("dns", {}).get("base_domain", "")
        build_no_val = request.config.getoption("--build-no") or ""
        star_domain  = f"shc-{build_no_val}.{base_domain}" if base_domain and build_no_val else ""

    engine = UpgradeEngine(
        ssh_client=ssh_client,
        src_version=src_version,
        dst_version=dst_version,
        src_package=src_package or "",
        dst_package=dst_package,
        dst_package_url=request.config.getoption("--dst-package-url") or "",
        install_dir=raw_config.get("package", {}).get("install_dir", "/opt/rafay"),
        star_domain=star_domain,
        nsg_manager=nsg_manager,
    )

    try:
        engine.run()
    except Exception as e:
        pytest.fail(f"Controller upgrade failed: {e}")

    yield

@pytest.fixture
def extras(extra):
    """
    pytest-html extras fixture — attach HTML/images/text to test report.
    Works with both old (extra) and new (extras) pytest-html APIs.
    """
    return extra