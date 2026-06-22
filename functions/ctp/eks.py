"""02-eks — EKS cluster XR."""

from crossplane.function import resource

from .prelude import stamp


def add_eks_resource(rsp, id_val, region, provider_config, version, nodes,
                    access_config, mgmt_policies, iam_param, config):
    eks = {
        "apiVersion": "aws.platform.upbound.io/v1alpha1",
        "kind": "EKS",
        "metadata": {
            "name": id_val,
            "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": "eks-cluster"
            }
        },
        "spec": {
            "parameters": {
                "id": id_val,
                "region": region,
                "providerConfigName": provider_config,
                "version": version,
                "nodes": nodes,
                "accessConfig": access_config,
                "managementPolicies": mgmt_policies
            }
        }
    }

    if iam_param:
        eks["spec"]["parameters"]["iam"] = iam_param

    stamp(eks, config)
    resource.update(rsp.desired.resources["eks-cluster"], eks)
