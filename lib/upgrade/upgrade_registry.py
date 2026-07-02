"""
lib/upgrade/upgrade_registry.py

Defines what is DIFFERENT per upgrade hop.
This is the ONLY file you edit when adding a new version.

To add 3.1-40 → 3.1-41:
  1. Add entry to UPGRADE_REGISTRY below with pre_commands and post_commands
  2. Done — engine untouched, hooks file gone

Command rules:
  - Always end with || true for non-fatal commands
  - Remove || true if you want the upgrade to STOP on failure
  - Use plain kubectl / bash commands — exactly what you'd run manually
"""

UPGRADE_REGISTRY = [
    {
        "from": "3.1-39",
        "to":   "3.1-40",

        # Commands that run BEFORE download/radm phases
        # All warn-only — end with || true
        "pre_commands": [
            # Cleanup tmp
            "sudo rm -rf /tmp/radm.log /tmp/rafay-dep* /tmp/rafay-cluster* /tmp/rafay-core* /tmp/istio-* 2>/dev/null || true",

            # Install pigz for faster extraction
            "sudo apt-get -o Acquire::ForceIPv4=true install -y pigz 2>&1 || true",

            # Remove default storageclass
            "kubectl patch storageclass openebs-hostpath --type=json -p='[{\"op\":\"remove\",\"path\":\"/metadata/annotations/storageclass.kubernetes.io~1is-default-class\"}]' 2>/dev/null || true",

            # Patch istio CRDs
            """for crd in $(kubectl get crd -o name | grep -E '\\.istio\\.io$'); do kubectl annotate "$crd" meta.helm.sh/release-name=istio-base meta.helm.sh/release-namespace=istio-system --overwrite 2>/dev/null; kubectl label "$crd" app.kubernetes.io/managed-by=Helm --overwrite 2>/dev/null; done || true""",
            "kubectl annotate crd istiooperators.install.istio.io meta.helm.sh/release-name=istio-base meta.helm.sh/release-namespace=istio-system --overwrite 2>/dev/null || true",
            "kubectl label crd istiooperators.install.istio.io app.kubernetes.io/managed-by=Helm --overwrite 2>/dev/null || true",

            # Patch nexus
            """kubectl get cm,sts,svc,destinationrule -n rafay-core -o json | jq -r '.items[] | select(.metadata.annotations["meta.helm.sh/release-name"]=="rafay-core") | select(.metadata.name|test("nexus")) | "\\(.kind)/\\(.metadata.name)"' | while read -r r; do kubectl annotate -n rafay-core "$r" meta.helm.sh/release-name=rafay-repo meta.helm.sh/release-namespace=rafay-core --overwrite 2>/dev/null; kubectl label -n rafay-core "$r" app.kubernetes.io/managed-by=Helm --overwrite 2>/dev/null; done || true""",

            # Delete nexus StatefulSet — spec changes require delete + recreate
            # Annotation alone is not enough for StatefulSet spec changes
            "kubectl delete sts nexus -n rafay-core --ignore-not-found=true 2>/dev/null || true",
            "kubectl delete sts clickhouse -n rafay-core --ignore-not-found=true 2>/dev/null || true",

            # Patch redis
            "kubectl annotate ds redis-replicas -n rafay-core meta.helm.sh/release-name=redis meta.helm.sh/release-namespace=rafay-core --overwrite 2>/dev/null && kubectl label ds redis-replicas -n rafay-core app.kubernetes.io/managed-by=Helm --overwrite 2>/dev/null || true",
            "kubectl annotate deploy redis-master -n rafay-core meta.helm.sh/release-name=redis meta.helm.sh/release-namespace=rafay-core --overwrite 2>/dev/null && kubectl label deploy redis-master -n rafay-core app.kubernetes.io/managed-by=Helm --overwrite 2>/dev/null || true",

            # Delete prometheus stack
            """kubectl delete $(kubectl api-resources --verbs=list --namespaced -o name | tr '\\n' ',' | sed 's/,$//') -l app.kubernetes.io/part-of=kube-prometheus-stack -n rafay-core 2>/dev/null || true""",

        ],

        # Commands that run AFTER radm_cluster_old, BEFORE radm_cluster_new
        # All warn-only — end with || true
        "post_commands": [
            # Enable paas-api DAY2 operations
            "kubectl set env deployment/paas-api WORKSPACE_API_ALLOW_DAY2_OPERATIONS=true -n rafay-core 2>/dev/null || true",
        ],
    },

    # ── Add new versions below ─────────────────────────────────────────────
    # {
    #     "from": "3.1-40",
    #     "to":   "3.1-41",
    #     "pre_commands": [
    #         "sudo rm -rf /tmp/radm.log /tmp/rafay-dep* 2>/dev/null || true",
    #         "kubectl patch svc admin-api -n rafay-core --type=json -p='[...]' || true",
    #     ],
    #     "post_commands": [
    #         "kubectl set env deployment/paas-api WORKSPACE_API_ALLOW_DAY2_OPERATIONS=true -n rafay-core || true",
    #     ],
    # },
]


def get_hop(from_version: str, to_version: str) -> dict:
    """
    Look up a hop in the registry.
    Raises ValueError if hop not found.
    """
    for hop in UPGRADE_REGISTRY:
        if hop["from"] == from_version and hop["to"] == to_version:
            return hop
    raise ValueError(
        f"No upgrade path registered for {from_version} → {to_version}.\n"
        f"Add an entry to lib/upgrade/upgrade_registry.py."
    )