# ctp add-ons implementation plan (aws-ctp)

- **Date:** 2026-07-16
- **Status:** Ready for implementation. **Step 2 (nginx-ingress) is superseded**
  by [`docs/superpowers/specs/2026-07-21-gateway-api-migration-design.md`](superpowers/specs/2026-07-21-gateway-api-migration-design.md) -
  the community `ingress-nginx` was retired 2026-03-24 and replaced with Envoy
  Gateway (Kubernetes Gateway API), gated `k8gb OR argocd` instead of always-on.
  k8gb is now pinned to v0.20.0 (was v0.15.0 when this plan was written).
- **Scope:** `configuration-aws-ctp` only (azure/gcp ported later). One PR, one
  commit per step, composition tests per step, a single installation e2e at the
  end.
- **Context / why:** see [`gslb-dns-architecture.md`](gslb-dns-architecture.md).
  This PR implements the **producer** role from that doc: install the add-ons on
  the child cluster and surface the k8gb status contract. The FleetGslb
  aggregator and resilient-ctp `Gslb` ownership are separate workstreams. After
  this PR **GSLB is not yet functional end-to-end** - nothing writes the NS
  delegation until FleetGslb exists.

## Goal of this PR

Extend the `ControlPlane` composition so a child cluster gets:
- **cert-manager** and **nginx-ingress** installed **unconditionally** (always-on),
- the **AWS Load Balancer Controller** (prerequisite for the CoreDNS NLB and
  stable app LB IPs) when `k8gb.enabled`,
- **k8gb** (operator + CoreDNS via a UDP+TCP NLB) installed when `k8gb.enabled`,
- **ArgoCD** (+ UI Ingress + a root app-of-apps `Application`) when `argocd.enabled`,
- the **status contract** `status.controlplane.k8gb.coreDNSEndpoint`, `nsName`,
  `glueAddresses`, `delegationRecord` surfaced for the FleetGslb aggregator to consume.

## Repo orientation (for fresh context)

- Composition function lives in `functions/ctp/` (Python). Entry point
  `function/fn.py::compose(req, rsp)`; ordered sibling modules under `function/`: `prelude.py` (helpers),
  `network.py`, `eks.py`, `uxp.py`, `usages.py`, `backup.py`, `irsa.py`,
  `licensing.py`, `vpa.py`, `knative.py`, `runtime_config.py`, `status.py`.
- XRD: `apis/ctp/definition.yaml`. Composition: `apis/ctp/composition.yaml`.
- **Existing add-on pattern to copy (knative):**
  - Param under `spec.parameters.<feature>.enabled` (`"yes"`/`"no"`).
  - A module `add_<feature>_resources(...)` invoked conditionally from `main.py`.
  - Helm installs use `helm.m.crossplane.io/v1beta1` `Release`; raw manifests use
    `kubernetes.m.crossplane.io/v1alpha1` `Object`.
  - **Child-cluster** resources use `providerConfigRef: {name: <id>, kind: ProviderConfig}`
    (the `id` param); **AWS** MRs use `{name: <providerConfigName>, kind: ProviderConfig}`.
  - **provider-helm v1.2.2 stale-Ready workaround:** stamp
    `metadata.annotations["crossplane.io/ready"] = "True"` once the release
    reports `status.atProvider.state == "deployed"` (see `is_release_deployed`
    in `prelude.py` and how `uxp.py`/`knative.py` use it).
  - Every resource is passed through `prelude.stamp(...)` (last-reconcile-date annotation).
- **Tests:** composition tests in `tests/test-controlplane/` (python embedded test
  package; `kind: CompositionTest` with `assertResources`, cases in `test/_cases.py`). e2e in
  `tests/e2etest-controlplane/`.
- **Verify:** `up project build` then `up test run tests/*` (offline). e2e:
  `up test run tests/* --e2e` (CI runs it only on the `run-e2e-tests` label).
- **Skills (mandated for this repo):** author Python composition code via the
  `control-plane-project:author-composition-python` skill; verify via
  `control-plane-project:verify-configuration`; run e2e via
  `control-plane-project:e2e-test-configuration`.

## Locked decisions

