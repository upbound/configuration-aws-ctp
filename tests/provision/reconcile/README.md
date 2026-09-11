# reconcile

Reconciles every `controlplanes/*.yaml` whose `managementMode` is `Provision` or
`ObserveOnly`: created/imported/updated, then orphaned on teardown. Driven by
`.github/workflows/provision.yaml`; see `controlplanes/README.md` to run it
locally.

Not part of PR CI - it provisions real AWS infrastructure.
