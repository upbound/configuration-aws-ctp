"""02-eks — EKS cluster XR."""

from crossplane.function import resource

from .prelude import stamp


def add_eks_resource(rsp, id_val, region, provider_config, version, nodes,
                    access_config, mgmt_policies, iam_param, config,
                    naming="Generated"):
    eks = {
        "apiVersion": "aws.platform.upbound.io/v1alpha1",
        "kind": "EKS",
        "metadata": {
            "name": id_val,
            "namespace": config["namespace"],
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

    # Forwarded to configuration-aws-eks v2.1.0+. Deterministic makes the cluster,
    # the three IAM roles and the node group name-as-identifier, so a stateless
    # bootstrap re-adopts them without any AWS query.
    eks["spec"]["parameters"]["naming"] = naming

    stamp(eks, config)
    resource.update(rsp.desired.resources["eks-cluster"], eks)
