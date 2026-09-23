"""11-import - external-name discovery for the import Composition.

AWS assigns most resource identifiers, so a stateless bootstrap cluster cannot
find what a previous run created. apis/ctp/composition-import.yaml derives the
discovery filters from spec.parameters.id, runs function-aws-query, and leaves:

  context.import.tagged      [{arn, tags}]                    Tagging API
  context.import.subnets     [{subnetId, tags, ...}]          ec2:DescribeSubnets
  context.import.routeTables [{routeTableId, associations, tags, ...}]

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

import re
import zlib

from crossplane.function import resource


# Mirrored from configuration-aws-network functions/network/main.k:278-322.
# Drift is SILENT: terraform-provider-aws matches a rule by re-expanding its own
# spec, not by the id, so a wrong hash still imports. Only NON-EMPTINESS matters -
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
# MainRouteTableAssociation, never imported (see _association_external_names).
_ROUTE_TABLE_KEYS = frozenset({"rt"})

_CTP_ID_TAG = "upbound.io/ctp-id"

_EXTERNAL_NAME = "crossplane.io/external-name"

# The logical names the tag sweep may supply, each with the (service, type) its
# ARN must carry. The name check stops the tag value from picking any composed
# resource (a Helm Release takes its external-name as the release name); the
# type check stops a subnet ARN tagged "vpc" from becoming the VPC id.
#
# subnet-* and rt are absent on purpose: the sweep indexes deleted resources for
# hours (9 tagged subnets for 6 live), so only the EC2 describes may supply them.
# A phantom subnet cost more than a bad reconcile: the Cluster resolved
# subnetIdRefs -> subnetIds against it once, and CreateCluster then failed with
# InvalidSubnetID.NotFound forever (2026-09-09).
_TAGGED_ARN_TYPES = {
    "vpc": ("ec2", "vpc"),
    "igw": ("ec2", "internet-gateway"),
    "sg": ("ec2", "security-group"),
    "lb-controller-pia": ("eks", "podidentityassociation"),
    "ebsCSIDriverPodIdentityAssociation": ("eks", "podidentityassociation"),
}
# One EIP per public subnet, so matched by pattern.
_K8GB_EIP = re.compile(r"k8gb-eip-\d+")

# Resources composed here whose external-name the import path can inject.
_LOCAL_IMPORT_KEYS = frozenset({
    "lb-controller-pia", "oidc-provider", "backup-policy-attachment",
    "lb-controller-attach",
})


def build_import_filters(id_val: str) -> dict:
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


def _arn_type(arn: str) -> tuple:
    """(service, resource type) of arn:<partition>:<service>:<region>:<acct>:<type>/<id>."""
    parts = arn.split(":", 5)
    if len(parts) != 6 or "/" not in parts[5]:
        return ("", "")
    return (parts[2], parts[5].split("/", 1)[0])


def _tag_sweep_accepts(logical: str, arn: str) -> bool:
    """True when the sweep may supply `logical` and `arn` is the type it names."""
    want = _TAGGED_ARN_TYPES.get(logical)
    if want is None and _K8GB_EIP.fullmatch(logical):
        want = ("ec2", "elastic-ip")
    return want is not None and _arn_type(arn) == want


def _pia_cluster(arn: str) -> str:
    """The cluster segment of an EKS Pod Identity Association ARN.

    arn:aws:eks:<region>:<acct>:podidentityassociation/<cluster>/<assoc-id>
    Empty string for any other ARN shape.
    """
    marker = ":podidentityassociation/"
    if marker not in arn:
        return ""
    return arn.split(marker, 1)[1].split("/", 1)[0]


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


def _by_resource_tag(tagged: list, ctp_id: str, cluster_name: str = "") -> dict:
    """Index the Tagging-API result by upbound.io/ctp-resource, scoped to ctp_id.

    Returns a LIST per logical name: the Tagging API keeps returning deleted
    resources under the same tag as their live replacement and tag data cannot
    tell them apart (measured: 16 entries for 10 live resources).

    The identity check is not redundant with the server-side filter. Unlike the
    EC2 describes, GetResources has no empty-filter guard, so an unresolvable
    filtersRef reads the whole region and this is the only boundary left - and
    one foreign control plane then makes its VPC the sole, unambiguous candidate
    for "vpc". Names and ARN types are checked against _TAGGED_ARN_TYPES for the
    same reason _live_by_resource_tag checks `allowed`.
    """
    out = {}
    if not ctp_id:
        return out
    for entry in tagged or []:
        tags = entry.get("tags") or {}
        if tags.get(_CTP_ID_TAG) != ctp_id:
            continue
        logical = tags.get("upbound.io/ctp-resource")
        arn = entry.get("arn", "")
        if not logical or not _tag_sweep_accepts(logical, arn):
            continue
        identifier = arn_identifier(arn)
        # The ARN says which cluster an association belongs to, so one from
        # another cluster is wrong whether or not it still exists. It also
        # resolves the ambiguity phantoms cause: 6 associations for one name
        # where 1 was live (2026-09-09). Ambiguity injects no external-name, so
        # Crossplane creates and AWS answers 409 ResourceInUseException.
        # cluster_name is empty only under Generated naming; see fn.py.
        pia_cluster = _pia_cluster(arn)
        if cluster_name and pia_cluster and pia_cluster != cluster_name:
            continue
        if identifier:
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
    earlier run created, where importing either strands the other.
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
        # subnets' associations, and importing it is not inert: route_table_id is
        # not ForceNew and Update calls ReplaceRouteTableAssociation, so the next
        # reconcile silently repoints that subnet at our IGW-default table and it
        # loses NAT egress. Un-imported it failed loudly instead.
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
            # Never import the main association. Nothing composes a
            # MainRouteTableAssociation any more (aws-network dropped it in
            # v2.2.0 - it made the VPC undeletable), and an imported one could
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


def _live_vpc_and_igw(import_ctx: dict, ctp_id: str) -> dict:
    """VPC and IGW ids the live EC2 describes vouch for.

    Neither type has a describe of its own in function-aws-query v0.2.0, but every
    subnet and route table carries vpcId and every route its gatewayId. Used only
    to NARROW sweep candidates, never as a source. Empty when nothing live is
    found, which leaves the sweep result as it was.
    """
    vpcs, igws = set(), set()
    route_tables = import_ctx.get("routeTables") or []
    for entry in (import_ctx.get("subnets") or []) + route_tables:
        if (entry.get("tags") or {}).get(_CTP_ID_TAG) == ctp_id and entry.get("vpcId"):
            vpcs.add(entry["vpcId"])
    for rt in route_tables:
        if (rt.get("tags") or {}).get(_CTP_ID_TAG) != ctp_id:
            continue
        for route in rt.get("routes") or []:
            gw = route.get("gatewayId") or ""
            if (route.get("destinationCidrBlock") == _ROUTE_DESTINATION
                    and route.get("state") == "active" and gw.startswith("igw-")):
                igws.add(gw)
    return {"vpc": vpcs, "igw": igws}


def build_external_names(import_ctx: dict, id_val: str, cluster_name: str,
                         account_id: str, oidc_host: str,
                         subnets: list = None) -> dict:
    """Map composition-resource-name to the external name to inject.

    Only resources this configuration owns are keyed here; the network and EKS
    leaves receive their subsets through their XRs' externalNames parameter.
    `subnets` is the list the Network XR will get, and it bounds which
    subnet-*/rta-* names may be discovered at all.
    """
    import_ctx = import_ctx or {}
    subnet_keys = frozenset(
        "subnet-" + suffix for suffix in _subnet_suffixes(subnets))

    # Unambiguous tag hits only. A dead identifier is the duplicate import
    # exists to prevent, plus a confusing error, so ambiguity injects nothing.
    # VPC and IGW phantoms are dropped first when the live describes vouch for
    # something, so a deleted VPC neither shadows the live one nor stands in
    # for it.
    names = {}
    vouched = _live_vpc_and_igw(import_ctx, id_val)
    for logical, candidates in _by_resource_tag(
            import_ctx.get("tagged"), id_val, cluster_name).items():
        if vouched.get(logical):
            candidates = [c for c in candidates if c in vouched[logical]]
        if len(candidates) == 1:
            names[logical] = candidates[0]

    # Live overlay, and the ONLY source for the types it covers. It also
    # supplies the associations nothing indexes. The route table matters twice
    # over, because `route` is derived from whatever `rt` ends up being.
    live_subnets = _live_by_resource_tag(
        import_ctx.get("subnets"), "subnetId", id_val, subnet_keys)
    names.update(live_subnets)
    names.update(_live_by_resource_tag(
        import_ctx.get("routeTables"), "routeTableId", id_val,
        _ROUTE_TABLE_KEYS))
    names.update(_association_external_names(
        import_ctx.get("routeTables"), live_subnets, id_val))

    # Gate the derivations below on import_ctx. They need no query, so emitting
    # them unconditionally is tempting - but on the default Composition every
    # existing consumer would gain an external-name it never had, and an
    # off-by-one derivation there means a duplicate OIDC provider, which breaks
    # IRSA. The default path never imports, so the upside is nil.
    if not import_ctx:
        return {k: v for k, v in names.items() if v}

    sg_id = names.get("sg")
    if sg_id:
        for logical, (fp, tp, proto, rtype, cidrs) in _LEGACY_SG_RULES.items():
            names[logical] = sgrule_external_name(
                sg_id, fp, tp, proto, rtype, cidrs)

    # Routes are not taggable and no API returns this id. Import does not
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
        names["lb-controller-attach"] = (
            f"{id_val}-lb-controller/arn:aws:iam::{account_id}:policy/{id_val}-lb-controller"
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
    delete. Dropping the key instead costs one unimported subnet, which is also
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
    passed through, so they are not importable - nothing discovers them either.
    """
    return {
        k: v for k, v in (external_names or {}).items()
        if k in _EKS_KEYS
    }


