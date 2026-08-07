# ControlPlane E2E test

Provisions one real EKS ControlPlane and asserts Ready=True (UXP + IRSA backup +
k8gb producer + ArgoCD + cert-manager + Envoy Gateway + AWS LB Controller). Uses
Upbound-injected identity; installation-only. Run with
`up test run tests/e2etest-controlplane --e2e`.
