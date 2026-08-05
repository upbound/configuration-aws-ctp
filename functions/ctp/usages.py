"""04-usages — deletion-order Usage guards.

Base guards: the UXP Release must finish uninstalling before the EKS cluster is
deleted, and the EKS cluster must be fully gone before the VPC/subnets are
removed.

Add-on guards: every child-cluster Release/Object added by an add-on (LB
controller, k8gb, ArgoCD) also gets an `of: EKS, by: <resource>` guard, so it
finishes uninstalling before the EKS cluster/kubeconfig is torn out from under
it (otherwise the child Objects orphan-finalize). Emitted only when the add-on
is enabled.
"""

from crossplane.function import resource

from .prelude import stamp


def _emit_eks_usage(rsp, id_val, cr_name, by_api_version, by_kind, by_name,
                    reason, config):
    """Emit an `of: EKS, by: <resource>` Usage guarding a child-cluster
    resource against premature EKS deletion."""
    usage = {
        "apiVersion": "protection.crossplane.io/v1beta1",
        "kind": "Usage",
        "metadata": {
            "name": f"{id_val}-{cr_name}",
            "namespace": config["namespace"],
            "annotations": {
                "crossplane.io/composition-resource-name": cr_name
            }
        },
        "spec": {
            "of": {
                "apiVersion": "aws.platform.upbound.io/v1alpha1",
                "kind": "EKS",
                "resourceRef": {
                    "name": id_val,
                    "namespace": config["namespace"]
                }
            },
            "by": {
                "apiVersion": by_api_version,
                "kind": by_kind,
                "resourceRef": {
                    "name": by_name
                }
            },
            "reason": reason,
            "replayDeletion": True
        }
    }
    stamp(usage, config)
    resource.update(rsp.desired.resources[cr_name], usage)


def add_usage_resources(rsp, id_val, config, k8gb_enabled=False,
                        argocd_enabled=False, k8gb_eip_count=0):
    usage_release_eks = {
        "apiVersion": "protection.crossplane.io/v1beta1",
        "kind": "Usage",
        "metadata": {
            "name": f"{id_val}-usage-release-eks",
            "namespace": config["namespace"],
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
                    "namespace": config["namespace"]
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
            "namespace": config["namespace"],
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
                    "namespace": config["namespace"]
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

    gateway_enabled = k8gb_enabled or argocd_enabled

    if gateway_enabled:
        _emit_eks_usage(
            rsp, id_val, "usage-lbcontroller-eks",
            "helm.m.crossplane.io/v1beta1", "Release",
            f"{id_val}-lb-controller",
            "AWS Load Balancer Controller Release must finish uninstalling before the EKS cluster is deleted",
            config)
        _emit_eks_usage(
            rsp, id_val, "usage-envoy-gateway-eks",
            "helm.m.crossplane.io/v1beta1", "Release",
            f"{id_val}-envoy-gateway",
            "Envoy Gateway Release must finish uninstalling before the EKS cluster is deleted",
            config)
        for cr_name in ("envoy-proxy-config", "gateway-class"):
            _emit_eks_usage(
                rsp, id_val, f"usage-{cr_name}-eks",
                "kubernetes.m.crossplane.io/v1alpha1", "Object",
                f"{id_val}-{cr_name}",
                f"Envoy Gateway {cr_name} Object must be removed before the EKS cluster is deleted",
                config)

    if k8gb_enabled:
        _emit_eks_usage(
            rsp, id_val, "usage-k8gb-eks",
            "helm.m.crossplane.io/v1beta1", "Release",
            f"{id_val}-k8gb",
            "k8gb Release must finish uninstalling before the EKS cluster is deleted",
            config)
        # The observe-only CoreDNS Object also guards the EKS cluster so it does
        # not orphan-finalize when the cluster/kubeconfig is torn out first.
        _emit_eks_usage(
            rsp, id_val, "usage-k8gb-coredns-eks",
            "kubernetes.m.crossplane.io/v1alpha1", "Object",
            f"{id_val}-k8gb-coredns",
            "k8gb CoreDNS observe Object must be removed before the EKS cluster is deleted",
            config)

        # The controller must outlive the k8gb Release so it deletes the CoreDNS
        # NLB (and frees its EIPs) before the controller is removed.
        lbc_k8gb_usage = {
            "apiVersion": "protection.crossplane.io/v1beta1",
            "kind": "Usage",
            "metadata": {
                "name": f"{id_val}-usage-lbcontroller-k8gb",
                "namespace": config["namespace"],
                "annotations": {
                    "crossplane.io/composition-resource-name": "usage-lbcontroller-k8gb"
                }
            },
            "spec": {
                "of": {
                    "apiVersion": "helm.m.crossplane.io/v1beta1",
                    "kind": "Release",
                    "resourceRef": {"name": f"{id_val}-lb-controller"}
                },
                "by": {
                    "apiVersion": "helm.m.crossplane.io/v1beta1",
                    "kind": "Release",
                    "resourceRef": {"name": f"{id_val}-k8gb"}
                },
                "reason": "AWS Load Balancer Controller must outlive the k8gb Release so it deletes the CoreDNS NLB (and frees its EIPs) before the controller is removed",
                "replayDeletion": True
            }
        }
        stamp(lbc_k8gb_usage, config)
        resource.update(rsp.desired.resources["usage-lbcontroller-k8gb"], lbc_k8gb_usage)

        # Each CoreDNS Elastic IP must outlive the k8gb Release: releasing an
        # EIP still associated with the live NLB fails. of: EIP, by: Release.
        for i in range(k8gb_eip_count):
            usage = {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "metadata": {
                    "name": f"{id_val}-usage-k8gb-eip-{i}-release",
                    "namespace": config["namespace"],
                    "annotations": {
                        "crossplane.io/composition-resource-name": f"usage-k8gb-eip-{i}-release"
                    }
                },
                "spec": {
                    "of": {
                        "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                        "kind": "EIP",
                        "resourceRef": {
                            "name": f"{id_val}-k8gb-eip-{i}",
                            "namespace": config["namespace"]
                        }
                    },
                    "by": {
                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                        "kind": "Release",
                        "resourceRef": {"name": f"{id_val}-k8gb"}
                    },
                    "reason": "CoreDNS Elastic IP must be released only after the k8gb Release (and its NLB) is gone",
                    "replayDeletion": True
                }
            }
            stamp(usage, config)
            resource.update(rsp.desired.resources[f"usage-k8gb-eip-{i}-release"], usage)

    if argocd_enabled:
        _emit_eks_usage(
            rsp, id_val, "usage-argocd-eks",
            "helm.m.crossplane.io/v1beta1", "Release",
            f"{id_val}-argocd",
            "ArgoCD Release must finish uninstalling before the EKS cluster is deleted",
            config)
        # Every child-cluster ArgoCD Object also guards the EKS cluster.
        for cr_name in ("argocd-issuer", "argocd-cert", "argocd-gateway",
                        "argocd-httproute", "argocd-app"):
            _emit_eks_usage(
                rsp, id_val, f"usage-{cr_name}-eks",
                "kubernetes.m.crossplane.io/v1alpha1", "Object",
                f"{id_val}-{cr_name}",
                f"ArgoCD {cr_name} Object must be removed before the EKS cluster is deleted",
                config)
