"""11-adopt - external-name discovery for the adopt Composition.

AWS assigns most resource identifiers, so a stateless bootstrap cluster cannot
find what a previous run created. apis/ctp/composition-adopt.yaml derives the
discovery filters from spec.parameters.id, runs function-aws-query, and leaves:

  context.adopt.tagged      [{arn, tags}]                    Tagging API
  context.adopt.subnets     [{subnetId, tags, ...}]          ec2:DescribeSubnets
  context.adopt.routeTables [{routeTableId, associations, tags, ...}]

build_external_names turns those into {composition-resource-name: external-name};
apply_external_names stamps them so Crossplane observes instead of creating.

Both sources are needed: the Tagging API covers every taggable type but keeps
returning deleted resources, which makes a logical name ambiguous. An EC2
describe returns only live resources and carries route-table associations, which
nothing else indexes, so it wins for the two types it covers.

Everything read out of the context is untrusted. upbound.io/ctp-resource is an
ordinary AWS tag and its value picks which resource an identifier lands on, so
every entry is checked against this XR's upbound.io/ctp-id and against the set of
logical names this configuration can emit. Ambiguity is refused, never guessed.
"""

import zlib

from crossplane.function import resource


# Mirrored from configuration-aws-network functions/network/main.k:278-322.
# Drift is SILENT: terraform-provider-aws matches a rule by re-expanding its own
# spec, not by the id, so a wrong hash still adopts. Only NON-EMPTINESS matters -
# an empty external-name means "absent", so Crossplane creates and AWS returns
# InvalidPermission.Duplicate. Kept exact anyway so the annotation matches what
# upjet would have written.
_LEGACY_SG_RULES = {
    "sgr-postgres": (5432, 5432, "tcp", "ingress", ["0.0.0.0/0"]),
    "sgr-mysql": (3306, 3306, "tcp", "ingress", ["0.0.0.0/0"]),
}


# Mirrored from main.k:228. Must be written as {route-table-id}_{destination}:
# upjet's aws_route GetIDFn rebuilds the id from spec.forProvider and ignores the
# annotation, so any other form is rewritten on every reconcile forever. The
# r-{rt}{hashcode} form is Terraform's internal id, set only on create.
_ROUTE_DESTINATION = "0.0.0.0/0"

# The only logical name the route-table describe may supply. `mrt` is the
# MainRouteTableAssociation, never adopted (see _association_external_names).
_ROUTE_TABLE_KEYS = frozenset({"rt"})

_CTP_ID_TAG = "upbound.io/ctp-id"


def build_adopt_filters(id_val: str) -> dict:
    """Discovery filters for the three queries, derived from `id`.

    The two APIs need different shapes for one intent: a Tagging-API TagFilter
    Key is a bare tag key, an EC2 Filter Name comes from a fixed vocabulary where
    tags are "tag:<key>". Derived rather than configured so a filter that does
    not scope to this control plane cannot be written at all.
    """
    if not id_val:
        return {}
    return {
        "tagged": [{"name": _CTP_ID_TAG, "values": [id_val]}],
        "ec2": [{"name": "tag:" + _CTP_ID_TAG, "values": [id_val]}],
    }


def _string_hashcode(s: str) -> int:
    """Terraform's create.StringHashcode: the unsigned crc32, unmodified.

    Upstream looks like it wraps to a signed int, but Go's `int` is 64-bit on
    both published platforms, so a uint32 widens and is returned as-is. Do NOT
    "fix" this to wrap at 2^31 - that was tried and measured wrong on live AWS:
    real rules came back sgrule-3302807844 / sgrule-2392464648, where wrapping
    gives 992159452 / 1902502648. Keep a test vector on each side of 2^31; the
    earlier ones were all below it and could not tell the two apart.
    """
    return zlib.crc32(s.encode()) & 0xffffffff


