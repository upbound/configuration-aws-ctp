# Fleet GSLB DNS architecture - decision record

- **Date:** 2026-07-16
- **Status:** Decided (design); implementation not started. k8gb coordination
  mechanism validated against upstream (the k8gb `resolver` package + docs) on
  2026-07-16.
- **Scope:** Cross-cutting. Spans `configuration-{aws,azure,gcp}-ctp`,
  `configuration-resilient-ctp`, and a **new** `configuration-fleet-gslb`
  package. Documented here in `configuration-aws-ctp` for convenience; it is
  **not** AWS-specific.
- **Related:** `configuration-resilient-ctp` `docs/SPEC.md` (§4.1 optional k8gb
  install, §12 "move k8gb to ctp packages"), `docs/ROADMAP.md`, and
  `docs/DNS_HEARTBEAT_BACKEND.md`.

## 1. Goal

Give a fleet of Upbound control planes **health-aware, DNS-based global
failover** across regions and clouds using **k8gb** (Kubernetes Global
Balancer), in a way that:

- is **uniform** across AWS / Azure / GCP control planes,
- keeps the **workload/child clusters free of cloud DNS write credentials**
  (an enterprise requirement), and
- still **automates** the DNS wiring rather than requiring manual steps per
  cluster.

## 2. How k8gb uses DNS (background)

k8gb runs its own **CoreDNS** (with a health-aware plugin) that is the
**authoritative nameserver for a load-balanced zone** (e.g. `gslb.example.com`).
It answers queries *dynamically* - returning only healthy clusters' IPs - which
a static cloud-DNS record cannot do.

Two facts about how k8gb coordinates across clusters shape the whole design.
Both were validated against the k8gb `resolver` package and the k8gb docs:

- **external-dns's only job is the NS zone delegation.** In stock k8gb,
  external-dns takes the delegation `DNSEndpoint` k8gb generates and writes the
  `NS` + glue `A` records into the parent zone. It does **not** publish per-host
  or `localtargets` records.
- **Each cluster's CoreDNS serves its own per-host and `localtargets` records;
  peers are found through the delegation plus a static geo-tag list.** Every
  cluster is authoritative for the load-balanced zone and serves
  `localtargets-<host>` = its own ingress IPs. A k8gb instance learns its peers
  from `spec.k8gb.extGslbClustersGeoTags` (a **static** list - dynamic geo-tag
  discovery works only with Infoblox, not Route53 / Azure DNS / Cloud DNS) and
  resolves their targets through the delegated NS hierarchy.

Two consequences follow, and both are load-bearing:

1. **CoreDNS must be exposed and mutually reachable on port 53 (UDP and TCP).**
   External resolvers reach it to resolve the zone, and peer clusters' k8gb reach
   it to exchange targets/health. Across clouds this means either public `:53` on
   each CoreDNS load balancer or private inter-cloud connectivity.
2. **The delegation is the whole of the DNS-write automation k8gb needs.**
   Nothing else has to be written to the parent zone. This is exactly why a
   single parent-side writer can replace per-child external-dns (see §5).

## 3. The core problem

There is exactly **one authoritative parent zone**, and it lives in **one**
place: the cloud DNS service the organization designates (Route53, Azure DNS, or
Cloud DNS). In a multi-cloud fleet:

- `external-dns` (the tool that would automate the delegation) writes to **its
  own cloud's** DNS and needs **that cloud's write credentials**. Its identity
  wiring (AWS IRSA / Azure Workload Identity / GKE Workload Identity) is
  **irreducibly per-cloud**.
- So automating the delegation *per child cluster* only works when a child can
  write the zone's cloud. For a **single shared zone**, the foreign-cloud
  children cannot - you'd have to hand them the zone-hosting cloud's credentials
  (**cross-cloud credential sprawl**), which defeats the point.

This single-shared-zone constraint is inherent; no ownership choice removes it.
It is the fact around which every option below is shaped.

## 4. Options considered

