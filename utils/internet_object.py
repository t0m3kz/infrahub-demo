"""Rules of the carriers that end on THE INTERNET's one interface (pure; see AGENTS.md "Circuit placement").

I1 one INTERNET object with one bus interface; I2 an attachment joins it to an ISP router or cloud transport; I3 an ISP
access has an attachment.
"""

from __future__ import annotations

from typing import Any

from utils.circuit_underlay import DECOMMISSIONED, INTERNET_INTERFACE_KIND, INTERNET_KIND, is_provider_device

ATTACHMENT_CARRIER_TYPE = "internet"
# Regions a public side transport may sit in; an ISP router has none, a provider connect is accepted by kind.
FAR_END_FACILITY_KINDS = ("TopologyCloudRegion", "TopologySaasRegion")
PROVIDER_CONNECT_KIND = "TopologyProviderConnect"
Finding = tuple[dict[str, Any], str]


def _ports(carrier: dict[str, Any]) -> list[dict[str, Any]]:
    return [port for port in carrier.get("capabilities") or [] if isinstance(port, dict)]


def _device(port: dict[str, Any]) -> dict[str, Any]:
    device = port.get("device")
    return device if isinstance(device, dict) else {}


def _on_internet_interface(port: dict[str, Any]) -> bool:
    return port.get("typename") == INTERNET_INTERFACE_KIND


def _facility_kind(device: dict[str, Any]) -> str | None:
    place = device.get("deployment") or device.get("region") or device.get("saas_region") or device.get("location")
    return place.get("typename") if isinstance(place, dict) else None


def _name(node: dict[str, Any]) -> str:
    return str(node.get("name") or node.get("id") or "<unnamed>")


def is_attachment(carrier: dict[str, Any]) -> bool:
    """The carrier has an end on THE INTERNET's interface (cleaned dict)."""
    return any(_on_internet_interface(port) for port in _ports(carrier))


def _object_findings(internet_objects: list[dict[str, Any]]) -> list[Finding]:
    """I1: one object, one interface, of the internet interface kind."""
    findings: list[Finding] = []
    if len(internet_objects) > 1:
        names = ", ".join(sorted(_name(node) for node in internet_objects))
        findings.append(
            (internet_objects[1], f"There are {len(internet_objects)} INTERNET objects ({names}): THE INTERNET is one.")
        )
    for node in internet_objects:
        ports = [port for port in node.get("interfaces") or [] if isinstance(port, dict)]
        wrong = [_name(port) for port in ports if not _on_internet_interface(port)]
        if len(ports) != 1 or wrong:
            findings.append(
                (
                    node,
                    f"INTERNET '{_name(node)}' has {len(ports)} interface(s), {len(wrong)} of another kind than "
                    f"{INTERNET_INTERFACE_KIND}: it has exactly one, the shared interface.",
                )
            )
    return findings


def _attachment_findings(carrier: dict[str, Any]) -> list[str]:
    """I2, for one attachment."""
    name = _name(carrier)
    messages: list[str] = []
    if carrier.get("circuit_type") != ATTACHMENT_CARRIER_TYPE:
        messages.append(
            f"Carrier circuit '{name}' ends on THE INTERNET but is a {carrier.get('circuit_type')}: an attachment is "
            f"a {ATTACHMENT_CARRIER_TYPE} circuit."
        )
    ports = _ports(carrier)
    for port in ports:
        if _on_internet_interface(port) and _device(port).get("typename") != INTERNET_KIND:
            messages.append(
                f"Carrier circuit '{name}' ends on '{_name(_device(port))}:{_name(port)}', an "
                f"{INTERNET_INTERFACE_KIND} of a device that is not THE INTERNET."
            )
    far = [port for port in ports if not _on_internet_interface(port)]
    bad = [
        _name(_device(port))
        for port in far
        if _device(port).get("typename") != PROVIDER_CONNECT_KIND
        and not is_provider_device(_device(port))
        and _facility_kind(_device(port)) not in FAR_END_FACILITY_KINDS
    ]
    if len(far) != 1 or bad:
        messages.append(
            f"Carrier circuit '{name}' ends on THE INTERNET and on {', '.join(bad) or 'its interface again'}: its other "
            "end is an ISP router, the port of a cloud or SaaS transport or a provider connect."
        )
    return messages


def internet_findings(carriers: list[dict[str, Any]], internet_objects: list[dict[str, Any]]) -> list[Finding]:
    """(carrier or INTERNET object, message) per finding, for the live carriers and the INTERNET objects."""
    findings = _object_findings(internet_objects)
    live = [carrier for carrier in carriers if carrier.get("status") != DECOMMISSIONED]
    attached: set[str] = set()
    for carrier in live:
        if is_attachment(carrier):
            findings.extend((carrier, message) for message in _attachment_findings(carrier))
            attached.update(str(_device(port).get("id")) for port in _ports(carrier))
    if internet_objects:
        seen = set(attached)
        for carrier in live:
            if is_attachment(carrier) or carrier.get("circuit_type") != ATTACHMENT_CARRIER_TYPE:
                continue
            isp_routers = [d for d in (_device(port) for port in _ports(carrier)) if is_provider_device(d)]
            if any(str(device.get("id")) in attached for device in isp_routers):
                continue
            for device in isp_routers:
                if str(device.get("id")) not in seen:
                    seen.add(str(device.get("id")))
                    findings.append(
                        (
                            carrier,
                            f"ISP router '{_name(device)}' serves internet access ('{_name(carrier)}') but has no "
                            "attachment to THE INTERNET: no public side is reachable through it.",
                        )
                    )
    return findings