- cert-manager: **always installed** (not gated). Free component; no license gate.
- nginx-ingress: **always installed** (not gated). *Caveat: this provisions an
  idle cloud LB on baseline control planes that create no Ingress (knative uses
  its own networking, not nginx). If that cost matters, gate behind
  `k8gb.enabled OR argocd.enabled` instead - open for confirmation.*
- **AWS Load Balancer Controller: required on the child before the CoreDNS LB can
  serve DNS.** On EKS, a `type: LoadBalancer` Service defaults to a Classic ELB,
  which cannot do UDP; CoreDNS needs **UDP:53** (and TCP:53), i.e. an NLB with a
  `TCP_UDP` listener, which reliably needs this controller (the legacy in-tree
  path defaults to a Classic ELB and does not reliably do UDP/mixed-protocol).
  Installed **in aws-ctp as a helm `Release` + EKS Pod Identity**, when
  `k8gb.enabled` (see Step 3).
- k8gb: gated by `k8gb.enabled`; **CoreDNS exposed via an NLB serving both UDP:53
  and TCP:53**, **`extdns.enabled: false`** (no external-dns). **Pin the k8gb
  chart to a version whose `Gslb` CRD matches resilient-ctp's consumer
  (`k8gb.absa.oss/v1beta1`; resilient-ctp installs v0.15.0) - do NOT blindly track
  newest**, or the producer/consumer CRD contract drifts. Renovate with a
  constraint, not latest. Reuse resilient-ctp's k8gb **operator values shape**
  (`dnsZones`, `clusterGeoTag`, `extGslbClustersGeoTags`, `edgeDNSServers`,
  `deployCrds/deployRbac`) but **not** its hostNetwork nginx.
- ArgoCD: gated by `argocd.enabled`; params `argocd.hostname` (UI Ingress host)
  and `argocd.url` (public git repo for the root app-of-apps).
- **Teardown: every new child-cluster `Release`/`Object` gets an
  `of: EKS, by: <resource>` `Usage` guard.** Child Objects orphan-finalize if the
  EKS cluster/kubeconfig is deleted first (known deletion-ordering gap; only
  Release->EKS and EKS->Network guards exist today). Applies to the LB controller,
  k8gb, the CoreDNS observe Object, and every argocd Object - not just the k8gb
  Release. Alternatively, land the standalone child-cluster deletion-guards fix
  before this PR.
- No e2e per step; **one** installation e2e at the end, behind `run-e2e-tests`.

## Step 1 - cert-manager decouple (always-on refactor)

- Create `functions/ctp/certmanager.py` with `add_certmanager_resources(rsp, id_val, config)`
  that emits the cert-manager `Release` currently built inside `knative.py`
  (chart `cert-manager` from `https://charts.jetstack.io`, `crds.enabled: true`,
  `wait: true`, stale-Ready workaround). Use a stable resource name, e.g.
  `certmanager-release`.
- In `main.py`: call `add_certmanager_resources(...)` **unconditionally** (drops
  both the `knative.enabled` and the `features_licensed` gates it lives behind
  today); update the readiness read
  `certmanager_ready = is_release_deployed(observed_resources, "certmanager-release")`
  (was `"knative-certmanager-release"`).
- In `knative.py`: **remove** the cert-manager `Release`; keep the KnativeServing
  CR gate on `certmanager_ready` (now sourced from the always-on release), i.e.
  separate cert-manager readiness from the knative chain so k8gb/argocd do not
  become coupled to knative.
- Tests: cert-manager `Release` now asserted in the **baseline** case (no knative).
- Verify: `up project build` + `up test run tests/*`.

## Step 2 - nginx-ingress (always-on, new) [SUPERSEDED]

> nginx-ingress was retired 2026-03-24 and replaced by Envoy Gateway (Gateway
> API), gated `k8gb OR argocd` rather than always-on - see
> [`docs/superpowers/specs/2026-07-21-gateway-api-migration-design.md`](superpowers/specs/2026-07-21-gateway-api-migration-design.md).

- Create `functions/ctp/ingress.py` with `add_ingress_resources(rsp, id_val, config)`:
  an `ingress-nginx` `Release` (repo `https://kubernetes.github.io/ingress-nginx`,
  pinned + renovate), **standard LoadBalancer service** (not hostNetwork),
  `wait: true`, stale-Ready workaround, child-cluster ProviderConfig.