def carried_external_names(observed_xr: dict, import_ctx: dict) -> dict:
    """The externalNames map a sub-XR received last time, when nothing is discovered.

    externalNames is a granular map, so a key this reconcile omits is deleted
    from the sub-XR and upstream stops stamping it, which strips the annotation
    as described in apply_external_names. Carried only without an import
    context (the default Composition): the value is what was forwarded, not what
    the MR carries now, so while discovery runs it stays authoritative -
    otherwise a forwarded phantom the provider has since replaced is re-stamped
    for good. Discovery misses are for upstream to cover, by preferring the
    observed annotation over externalNames.
    """
    if import_ctx:
        return {}
    return (((observed_xr or {}).get("spec") or {}).get("parameters") or {}).get(
        "externalNames") or {}


def apply_external_names(rsp, external_names: dict, observed: dict) -> None:
    """Stamp crossplane.io/external-name on matching desired resources.

    Precedence: an annotation the desired resource already carries (backup.py
    sets the Bucket's from the location ARN), then the observed one, then the
    discovered one. Observed is carried forward because Crossplane applies
    composed resources with SSA and upjet writes the annotation back only when it
    changes, so once injected this function is its sole owner and any reconcile
    that omits it deletes it - a discovery miss, or a switch back to the default
    Composition. Limited to importable names, the only ones this function can
    end up owning, and it runs on both Compositions.
    """
    for name in list(rsp.desired.resources.keys()):
        res = resource.struct_to_dict(rsp.desired.resources[name].resource)
        ann = res.get("metadata", {}).get("annotations", {}) or {}
        if ann.get(_EXTERNAL_NAME):
            continue
        logical = ann.get("crossplane.io/composition-resource-name", name)
        target = external_names.get(logical)
        if logical in _LOCAL_IMPORT_KEYS or _K8GB_EIP.fullmatch(logical):
            observed_ann = (((observed or {}).get(name) or {}).get("metadata") or {}).get(
                "annotations") or {}
            target = observed_ann.get(_EXTERNAL_NAME) or target
        if not target:
            continue
        res.setdefault("metadata", {}).setdefault("annotations", {})[
            _EXTERNAL_NAME] = target
        resource.update(rsp.desired.resources[name], res)
