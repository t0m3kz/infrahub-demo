"""The pure half of the near-port selection of add_provider_connection.

A provider connection is a service request: it names the near facility and the far one, never the port. This module
decides, from facts the generator read, WHICH port of the near facility carries the connection and WHICH existing
carrier circuit the connection reuses. Nothing here calls the client.

Candidates: the ports of the near facility's devices that have the interface role of the far kind (:func:`near_role`):
``cloud`` for a ``TopologyCloudRegion``, ``saas`` for a ``TopologySaasRegion``, ``partner`` for everything else a
connection can land in (an external provider or a zone of another operator; an ISP is never a connection's far side,
its access is an internet carrier). A candidate is FREE when nothing sits on it (:attr:`PortFacts.busy_reasons`). The
device with the FEWEST existing connections is tried first (ties by device name) and the first free port of it by
natural name order is taken; a device without a free port hands over to the next.

An explicit ``near_port`` wins and is never replaced: if it conflicts (:attr:`PortFacts.conflict_reasons`) the choice
fails, silently taking another port would hide a mistake.

Reuse: a carrier that already ends on the chosen port, is live, has an end in the far facility and is not claimed by
another connection is the connection's carrier, found by the end edge (never by a name or a list).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

ROLE_CLOUD = "cloud"
ROLE_SAAS = "saas"
ROLE_PARTNER = "partner"
NEAR_PORT_ROLES = (ROLE_CLOUD, ROLE_SAAS, ROLE_PARTNER)

CARRIER_CROSS_CONNECT = "cross_connect"
CARRIER_VIRTUAL_CONNECTION = "virtual_connection"

_CLOUD_REGION = "TopologyCloudRegion"
_SAAS_REGION = "TopologySaasRegion"
_VIRTUAL_COLOCATION = "TopologyVirtualColocation"

_DIGITS = re.compile(r"(\d+)")


class NearPortError(ValueError):
    """No port can carry the connection (no free port, an explicit port in conflict, an ambiguous carrier)."""


@dataclass(frozen=True)
class PortFacts:
    """A port of the near facility with the facts that decide whether it is free.

    Every count EXCLUDES what belongs to the connection being planned (its own carrier, its own sub-interfaces and
    sessions): a rerun sees its own port as free again.
    """

    id: str
    name: str
    device_id: str
    device_name: str
    role: str | None = None
    cabled: bool = False
    lag_member: bool = False
    foreign_carriers: int = 0  # live carrier circuits, not claimed by this connection, with an end here
    foreign_sub_interfaces: int = 0  # sub-interfaces that are not an end of this connection's sessions
    foreign_sessions: int = 0  # session circuits of another connection with an end here

    @property
    def busy_reasons(self) -> tuple[str, ...]:
        """Why an automatic choice must not take this port (empty: free)."""
        reasons = []
        if self.cabled:
            reasons.append("cabled")
        if self.foreign_carriers:
            reasons.append("end of a carrier circuit")
        if self.foreign_sub_interfaces:
            reasons.append("has sub-interfaces")
        if self.lag_member:
            reasons.append("member of a LAG")
        if self.foreign_sessions:
            reasons.append("end of a session circuit")
        return tuple(reasons)

    @property
    def conflict_reasons(self) -> tuple[str, ...]:
        """Why an explicit ``near_port`` is refused.

        Less strict than :attr:`busy_reasons`: the operator chose the port, so its cable (a patch to the provider's own
        port) and the carriers of other connections (a port hosts one virtual connection per S-tag) are no conflict.
        """
        reasons = []
        if self.lag_member:
            reasons.append("member of a LAG")
        if self.foreign_sub_interfaces:
            reasons.append("has sub-interfaces of another use")
        if self.foreign_sessions:
            reasons.append("end of a session circuit of another connection")
        return tuple(reasons)

    @property
    def label(self) -> str:
        return f"{self.device_name}/{self.name}"


@dataclass(frozen=True)
class CarrierFacts:
    """A carrier circuit that ends on the chosen port, as far as reuse needs it."""

    id: str
    name: str
    circuit_type: str
    live: bool
    far_end_deployments: frozenset[str]  # deployments of the devices at its ends
    claimed_by: frozenset[str] = frozenset()  # ids of the provider connections that list it


def near_role(far_kind: str | None) -> str:
    """The interface role of the near ports for a far facility of this kind."""
    if far_kind == _CLOUD_REGION:
        return ROLE_CLOUD
    if far_kind == _SAAS_REGION:
        return ROLE_SAAS
    return ROLE_PARTNER


def carrier_type_for(far_kind: str | None) -> str:
    """The type of a carrier the generator creates: provider-delivered for a cloud or SaaS region, else a cross-connect."""
    if far_kind in (_CLOUD_REGION, _SAAS_REGION, _VIRTUAL_COLOCATION):
        return CARRIER_VIRTUAL_CONNECTION
    return CARRIER_CROSS_CONNECT


def natural_key(name: str) -> tuple[object, ...]:
    """Sort key putting ``Ethernet1/2`` before ``Ethernet1/10``."""
    return tuple(int(part) if part.isdigit() else part for part in _DIGITS.split(name))


def choose_near_port(
    candidates: Iterable[PortFacts], load: Mapping[str, int], *, explicit: PortFacts | None = None
) -> PortFacts:
    """The port that carries a new connection.

    ``candidates`` are the ports of the near facility with the far kind's role; ``load`` is the number of existing
    connections per device id (absent: 0); ``explicit`` is the port the request named. Raises :class:`NearPortError`.
    """
    if explicit is not None:
        reasons = explicit.conflict_reasons
        if reasons:
            raise NearPortError(f"near_port {explicit.label} cannot be used: {', '.join(reasons)}")
        return explicit
    ports = list(candidates)
    free = [port for port in ports if not port.busy_reasons]
    if not free:
        detail = "; ".join(f"{port.label}: {', '.join(port.busy_reasons)}" for port in sorted(ports, key=_port_order))
        raise NearPortError(
            "no free port with the required role in the near facility"
            + (f" ({detail})" if detail else " (none has it)")
        )
    by_device: dict[str, list[PortFacts]] = {}
    for port in free:
        by_device.setdefault(port.device_id, []).append(port)
    device_id = min(by_device, key=lambda d: (load.get(d, 0), by_device[d][0].device_name, d))
    return min(by_device[device_id], key=_port_order)


def _port_order(port: PortFacts) -> tuple[object, ...]:
    return (port.device_name, natural_key(port.name), port.id)


def connection_load(device_ids_of_connections: Iterable[Iterable[str]]) -> dict[str, int]:
    """Connections per device: ``device_ids_of_connections`` holds, per connection, the ids of the devices its carriers
    or sessions end on (a device counts once per connection)."""
    load: dict[str, int] = {}
    for device_ids in device_ids_of_connections:
        for device_id in set(device_ids):
            load[device_id] = load.get(device_id, 0) + 1
    return load


def find_reusable_carrier(
    carriers_on_port: Sequence[CarrierFacts], *, far_facility_id: str, connection_id: str
) -> CarrierFacts | None:
    """The carrier the connection reuses among those that end on its near port, or None (the generator creates one).

    Live, with an end in the far facility, and not claimed by another connection. Several such carriers are
    ambiguous (NearPortError), because nothing in the request says which one.
    """
    fits = [
        carrier
        for carrier in carriers_on_port
        if carrier.live and far_facility_id in carrier.far_end_deployments and carrier.claimed_by <= {connection_id}
    ]
    if len(fits) > 1:
        names = ", ".join(sorted(carrier.name for carrier in fits))
        raise NearPortError(f"several carrier circuits on the near port reach the far facility ({names})")
    return fits[0] if fits else None


def choose_far_port(candidates: Iterable[PortFacts]) -> PortFacts:
    """The port of a physical provider device that ends the new carrier: the first free one by name."""
    free = [port for port in candidates if not port.busy_reasons]
    if not free:
        raise NearPortError("the provider device has no free port to end the carrier on")
    return min(free, key=_port_order)
