"""Base class and shared helpers for Infrahub device transforms."""

import logging
from typing import Any, ClassVar

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Template
from netutils.utils import jinja2_convenience_function

from transforms.helpers.acl import get_acls
from transforms.helpers.bgp import get_bgp_profile
from transforms.helpers.bgp_facts import annotate_bgp_policy
from transforms.helpers.cisco_xe import get_cisco_xe_acls, get_cisco_xe_vrf_routes, refuse_unfiltered_ipv6
from transforms.helpers.firewall import (
    get_exchange_routes,
    get_exchange_transits,
    get_leaf_firewall_transits,
)
from transforms.helpers.firewall_contexts import _flatten_deployment_firewall_contexts
from transforms.helpers.firewall_pbr import get_customer_pbr_rules
from transforms.helpers.group_policy import GPO_CONFIG_KEYS, group_policy_device_view
from transforms.helpers.lisp import build_lisp_inputs, get_lisp_config
from transforms.helpers.mlag import get_mlag, get_sonic_mlag_config
from transforms.helpers.origination import get_origination
from transforms.helpers.ospf import get_ospf, refuse_unrenderable_ospf_auth
from transforms.helpers.overlay import OVERLAY_CONFIG_KEYS, OVERLAY_RENDERERS
from transforms.helpers.segments import get_vlans, interface_segments, routed_activations
from transforms.helpers.templates import load_template
from transforms.helpers.vrf import VrfRealizationError, l3_vrf_names, svi_vrfs, unrealized_vrfs
from transforms.helpers.vxlan import get_interfaces, get_vxlan_config
from utils.circuit_underlay import NETWORK_APPLIANCE_KIND
from utils.data_cleaning import clean_data
from utils.exchange_transit import service_context_caps
from utils.fabric_roles import BORDER_ROLES, OFFICE_LISP_ROLES
from utils.limits import UNDERLAY_MTU
from utils.segment_addressing import segment_hosting_candidates
from utils.vrf_demand import SVI_ROLE

_log = logging.getLogger(__name__)

# The platforms whose BGP template (common/cisco_ios_bgp.j2) renders the session["unfiltered_banner"] comment above
# a VRF neighbour. A hosted appliance on any other platform is refused: its template would render no banner.
UNFILTERED_BANNER_PLATFORMS = frozenset({"cisco_ios", "cisco_xe"})

# Interface roles the generic interface loop does not render: the MLAG control SVI is rendered by the platform's MLAG
# include (it needs the peer-link VLAN/trunk group around it), a segment SVI (role svi, an object since the VRF
# instances) by the VLAN loop from its segment (get_vlans), and neither is a port.
_NON_TEMPLATE_INTERFACE_ROLES = frozenset({"mlag-control", SVI_ROLE})


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


