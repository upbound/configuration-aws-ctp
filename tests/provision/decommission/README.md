# decommission

Imports and deletes every `controlplanes/*.yaml` whose `managementMode` is
`Deprovision`, cascading the AWS teardown. Run with
`--skip-control-plane-cleanup` and poll until `kubectl get managed -A` is empty;
see `controlplanes/README.md`.

Not part of PR CI - it destroys real AWS infrastructure.
