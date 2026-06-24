"""Root conftest — CLI options, session fixtures, controller profile injection."""

import os
import pytest
from utils.config_loader import load_controller_profile, load_config, load_package_profile


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
    parser.addoption("--secondary-ips",   default=None,   help="Comma-separated IPs of secondary nodes e.g. 1.2.3.4,5.6.7.8")
    parser.addoption("--secondary-ids",   default=None,   help="Comma-separated OCI instance IDs of secondary nodes")
    parser.addoption("--controller-instance-id", default=None, help="OCI instance ID of primary node (for NSG in static IP mode)")
    # ── NEW ───────────────────────────────────────────────────────────────────
    parser.addoption("--skip-bringup",    action="store_true", default=False,
                     help="Skip controller bringup — run validation tests only against existing install")


@pytest.fixture(scope="session")
def raw_config(request):
    cfg = load_config(request.config.getoption("--env"))
    request.session._raw_config = cfg
    return cfg


@pytest.fixture(scope="session")
def controller_profile(request, raw_config):
    ha_raw = request.config.getoption("--ha")
    ha_override = None
    if ha_raw is not None:
        ha_override = ha_raw.lower() in ("true", "1", "yes")
    profile = load_controller_profile(
        env=request.config.getoption("--env"),
        controller_ip=request.config.getoption("--controller-ip"),
        ssh_key=request.config.getoption("--ssh-key"),
        ssh_user=request.config.getoption("--ssh-user"),
        controller_size=request.config.getoption("--controller-size"),
        ha=ha_override,
        os_type=request.config.getoption("--os-type"),
    )
    print(f"\n[conftest] {profile.summary()}")
    return profile


