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

The adopt path closes this by tagging every owned resource and querying those
tags to inject `crossplane.io/external-name` before Crossplane reconciles (see
"Adoption on AWS" in the top-level README). Selecting the Composition is the
whole opt-in - the filters are derived from `id`:

```yaml
spec:
  crossplane:
    compositionRef:
      name: controlplane-adopt.aws.platform.upbound.io
  parameters:
    id: <id>
```

It also needs an `aws-creds` Secret in `default`, which both provision suites
create. The query steps read it from a static block in the Composition, which is
why adopt is a separate Composition rather than a flag.

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

> **Status.** A real-AWS run (2026-09-07) adopted 31/31 resources with no
> duplicates, so cross-run `Provision` is exercised. Cross-run `Deprovision`
> leaked a VPC until `configuration-aws-network` v2.2.0 dropped its
> `MainRouteTableAssociation`; this configuration now pulls that in via
> `configuration-aws-eks` v2.2.0. The leak is structurally gone but has **not
> been re-verified on real AWS**. See the migration note below if you have a
> control plane provisioned before that version.

## Migration: control planes provisioned before network v2.2.0

`configuration-aws-network` used to compose a `MainRouteTableAssociation` (`mrt`)
that made the composed route table the VPC's main one. It could not be adopted -
it deletes by restoring `original_route_table_id`, which AWS never returns - so a
stateless re-apply recorded the composed table as its own "original", and its
delete left that table still main. Deleting a route table disassociates every
association including the main one, which AWS refuses, so `rt` and the VPC leaked.

v2.2.0 dropped the resource, and this configuration pulls it in via
`configuration-aws-eks` v2.2.0. **New control planes are unaffected.**

A control plane provisioned before that version still has the association live in
AWS, and nothing will delete it - Crossplane does not manage what is no longer
composed. Its decommission still stalls with `rt` stuck on
`InvalidParameterValue: cannot disassociate the main route table association` and
the VPC behind it on `DependencyViolation`. Point the main association back at the
VPC's default route table once, and the reconcile continues:

```bash
VPC=vpc-...        # the stuck VPC
DEFAULT=$(aws ec2 describe-route-tables \
  --filters "Name=vpc-id,Values=$VPC" \
  --query 'RouteTables[?Associations[?Main==`true`]] | [0].RouteTableId' --output text)
ASSOC=$(aws ec2 describe-route-tables \
  --filters "Name=vpc-id,Values=$VPC" \
  --query 'RouteTables[].Associations[?Main==`true`].RouteTableAssociationId | [0][0]' \
  --output text)
aws ec2 replace-route-table-association \
  --association-id "$ASSOC" --route-table-id "$DEFAULT"
```

If the runner is already gone, delete `rt` and the VPC by hand.

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
