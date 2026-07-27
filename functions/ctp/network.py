"""01-network — VPC + subnets via the Network XR."""

from crossplane.function import resource

from .prelude import stamp


def _default_subnets(region):
    """Resilient three-AZ layout: one public and one private /19 subnet in each
    of <region>a/b/c. Six /19 blocks fit comfortably in the default /16 VPC and
    spread nodes across three AZs so the loss of one AZ does not take down the
    control plane."""
    zones = ["a", "b", "c"]
    public = ["192.168.0.0/19", "192.168.32.0/19", "192.168.64.0/19"]
    private = ["192.168.96.0/19", "192.168.128.0/19", "192.168.160.0/19"]
    subnets = []
    for zone, cidr in zip(zones, public):
        subnets.append({"availabilityZone": f"{region}{zone}", "cidrBlock": cidr, "type": "public"})
    for zone, cidr in zip(zones, private):
        subnets.append({"availabilityZone": f"{region}{zone}", "cidrBlock": cidr, "type": "private"})
    return subnets


def add_network_resource(rsp, id_val, region, provider_config, mgmt_policies,
                         network_param, config):
    vpc_cidr = network_param.get("vpcCidrBlock", "192.168.0.0/16")
    subnets = network_param.get("subnets") or _default_subnets(region)
    network = {
        "apiVersion": "aws.platform.upbound.io/v1alpha1",
        "kind": "Network",
        "metadata": {
            "name": id_val,
            "namespace": config["namespace"],
            "annotations": {
                "crossplane.io/composition-resource-name": "network"
            }
        },
        "spec": {
            "parameters": {
                "id": id_val,
                "region": region,
                "providerConfigName": provider_config,
                "managementPolicies": mgmt_policies,
                "vpcCidrBlock": vpc_cidr,
                "subnets": subnets,
            }
        }
    }
    # XR; no forProvider.tags — the underlying composition handles AWS tags.
    stamp(network, config)
    resource.update(rsp.desired.resources["network"], network)