@pytest.fixture(scope="session")
def ssh_client(request, raw_config, controller_profile):
    """
    Full VM lifecycle:

    provision: true (dev.yaml):
      1. Terraform apply  → OCI VM + 1TB data volume
      2. boto3 Route53    → *.shc-{N}.dev.rafay-edge.net → VM IP
      3. NSG attach       → internet access for apt + S3 download
      4. SSH connect      → opens session

    On teardown:
      5. SSH disconnect
      6. NSG safety detach
      7. boto3 Route53    → delete DNS record
      8. Terraform destroy → VM + data volume deleted
    """
    from lib.ssh.ssh_client import SSHClient

    cli_ip    = request.config.getoption("--controller-ip")
    provision = raw_config.get("controller", {}).get("provision", False)

    # ── Case 1: explicit CLI override ─────────────────────────────────────────
    if cli_ip:
        print(f"\n[ssh_client] Using --controller-ip: {cli_ip}")
        ip = cli_ip

    # ── Case 2: provision via Terraform + boto3 DNS ───────────────────────────
    elif provision:
        from lib.oci.vm_manager import load_oci_profile, OCINSGManager
        from lib.terraform.tf_manager import TerraformManager

        oci_profile = load_oci_profile(raw_config)
        dns_cfg     = raw_config.get("dns", {})
        tf_manager  = TerraformManager(oci_profile)
        build_no    = request.config.getoption("--build-no") or os.environ.get("BUILD_NUMBER")

        # Step 1: Terraform — create OCI VM(s) + data volume(s)
        print("\n[ssh_client] Provisioning VM(s) via Terraform ...")
        instance_ids, public_ips, _ = tf_manager.apply(
            build_no=build_no,
            dns_cfg=dns_cfg,
            controller_profile=controller_profile,
        )
        instance_id = instance_ids[0]
        ip          = public_ips[0]
        print(f"[ssh_client] VM ready — id={instance_id}  ip={ip}")

        # Step 2: boto3 Route53 — create DNS record
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

        # Step 3: NSG attach — internet access for apt + S3 download
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
            print(f"[ssh_client] NSG attached — all VMs have internet access")

        # Store on session for fixtures and teardown
        request.session._tf_instance_id  = instance_id
        request.session._tf_instance_ids = instance_ids
        request.session._tf_public_ip    = ip
        request.session._tf_public_ips   = public_ips
        request.session._tf_fqdn         = fqdn
        request.session._tf_manager      = tf_manager
        request.session._dns_manager     = dns_mgr
        request.session._nsg_manager     = nsg_mgr
        request.session._keep_vm         = request.config.getoption("--keep-vm")

    # ── Case 3: static IP ─────────────────────────────────────────────────────
    else:
        ip = controller_profile.ip
        if not ip:
            raise ValueError(
                "No IP available.\n"
                "Set controller.provision: true, controller.ip, or --controller-ip."
            )
        print(f"\n[ssh_client] Using static IP: {ip}")

    # Step 4: Open SSH
    client = SSHClient(host=ip, user=controller_profile.user, key_path=controller_profile.ssh_key)
    client.connect()
    yield client

    # ── Teardown ──────────────────────────────────────────────────────────────
    client.disconnect()

    if provision and not cli_ip:
        nsg = getattr(request.session, "_nsg_manager", None)
        if nsg:
            try:
                nsg.detach()
            except Exception as e:
                print(f"[ssh_client] NSG detach warning: {e}")

        dns = getattr(request.session, "_dns_manager", None)
        ip  = getattr(request.session, "_tf_public_ip", "")
        if dns and ip:
            try:
                dns.delete_record(ip)
            except Exception as e:
                print(f"[ssh_client] DNS delete warning: {e}")

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
    # Priority 1 — CLI flag
    cli_ids = request.config.getoption("--secondary-ids", default=None)
    if cli_ids:
        return [i.strip() for i in cli_ids.split(",") if i.strip()]

    # Priority 2 — dev.yaml secondary_ids
    yaml_ids = raw_config.get("controller", {}).get("secondary_ids", [])
    yaml_ids_clean = [i.strip() for i in (yaml_ids or []) if i and i.strip()]
    if yaml_ids_clean:
        return yaml_ids_clean

    # Priority 3 — Terraform session output (provision: true)
    all_ids = getattr(request.session, "_tf_instance_ids", [])
    if len(all_ids) > 1:
        return all_ids[1:]

    # Priority 4 — read_state() from existing tfstate (provision: false, VMs from TF)
    # Handles case where VMs were created by Terraform but provision: false is set
    # for iterative re-runs without re-provisioning.
    # Falls back gracefully — returns [] if no tfstate found (no crash, NSG skipped)
    provision = raw_config.get("controller", {}).get("provision", False)
    if not provision:
        try:
            from lib.oci.vm_manager import load_oci_profile
            from lib.terraform.tf_manager import TerraformManager
            oci_profile = load_oci_profile(raw_config)
            tf_manager  = TerraformManager(oci_profile)
            all_ids, _, _ = tf_manager.read_state()
            if len(all_ids) > 1:
                print(f"[secondary_instance_ids] read_state: found {len(all_ids)-1} secondary ID(s) from tfstate")
                return all_ids[1:]
        except Exception as e:
            print(f"[secondary_instance_ids] read_state failed ({e}) — NSG will be skipped for secondary nodes")

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
    profile = load_package_profile(
        cfg=raw_config,
        package_name=request.config.getoption("--package-name"),
    )
    print(f"\n[conftest] {profile.summary()}")
    return profile


# ── NEW: controller_bringup fixture ──────────────────────────────────────────
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
    """
    Runs full controller bringup ONCE per session before any test runs.
    VM is already provisioned by ssh_client fixture above — this just
    does the install (download, extract, radm phases).

    Skip with --skip-bringup to re-run validation only against existing install:
        pytest tests/ --skip-bringup --provision=false --controller-ip=<ip>

    Normal Jenkins run:
        pytest tests/ --controller-size=M --package-name=rafay-airgapped-controller-v3.1-39.tar.gz
    """
    from lib.controller.bringup import ControllerBringup, BringupError

    # --skip-bringup: validation only, no install steps
    if request.config.getoption("--skip-bringup"):
        print("[conftest] --skip-bringup — skipping install, running validation only")
        yield
        return

    # Resolve star_domain from FQDN or build-no + base_domain
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

    yield   # validation tests run after this point