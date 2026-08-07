# AWS EKS Control Plane composition function

The Python composition function for `configuration-aws-ctp`. Entrypoint:
`function.main:cli`; composition logic in `function/fn.py` (`compose`), with one
sibling module per composed section (network, eks, uxp, backup, k8gb, argo, ...).
