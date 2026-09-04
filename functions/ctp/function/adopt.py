"""11-adopt - external-name discovery for the adopt Composition.

AWS assigns most resource identifiers, so a stateless bootstrap cluster holds no
way to find the resources a previous run created. The adopt Composition
(apis/ctp/compositions/adopt.yaml) runs function-aws-query ahead of this function
and leaves three lists in the pipeline context:

  context.adopt.tagged  [{arn, tags}]                  Resource Groups Tagging API
  context.adopt.assoc   [{identifier, properties}]     AWS::EC2::SubnetRouteTableAssociation
  context.adopt.pia     [{identifier, properties}]     AWS::EKS::PodIdentityAssociation

build_external_names turns them, plus the identifiers that are derivable without
any query, into a {composition-resource-name: external-name} map.
apply_external_names stamps it onto the desired resources. Crossplane then
observes the existing AWS object instead of creating a duplicate.

Three identifiers need no query at all:
  Route                  {route-table-id}_0.0.0.0/0        (upjet route() GetIDFn)
  OpenIDConnectProvider  arn:aws:iam::{acct}:oidc-provider/{oidc-host}
  RolePolicyAttachment   {role-name}/{policy-arn}
"""

from crossplane.function import resource


def arn_identifier(arn: str) -> str:
    """The AWS identifier carried by an ARN.

    Tagging-API ARNs put the identifier last, after either a slash
    (arn:aws:ec2:r:a:vpc/vpc-0abc) or a colon
    (arn:aws:eks:r:a:cluster/name). Take the final slash- or colon-delimited
    segment, whichever comes later.
    """
    if not arn:
        return ""
    tail = arn.rsplit("/", 1)[-1]
    return tail.rsplit(":", 1)[-1]


def _by_resource_tag(tagged: list) -> dict:
    """Index the Tagging-API result by the upbound.io/ctp-resource tag.

    Returns {logical: [identifier, ...]} - a LIST, not a single value, because
    the Resource Groups Tagging API keeps returning deleted resources and they
    carry the same upbound.io/ctp-resource tag as their live replacements.

    Measured 2026-09-03 on control plane awsctpcp1: GetResources returned 16
    entries where only 10 resources existed; 6 were deleted subnets, and two
    entries shared the tag subnet-eu-central-1a-192-168-96-0-19-private - one
    live, one gone. A last-write-wins dict would silently pick whichever came
    last in pagination order, and injecting a dead identifier makes Crossplane
    observe nothing and create a duplicate. So collect every candidate and let
    the caller resolve which is live.
    """
    out = {}
    for entry in tagged or []:
        tags = entry.get("tags") or {}
        logical = tags.get("upbound.io/ctp-resource")
        identifier = arn_identifier(entry.get("arn", ""))
        if logical and identifier:
            out.setdefault(logical, []).append(identifier)
    return out


def build_external_names(adopt_ctx: dict, id_val: str, cluster_name: str,
                         account_id: str, oidc_host: str) -> dict:
    """Map composition-resource-name to the external name to inject.

    Only resources this configuration owns are keyed here. The leaves owned by
    configuration-aws-network and configuration-aws-eks are handed the same map
    through their XRs' externalNames parameter (see network.py / eks.py).
    """
    adopt_ctx = adopt_ctx or {}

    # Take a tag-discovered identifier only when it is unambiguous. Where the
    # Tagging API offered several candidates for one logical resource, at least
    # one is a deleted resource still indexed (see _by_resource_tag), and there
    # is no way to tell which from tag data alone - so inject nothing and let
    # Crossplane create. A duplicate is bad; adopting a dead identifier is the
    # same duplicate plus a confusing error, so ambiguity must not be guessed.
    #
    # Resolving these properly needs an authoritative lookup (Cloud Control, or
    # ec2:DescribeSubnets and friends) which lists only live resources. Until
    # that exists, log the ambiguity rather than hiding it.
    names = {}
    for logical, candidates in _by_resource_tag(adopt_ctx.get("tagged")).items():
        if len(candidates) == 1:
            names[logical] = candidates[0]

    # The derived entries below are gated on adopt_ctx, i.e. on the adopt
    # Composition having run its discovery steps. They need no query - they are
    # computed from the account id and the cluster's OIDC host - so it is
    # tempting to emit them unconditionally. Do not.
    #
    # On the default Composition adopt_ctx is {} and nothing must be injected:
    # every existing consumer would otherwise gain a crossplane.io/external-name
    # on its OpenIDConnectProvider and RolePolicyAttachment where it previously
    # had none. If either derivation is off by a character, that consumer
    # observes nothing and creates a duplicate - a second OIDC provider breaks
    # IRSA. The upside is nil, because the default path never adopts.
    if not adopt_ctx:
        return {k: v for k, v in names.items() if v}

    # Derived: the OIDC provider's Terraform ID is its ARN, which fn.py already
    # computes from the cluster's OIDC issuer host and the account ID.
    if account_id and oidc_host:
        names["oidc-provider"] = (
            f"arn:aws:iam::{account_id}:oidc-provider/{oidc_host}"
        )

    # Derived: a role-policy attachment is imported as role-name/policy-arn, and
    # both halves are deterministic names this configuration chose.
    if account_id:
        names["backup-policy-attachment"] = (
            f"{id_val}-backup-irsa/arn:aws:iam::{account_id}:policy/{id_val}-backup-s3"
        )

    # Pod Identity associations have an opaque identifier ("a-" + 17 chars) and
    # can arrive from either source, so take whichever supplied it. The tag
    # sweep already populated `names` if the Tagging API indexes the type; this
    # only fills the gap when it does not. Matched on cluster + service account
    # because the identifier itself carries no meaning.
    for entry in adopt_ctx.get("pia") or []:
        props = entry.get("properties") or {}
        if props.get("ClusterName") != cluster_name:
            continue
        sa = props.get("ServiceAccount")
        logical = {
            "aws-load-balancer-controller": "lb-controller-pia",
            "ebs-csi-controller-sa": "ebsCSIDriverPodIdentityAssociation",
        }.get(sa)
        if logical and not names.get(logical):
            names[logical] = entry.get("identifier", "")

    return {k: v for k, v in names.items() if v}


def apply_external_names(rsp, external_names: dict) -> None:
    """Stamp crossplane.io/external-name on every desired resource whose
    composition-resource-name appears in the map.

    An annotation already present wins: backup.py sets the Bucket's from the
    location ARN and must not be second-guessed here.
    """
    if not external_names:
        return
    for name in list(rsp.desired.resources.keys()):
        res = resource.struct_to_dict(rsp.desired.resources[name].resource)
        ann = res.get("metadata", {}).get("annotations", {}) or {}
        logical = ann.get("crossplane.io/composition-resource-name", name)
        target = external_names.get(logical)
        if not target or ann.get("crossplane.io/external-name"):
            continue
        res.setdefault("metadata", {}).setdefault("annotations", {})[
            "crossplane.io/external-name"] = target
        resource.update(rsp.desired.resources[name], res)