def sgrule_external_name(sg_id: str, from_port: int, to_port: int,
                         protocol: str, rule_type: str,
                         cidrs: list) -> str:
    """Terraform's aws_security_group_rule id: crc32 of the rule signature.

    Mirrors securityGroupRuleCreateID (vpc_security_group_rule.go). No AWS API
    exposes it. Ports are written only when > 0, and the ipv6/prefix-list/
    user-id-group-pair fields upstream appends are each guarded by len > 0 with
    no empty marker, so omitting them here is byte-identical for these rules.
    """
    buf = f"{sg_id}-"
    if from_port > 0:
        buf += f"{from_port}-"
    if to_port > 0:
        buf += f"{to_port}-"
    buf += f"{protocol}-{rule_type}-"
    for cidr in sorted(cidrs):
        buf += f"{cidr}-"
    return f"sgrule-{_string_hashcode(buf)}"


def arn_identifier(arn: str) -> str:
    """The identifier an ARN carries: its last slash- or colon-delimited segment."""
    if not arn:
        return ""
    tail = arn.rsplit("/", 1)[-1]
    return tail.rsplit(":", 1)[-1]


def _format_subnet(entry: dict) -> str:
    """One subnets entry -> the suffix upstream derives its names from.

    Mirrors configuration-aws-network main.k:66-68. Published API: the Network
    XRD documents its keys as subnet-<az>-<cidr>-<type> / rta-<same>. Mirrored to
    validate only - see network_external_names for why that direction matters.
    """
    cidr = str(entry.get("cidrBlock") or "").replace(".", "-").replace("/", "-")
    return "{}-{}-{}".format(
        entry.get("availabilityZone") or "", cidr, entry.get("type") or "")


def _subnet_suffixes(subnets: list) -> frozenset:
    return frozenset(_format_subnet(entry) for entry in subnets or [])


def _by_resource_tag(tagged: list, ctp_id: str) -> dict:
    """Index the Tagging-API result by upbound.io/ctp-resource, scoped to ctp_id.

    Returns a LIST per logical name: the Tagging API keeps returning deleted
    resources under the same tag as their live replacement and tag data cannot
    tell them apart (measured: 16 entries for 10 live resources).

    The identity check is not redundant with the server-side filter. Unlike the
    EC2 describes, GetResources has no empty-filter guard, so an unresolvable
    filtersRef reads the whole region and this is the only boundary left - and
    one foreign control plane then makes its VPC the sole, unambiguous candidate
    for "vpc".
    """
    out = {}
    if not ctp_id:
        return out
    for entry in tagged or []:
        tags = entry.get("tags") or {}
        if tags.get(_CTP_ID_TAG) != ctp_id:
            continue
        logical = tags.get("upbound.io/ctp-resource")
        identifier = arn_identifier(entry.get("arn", ""))
        if logical and identifier:
            out.setdefault(logical, []).append(identifier)
    return out


def _live_by_resource_tag(entries: list, id_field: str, ctp_id: str,
                          allowed: frozenset) -> dict:
    """Index an EC2 describe by upbound.io/ctp-resource. Ambiguity is refused.

    Scoped by identity (the server-side filter is a scope, not a boundary) and by
    `allowed`, because the tag value is writable by anyone with ec2:CreateTags -
    without it a subnet tagged ctp-resource=vpc has its id injected as the VPC's
    external name and a duplicate VPC is created. `allowed` also guarantees the
    prefix callers assume. An empty ctp_id indexes nothing; an identity check
    must not fail open.

    Two candidates here are two *live* resources under one name - a duplicate an
    earlier run created, where adopting either strands the other.
    """
    out = {}
    if not ctp_id:
        return out
    for entry in entries or []:
        tags = entry.get("tags") or {}
        if tags.get(_CTP_ID_TAG) != ctp_id:
            continue
        logical = tags.get("upbound.io/ctp-resource")
        identifier = entry.get(id_field)
        if not logical or not identifier or logical not in allowed:
            continue
        out.setdefault(logical, []).append(identifier)
    return {k: v[0] for k, v in out.items() if len(v) == 1}


