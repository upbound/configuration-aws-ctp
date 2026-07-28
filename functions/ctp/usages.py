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
                        argocd_enabled=False):
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
