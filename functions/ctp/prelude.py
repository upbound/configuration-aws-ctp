"""
00-prelude — shared extractors and helpers.

The read-only logic that inspects parameters and observed state to derive
values consumed by every other section.
"""

import re
from typing import Dict, List, Optional


def stamp(resource_dict: dict, config: Dict, aws_tags: bool = False) -> None:
    """Stamp a resource with the current reconciliation timestamp.

    Mirrors the labs/python-devex-ai pattern: every resource carries
    `last-reconcile-date` as a metadata annotation so an operator can see when
    this composition function last touched it. AWS managed resources that
    accept native tags (S3 Bucket, IAM Role/Policy/OIDC Provider, EKS
    Cluster/NodeGroup) also get the timestamp in `spec.forProvider.tags` so
    it propagates to the AWS-side object.
    """
    meta = resource_dict.setdefault("metadata", {})
    ann = meta.setdefault("annotations", {})
    ann["last-reconcile-date"] = config["last_reconcile_date"]

    if aws_tags:
        fp = resource_dict.setdefault("spec", {}).setdefault("forProvider", {})
        tags = fp.setdefault("tags", {})
        tags["last-reconcile-date"] = config["last_reconcile_date"]


def check_license_conflict(id_val: str, license_param: Optional[Dict],
                           all_ctps: List[Dict]) -> str:
    """Return the name of another ControlPlane that already claims the same
    license secret (namespace/name pair), or "" if there is no conflict."""
    if not license_param or not all_ctps:
        return ""

    my_ns = license_param.get("secretRef", {}).get("namespace", "default")
    my_name = license_param.get("secretRef", {}).get("name", "")
    my_key = f"{my_ns}/{my_name}"

    for ctp in all_ctps:
        c_name = ctp.get("metadata", {}).get("name", "")
        if c_name and c_name != id_val:
            c_license = ctp.get("spec", {}).get("parameters", {}).get("license", {})
            if c_license and c_license.get("secretRef"):
                c_ns = c_license["secretRef"].get("namespace", "default")
                c_name2 = c_license["secretRef"].get("name", "")
                c_key = f"{c_ns}/{c_name2}"
                if c_name2 and c_key == my_key:
                    return c_name
    return ""


def extract_oidc_info(backup: Dict, observed: Dict) -> tuple:
    """Extract (oidc_url, cluster_arn, oidc_host, account_id) from the composed
    EKS XR's status.eks. Returns empty strings until the EKS XR surfaces those
    typed fields (configuration-aws-eks v2.0.2+)."""
    if backup.get("enabled") != "yes":
        return "", "", "", ""

    obs = observed.get("eks-cluster")
    if not obs:
        return "", "", "", ""

    res = obs.resource if hasattr(obs, "resource") else obs
    eks_status = res.get("status", {}).get("eks", {})
    oidc_url = eks_status.get("oidcIssuerUrl", "")
    cluster_arn = eks_status.get("clusterArn", "")

    oidc_host = oidc_url.replace("https://", "") if oidc_url else ""

    account_id = ""
    if cluster_arn:
        parts = cluster_arn.split(":")
        if len(parts) >= 5:
            account_id = parts[4]

    return oidc_url, cluster_arn, oidc_host, account_id


def is_release_deployed(observed: Dict, name: str) -> bool:
    """True when the observed Helm Release has atProvider.state == 'deployed'."""
    obs = observed.get(name)
    if not obs:
        return False

    res = obs.resource if hasattr(obs, "resource") else obs
    state = res.get("status", {}).get("atProvider", {}).get("state", "")
    return state == "deployed"


def is_knative_serving_ready(observed: Dict) -> bool:
    """True when the KnativeServing CR reports Ready=True in its embedded
    manifest status (provider-kubernetes Object)."""
    obs = observed.get("knative-serving-cr")
    if not obs:
        return False

    res = obs.resource if hasattr(obs, "resource") else obs
    manifest_status = res.get("status", {}).get("atProvider", {}).get("manifest", {}).get("status", {})
    for cond in manifest_status.get("conditions", []):
        if cond.get("type") == "Ready" and cond.get("status") == "True":
            return True
    return False


def is_license_applied(observed: Dict) -> bool:
    """True when the License Object reports Ready=True (license accepted)."""
    obs = observed.get("uxp-license")
    if not obs:
        return False

    res = obs.resource if hasattr(obs, "resource") else obs
    for cond in res.get("status", {}).get("conditions", []):
        if cond.get("type") == "Ready" and cond.get("status") == "True":
            return True
    return False


def build_manager_args(vpa: Optional[Dict], knative: Optional[Dict],
                       vpa_ready: bool, knative_ready: bool,
                       features_licensed: bool) -> List[str]:
    """Assemble the upbound.manager.args list for the UXP Helm Release based on
    which optional features are enabled, deployed, and licensed."""
    args: List[str] = []

    if vpa and vpa.get("enabled") == "yes" and vpa_ready and features_licensed:
        args.append("--enable-provider-vpa")

    if knative and knative.get("enabled") == "yes" and knative_ready and features_licensed:
        args.append("--enable-knative-runtime")

    return args


def extract_bucket_name(location: str) -> str:
    """Extract the bucket name from an S3 ARN (arn:aws:s3:::name)."""
    if not location:
        return ""
    match = re.match(r"^arn:aws:s3:::([a-z0-9][a-z0-9.-]*[a-z0-9])$", location)
    return match.group(1) if match else ""


def get_nodegroup_actual_type(observed: Dict) -> str:
    """Return the running node-group instance type reported by the composed EKS
    XR's status.eks.nodeGroup.instanceType, or "" until the XR surfaces it
    (configuration-aws-eks v2.0.2+)."""
    obs = observed.get("eks-cluster")
    if not obs:
        return ""

    res = obs.resource if hasattr(obs, "resource") else obs
    node_group = res.get("status", {}).get("eks", {}).get("nodeGroup", {})
    return node_group.get("instanceType", "")
