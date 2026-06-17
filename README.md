# rafay-pytest-framework

Pytest automation framework for testing Rafay airgapped Kubernetes controllers.

## Project Structure

```
rafay-pytest-framework/
├── conftest.py                          # Root fixtures & CLI options
├── pytest.ini                           # Markers, HTML report config
├── requirements.txt
│
├── config/
│   ├── dev.yaml                         # Dev environment (edit this)
│   └── staging.yaml                     # Staging environment
│
├── tests/
│   ├── smoke/
│   │   └── test_controller_health.py    # Fast sanity checks
│   ├── infra/
│   │   └── test_oci_bringup.py          # OCI VM provisioning tests
│   ├── controller/
│   │   └── test_controller_install.py   # radm install + preflight tests
│   ├── kubectl/
│   │   └── test_cluster_state.py        # Cluster state via kubectl
│   └── regression/
│       └── test_end_to_end.py           # Cross-layer e2e tests
│
├── lib/
│   ├── ssh/ssh_client.py                # Paramiko SSH wrapper
│   ├── kubectl/kubectl_client.py        # kubectl subprocess wrapper
│   └── oci/vm_manager.py               # OCI compute VM lifecycle
│
├── fixtures/
│   ├── controller_fixtures.py           # Module/function scoped SSH fixtures
│   └── oci_fixtures.py                  # OCI VM session fixtures
│
├── utils/
│   ├── config_loader.py                 # YAML loader + ControllerProfile
│   ├── logger.py                        # Centralised logging
│   └── helpers.py                       # wait_until, assert helpers
│
└── reports/                             # HTML reports (auto-generated)
```

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure your environment
vi config/dev.yaml   # fill in controller IP, OCI OCIDs, etc.

# 3. Run smoke tests only (fastest — no VM provisioning)
pytest -m smoke --env=dev --controller-ip=129.146.108.42 --ssh-key=~/.ssh/key.pem

# 4. Full flow: provision OCI VM → preflight → install → health checks
pytest tests/infra/ tests/controller/ -v --build-no=42

# 5. Full regression
pytest -m regression --env=dev
```

## CLI Options

| Option | Default | Description |
|--------|---------|-------------|
| `--env` | `dev` | Config file to use (`dev` → `config/dev.yaml`) |
| `--controller-ip` | from YAML | Override controller IP |
| `--ssh-key` | from YAML | Path to SSH private key |
| `--ssh-user` | from YAML | SSH username |
| `--controller-size` | from YAML | `S` / `M` / `L` |
| `--ha` | from YAML | `true` / `false` |
| `--os-type` | from YAML | `ubuntu24` / `rhel8` / `rhel9` |
| `--build-no` | `None` | Build number → used in OCI VM display name |
| `--keep-vm` | `False` | Skip OCI VM termination after session |

## Environment Variable Overrides

Any YAML value can be overridden without editing files:

```bash
CONTROLLER_IP=10.0.0.5 \
CONTROLLER_SIZE=M \
CONTROLLER_HA=true \
OCI_COMPARTMENT_ID=ocid1.compartment... \
pytest tests/infra/ -v
```

## OCI VM Lifecycle

The `oci_instance` session fixture:
1. Reads all OCI params from `config/dev.yaml` under the `oci:` key
2. Launches a VM, waits for RUNNING state, retrieves public IP
3. Yields `{"id": <ocid>, "public_ip": <ip>, "profile": <OCIProfile>}` to tests
4. Terminates the VM after the session

**Skip VM creation** (test against an existing controller):
```bash
SKIP_OCI_CREATE=1 pytest tests/controller/ -v
```

**Keep VM alive** after tests (debug mode):
```bash
pytest tests/infra/ tests/controller/ -v --keep-vm
```

## Controller Size / HA Rules

| Size | CPUs | Memory | HA Supported | Non-HA Supported |
|------|------|--------|--------------|------------------|
| S    | 64   | 128 GB | ✗            | ✓                |
| M    | 96   | 192 GB | ✓            | ✓                |
| L    | 128  | 192 GB | ✓            | ✗                |

## Markers

| Marker | Description |
|--------|-------------|
| `smoke` | Fast sanity checks — run first |
| `infra` | OCI VM provisioning tests |
| `controller` | SSH-based controller tests |
| `kubectl` | kubectl cluster state tests |
| `regression` | Full regression suite |
| `slow` | Tests that take > 60s |

```bash
# Run by marker
pytest -m smoke
pytest -m "infra or controller"
pytest -m "regression and not slow"
```

## Reports

HTML report is auto-generated after every run at `reports/report.html`.
Every test embeds its raw command output directly in the report.
