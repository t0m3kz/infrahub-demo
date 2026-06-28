"""
Common utilities and base class for Infrahub device transforms.

Public API — all symbols importable directly from ``transforms.common``.
Implementation lives in ``transforms/helpers/`` submodules.
"""

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, Template
from netutils.utils import jinja2_convenience_function

from transforms.helpers.acl import _build_acl_rule, get_acls
from transforms.helpers.bgp import (
    _build_peer_groups,
    _build_session_from_peering,
    _normalize_afs,
    _sort_key_ip,
    get_bgp_profile,
)
from transforms.helpers.firewall import (
    get_firewall_static_routes,
    get_firewall_zones,
    get_vrf_default_gateways,
    get_zone_policies,
)
from transforms.helpers.ha import _HA_TYPENAMES, get_ha
from transforms.helpers.management import get_aaa, get_ntp, get_snmp, get_syslog
from transforms.helpers.mlag import get_mlag
from transforms.helpers.ospf import get_ospf
from transforms.helpers.segments import (
    _get_segment_gateways,
    _get_segment_namespace,
    _get_segment_prefix_str,
    _vlans_from_activations,
    get_vlans,
)
from transforms.helpers.vxlan import (
    _collect_l3_vni_from_namespaces,
    _l2_from_activations,
    _l3_from_activations,
    _transform_vxlan_arista,
    _transform_vxlan_nxos,
    _transform_vxlan_platform,
    _transform_vxlan_sonic,
    get_interfaces,
    get_vxlan_config,
)
from utils.data_cleaning import clean_data, get_data


