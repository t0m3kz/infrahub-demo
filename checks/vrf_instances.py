"""VRF instances (ManagedVRF) match what each device routes in, and its segment SVIs match its VLAN-domain rows.

A device has an instance exactly for the non-``default`` namespaces it routes in (utils/vrf_demand.py is the one
derivation, shared with the generator that writes them). The generator converges them on every run, but a device
edit (role, deployment, an MLAG formed) re-runs nothing, so the check reports what drifted. Every rule is a pure
function over ``DeviceVrfFacts``; the check class only logs.

Instances:

- (a) ERROR: a namespace the device needs has no instance;
- (b) WARNING: an instance for a namespace the device does not need (the generator removes it on its next run);
- (c) ERROR: two instances of one namespace on a device, or one instance linked to two devices;
- (d) ERROR: an instance whose namespace has no ``l3_vni`` or ``namespace_type``, or is ``default``;
- (e) ERROR: an instance on a device that never carries one (spine, L2 leaf, firewall, load balancer, party device).

Segment SVIs (``DcimVirtualInterface`` with role ``svi``, a Component of its VLAN-domain row):

- S1 ERROR: each routed row of the domain has exactly one SVI named ``Vlan<vlan_id>`` linked to it;
- S2 WARNING: an SVI with no row or whose row is not routed (the generator deletes it on its next run);
- S3 ERROR: its ``vrf`` is one of the device's own instances in the namespace the row routes in (``vrf`` may be
  unset only for a ``default`` row);
- S4 ERROR: it has no address (the anycast gateway is ``segment.gateway``) and no capabilities;
- S5 ERROR: its row is one of the device's own VLAN-domain rows (``common_parent`` cannot say so).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any

from checks.base import WarningCheck
from utils.data_cleaning import clean_data
from utils.limits import DEFAULT_NAMESPACE
from utils.vrf_demand import DeviceVrfFacts, InstanceFacts, device_vrf_facts

_Rule = Callable[[DeviceVrfFacts], list[str]]


def _why(facts: DeviceVrfFacts, namespace: str) -> str:
    """Why the device needs an instance of ``namespace``: the sources that name it."""
    inputs = facts.inputs
    sources = (
        ("a routed segment row of its VLAN domain", inputs.row_namespaces),
        ("an SVI", inputs.svi_namespaces),
        ("an address on one of its interfaces", inputs.address_namespaces),
        ("a firewall or load-balancer context leg on a service port", inputs.leg_namespaces),
    )
    return ", ".join(label for label, found in sources if namespace in found)


def missing_instance_errors(facts: DeviceVrfFacts) -> list[str]:
    """(a) a demanded namespace without an instance."""
    have = {instance.namespace for instance in facts.instances}
    return [
        f"VRF (a): device '{facts.name}' routes in namespace '{namespace}' ({_why(facts, namespace)}) but has no "
        "ManagedVRF instance of it. Re-run the generator of that object (segment, circuit or context) to create it."
        for namespace in sorted(facts.demand - have)
    ]


def unneeded_instance_warnings(facts: DeviceVrfFacts) -> list[str]:
    """(b) an instance of a namespace the device does not need (only a device that can carry one: (e) covers the rest)."""
    if not facts.carries_vrfs:
        return []
    return [
        f"VRF (b): device '{facts.name}' has ManagedVRF '{instance.name}' but nothing on it routes in namespace "
        f"'{instance.namespace}'. The generator removes it on its next run, or delete it."
        for instance in facts.instances
        if instance.namespace not in facts.demand
    ]


def duplicate_instance_errors(facts: DeviceVrfFacts) -> list[str]:
    """(c) more than one instance of a namespace on one device."""
    by_namespace: dict[str | None, list[InstanceFacts]] = defaultdict(list)
    for instance in facts.instances:
        by_namespace[instance.namespace].append(instance)
    return [
        f"VRF (c): device '{facts.name}' has {len(found)} ManagedVRF instances of namespace '{namespace}': "
        f"{', '.join(sorted(str(instance.name) for instance in found))}. Keep one and delete the others."
        for namespace, found in sorted(by_namespace.items(), key=lambda item: str(item[0]))
        if len(found) > 1
    ]


def shared_instance_errors(devices: list[DeviceVrfFacts]) -> list[str]:
    """(c) one instance linked to more than one device (an instance is the capability of exactly one)."""
    owners: dict[str, dict[str, str]] = defaultdict(dict)
    names: dict[str, str | None] = {}
    for facts in devices:
        for instance in facts.instances:
            owners[instance.id][facts.id] = facts.name
            names[instance.id] = instance.name
    return [
        f"VRF (c): ManagedVRF '{names[instance_id]}' is linked to {len(found)} devices: "
        f"{', '.join(sorted(found.values()))}. An instance belongs to one device; give each its own."
        for instance_id, found in sorted(owners.items())
        if len(found) > 1
    ]


def namespace_errors(facts: DeviceVrfFacts) -> list[str]:
    """(d) an instance whose namespace cannot be realized: no l3_vni, no namespace_type, or ``default``."""
    errors = []
    for instance in facts.instances:
        if instance.namespace == DEFAULT_NAMESPACE:
            problem = "is the underlay namespace 'default', which is never an instance"
        elif instance.l3_vni is None or not instance.namespace_type:
            problem = "has no l3_vni or no namespace_type"
        else:
            continue
        errors.append(
            f"VRF (d): ManagedVRF '{instance.name}' on device '{facts.name}': namespace '{instance.namespace}' "
            f"{problem}. Delete the instance, or set l3_vni and namespace_type on the namespace."
        )
    return errors


def uncarried_instance_errors(facts: DeviceVrfFacts) -> list[str]:
    """(e) an instance on a device that never carries one."""
    if facts.carries_vrfs:
        return []
    return [
        f"VRF (e): device '{facts.name}' (role '{facts.inputs.role}') never carries VRF instances but has ManagedVRF "
        f"'{instance.name}'. Delete it (spines, L2 leaves, firewalls, load balancers and party devices have none)."
        for instance in facts.instances
    ]


def svi_missing_errors(facts: DeviceVrfFacts) -> list[str]:
    """S1 each routed row of the domain has exactly one role-svi interface ``Vlan<vlan_id>`` linked to it."""
    errors = []
    for name, row in facts.desired_svis().items():
        found = [svi for svi in facts.svis if svi.name == name and svi.row_id == row.id]
        if len(found) != 1:
            errors.append(
                f"SVI (S1): device '{facts.name}' has {len(found)} svi interfaces '{name}' linked to the row of "
                f"segment '{row.segment_name}' (VLAN {row.vlan_id}), expected exactly 1. Re-run add_vxlan_segment "
                "for the segment to converge them."
            )
    return errors


def svi_orphan_warnings(facts: DeviceVrfFacts) -> list[str]:
    """S2 an SVI with no row, or whose row is not routed."""
    return [
        f"SVI (S2): svi interface '{svi.name}' on device '{facts.name}' "
        + ("has no VLAN-domain row" if svi.row_id is None else "sits on a row the fabric does not route")
        + ". The segment reconcile deletes it on its next run, or delete it."
        for svi in facts.svis
        if svi.row_id is None or svi.row_id in facts.unrouted_row_ids
    ]


def svi_vrf_errors(facts: DeviceVrfFacts) -> list[str]:
    """S3 ``svi.vrf`` is set (unless the row is ``default``), one of the device's instances, in the row's namespace."""
    rows = {row.id: row for row in facts.routed_rows}
    instance_ids = {instance.id for instance in facts.instances}
    errors = []
    for svi in facts.svis:
        row = rows.get(svi.row_id or "")
        if row is None:
            continue
        where = f"svi interface '{svi.name}' on device '{facts.name}'"
        if svi.vrf_id is None:
            if row.namespace != DEFAULT_NAMESPACE:
                errors.append(
                    f"SVI (S3): {where} has no vrf, its row routes in namespace '{row.namespace}'. "
                    "Re-run add_vxlan_segment for the segment."
                )
        elif svi.vrf_id not in instance_ids:
            errors.append(
                f"SVI (S3): {where} points at a ManagedVRF that is not one of the device's own instances. "
                "Point it at the device's instance of the row's namespace."
            )
        elif svi.vrf_namespace != row.namespace:
            errors.append(
                f"SVI (S3): {where} is in VRF namespace '{svi.vrf_namespace}', its row routes in '{row.namespace}'. "
                "Re-run add_vxlan_segment for the segment."
            )
    return errors


def svi_content_errors(facts: DeviceVrfFacts) -> list[str]:
    """S4 an SVI has no address and no capabilities."""
    return [
        f"SVI (S4): svi interface '{svi.name}' on device '{facts.name}' has "
        + ("an address" if svi.has_address else "capabilities")
        + " (the anycast gateway is segment.gateway and a port carries the segment, not its SVI). Remove it."
        for svi in facts.svis
        if svi.has_address or svi.has_capabilities
    ]


def svi_domain_errors(facts: DeviceVrfFacts) -> list[str]:
    """S5 the SVI's row is one of the device's own VLAN-domain rows."""
    return [
        f"SVI (S5): svi interface '{svi.name}' on device '{facts.name}' is linked to a row outside the device's "
        "VLAN domain (its MLAG's when paired, else its own). Re-run add_vxlan_segment, which deletes it."
        for svi in facts.svis
        if svi.row_id is not None and svi.row_id not in facts.domain_row_ids
    ]


ERROR_RULES: tuple[_Rule, ...] = (
    missing_instance_errors,
    duplicate_instance_errors,
    namespace_errors,
    uncarried_instance_errors,
    svi_missing_errors,
    svi_vrf_errors,
    svi_content_errors,
    svi_domain_errors,
)
WARNING_RULES: tuple[_Rule, ...] = (unneeded_instance_warnings, svi_orphan_warnings)


class CheckVrfInstances(WarningCheck):
    """Every device's ManagedVRF instances and segment SVIs match what the fabric says it routes (rules above)."""

    query = "vrf_instances"

    def validate(self, data: Any) -> None:
        devices = [device_vrf_facts(device) for device in clean_data(data).get("DcimDevice") or []]
        for facts in devices:
            for rule in ERROR_RULES:
                for message in rule(facts):
                    self.log_error(message=message)
            for rule in WARNING_RULES:
                for message in rule(facts):
                    self.log_warning(message=message)
        for message in shared_instance_errors(devices):
            self.log_error(message=message)
