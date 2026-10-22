# k8gb bootstrap nudge composition tests

Renders `apis/ctp/composition.yaml` with k8gb enabled and asserts the zone observe Object,
the nudge Service Object and their Usage guards per `managementMode` (#24). One entry per
case in `test/_cases.py`; `test/__main__.py` wraps each in a CompositionTest and prints the
`items` array. Run with `up test run tests/test-k8gb-nudge`.
