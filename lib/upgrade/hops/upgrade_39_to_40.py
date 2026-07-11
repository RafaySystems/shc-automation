"""
lib/upgrade/hops/upgrade_39_to_40.py

Upgrade hop: 3.1-39 -> 3.1-40

This is the ONLY file to touch for this specific hop's behavior. The
engine (lib/upgrade/upgrade_engine.py) discovers this file automatically
by filename -- see lib/upgrade/hops/__init__.py for the naming convention
and for how it's loaded and validated.

Command rules (same as before):
  - Always end with `|| true` for commands that are allowed to fail
    without stopping the upgrade.
  - Remove `|| true` if you want the upgrade to STOP on that command's
    failure.
  - Use plain kubectl / bash commands -- exactly what you'd run manually.

Available hook keys, in the order the engine runs them:

    pre_commands              -- before download/radm phases run at all
    [new config.yaml created from old config.yaml + archive-directory patch]
    config_patches            -- version-specific field edits against the
                                 NEW config.yaml, right after it's created
    [radm dependency runs -- NEW package only]
    after_radm_dependency     -- right after radm dependency completes
    [radm application runs -- NEW package only]
    after_radm_application    -- right after radm application completes
    post_commands             -- right before radm cluster runs
    [radm cluster runs -- NEW package only]
    after_radm_cluster        -- right after radm cluster completes,
                                 before final pod-health polling

NOTE: every radm command (dependency, application, cluster) in this engine
runs exactly once, against the NEW package only. There is no separate
"old package" radm cluster pass.

Any hook key can be omitted entirely -- get_hop() in
lib/upgrade/hops/__init__.py fills in an empty list for any key not
defined here, so you only need to write the hooks this particular hop
actually needs.
"""

HOP = {
    "from": "3.1-39",
    "to":   "3.1-40",

    # ── pre_commands ────────────────────────────────────────────────────
    # Runs BEFORE download/radm phases. All warn-only -- end with || true.
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

    # ── config_patches ──────────────────────────────────────────────────
    # Runs against the NEW config.yaml right after create_upgrade_config
    # copies the old config forward as a base -- before any radm command
    # reads it. Nothing needed for this hop -- storageClass and every
    # other field carry forward unchanged from 3.1-39's config.yaml.
    "config_patches": [],

    # ── after_radm_dependency ───────────────────────────────────────────
    # Runs immediately after the new version's `radm dependency` completes,
    # before `radm application` starts. Nothing needed for this hop yet --
    # add commands here if a future dependency-stage issue needs patching
    # before application runs.
    "after_radm_dependency": [],

    # ── after_radm_application ──────────────────────────────────────────
    # Runs immediately after the new version's `radm application` completes,
    # before post_commands / radm cluster.
    "after_radm_application": [],

    # ── post_commands ───────────────────────────────────────────────────
    # Runs right before the (single, new-package-only) radm cluster call.
    # All warn-only -- end with || true.
    "post_commands": [
        # Enable paas-api DAY2 operations
        "kubectl set env deployment/paas-api WORKSPACE_API_ALLOW_DAY2_OPERATIONS=true -n rafay-core 2>/dev/null || true",
    ],

    # ── after_radm_cluster ──────────────────────────────────────────────
    # Runs immediately after the new version's `radm cluster` completes,
    # before final pod-health polling. Nothing needed for this hop yet.
    "after_radm_cluster": [],
}