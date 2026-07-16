"""
Composition function for AWS EKS Control Plane with UXP backup support.

Each section below is implemented in a sibling module (NN denotes the
ordered section it corresponds to):

  prelude.py            (00) shared extractors and helpers
  network.py            (01) VPC + subnets
  eks.py                (02) EKS cluster
  uxp.py                (03) UXP v2 Helm Release
  usages.py             (04) deletion-order Usage guards
  backup.py             (05) S3 bucket, BackupConfig, RBAC, Schedule
  irsa.py               (06) OIDC Provider, Role, Policy, SA annotation, controller restart, restore
  licensing.py          (07) License Secret + License CR
  vpa.py                (08) VPA + metrics-server Helm Releases
  certmanager.py        (09a) always-on cert-manager Helm Release
  knative.py            (09) knative-operator + serving CR
  runtime_config.py     (10) UpboundRuntimeConfig (ProviderVPA + Knative caps)
  status.py             (99) XR status writeback + ClaimConditions

Cluster metadata (OIDC issuer/ARN, running node-group instance type) is read
from the composed EKS XR's status.eks (configuration-aws-eks v2.0.2+), so no
observe-only managed resources are composed here.
"""

from datetime import datetime, timezone

from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .backup import add_backup_resources
from .certmanager import add_certmanager_resources
from .eks import add_eks_resource
from .ingress import add_ingress_resources
from .irsa import add_irsa_resources
from .knative import add_knative_resources
from .licensing import add_license_resources
from .network import add_network_resource
from .prelude import (
    build_manager_args,
    check_license_conflict,
    extract_bucket_name,
    extract_oidc_info,
    get_nodegroup_actual_type,
    is_knative_serving_ready,
    is_license_applied,
    is_release_deployed,
)
from .runtime_config import add_runtime_config
from .status import update_status
from .usages import add_usage_resources
from .uxp import add_uxp_release
from .vpa import add_vpa_resources


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Main composition function entry point."""
    # Capture the reconciliation timestamp once and thread it through every
    # section so every emitted resource carries the same value. Pattern
    # borrowed from upbound/labs python-devex-ai compose-network.
    config = {
        "last_reconcile_date": datetime.now(timezone.utc).strftime(
            "%A %Y-%m-%d %H:%M:%S UTC"
        ),
    }

    # The protobuf Struct types in req do not implement Python dict
    # semantics (no .get / .setdefault). Convert at the boundary once so the
    # rest of the pipeline can use plain dict access.
    xr = resource.struct_to_dict(req.observed.composite.resource)
    params = xr.get("spec", {}).get("parameters", {})

    id_val = params.get("id", "")
    region = params.get("region", "")
    provider_config = params.get("providerConfigName", "default")
    version = params.get("version", "1.34")
    nodes = params.get("nodes", {})
    network_param = params.get("network", {})
    access_config = params.get("accessConfig", {
        "bootstrapClusterCreatorAdminPermissions": True,
        "authenticationMode": "API_AND_CONFIG_MAP"
    })
    iam_param = params.get("iam")
    backup = params.get("backup", {"enabled": "no"})
    install_from = backup.get("installFrom")
    license_param = params.get("license")
    mgmt_policies = params.get("managementPolicies", ["*"])
    uxp_version = params.get("uxp", {}).get("version", "2.2.1-up.1")
    vpa = params.get("providerVerticalPodAutoscaling")
    knative = params.get("knative")
    k8gb = params.get("k8gb")
    argocd = params.get("argocd")

    k8gb_enabled = bool(k8gb) and k8gb.get("enabled") == "yes"
    argocd_enabled = bool(argocd) and argocd.get("enabled") == "yes"

    # function-extra-resources delivers `allControlPlanes` via the
    # apiextensions.crossplane.io/extra-resources context key.
    context_dict = resource.struct_to_dict(req.context)
    extra_ctx = context_dict.get("apiextensions.crossplane.io/extra-resources", {})
    all_ctps = extra_ctx.get("allControlPlanes", [])

    license_conflict = check_license_conflict(id_val, license_param, all_ctps)

    observed_resources = {
        name: resource.struct_to_dict(res.resource)
        for name, res in req.observed.resources.items()
    }

    oidc_url, _cluster_arn, oidc_host, account_id = extract_oidc_info(
        backup, observed_resources
    )
    oidc_provider_arn = ""
    role_arn = ""
    if oidc_host and account_id:
        oidc_provider_arn = f"arn:aws:iam::{account_id}:oidc-provider/{oidc_host}"
        role_arn = f"arn:aws:iam::{account_id}:role/{id_val}-backup-irsa"

    uxp_deployed = is_release_deployed(observed_resources, "uxp-release")
    vpa_ready = is_release_deployed(observed_resources, "vpa-release")
    certmanager_ready = is_release_deployed(observed_resources, "certmanager-release")
    ingress_ready = is_release_deployed(observed_resources, "ingress-nginx-release")
    knative_op_ready = is_release_deployed(observed_resources, "knative-operator-release")
    knative_deps_ready = certmanager_ready and knative_op_ready
    knative_serving_ready = is_knative_serving_ready(observed_resources)
    knative_fully_ready = knative_deps_ready and knative_serving_ready

    license_applied = is_license_applied(observed_resources)
    features_licensed = not license_param or license_applied

    mgr_args = build_manager_args(vpa, knative, vpa_ready, knative_fully_ready, features_licensed)

    bucket_name = extract_bucket_name(backup.get("location", ""))
    # The backup bucket may live in a different region than the cluster (for
    # cross-region DR). Everything that touches the bucket uses bucket_region.
    bucket_region = backup.get("bucketRegion") or region

    ng_actual_type = get_nodegroup_actual_type(observed_resources)
    ng_type_mismatch = bool(ng_actual_type) and ng_actual_type != nodes.get("instanceType", "")

    # --- Compose resources ---
    add_network_resource(rsp, id_val, region, provider_config, mgmt_policies,
                         network_param, config)
    add_eks_resource(rsp, id_val, region, provider_config, version, nodes,
                     access_config, mgmt_policies, iam_param, config)
    add_uxp_release(rsp, id_val, uxp_version, uxp_deployed, mgr_args, config)
    add_usage_resources(rsp, id_val, config)

    # cert-manager is always installed (free component, no license gate) so the
    # k8gb/argocd add-ons can rely on it for Ingress TLS independently of knative.
    add_certmanager_resources(rsp, id_val, certmanager_ready, config)

    # nginx-ingress is installed only when an add-on needs an Ingress, so plain
    # control planes do not pay for an idle cloud load balancer.
    if k8gb_enabled or argocd_enabled:
        add_ingress_resources(rsp, id_val, ingress_ready, config)

    if backup.get("enabled") == "yes":
        add_backup_resources(rsp, id_val, bucket_region, provider_config,
                             bucket_name, backup, uxp_deployed, config)

    if backup.get("enabled") == "yes" and oidc_url and uxp_deployed:
        add_irsa_resources(rsp, id_val, bucket_region, provider_config,
                           oidc_host, oidc_provider_arn, role_arn, bucket_name,
                           observed_resources, install_from, account_id, config)

    if license_param and not license_conflict:
        add_license_resources(rsp, id_val, license_param, config)

    if vpa and vpa.get("enabled") == "yes" and features_licensed:
        add_vpa_resources(rsp, id_val, vpa, vpa_ready, config)

    if knative and knative.get("enabled") == "yes" and features_licensed:
        add_knative_resources(rsp, id_val, knative_op_ready,
                              knative_deps_ready, knative_serving_ready,
                              observed_resources, config)

    if (vpa and vpa.get("enabled") == "yes" and vpa_ready) or \
       (knative and knative.get("enabled") == "yes" and knative_fully_ready):
        add_runtime_config(rsp, id_val, vpa, knative, vpa_ready,
                           knative_fully_ready, config)

    update_status(rsp, id_val, params, uxp_version, uxp_deployed, backup,
                  role_arn, bucket_name, observed_resources, nodes,
                  ng_actual_type, ng_type_mismatch, vpa, knative,
                  license_conflict, config)