def _association_external_names(route_tables: list, subnets: dict,
                                ctp_id: str) -> dict:
    """Map rta-<suffix> to its live RouteTableAssociation id.

    Associations carry no tags and the Tagging API does not index them, so they
    are matched through their subnet: upstream derives subnet-<suffix> and
    rta-<suffix> from one subnets entry (main.k:146,178). `subnets` is the
    already-validated {logical: subnetId} map, which is what makes the prefix
    strip below safe.
    """
    by_subnet_id = {v: k for k, v in (subnets or {}).items()}
    out = {}
    for rt in route_tables or []:
        # A foreign or operator-added (e.g. NAT) route table can own one of our
        # subnets' associations, and adopting it is not inert: route_table_id is
        # not ForceNew and Update calls ReplaceRouteTableAssociation, so the next
        # reconcile silently repoints that subnet at our IGW-default table and it
        # loses NAT egress. Un-adopted it failed loudly instead.
        if (rt.get("tags") or {}).get(_CTP_ID_TAG) != ctp_id:
            continue
        for assoc in rt.get("associations") or []:
            # States are associating/associated/disassociating/disassociated/
            # failed. Empty is accepted because function-aws-query emits "" only
            # when AWS omits AssociationState, which old responses do.
            if (assoc.get("state") or "associated") != "associated":
                continue
            assoc_id = assoc.get("routeTableAssociationId")
            if not assoc_id:
                continue
            # Never adopt the main association. Nothing composes a
            # MainRouteTableAssociation any more (aws-network dropped it in
            # v2.2.0 - it made the VPC undeletable), and an adopted one could
            # not be deleted anyway: it restores original_route_table_id, which
            # AWS never returns. Redundant in practice too, since AWS reports no
            # subnet id for an implicit association.
            if assoc.get("main"):
                continue
            subnet_logical = by_subnet_id.get(assoc.get("subnetId"))
            if not subnet_logical:
                continue
            out.setdefault(
                "rta-" + subnet_logical[len("subnet-"):], []).append(assoc_id)
    # Refuse an ambiguous key, as everywhere else. AWS allows one association per
    # subnet, but DescribeRouteTables is paginated, so a read straddling a
    # ReplaceRouteTableAssociation can return both the old and the new one.
    return {k: v[0] for k, v in out.items() if len(v) == 1}


