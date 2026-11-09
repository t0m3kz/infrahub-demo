"""The PBR bindings of a switch, derived from the switch's own nested data (pure, no I/O).

PBR steering is a capability of the device (``ManagedPBR``, ``has_pbr``): the SVIs it steers to a firewall, and for which
customer footprint, carry no setting of their own, so they are derived when the device is rendered or reconciled.

- ``has_pbr``: whether the device steers (a ``ManagedPBR`` among its ``capabilities``).
- ``pbr_bindings``: the segment SVIs steered to the firewall of a footprint.
- ``overlay_pbr_bindings``: the L3 VNI SVIs of a border leaf or border spine that steer what enters the VRF from the
  overlay, per footprint.

The input is ONE cleaned device (``utils.data_cleaning.clean_data`` of a ``DcimDeviceManaged`` node) shaped by the
fragment ``DeviceServicesFields`` (queries/fragments/device_services.gql). In short:

- the device: ``typename``, ``id``, ``name``, ``role``, ``status``, ``member_of_groups``, ``deployment`` (with the
  hosting ``parent``), ``vlans`` (the segment rows of ``vrf_instances.gql``, with each footprint's ``parent`` and
  ``serving_firewall_context(s)``), ``shared_capabilities`` and ``interfaces`` (see utils/service_contexts.py for the
  ports and legs of the contexts).

Nothing here re-states a rule that exists elsewhere: the SVI steering is ``DeviceVrfFacts.pbr_footprints_by_name`` and
``pbr_overlay_footprints_by_name`` (utils/vrf_demand.py); the footprints of the overlay steering are those of the
contexts on the switch's service ports (utils/service_contexts.py ``service_footprints``).
"""

from __future__ import annotations

from typing import Any

from utils.vrf_demand import PBR_KIND, device_vrf_facts


def has_pbr(device: dict[str, Any]) -> bool:
    """Whether the cleaned device has a ``ManagedPBR`` among its ``capabilities``: it steers its tenant SVIs."""
    return any(cap.get("typename") == PBR_KIND for cap in device.get("capabilities") or [])


def pbr_bindings(device: dict[str, Any]) -> dict[str, str]:
    """``Vlan<id> -> footprint id`` of the segment SVIs the device steers to a firewall.

    A routed tenant segment's SVI is steered for its footprint in the device's hosting DC that has a serving firewall
    context (the lowest id when there are two); a terminated or gateway-less segment has no SVI, and a border leaf or
    border spine steers none (utils/vrf_demand.py ``DeviceVrfFacts.pbr_footprints_by_name``)."""
    facts = device_vrf_facts(device)
    return {name: footprint for name, footprint in facts.pbr_footprints_by_name().items() if footprint}


def overlay_pbr_bindings(device: dict[str, Any]) -> dict[str, frozenset[str]]:
    """``Vlan<slot> -> footprint ids`` of the L3 VNI SVIs of a border leaf or border spine that steer what enters the VRF
    from the overlay to the firewall of those footprints (``pbr_overlay_footprints_by_name``)."""
    return device_vrf_facts(device).pbr_overlay_footprints_by_name()