| # | Option | Verdict |
|---|--------|---------|
| 0 | **external-dns on every child, per-cloud identity** - each child runs external-dns with its own cloud's DNS-write identity (AWS IRSA/Route53, Azure Workload Identity, GKE Workload Identity). | **Rejected.** 3× per-cloud identity code, cluster workloads granted DNS-write, and it *breaks* for a shared cross-cloud zone (foreign children can't write it). |
| 1 | **Manual delegation everywhere** - no external-dns; a human adds the NS records once. | Viable and credential-free, but manual and non-self-healing. Kept as the universal **status-surfacing baseline**. |
| 2 | **Per-cloud subzones** - each cloud hosts its own subzone (`aws.gslb…` in Route53, `azure.gslb…` in Azure DNS, …); each child writes only its own subzone with **local** creds; parent gets a stable one-time NS delegation per subzone. | Technically cleanest (zero cross-cloud creds, fully decoupled). Cost: hierarchical topology + standing up **authoritative subzones in three clouds** (governance friction). Held as the alternative. |
| 3 | **Hybrid** - automate the home-cloud child's record via external-dns, surface foreign ones in status for a human. | A midpoint; payoff scales only when the DNS-hosting cloud has many clusters. |
| 4 | **Child-side aggregation** - a child reads *all* peers' DNS details and writes the full delegation. | **Failed.** `requiredResources` reads only the **local** API server; a child cannot see other children's secrets/status. Needs a shared vantage point. |
| 5 | **Parent-side aggregation** (chosen) - children publish their endpoint to the parent; one fleet-level aggregator on the parent reads all and writes the single main zone. | **Chosen - see §5.** |

### Why option 4 failed, and how it led to 5

`ResilientControlPlane` and the ctp `ControlPlane`s all run against a Crossplane
API server, and `requiredResources`/`function-extra-resources` fetch resources
**from that same API server only**. A `ResilientControlPlane` on child cluster
A therefore sees only child A's local DNS secret - never child B's. Aggregating
on a child is impossible without a shared store.

The resolution: the **parent (management) cluster already is the shared
vantage** - it created every `ControlPlane` XR, so all of them (across clouds)
live on its one API server, with status. Aggregate there.

## 5. Decision

Adopt **option 5: a single flat main zone, written by a dedicated
parent-side aggregator** - a **new configuration package**,
`configuration-fleet-gslb`, exposing a `FleetGslb` XR of which **one instance**
runs on the parent.

**Why this model:**

- **Fits enterprise DNS governance** - one authoritative zone the org already
  runs, with an explicit "this is where DNS lives" choice, rather than
  authoritative subzones spun up in three clouds.
- **Children stay DNS-credential-free** - children never write DNS; the parent
  does, to the one main zone, with credentials it already holds. Children only
  run k8gb + CoreDNS and *publish* their endpoint. This satisfies the enterprise
  requirement **and** keeps full automation.
- **The parent has native global visibility** - it holds every `ControlPlane`
  XR, so the aggregator discovers all endpoints via `requiredResources`, with
  **no shared store and no cross-cloud read credentials**.
- **One write credential, one place** - the per-cloud write path collapses to a
  single provider (whichever cloud hosts the main zone), configured once on the
  parent. Because external-dns's only job is the delegation (§2), writing that
  delegation from the parent fully replaces per-child external-dns.

**Unifying principle.** The parent is the fleet's **single DNS-credentialed
controller and aggregation point**: it writes the delegation, issues and holds
the TLS certificates for the global hostnames, distributes fleet membership to
the children, and (where cross-cloud single-active is required) elects the
leader. Children are **credential-free execution endpoints**: they run k8gb +
CoreDNS, expose `:53`, serve their own records, and publish their status upward.
Every "how does a child do X without credentials" question has the same answer -
it does not, the parent does.

**Assumption (accepted for now):** a **single management plane** holds all
`ControlPlane`s across clouds. A multi-parent topology is a later evolution
(see §10).

## 6. Chosen architecture - three roles