def _get_sgt_rules(activations: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Derive SecurityTagRule list from segment activations.

    Traverses segment → security_tag → rules_as_source to collect only the
    rules relevant to segments present on this device. Deduplicates by
    (src_sgt, dst_sgt) pair.
    """
    if not activations:
        return []
    seen: set[tuple[int, int]] = set()
    rules: list[dict[str, Any]] = []
    for act in activations:
        tag = (act.get("segment") or {}).get("security_tag") or {}
        src_sgt = tag.get("group_id")
        src_name = tag.get("name")
        if not src_sgt:
            continue
        for rule in tag.get("rules_as_source") or []:
            dst = rule.get("destination_tag") or {}
            dst_sgt = dst.get("group_id")
            dst_name = dst.get("name")
            if not dst_sgt:
                continue
            key = (src_sgt, dst_sgt)
            if key in seen:
                continue
            seen.add(key)
            rules.append(
                {
                    "src_name": src_name,
                    "src_sgt": src_sgt,
                    "dst_name": dst_name,
                    "dst_sgt": dst_sgt,
                    "action": rule.get("action", "permit"),
                    "log": rule.get("log", False),
                }
            )
    return rules


def get_capabilities(data: dict[str, Any]) -> dict[str, Any]:
    """Derive device capabilities from services.

    Capabilities are derived from device_capabilities (BGP/OSPF presence).

    Args:
        data: Device data from GraphQL query (after clean_data)

    Returns:
        Dict with capability flags for template rendering.
    """
    services = data.get("capabilities") or []
    bgp_enabled = any(s.get("typename") == "ManagedBGP" for s in services)
    ospf_enabled = any(s.get("typename") == "ManagedOSPF" for s in services)
    mlag_enabled = any(s.get("typename") == "ManagedMLAG" for s in services)
    ntp_enabled = any(s.get("typename") == "ManagedNTP" for s in services)
    syslog_enabled = any(s.get("typename") == "ManagedSyslog" for s in services)
    snmp_enabled = any(s.get("typename") == "ManagedSNMP" for s in services)
    aaa_enabled = any(s.get("typename") == "ManagedAAA" for s in services)
    ha_enabled = any(s.get("typename") in _HA_TYPENAMES for s in services)

    return {
        "bgp_enabled": bgp_enabled,
        "ospf_enabled": ospf_enabled,
        "mlag_enabled": mlag_enabled,
        "ntp_enabled": ntp_enabled,
        "syslog_enabled": syslog_enabled,
        "snmp_enabled": snmp_enabled,
        "aaa_enabled": aaa_enabled,
        "ha_enabled": ha_enabled,
    }


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

    async def transform(self, data: Any) -> Any:
        cleaned = clean_data(data)

        # Device node is always the first root
        if not isinstance(cleaned, dict) or not cleaned:
            raise ValueError("clean_data() did not return a non-empty dictionary")
        first_key = next(iter(cleaned))
        first_value = cleaned[first_key]
        device_data = first_value[0] if isinstance(first_value, list) and first_value else (first_value or {})

        # Extra roots (e.g. DcimFirewallInterface for VRF default gateways)
        extra_roots = {k: v for k, v in cleaned.items() if k != first_key}

        platform = device_data.get("platform") or {}
        platform_name = platform.get("netmiko_device_type")

        if not platform_name:
            device_name = device_data.get("name", "Unknown Device")
            return (
                f"! Device {device_name} has no platform with "
                f"netmiko_device_type defined.\n! No configuration generated.\n"
            )

        # Collect segment activations from interface capabilities (segment → segment_deployments)
        activations = self._collect_activations_from_interfaces(device_data.get("interfaces") or [])
        if activations:
            device_data["segment_deployments"] = self._filter_segment_deployments(activations)

        config = self._build_config(device_data, platform_name)
        config.update(self._extra_config(device_data, platform_name, extra_roots=extra_roots))

        template = self._load_template(platform_name)
        return template.render(**config)

    def _build_config(self, data: dict, platform_name: str) -> dict:
        """Build the base template context shared by all device transforms."""
        interfaces = data.get("interfaces") or []
        device_capabilities = data.get("capabilities") or []
        device_name = data.get("name", "")
        activations = data.get("segment_deployments")
        config = {
            "name": device_name,
            "hostname": device_name,
            "device_role": data.get("role", ""),
            "interfaces": get_interfaces(interfaces, activations=activations),
            "bgp": get_bgp_profile(
                device_capabilities,
                interfaces,
                device_name=device_name,
                device_role=data.get("role", ""),
            ),
            "ospf": get_ospf(device_capabilities),
            "mlag": get_mlag(device_capabilities, interfaces),
            "ntp": get_ntp(device_capabilities),
            "syslog": get_syslog(device_capabilities),
            "snmp": get_snmp(device_capabilities),
            "aaa": get_aaa(device_capabilities),
        }
        capabilities = get_capabilities(data)
        if capabilities:
            config["capabilities"] = capabilities
        return config

    def _extra_config(self, data: dict, platform_name: str, extra_roots: dict | None = None) -> dict:  # noqa: ARG002
        """Return device-specific template variables.

        Default implementation adds VLANs, VXLAN config, ACLs, VRF default
        gateways, and SGT rules when device_role is set.
        Override in subclasses for different behavior.
        """
        if not self.device_role:
            return {}
        activations = data.get("segment_deployments")
        vlans = get_vlans(activations=activations)

        # VRF default gateways: derived from segment → security_zone → firewall_interface
        vrf_gateways = get_vrf_default_gateways(activations)

        # SGT rules derived from segment activations (via security_tag.rules_as_source)
        sgt_rules = _get_sgt_rules(activations)

        return {
            "vlans": vlans,
            "vxlan": get_vxlan_config(data, platform_name, device_role=self.device_role, activations=activations),
            "acls": get_acls(activations=activations),
            "vrf_gateways": vrf_gateways,
            "sgt_rules": sgt_rules,
        }

    def _collect_activations_from_interfaces(self, interfaces: list[dict]) -> list[dict]:
        """Collect unique segment activations from interface_capabilities.

        Each segment in interface_capabilities now carries its own segment_deployments
        (vlan_id + vni). We deduplicate by segment id so each segment appears once.
        """
        seen: set[str] = set()
        activations: list[dict] = []
        for iface in interfaces:
            for cap in iface.get("interface_capabilities") or []:
                seg_id = cap.get("id") or cap.get("name")
                if not seg_id or seg_id in seen:
                    continue
                seg_deps = cap.get("segment_deployments") or []
                if not seg_deps:
                    continue
                seen.add(seg_id)
                # seg_deps is a flat list of {vlan_id, vni} dicts — pick the first (one per DC)
                dep = seg_deps[0]
                activations.append(
                    {
                        "vlan_id": dep.get("vlan_id"),
                        "vni": dep.get("vni"),
                        "segment": cap,
                    }
                )
        return activations

    def _filter_segment_deployments(self, activations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Filter segment activations before they are used in config generation.

        Override in subclasses to restrict which segments appear in the config.
        Default: return all activations unchanged.
        """
        return activations

    def _load_template(self, platform_name: str) -> Template:
        """Load the Jinja2 template for the given platform."""
        path = f"{self.root_directory}/templates/configs"
        env = Environment(
            loader=FileSystemLoader(path),
            autoescape=False,
            keep_trailing_newline=True,
        )
        env.filters.update(jinja2_convenience_function())
        return env.get_template(f"{self.template_subdir}/{platform_name}.j2")


__all__ = [
    "BaseDeviceTransform",
    "_get_sgt_rules",
    "clean_data",
    "get_acls",
    "get_bgp_profile",
    "get_capabilities",
    "get_data",
    "get_firewall_static_routes",
    "get_ha",
    "get_firewall_zones",
    "get_interfaces",
    "get_ospf",
    "get_vlans",
    "get_vrf_default_gateways",
    "get_vxlan_config",
    "get_zone_policies",
    # private helpers (imported by unit tests)
    "_build_acl_rule",
    "_build_peer_groups",
    "_build_session_from_peering",
    "_collect_l3_vni_from_namespaces",
    "_get_segment_gateways",
    "_get_segment_namespace",
    "_get_segment_prefix_str",
    "_l2_from_activations",
    "_l3_from_activations",
    "_normalize_afs",
    "_sort_key_ip",
    "_transform_vxlan_arista",
    "_transform_vxlan_nxos",
    "_transform_vxlan_platform",
    "_transform_vxlan_sonic",
    "_vlans_from_activations",
]