def build_external_names(adopt_ctx: dict, id_val: str, cluster_name: str,
                         account_id: str, oidc_host: str,
                         subnets: list = None) -> dict:
    """Map composition-resource-name to the external name to inject.

    Only resources this configuration owns are keyed here; the network and EKS
    leaves receive their subsets through their XRs' externalNames parameter.
    `subnets` is the list the Network XR will get, and it bounds which
    subnet-*/rta-* names may be discovered at all.
    """
    adopt_ctx = adopt_ctx or {}
    subnet_keys = frozenset(
        "subnet-" + suffix for suffix in _subnet_suffixes(subnets))

    # Unambiguous tag hits only. A dead identifier is the duplicate adoption
    # exists to prevent, plus a confusing error, so ambiguity injects nothing.
    names = {}
    for logical, candidates in _by_resource_tag(
            adopt_ctx.get("tagged"), id_val).items():
        if len(candidates) == 1:
            names[logical] = candidates[0]

    # Live overlay: settles what the sweep had to refuse, and supplies the
    # associations nothing indexes. The route table matters twice over, because
    # `route` is derived from whatever `rt` ends up being.
    live_subnets = _live_by_resource_tag(
        adopt_ctx.get("subnets"), "subnetId", id_val, subnet_keys)
    names.update(live_subnets)
    names.update(_live_by_resource_tag(
        adopt_ctx.get("routeTables"), "routeTableId", id_val,
        _ROUTE_TABLE_KEYS))
    names.update(_association_external_names(
        adopt_ctx.get("routeTables"), live_subnets, id_val))

    # Gate the derivations below on adopt_ctx. They need no query, so emitting
    # them unconditionally is tempting - but on the default Composition every
    # existing consumer would gain an external-name it never had, and an
    # off-by-one derivation there means a duplicate OIDC provider, which breaks
    # IRSA. The default path never adopts, so the upside is nil.
    if not adopt_ctx:
        return {k: v for k, v in names.items() if v}

    sg_id = names.get("sg")
    if sg_id:
        for logical, (fp, tp, proto, rtype, cidrs) in _LEGACY_SG_RULES.items():
            names[logical] = sgrule_external_name(
                sg_id, fp, tp, proto, rtype, cidrs)

    # Routes are not taggable and no API returns this id. Adoption does not
    # actually depend on it (upjet ignores the annotation) - it is emitted so the
    # rendered annotation matches the one upjet writes back.
    rt_id = names.get("rt")
    if rt_id:
        names["route"] = f"{rt_id}_{_ROUTE_DESTINATION}"

    if account_id and oidc_host:
        names["oidc-provider"] = (
            f"arn:aws:iam::{account_id}:oidc-provider/{oidc_host}"
        )

    if account_id:
        names["backup-policy-attachment"] = (
            f"{id_val}-backup-irsa/arn:aws:iam::{account_id}:policy/{id_val}-backup-s3"
        )

    # Pod Identity associations arrive through the tag sweep, via the
    # eks:podidentityassociation entry in resourceTypeFilters. There is
    # deliberately no second source: an earlier design read them from a Cloud
    # Control step that was dropped, and the reader outlived the producer.
    return {k: v for k, v in names.items() if v}


# Each sub-configuration rejects unknown externalNames keys outright, aborting
# its own composition, so the map must be split before it is forwarded. Keys
# absent from both sets belong to resources composed here and are applied
# locally by apply_external_names.
_NETWORK_KEYS = frozenset({
    "vpc", "igw", "rt", "route", "sg", "sgr-postgres", "sgr-mysql",
})
_EKS_KEYS = frozenset({
    "controlplaneRole", "kubernetesCluster", "clusterSecurityGroupImport",
    "kubernetesClusterAuth", "nodegroupRole", "nodeGroupPublic",
    "vpc-cni-addon", "ebsCSIDriverRole", "ebsCSIDriverPodIdentityAssociation",
    "aws-ebs-csi-driver-addon", "eks-pod-identity-agent-addon",
    "providerConfig-kubernetes", "providerConfig-helm",
})


def network_external_names(external_names: dict, subnets: list = None) -> dict:
    """The subset configuration-aws-network composes.

    Subnet and association names depend on the caller's subnet list, so they are
    validated against it rather than enumerated. Forwarding a subnet-*/rta-* key
    outside that set aborts the Network composition and takes the VPC, every
    subnet and the route table with it - and a subnet orphaned by an earlier
    layout is the expected steady state, since Provision/ObserveOnly never
    delete. Dropping the key instead costs one unadopted subnet, which is also
    why _format_subnet is mirrored rather than trusted.
    """
    accepted = frozenset(
        f"{prefix}-{suffix}"
        for suffix in _subnet_suffixes(subnets)
        for prefix in ("subnet", "rta")
    )
    return {
        k: v for k, v in (external_names or {}).items()
        if k in _NETWORK_KEYS or k in accepted
    }


def eks_external_names(external_names: dict) -> dict:
    """The subset configuration-aws-eks composes.

    A strict allow-list. The sha256-digest AccessEntry names are dropped, not
    passed through, so they are not adoptable - nothing discovers them either.
    """
    return {
        k: v for k, v in (external_names or {}).items()
        if k in _EKS_KEYS
    }


def apply_external_names(rsp, external_names: dict) -> None:
    """Stamp crossplane.io/external-name on matching desired resources.

    An annotation already present wins: backup.py sets the Bucket's from the
    location ARN and must not be second-guessed.
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
