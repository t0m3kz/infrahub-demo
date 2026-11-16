"""The plain-uplink dual-homing flow of the endpoint generator, as pure builders: which of a server's free uplinks land on
which port of which switch of a pair (speed-matched, alternating between the two switches), and the plan of their cables
(``plan_link`` of generators/dcim/cabling_plan.py). The device grouping and switch-pair selection are shared with the
LAG flow (endpoint_lag.py). Nothing here reads or writes.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from netutils.interface import sort_interface_list

from generators.connectivity.connection_fingerprint import ConnectionFingerprint
from generators.connectivity.endpoint_targets import interface_type_of
from generators.dcim.cabling_plan import PortFacts, plan_link
from generators.helpers.cable_attributes import RackPosition
from generators.helpers.cabling import ConnectionValidator, InterfaceSpeedMatcher, pick_matched_switch_port_name
from generators.plan import TopologyPlan
from generators.protocols import DcimPhysicalInterface

ENDPOINT_P2P_PREFIX_LENGTH = 31
"""Unused by an endpoint cable (it carries no address, ``pool_id`` is None); the length ``plan_link`` asks for."""


def interface_device_name(intf: Any) -> str | None:
    """The interface's device name (queried with ``include=["device"]``), or None without a device peer."""
    device = intf.device.peer
    return str(device.name.value) if device else None


def group_by_device(interfaces: Sequence[Any], logger: logging.Logger) -> dict[str, list[Any]]:
    """Interfaces keyed by device name, in input order; unresolvable ones are dropped with a warning."""
    by_device: dict[str, list[Any]] = {}
    for intf in interfaces:
        device_name = interface_device_name(intf)
        if not device_name:
            logger.warning(f"Could not determine device name for interface {intf.name.value}")
            continue
        by_device.setdefault(device_name, []).append(intf)
    return by_device


def select_switch_pair(device_names: list[str], existing_switch_names: set[str]) -> list[str]:
    """Up to two switches to dual-home onto: those already cabled to (prior run) first, so new ports land on the same
    pair.
    """
    sticky = sorted(name for name in device_names if name in existing_switch_names)
    remaining = [name for name in device_names if name not in existing_switch_names]
    return (sticky + remaining)[:2]


def plan_uplink_connections(
    *,
    endpoint_name: str,
    free_interfaces: list[DcimPhysicalInterface],
    target_interfaces: list[DcimPhysicalInterface],
    existing_switch_names: set[str],
    logger: logging.Logger,
) -> list[ConnectionFingerprint]:
    """Dual-home the endpoint's free uplinks onto a switch pair: the connections, speed group by speed group. A target
    list that is empty, has fewer than two devices or shares no speed with the uplinks is an error (``logger.error``).
    """
    if not target_interfaces:
        logger.error(
            f"Endpoint {endpoint_name}: No compatible interfaces found on target devices. "
            "Cannot create endpoint connectivity."
        )
        return []

    device_groups = group_by_device(target_interfaces, logger)
    if len(device_groups) < 2:
        logger.error(
            f"Endpoint {endpoint_name}: Need at least 2 devices for dual-homing, found {len(device_groups)}. "
            "Cannot create endpoint connectivity."
        )
        return []

    selected_devices = select_switch_pair(list(device_groups), existing_switch_names)
    logger.info(f"Selected device pair for {endpoint_name}: {selected_devices}")

    speed_groups = InterfaceSpeedMatcher.group_by_speed(
        server_interfaces=free_interfaces,
        switch_interfaces=[intf for name in selected_devices for intf in device_groups[name]],
    )
    if not speed_groups:
        logger.error(
            f"Endpoint {endpoint_name}: No matching speed groups found between endpoint and {selected_devices}. "
            "Cannot create endpoint connectivity (speed-aware mode)."
        )
        return []

    logger.info(f"Found {len(speed_groups)} speed group(s): {sorted(speed_groups.keys())} Mbps")
    planned: list[ConnectionFingerprint] = []
    for speed, (server_intfs, switch_intfs) in sorted(speed_groups.items()):
        logger.info(
            f"Processing {speed} Mbps group: {len(server_intfs)} server interfaces, {len(switch_intfs)} switch interfaces"
        )
        connection_plan = build_connection_plan(
            endpoint_name=endpoint_name,
            server_interfaces=server_intfs,
            switch_interfaces=switch_intfs,
            target_device_names=selected_devices,
            planned=set(planned),
            logger=logger,
        )
        if not connection_plan:
            logger.warning(f"No connection plan created for {speed} Mbps group")
            continue
        is_valid, message = ConnectionValidator.validate_plan(connection_plan, min_connections=1)
        if not is_valid:
            logger.error(f"Connection plan validation failed for {speed} Mbps: {message}")
            continue
        logger.info(f"Connection plan for {speed} Mbps: {len(connection_plan)} connection(s) planned")
        planned.extend(connection_plan)
    logger.info(f"Completed all connectivity for {endpoint_name}: {len(planned)} total connection(s) established")
    return planned


