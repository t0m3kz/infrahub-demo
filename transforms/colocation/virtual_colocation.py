"""Transform: TopologyVirtualColocation -> Equinix Network Edge Terraform (HCL), one artifact per presence.

The kit of a virtual colocation is its CloudNetworkAppliances, created by generators/topology/colocation_fabric.py from
the presence's `fabric_templates`. Everything that is ordered lives on the appliance itself (service code, package
code, software version, vCPUs, term), copied from its template when the generator created it. An appliance is a single
Equinix device, an edge router included: no redundancy is expressed for routers. Only firewalls are paired, as the two
members of one ManagedFirewallHA domain (mode `active-passive`) named `{first}-{second}`, linked through the
appliances' `capabilities` edge; that pair is one Equinix cluster. The `fabric_templates` entries are read for role and
quantity only, to refuse a presence whose generator stopped midway.

normalise_virtual_colocation() is the pure part: query result in, the plain-dict contract of
render_equinix_network_devices() out. `cluster` comes from the HA domain; node 0 is the first member by name, the repo
convention for every HA pair (transforms/helpers/ha.py). The billing account number is read from the presence's
account and the notification e-mails from the account's owner; licences and other secrets are never read, a licence
token is a Terraform variable in the rendered file.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from transforms.colocation.virtual_terraform import render_equinix_network_devices
from utils.data_cleaning import clean_data

_KIND = "TopologyVirtualColocation"
_APPLIANCE_KIND = "CloudNetworkAppliance"
_ORCHESTRATOR = "terraform"
_CLUSTER = "active-passive"
# renderer ordering field -> the CloudNetworkAppliance attribute it is read from
_APPLIANCE_FIELDS = {
    "type_code": "service_code",
    "package_code": "package_code",
    "version": "software_version",
    "term_length": "term_length",
}


def _virtual_colocation(cleaned: dict[str, Any]) -> dict[str, Any]:
    entries = cleaned.get(_KIND) or []
    if not entries:
        raise ValueError(f"no {_KIND} in the query result")
    return entries[0]


def _check_complete(vc_name: str, cleaned_vc: dict[str, Any], devices: list[dict[str, Any]]) -> None:
    """Refuse a presence whose generator stopped midway: rendering what exists would order half a kit."""
    declared: dict[Any, int] = {}
    for entry in cleaned_vc.get("fabric_templates") or []:
        declared[entry.get("role")] = declared.get(entry.get("role"), 0) + (entry.get("quantity") or 0)
    present: dict[Any, int] = {}
    for device in devices:
        present[device.get("role")] = present.get(device.get("role"), 0) + 1
    mismatched = {
        role: (count, declared[role]) for role, count in present.items() if role in declared and count != declared[role]
    }
    if mismatched:
        raise ValueError(
            f"{vc_name}: devices do not match the fabric_templates quantities (role: devices, declared) "
            f"{mismatched}; run add_virtual_colocation before rendering"
        )


def _ha_domains(device: dict[str, Any]) -> list[dict[str, Any]]:
    """The HA domains among a device's capabilities: the ones that carry a `mode` (a ManagedBGP has none)."""
    return [capability for capability in device.get("capabilities") or [] if "mode" in capability]


def _member_names(domain: dict[str, Any]) -> list[str]:
    return sorted(member["name"] for member in domain.get("capabilities") or [])