- Wire unconditionally in `main.py` (subject to the always-on caveat above).
- Tests: nginx `Release` asserted in baseline.
- Verify: build + tests.

## Step 3 - AWS Load Balancer Controller (gated `k8gb.enabled`; prerequisite for the CoreDNS NLB)

**Why:** CoreDNS must serve UDP:53. On EKS a `type: LoadBalancer` Service is a
Classic ELB by default (no UDP); reliable UDP + `TCP_UDP` + EIP pinning are AWS
Load Balancer Controller features. Neither `configuration-aws-eks` nor this
package installs it today (aws-eks provisions only the vpc-cni / ebs-csi /
pod-identity-agent addons - verified). This is exactly why resilient-ctp exposed
CoreDNS via hostNetwork nginx instead of a LB; switching to a LB (needed for
stable glue IPs) requires this controller.

**Identity: EKS Pod Identity** (not IRSA). aws-eks already installs the
`eks-pod-identity-agent` addon (`functions/eks/main.k:355`) and already uses a
`PodIdentityAssociation` for the EBS CSI driver (`main.k:319`), so Pod Identity is
the fleet's established addon-identity pattern: zero new dependencies, a static
`pods.eks.amazonaws.com` trust policy (cluster-agnostic), **no IAM OIDC provider**
(aws-eks registers none; it only surfaces the issuer URL to status), and no
`status.eks.oidcIssuerUrl` dependency.

**Approach: helm `Release` + Pod Identity in aws-ctp** (single self-contained PR;
mirror the aws-eks EBS CSI shape).
- Create `functions/ctp/lbcontroller.py` `add_lbcontroller_resources(...)`:
  - **IAM `Role` + `Policy`** (AWS-side, `provider_config`): the published AWS Load
    Balancer Controller policy attached to a role whose trust allows
    `pods.eks.amazonaws.com` (`sts:AssumeRole` + `sts:TagSession`). No OIDC, no
    issuer-URL interpolation.
  - **`PodIdentityAssociation`** (AWS-side, `provider_config`) mapping
    `(clusterName, namespace: kube-system, serviceAccount: aws-load-balancer-controller)`
    to that role. Use aws-ctp's **namespaced** MR variant
    (`eks.aws.m.upbound.io/...`) to match its other AWS MRs; aws-eks's EBS CSI
    association at `main.k:319` is the reference shape.
  - `aws-load-balancer-controller` `Release` (chart repo
    `https://aws.github.io/eks-charts`, pinned + renovate, `wait: true`,
    stale-Ready workaround, child ProviderConfig): set `clusterName`; let the chart
    create its SA (`serviceAccount.create: true`, `aws-load-balancer-controller` in
    `kube-system`) with **no** role-arn annotation - the association binds the
    role. Confirm the pinned chart version supports Pod Identity (recent ones do).
  - Gate the `Release` on the IAM role + association existing, same chaining style
    as the other releases.
- **`functions/ctp/usages.py`**: `of: EKS, by: ...` `Usage` guard for the
  controller `Release` (a child-cluster resource); the IAM Role +
  `PodIdentityAssociation` are AWS-side and need no kubeconfig-survival guard.
  Emit only when k8gb is enabled.
- **`main.py`**: invoke when `k8gb.enabled == "yes"`, before k8gb (Step 4).
- Tests: the controller `Release` + IAM Role + `PodIdentityAssociation` render when
  `k8gb.enabled`, absent otherwise.
- Verify: build + tests.

## Step 4 - k8gb producer (gated `k8gb.enabled`; defines the status contract)

- **XRD** `apis/ctp/definition.yaml`:
  - `spec.parameters.k8gb`: `enabled` (`yes`/`no`, default `no`), `dnsZone`,
    `parentZone`, `clusterGeoTag` (optional; unique-per-CP default derived
    in-function - see below), `strategy` (`failover`/`roundRobin`/`geoip`,
    default `failover`).
  - `status.controlplane.k8gb`: `enabled`, `coreDNSEndpoint`, `nsName`,
    `glueAddresses`, `delegationRecord`.
