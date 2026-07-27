"""08-vpa — Vertical Pod Autoscaler + metrics-server Helm Releases."""

from crossplane.function import resource

from .prelude import stamp


def add_vpa_resources(rsp, id_val, vpa, vpa_ready, config):
    vpa_release = {
        "apiVersion": "helm.m.crossplane.io/v1beta1",
        "kind": "Release",
        "metadata": {
            "name": f"{id_val}-vpa",
            "namespace": config["namespace"],
            "annotations": {
                "crossplane.io/composition-resource-name": "vpa-release"
            }
        },
        "spec": {
            "forProvider": {
                "chart": {
                    "name": "vpa",
                    "repository": "https://charts.fairwinds.com/stable",
                    # renovate: datasource=helm depName=vpa registryUrl=https://charts.fairwinds.com/stable
                    "version": "4.10.1"
                },
                "namespace": "kube-system",
                "skipCreateNamespace": False,
                "wait": True
            },
            "providerConfigRef": {
                "name": id_val,
                "kind": "ProviderConfig"
            }
        }
    }
    stamp(vpa_release, config)
    resource.update(rsp.desired.resources["vpa-release"], vpa_release)

    metrics_release = {
        "apiVersion": "helm.m.crossplane.io/v1beta1",
        "kind": "Release",
        "metadata": {
            "name": f"{id_val}-metrics-server",
            "namespace": config["namespace"],
            "annotations": {
                "crossplane.io/composition-resource-name": "metrics-server-release"
            }
        },
        "spec": {
            "forProvider": {
                "chart": {
                    "name": "metrics-server",
                    "repository": "https://kubernetes-sigs.github.io/metrics-server/",
                    # renovate: datasource=helm depName=metrics-server registryUrl=https://kubernetes-sigs.github.io/metrics-server/
                    "version": "3.12.2"
                },
                "namespace": "kube-system",
                "skipCreateNamespace": False,
                "wait": True
            },
            "providerConfigRef": {
                "name": id_val,
                "kind": "ProviderConfig"
            }
        }
    }
    stamp(metrics_release, config)
    resource.update(rsp.desired.resources["metrics-server-release"], metrics_release)
