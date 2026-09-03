"""
Composition function for AWS EKS Control Plane with UXP backup support.

Each section below is implemented in a sibling module (NN denotes the
ordered section it corresponds to):

  prelude.py            (00) shared extractors and helpers
  network.py            (01) VPC + subnets
  eks.py                (02) EKS cluster
  uxp.py                (03) UXP v2 Helm Release
  lbcontroller.py       (03b) AWS Load Balancer Controller (Pod Identity, k8gb/argocd)
  k8gb.py               (04b) k8gb operator + CoreDNS producer
  argo.py               (05b) ArgoCD add-on (UI Gateway/HTTPRoute + app-of-apps)
  usages.py             (04) deletion-order Usage guards
  backup.py             (05) S3 bucket, BackupConfig, RBAC, Schedule
  irsa.py               (06) OIDC Provider, Role, Policy, SA annotation, controller restart, restore
  licensing.py          (07) License Secret + License CR
  vpa.py                (08) VPA + metrics-server Helm Releases
  certmanager.py        (09a) always-on cert-manager Helm Release
  knative.py            (09) knative-operator + serving CR
  runtime_config.py     (10) UpboundRuntimeConfig (ProviderVPA + Knative caps)
  adopt.py              (11) external-name discovery for the adopt Composition
  status.py             (99) XR status writeback + ClaimConditions

Cluster metadata (OIDC issuer/ARN, running node-group instance type) is read
from the composed EKS XR's status.eks (configuration-aws-eks v2.0.2+); the only
observe-only resource composed here is the k8gb CoreDNS Service Object (to read
its LoadBalancer endpoint for the status contract).
"""

from datetime import datetime, timezone

import grpc
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1

from .adopt import apply_external_names, build_external_names
from .argo import add_argocd_resources
from .backup import add_backup_resources
from .certmanager import add_certmanager_resources
from .eks import add_eks_resource
from .gateway import add_gateway_resources
from .irsa import add_irsa_resources
from .k8gb import add_k8gb_resources
from .knative import add_knative_resources
from .lbcontroller import add_lbcontroller_resources
from .licensing import add_license_resources
from .network import add_network_resource
from .prelude import (
    build_manager_args,
    check_license_conflict,
    derive_k8gb_ext_geo_tags,
    derive_k8gb_geo_tag,
    extract_bucket_name,
    extract_cluster_identity,
    extract_k8gb_eips,
    extract_oidc_info,
    extract_vpc_id,
    get_nodegroup_actual_type,
    is_knative_serving_ready,
    is_license_applied,
    is_release_deployed,
    is_resource_ready,
    public_subnet_count,
)
from .runtime_config import add_runtime_config
from .status import update_status
from .usages import add_usage_resources
from .uxp import add_uxp_release
from .vpa import add_vpa_resources


