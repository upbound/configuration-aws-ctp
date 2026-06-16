"""01-network — VPC + subnets via the Network XR."""

from crossplane.function import resource

from .prelude import stamp


def add_network_resource(rsp, id_val, region, provider_config, mgmt_policies, config):
    network = {
        "apiVersion": "aws.platform.upbound.io/v1alpha1",
        "kind": "Network",
        "metadata": {
            "name": id_val,
            "namespace": "default",
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
                "vpcCidrBlock": "192.168.0.0/16",
                "subnets": [
                    {"availabilityZone": f"{region}a", "cidrBlock": "192.168.0.0/18", "type": "public"},
                    {"availabilityZone": f"{region}b", "cidrBlock": "192.168.64.0/18", "type": "public"},
                    {"availabilityZone": f"{region}a", "cidrBlock": "192.168.128.0/18", "type": "private"},
                    {"availabilityZone": f"{region}b", "cidrBlock": "192.168.192.0/18", "type": "private"},
                ]
            }
        }
    }
    # XR; no forProvider.tags — the underlying composition handles AWS tags.
    stamp(network, config)
    resource.update(rsp.desired.resources["network"], network)