```
PARENT (management) cluster
 ├─ ControlPlane XRs  (aws/azure/gcp kinds)          [PRODUCER - one per control plane]
 │     each ctp composition:
 │       • installs k8gb + CoreDNS (LoadBalancer, static IP, :53 UDP+TCP) on its child
 │       • installs cert-manager unconditionally (free; no gate)
 │       • observes the child CoreDNS Service
 │       • surfaces status.k8gb.coreDNSEndpoint (+ delegation record)
 │       • receives the fleet peer geo-tag list from the parent (extGslbClustersGeoTags)
 │       • NO external-dns, NO DNS credentials on the child
 │
 └─ FleetGslb XR      (configuration-fleet-gslb)     [AGGREGATOR / WRITER / MEMBERSHIP / CERTS - one instance]
        • reads ALL ControlPlane statuses via requiredResources (label selector)
        • assembles the full NS + glue delegation set and writes it to the main zone
          via a Crossplane DNS provider MR (Route53 / Azure DNS / Cloud DNS)
        • NS names match k8gb's ClusterNSName/ExtClusterNSNames convention
        • distributes the fleet member/geo-tag list back to each child's k8gb
        • issues the global-hostname TLS cert (DNS-01, parent creds) and syncs it to children
        • input: main-zone location + participant selector

CHILD clusters
 └─ ResilientControlPlane XR  (configuration-resilient-ctp)   [FAILOVER + Gslb owner - one per control plane]
        • creates the k8gb Gslb CR for the control-plane app endpoint
        • failover topology per §7 (active-active geo across clouds; election within a cloud)
```

- **Producer** = each ctp `ControlPlane`: publishes *its own* endpoint into its
  own status, installs k8gb + CoreDNS + cert-manager on the child, and receives
  the fleet peer list from the parent.
- **Writer / membership / certs** = one `FleetGslb`: reads everyone, writes the
  one main zone, distributes the geo-tag list down, and issues+syncs the TLS
  cert.
- **Failover brain** = `ResilientControlPlane` on each child: creates the k8gb
  `Gslb` for the app endpoint and consumes its health signal + heartbeats.

Explicitly: the aggregator is **not** replicated into each ctp. N per-ctp
aggregators would race to write the same records and would each need to read the
other clouds' `ControlPlane` kinds - the wrong shape.

## 7. Failover topology

**Two senses of "active/passive"** - separate, and only the second needs
election:

- **Traffic** - which cluster receives client requests. Decided purely by k8gb /
  DNS.
- **Reconcile** - which control plane holds `managementPolicies: ["*"]` and
  actually *writes* the shared managed resources (peers sit at `["Observe"]`).
  resilient-ctp's leader election governs this; it exists to stop two control
  planes corrupting the same resources.

Which failover model fits depends on whether the clusters share resources:

- **Active-active** (`roundRobin` / `geoip`): all healthy clusters serve, each
  reconciling its **own disjoint** resources. No single-leader guarantee is
  needed, so no cross-cloud coordination is required - the simplest, safest
  cross-cloud mode. It only fits when clusters own disjoint work (or are
  stateless); two clusters reconciling the *same* resource active-active is as
  wrong as an unguarded active-passive.
