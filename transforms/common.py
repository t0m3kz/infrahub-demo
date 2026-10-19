"""Base class and shared helpers for Infrahub device transforms."""

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Template
from netutils.utils import jinja2_convenience_function

from transforms.helpers.acl import get_acls
from transforms.helpers.bgp import get_bgp_profile
from transforms.helpers.cisco_xe import get_cisco_xe_acls, get_cisco_xe_vrf_routes, refuse_unfiltered_ipv6
from transforms.helpers.firewall import (
    _flatten_deployment_firewall_contexts,
    _service_context_caps,
    get_customer_pbr_rules,
    get_exchange_routes,
    get_exchange_transits,
    get_leaf_firewall_transits,
)
from transforms.helpers.group_policy import GPO_CONFIG_KEYS, group_policy_device_view
from transforms.helpers.lisp import build_lisp_inputs, get_lisp_config
from transforms.helpers.mlag import get_mlag, get_sonic_mlag_config
from transforms.helpers.ospf import get_ospf
from transforms.helpers.overlay import OVERLAY_CONFIG_KEYS, OVERLAY_RENDERERS
from transforms.helpers.segments import (
    get_vlans,
    routed_activations,
    segment_hosting_candidates,
)
from transforms.helpers.templates import load_template
from transforms.helpers.vxlan import get_interfaces, get_vxlan_config
from utils.data_cleaning import clean_data
from utils.fabric_roles import BORDER_ROLES, OFFICE_LISP_ROLES


def _fabric_rt_asn(deployment: Any) -> int | None:
    """Return the fabric-wide EVPN route-target admin ASN, if the fabric has one.

    Reads ``TopologySegmentHosting.evpn_rt_as`` off the device's own deployment.
    Every VTEP in a fabric resolves the same value here, which is the whole
    point: route-targets must match fabric-wide or VTEPs never import each
    other's routes. Returns None when unset, and get_vxlan_config falls back to
    the overlay process ASN.
    """
    for candidate in segment_hosting_candidates(deployment):
        asn = (candidate.get("evpn_rt_as") or {}).get("asn")
        if isinstance(asn, int):
            return asn
    return None


def _fabric_anycast_mac(deployment: Any) -> str | None:
    """Return the fabric-wide anycast-gateway MAC, if the fabric sets one.

    Reads ``TopologySegmentHosting.evpn_anycast_gateway_mac``. Same fabric-wide
    resolution as _fabric_rt_asn and for the same reason: a host caches its
    gateway MAC, so two VTEPs answering with different MACs blackhole traffic on
    whichever one did not answer. None means get_vxlan_config uses its default.
    """
    for candidate in segment_hosting_candidates(deployment):
        mac = candidate.get("evpn_anycast_gateway_mac")
        if isinstance(mac, str) and mac.strip():
            return mac.strip()
    return None


def get_capabilities(data: dict[str, Any]) -> dict[str, Any]:
    """Derive device capabilities from services.

    Capabilities are derived from device_capabilities (BGP/OSPF presence).

    Args:
        data: Device data from GraphQL query (after clean_data)

    Returns:
        Dict with capability flags for template rendering.
    """
    typenames = {s.get("typename") for s in data.get("capabilities") or []}
    return {
        "bgp_enabled": "ManagedBGP" in typenames,
        "ospf_enabled": "ManagedOSPF" in typenames,
    }


def _loopback_name(interfaces: list[dict[str, Any]], platform_name: str) -> str:
    """The loopback the templates source BGP updates and redistribution from.

    The first interface named "loopback" (any case), or with a name of 14+
    characters starting with it, else the platform's Loopback0 spelling.
    Shorter names such as "Loopback1" never match, so devices get Loopback0.
    """
    for iface in interfaces:
        name = iface["name"].lower()
        if (name if len(name) <= 13 else name[:8]) == "loopback":
            return iface["name"]
    return "loopback0" if platform_name == "cisco_nxos" else "Loopback0"


