"""04-usages — deletion-order Usage guards.

Release must finish uninstalling before the EKS cluster is deleted, and the
EKS cluster must be fully gone before the VPC/subnets are removed.
"""

from crossplane.function import resource

from .prelude import stamp


def add_usage_resources(rsp, id_val, config):
    usage_release_eks = {
        "apiVersion": "protection.crossplane.io/v1beta1",
        "kind": "Usage",
        "metadata": {
            "name": f"{id_val}-usage-release-eks",
            "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": "usage-release-eks"
            }
        },
        "spec": {
            "of": {
                "apiVersion": "aws.platform.upbound.io/v1alpha1",
                "kind": "EKS",
                "resourceRef": {
                    "name": id_val,
                    "namespace": "default"
                }
            },
            "by": {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "Release",
                "resourceRef": {
                    "name": f"{id_val}-uxp"
                }
            },
            "reason": "UXP Helm Release must finish uninstalling before the EKS cluster is deleted",
            "replayDeletion": True
        }
    }

    usage_eks_network = {
        "apiVersion": "protection.crossplane.io/v1beta1",
        "kind": "Usage",
        "metadata": {
            "name": f"{id_val}-usage-eks-network",
            "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": "usage-eks-network"
            }
        },
        "spec": {
            "of": {
                "apiVersion": "aws.platform.upbound.io/v1alpha1",
                "kind": "Network",
                "resourceRef": {
                    "name": id_val,
                    "namespace": "default"
                }
            },
            "by": {
                "apiVersion": "aws.platform.upbound.io/v1alpha1",
                "kind": "EKS",
                "resourceRef": {
                    "name": id_val
                }
            },
            "reason": "EKS cluster must be fully deleted before VPC/subnets are removed",
            "replayDeletion": True
        }
    }

    stamp(usage_release_eks, config)
    stamp(usage_eks_network, config)
    resource.update(rsp.desired.resources["usage-release-eks"], usage_release_eks)
    resource.update(rsp.desired.resources["usage-eks-network"], usage_eks_network)
