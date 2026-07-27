"""04b-k8gb — k8gb operator + CoreDNS producer (docs/gslb-dns-architecture.md).

Installs the k8gb operator and its CoreDNS on the child cluster, exposing
CoreDNS via an NLB serving UDP+TCP:53, and observes that Service so the XR can
surface the k8gb status contract (coreDNSEndpoint + delegationRecord) for the
FleetGslb aggregator to consume.

- Chart pinned to v0.20.0, which ships both the legacy `k8gb.absa.oss/v1beta1`
  Gslb CRD (via `installLegacyCrds: true`, the default) and the new
  `k8gb.io/v1beta1`, matching configuration-resilient-ctp's consumer - do not
  blindly track latest.
- `extdns.enabled: false`: this package is a producer only; the parent-side
  FleetGslb writes the NS delegation, not per-child external-dns.
- The Helm release name is pinned to `k8gb` (external-name) so its CoreDNS
  Service is `k8gb-coredns` in namespace `k8gb`, the name k8gb expects.
"""

from crossplane.function import resource

from .prelude import stamp


def add_k8gb_resources(rsp, id_val, k8gb_param, geo_tag, ext_geo_tags,
                       k8gb_deployed, config):
    dns_zone = k8gb_param.get("dnsZone", "")
    parent_zone = k8gb_param.get("parentZone", "")

    values = {
        "k8gb": {
            "deployCrds": True,
            "deployRbac": True,
            "clusterGeoTag": geo_tag,
            "extGslbClustersGeoTags": ext_geo_tags,
            "dnsZones": [
                {
                    "loadBalancedZone": dns_zone,
                    "parentZone": parent_zone
                }
            ],
            "edgeDNSServers": ["1.1.1.1"]
        },
        # Producer only — the parent (FleetGslb) writes the NS delegation.
        "extdns": {"enabled": False},
        "coredns": {
            "serviceType": "LoadBalancer",
            "service": {
                "annotations": {
                    # AWS Load Balancer Controller-managed NLB (Step 3), not the
                    # in-tree `nlb` value — the controller reliably serves UDP +
                    # mixed-protocol TCP_UDP:53, which the in-tree path does not.
                    "service.beta.kubernetes.io/aws-load-balancer-type": "external",
                    "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type": "instance",
                    "service.beta.kubernetes.io/aws-load-balancer-scheme": "internet-facing"
                }
            }
        }
    }

    release_annotations = {
        "crossplane.io/composition-resource-name": "k8gb-release",
        # Pin the Helm release name so CoreDNS is `k8gb-coredns` in ns `k8gb`.
        "crossplane.io/external-name": "k8gb"
    }
    if k8gb_deployed:
        release_annotations["crossplane.io/ready"] = "True"

    release = {
        "apiVersion": "helm.m.crossplane.io/v1beta1",
        "kind": "Release",
        "metadata": {
            "name": f"{id_val}-k8gb",
            "namespace": config["namespace"],
            "annotations": release_annotations
        },
        "spec": {
            "forProvider": {
                "chart": {
                    "name": "k8gb",
                    "repository": "https://www.k8gb.io",
                    # renovate: datasource=helm depName=k8gb registryUrl=https://www.k8gb.io
                    # Pinned: v0.20.0 ships both the legacy k8gb.absa.oss and new
                    # k8gb.io Gslb CRDs; the CRD version is the producer/consumer contract.
                    "version": "v0.20.0"
                },
                "namespace": "k8gb",
                "skipCreateNamespace": False,
                "wait": True,
                "values": values
            },
            "providerConfigRef": {
                "name": id_val,
                "kind": "ProviderConfig"
            }
        }
    }
    stamp(release, config)
    resource.update(rsp.desired.resources["k8gb-release"], release)

    # Observe-only Object on the child CoreDNS Service to read its LB endpoint.
    coredns_observe = {
        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
        "kind": "Object",
        "metadata": {
            "name": f"{id_val}-k8gb-coredns",
            "namespace": config["namespace"],
            "annotations": {
                "crossplane.io/composition-resource-name": "k8gb-coredns-observe"
            }
        },
        "spec": {
            "managementPolicies": ["Observe"],
            "forProvider": {
                "manifest": {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {
                        "name": "k8gb-coredns",
                        "namespace": "k8gb"
                    }
                }
            },
            "providerConfigRef": {
                "name": id_val,
                "kind": "ProviderConfig"
            }
        }
    }
    stamp(coredns_observe, config)
    resource.update(rsp.desired.resources["k8gb-coredns-observe"], coredns_observe)