# managementMode -> Crossplane managementPolicies. Provision and ObserveOnly
# never include Delete, so the provisioned control plane is orphaned (never torn
# down) when the XR is removed. Full (default) is the standard "*" lifecycle.
# Deprovision is the pipeline's decommission signal: adopt (Observe/Create) and
# Delete, but no Update/LateInitialize - a drifted or broken cluster must not have
# changes pushed to it on the way out, only be torn down.
_MODE_POLICIES = {
    "Provision": ["Observe", "Create", "Update", "LateInitialize"],
    "ObserveOnly": ["Observe"],
    "Full": ["*"],
    "Deprovision": ["Create", "Delete", "Observe"],
}


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

    # The XR is namespaced (apis/ctp/definition.yaml scope: Namespaced); every
    # composed resource and the sub-XRs' connection secrets co-locate in the XR's
    # own namespace. Falls back to "default" when unset.
    config["namespace"] = xr.get("metadata", {}).get("namespace") or "default"

    # The adoption key. stamp() writes it to spec.forProvider.tags on every AWS
    # resource this configuration owns; the adopt path queries on it.
    config["ctp_id"] = params.get("id", "")

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
    # managementPolicies is published API and stays the escape hatch: when set
    # explicitly it wins, otherwise managementMode supplies the policy array.
    management_mode = params.get("managementMode", "Full")
    mgmt_policies = params.get("managementPolicies") or _MODE_POLICIES.get(
        management_mode, _MODE_POLICIES["Full"])
    naming = params.get("naming", "Generated")
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

    # k8gb geo tags: this cluster's unique tag, plus same-cloud k8gb peers on
    # the same dnsZone (cross-cloud peers are injected later by FleetGslb).
    k8gb_geo_tag = ""
    k8gb_ext_geo_tags = ""
    k8gb_eip_count = 0
    k8gb_eip_alloc_ids = []
    k8gb_eip_ips = []
    if k8gb_enabled:
        k8gb_geo_tag = derive_k8gb_geo_tag(k8gb, region, id_val)
        k8gb_ext_geo_tags = derive_k8gb_ext_geo_tags(
            id_val, k8gb.get("dnsZone", ""), k8gb_geo_tag, all_ctps)
        k8gb_eip_count = public_subnet_count(network_param)

    observed_resources = {
        name: resource.struct_to_dict(res.resource)
        for name, res in req.observed.resources.items()
    }

    if k8gb_enabled:
        eips = extract_k8gb_eips(observed_resources, k8gb_eip_count)
        k8gb_eip_alloc_ids = [e["allocationId"] for e in eips]
        k8gb_eip_ips = [e["publicIp"] for e in eips]

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
    gateway_ready = is_release_deployed(observed_resources, "envoy-gateway-release")

    # Cluster name/account for EKS Pod Identity (k8gb LB controller), read from
    # the EKS XR's status.eks — independent of backup.
    cluster_name, cluster_account_id, _cluster_region = extract_cluster_identity(observed_resources)
    vpc_id = extract_vpc_id(observed_resources)
    lb_identity_ready = is_resource_ready(observed_resources, "lb-controller-pia")
    lb_release_deployed = is_release_deployed(observed_resources, "lb-controller-release")
    k8gb_deployed = is_release_deployed(observed_resources, "k8gb-release")
    argocd_deployed = is_release_deployed(observed_resources, "argocd-release")
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
                     access_config, mgmt_policies, iam_param, config,
                     naming=naming)
    add_uxp_release(rsp, id_val, uxp_version, uxp_deployed, mgr_args, config)
    add_usage_resources(rsp, id_val, config, k8gb_enabled=k8gb_enabled,
                        argocd_enabled=argocd_enabled,
                        k8gb_eip_count=k8gb_eip_count)

    # cert-manager is always installed (free component, no license gate) so the
    # k8gb/argocd add-ons can rely on it for Gateway TLS independently of knative.
    add_certmanager_resources(rsp, id_val, certmanager_ready, config)

    # Envoy Gateway is installed only when an add-on needs an HTTP data plane, so
    # plain control planes do not run an idle gateway. Unlike nginx it provisions
    # no cloud LB until a Gateway resource exists.
    if k8gb_enabled or argocd_enabled:
        add_gateway_resources(rsp, id_val, gateway_ready, config)

    # AWS Load Balancer Controller (Pod Identity) - shared prerequisite for any
    # NLB-backed data plane: the k8gb CoreDNS UDP+TCP:53 NLB and/or the Envoy
    # Gateway data-plane NLB. Installed whenever a Gateway/CoreDNS LB may exist.
    if k8gb_enabled or argocd_enabled:
        add_lbcontroller_resources(rsp, id_val, provider_config, cluster_name,
                                   cluster_account_id, region, vpc_id,
                                   lb_identity_ready, lb_release_deployed, config)

    if k8gb_enabled:
        add_k8gb_resources(rsp, id_val, k8gb, k8gb_geo_tag, k8gb_ext_geo_tags,
                           k8gb_deployed, region, provider_config,
                           k8gb_eip_count, k8gb_eip_alloc_ids, config)

    if argocd_enabled:
        add_argocd_resources(rsp, id_val, argocd, argocd_deployed,
                             certmanager_ready, gateway_ready, config)

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

    # --- Comprehensive orphan policy ---
    # Every composed managed resource (helm Release, provider-kubernetes Object,
    # and AWS MRs all carry spec.forProvider) inherits mgmt_policies, so
    # Provision/ObserveOnly never delete the provisioned control plane on
    # teardown. Resources with an explicit policy (backup bucket, k8gb CoreDNS
    # observe, knative serving) and composed XRs / Usage guards (no forProvider)
    # are left untouched.
    for _name in list(rsp.desired.resources.keys()):
        _res = resource.struct_to_dict(rsp.desired.resources[_name].resource)
        _spec = _res.get("spec", {})
        if "forProvider" not in _spec or "managementPolicies" in _spec:
            continue
        _res["spec"]["managementPolicies"] = mgmt_policies
        resource.update(rsp.desired.resources[_name], _res)

    # --- Adopt: inject external-names discovered by function-aws-query ---
    # Only the adopt Composition fills context.adopt; on the default Composition
    # this is an empty dict and the whole block is a no-op.
    adopt_ctx = context_dict.get("adopt", {})
    external_names = build_external_names(
        adopt_ctx, id_val, cluster_name, cluster_account_id, oidc_host)
    apply_external_names(rsp, external_names)

    update_status(rsp, id_val, params, uxp_version, uxp_deployed, backup,
                  role_arn, bucket_name, observed_resources, nodes,
                  ng_actual_type, ng_type_mismatch, vpa, knative,
                  k8gb, k8gb_geo_tag, k8gb_eip_ips, k8gb_eip_count,
                  license_conflict, config)


class FunctionRunner(grpcv1.FunctionRunnerService):
    """Handles gRPC RunFunctionRequests for the AWS ControlPlane composition."""

    def __init__(self):
        """Create a new FunctionRunner."""
        self.log = logging.get_logger()

    async def RunFunction(
        self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext
    ) -> fnv1.RunFunctionResponse:
        """Build a response, run the composition, and return it."""
        log = self.log.bind(tag=req.meta.tag)
        log.info("Running function")
        rsp = response.to(req)
        compose(req, rsp)
        return rsp
