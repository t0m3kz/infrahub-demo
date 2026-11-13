# Changelog

## v0.6.0 (2026-11-13)

### BREAKING CHANGE

- design is required on customer footprints; tier S_DC, M_DC, L_DC, XL_DC are S, M, L, XL;
max_segments moves to the TopologyCustomerTemplate generic. The schema and data need a rebuilt stack.
- segments carry ip_version; DC and colocation templates require segment_prefix_length,
ipv6_prefix_length and ipv6_segment_prefix_length; the schema and data need a rebuilt stack.
- segment_prefix requires a family; classifier_prefix is own_prefix.
- RoutingOSPFInterface loses its device Parent and DcimPhysicalDevice.ospf_interfaces; the schema needs
a rebuilt stack.
- DcimSubInterface loses inner_vlan_id; the schema needs a rebuilt stack.
- two generics and one attribute are removed; the schema and data need a rebuilt stack.
- TopologyExternalProviders is removed; schema and data need a rebuilt stack.
- ManagedSegmentVlan.svi and DcimVirtualInterface.segment_vlan are removed.
- ManagedOSPFPeering and TopologyRoutedExchange no longer inherit ManagedGenericInterfaces (no
capabilities edge); RoutingOSPFInterface requires device; ManagedFirewallContext gains exchanges. A rebuilt stack is
needed (inherit_from and a new Parent cannot be changed in place).
- a port's segment is read from its VLAN row; ManagedSegmentVlan's Parent is
its device; port memberships carry no edge source.
- TopologyCustomerDC.serving_firewall_context, TopologySegmentHosting.
internet_edge_contexts and ManagedFirewallContext.internet_edge_hostings are removed;
ManagedNetworkSegment.security_zone is required; `invoke check-model` and
utils/path_trace.py are gone (use `invoke test-integration-fast`).
- DcimSwitchedInterface.segments is removed; a port joins a segment through
the deployment's interface capabilities. ManagedSegmentVlan is no longer a device
capability.
- DcimCapabilities no longer exists; a query reads `capabilities` and
`shared_capabilities` through `... on DcimDeviceManaged`. Rebuild the stack.
- ManagedFirewallContext/ManagedLoadbalancerContext have no `cluster`,
ManagedFirewallHA/ManagedLoadbalancerHA no `contexts`; data names a context's
`deployment` and `shared_devices`. Rebuild the stack (inherit_from changed).
- ip_namespace no longer selects a routing table; schemas,
bootstrap data and demo data must be reloaded on a rebuilt stack.
- DcimPatchPanel requires panel_type and has no object template;
TemplateDcimPatchPanel and the PP-* templates are removed.
- the created node trigger rules are replaced by group trigger rules with new names (`...-on-added`); an instance loaded with the old rules keeps them until they are deleted, and would run each generator twice.
- the sample's KTW1 and PAR files moved to per-site folders, and the `pod` key of tests/traversal/cases.yml is now the list `pods`.
- names in the full-topology sample change; reload it on a fresh stack.
- a fresh Infrahub database is required; see Risk / follow-ups.
- the L3VNI SVI VLAN of existing devices is renumbered (for
example INTERNET 3900 -> 3902); NX-OS refuses to move an in-use vn-segment, so
rolling it out needs a config replace or a maintenance window.
- ManagedVRF no longer sits on the device capabilities edge,
host_interface and DcimInterface.capabilities for L2 segment membership are
removed, and physical and LAG addresses are no longer cascade-deleted with the
port.
- DcimDevice.vrfs and ManagedVRF.devices are removed; read
the memberships from DcimCapabilities.shared_capabilities.
- ManagedGenericVlanDomain, the row's vlan_domain and
ManagedVlanDomainSegment.svis are removed (device and svi replace them), and a
segment is no longer on the interface capabilities edge; rebuild rows and
inline terminations by re-running add_vxlan_segment.
- the patch-panel device role is removed; use DcimPatchPanel.
- CloudNetworkAppliance.service_code, package_code and
software_version are removed; set the device type, the software image and its
package instead. An appliance on a long-lived branch keeps its physical ports until
its template and appliances are reloaded.
- TopologyPhysicalCircuit, TopologyTunnelCircuit,
TopologyInterconnectCircuit, TopologyHandoffCircuit and TopologyVirtualCircuit
are removed (the group physical_circuits and the check validate_physical_circuit_ports
are renamed carrier_circuits and validate_carrier_circuit_ports); reload the
circuit data on existing branches.
- ManagedVlan, INFRA_VLAN_ID_MIN/MAX and the validate_vlan_domain_membership
check are removed.
- ManagedVlanSegment, ManagedExternalSegment and serving_zone are
removed; ManagedVxlanSegment.customer_deployments replaces customer_deployment.
- LoadbalancerVIP.component is removed and pool_members points at servers instead of addresses; reload the
schema and the data on a fresh branch.
- context, HA and dedicated device names change, LocationRack.pod becomes LocationRack.deployment,
TopologyVirtualColocation.devices becomes appliances; load the schema and the data on a fresh branch.
- DcimVirtualInterface.parent_interface peers DcimInterface and
has an inverse sub_interfaces; mtu is declared on DcimInterface; the virtual
firewall and load balancer templates create DcimVirtualInterface NICs. Reload the
schema and the bootstrap data on a fresh instance.
- schema attributes, choices, kinds and platform names listed above are removed; DcimPlatform
carries only name and containerlab_os.
- every OrganizationCustomer must be a member of the customers group to receive a tag.
- ManagedGenericVlanDomain.vlan_pool is removed; ManagedVlanDomainSegment.vlan_id is written
explicitly instead of drawn from a pool.
- the colocation metro loses deployment_type, fabric_templates and the zone-level pools; the zone loses
deployment_type; colocation device names include the zone (eg-fr01 is now eg-fr201); virtual colocation functions are
CloudNetworkAppliance, not DcimVirtualDevice; generator target groups change kind. Reload a stack from scratch.
- TopologyExternal is removed; TopologyExternalProviders.parent is TopologyLabel.
- TopologyDataCenter.size and TopologyPod.layout are replaced by
design.
- locations moved from TopologyCircuit onto the physical and virtual
circuit nodes; the four attributes above are gone.
- ManagedLoadbalancerHA.tenant, LoadbalancerVIP.snat_enabled and
backend_segment are removed; LoadbalancerVIP load_balancer is a context.
- ManagedNetworkSegment.security_tag is removed; tags now belong to AppComponent.
- cloud dependencies no longer generate sg-{app} CloudSecurityGroup objects; they produce SecurityPolicyRules like every other flow.
- SecurityPolicyRule.source_zone/destination_zone,
SecurityZone.rules_as_source/rules_as_dest and ManagedHA.inline_vlan_id/
inline_vlan_pool are removed (state: absent); set zones on segments.
- ManagedNetworkSegment.security_policies is removed
(state: absent) in favour of security_policy (cardinality one); a segment
that held several policies keeps none and must be given its single
egress policy.
- SecurityTagRule, SecurityZone.zone_type and the
SecuritySecurityProfile *_enabled flags are removed; SecurityPolicyRule
needs source_segment and lives in that segment's policy; SecurityPolicy no
longer has capabilities.

