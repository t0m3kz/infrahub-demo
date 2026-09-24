# Changelog

## v0.4.0 (2026-09-24)

### Feat

- **colocation**: cable a metro's service pair to its on-ramp, physical only
- **topology**: generate a colocation metro's on-ramp from templates
- **checks**: walk the VM-to-host edge in change risk
- **data**: model the 30_all compute layer end to end
- **data**: 30_all interconnects over colo, cloud hub and SD-WAN
- **data**: add Megaport, CoreSite and VeloCloud providers
- **schema**: let shared colo cages and cloud regions terminate circuits
- **checks**: change-risk blast radius from a branch diff
- **schema**: cloud hub attachments, and circuits over interface capabilities

### Fix

- **generators**: stop HA nodes flip-flopping and mark sync ports active
- **generators**: fail only the racks that actually need a spine
- **generators**: turn on the routing planner's strict mode
- **generators**: refuse to generate a rack that cannot be routed
- **generators**: wait for the pod ASN pool before routing a rack
- **data**: make a clean 30_all load produce no failed generator tasks
- **data**: give the US metros their country parent
- **checks**: make change-risk pass 1 enumerate targets, not guess
- **queries**: narrow AppGeneric hierarchy hops in impact_exposure
- **deps**: pin infrahub-sdk floor to 1.23.2 to prevent silent downgrades
- **data**: use M_MIDDLE layout for DC3's pods, not L_MIDDLE
- retry border-leaf lookup instead of silently no-opping
- account for a sibling's own outbound slots in inter-pod mesh offset
- wait for trigger automations to activate before any bulk DC load
- also poll for DC super-spine devices in the existing DC-readiness retry
- point DC/pod created-triggers at plain bootstrap, not cascade
- stop verify_no_failed_tasks from dropping real failures
- stop pool-reference resaves from spuriously re-firing update triggers
- remove orphaned device_capabilities identifier override on HA nodes
- register shared-AS-id devices in autonomous_systems for overlay planning
- add missing watch blocks and guard non-DC racks in generator

## v0.3.0 (2026-09-14)

### Feat