def build_connection_plan(
    *,
    endpoint_name: str,
    server_interfaces: list[Any],
    switch_interfaces: list[DcimPhysicalInterface],
    target_device_names: list[str],
    planned: set[ConnectionFingerprint],
    logger: logging.Logger,
) -> list[ConnectionFingerprint]:
    """The connections of one speed group, as fingerprints (an already planned one is left out)."""
    plan: list[ConnectionFingerprint] = []

    # Regroup this speed's interfaces by device, in netutils interface order
    switch_by_device: dict[str, list[DcimPhysicalInterface]] = {}
    for device_name, intfs in group_by_device(switch_interfaces, logger).items():
        interface_map = {intf.name.value: intf for intf in intfs}
        switch_by_device[device_name] = [interface_map[name] for name in sort_interface_list(list(interface_map))]

    logger.info(
        f"Grouped {len(switch_interfaces)} interfaces into {len(switch_by_device)} devices: {list(switch_by_device.keys())}"
    )
    for dev_name, intfs in switch_by_device.items():
        logger.debug(f"  {dev_name}: {len(intfs)} interfaces")

    # Natural interface order, not query order, so "first 4" picks eth0-eth3
    server_intf_by_name = {intf.name.value: intf for intf in server_interfaces}
    sorted_server_names = sort_interface_list(list(server_intf_by_name.keys()))

    server_intfs = [server_intf_by_name[name] for name in sorted_server_names[:4]]

    logger.info(f"Planning connections for {len(server_intfs)} server interfaces (requested: {len(server_interfaces)})")
    logger.info(f"Target devices: {target_device_names}")

    # Alternate between switches, two server interfaces at a time, landing on the same port name on both switches
    # when possible, else each switch's first free port
    switch_a_name, switch_b_name = target_device_names[0], target_device_names[1]
    for pair_start in range(0, len(server_intfs), 2):
        pair = server_intfs[pair_start : pair_start + 2]
        switch_names_for_pair = [switch_a_name, switch_b_name][: len(pair)]

        matched_name = None
        if len(pair) == 2:
            free_names_by_switch = {
                name: [intf.name.value for intf in switch_by_device.get(name, [])]
                for name in (switch_a_name, switch_b_name)
            }
            matched_name = pick_matched_switch_port_name(free_names_by_switch, (switch_a_name, switch_b_name))

        for server_intf, switch_name in zip(pair, switch_names_for_pair):
            available_switch_intfs = switch_by_device.get(switch_name, [])
            server_intf_name = server_intf.name.value

            if not available_switch_intfs:
                # A NIC that should be dual-homed but has no switch port left is a failed deployment
                logger.error(f"No available interfaces on {switch_name} for {server_intf_name}")
                continue

            if matched_name is not None:
                switch_intf = next(intf for intf in available_switch_intfs if intf.name.value == matched_name)
            else:
                # No common free port name: fall back to each switch's first free port
                switch_intf = available_switch_intfs[0]
            available_switch_intfs.remove(switch_intf)

            fingerprint = ConnectionFingerprint(
                server_name=endpoint_name,
                server_interface=server_intf_name,
                switch_name=switch_name,
                switch_interface=switch_intf.name.value,
            )

            if fingerprint not in planned:
                plan.append(fingerprint)

    return plan


def plan_cables(
    plan: TopologyPlan,
    connections: list[tuple[ConnectionFingerprint, str | None, str | None]],
    *,
    deployment_id: str,
    positions: Mapping[str, RackPosition | None],
) -> None:
    """Add the cable of every connection to ``plan``: (fingerprint, the server port's type, the switch port's type). The
    ports are the existing ones (updated, never tracked), the cable is the run's own, no addresses.
    """
    for fingerprint, server_type, switch_type in connections:
        plan_link(
            plan,
            PortFacts(fingerprint.server_name, fingerprint.server_interface, server_type),
            PortFacts(fingerprint.switch_name, fingerprint.switch_interface, switch_type),
            deployment_id=deployment_id,
            pool_id=None,
            p2p_prefix_length=ENDPOINT_P2P_PREFIX_LENGTH,
            positions=positions,
        )


def with_port_types(
    fingerprints: list[ConnectionFingerprint],
    server_interfaces: Sequence[Any],
    target_interfaces: Sequence[Any],
) -> list[tuple[ConnectionFingerprint, str | None, str | None]]:
    """Each fingerprint with the interface types of its two ports, read off the interfaces the plan was built from."""
    server_types = {intf.name.value: interface_type_of(intf) for intf in server_interfaces}
    switch_types = {
        (interface_device_name(intf), intf.name.value): interface_type_of(intf) for intf in target_interfaces
    }
    return [
        (
            fingerprint,
            server_types.get(fingerprint.server_interface),
            switch_types.get((fingerprint.switch_name, fingerprint.switch_interface)),
        )
        for fingerprint in fingerprints
    ]
