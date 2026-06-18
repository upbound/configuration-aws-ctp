# configuration-aws-ctp

A Crossplane v2 [Configuration package](https://docs.upbound.io/manuals/marketplace/packages/)
that provisions **AWS EKS clusters configured as Upbound control planes**, with optional UXP
backup using `InjectedIdentity` (IRSA).

It exposes a single composite resource, `ControlPlane`
(`aws.platform.upbound.io/v1alpha1`), implemented with a Python composition function
(`functions/ctp`). One `ControlPlane` composes the full stack: VPC/networking, an EKS cluster
and managed node group, IRSA roles, a UXP (Universal Crossplane) installation, and — when
requested — UXP backup/restore, an enterprise license, Knative scale-to-zero, and provider
Vertical Pod Autoscaling.

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

Because the composed managed resources are namespaced Crossplane v2 MRs
(`*.aws.m.upbound.io`), a namespaced `ProviderConfig` must exist in the namespace where the
MRs live — see [`examples/controlplane/providerconfig-namespaced.yaml`](examples/controlplane/providerconfig-namespaced.yaml).

## Usage

Minimal example ([`examples/controlplane/basic.yaml`](examples/controlplane/basic.yaml)):

```yaml
apiVersion: aws.platform.upbound.io/v1alpha1
kind: ControlPlane
metadata:
  name: my-control-plane
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
| `version` | no | Kubernetes version (`1.31`–`1.35`, default `1.34`). |
| `providerConfigName` | no | ProviderConfig to use (default `default`). |
| `accessConfig` | no | EKS authentication mode and cluster-creator admin bootstrap. |
| `iam.principalArn` | no | Principal ARN to grant ClusterAdmin. |
| `uxp.version` | no | UXP Helm chart version (default `2.2.1-up.1`). |
| `backup` | no | UXP backup via IRSA — see below. |
| `license.secretRef` | no | Reference to a Secret holding the UXP enterprise license JSON. |
| `knative.enabled` | no | Install cert-manager + Knative Serving for scale-to-zero functions. |
| `providerVerticalPodAutoscaling` | no | Enable VPA for UXP providers (CPU/memory bounds). |
| `managementPolicies` | no | Crossplane management policies (default `["*"]`). |

### Backup (IRSA)

When `backup.enabled: "yes"`, the composition wires UXP backup to an S3 bucket using
InjectedIdentity (IRSA) — no static credentials. The bucket at `backup.location` is created if
it does not already exist and is **never deleted** by Crossplane. Set `backup.schedule` (named
shortcuts like `@daily`, 5-field cron, or `@every` durations) to create a `BackupSchedule`, and
`backup.installFrom` to restore an existing backup at initial provisioning. See
[`examples/controlplane/with-backup.yaml`](examples/controlplane/with-backup.yaml).

UXP enterprise features (`license`, `knative`, `providerVerticalPodAutoscaling`) require a UXP
license Secret on the management cluster — see the header of `with-backup.yaml` for how to
create it (`up uxp license apply <license.json>`).

## Testing

Composition (rendering) tests run offline:

```bash
up project build
up test run tests/*
```

End-to-end tests provision real AWS resources and require Upbound credentials:

```bash
up test run tests/* --e2e
```

In CI, e2e runs only on pull requests labeled `run-e2e-tests` (see `.github/workflows/e2e.yaml`).
