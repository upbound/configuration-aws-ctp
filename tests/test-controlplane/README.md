# ControlPlane composition tests

Renders `apis/ctp/composition.yaml` against ControlPlane XR fixtures and asserts the
composed resources. One entry per case in `test/_cases.py`; `test/__main__.py`
wraps each in a CompositionTest and prints the `items` array. Run with
`up test run tests/test-controlplane`.