- **`functions/ctp/k8gb.py`** `add_k8gb_resources(...)`:
  - k8gb `Release` (chart `k8gb`, repo `https://www.k8gb.io`, **version pinned to
    match resilient-ctp's `Gslb` v1beta1 consumer - not latest**): values reuse
    resilient-ctp's operator shape - `k8gb.dnsZones`, `k8gb.clusterGeoTag`,
    `k8gb.extGslbClustersGeoTags`, `k8gb.edgeDNSServers`,
    `k8gb.deployCrds/deployRbac`; plus **`extdns.enabled: false`**. Stale-Ready
    workaround.
  - **Expose CoreDNS via an NLB serving BOTH UDP:53 and TCP:53.** DNS needs UDP
    (primary) and TCP (large answers), so the CoreDNS Service must carry both
    ports on one NLB `TCP_UDP` listener (needs the Step 3 controller). **Verify
    the exact k8gb/coredns chart keys before coding** - e.g. `k8gb.coreDNSExposed`
    and the coredns subchart's `serviceType` / `serviceAnnotations`; do **not**
    assume `coredns.serviceType`. Put the AWS NLB (+ later EIP) annotations there.
  - `clusterGeoTag` default must be **unique per control plane** - `aws-<region>`
    collides if two CPs share a region; incorporate the CP `id`
    (e.g. `aws-<region>-<id-suffix>`) or require the param.
  - `extGslbClustersGeoTags` derived from **same-cloud peers** in
    `allControlPlanes` (same helper source as `check_license_conflict`) that have
    k8gb enabled + same `dnsZone`. Cross-cloud peers come from the fleet layer
    (out of scope here); empty is acceptable for a single-cluster start.
    **Ownership seam:** when FleetGslb lands it injects cross-cloud geo-tags -
    decide now whether FleetGslb owns the whole list or only a distinct
    cross-cloud value, so there are not two writers to this one Helm value.
  - **Observe-only `Object`** (`managementPolicies: ["Observe"]`) on the child
    k8gb CoreDNS `Service` to read `status.loadBalancer.ingress`.
- **`functions/ctp/usages.py`**: add `Usage` guards (`of: EKS, by: ...`) for
  **both** the k8gb `Release` **and** the CoreDNS observe `Object`, emitted only
  when k8gb is enabled. (Confirm whether observe-only Objects hang on teardown;
  guard them regardless.)
- **`functions/ctp/status.py`**: populate `status.controlplane.k8gb` -
  `enabled`, `coreDNSEndpoint` (from the observed CoreDNS Service Object),
  `nsName` (the k8gb NS name), `glueAddresses` (the pinned CoreDNS EIPs), and
  `delegationRecord` (computed NS+glue string). **This is the contract the
  FleetGslb aggregator reads - keep the field names stable, and make the NS names
  match k8gb's `ClusterNSName`/`ExtClusterNSNames` convention** (not an ad-hoc
  format), or FleetGslb's later writes will not line up with what each k8gb
  expects. Glue is built from `glueAddresses` (the pinned EIPs), not from
  `coreDNSEndpoint` - see the EIP requirement in Assumptions.
- **`main.py`**: `if k8gb and k8gb.get("enabled") == "yes": add_k8gb_resources(...)`.
- Tests: k8gb `Release` + CoreDNS observe `Object` + both `Usage`s render when
  enabled, absent when disabled.
- Verify: build + tests.

## Step 5 - argocd (gated `argocd.enabled`; app-of-apps)

- **XRD**: `spec.parameters.argocd`: `enabled` (`yes`/`no`, default `no`),
  `hostname` (UI Ingress host), `url` (public git repo).
- **`functions/ctp/argo.py`** `add_argocd_resources(...)`:
  - ArgoCD `Release` (chart `argo-cd`, repo `https://argoproj.github.io/argo-helm`,
    pinned + renovate), `wait: true`, stale-Ready workaround, child ProviderConfig.
  - UI `Ingress` (host `argocd.hostname`, nginx ingress class) with TLS. For a
    standalone CP a local cert-manager `Certificate` is fine; note that the
    **global** hostname's production cert is issued by the parent and synced down
    (see gslb-dns-architecture §8) - this PR only needs the UI reachable. Applied
    via provider-kubernetes `Object`(s).
  - **Root `Application` (app-of-apps)** as a provider-kubernetes `Object`
    (`argoproj.io/v1alpha1`, kind `Application`): `spec.source.repoURL = argocd.url`,
    `path: "."`, `targetRevision: HEAD`, destination in-cluster, automated sync.
    **Gate this Object on the ArgoCD release being deployed** (so the Application
    CRD exists), same pattern as the KnativeServing CR gate. Public repo -> no
    repo Secret.