### Feat

- change requests created by Infrahub, kicks for demo and integration deployments (#285)
- tiered customer prefix pools, IPv6 segments and interface encapsulation (#282)
- **schema**: a port is a member of its switch's VLAN row, the deployment holds the request
- **schema**: a footprint lists all its firewall contexts, every segment names its zone; the traversal suite goes
- **transform**: a rule from the internet into a segment renders on the firewall that holds the internet rules
- **schema**: a port is a member of a segment deployment, the VLAN row keeps a plain device
- **schema**: the VLAN row is a device capability, switches render every row, border leaves carry stretched segments L2 only
- **schema**: PBR is a device capability, a VRF lists its interfaces, contexts lose the interface edge
- **schema**: ManagedMLAG lists the interfaces it configures
- **schema**: a sub-interface states its VLAN tag in vlan_id, nothing parses it from the name
- **schema**: service contexts are shared capabilities of their member devices
- firewall steering and static routing as device capabilities, Internet edge with external firewall pairs (#276)
- **check**: the routed prefixes of two on-prem segments never overlap in a namespace (#275)
- BGP routing policies as data, rendered on every platform, with generated transit policies (#274)
- **l2**: shared VLAN scope, a stretched segment on every VTEP, an L2 tracer layer, office Wi-Fi and firewall policy (#272)
- **schema**: provider connects, the provider's ports, and one software image model (#270)
- **schema**: colocation interface, cage internet access and a repeatable sample load (#269)
- **schema**: provider port generic, colocation connect and clusters menu (#268)
- clusters, virtual switches and SDNs (#267)
- office campus and a second office, far-end reachability, sarcastic demo READMEs (#266)
- cloud spoke accounts, circuit names by their ends, a topology diagram and patch panels built from their type (#265)
- **data**: the full topology spans three DCs and three cages over a DCI mesh, with VMs in one application (#262)
- **data**: full topology with three sites (Katowice DC, Paris colocation, Barcelona office), paths to the Internet and the cloud, organised like 30_all (#261)
- route protection, flat patch panel, port-side segments edge, universal cloud and SaaS connection model, THE INTERNET (#259)
- **menu**: VRF instances under Routing, Network Services replaces Services (#257)
- **schema,generator,transform,check**: VRF per topology, VLAN rows per device, patch panel, two circuit kinds, provider-hosted appliances (#255)
- **schema,transform,check**: ManagedVlan, an infrastructure VLAN with a fixed ID in a VLAN domain (#252)
- **schema,generator,check**: VXLAN is the only segment kind, internet is a zone, colocation is served by every firewall zone of its metro (#251)
- **schema,generator,transform**: cloud load balancer at layer 4 and 7, pool members are servers (#250)
- **schema,generator**: per-namespace service contexts, typed hosting links, LocationRack.deployment, LB context from the segment (#248)
- **schema,generator**: a device is its own VLAN domain, the VLAN row sits under the segment deployment, the segment owns its port tags (#247)
- **schema,generator,check**: pin virtual machine NICs to the ports of their hosting device (#243)
- **schema,generator,transform**: routing identity, customer and segment tags rendered as communities (#240)
- **schema,generator,check**: register the destinations C001's egress policy names (#238)
- **schema,generator,check**: restore the suite layout, add cable id, colour and length, README badges and Apache-2.0 (#234)
- **schema,generator**: virtual circuits as the routing adjacency between topologies (#232)
- **schema,generator**: virtual colocation, per-zone colocation kit and Equinix Terraform artifact (#231)
- **schema,generator**: fabric designs as data, pool sizes derived from the envelope (#225)
- **schema,generator**: circuits terminate on facilities, locations derived and checked (#224)
- **schema,generator**: load-balancer context in one routing table per environment (#223)
- tenant L3 on SONiC, fatal cabling errors, integration helper fixes (#220)
- SD-Access style LISP for offices, WAN exit, capabilities cleanup (#218)
- **schema,generator**: office fabric generator (SD-Access style, input is one fabric_templates line)
- **template**: IOS-XE SVI ACLs and VRF static routes on cisco_xe (#216)
- **data**: Arista EOS MSS-G reference-fabric scope with its own integration suite (#217)
- neutral overlay model, IOS-XE EVPN platform, and a LISP overlay (overlay_control_plane) (#214)
- **schema**: Arista MSS-G renderer, per-DC enforcement switch, vPC / SR OS fixes, remove RouteLeakExchange (#212)
- **template**: classify GPO component groups by VLAN and host port; enforce DC10 PROD (D2) (#211)
- **template**: render NX-OS VXLAN GPO on reference-fabric VTEPs (D1) (#208)
- **data**: DC10 is a reference fabric - GX and FX3 VTEPs on NX-OS 10.6(3)F (#207)
- **schema**: every routed tenant segment gets its own group tag; default-deny contracts from the segment rules (#203)
- **schema**: component group tags and a data-driven reference group-policy profile (NX-OS GPO, Arista MSS-G) (#202)
- filtered eBGP from the colo edge into cloud and SD-WAN, per VRF (#199)
- **schema**: office LAN prefixes registered in IPAM (#198)
- **data**: cloud VPC aggregates registered in their environment's VRF (#196)
- **check**: every PBR firewall context has a border or edge carrying its tenant-VRF leg (#194)
- **check**: routing invariants between topologies (#189)
- **transform**: render cloud security groups from policy rules (#187)
- **generator**: cloud flows are SecurityPolicyRules (#185)
- **data**: demo segments get a gateway and prefix so their rules stop rendering as any (#182)
- **check**: segment security invariants for all segment kinds (#181)
- colocation firewall contexts get their transit legs back, rendered by the edge (#178)
- metro edge pair with a fabric link and a second Multi-Site BGW (#177)
- MANAGEMENT as a third exchange peer (tenant-initiated, NAT behind the leg VIP, DC-local) (#175)
- SNAT on the firewall INTERNET leg, return route to the SNAT /32, public aggregate on the edge (#173)
- INTERNET as an L3VNI VRF on the metro BGW, default exported as type-5, transit filters (#171)
- internet egress rules, and leaf ACLs finally carry their segment's rules (#169)
- inter-VRF exchange legs through firewall contexts, no PBR on the border leaf (#168)
- inline segment termination on HA pairs, segment-owned security policies, parallel DC integration suite (#164)

### Fix

- a rule with a prefix or address selector names its zone, the edge default is not exported to EVPN
- **data**: the 30_all interfaces with an authored address name their VRF through the interface edge
- **data**: name each nested LAG of a segment deployment with its lag_id, read circuit names through ManagedGeneric
- **generator**: read the legs' VRF zone explicitly when reusing a transit prefix
- **query**: select every field once per object, the server answers only the first
- **data,query**: 00_full_topology renders on every device that has a template
- **data**: interfaces, circuits and exchanges name their routing VRF, not a security zone
- **data**: IPAM prefixes carry no security zone, the routing owner holds it
- **query**: routing and segment-security checks read the SD-WAN gateway's interfaces through DcimDevice
- **query**: edge reads the SD-WAN gateway's interfaces through DcimDevice
- **query**: shared fragments are resolved from the operation, move the local ones out
- the shared_vlan query and an empty YAML document break the sync and the 30_all load (#273)
- **query**: read software_image through DcimSoftwareRunner, not DcimDeviceManaged (#271)
- **events**: generators start when an object joins their target group, and panels declare patch_panels themselves (#263)
- **data**: racks nested in a pod carry their name too (#258)
- **schema,generator,check**: interface parents on one device (common_parent), the NIC pin is host_interface, unique order_weight (#244)
- **query,check**: render a config artifact for dedicated virtual firewalls (#242)
- **template,generator**: NX-OS and EOS structural correctness, a golden lint, one neutral MTU (#239)
- **transform,check**: filter external BGP sessions per owner, same-owner exchange, appliances render (#235)
- hardening sweep (external segment name, border-spine pods, config correctness, circuit and LB follow-ups) (#230)
- **menu**: add the fabric (DC and pod) templates to the Design menu (#228)
- **schema,data**: drop the generated CLOUD/SAAS label suffix and the redundant TopologyExternal level (#227)
- **query**: pod-tier VTEPs read the fabric evpn_rt_as (spread the EVPN fragment first) (#215)
- SD-WAN hand-off key, deterministic trunk, controller attach race, EOS field-set placement (#213)
- **transform**: a border gateway's hostless stretched segments carry no ACL or PBR input (#210)
- write-path races, dual-stack prefix lengths, border-spine parity, hyper-spine RR, SONiC VLAN_MEMBER (#206)
- **transform**: a host port carrying several segments renders as a trunk of all of them (#200)
- **template**: NX-OS emits feature bfd when a BGP neighbor uses bfd (#197)
- **template**: SR OS leaf binds the segment filter on every VLAN's SAP and refuses VLAN-only segment ACLs (#195)
- **transform**: leaf PBR to the firewall VIP gets a local transit leg (#193)
- **template**: NX-OS PBR bypass uses a permit-only ACL and a deny route-map sequence (#192)
- **template**: valid PBR on NX-OS/EOS, drop unsupported GPO/CTS stanzas, guard unprotected tenant SVIs (#190)
- **generator**: prod/non-prod guard on segment namespace type (#180)
- treat the exchange peers generically instead of assuming INTERNET is the only one (#174)
- keep customer and firewall-context VLANs apart, stop deleting namespaces with their exchange (#166)
- deterministic firewall cluster, cable-paired PBR context legs, ManagedExternalSegment (#165)

### Refactor

- firewall gateway flag, flattened queries, rules by id, linux edge template (#284)
- **schema**: remove TopologyExternalProviders, add provider reverse edges and a public-endpoint check (#280)
- **schema**: segment deployment and VLAN row labels read without @ (#279)
- **schema**: the segment SVI keeps no link to its row and carries the anycast gateway
- **schema**: OSPF peering and routed exchange off the interface edge, OSPF interface settings a child of their device
- **transform**: readers derive context legs, border ports and PBR bindings from the device
- **schema**: the device end of the capability edges is DcimDeviceManaged, DcimCapabilities is gone
- SecurityZone is the routing authority, IPAM uses the default namespace
- **schema,data**: suite and rack names are plain attributes, not computed (#256)
- drop invoke validate-queries, one rule for the VLAN domain of a device (#254)
- **generator,transform**: split modules over 1000 lines, cut redundant reads, allocate VLAN ids without per-device pools (#237)
- split group_policy, derive the layout prefixes, drop dead fragments and Pod.deployment_type (#221)
- **schema**: remove redundant model elements, enrich the DC customer tier, office without environment (#219)
- simplification and standardization sweep after minimum-data deployment (#205)
- **data**: minimum-data deployment - derive what the generators know (#204)
- **transform**: split the cloud VPC terraform transform by provider (#188)
- **transform**: one rule-view helper and the stateful/stateless contract (#186)
- **generator**: one zone helper for every segment kind (#184)
- one segment-prefix resolver for generators and transforms (#179)
- **template**: share the NX-OS switchport and VRF static route-map blocks (#176)
- colocation firewall contexts are policy-only, one leg path for DC (#170)

### Perf

- **generator**: batch SDK reads in the shared mixins (#226)

- remove dead code and unused schema, platform template key is platform.name, utils/limits.py (#241)

## v0.5.0 (2026-10-07)

### BREAKING CHANGE

- an eBGP overlay now requires fabric_rt_asn on the topology.
Fabrics using routing_strategy ebgp-ebgp or ebgp-ibgp without it previously
rendered per-VTEP route-targets and must set it before regenerating artifacts.
Rendered BGP configuration changes for every ebgp-ibgp device: one instance on
the overlay ASN with per-session local-as, replacing two stanzas.
- the CustomerQuotation* and TopologyInterconnectRequest
schema kinds no longer exist. Branches holding objects of these kinds must
delete them before loading this schema, and the add_quotation_dc,
add_quotation_office and add_interconnect generators are gone.
- AppServicePort and Application.service_ports are
removed; ports live on AppDependency. Existing "-return" policy rules are
deleted by the generator on its next run.
- AppEndpoint and AppExternalService are removed,
AppComponent.slug is replaced by fqdn, and the AppDependency.target peer
changed. The schema cannot be migrated in place; a fresh load is
required. Generated rule names change and old rules are cleaned up by
delete_unused_nodes on the first rerun.
- a ManagedVxlanSegment with no AppComponent wiring loses its
customer-port VLAN assignment on the next generator run. Every segment that
needs one must have an AppComponent naming it via network_segment with at
least one instance.
- AppComponent object-load data without a ports list will
be rejected by the schema.
- IpamNamespace.namespace_type choices transit/shared/customer
are removed (replaced by prod/non_prod/management); the TRANSIT namespace and
the orphaned data/segments/ per-customer-VRF fixture are deleted.
- AppDeploymentRequest, AppDeploymentRequestComponent,
AppDeploymentRequestEndpoint, and AppDeploymentRequestDependency no longer
exist. Anything that declared applications through that graph must author
AppApplication/AppComponent/AppEndpoint/AppDependency directly instead.

### Feat

- simplify VRF namespaces to 4 fixed globals, automate inter-topology interconnects (#159)
- automate ZTNA access profiles and DC-fabric security zones (#155)
- **application**: simplify application graph, remove DeploymentRequest duplicate schema (#153)

### Fix

- **evpn**: EVPN correctness, segment-anchored security, AppComponent model, generator ownership, DCI from circuits (#160)
- **generators**: publish private_access endpoints via private_access_service
- **colocation**: drop uncablable service entries and dedup border-service device creation (#152)

### Refactor

- **generator**: split AppApplicationGenerator into domain mixins (#157)

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
