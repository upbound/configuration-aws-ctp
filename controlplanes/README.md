# Provisioned control planes

Each `*.yaml` here is one persistent AWS EKS+UXP control plane, declared as a
`ControlPlane` XR (the desired state) - just the XR, no `E2ETest` boilerplate and
no credentials.

Each file's `spec.parameters.managementMode` decides its lifecycle. The
provisioning pipeline makes two passes over this folder, keying on the **explicit**
mode (a file without a `managementMode` is ignored by both):

- **reconcile** - control planes set to `Provision` or `ObserveOnly`:
  created/adopted/updated, then orphaned on teardown.
- **decommission** - control planes set to `Deprovision`: adopted, then deleted
  (AWS torn down).

`tests/provision/reconcile` and `tests/provision/decommission` (Python E2E tests)
each load this folder and keep only their subset. The credential comes from
`UP_CLOUD_CREDENTIALS`, which `up test` injects into the test container.

## Rules

- Every `ControlPlane` file here must set `metadata.namespace: default`, matching
  the pipeline's ProviderConfig namespace - a namespaced ProviderConfig resolves
  credentials from its own namespace. The pipeline runs on an ephemeral bootstrap
  cluster where `default` always exists, so it never depends on a pre-created
  namespace (these static files can't create one).
- `parameters.id` is the identity key - short, lowercase, alphanumeric, stable. It
  is stamped as the `upbound.io/ctp-id` tag on every composed AWS resource, and it
  drives the names of the resources this configuration owns (`{id}-uxp`,
  `{id}-backup-irsa`, `{id}-k8gb-eip-N`). Changing it provisions a new control
  plane instead of adopting the existing one.
- `parameters.managementMode` (required here): `Provision` (create + adopt +
  update, never delete - the steady state for a persistent control plane),
  `ObserveOnly` (adopt + watch, no changes), `Deprovision` (decommission - see
  below). Omitting it defaults to `Full` at the XRD (standard lifecycle, not acted
  on by the pipeline), so always set an explicit mode here.
- Immutable EKS fields (`nodes.instanceType`) reprovision via the backup +
  `installFrom` path, not in place.

## AWS caveat: cross-run adoption is not free

On Azure every managed resource has a deterministic name and adoption is
automatic. On AWS most identifiers are assigned by the cloud, so a fresh
bootstrap cluster holds no way to find the resources it created last time.

The adopt path closes this by tagging every owned resource with the control
plane's identity and querying those tags to inject `crossplane.io/external-name`
before Crossplane reconciles (see the "Adoption on AWS" section of the top-level
README, and
`docs/superpowers/specs/2026-09-01-aws-ctp-dynamic-provisioning-design.md`). It
is **opt-in**: a control plane must select the adopt Composition and supply both
filter lists.

```yaml
spec:
  crossplane:
    compositionRef:
      name: controlplane-adopt.aws.platform.upbound.io
  parameters:
    adopt:
      tagFilters:
      - name: upbound.io/ctp-id
        values: ["<id>"]
      ec2Filters:
      - name: tag:upbound.io/ctp-id
        values: ["<id>"]
```

It also needs an `aws-creds` Secret in `default` - the query steps read
credentials from a static block in the Composition, which is why they live in a
separate Composition rather than behind a flag on the default one.

Without that opt-in, this folder supports:

- **create** - a control plane that does not exist yet, and
- **same-run update** - changes applied while that run's bootstrap is alive.

and does **not** support:

- **update across runs** - a second run creates a second, parallel stack rather
  than updating the first.
- **`Deprovision` across runs** - the pass would create a whole new control plane,
  wait for it, delete *that*, and report success, leaving the original running and
  orphaned.

Treat a control plane provisioned without the identity tags as create-only:
tagging is what makes it adoptable, and it cannot be applied retroactively by
this configuration.

> **Status.** Last measured full cycle adopted 23 of 31 resources with zero
> duplicates. The 8 that did not were 3 private subnets, 3 route-table
> associations and 2 security-group rules; the subnets and associations are what
> the `ec2Filters` queries above address. That has passed composition tests but
> has **not yet been re-run against real AWS**, so treat cross-run
> `Provision`/`Deprovision` as unverified rather than working.

## Add a control plane

    cp controlplanes/cp1.yaml controlplanes/<name>.yaml
    # edit metadata.name, a unique id, region, nodes, add-ons; set managementMode.

## Run locally

Running e2e is a manual, owner-driven step (real AWS). Export the base64
shared-credentials INI as `UP_CLOUD_CREDENTIALS` (what `up test` forwards into the
container), then run the pass you want:

    export UP_CLOUD_CREDENTIALS="$(base64 < ~/.aws/credentials)"
    up test run tests/provision/reconcile    --e2e --local   # Provision/ObserveOnly
    up test run tests/provision/decommission --e2e --local --skip-control-plane-cleanup

## Decommission (`managementMode: Deprovision`)

Set a control plane's `managementMode` to `Deprovision`, then run the decommission
pass. `up test`'s delete phase returns as soon as the composite XR is
background-collected (< 1s), long before the 20-30 min AWS cascade (add-ons and
releases -> EKS node group -> EKS cluster -> network) finishes, so keep KIND alive
with `--skip-control-plane-cleanup` and wait until it drains:

    up test run tests/provision/decommission --e2e --local --skip-control-plane-cleanup
    kubectl get managed -A   # repeat until empty

`.github/workflows/provision.yaml` does this automatically: it polls the
kept-alive KIND until no managed resources remain (~45-min ceiling).

## After a decommission: delete the file

Optionally confirm deletion on the AWS side first:

    aws eks describe-cluster --name <cluster-name>   # want: ResourceNotFoundException

Then **delete the control plane's file**:

    git rm controlplanes/<name>.yaml

A file left at `Deprovision` is **not inert**. The policy keeps `Create`, so the
next dispatch re-creates the entire control plane, waits for `Ready`, and destroys
it again - 40-60 min of real AWS spend, reported as success.

`Create` cannot be dropped to prevent this. Crossplane defines no Delete-capable
managementPolicies combination that omits `Create` - a delete-without-create policy
is not a supported
[combination](https://docs.crossplane.io/latest/managed-resources/managed-resources/#managementpolicies).
It is also the adopt path. Removing the file is the only safeguard.
