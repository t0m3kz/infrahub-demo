"""The closure rules of the BGP route protection: what every session end may carry, compared end to end. No I/O.

``checks/bgp_expectations.py`` feeds this the cleaned ``bgp_expectations`` query and reports the findings. The
expectations are the ones the device transforms render (utils/bgp_expectation.py, facts read by
transforms/helpers/bgp_facts.py), derived here for BOTH ends of every peering from each end's own facts, so a filter
that disagrees with the model, or with the other end, shows up before ``routing_filter_enforce`` is turned on.

- E1 `out(B)` is a subset of `in(A)` for the two ends of every underlay, fabric and DCI underlay session.
- E2 every loopback and multisite VIP of a device is accepted by every underlay inbound of its loopback pool (a hop of
  its path), so it is inside the pool; one outside it reaches no further than its first hop.
- E3 every connected prefix of a device in an environment VRF is accepted by the EVPN filter that would carry its
  type-5 route (it is a prefix of the VRF's namespace in the model).
- E4 a device with an upstream transit session is a default exit, the only device an EVPN filter takes a default from.
- E5 a session is rendered unprotected: a family that cannot be derived from the query data, or a platform that renders
  no filter for it.

A finding is an error where the end's hosting deployment enforces (``routing_filter_enforce``: the filter would be
live, so the mismatch is an outage) and a warning where it stages.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from ipaddress import ip_interface
from typing import Any

from transforms.helpers.bgp import _build_session_from_peering
from transforms.helpers.bgp_facts import build_device, build_fabric, routing_filter_enforce
from transforms.helpers.bgp_policy import EVPN_FILTER_PLATFORMS, UNICAST_FILTER_PLATFORMS
from utils.bgp_expectation import (
    DCI_UNDERLAY,
    EVPN,
    FABRIC,
    UNCLASSIFIED,
    UNDERLAY,
    Device,
    Entry,
    Expectation,
    Fabric,
    accepts,
    covers,
    exact_entry,
    expect_session,
    host_entry,
    session_class,
)
from utils.limits import DEFAULT_NAMESPACE

PAIR_CLASSES = (UNDERLAY, FABRIC, DCI_UNDERLAY)


@dataclass(frozen=True)
class Finding:
    """One result of the closure check: ``severity`` is ``error`` or ``warning``, ``rule`` E1..E5."""

    severity: str
    rule: str
    message: str


@dataclass(frozen=True)
class End:
    """One end of a peering: the session as that device builds it, its expectation and what decides the severity."""

    peering: str
    device: Device
    platform: str
    enforce: bool
    session: dict[str, Any]
    expectation: Expectation
    fabric: Fabric


def _severity(enforce: bool) -> str:
    return "error" if enforce else "warning"


def _process_asn(peering: dict[str, Any], device: str) -> int | None:
    """The ASN of the BGP process of ``device`` in a peering."""
    for process in peering.get("bgp_processes") or []:
        names = {d.get("name") for d in process.get("capabilities") or [] if isinstance(d, dict)}
        asn = (process.get("local_as") or {}).get("asn")
        if device in names and isinstance(asn, int):
            return asn
    return None


def build_ends(cleaned: dict[str, Any]) -> list[End]:
    """Both ends of every peering of the cleaned query, with the expectation each derives."""
    devices = {d["name"]: d for d in cleaned.get("DcimDevice") or [] if d.get("name")}
    base = build_fabric({}, cleaned)
    by_ranges: dict[tuple[str, ...], Fabric] = {}
    ends: list[End] = []
    for peering in cleaned.get("ManagedBGPPeering") or []:
        for capability in peering.get("capabilities") or []:
            name = str((capability.get("device") or {}).get("name") or "")
            node = devices.get(name)
            asn = _process_asn(peering, name)
            if node is None or asn is None:
                continue
            try:
                session = _build_session_from_peering(peering, name, {"asn": asn}, None, warnings=[])
            except ValueError:
                # A peering that is not two ends (malformed data): the transform refuses it, the model checks report it.
                continue
            if session is None:
                continue
            deployment = node.get("deployment")
            ranges = build_fabric(node, {}).loopback_ranges
            fabric = by_ranges.setdefault(ranges, replace(base, loopback_ranges=ranges))
            device = build_device(node)
            ends.append(
                End(
                    peering=str(peering.get("name")),
                    device=device,
                    platform=str((node.get("platform") or {}).get("name") or ""),
                    enforce=routing_filter_enforce(deployment),
                    session=session,
                    expectation=expect_session(session, device, fabric),
                    fabric=fabric,
                )
            )
    return ends


def _show(entries: list[Entry]) -> str:
    return ", ".join(e.prefix + (f" le {e.le}" if e.le is not None else "") for e in entries[:6]) + (
        f" and {len(entries) - 6} more" if len(entries) > 6 else ""
    )


def pair_findings(ends: list[End]) -> list[Finding]:
    """E1: what one end announces, the other end accepts (underlay, fabric iBGP, DCI underlay)."""
    by_peering: dict[str, list[End]] = {}
    for end in ends:
        if end.expectation.klass in PAIR_CLASSES:
            by_peering.setdefault(end.peering, []).append(end)
    findings: list[Finding] = []
    for peering, both in sorted(by_peering.items()):
        if len(both) != 2:
            continue
        for sender, receiver in (both, both[::-1]):
            for family, out in sender.expectation.families.items():
                inbound = receiver.expectation.families.get(family)
                if family == EVPN or out.outbound is None or inbound is None or inbound.inbound is None:
                    continue
                missing = sorted(e for e in out.outbound if not covers(inbound.inbound, frozenset({e})))
                if missing:
                    findings.append(
                        Finding(
                            _severity(receiver.enforce),
                            "E1",
                            f"{peering} [{family}]: {sender.device.name} announces {_show(missing)}, which "
                            f"{receiver.device.name} would filter out",
                        )
                    )
    return findings


def loopback_findings(ends: list[End], devices: dict[str, Device]) -> list[Finding]:
    """E2: every loopback and VIP is accepted by every underlay inbound of its pool."""
    findings: list[Finding] = []
    seen: set[tuple[str, str, str]] = set()
    for end in ends:
        if end.expectation.klass != UNDERLAY:
            continue
        for family, policy in end.expectation.families.items():
            if policy.inbound is None:
                continue
            for device in devices.values():
                for address in sorted(device.addrs.loopbacks | device.addrs.vips):
                    entry = host_entry(address)
                    key = (end.device.name, device.name, address)
                    if entry.family != family or key in seen or not end.fabric.loopback_ranges:
                        continue
                    seen.add(key)
                    if not accepts(policy.inbound, entry.prefix):
                        findings.append(
                            Finding(
                                _severity(end.enforce),
                                "E2",
                                f"{device.name} address {address} is not accepted by the underlay inbound filter of "
                                f"{end.device.name} (session {end.peering}): it is outside the loopback pool",
                            )
                        )
    return findings


def connected_prefix_findings(cleaned: dict[str, Any], ends: list[End]) -> list[Finding]:
    """E3: a connected prefix in an environment VRF is a prefix of that namespace, or its type-5 route is filtered."""
    carriers = {end.device.name: end for end in ends if EVPN in end.expectation.families}
    findings: list[Finding] = []
    for node in cleaned.get("DcimDevice") or []:
        end = carriers.get(node.get("name"))
        if end is None:
            continue
        for interface in node.get("interfaces") or []:
            address = (interface.get("ip_address") or {}).get("address")
            namespace = ((interface.get("ip_address") or {}).get("ip_namespace") or {}).get("name")
            if not address or not namespace or namespace == DEFAULT_NAMESPACE:
                continue
            known = end.fabric.namespace_prefixes.get(namespace)
            if known is None:
                continue
            network = str(ip_interface(address).network)
            allowed = frozenset(
                Entry(p, ip_interface(p).network.max_prefixlen)
                if namespace in end.fabric.range_namespaces
                else exact_entry(p)
                for p in known
            )
            if not accepts(allowed, network):
                findings.append(
                    Finding(
                        _severity(end.enforce),
                        "E3",
                        f"{node['name']} {interface.get('name')} {address} in {namespace}: {network} is no prefix of "
                        f"{namespace} in the model, so the EVPN filter drops its type-5 route",
                    )
                )
    return findings


def default_exit_findings(ends: list[End]) -> list[Finding]:
    """E4: a device with an upstream transit session is a default exit."""
    findings: list[Finding] = []
    for end in ends:
        if not end.session.get("transit"):
            continue
        namespace = str(end.session.get("vrf"))
        if end.device.name not in end.fabric.default_exits.get(namespace, frozenset()):
            findings.append(
                Finding(
                    _severity(end.enforce),
                    "E4",
                    f"{end.device.name} has the transit session {end.peering} in {namespace} but is no default exit "
                    "(a border gateway in the internet VRF instance): no EVPN filter takes its default route",
                )
            )
    return findings


def unprotected_findings(ends: list[End]) -> list[Finding]:
    """E5: a session end the route protection leaves open, and why."""
    findings: list[Finding] = []
    for end in ends:
        klass = session_class(end.session, end.fabric)
        if klass == UNCLASSIFIED:
            findings.append(
                Finding("warning", "E5", f"{end.peering} on {end.device.name}: class not covered (SD-WAN/LISP)")
            )
            continue
        for family, policy in end.expectation.families.items():
            if klass in ("external", "transit"):
                continue
            if policy.inbound is None or policy.outbound is None:
                why = "cannot be derived from the model (no loopback pool, far end or environment prefixes)"
            elif family == EVPN and end.platform not in EVPN_FILTER_PLATFORMS:
                why = f"{end.platform or 'this platform'} renders no EVPN filter"
            elif family != EVPN and end.platform not in UNICAST_FILTER_PLATFORMS:
                why = f"{end.platform or 'this platform'} renders no prefix filter"
            else:
                continue
            findings.append(
                Finding("warning", "E5", f"{end.peering} on {end.device.name} [{family}] is unprotected: {why}")
            )
    return findings


def analyse(cleaned: dict[str, Any]) -> list[Finding]:
    """Every finding of the cleaned ``bgp_expectations`` query, in rule order."""
    ends = build_ends(cleaned)
    devices = {end.device.name: end.device for end in ends}
    return [
        *pair_findings(ends),
        *loopback_findings(ends, devices),
        *connected_prefix_findings(cleaned, ends),
        *default_exit_findings(ends),
        *unprotected_findings(ends),
    ]
