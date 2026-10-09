"""Base class and shared helpers for Infrahub device transforms."""

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Template
from netutils.utils import jinja2_convenience_function

from transforms.helpers.acl import get_acls
from transforms.helpers.bgp import get_bgp_profile
from transforms.helpers.firewall import (
    _flatten_deployment_firewall_contexts,
    get_customer_pbr_rules,
    get_vrf_default_gateways,
)
from transforms.helpers.ha import _HA_TYPENAMES
from transforms.helpers.loadbalancer_pbr import _flatten_deployment_lb_vips, get_lb_backend_pbr_rules
from transforms.helpers.management import get_management_services
from transforms.helpers.mlag import get_mlag, get_sonic_mlag_config
from transforms.helpers.ospf import get_ospf
from transforms.helpers.policy import get_sgt_rules
from transforms.helpers.segments import get_vlans, routed_activations, segment_hosting_candidates
from transforms.helpers.templates import load_template
from transforms.helpers.vxlan import get_interfaces, get_vxlan_config
from utils.data_cleaning import clean_data


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
        "mlag_enabled": "ManagedMLAG" in typenames,
        "ntp_enabled": "ManagedNTP" in typenames,
        "syslog_enabled": "ManagedSyslog" in typenames,
        "snmp_enabled": "ManagedSNMP" in typenames,
        "aaa_enabled": "ManagedAAA" in typenames,
        "ha_enabled": not typenames.isdisjoint(_HA_TYPENAMES),
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


