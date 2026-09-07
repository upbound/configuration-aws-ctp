# configuration-aws-ctp

A Crossplane v2 [Configuration package](https://docs.upbound.io/manuals/marketplace/packages/)
that provisions **AWS EKS clusters configured as Upbound control planes**, with optional UXP
backup using `InjectedIdentity` (IRSA).

It exposes a single composite resource, `ControlPlane`
(`aws.platform.upbound.io/v1alpha1`), implemented with a Python composition function
(`functions/ctp`). One `ControlPlane` composes the full stack: VPC/networking, an EKS cluster
and managed node group, IRSA roles, a UXP (Universal Crossplane) installation, and — when
requested — UXP backup/restore, an enterprise license, Knative scale-to-zero, provider
Vertical Pod Autoscaling, k8gb global failover, and ArgoCD. cert-manager is always installed.

## Installation

Add it as a dependency of an existing project:

```bash
up dependency add xpkg.upbound.io/upbound/configuration-aws-ctp
```

Or install it onto a control plane directly:

```yaml
apiVersion: pkg.crossplane.io/v1
kind: Configuration
metadata:
  name: configuration-aws-ctp
spec:
  package: xpkg.upbound.io/upbound/configuration-aws-ctp:v0.1.0
```

This configuration depends on `configuration-aws-eks`, `function-extra-resources`,
`function-auto-ready`, and `provider-aws-s3` (see [`upbound.yaml`](upbound.yaml)).

### The ControlPlane is namespaced

The `ControlPlane` XR is **namespaced** (`scope: Namespaced`). Every resource it
manages - the credentials Secret, the AWS `ProviderConfig`, the composed managed
resources (`*.aws.m.upbound.io`), and the connection secrets the composition
writes (the EKS cluster-admin kubeconfig and the XR connection secret) - lives in
the XR's namespace. Choose an operator-managed, RBAC-restricted namespace to keep
cluster-admin kubeconfigs and cloud credentials out of `default`, and create it
first (Crossplane does not create it):

```bash
kubectl apply -f examples/install/namespace.yaml   # namespace: platform
```

Applying a `ControlPlane` with no namespace falls back to `default`. Resources
installed on the inner EKS cluster (UXP in `crossplane-system`, cert-manager,
add-ons, etc.) keep their own fixed namespaces regardless of the XR's namespace.

The providers authenticate with a namespaced AWS `ProviderConfig` that must live
in the **same namespace as the ControlPlane XR** (a namespaced ProviderConfig
reads its credentials from its own namespace, and the composed managed resources
resolve the ProviderConfig by name within their own namespace). See
[`examples/install/providerconfig-namespaced.yaml`](examples/install/providerconfig-namespaced.yaml).
Apply the namespace, then the ProviderConfig into that same namespace, then apply
`ControlPlane` XRs into it.

## Usage

Minimal example ([`examples/controlplane/basic.yaml`](examples/controlplane/basic.yaml)):

```yaml
apiVersion: aws.platform.upbound.io/v1alpha1
kind: ControlPlane
metadata:
  name: my-control-plane
  namespace: platform
spec:
  parameters:
    id: my-control-plane
    region: us-west-2
    version: "1.34"
    nodes:
      count: 3
      instanceType: t3.medium
    accessConfig:
      authenticationMode: API_AND_CONFIG_MAP
      bootstrapClusterCreatorAdminPermissions: true
```

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `id` | yes | Identifier other objects use to refer to this control plane. |
| `region` | yes | AWS region. |
| `nodes` | yes | EKS node group config (`count`, `instanceType`, default `t3.small`). |
| `network` | no | VPC topology (`vpcCidrBlock`, `subnets`). Defaults to a resilient three-AZ layout across `<region>a/b/c`. |
| `version` | no | Kubernetes version (`1.31`–`1.35`, default `1.34`). |
| `providerConfigName` | no | ProviderConfig to use (default `default`). |
| `accessConfig` | no | EKS authentication mode and cluster-creator admin bootstrap. |
| `iam.principalArn` | no | Principal ARN to grant ClusterAdmin. |
| `uxp.version` | no | UXP Helm chart version (default `2.2.1-up.1`). |
| `backup` | no | UXP backup via IRSA — see below. |
| `license.secretRef` | no | Reference to a Secret holding the UXP enterprise license JSON. |
| `knative.enabled` | no | Install Knative Serving for scale-to-zero functions (cert-manager is always installed). |
| `k8gb` | no | Enable k8gb global failover — see below. |
| `argocd` | no | Enable ArgoCD (GitOps app-of-apps) — see below. |
| `providerVerticalPodAutoscaling` | no | Enable VPA for UXP providers (CPU/memory bounds). |
| `managementPolicies` | no | Crossplane management policies. No schema default: when set it wins over `managementMode`. |
| `managementMode` | no | Lifecycle: `Full` (default, standard), `Provision` (create/adopt/update, never delete), `ObserveOnly` (watch only), `Deprovision` (adopt and delete). See below. |
| `naming` | no | How the composed EKS resources are named, forwarded to the EKS XR: `Generated` (default) or `Deterministic` (names derived from `id`, so the cluster, its three IAM roles and the node group are adoptable by name with no AWS query). **Destructive to change on a live control plane** - Crossplane cannot rename a composed resource, so it deletes the EKS cluster and builds a replacement. |

> **cert-manager** is installed unconditionally on every control plane (a free
> dependency of Knative/k8gb/ArgoCD Gateway TLS). **Envoy Gateway (Gateway API)**
> is installed only when `k8gb` or `argocd` is enabled, so plain control planes
> do not pay for an idle cloud load balancer. The community `ingress-nginx` this
> replaced was retired 2026-03-24.

### k8gb (global failover)

When `k8gb.enabled: "yes"`, the control plane becomes a **producer** in the fleet
GSLB architecture ([`docs/gslb-dns-architecture.md`](docs/gslb-dns-architecture.md)):
it installs the AWS Load Balancer Controller (via EKS Pod Identity), the k8gb
operator, and CoreDNS exposed through an NLB serving UDP+TCP:53, and surfaces
`status.controlplane.k8gb.coreDNSEndpoint`, `nsName`, `glueAddresses`, and
`delegationRecord` for the parent-side FleetGslb aggregator. Parameters:
`dnsZone` (load-balanced zone), `parentZone`,
`clusterGeoTag` (defaults to `aws-<region>-<id>`), and `strategy`
(`failover`/`roundRobin`/`geoip`). See
[`examples/controlplane/with-k8gb.yaml`](examples/controlplane/with-k8gb.yaml).
GSLB is not yet functional end-to-end — nothing writes the NS delegation until
FleetGslb exists.

### ArgoCD

When `argocd.enabled: "yes"`, ArgoCD is installed with a UI exposed via Envoy
Gateway (`argocd.hostname`, a Gateway + HTTPRoute and a self-signed cert-manager
Certificate) and a root app-of-apps `Application` pointing at the public git
repo `argocd.url`. See
[`examples/controlplane/with-argocd.yaml`](examples/controlplane/with-argocd.yaml).

### Backup (IRSA)

When `backup.enabled: "yes"`, the composition wires UXP backup to an S3 bucket using
InjectedIdentity (IRSA) — no static credentials. The bucket at `backup.location` is created if
it does not already exist and is **never deleted** by Crossplane. Set `backup.schedule` (named
shortcuts like `@daily`, 5-field cron, or `@every` durations) to create a `BackupSchedule`, and
`backup.installFrom` to restore an existing backup at initial provisioning. See
[`examples/controlplane/with-backup.yaml`](examples/controlplane/with-backup.yaml).

By default the backup bucket is created in the control plane's `region`. Set
`backup.bucketRegion` to a **different** region to keep backups off the cluster's region, so a
full regional outage does not take out both the control plane and its backups — the basis for
cross-region disaster recovery. The observe-only EKS cluster and all other resources stay in the
control plane `region`; only the S3 bucket and the UXP `BackupConfig`/`Restore` storage client
use `bucketRegion`.

UXP enterprise features (`license`, `knative`, `providerVerticalPodAutoscaling`) require a UXP
license Secret on the management cluster — see the header of `with-backup.yaml` for how to
create it (`up uxp license apply <license.json>`).

## Testing

Composition (rendering) tests run offline:

```bash
up project build
up test run tests/*
```

End-to-end tests provision real AWS resources and require Upbound credentials.
They also require a working AWS `ProviderConfig` named `default`, namespaced in
the same namespace as the `ControlPlane` under test (the e2e uses `platform`),
with permissions to create VPCs, EKS clusters, IAM roles, and S3 buckets:

```bash
up test run tests/* --e2e
```

In CI, e2e runs only on pull requests labeled `run-e2e-tests` (see `.github/workflows/e2e.yaml`).

## Managed Resource Activation Policy

This configuration includes a `ManagedResourceActivationPolicy` (MRAP) that enables only the required CRDs from dependent providers. If you're running Crossplane without a default activation policy, this ensures that only the necessary CRDs are activated, reducing resource overhead and improving control plane performance.

To view the MRAP:
```bash
kubectl get managedresourceactivationpolicy configuration-aws-ctp -o yaml
```

## Dynamic control-plane provisioning

`controlplanes/*.yaml` declares persistent EKS+UXP control planes as
`ControlPlane` XRs. `.github/workflows/provision.yaml` (manual dispatch) stands
up a disposable local KIND bootstrap and reconciles or decommissions them
according to each file's `managementMode`. See `controlplanes/README.md`.

Two layers are involved and must not be conflated:

| Layer | What it is | Lifecycle |
|---|---|---|
| Management (bootstrap) | Local KIND + UXP running this package | Ephemeral: created and destroyed each run |
| Provisioned (product) | The EKS cluster with UXP + add-ons installed on it | Persistent: created once, adopted and updated thereafter |

### Adoption on AWS

On Azure a stateless bootstrap re-adopts by deterministic name. AWS assigns most
resource identifiers itself, so adoption has to be manufactured: every AWS
resource this configuration owns is tagged `upbound.io/ctp-id: <id>` and
`upbound.io/ctp-resource: <logical name>`, and the adopt path queries those tags
to inject `crossplane.io/external-name` before Crossplane reconciles.

**A resource provisioned without those tags is not adoptable.** Tag before you
provision anything you intend to keep. See
`docs/superpowers/specs/2026-09-01-aws-ctp-dynamic-provisioning-design.md`.

Discovery uses two sources, because the tag query alone is not sufficient:

| Source | Finds | Why both |
|---|---|---|
| Resource Groups Tagging API (`adopt.tagFilters`) | Every taggable resource in one server-side call | Broad, but it keeps returning deleted resources under the same tag, which makes a logical name ambiguous |
| `ec2:DescribeSubnets` / `ec2:DescribeRouteTables` (`adopt.ec2Filters`) | Live subnets, and route-table associations | Returns only live resources, so it settles that ambiguity; associations carry no tags and the Tagging API does not index them at all |

Ambiguity is never guessed: where a logical name still has more than one
candidate, nothing is injected and Crossplane creates instead. A duplicate is
bad, but adopting a dead identifier is the same duplicate plus a confusing
error.

Both filter lists duplicate `id` on purpose - `function-aws-query` resolves them
from a field path and cannot template a scalar into a `{name, values}` list. They
are not interchangeable: `tagFilters` takes a bare tag key, `ec2Filters` takes
native EC2 filter names, so the same filter is `upbound.io/ctp-id` in one and
`tag:upbound.io/ctp-id` in the other.

```yaml
spec:
  crossplane:
    compositionRef:
      name: controlplane-adopt.aws.platform.upbound.io
  parameters:
    id: awsctpcp1
    managementMode: Provision
    adopt:
      tagFilters:
      - name: upbound.io/ctp-id
        values: ["awsctpcp1"]
      ec2Filters:
      - name: tag:upbound.io/ctp-id
        values: ["awsctpcp1"]
```

The two database `SecurityGroupRule`s are the one exception: their external-name
is a Terraform-computed `sgrule-<crc32>` hash rather than an AWS identifier, so
no query can return it and the adopt path computes it from the discovered
security-group id instead.