- **app**: add approved deployment request workflow
- **schema**: app-to-app dependencies, egress proxy, and ZTNA publishing
- **vlan**: allocate local VLAN ID per VLAN domain instead of DC-wide
- **lb**: add SNAT-aware backend PBR; fix spine/super-spine VXLAN leak; explicit BGP process/peering roles
- **vxlan-gpo**: close border-leaf NVE gap, bind PBR to FW-context sub-iface, provision dedicated firewall pairs
- **schema**: add TopologyCustomer.firewall_context reverse relationship
- **security**: auto-provision FirewallContext + PBR default-to-firewall
- **schema**: add TopologyCustomerSaas, reverse owner->segments lookup
- **orchestrator-routing**: finish infrastructure/application delivery routing
- **segment**: add stretch-aware VXLAN deployments and app demos
- **topology**: rework office customer model and relocate controller schema
- **transform**: generate vendor payloads for deployed controllers
- **demo**: add DC11/DC12 controller-managed DCs, give ManagedController its own HFID
- **controller**: route fabric/firewall/load-balancer devices to a ManagedController
- **schema**: add granular management_mode choices to TopologyDataCenter
- **schema**: add namespace_type to IpamNamespace
- **dc**: auto-provision shared production/non-production virtual firewall/LB instances
- **generator**: integrate customer deployment exchange flow
- **topology**: add common exchange and demo16 stretched vxlan import fixes
- **quotation**: split dc/office generators and add campus design output
- **generator**: finalize dynamic interface template generation
- **quotation**: add CustomerQuotation DC-fabric sizing/pricing generator
- **naming**: add 4th "computed" strategy, short role codes, fix fab-index bug
- **dc-design**: add hyper-spine tier, consolidate designs to pure S/M/L/XL sizes
- **border-spine**: add collapsed spine+border-leaf role for micro-fabrics
- **dc**: replace fully_managed boolean with management_mode dropdown
- **16_hub_and_spoke**: colo controllers (WLC + SD-WAN gateway) + office APs
- **controller**: ManagedController generic for physical/virtual controllers
- **organization**: device ownership, provider/customer owner unification, drop os_version
- **topology**: move TopologyConnectableLocation to customer footprints
- **topology**: 16_hub_and_spoke demo — multi-tenant isolation, real shared services, VirtCluster reparenting
- **topology**: hub-and-spoke exchange model — SHARED-SERVICES/INTERNET namespaces, firewall contexts, customer offices
- **demos**: move C001-C003 boarding onto DC6, share servers across customers
- **topology**: add TopologyExchangeGateway for inter-namespace traffic exchange
- **organization**: add customer profile schema and boarding generator
- **data**: add software images for all platforms, wire into templates
- **demos**: add network segment and server sample data for c001-c003
- **rack**: move firewall/load-balancer provisioning into rack generator
- **endpoint**: LAG/MLAG-attached server bonds + resource lock helper
- **events**: trigger pod_rack_cascade on mlag_create change
- **rack**: auto-create MLAG domains for paired ToR/leaf devices
- **generators**: add bootstrap/cascade split with wait-for-parent guards
- **application**: refactor app rule and port generation workflow
- **security**: add SecurityIntent model and materialization pipeline
- **data**: assign security profiles in app catalogue
- **application**: introduce explicit app security profiles
- **application**: add fqdn/ingress_mode to AppApplication, fix sync .add() usage
- **demo**: use access-leaf instead of tor in 03_rack demo fixture
- **topology**: add access-leaf hardware templates
- **topology**: add access-leaf role — routed VTEP below leafs
- **checks**: validate BGP/OSPF routing password is set; fix l2-leaf P2P IP allocation
- **config**: add l2-leaf config artifact pipeline
- **routing**: create ManagedOSPFPeering, dedup with RoutingOSPFInterface
- **routing**: generate shared BGP/OSPF auth keys and wire into underlay/overlay peerings
- add crossplane-bridge service — Infrahub approval gate for ACP XR lifecycle
- add SaaS service capability data to 100 demo
- add ManagedSaasService generic and concrete SaaS capability nodes
- move subnet sizes to DataCenterDesign and use protocol-native prefix lengths
- AppInstance generic — PhysicalDevice, VirtualDevice, CloudInstance inherit from it; remove backend_port from AppComponent
- application catalogue — l2-leaf, MLAG/HA generators, service ports, software images
- webhook-driven deployment orchestration with PC comment threads

### Fix