- **`functions/ctp/usages.py`**: `of: EKS, by: ...` `Usage` guards for the argocd
  `Release` and each argocd `Object` (Ingress, Certificate, Application), emitted
  only when argocd is enabled.
- **`main.py`**: `if argocd and argocd.get("enabled") == "yes": add_argocd_resources(...)`.
- Tests: ArgoCD `Release`, UI `Ingress`/`Certificate`, root `Application`, and the
  `Usage` guards render when enabled; absent when disabled.
- Verify: build + tests.

## Step 6 - installation e2e (same PR, behind `run-e2e-tests`)

- New `tests/e2etest-addons/` mirroring `tests/e2etest-controlplane/`: a
  `ControlPlane` with `k8gb.enabled: "yes"` (+ `dnsZone`/`parentZone`/`strategy`)
  and `argocd.enabled: "yes"` (+ `hostname`/`url`).
- Assertions (management-plane-visible signals):
  - `Release` MRs **Synced + Ready**: cert-manager, nginx-ingress,
    aws-load-balancer-controller, k8gb, argocd (with `wait: true`,
    `state=deployed` implies the chart's resources are up).
  - k8gb CoreDNS observe-`Object` shows an LB ingress; XR
    `status.controlplane.k8gb.coreDNSEndpoint` is non-empty. **This exercises the
    real UDP NLB - it will fail if the LB controller (Step 3) is absent or the
    CoreDNS Service is not dual-protocol.**
  - ArgoCD root `Application` `Object` applied (bonus: Synced/Healthy if cheap).
  - XR `Ready=True`.
- **Scope:** this is an *installation* e2e. It does not test DNS failover -
  nothing writes the NS delegation yet (FleetGslb is a separate workstream), so
  GSLB is not functional end-to-end after this PR.
- Run via the `control-plane-project:e2e-test-configuration` skill (wraps
  `up test run --e2e` with monitoring/stuck-detection). Expect 10-20+ min.

## Cross-cutting (every step)

- Update `README.md` parameter table; add `examples/controlplane/with-k8gb.yaml`
  and `examples/controlplane/with-argocd.yaml`.
- No new package dependencies expected - cert-manager/nginx/k8gb/argocd and the LB
  controller all reuse `helm.m.crossplane.io` + `kubernetes.m.crossplane.io`
  (already used by knative), and the LB controller's IAM Role +
  `PodIdentityAssociation` reuse the existing AWS provider MRs (aws-eks already
  uses `PodIdentityAssociation` for EBS CSI). Confirm they resolve.
- Keep the FleetGslb status contract (`coreDNSEndpoint`, `nsName`, `glueAddresses`,
  `delegationRecord`) stable once defined in Step 4.

## Assumptions / deferred

- Public git repo for ArgoCD (repo credentials Secret deferred).
- Cross-cloud k8gb mesh membership (`extGslbClustersGeoTags` across clouds) is a
  fleet-layer concern; this PR wires only same-cloud peers (or none).
- Stable CoreDNS glue IPs (NLB Elastic IPs) - **DONE/shipped in the aws producer**:
  one EIP is pinned per public subnet on the CoreDNS NLB, and `status.controlplane.k8gb.glueAddresses`/`delegationRecord`
  publish only once all pinned EIPs are allocated. NS glue is now built from those
  EIPs, not from the NLB's (rotating) hostname.
- Cross-cluster CoreDNS reachability on `:53` (peer k8gb + external resolvers) and
  its security posture - a fleet-layer concern, not exercised by this PR.
- azure-ctp / gcp-ctp ports - later PRs. **NOT just LB annotations:** Azure/GCP
  need a pre-provisioned static IP resource for the CoreDNS LB and the
  `MixedProtocolLBService` feature gate for TCP+UDP:53 (AWS pins NLB EIPs via
  annotation). The add-on modules port cleanly; the CoreDNS LB does not.
