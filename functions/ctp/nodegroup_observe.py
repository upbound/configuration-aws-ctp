"""11-nodegroup-observe — observe-only NodeGroup for instance-type drift
detection. The status section uses the observed instanceTypes to surface a
NodeGroupTypeImmutable condition when the running type differs from the
desired one."""

from crossplane.function import resource

from .prelude import stamp


def add_nodegroup_observe(rsp, id_val, ng_ref_name, region, provider_config, config):
    ng_observe = {
        "apiVersion": "eks.aws.m.upbound.io/v1beta1",
        "kind": "NodeGroup",
        "metadata": {
            "name": f"{id_val}-nodegroup",
            "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": "nodegroup-observe",
                "crossplane.io/external-name": ng_ref_name
            }
        },
        "spec": {
            "managementPolicies": ["Observe"],
            "forProvider": {
                "region": region,
                "clusterNameSelector": {
                    "matchControllerRef": True
                }
            },
            "providerConfigRef": {
                "name": provider_config,
                "kind": "ProviderConfig"
            }
        }
    }
    # Observe-only: tags would not propagate, so annotation only.
    stamp(ng_observe, config)
    resource.update(rsp.desired.resources["nodegroup-observe"], ng_observe)
