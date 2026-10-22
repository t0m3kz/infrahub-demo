"""The physical dependencies of a virtual circuit, shared by the generator and the check.

A virtual circuit's underlay is derived, never declared: the TopologyPhysicalCircuit entries found in the
``capabilities`` of the circuit's own ports. Both ``generators/topology/circuit.py`` (session status, waits) and
``checks/circuit_placement.py`` (contiguity) read the same cleaned dicts through :func:`derived_underlay`.
"""

from __future__ import annotations

from typing import Any

DECOMMISSIONED = "decommissioned"
PHYSICAL_CIRCUIT = "TopologyPhysicalCircuit"

# Link types of a tunnel of ours: an overlay session is built for these only. Any other link type is
# provider-managed (cloud on-ramps, exchange fabrics, MPLS): the session there is the provider's.
TUNNEL_LINK_TYPES = frozenset({"sd_wan", "vpn_ipsec", "vpn_ssl", "gre", "geneve"})


def derived_underlay(circuit: dict[str, Any]) -> list[dict[str, Any]]:
    """The physical dependencies at a virtual circuit's ports: the physical circuits terminating on them.

    Each of the circuit's ``capabilities`` interfaces lists the entries that terminate on it in its own
    ``capabilities`` (which also hold segments, peerings and other circuits); the TopologyPhysicalCircuit ones,
    deduplicated by id in first-seen order, are the underlay, whatever their status.

    Limitations, all silent: only the circuit's own ports count, there is no walk to a parent interface, so an end
    on a logical port (LAG, sub-interface) derives nothing for that end; a middle hop of a chain that touches none
    of the circuit's ports is not found; and a port removed from a physical circuit reruns nothing, so the virtual
    circuits that sat on it keep their session until their next run.
    """
    underlay: dict[str, dict[str, Any]] = {}
    for interface in circuit.get("capabilities") or []:
        for cap in interface.get("capabilities") or []:
            if isinstance(cap, dict) and cap.get("typename") == PHYSICAL_CIRCUIT and cap.get("id"):
                underlay.setdefault(cap["id"], cap)
    return list(underlay.values())