- **Active-passive** (`failover`): one primary serves traffic and reconciles the
  shared resources; a standby takes over on failure. Exclusivity is carried by
  the **GSLB signal**, which is DNS-based and therefore **cross-cloud-visible**
  (resilient-ctp `SPEC.md` §6.1: in `failover`, GSLB yields exclusive activeness;
  heartbeat + priority only break residual ties). This works across clouds, with
  an asymmetry in how much backs it up:
  - **Same-cloud** (e.g. 2 regions): leadership stands on **two** independent
    signals - the GSLB signal *and* the shared-store heartbeat (SSM/RG/label) -
    which back each other up. Region-death failover is proven here (Test 1). This
    is the solid mode today.
  - **Cross-cloud** (e.g. AWS primary / Azure standby): the heartbeat store is
    per-cloud, so peers cannot read each other's heartbeat. Leadership then rests
    on the **GSLB signal alone** (plus each cluster's own self-heartbeat) - a
    thinner margin. Supported by design, but only safe once the two guards below
    hold; until then, prefer active-active for cross-cloud.

**Guards cross-cloud active-passive depends on** (both currently unimplemented -
land them before relying on it in production):

1. **"unreadable ≠ down."** `election.py` currently treats an unreadable
   higher-priority peer as *down*; `SPEC.md` §6 requires GSLB to independently
   confirm that peer's geo unhealthy before promoting. A cross-cloud peer is
   always unreadable, so without this guard it reads as down.
2. **GSLB must not degrade open.** `gslb.py` defaults `healthy=active=True` when
   no `Gslb` is present. Combined with (1), an unreadable peer + a degraded-open
   GSLB promotes a second leader. This is why resilient-ctp must **own and always
   create the `Gslb`** (§9) - the one leg cross-cloud leadership stands on must
   never silently disappear.

**Optional later strengthening:** cross-cloud single-active can be made
*independent* of GSLB by electing **on the parent**, which already holds every
`ControlPlane` status (the same visibility that drove option 5). That yields a
cross-cloud leader signal without a shared child-side store, at the cost of
parent-bound latency. Not required for v1.

**v1 scope:** same-cloud active-passive (both signals) is the proven mode;
active-active geo is the clean cross-cloud mode where clusters own disjoint work;
cross-cloud active-passive via `failover` is supported by design and becomes
production-ready once the two guards above land.

## 8. TLS for the global hostnames

Standby clusters must already hold a valid certificate for the global hostname
**before** failover, but they cannot complete ACME **HTTP-01** (the hostname
resolves to the *active* cluster) and must not hold DNS-write credentials (the
whole point of the design). Resolution:

- **The parent issues the certificate and distributes it.** cert-manager on the
  parent runs a **DNS-01** issuer against the main zone (the credentials
  `FleetGslb` already holds), obtains the cert once, and pushes the resulting
  `Secret` to every child via the parent's existing provider-kubernetes `Object`
  write path (the same path that already delivers, e.g., the UXP license).
  Children reference the synced `Secret` on their Ingress. Use a **wildcard**
  `*.<loadBalancedZone>` to cover all global hostnames with one cert where org
  policy allows; otherwise per-host.
- **Internal-only audiences** may instead use a **private-CA / enterprise-PKI**
  issuer on each child - no ACME, no DNS-01, no distribution.
- **Rejected:** DNS-01 on children (reintroduces the credentials being removed)
  and HTTP-01 (cannot validate on a standby that is not the DNS target).

Caveats: the private key traverses the parent→child write path (the parent
already holds everything, so acceptable); renewal re-syncs the `Secret`. Note
cert-manager is installed unconditionally on every child (§9); it is a free
component, so there is no licensing consideration.

## 9. Impact per package

### `configuration-{aws,azure,gcp}-ctp` - become PRODUCERS
- Add a `k8gb` parameter block (enable + zone/geo inputs, and the fleet peer
  geo-tag list supplied by the parent) and a `k8gb` status block.
- Install k8gb + CoreDNS on the child via the existing provider-helm path,
  exposing CoreDNS as a **LoadBalancer Service on `:53` (UDP + TCP) with a pinned
  static IP**. **No external-dns, no DNS IAM/IRSA.**
- **Static IP is not a per-cloud one-liner.** AWS can pin NLB Elastic IPs via
  annotation, but **Azure and GCP require a pre-provisioned static IP resource**
  (Azure Standard Public IP; GCP reserved regional address) as an additional
  managed resource with its own IAM, then referenced by the Service. Mixed
  TCP+UDP on `:53` in one Service is native on AWS NLB but needs the Kubernetes
  `MixedProtocolLBService` feature gate on Azure/GCP (and newer GKE with
  subsetting/RBS).
- Compose an **observe-only** provider-kubernetes `Object` on the child CoreDNS
  `Service`, and surface `status.…k8gb.coreDNSEndpoint` (+ a ready-to-use
  delegation record) on the parent-side `ControlPlane` XR. This reintroduces the
  observe-only `Object` pattern dropped in commit `dc8644d`; see the teardown
  note below.
- **cert-manager is installed unconditionally** (decoupled from the knative
  gate). It is a free component; the previous knative-and-license gating is
  removed. Optional add-ons (knative, k8gb, ArgoCD) stay behind their own flags.
- **Installs the globally-balanced apps as add-ons** - ArgoCD first (gated, like
  the existing knative add-on), and later the UXP/console or an HTTP-API app -
  each exposed via an Ingress carrying the global hostname with the
  parent-issued cert (§8). Those Ingresses are what resilient-ctp's `Gslb`
  references.
- The add-on composition logic is cloud-agnostic; the **cloud-specific work is
  the CoreDNS LoadBalancer** (static IP + protocol handling above), which lands
  in the existing per-cloud network/identity modules, not the shared add-on
  layer.
- **Teardown dependency:** the new child-cluster `Object`s (k8gb Helm `Release`,
  CoreDNS observe `Object`) must carry `Usage` guards protecting the child
  cluster + its kubeconfig secret, or they orphan-finalize on delete. Only
  `Release`→cluster and cluster→`Network` guards exist today; the child-cluster
  deletion-guards fix must land before these Objects ship.

> **ctp add-on install model - change required.** cert-manager is today
> installed *inside* the knative add-on, gated by `knative.enabled` (plus a
> license gate). The k8gb UI Ingresses need cert-manager **regardless of
> knative**, so **cert-manager is decoupled and installed unconditionally**. Its
> readiness signal must be **separated from the knative readiness chain** (today
> `certmanager_ready` feeds knative gating) so k8gb does not become coupled to
> knative being enabled.

### `configuration-fleet-gslb` - NEW, the AGGREGATOR / WRITER / MEMBERSHIP / CERTS
- New Crossplane v2 Configuration package; a single `FleetGslb` XR instance on
  the parent.
- Input: main-zone provider + zone id/name (where DNS "sits"), and a
  selector/member list of participating `ControlPlane`s (one selector per cloud
  kind, or a shared `fleet.upbound.io/set` label).
- Reads all `ControlPlane` statuses via `function-extra-resources`, assembles
  NS+glue, and writes them as **provider DNS `Record` MRs** for the main zone's
  cloud (parent already holds those creds). **The NS record names must match the
  `ClusterNSName`/`ExtClusterNSNames` convention each k8gb expects for its
  peers**, or peer resolution silently fails.
- **Distributes the fleet member/geo-tag list** to each child's k8gb
  (`extGslbClustersGeoTags`) and keeps it current as membership changes - dynamic
  geo-tag discovery is not available on the target cloud DNS providers, so this
  must be pushed explicitly.
- **Issues and syncs the global-hostname TLS cert** (§8).
- Only adds a cluster to the NS set once its CoreDNS is verified **serving**, and
  removes it promptly on failure - otherwise one bad cluster lames the whole zone
  (§10).
- Optionally republishes the assembled delegation in its own status for
  visibility/audit.

### `configuration-resilient-ctp` - FAILOVER + owns the `Gslb`
- Keeps its role: per-child leader election + failover, consuming the k8gb
  `Gslb` signal and the heartbeat ledger.
- **Now owns (creates) the `Gslb` CR** for the globally-balanced HTTP endpoints.
  This is **net-new**: today the composition only *consumes* an externally-
  created `Gslb` and degrades open when none is present. It will create the
  `Gslb` (a provider-kubernetes `Object` wrapping the CRD) referencing the **HTTP
  app Ingress** it balances (ArgoCD UI, UXP/console, or an app's HTTP API) with
  TLS via the parent-synced cert (§8) - **not** the kube-apiserver (no global
  k8s-API access is planned). Update SPEC §1/§3/§5, which currently scope
  resilient-ctp as read-only over the `Gslb`.
- **Leadership health signal = `spec.gslb.hostname`** - the single hostname whose
  `Gslb` health (ANDed with heartbeat + priority) decides whether this control
  plane may be leader.
- **k8gb ownership moves to the ctp packages** → its optional built-in install
  (`spec.k8gb.install`) defaults to / stays `never` and can later be retired.
  This is the repo's **declared end-state** (SPEC §12 and the ROADMAP backlog),
  not a new direction.
- **Heartbeat backend:** because children no longer run external-dns, the
  **DNS-TXT heartbeat backend** (which depends on external-dns) is not used; use
  the **cloud-resource (SSM/RG/label) backend**. Note that **only the AWS SSM
  backend is implemented today**; the Azure Resource Group and GCP label backends
  are design-only (raise `NotImplementedError`) and must be built before those
  clouds can participate.
- **Failover topology** per §7: active-active geo across clouds, election within
  a cloud.

## 10. Consequences & trade-offs

**Gains**
- Children are DNS-credential-free; DNS write is one provider, one cred, one
  place; automation preserved end-to-end.
- Uniform, cloud-agnostic producer *add-on* logic across the three ctp packages
  (Azure and GCP are already at the AWS baseline: both compose cluster + network
  XRs and read `status.{aks,gke}`).
- No shared store and no cross-cloud read credentials - the parent's native
  visibility does the discovery.

**Costs / risks**
- **SPOF for updates:** the single parent is the sole delegation writer. If it
  is down, existing records persist and k8gb keeps serving, but *new/changed*
  delegations don't propagate until it recovers. Acceptable now; multi-parent
  later.
- **Single-management-plane assumption:** if the fleet later uses one parent per
  cloud, no single parent sees everyone - revert to per-cloud subzones (option 2)
  or add a cross-parent store.
- **Cross-kind reads:** the aggregator reads three `ControlPlane` kinds
  (aws/azure/gcp) - handled with one selector per kind or a shared label.
- **Cross-cluster CoreDNS reachability (hard requirement):** every cluster's
  CoreDNS must be reachable on `:53` (UDP+TCP) by external resolvers **and** by
  peer clusters' k8gb, across clouds. This implies public `:53` exposure (or
  private inter-cloud connectivity) and a security posture for it. Without it,
  health-aware cross-cluster answers do not form.
- **Stable glue IPs are a requirement, not a nicety:** stale NS glue is *lame
  delegation* and takes the **whole zone** down, not one cluster. Pin static IPs
  on every CoreDNS LB. Per-cloud cost differs (Azure/GCP need a pre-provisioned
  IP resource + IAM; AWS an EIP annotation), and mixed TCP+UDP:53 needs the
  `MixedProtocolLBService` gate on Azure/GCP.
- **Membership distribution:** the parent must push the geo-tag list to children
  and keep it current; a stale list makes a cluster invisible to its peers.

## 11. Rejected alternatives (why not)

- **Option 0 (external-dns everywhere):** breaks for a shared cross-cloud zone
  and grants DNS-write to every workload cluster.
- **Option 2 (per-cloud subzones):** cleanest technically and kept as the
  fallback, but requires authoritative subzones in three cloud DNS services -
  governance friction the single-zone model avoids. Reconsider if the
  single-management-plane assumption or a single authoritative zone becomes
  untenable, or if the cross-cluster `:53` reachability posture proves
  unworkable.
- **Option 4 (child-side aggregation):** impossible - `requiredResources` is
  local to one API server.

## 12. Open items / follow-ups

- Define the `FleetGslb` XR schema (main-zone provider/zone, participant
  selector, TTLs, strategy, member/geo-tag distribution, cert issuance) and which
  cloud DNS providers it supports first.
- Decide participant discovery: label selector vs. explicit member list.
- Membership → child geo-tag distribution mechanism (how the parent writes
  `extGslbClustersGeoTags` into each child's k8gb `Release`).
- NS-naming contract between `FleetGslb`'s delegation records and k8gb's expected
  peer NS names.
- Cross-cluster CoreDNS `:53` exposure and security posture (public vs. private
  inter-cloud connectivity).
- Per-cloud static-IP MRs for the CoreDNS LBs (Azure Public IP, GCP reserved
  address) and the `MixedProtocolLBService` requirement.
- Child-cluster deletion-guards (`Usage`) fix must land before the k8gb/CoreDNS
  `Object`s ship.
- Implement the Azure RG and GCP label heartbeat backends (only AWS SSM exists).
- Implement the SPEC §6 "unreadable ≠ down" election guard before any
  cross-cloud/region election.
- cert-manager: decouple from the knative gate and install unconditionally;
  separate its readiness from the knative chain. (Decided; free component, no
  license gate.)
- Multi-parent evolution (removes the SPOF; needs a cross-parent aggregation
  story) and cross-cloud single-active via parent-side election.
