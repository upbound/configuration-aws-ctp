"""E2E test: one real EKS ControlPlane, full stack, asserts Ready=True.

Spins up a real EKS cluster via the composition and exercises the whole stack in
one run: UXP + IRSA-based S3 backup + the k8gb producer (operator + CoreDNS via a
UDP+TCP NLB) + ArgoCD (UI Gateway/HTTPRoute + app-of-apps), which also pulls in
cert-manager (always-on), the Envoy Gateway data plane, and the AWS Load Balancer
Controller (EKS Pod Identity).

Credentials: the AWS ProviderConfig "default", namespaced in the ControlPlane's
namespace (platform), uses Upbound-injected identity (source: Upbound) federated
to the solutions e2e AWS role, so no pre-provisioned Secret is required. The OIDC
subject up test presents embeds this test's name
(mcp:<org>/configuration-aws-ctp-uptest-controlplane:provider:provider-aws); the
role trust must allow it, so keep the name `controlplane`.

Asserts the ControlPlane XR reaches Ready=True; function-auto-ready aggregates
every composed resource, so Ready implies UXP + backup chain + IRSA + every
add-on came up. Installation only - it does NOT test DNS failover. Requires AWS
quota for a three-node t3.medium EKS cluster plus the add-on load balancers.
Expected runtime: 40-70 minutes.
"""

import yaml
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as k8s
from models.io.upbound.dev.meta.e2etest import v1alpha1 as e2etest

test = e2etest.E2ETest(
    metadata=k8s.ObjectMeta(name="controlplane"),
    spec=e2etest.Spec(
        crossplane=e2etest.Crossplane(
            autoUpgrade=e2etest.AutoUpgrade(channel="Stable"),
        ),
        defaultConditions=["Ready"],
        timeoutSeconds=5400,
        cleanupTimeoutSeconds=1800,
        extraResources=[
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": "platform"},
            },
            {
                "apiVersion": "aws.m.upbound.io/v1beta1",
                "kind": "ProviderConfig",
                "metadata": {"name": "default", "namespace": "platform"},
                "spec": {
                    "credentials": {
                        "source": "Upbound",
                        "upbound": {
                            "webIdentity": {
                                "roleARN": "arn:aws:iam::609897127049:role/solutions-e2e-provider-aws",
                            },
                        },
                    },
                },
            },
        ],
        manifests=[
            {
                "apiVersion": "aws.platform.upbound.io/v1alpha1",
                "kind": "ControlPlane",
                "metadata": {"name": "e2e-test-cp", "namespace": "platform"},
                "spec": {
                    "parameters": {
                        "id": "e2e-test-cp",
                        "region": "us-east-1",
                        "version": "1.34",
                        "nodes": {"count": 3, "instanceType": "t3.medium"},
                        "backup": {
                            "enabled": "yes",
                            "location": "arn:aws:s3:::upbound-e2e-test-cp-backup",
                        },
                        "k8gb": {
                            "enabled": "yes",
                            "dnsZone": "gslb.example.com",
                            "parentZone": "example.com",
                            "strategy": "failover",
                        },
                        "argocd": {
                            "enabled": "yes",
                            "hostname": "argocd.example.com",
                            "url": "https://github.com/argoproj/argocd-example-apps",
                        },
                    },
                },
            },
        ],
        skipDelete=False,
    ),
)

# The test runner expects an "items" array, one entry per test.
item = test.model_dump(by_alias=True, exclude_none=True)
# Strip the two model-default fields the retired test.yaml never carried, so the
# emitted E2ETest is field-for-field identical to it (both equal the platform
# defaults; yaml.dump sorts keys, so only byte order differs):
#   spec.crossplane.state == "Running", spec.setupTimeoutSeconds == 600
item["spec"]["crossplane"].pop("state", None)
item["spec"].pop("setupTimeoutSeconds", None)
print(yaml.dump({"items": [item]}))