- **app**: scope dependencies to applications
- **app**: key cleanup by application payload
- **app**: scope policy cleanup per application
- **app**: retain generated policy cleanup
- **app**: preserve shared enforcement references
- **app**: retain referenced enforcement nodes
- **app**: link checkout request components
- **app**: target materialized applications
- **app**: link checkout request dependency
- **app**: attach request dependency intent
- **routing**: sequence pod and rack bootstrap
- **routing**: sequence DC and pod bootstrap
- **app**: attach request dependencies to request
- **app**: discover request dependencies
- **app**: reconcile component dependencies
- **app**: target approved checkout request
- **app**: normalize request environments
- **app**: use customer identity for proxy policies
- **app**: validate request target owner hierarchy
- **app**: query request children through generic hierarchy
- **app**: validate endpoint query hierarchy
- **mlag**: always create/save full desired state instead of retrying
- **mlag**: re-track unchanged peer-link nodes so they survive cleanup
- **mlag**: inline peer-link wiring into MLAGWiringMixin, drop create-trigger
- **pools**: drop invalid identifier field from IpamPrefix data payload
- **topology**: set parent: DC on all data center topology definitions
- **rack**: mix PoolMixin into RackGenerator
- **ha**: find virtual devices' HA sync interface via generic DcimInterface kind
- **routing**: allocate eBGP underlay ASN per group, not per device
- **lb**: drop stale vlan_id query on ManagedSegmentDeployment.segment_deployments
- **ha**: pass known member_ids for freshly-created HA domains, skip capabilities.fetch()
- **generators**: log Ensured (not Created) for always-upsert FirewallContext
- **generators**: log Updated (not Created) for pre-existing devices/loopbacks
- **generators**: wait for in-flight add_dc before reading segment deployment vlan_pool
- **pools**: make FW-context P2P pool creation idempotent; fix dedicated firewall uplink role
- **generators**: wait for in-flight add_dc before reading firewall/LB devices
- **generators**: shorten dedicated FirewallContext/HA instance names
- **events**: restore missing customer-deployment triggers; add process_role attribute; dedicated generator groups
- **generator**: self-heal firewall HA pairing for customer FirewallContext
- **templates**: render address-family-aware IP commands in firewall templates
- **pools**: allocate FW-context P2P pool from global bootstrap pool, IPv6-first
- **generator**: FirewallContext provisioning failures are hard errors
- **generator**: create FirewallContext sub-interface on every HA-pair firewall
- **pbr**: match dedicated FirewallContext by deployment, not org id
- **pbr**: scope FirewallContext lookup to the device's own DC via graph traversal
- **generator**: create IpamIPAddress node for p2p legs instead of inline dict
- add DcimInterface role=service enum, fix border-leaf FW-facing role
- **generator**: resolve trunk interface via server-side role filter
- **generator**: use namespaced ManagedFirewallContext kind in pool upsert
- **events**: add missing created-trigger for add_vxlan_segment generator
- **schema**: simplify TopologyCustomer* human_friendly_id to name__value
- **demo-data**: distinguish C005's two DC deployments by environment
- **query**: add inline fragments for customer_deployments.parent in segment_placement
- **types**: resolve pre-existing ty-check diagnostics
- **data**: correct load order and YAML indentation in 30_all customer boarding
- **ha**: add DcimVirtualDevice fragment to ha_domain query; fix DC10 name mismatch
- **pod**: use dict indexing for DC's super-spine devices, not attribute access
- **generators**: store fixed pool sizes as protocol-agnostic host bits
- **generators**: pair firewall/load-balancer/MLAG devices for any quantity, inline DC-only mixin
- remove duplicate HA on-create triggers
- align generator tests and shared routing flow
- **generator**: use pair-unique pod mesh offsets to avoid endpoint collisions
- **generator**: harden group bootstrap and stabilize sibling uplink slotting
- **generator**: stabilize parent wait and inter-pod mesh cabling
- **ha**: preserve template interfaces in device upserts
- **generator**: propagate template owner to device upserts
- **topology**: use device_type relations in demo fabric templates
- **data**: restore bootstrap validation
- **topology**: switch network elements to device_type
- **smoke-test**: regenerate cabling fixture to match current query shape
- **border-spine**: add missing TopologyElement declarations
- **border-spine**: rename pod design to avoid DC/pod ID collision
- **demos**: reorder 16_hub_and_spoke load sequence to match dependency graph
- **routing**: close add_dc/add_pod visibility races, consolidate retry loops
- **bootstrap**: remove duplicate template_name entries shadowing real interfaces
- **cabling**: index-pair chain cabling instead of any-to-any mesh
- **lb**: give uplink/downlink 2 ports each, not 1
- **dc/pod**: fix pod spine template query, NetScaler HA port, inline LB port split
- **dc**: fresh-reset cabling mismatches, shared fabric loopback/ASN pools
- **dc**: surface an error when BLF<->FW/LB cabling produces no connections
- **bootstrap**: swap BIG-IP-i5800 uplink/customer interface roles
- **bootstrap**: rename stale max_border_leafs_per_pod field, bump caps to 2
- **dc**: retry-wait for pod loopback pool, read fabric ASN pool directly
- **pod**: don't auto-trigger dc_pod_cascade from add_pod
- **dc**: compute border-leaf spine offset from design capacity, not live cabling
- **queries**: use inline fragments for CoreObjectTemplate generic peer
- **controller**: deployment extension + DC9 pod/deployment references
- **query**: customer_dc_data owner.name needs inline fragments
- **bootstrap**: switch RoutingAutonomousSystem.owner to provider org_id
- **exchange_gateway**: support TopologyPhysicalCircuit and mandatory locations
- **demos**: move DC6 switch-add rack to row 2 (row 1 already occupied)
- **rack**: auto-fallback virtual MLAG to back-to-back for L2-only roles
- **demos**: let ExchangeGatewayGenerator provision customer VRF namespaces
- **demos**: rewrite customer segments onto current schema, fix vrf pool bug, wire mlag
- **bgp**: migrate external partner peering to ManagedBGPPeering, fix edge rendering
- **query**: drop stale ip_namespace.owner and generic inline_service refs
- **application**: rewire AppApplication.owner and application.gql after Portfolio removal
- **application**: repair broken repo import after Portfolio removal
- **templates**: rebalance Edgecore border-leaf, add dci role to DCI-capable edges
- **rack**: fix pod-pool ValueError, remove FW/LB border-leaf cabling for now
- **common**: retry parent pool lookup to close add_pod/add_dc pool race
- **demos**: let VrfNamespaceGenerator allocate l3_vni instead of hardcoding it
- **events**: remove endpoint trigger that fired on every device's interfaces
- **rack**: harden pod/row readiness checks against generator-trigger races
- **common**: stop stale-lock check from 500ing under real contention
- **endpoint**: re-touch already-cabled LAG bonds instead of skipping
- **endpoint**: always track device, fail loud on unwirable LAG bonds
- **mlag,cabling**: stop double-invocation loop and interface deletion
- **rack**: break row-dependent-rack deadlock and MLAG group membership
- **rack**: query mlag-peer template interfaces alongside uplink
- **rack,schema**: fix border-leaf uplink sizing and add device delete cascade
- **queries**: remove leftover checksum field references after cascade removal
- **generators**: guard fabric_templates role vs deployment_type; treat compute racks as row-dependent like tor
- **demo**: move 03_rack scenario off row_index 3 to avoid colliding with dc6 border-leaf rack
- **demo**: use access-leaf instead of tor in 04_pod middle_rack scenario
- **schema**: enforce SecurityPolicyRule name uniqueness per policy at the DB level
- **graphql**: remove redundant inline type fragments breaking cardinality-one fields
- **transform**: harden and simplify BGP local_as handling
- **graphql**: inline topology query fragment dependencies
- **graphql**: inline rack and pod fragment dependencies
- **graphql**: make endpoint connectivity query self-contained
- **graphql**: align interface capability fragment usage and smoke coverage
- **graphql**: resolve interface capability fragment imports in config queries
- **infrahub**: load graphql fragments from queries directory
- **queries**: remove deprecated security_policies from segment queries
- **security**: simplify intent model and harden rule reconciliation
- **generator**: support HA sync for virtual firewalls
- **data**: align app security triggers and dependency groups
- **demo**: repair DC2 OSPF peerings load order and app_dependencies group
- **generators**: always re-allocate DC pools instead of skipping when present
- **generators**: fix double-wrapped hfid in pool-attach relationship refs
- **config**: render interface description/status/mtu in SONiC JSON template
- **dc**: resolve existing pool refs to id before passing to create_devices
- **routing**: retry peering save on cross-call NODE_NOT_FOUND write race
- **rack**: use live ToR count for border-leaf cabling offset in tor deployments
- **routing**: pre-seed spine overlay BGP in pod.py, not dc.py, for back-to-back
- **routing**: fetch existing overlay as ManagedBGP, not ManagedOSPF, for ospf-ibgp
- **routing**: move back-to-back inter-pod spine cabling to dc.py
- **routing**: seed spine overlay BGP for OSPF_IBGP pods with zero super-spines
- **routing**: reference already-existing overlay BGP process by id, not HFID
- **tests**: correct DC2/DC3/DC4 expected device counts — no super-spine data
- **checks**: disable validate_management_services in leaf/border-leaf checks
- **routing**: use caller-known device role instead of querying DcimDevice.role
- **routing**: use actual racks-per-row for ToR cabling offset, not design max
- **bgp**: resolve remote ASN via bgp_processes.capabilities, not .device
- **routing**: resolve HFID refs to in-plan processes before saving peerings
- **schema**: drop ManagedGeneric from ManagedCloudProxyHA; fix saas_service comment
- update webex and zscaler to saas_services (cardinality many)
- add mandatory parent and region_name to SaasRegion upsert blocks
- use computed name for TopologySaasRegion HFID resolution
- anchor SaaS service capabilities at region level
- add human_friendly_id to ManagedProxyService generic
- remove proxy_service references and stale BCN branch capabilities
- use ManagedProxyService as proxy_service peer — accepts ProxyHA and CloudProxyHA, not all generics
- revert proxy_service peer to ManagedGeneric — ManagedProxyHA does not inherit ManagedSaasService
- generics cannot use inherit_from — move ManagedGeneric to concrete nodes
- remove prefix length fields from DC demo topology files
- correct ha interface roles in device templates and guard object_template re-application
- parse device name from BGP process name instead of inbound relationship
- correct BL offset formula and add N9K-C9364C-GX border-leaf template
- reserve border-leaf uplinks for DCI/super-spine before pod-spine cabling
- add missing pod.index to bl_indexes in border-leaf device naming
- remove M_TOR and L_TOR designs — spine port count infeasible at that scale
- restore M_TOR, M_MIXED, L_TOR, L_MIXED pod designs and add max_border_leafs_per_pod
- correct border-leaf cabling offsets and remove redundant topology fields
- remove redundant cloud_capabilities — CloudInstance uses capabilities via DcimCapabilities inheritance
- replace backend_port/capabilities/cluster_capabilities with instances in all component data files
- move AppInstance to base schema; update app_component query and generator to use instances/service_ports
- resolve DcimCable endpoint conflicts across DC1/DC2/DC3
- remove outdated DC1 cabling file — superseded by 10_cabling.yml with correct port names
- remove profile references from capabilities files — profiles not defined
- remove sync_group from DC3 LB data — field not in schema
- app_service_ports query — vip_service is cardinality-one
- complete menu — HA domains, security tags and proxy policies
- menu cleanup and security schema menu control
- yamllint errors and upgrade uv.lock