def _ha_of(vc_name: str, device: dict[str, Any], presence: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The renderer's HA field of one appliance: {} for a single device, `cluster` for a cluster member.

    Refuses a device in several domains, a domain that is not exactly this device and one other device of the
    presence that lists the same domain, and a mode other than active-passive.
    """
    name = device["name"]
    domains = _ha_domains(device)
    if not domains:
        return {}
    if len(domains) > 1:
        raise ValueError(
            f"{vc_name}: {name} is a member of several HA domains ({sorted(str(d.get('name')) for d in domains)}); "
            "a device belongs to at most one"
        )
    domain = domains[0]
    domain_name = domain.get("name")
    members = _member_names(domain)
    if len(members) != 2 or name not in members:
        raise ValueError(
            f"{vc_name}: HA domain {domain_name} of {name} has members {members}; expected exactly two, {name} one of them"
        )
    partner_name = next(member for member in members if member != name)
    partner = presence.get(partner_name)
    if partner is None:
        raise ValueError(
            f"{vc_name}: {name} is in HA domain {domain_name} with {partner_name}, which is not a device of this presence"
        )
    if domain_name not in {capability.get("name") for capability in _ha_domains(partner)}:
        raise ValueError(f"{vc_name}: {name} is in HA domain {domain_name} but its partner {partner_name} is not")
    mode = domain.get("mode")
    if mode != _CLUSTER:
        raise ValueError(
            f"{vc_name}: {name} is in HA domain {domain_name} with mode {mode!r}; only a cluster ({_CLUSTER!r}) is "
            "rendered, a router or any other device is a single device without an HA domain"
        )
    return {"cluster": {"name": domain_name, "node": members.index(name)}}


def _render_device(
    vc_name: str, device: dict[str, Any], metro_code: str, presence: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """The renderer's dict of one appliance: ordering parameters, plus `cluster` from its HA domain."""
    result = {"name": device["name"], "metro_code": metro_code}
    result.update({field: device.get(attribute) for field, attribute in _APPLIANCE_FIELDS.items()})
    # The device type carries the vCPU count (DcimDeviceType.cpu_count, 4 for a C8000V): it is what the order's
    # core_count is, so the appliance has no field of its own for it.
    result["core_count"] = (device.get("device_type") or {}).get("cpu_count")
    result.update(_ha_of(vc_name, device, presence))
    return result


def normalise_virtual_colocation(cleaned: dict[str, Any]) -> dict[str, Any]:
    """The renderer's input for the virtual colocation in a cleaned `virtual_colocation_config` result.

    Provider: the colocation operator above the metro, lower-cased ("EQUINIX" -> "equinix"); the renderer refuses any
    other. metro_code is the metro's name: Equinix metro codes equal our metro names (FR, PA, AM). account_number is
    the presence account's account_id and notifications the notification_emails of the account's owner.
    Every device must be a CloudNetworkAppliance. An appliance without an HA domain is a single device. An
    `active-passive` domain is a cluster named after the domain, the node being the device's index among the
    domain's members sorted by name. A device in several domains, a domain without exactly two members, a partner
    outside the presence or not in the same domain, and any other mode (active-active, cloud_managed, none) are
    refused, as is a presence with fewer devices of a role than its fabric_templates declare. A missing ordering parameter is left
    empty for the renderer to report by device.
    """
    cleaned_vc = _virtual_colocation(cleaned)
    vc_name = cleaned_vc.get("name") or "unnamed"
    metro = cleaned_vc.get("parent") or {}
    operator = (metro.get("parent") or {}).get("name")
    if not metro.get("name") or not operator:
        raise ValueError(f"{vc_name}: has no parent metro with a colocation operator above it")

    devices = cleaned_vc.get("appliances") or []
    for device in devices:
        if device.get("typename") != _APPLIANCE_KIND:
            raise ValueError(
                f"{vc_name}: {device.get('name')} is a {device.get('typename')}, not a {_APPLIANCE_KIND}; "
                "only provider-hosted appliances are rendered"
            )
    _check_complete(vc_name, cleaned_vc, devices)
    presence = {device["name"]: device for device in devices}

    account = cleaned_vc.get("account") or {}
    return {
        "name": vc_name,
        "provider": operator.lower(),
        "account_number": account.get("account_id"),
        "notifications": (account.get("owner") or {}).get("notification_emails") or [],
        "devices": [
            _render_device(vc_name, device, metro["name"], presence)
            for device in sorted(devices, key=lambda d: d["name"])
        ],
    }


class VirtualColocationTerraform(InfrahubTransform):
    """Equinix Network Edge Terraform for every device of one TopologyVirtualColocation."""

    query = "virtual_colocation_config"

    async def transform(self, data: Any) -> str:
        cleaned = clean_data(data)
        vc = _virtual_colocation(cleaned)
        if vc.get("infrastructure_orchestrator") != _ORCHESTRATOR:
            raise ValueError(
                f"{vc.get('name')}: infrastructure_orchestrator is {vc.get('infrastructure_orchestrator')!r}, "
                f"not {_ORCHESTRATOR!r}; no Terraform is rendered for it"
            )
        return render_equinix_network_devices(normalise_virtual_colocation(cleaned))