def _own_devices(deployment: Any) -> frozenset[str]:
    """Names of the devices the deployment itself owns beyond BGP process roles: an office's SD-WAN edge.

    A BGP session to one of them is internal (transforms/helpers/bgp.py), the office WAN exit for one.
    """
    wan_edges = deployment.get("lisp_wan_edges") if isinstance(deployment, dict) else None
    return frozenset(edge["name"] for edge in wan_edges or [] if isinstance(edge, dict) and edge.get("name"))


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
    # Resolve a VxlanSegment's local VLAN from this device's own row of the segment deployment.
    resolve_vlan_row: bool = True
    # The platforms whose template defines the RM-<session>-IN/-OUT maps of an external BGP session
    # (transforms/helpers/bgp.py). Any other platform would render that session with no policy.
    external_policy_platforms: ClassVar[frozenset[str]] = frozenset()

    def _device_activations(self, device: dict[str, Any]) -> list[dict[str, Any]]:
        """Segment activations of one device: from its interface capabilities, plus the
        stretched segments its multi-site border gateway carries."""
        interfaces = device.get("interfaces") or []
        activations = self._collect_activations_from_interfaces(interfaces, device_id=device.get("id"))
        if any(iface.get("role") == "multisite-vip" for iface in interfaces):
            # The site hosting the segments: the DC for a border-leaf, the metro (one `parent` hop
            # above the zone the edge is deployed to) for a colocation edge.
            site = next(
                (c for c in segment_hosting_candidates(device.get("deployment")) if "segment_deployments" in c), None
            )
            if site is None or site.get("segment_deployments") is None:
                # The query returned no site that carries segment_deployments (a SegmentHosting fragment on the
                # zone -> metro hop can come back null): stretched segments would vanish from the config silently.
                _log.warning(
                    "%s: multi-site border gateway without a site carrying segment_deployments; "
                    "stretched segments are not rendered.",
                    device.get("name") or "device",
                )
            activations.extend(
                self._collect_border_gateway_activations(
                    site,
                    device_id=device.get("id"),
                    seen={(act.get("segment") or {}).get("id") for act in activations},
                )
            )
        return activations

    def prepared_device(self, data: Any) -> tuple[dict, dict, str | None]:
        """Clean `data` and return (device, the other query roots, platform name), the device ready to render.

        The device carries its segment activations (``segment_deployments``) and the transit legs of the contexts
        tagged on its service ports (``exchange_transits``: VLAN/VNI/VRF/anycast only, never segment policy input).
        """
        device_data, extra_roots, platform_name = self._device_and_platform(data)
        if platform_name:
            activations = self._device_activations(device_data)
            if activations:
                device_data["segment_deployments"] = activations
            device_data["exchange_transits"] = get_exchange_transits(
                service_context_caps(device_data.get("interfaces"))
            )
        return device_data, extra_roots, platform_name

    async def transform(self, data: Any) -> Any:
        device_data, extra_roots, platform_name = self.prepared_device(data)
        if not platform_name:
            return self._no_platform_config(device_data)

        vrf_banner = self._refuse_unrealized_vrfs(platform_name, device_data)
        config = self._template_context(device_data, platform_name, extra_roots)
        if vrf_banner:
            config["vrf_banner"] = vrf_banner
        self._refuse_cisco_xe_unrendered(platform_name, config)
        config.update(self._cisco_xe_context(platform_name, config))
        return self._render(platform_name, config)

    @staticmethod
    def _refuse_unrealized_vrfs(platform_name: str, device_data: dict) -> list[str]:
        """Raise ``VrfRealizationError`` when the device's config uses a VRF or SVI it has no instance of.

        A VRF instance (ManagedVRF) is the device's only VRF set (transforms/helpers/vrf.py): a routed segment,
        an SVI, a session or address, or a transit leg in a namespace with no instance, and a routed segment with no
        SVI object (or one in another VRF), would render a config that routes in a table the device does not have.
        One raise lists everything missing.

        A provider-hosted ``CloudNetworkAppliance`` on a ``UNFILTERED_BANNER_PLATFORMS`` platform must still get its
        artifact: the problems are logged as a WARNING and returned as the ``vrf_banner`` comment lines instead.
        """
        problems = unrealized_vrfs(device_data)
        if not problems:
            return []
        name = device_data.get("name") or "device"
        if device_data.get("typename") == NETWORK_APPLIANCE_KIND and platform_name in UNFILTERED_BANNER_PLATFORMS:
            _log.warning(
                "%s: renders with VRFs it has no instance of (%s); the VRF generator has not converged",
                name,
                "; ".join(problems),
            )
            return [f"VRF-UNREALIZED: {problem}" for problem in problems]
        raise VrfRealizationError(
            f"{name}: {platform_name} config uses VRFs the device has no instance of ({'; '.join(problems)}); "
            "refusing a config that routes in a table the device does not have"
        )

    def _refuse_unfiltered_external_sessions(self, platform_name: str, config: dict, device_kind: str | None) -> None:
        """Raise when an external BGP session would render with no owner prefix filter.

        An external session (session["external"]: a regular peer outside our administration in a tenant VRF)
        must be limited to its owners' prefixes in both directions. Only the templates of
        ``external_policy_platforms`` define those route-maps; on any other platform or role the session
        would be an open one, so refuse instead, like the unrendered cisco_xe features.

        A provider-hosted ``CloudNetworkAppliance`` (``device_kind``) must still get its artifact: on a
        ``UNFILTERED_BANNER_PLATFORMS`` platform each external session is marked with
        ``session["unfiltered_banner"]`` (the template renders it as a comment above the neighbour) and
        logged as a WARNING instead (never an error: the SDK would fail the artifact). An unknown kind
        refuses, fail-closed.
        """
        if platform_name in self.external_policy_platforms:
            return
        external = [
            session
            for bgp in config.get("bgp") or []
            for session in bgp.get("sessions") or []
            if session.get("external")
        ]
        if not external:
            return
        if device_kind == NETWORK_APPLIANCE_KIND and platform_name in UNFILTERED_BANNER_PLATFORMS:
            for session in external:
                session["unfiltered_banner"] = (
                    f"UNFILTERED: no external-policy renderer for {platform_name}; this session announces and "
                    "accepts everything the peer sends - provider-side filter required"
                )
                _log.warning(
                    "%s: external BGP session %s (%s) renders unfiltered on %s %s; provider-side filter required",
                    config.get("name"),
                    session.get("name"),
                    session.get("remote_device") or "unknown far end",
                    platform_name,
                    device_kind,
                )
            return
        sessions = ", ".join(f"{s.get('name')} ({s.get('remote_device') or 'unknown far end'})" for s in external)
        role = self.device_role or config.get("device_role") or "device"
        raise ValueError(
            f"{config.get('name')}: {platform_name} {role} cannot render the owner prefix filters of "
            f"external BGP session(s) {sessions}; refusing an open session"
        )

    # The roles rendered by a cisco_xe fabric template (leafs/, border_leafs/ and the office's
    # access_switches/, distribution_switches/); the Edge's cisco_xe template is IOS.
    _CISCO_XE_FABRIC_ROLES = frozenset({"leaf", "tor", "access-leaf", "border-leaf"}) | OFFICE_LISP_ROLES

    def _refuse_cisco_xe_unrendered(self, platform_name: str, config: dict) -> None:
        """Raise when a Catalyst 9000 (cisco_xe) fabric device needs what its EVPN-VXLAN templates do not render.

        Those templates render the overlay (EVPN-VXLAN or LISP), BGP, OSPF, the gateway SVIs, the segment ACLs and
        the inter-VRF exchange statics. Dropping PBR, MLAG or Multi-Site would ship an unprotected or incomplete
        config, so refuse instead, like SONiC. What the ACLs and statics themselves cannot express is
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
            # other VRF (an instance that is VRF-lite, or has no L3 instance-id) would render under a VRF the
            # template does not define.
            session_vrfs = {vrf for bgp in config.get("bgp") or [] for vrf in bgp.get("vrf_sessions") or {}}
            undefined = sorted(session_vrfs - set(l3_vrf_names(lisp, None)))
            if undefined:
                unsupported.append(
                    f"a BGP session in VRF {', '.join(undefined)}, which is no L3 instance of the LISP site"
                )
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
        defined_vrfs = l3_vrf_names(lisp, config.get("vxlan"))
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
        annotate_bgp_policy(config, device_data, extra_roots, unmanaged=_own_devices(device_data.get("deployment")))
        return config

    @staticmethod
    def _device_and_platform(data: Any) -> tuple[dict, dict, str | None]:
        """Clean `data` and return (device, the other query roots, platform name).

        The device node is always the first query root.
        """
        cleaned = clean_data(data)
        if not isinstance(cleaned, dict) or not cleaned:
            raise ValueError("clean_data() did not return a non-empty dictionary")
        first_key = next(iter(cleaned))
        first_value = cleaned[first_key]
        device = first_value[0] if isinstance(first_value, list) and first_value else (first_value or {})
        extra_roots = {k: v for k, v in cleaned.items() if k != first_key}
        return device, extra_roots, (device.get("platform") or {}).get("name")

    def _no_platform_config(self, device: dict) -> str:
        """Placeholder config for a device without a platform."""
        c = self.comment_char
        device_name = device.get("name", "Unknown Device")
        return f"{c} Device {device_name} has no platform defined.\n{c} No configuration generated.\n"

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
        refuse_unrenderable_ospf_auth(platform_name, template_interfaces)
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
                own_devices=_own_devices(data.get("deployment")),
            ),
            "ospf": get_ospf(device_capabilities, interfaces),
            "mlag": mlag,
            "capabilities": get_capabilities(data),
            "underlay_mtu": UNDERLAY_MTU,
        }
        self._refuse_unfiltered_external_sessions(platform_name, config, data.get("typename"))
        if platform_name in {"sonic", "dell_sonic"}:
            config["sonic_mlag"] = get_sonic_mlag_config(mlag)
        return config

    @staticmethod
    def _template_interfaces(data: dict, *, host_vlans: dict[str, int] | None = None) -> list[dict[str, Any]]:
        """The interfaces the templates render (get_interfaces), ``host_vlans`` being the NX-OS GPO host ports.

        Not the roles of ``_NON_TEMPLATE_INTERFACE_ROLES`` (the MLAG control SVI, the segment SVIs).
        """
        activations = (data.get("segment_deployments") or []) + (data.get("exchange_transits") or []) or None
        return get_interfaces(
            [iface for iface in data.get("interfaces") or [] if iface.get("role") not in _NON_TEMPLATE_INTERFACE_ROLES],
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

        # Exchange transits (see transform()) and the leaf's local transit legs: they
        # only add VLANs, VNIs, VRFs and the anycast gateway; policy (ACL, PBR) stays
        # on real segments. They are not bound to any host port (get_interfaces only
        # binds a transit VLAN to a port carrying the context).
        transits = (data.get("exchange_transits") or []) + leaf_transits
        vlans = get_vlans(activations=(activations or []) + transits, svi_vrfs=svi_vrfs(data.get("interfaces")))

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
        config = {
            "vlans": vlans,
            "vxlan": vxlan,
            "acls": acls,
            "vrf_routes": vrf_routes,
            "leaf_pbr_rules": leaf_pbr_rules,
            # The leaf's own transit /29s: kept out of the direct-to-EVPN redistribution
            # so only the borders originate them as type-5 routes.
            "leaf_transit_prefixes": sorted({t["segment"]["gateway"]["ip_prefix"]["prefix"] for t in leaf_transits}),
            # The tagged segments' SVI prefixes with the routing-identity communities their EVPN export adds
            # (transforms/helpers/origination.py); empty, so every template renders as before, without tags.
            "origination": get_origination(activations, l3_vrf_names(None, vxlan)),
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
            vrfs=l3_vrf_names(lisp, vxlan),
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
    def _own_vlan_row(deployments: list[dict], device_id: str | None) -> tuple[dict, dict] | None:
        """(row, segment deployment) of the segment's row of this device.

        A row points straight at its device (a paired device has its own row, a peer's row is
        ignored). The row is a child of the deployment it sits in, so its VLAN ID and the
        deployment's VNI come from one record."""
        for deployment in deployments:
            for row in deployment.get("vlan_domain_segments") or []:
                if device_id and (row.get("device") or {}).get("id") == device_id:
                    return row, deployment
        return None

    def _collect_activations_from_interfaces(
        self,
        interfaces: list[dict],
        *,
        device_id: str | None = None,
    ) -> list[dict]:
        """Collect unique segment activations from the interfaces' segments (interface_segments).

        VxlanSegment.segment_deployments
        is cardinality:many (multi-site stretch — clean_data unwraps it to a
        list, already filtered to active/provisioning by the query) and
        carries the vni and, under it, the vlan_domain_segments rows — the LOCAL
        vlan_id for a VxlanSegment is the row of THIS device (``row.device.id``;
        a paired device uses its own row, never its peer's)
        and the vni is its deployment's, one record for both. A VxlanSegment
        with no row for this device yet (allocation not
        converged) is skipped.
        Without ``resolve_vlan_row`` (a firewall has no VLAN rows)
        a VxlanSegment takes the first segment_deployments entry as is.
        We deduplicate by segment id so each segment appears once.
        """
        seen: set[str] = set()
        activations: list[dict] = []
        for iface in interfaces:
            for cap in interface_segments(iface):
                seg_id = cap.get("id") or cap.get("name")
                if not seg_id or seg_id in seen:
                    continue
                seg_deps = cap.get("segment_deployments")
                if not seg_deps:
                    continue
                if self.resolve_vlan_row:
                    own_row = self._own_vlan_row(seg_deps, device_id)
                    if own_row is None:
                        continue
                    vlan_id, vni = own_row[0].get("vlan_id"), own_row[1].get("vni")
                else:
                    vlan_id, vni = seg_deps[0].get("vlan_id"), seg_deps[0].get("vni")
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
        seen: set[str | None],
    ) -> list[dict[str, Any]]:
        """Activations for every stretched VXLAN segment of an EVPN Multi-Site BGW's site.

        A border gateway stitches each stretched segment between its fabric and
        the DCI without any customer-facing port carrying it, so
        capabilities never surface those segments. They come from the
        site's own TopologySegmentHosting.segment_deployments instead (queries/
        fragments/network_segment.gql SegmentDeploymentsOnDeploymentFields),
        keeping only stretch_scope != local. The local VLAN still has to be the
        BGW's own row (generators/topology/segment.py assigns one
        to border-leaf/edge devices for stretched segments); a segment without
        one has not converged yet and is skipped, same as on a leaf.
        """
        activations: list[dict[str, Any]] = []
        for dep in (deployment or {}).get("segment_deployments") or []:
            seg = dep.get("segment") or {}
            seg_id = seg.get("id")
            if not seg_id or seg_id in seen or (seg.get("stretch_scope") or "local") == "local" or not dep.get("vni"):
                continue
            own_row = self._own_vlan_row([dep], device_id)
            if own_row is None or not own_row[0].get("vlan_id"):
                continue
            seen.add(seg_id)
            activations.append(
                {"vlan_id": own_row[0]["vlan_id"], "vni": dep["vni"], "segment": seg, "border_gateway": True}
            )
        return activations

    def _load_template(self, platform_name: str) -> Template:
        """Load the Jinja2 template for the given platform."""
        return load_template(
            f"{self.root_directory}/templates/configs",
            f"{self.template_subdir}/{platform_name}.j2",
            filters=jinja2_convenience_function(),
        )