def _combine_leaf_pbr_rules(
    customer_rules: list[dict[str, Any]], lb_backend_rules: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Combine firewall and LB redirects into one policy per VLAN."""
    by_vlan = {rule["vlan_id"]: {**rule, "backend_ips": [], "lb_nexthop": None} for rule in customer_rules}
    for rule in lb_backend_rules:
        vlan_id = rule["vlan_id"]
        if vlan_id in by_vlan:
            by_vlan[vlan_id].update(backend_ips=rule["backend_ips"], lb_nexthop=rule["lb_nexthop"])
        else:
            by_vlan[vlan_id] = {**rule, "bypass_prefixes": [], "fw_nexthop": None}
    return [by_vlan[vlan_id] for vlan_id in sorted(by_vlan)]


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
    # Comment character of the "no platform" stub config.
    comment_char: str = "!"
    # Resolve a VxlanSegment's local VLAN through this device's own VLAN domain.
    resolve_vlan_domain: bool = True

    async def transform(self, data: Any) -> Any:
        device_data, extra_roots, platform_name = self._device_and_platform(data)
        if not platform_name:
            return self._no_platform_config(device_data)

        # Collect segment activations from interface capabilities (segment → segment_deployments)
        activations = self._collect_activations_from_interfaces(
            device_data.get("interfaces") or [],
            device_id=device_data.get("id"),
            device_capabilities=device_data.get("capabilities") or [],
        )
        if any(iface.get("role") == "multisite-vip" for iface in device_data.get("interfaces") or []):
            activations.extend(
                self._collect_border_gateway_activations(
                    device_data.get("deployment"),
                    device_id=device_data.get("id"),
                    device_capabilities=device_data.get("capabilities") or [],
                    seen={(act.get("segment") or {}).get("id") for act in activations},
                )
            )
        if activations:
            device_data["segment_deployments"] = self._filter_segment_deployments(activations)

        config = self._build_config(device_data, platform_name)
        config.update(self._extra_config(device_data, platform_name, extra_roots=extra_roots))
        if platform_name in {"sonic", "dell_sonic"}:
            unsupported = [key for key in ("snmp", "aaa") if config.get(key)]
            if unsupported:
                raise ValueError(f"{platform_name} ConfigDB rendering does not support: {', '.join(unsupported)}")
        return self._render(platform_name, config)

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

    def _build_config(self, data: dict, platform_name: str) -> dict:
        """Build the base template context shared by all device transforms."""
        interfaces = data.get("interfaces") or []
        device_capabilities = data.get("capabilities") or []
        device_name = data.get("name", "")
        activations = data.get("segment_deployments")
        management_services = get_management_services(device_capabilities)
        mlag = get_mlag(device_capabilities, interfaces, device_name=device_name)
        # The MLAG control SVI is rendered by the platform's MLAG include
        # (it needs the peer-link VLAN/trunk group around it), not as a
        # generic interface.
        template_interfaces = get_interfaces(
            [iface for iface in interfaces if iface.get("role") != "mlag-control"],
            activations=activations,
            device_name=device_name,
        )
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
            "ntp": management_services["ntp"],
            "syslog": management_services["syslog"],
            "snmp": management_services["snmp"],
            "aaa": management_services["aaa"],
            "capabilities": get_capabilities(data),
        }
        if platform_name in {"sonic", "dell_sonic"}:
            config["sonic_mlag"] = get_sonic_mlag_config(mlag)
        return config

    def _extra_config(self, data: dict, platform_name: str, extra_roots: dict | None = None) -> dict:
        """Return device-specific template variables.

        Default implementation adds VLANs, VXLAN config, ACLs, VRF default
        gateways, and SGT rules when device_role is set.
        Override in subclasses for different behavior.
        """
        if not self.device_role:
            return {}
        activations = data.get("segment_deployments")
        vlans = get_vlans(activations=activations)
        # PBR and the zero-trust ACLs hang off the segment's SVI; a
        # terminate_inline segment has none here (its HA pair routes it).
        routed = routed_activations(activations)

        # VRF default gateways: from TopologyRoutedExchange capabilities on this
        # device's own interfaces — both legs of the inter-VRF hop live here.
        vrf_gateways = get_vrf_default_gateways(data.get("interfaces"))

        # Tier-to-tier GPO contracts, derived from the segments' permit rules
        sgt_rules = get_sgt_rules(activations)

        # Customer PBR: default-redirect to the firewall context serving this
        # segment's owner; a SecurityPolicyRule permit is the only bypass.
        # ManagedFirewallContext is reached via a device-scoped traversal —
        # this device's own `deployment` (queries/fragments/firewall_contexts.gql's
        # FirewallContextsOnDeploymentFields), not a global query root — no
        # leaf ever owns a FirewallContext interface itself, only the
        # firewall/border-leaf do.
        firewall_contexts = _flatten_deployment_firewall_contexts(data.get("deployment"))
        customer_pbr_rules = get_customer_pbr_rules(routed, firewall_contexts)

        # LB backend no-SNAT return-path PBR: same device-scoped deployment
        # traversal as firewall_contexts above, leaf-only in practice since
        # border-leaf never hosts pool members (no activations there).
        lb_vips = _flatten_deployment_lb_vips(data.get("deployment"))
        lb_backend_pbr_rules = get_lb_backend_pbr_rules(routed, lb_vips)
        leaf_pbr_rules = _combine_leaf_pbr_rules(customer_pbr_rules, lb_backend_pbr_rules)
        if self.device_role in {"leaf", "tor", "access-leaf"}:
            # SONiC renders PBR (ConfigDB PBR ACL table) but has no GPO;
            # SR OS leafs render neither. Refuse rather than ship a VLAN
            # whose firewall steering or segmentation silently vanished.
            wants_gpo = bool(sgt_rules) or any(vlan.get("sgt") for vlan in vlans)
            if platform_name in {"sonic", "dell_sonic"} and wants_gpo:
                raise ValueError(f"{platform_name} leaf cannot render GPO policy; refusing unprotected config")
            if platform_name == "nokia_sros" and (wants_gpo or leaf_pbr_rules):
                raise ValueError(f"{platform_name} leaf cannot render GPO or PBR policy; refusing unprotected config")

        acls = get_acls(activations=routed)
        acl_names = {acl["vlan_id"]: acl["name"] for acl in acls}
        for vlan in vlans:
            vlan["acl_name"] = acl_names.get(vlan["vlan_id"])

        return {
            "vlans": vlans,
            "vxlan": get_vxlan_config(
                data,
                platform_name,
                device_role=self.device_role,
                activations=activations,
                fabric_rt_asn=_fabric_rt_asn(data.get("deployment")),
                fabric_anycast_mac=_fabric_anycast_mac(data.get("deployment")),
            ),
            "acls": acls,
            "vrf_gateways": vrf_gateways,
            "sgt_rules": sgt_rules,
            "customer_pbr_rules": customer_pbr_rules,
            "lb_backend_pbr_rules": lb_backend_pbr_rules,
            "leaf_pbr_rules": leaf_pbr_rules,
        }

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
        """Collect unique segment activations from interface_capabilities.

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
            for cap in iface.get("interface_capabilities") or []:
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
        interface_capabilities never surface those segments. They come from the
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
            activations.append({"vlan_id": own_domain_seg["vlan_id"], "vni": dep["vni"], "segment": seg})
        return activations

    def _filter_segment_deployments(self, activations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Filter segment activations before they are used in config generation.

        Override in subclasses to restrict which segments appear in the config.
        Default: return all activations unchanged.
        """
        return activations

    def _load_template(self, platform_name: str) -> Template:
        """Load the Jinja2 template for the given platform."""
        return load_template(
            f"{self.root_directory}/templates/configs",
            f"{self.template_subdir}/{platform_name}.j2",
            filters=jinja2_convenience_function(),
        )