def _gpo_host_vlans(group_policy: dict[str, Any] | None) -> dict[str, int] | None:
    """Host port -> the VLAN to make its native VLAN (``get_interfaces``), or None when there is none.

    A host port a component selector names must be a trunk (spec 3.6: interface+VLAN selectors are
    documented for trunk ports only): the one mode rule (get_interfaces) trunks the access ones with
    the segment's VLAN as native VLAN. ``group_policy`` is the device view, None on a non-GPO device.
    """
    host_vlans = {
        port["name"]: port["vlan_id"]
        for group in (group_policy or {}).get("groups", [])
        for port in group["interfaces"]
    }
    return host_vlans or None


class BaseDeviceTransform(InfrahubTransform):
    """Base class for device configuration transforms.

    Eliminates boilerplate shared across device transforms by handling:
    - GraphQL data extraction and cleaning
    - Platform detection with null safety
    - Jinja2 environment setup with netutils filters
    - Standard config building (interfaces, BGP, OSPF)

    Subclasses set class attributes and optionally override ``_extra_config()``
    to add device-specific template variables (VLANs, VXLAN, etc.).

    Class attributes:
        template_subdir: Subdirectory under templates/configs/ for this device type.
        device_role: Role passed to get_vxlan_config (e.g. "spine", "leaf").
                     Set to "" to omit VXLAN from the template context.
    """

    template_subdir: str = ""
    device_role: str = ""
    # Whether the shared firewall context's tenant-VRF 0/0 -> VIP static is
    # redistributed into EVPN (see get_exchange_routes); Edge turns it off.
    export_tenant_default_to_evpn: bool = True
    # Comment character of the "no platform" stub config.
    comment_char: str = "!"
    # Resolve a VxlanSegment's local VLAN through this device's own VLAN domain.
    resolve_vlan_domain: bool = True

    def _device_activations(self, device: dict[str, Any]) -> list[dict[str, Any]]:
        """Segment activations of one device: from its interface capabilities, plus the
        stretched segments its multi-site border gateway carries."""
        interfaces = device.get("interfaces") or []
        capabilities = device.get("capabilities") or []
        activations = self._collect_activations_from_interfaces(
            interfaces, device_id=device.get("id"), device_capabilities=capabilities
        )
        if any(iface.get("role") == "multisite-vip" for iface in interfaces):
            activations.extend(
                self._collect_border_gateway_activations(
                    device.get("deployment"),
                    device_id=device.get("id"),
                    device_capabilities=capabilities,
                    seen={(act.get("segment") or {}).get("id") for act in activations},
                )
            )
        return activations

    async def transform(self, data: Any) -> Any:
        device_data, extra_roots, platform_name = self._device_and_platform(data)
        if not platform_name:
            return self._no_platform_config(device_data)

        activations = self._device_activations(device_data)
        if activations:
            device_data["segment_deployments"] = activations
        # Transit legs of the firewall and load-balancer contexts tagged on this
        # device's service ports: VLAN/VNI/VRF/anycast only, never segment policy input.
        device_data["exchange_transits"] = get_exchange_transits(_service_context_caps(device_data.get("interfaces")))

        config = self._template_context(device_data, platform_name, extra_roots)
        self._refuse_cisco_xe_unrendered(platform_name, config)
        config.update(self._cisco_xe_context(platform_name, config))
        return self._render(platform_name, config)

    # The roles rendered by a cisco_xe fabric template (leafs/, border_leafs/ and the office's
    # access_switches/, distribution_switches/); the Edge's cisco_xe template is IOS.
    _CISCO_XE_FABRIC_ROLES = frozenset({"leaf", "tor", "access-leaf", "border-leaf"}) | OFFICE_LISP_ROLES

    def _refuse_cisco_xe_unrendered(self, platform_name: str, config: dict) -> None:
        """Raise when a Catalyst 9000 (cisco_xe) fabric device needs what its EVPN-VXLAN templates do not render.

        Those templates render the overlay (EVPN-VXLAN or LISP), BGP, OSPF, the gateway SVIs, the segment ACLs and
        the inter-VRF exchange statics. Dropping PBR, MLAG or Multi-Site would ship an unprotected or incomplete
        config, so refuse instead, like SONiC and SR OS. What the ACLs and statics themselves cannot express is
        refused by ``_cisco_xe_context``.
        """
        if platform_name != "cisco_xe":
            return
        if self.device_role == "border-spine":
            # It shares the border_leafs/ template directory but is also a route reflector, which that template does not render.
            raise ValueError("cisco_xe border-spine cannot render the route-reflector role; refusing incomplete config")
        if self.device_role not in self._CISCO_XE_FABRIC_ROLES:
            return
        vxlan = config.get("vxlan") or {}
        unsupported = [key for key in ("leaf_pbr_rules", "mlag") if config.get(key)]
        if vxlan.get("multisite"):
            unsupported.append("multisite")
        if vxlan.get("enabled") and not (vxlan.get("evpn") or {}).get("enabled"):
            unsupported.append("VXLAN without an EVPN BGP process")
        lisp = config.get(OVERLAY_CONFIG_KEYS["lisp"])
        if lisp:
            # The LISP template defines the VRFs of its L3 instances; a BGP session (an office's WAN exit) in any
            # other VRF would render under a VRF the device does not have.
            session_vrfs = {vrf for bgp in config.get("bgp") or [] for vrf in bgp.get("vrf_sessions") or {}}
            undefined = sorted(session_vrfs - {l3["vrf"] for l3 in lisp.get("l3_instances") or []})
            if undefined:
                unsupported.append(f"a BGP session in VRF {', '.join(undefined)}, which no hosted segment defines")
        if unsupported:
            raise ValueError(
                f"cisco_xe {self.device_role} cannot render: {', '.join(unsupported)}; refusing incomplete config"
            )

    def _cisco_xe_context(self, platform_name: str, config: dict) -> dict[str, Any]:
        """The IOS-XE form of the segment ACLs and the VRF statics (``xe_acls``, ``xe_vrf_routes``).

        Empty for anything but a cisco_xe fabric role, so no other device's context changes. Raises ValueError
        for an ACL or static IOS-XE cannot express (see transforms/helpers/cisco_xe.py).
        """
        if platform_name != "cisco_xe" or self.device_role not in self._CISCO_XE_FABRIC_ROLES:
            return {}
        refuse_unfiltered_ipv6(config.get("vlans") or [], self.device_role)
        lisp = config.get(OVERLAY_CONFIG_KEYS["lisp"])
        defined_vrfs = (
            [l3["vrf"] for l3 in lisp["l3_instances"]]
            if lisp
            else [mapping["vrf_name"] for mapping in (config.get("vxlan") or {}).get("l3_vni_mappings", [])]
        )
        return {
            "xe_acls": get_cisco_xe_acls(config.get("acls"), self.device_role),
            "xe_vrf_routes": get_cisco_xe_vrf_routes(
                config.get("vrf_routes"), defined_vrfs, self.device_role, lisp=bool(lisp)
            ),
        }

    def _template_context(self, device_data: dict, platform_name: str, extra_roots: dict) -> dict:
        """The template context: the device-specific part (``_extra_config``), then the base part.

        The device-specific part goes first because the host ports of its group policy decide how the
        base part builds the interfaces (``_gpo_host_vlans``), which then run once. Neither part
        modifies ``device_data``, so the order does not change what either reads. ``_extra_config``
        wins on a key both set.
        """
        extra = self._extra_config(device_data, platform_name, extra_roots=extra_roots)
        config = self._build_config(device_data, platform_name, host_vlans=_gpo_host_vlans(extra.get("group_policy")))
        config.update(extra)
        return config

    @staticmethod
    def _device_and_platform(data: Any) -> tuple[dict, dict, str | None]:
        """Clean `data` and return (device, the other query roots, netmiko platform).

        The device node is always the first query root.
        """
        cleaned = clean_data(data)
        if not isinstance(cleaned, dict) or not cleaned:
            raise ValueError("clean_data() did not return a non-empty dictionary")
        first_key = next(iter(cleaned))
        first_value = cleaned[first_key]
        device = first_value[0] if isinstance(first_value, list) and first_value else (first_value or {})
        extra_roots = {k: v for k, v in cleaned.items() if k != first_key}
        return device, extra_roots, (device.get("platform") or {}).get("netmiko_device_type")

    def _no_platform_config(self, device: dict) -> str:
        """Placeholder config for a device without a netmiko platform."""
        c = self.comment_char
        device_name = device.get("name", "Unknown Device")
        return f"{c} Device {device_name} has no platform with netmiko_device_type defined.\n{c} No configuration generated.\n"

    def _render(self, platform_name: str, config: dict) -> str:
        return self._load_template(platform_name).render(**config)

    def _build_config(self, data: dict, platform_name: str, host_vlans: dict[str, int] | None = None) -> dict:
        """Build the base template context shared by all device transforms.

        ``host_vlans``: the NX-OS GPO host ports (``_gpo_host_vlans``), None on any other device.
        """
        interfaces = data.get("interfaces") or []
        device_capabilities = data.get("capabilities") or []
        device_name = data.get("name", "")
        mlag = get_mlag(device_capabilities, interfaces, device_name=device_name)
        template_interfaces = self._template_interfaces(data, host_vlans=host_vlans)
        config = {
            "name": device_name,
            "hostname": device_name,
            "device_role": data.get("role", ""),
            "interfaces": template_interfaces,
            "loopback_name": _loopback_name(template_interfaces, platform_name),
            "bgp": get_bgp_profile(
                device_capabilities,
                interfaces,
                device_name=device_name,
                device_role=data.get("role", ""),
            ),
            "ospf": get_ospf(device_capabilities, interfaces),
            "mlag": mlag,
            "capabilities": get_capabilities(data),
        }
        if platform_name in {"sonic", "dell_sonic"}:
            config["sonic_mlag"] = get_sonic_mlag_config(mlag)
        return config

    @staticmethod
    def _template_interfaces(data: dict, *, host_vlans: dict[str, int] | None = None) -> list[dict[str, Any]]:
        """The interfaces the templates render (get_interfaces), ``host_vlans`` being the NX-OS GPO host ports.

        The MLAG control SVI is rendered by the platform's MLAG include (it needs the peer-link
        VLAN/trunk group around it), not as a generic interface.
        """
        activations = (data.get("segment_deployments") or []) + (data.get("exchange_transits") or []) or None
        return get_interfaces(
            [iface for iface in data.get("interfaces") or [] if iface.get("role") != "mlag-control"],
            activations=activations,
            device_name=data.get("name", ""),
            native_vlans=host_vlans,
        )

    def _extra_config(self, data: dict, platform_name: str, extra_roots: dict | None = None) -> dict:
        """Return device-specific template variables.

        Default implementation adds VLANs, VXLAN config, ACLs, PBR rules and
        inter-VRF exchange routes when device_role is set.
        Override in subclasses for different behavior.
        """
        if not self.device_role:
            return {}
        activations = data.get("segment_deployments")
        # PBR and the zero-trust ACLs hang off the segment's SVI; a
        # terminate_inline segment has none here (its HA pair routes it).
        routed = routed_activations(activations)
        # A border gateway's stitched segments are hostless (no customer-facing port),
        # so its anycast SVI carries no tenant traffic: hosts are served by the leaf
        # that hosts them, where ACL + PBR apply. They stay in `activations` (VLAN, VNI,
        # VRF/L3VNI and the SVI are rendered) but are no input to the policy below.
        if self.device_role in BORDER_ROLES:
            routed = [act for act in routed if not act.get("border_gateway")]

        # Customer PBR: default-redirect to the firewall context serving this
        # segment's owner; a SecurityPolicyRule permit is the only bypass.
        # ManagedFirewallContext is reached via a device-scoped traversal —
        # this device's own `deployment` (queries/fragments/firewall_contexts.gql's
        # FirewallContextsOnDeploymentFields), not a global query root — no
        # leaf ever owns a FirewallContext interface itself, only the
        # firewall/border-leaf do. Computed before the VLANs: a leaf's PBR next
        # hops decide which transit legs it holds locally.
        leaf_pbr_rules, leaf_transits = self._firewall_pbr(data, routed)

        if self.device_role in self._LEAF_ROLES:
            # SONiC renders PBR (ConfigDB PBR ACL table); SR OS leafs do not.
            # Refuse rather than ship a VLAN whose firewall steering silently vanished.
            if platform_name == "nokia_sros" and leaf_pbr_rules:
                raise ValueError(f"{platform_name} leaf cannot render PBR policy; refusing unprotected config")

        # Exchange transits (see transform()) and the leaf's local transit legs: they
        # only add VLANs, VNIs, VRFs and the anycast gateway; policy (ACL, PBR) stays
        # on real segments. They are not bound to any host port (get_interfaces only
        # binds a transit VLAN to a port carrying the context).
        transits = (data.get("exchange_transits") or []) + leaf_transits
        vlans = get_vlans(activations=(activations or []) + transits)
        transit_by_vlan = {act["vlan_id"]: act for act in transits}
        for vlan in vlans if transits else []:
            if vlan["vlan_id"] in transit_by_vlan:
                # Explicit VNI and VRF L3 VNI: SR OS's IRB fallback would use the VLAN id.
                transit = transit_by_vlan[vlan["vlan_id"]]
                namespace = transit["segment"]["gateway"]["ip_prefix"]["ip_namespace"]
                vlan.update(vni=transit["vni"], l3_vni=namespace["l3_vni"])

        # Inter-VRF exchange routes through the firewall context(s) tagged on
        # this device's service ports (statics in the VRF, redistributed to EVPN).
        vrf_routes = get_exchange_routes(
            data.get("interfaces"), export_tenant_default=self.export_tenant_default_to_evpn
        )

        acls = get_acls(activations=routed)
        acl_names = {acl["vlan_id"]: acl["name"] for acl in acls}
        for vlan in vlans:
            vlan["acl_name"] = acl_names.get(vlan["vlan_id"])

        if self.device_role in BORDER_ROLES:
            self._refuse_unprotected_tenant_svis(self.device_role, platform_name, acls, leaf_pbr_rules)

        lisp = None
        if self.device_role in OFFICE_LISP_ROLES:
            # An office's access/distribution switch always renders LISP: its site has no other overlay.
            if platform_name not in OVERLAY_RENDERERS["lisp"]:
                raise ValueError(
                    f"{platform_name} {self.device_role} cannot render the LISP overlay; refusing incomplete config"
                )
            vxlan = None
            lisp = get_lisp_config(
                data,
                self.device_role,
                (activations or []) + transits,
                build_lisp_inputs(data.get("deployment"), self.device_role),
                fabric_anycast_mac=_fabric_anycast_mac(data.get("deployment")),
            )
        else:
            vxlan = get_vxlan_config(
                data,
                platform_name,
                device_role=self.device_role,
                activations=(activations or []) + transits,
                fabric_rt_asn=_fabric_rt_asn(data.get("deployment")),
                fabric_anycast_mac=_fabric_anycast_mac(data.get("deployment")),
            )
        if self.device_role in self._LEAF_ROLES:
            self._refuse_unbindable_sros_acls(platform_name, acls, vxlan)

        config = {
            "vlans": vlans,
            "sros_irbs": self._sros_irbs(self.device_role, platform_name, vlans, vxlan),
            "vxlan": vxlan,
            "acls": acls,
            "vrf_routes": vrf_routes,
            "leaf_pbr_rules": leaf_pbr_rules,
            # The leaf's own transit /29s: kept out of the direct-to-EVPN redistribution
            # so only the borders originate them as type-5 routes.
            "leaf_transit_prefixes": sorted({t["segment"]["gateway"]["ip_prefix"]["prefix"] for t in leaf_transits}),
        }
        if lisp is not None:
            config[OVERLAY_CONFIG_KEYS["lisp"]] = lisp
        # Group policy of a VTEP in a reference fabric, from the fleet roots of the device query:
        # NX-OS VXLAN GPO as `group_policy`, EOS MSS-G as `segment_security`. Absent for every other
        # device, so its templates render exactly as before.
        found = group_policy_device_view(
            extra_roots or {},
            device_name=data.get("name") or "",
            role=self.device_role,
            platform=platform_name,
            activations=activations or [],
            vrfs=(
                [l3["vrf"] for l3 in lisp["l3_instances"]]
                if lisp
                else [mapping["vrf_name"] for mapping in (vxlan or {}).get("l3_vni_mappings", [])]
            ),
            interfaces=data.get("interfaces") or [],
        )
        if found is not None:
            kind, view = found
            config[GPO_CONFIG_KEYS[kind]] = view
        return config

    _LEAF_ROLES = frozenset({"leaf", "tor", "access-leaf"})

    def _firewall_pbr(
        self, data: dict, routed: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Firewall PBR rules for the routed activations, plus the leaf's local transit legs.

        Only a leaf role holds transit legs (get_leaf_firewall_transits raises when a
        next hop has none); every other role gets no leaf transits.
        """
        firewall_contexts = _flatten_deployment_firewall_contexts(data.get("deployment"))
        rules = get_customer_pbr_rules(routed, firewall_contexts)
        if self.device_role not in self._LEAF_ROLES:
            return rules, []
        return rules, get_leaf_firewall_transits(rules, firewall_contexts, device_name=data.get("name", ""))

    @staticmethod
    def _refuse_unprotected_tenant_svis(
        role: str, platform_name: str, acls: list[dict[str, Any]], leaf_pbr_rules: list[dict[str, Any]]
    ) -> None:
        """Raise when a border leaf or border spine (``role``) hosts a routed tenant SVI it cannot protect.

        No border_leafs/* template renders PBR or the SVI ACL definitions (the NX-OS and
        EOS ones would even bind a name nobody defines), so the segments' policy would
        silently vanish and leave an any-to-any SVI inside the shared tenant VRF. Only
        host-capable activations reach here: a border gateway's hostless stitched
        segments are kept out of the policy inputs (see _extra_config).
        """
        if leaf_pbr_rules:
            raise ValueError(
                f"{platform_name} {role} cannot render PBR policy for VLANs "
                f"{sorted(rule['vlan_id'] for rule in leaf_pbr_rules)}; refusing unprotected config"
            )
        if acls:
            raise ValueError(
                f"{platform_name} {role} cannot render the SVI ACL for VLANs "
                f"{sorted(acl['vlan_id'] for acl in acls)}; refusing unprotected config"
            )

    @staticmethod
    def _refuse_unbindable_sros_acls(
        platform_name: str, acls: list[dict[str, Any]], vxlan: dict[str, Any] | None
    ) -> None:
        """Raise when an SR OS leaf holds a segment ACL for a VLAN it renders no service for.

        The SR OS leaf binds each ip-filter as a SAP ingress filter inside the vpls
        of the segment's L2 VNI. A VLAN-only segment (no VNI) has no vpls, and the
        template models no local L2 service, so its filter would stay unbound.
        """
        if platform_name != "nokia_sros" or not acls:
            return
        bound = {m["vlan_id"] for m in (vxlan or {}).get("l2_vni_mappings", [])}
        unbound = sorted(acl["vlan_id"] for acl in acls if acl["vlan_id"] not in bound)
        if unbound:
            raise ValueError(
                f"{platform_name} leaf cannot bind the segment ACL for VLANs {unbound} (no L2 VNI service "
                "to attach the SAP filter to); refusing unprotected config"
            )

    @staticmethod
    def _sros_irbs(
        role: str, platform_name: str, vlans: list[dict[str, Any]], vxlan: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """The SR OS IRB interfaces (anycast gateway) to render, one per gateway VLAN: [] on any other platform.

        An IRB attaches the VLAN's bridged ``vpls-<vni>`` to a vprn: ``vprn`` is the VRF's L3 VNI
        (the vprn service id), ``vni`` the L2 VNI (the vpls service id). get_vlans() carries no VNIs,
        so they come from the VXLAN mapping of the same VLAN (a transit VLAN sets both itself).
        A gateway VLAN with no L2 VNI has no vpls, and one whose namespace has no L3 VNI in the VXLAN
        mappings has no vprn (vprn services come only from ``l3_vni_mappings``): the IRB would reference a
        service nothing creates, so refuse instead of rendering it. Without VXLAN the template renders no
        service at all.
        """
        if platform_name != "nokia_sros" or not vxlan:
            return []
        mappings = {mapping["vlan_id"]: mapping for mapping in vxlan.get("l2_vni_mappings") or []}
        vprns = {mapping["l3_vni"] for mapping in vxlan.get("l3_vni_mappings") or []}
        irbs: list[dict[str, Any]] = []
        for vlan in vlans:
            if not (vlan.get("gateway_ip") or vlan.get("gateway_ipv6")):
                continue
            mapping = mappings.get(vlan["vlan_id"]) or {}
            vni = vlan.get("vni") or mapping.get("vni")
            vprn = vlan.get("l3_vni") or mapping.get("l3_vni")
            if not vni or vprn not in vprns:
                raise ValueError(
                    f"{platform_name} {role} cannot render the gateway of VLAN {vlan['vlan_id']} "
                    "(no vpls/vprn to attach the IRB to); refusing incomplete config"
                )
            irbs.append(
                {
                    "vlan_id": vlan["vlan_id"],
                    "vni": vni,
                    "vprn": vprn,
                    "gateway_ip": vlan.get("gateway_ip"),
                    "gateway_ipv6": vlan.get("gateway_ipv6"),
                }
            )
        return irbs

    _ACTIVE_STATUSES = ("active", "provisioning")

    @staticmethod
    def _resolve_own_vlan_domain_id(device_id: str | None, device_capabilities: list[dict]) -> str | None:
        """Return this device's own VLAN domain id: its ManagedMLAG's id if
        paired, else its ManagedStandaloneVlanDomain's id. Local VLAN ID is
        allocated per VLAN domain, not DC-wide — see ManagedVlanDomainSegment
        / generators/vlan_domain.py. Falls back to the device id when the
        device carries neither."""
        standalone_id: str | None = None
        for cap in device_capabilities:
            typename = cap.get("typename")
            if typename == "ManagedMLAG" and cap.get("id"):
                return cap["id"]
            if typename == "ManagedStandaloneVlanDomain" and cap.get("id"):
                standalone_id = cap["id"]
        return standalone_id or device_id

    @staticmethod
    def _own_vlan_domain_segment(segment: dict, own_domain_id: str | None) -> dict | None:
        """The segment's vlan_domain_segments entry for this device's own VLAN domain."""
        return next(
            (
                v
                for v in segment.get("vlan_domain_segments") or []
                if (v.get("vlan_domain") or {}).get("id") == own_domain_id
            ),
            None,
        )

    def _collect_activations_from_interfaces(
        self,
        interfaces: list[dict],
        *,
        device_id: str | None = None,
        device_capabilities: list[dict] | None = None,
    ) -> list[dict]:
        """Collect unique segment activations from capabilities.

        VlanSegment.vlan_id is a plain manual attribute directly on the segment
        (single-site, no realization record). VxlanSegment.segment_deployments
        is cardinality:many (multi-site stretch — clean_data unwraps it to a
        list, already filtered to active/provisioning by the query) and
        carries only vni now — the LOCAL vlan_id for a VxlanSegment comes from
        vlan_domain_segments, resolved to THIS device's own VLAN domain (its
        ManagedMLAG if paired, else itself) via _resolve_own_vlan_domain_id.
        A VxlanSegment whose vlan_domain_segments has no entry for this
        device's own domain yet (allocation not converged) is skipped.
        Without ``resolve_vlan_domain`` (a firewall is never part of a VLAN
        domain) a VxlanSegment takes the first segment_deployments entry as is.
        We deduplicate by segment id so each segment appears once.
        """
        own_domain_id = self._resolve_own_vlan_domain_id(device_id, device_capabilities or [])
        seen: set[str] = set()
        activations: list[dict] = []
        for iface in interfaces:
            for cap in iface.get("capabilities") or []:
                seg_id = cap.get("id") or cap.get("name")
                if not seg_id or seg_id in seen:
                    continue
                if cap.get("typename") == "ManagedVlanSegment":
                    if cap.get("status") not in self._ACTIVE_STATUSES:
                        continue
                    vlan_id = cap.get("vlan_id")
                    vni = None
                else:
                    seg_deps = cap.get("segment_deployments")
                    if not seg_deps:
                        continue
                    vni = seg_deps[0].get("vni")
                    if self.resolve_vlan_domain:
                        own_domain_seg = self._own_vlan_domain_segment(cap, own_domain_id)
                        if own_domain_seg is None:
                            continue
                        vlan_id = own_domain_seg.get("vlan_id")
                    else:
                        vlan_id = seg_deps[0].get("vlan_id")
                seen.add(seg_id)
                activations.append(
                    {
                        "vlan_id": vlan_id,
                        "vni": vni,
                        "segment": cap,
                    }
                )
        return activations

    def _collect_border_gateway_activations(
        self,
        deployment: dict[str, Any] | None,
        *,
        device_id: str | None,
        device_capabilities: list[dict[str, Any]],
        seen: set[str | None],
    ) -> list[dict[str, Any]]:
        """Activations for every stretched VXLAN segment of an EVPN Multi-Site BGW's site.

        A border gateway stitches each stretched segment between its fabric and
        the DCI without any customer-facing port carrying it, so
        capabilities never surface those segments. They come from the
        site's own TopologySegmentHosting.segment_deployments instead (queries/
        fragments/network_segment.gql SegmentDeploymentsOnDeploymentFields),
        keeping only stretch_scope != local. The local VLAN still has to be the
        BGW's own vlan_domain entry (generators/topology/segment.py assigns one
        to border-leaf/edge devices for stretched segments); a segment without
        one has not converged yet and is skipped, same as on a leaf.
        """
        own_domain_id = self._resolve_own_vlan_domain_id(device_id, device_capabilities)
        activations: list[dict[str, Any]] = []
        for dep in (deployment or {}).get("segment_deployments") or []:
            seg = dep.get("segment") or {}
            seg_id = seg.get("id")
            if not seg_id or seg_id in seen or (seg.get("stretch_scope") or "local") == "local" or not dep.get("vni"):
                continue
            own_domain_seg = self._own_vlan_domain_segment(seg, own_domain_id)
            if own_domain_seg is None or not own_domain_seg.get("vlan_id"):
                continue
            seen.add(seg_id)
            activations.append(
                {"vlan_id": own_domain_seg["vlan_id"], "vni": dep["vni"], "segment": seg, "border_gateway": True}
            )
        return activations

    def _load_template(self, platform_name: str) -> Template:
        """Load the Jinja2 template for the given platform."""
        return load_template(
            f"{self.root_directory}/templates/configs",
            f"{self.template_subdir}/{platform_name}.j2",
            filters=jinja2_convenience_function(),
        )
