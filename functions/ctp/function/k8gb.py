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
- In modes that push Updates, k8gb's zone ConfigMap gates readiness and an
  inert nudge Service re-runs k8gb's CoreDNS bootstrap while that zone is
  missing (#24).
"""

from crossplane.function import resource

from .prelude import build_k8gb_nudge_annotations, stamp


def add_k8gb_resources(rsp, id_val, k8gb_param, geo_tag, ext_geo_tags,
                       k8gb_deployed, region, provider_config, eip_count,
                       eip_alloc_ids, config, compose_nudge=False,
                       nudging=False, zone_gates_ready=False):
    dns_zone = k8gb_param.get("dnsZone", "")
    parent_zone = k8gb_param.get("parentZone", "")

    # One Elastic IP per public subnet, pinned on the CoreDNS NLB so its glue
    # is a stable IPv4 A record (docs/gslb-dns-architecture.md §9/§10). EIPs are
    # AWS MRs (management creds), unlike the child Helm Release below.
    for i in range(eip_count):
        eip = {
            "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
            "kind": "EIP",
            "metadata": {
                "name": f"{id_val}-k8gb-eip-{i}",
                "namespace": config["namespace"],
                "annotations": {
                    "crossplane.io/composition-resource-name": f"k8gb-eip-{i}"
                }
            },
            "spec": {
                "forProvider": {"domain": "vpc", "region": region},
                "providerConfigRef": {"name": provider_config, "kind": "ProviderConfig"}
            }
        }
        stamp(eip, config, aws_tags=True, resource_tag=f"k8gb-eip-{i}")
        resource.update(rsp.desired.resources[f"k8gb-eip-{i}"], eip)

    # Hold the Release (which creates the NLB) until every EIP is allocated, so
    # the NLB is created once already carrying its EIPs (adding EIPs to a live
    # dynamic-IP NLB forces a recreate). Same hold pattern as lbcontroller.py.
    eips_ready = eip_count > 0 and len(eip_alloc_ids) == eip_count

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
                    # in-tree `nlb` value, which cannot serve UDP and TCP on one port.
                    "service.beta.kubernetes.io/aws-load-balancer-type": "external",
                    "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type": "instance",
                    "service.beta.kubernetes.io/aws-load-balancer-scheme": "internet-facing",
                    # The Service has UDP and TCP on port 53. Without this the
                    # controller builds only one listener (UDP), so TCP:53 is
                    # refused; with it, one TCP_UDP listener (controller >= v2.13).
                    "service.beta.kubernetes.io/aws-load-balancer-enable-tcp-udp-listener": "true",
                    # Instance targets exist only in AZs with nodes; without
                    # cross-zone, the EIP of an AZ without a node answers nothing.
                    "service.beta.kubernetes.io/aws-load-balancer-attributes": "load_balancing.cross_zone.enabled=true"
                }
            }
        }
    }

    if eips_ready:
        # EIP list is positional vs the controller's tag-discovered public
        # subnets; order is immaterial (fresh vpc EIPs have no AZ affinity),
        # only the count must match or NLB creation fails.
        values["coredns"]["service"]["annotations"][
            "service.beta.kubernetes.io/aws-load-balancer-eip-allocations"
        ] = ",".join(eip_alloc_ids)

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
    if eips_ready:
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

    # Observe-only Object on k8gb's zone ConfigMap. The chart creates it empty
    # and k8gb fills it once its bootstrap resolves the CoreDNS IPs. In modes
    # that nudge, the XR stays non-Ready until k8gb has written its zone
    # (CoreDNS loads it on its next start, a few minutes later).
    zone_observe = {
        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
        "kind": "Object",
        "metadata": {
            "name": f"{id_val}-k8gb-zone-delegation",
            "namespace": config["namespace"],
            "annotations": {
                "crossplane.io/composition-resource-name": "k8gb-zone-delegation-observe"
            }
        },
        "spec": {
            "managementPolicies": ["Observe"],
            "forProvider": {
                "manifest": {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": "k8gb-zone-delegation",
                        "namespace": "k8gb"
                    }
                }
            },
            # SuccessfulCreate is the provider default, explicit so tests can pin it.
            "readiness": {
                "policy": "DeriveFromCelQuery",
                "celQuery": "has(object.data) && size(object.data) > 0"
            } if zone_gates_ready else {"policy": "SuccessfulCreate"},
            "providerConfigRef": {
                "name": id_val,
                "kind": "ProviderConfig"
            }
        }
    }
    stamp(zone_observe, config)
    resource.update(rsp.desired.resources["k8gb-zone-delegation-observe"], zone_observe)

    if compose_nudge:
        _add_bootstrap_nudge(rsp, id_val, nudging, config)


def _add_bootstrap_nudge(rsp, id_val, nudging, config):
    """Inert Service whose updates re-run k8gb's CoreDNS bootstrap reconciler.

    k8gb v0.20.0 digs the CoreDNS NLB hostname once and stops without a requeue
    when it does not resolve yet, so the zone stays unwritten until the ~10h
    informer resync (#24). That reconciler re-runs on an update to any Service
    labelled app.kubernetes.io/name=coredns, cluster-wide, but reads the
    CoreDNS Service only from ns k8gb, so this one lives in `default` (always
    present) and is never mistaken for it. Drop it once upstream requeues.
    """
    annotations = build_k8gb_nudge_annotations(nudging, config["reconcile_time"])

    nudge = {
        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
        "kind": "Object",
        "metadata": {
            "name": f"{id_val}-k8gb-bootstrap-nudge",
            "namespace": config["namespace"],
            "annotations": {
                "crossplane.io/composition-resource-name": "k8gb-bootstrap-nudge"
            }
        },
        "spec": {
            "forProvider": {
                "manifest": {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {
                        "name": "k8gb-bootstrap-nudge",
                        "namespace": "default",
                        "labels": {"app.kubernetes.io/name": "coredns"},
                        "annotations": annotations
                    },
                    # Headless and selectorless: no ClusterIP, no endpoints,
                    # no traffic. Headless is what lets ports be omitted.
                    "spec": {"clusterIP": "None"}
                }
            },
            "providerConfigRef": {
                "name": id_val,
                "kind": "ProviderConfig"
            }
        }
    }
    stamp(nudge, config)
    resource.update(rsp.desired.resources["k8gb-bootstrap-nudge"], nudge)