### Refactor

- **app**: make endpoints own external semantics
- **app**: model endpoint intent and service assignments
- **mlag**: reuse create_devices()'s own device batch, drop re-fetch
- **pools**: replace raw from_pool Upsert mutations with plain SDK create()
- **ha**: fold add_ha generator into _ensure_ha_pairs, fully idempotent
- **generators**: split customer_deployment.py per-kind, rename cabling.py, share VLAN sub-interface helper
- **topology**: consolidate customer VRFs into the built-in default namespace
- **demo-data**: nest VLAN/VXLAN segments under OrganizationCustomer
- **demo-data**: move VLAN segment creation into 07_applications/*/00_segment.yml
- **schema**: replace NetworkSegment.owner with derived owner_org_id
- **generator**: split add_customer_deployment_exchange into four
- **tasks**: drop dev_/infra_/data_ prefixes on task functions
- **tasks**: consolidate tasks/ package into a single tasks.py
- **generators**: move border-leaf/spine to DC scope, fix routing gate, fix pool sizing
- **generators**: remove Pydantic models, drop dead dynamic-interface fallback, fix mixin composition
- **topology**: simplify circuit connectivity model and align demo data
- **topology**: replace TopologyDataCenterDesign/TopologyPodDesign nodes
with size/layout/deployment_type Dropdowns
- **location**: split LocationBuilding into LocationFacility, LocationOffice,
and LocationCampus(+Building+Floor)
- **schema**: move naming_convention from DataCenter to TopologyDeviceHosting
- **naming**: reshape "standard" strategy — role code first, compact hierarchy
- **naming**: replace **kwargs with explicit DeviceNameContext
- **naming**: fix two variable-naming inconsistencies
- **checks**: extract BaseDeviceCheck, collapse 8 near-identical checks
- **transforms**: remove dead legacy cable-extraction fallback
- **routing**: convert dataclass containers to Pydantic BaseModel
- **generators**: replace Any with a Protocol for RoutingOptions.design
- **generators**: rename cabling_mixin.py to cabling.py
- **generators**: split CommonGenerator into responsibility-scoped mixins
- **generators**: dedup DC-wide and pod-scoped firewall/LB provisioning
- **generators**: remove dead defensive guards backed by mandatory fields
- **demos**: split 16_hub_and_spoke into one-topic-per-file, phased layout
- **common**: promote chain cabling from dc.py to CommonGenerator
- **dc**: collapse BLF/FW/LB cabling into a generic chain helper
- **fw/lb**: unify inline chain on uplink/downlink port roles
- **dc**: rework border-leaf/firewall/load-balancer placement and cabling
- **topology**: replace scalar spine/super-spine fields with fabric_templates
- **schema**: remove ManagedExternalFabricPeering/ManagedExternalPeer/ManagedExternalEndpoint
- **ipam**: remove owner/status from IpamNamespace, derive from segment
- **generators**: replace checksum-cascade with direct parent-to-child generator fan-out
- **transform**: simplify BGP router_id handling; derive OSPF reference-bandwidth from real interface speed
- **transform**: simplify schema-driven helper logic and align tests
- **query**: consolidate shared graphql fragments and simplify app generation flow
- **security**: retire intent pipeline and harden app policy checks
- **security**: simplify app-vip mappings and segment policy model
- **generators**: move generators/add/* into generators/topology/, extract RackMixin
- **generators**: extract shared helpers, tighten BGP/management transforms
- **generators**: extract rack helpers, jitter retry backoff, fix upsert/mutable-default bugs
- **topology**: remove overlay_technology, derive Pod deployment_type from PodDesign
- **routing**: decentralize back-to-back mesh cabling into pod.py
- **routing**: make ManagedOSPFPeering model one object per link, not per interface
- **bgp**: clean up dead peering fields; add shared RoutingPassword and wire BGP/OSPF security fields
- **schema**: move network segment gateway to ManagedNetworkSegment.gateway
- **schema**: rename ManagedCloudProxyHA → ManagedCloudProxy; drop ManagedHA; add deployment_model + region
- rename ZSCALER-SASE topology to ZSCALER
- collapse multi-service topologies and use cardinality-many saas_services
- drop regional suffix from TopologySaas names
- use real vendor region identifiers for SaaS regions
- move saas_service exclusively to TopologySaasRegion
- schema optimisation — remove redundancy and structural issues
- eliminate fabric-p2p tag dependency and simplify cabling/routing flow

## v0.2.1 (2026-06-20)

### Fix

- query validation, CI release flow, and bootstrap cleanup
- **query**: use inline fragment for name on ManagedGenericDevice in capability_guard

## v0.2.0 (2026-06-20)

### Feat

- universal topology, graph tracing, schema cleanup, and release tooling

## v0.1.5 (2025-12-10)

### Fix

- correct brace hierarchy in leaf.gql ServiceOSPF fragment

## v0.1.3 (2025-08-27)

## v0.1.2 (2025-08-06)

## v0.1.1 (2025-07-18)

## v0.1.0 (2025-05-08)
