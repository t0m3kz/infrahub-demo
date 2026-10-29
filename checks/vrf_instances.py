"""VRF instances (ManagedVRF) match what each device routes in, and its segment SVIs match its VLAN-domain rows.

A device has an instance exactly for the non-``default`` namespaces it routes in (utils/vrf_demand.py is the one
derivation, shared with the generator that writes them). The generator converges them on every run, but a device
edit (role, deployment, an MLAG formed) re-runs nothing, so the check reports what drifted. Every rule is a pure
function over ``DeviceVrfFacts``; the check class only logs.

An instance is one namespace in one topology (its ``deployment``: the DC, colocation metro or office that hosts the
device, ``hosting_deployment``); every device of that topology that routes in the namespace is a member
(``device.vrfs``). Instances:

- (a) ERROR: a namespace the device needs has no instance of its hosting deployment it is a member of;
- (b) WARNING: a membership in an instance of a namespace the device does not need (the next reconcile drops it);
- (c) ERROR: a membership in an instance of another deployment than the device's hosting one, or in two instances
  of one namespace;
- (d) ERROR: an instance whose namespace has no ``l3_vni`` or ``namespace_type``, or is ``default``;
- (e) ERROR: an instance on a device that never carries one (spine, L2 leaf, firewall, load balancer, party device;
  not a decommissioned device: its leftover membership is the (b) WARNING);
- (f) WARNING: an instance with no member (the reconcile sweep missed it);
- (g) ERROR: the device needs an instance but has no hosting deployment (its ``deployment`` is neither a DC, a metro
  or an office, nor a pod or zone of one).

Segment SVIs (``DcimVirtualInterface`` with role ``svi``, a Component of its VLAN-domain row):

- S1 ERROR: each routed row of the domain has exactly one SVI named ``Vlan<vlan_id>`` linked to it;
- S2 WARNING: an SVI with no row or whose row is not routed, or any SVI left on a decommissioned device (the generator
  deletes it on its next run; S1, S3, S4 and S5 do not report a decommissioned device);
- S3 ERROR: its ``vrf`` is the instance of (the device's hosting deployment, the row's namespace) and the device is
  one of its members (``vrf`` may be unset only for a ``default`` row);
- S4 ERROR: it has no address (the anycast gateway is ``segment.gateway``) and no capabilities;
- S5 ERROR: its row is one of the device's own VLAN-domain rows (``common_parent`` cannot say so).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any

from checks.base import WarningCheck
from utils.data_cleaning import clean_data
from utils.limits import DEFAULT_NAMESPACE
from utils.vrf_demand import DECOMMISSIONED_STATUS, DeviceVrfFacts, InstanceFacts, all_instances, device_vrf_facts

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


def _decommissioned(facts: DeviceVrfFacts) -> bool:
    """Whether the device is out of service: it has no demand and no desired SVI, so only leftovers are reported."""
    return facts.inputs.status == DECOMMISSIONED_STATUS


def _hosted(facts: DeviceVrfFacts, namespace: str | None) -> InstanceFacts | None:
    """The device's membership in the instance of (its hosting deployment, ``namespace``), None when it has none."""
    return next(
        (
            i
            for i in facts.instances
            if i.namespace == namespace and facts.hosting_id and i.deployment_id == facts.hosting_id
        ),
        None,
    )


def missing_instance_errors(facts: DeviceVrfFacts) -> list[str]:
    """(a) a demanded namespace without a membership in the instance of the device's hosting deployment."""
    if not facts.hosting_id:
        return []
    return [
        f"VRF (a): device '{facts.name}' routes in namespace '{namespace}' ({_why(facts, namespace)}) but is not a "
        f"member of the ManagedVRF of '{facts.hosting_name}' for it. Re-run the generator of that object (segment, "
        "circuit or context) to create it."
        for namespace in sorted(facts.demand)
        if _hosted(facts, namespace) is None
    ]


def no_hosting_errors(facts: DeviceVrfFacts) -> list[str]:
    """(g) a device that needs an instance but has no hosting deployment to hold it."""
    if facts.hosting_id or not facts.demand:
        return []
    return [
        f"VRF (g): device '{facts.name}' routes in {', '.join(sorted(facts.demand))} but its deployment is not a "
        "data center, colocation metro or office (nor a pod or zone of one), so no ManagedVRF can hold it. Set the "
        "device's deployment."
    ]


def unneeded_instance_warnings(facts: DeviceVrfFacts) -> list[str]:
    """(b) a membership in an instance of a namespace the device does not need (only a device that can carry one:
    (e) covers the rest). A decommissioned device is a member of nothing it needs: every membership is stale."""
    if _decommissioned(facts):
        return [
            f"VRF (b): decommissioned device '{facts.name}' is still a member of ManagedVRF '{instance.name}'. "
            "The next reconcile drops it, or remove the membership."
            for instance in facts.instances
        ]
    if not facts.carries_vrfs:
        return []
    return [
        f"VRF (b): device '{facts.name}' is a member of ManagedVRF '{instance.name}' but nothing on it routes in "
        f"namespace '{instance.namespace}'. The generator drops the membership on its next run, or remove it."
        for instance in facts.instances
        if instance.namespace not in facts.demand
    ]


def membership_errors(facts: DeviceVrfFacts) -> list[str]:
    """(c) a member of another deployment's instance, or of two instances of one namespace."""
    if not facts.carries_vrfs:
        return []
    errors = [
        f"VRF (c): device '{facts.name}' (hosting deployment '{facts.hosting_name}') is a member of ManagedVRF "
        f"'{instance.name}', which belongs to another deployment. Remove the membership (the next reconcile does)."
        for instance in facts.instances
        if instance.deployment_id != facts.hosting_id
    ]
    by_namespace: dict[str | None, list[InstanceFacts]] = defaultdict(list)
    for instance in facts.instances:
        by_namespace[instance.namespace].append(instance)
    errors.extend(
        f"VRF (c): device '{facts.name}' is a member of {len(found)} ManagedVRF instances of namespace "
        f"'{namespace}': {', '.join(sorted(str(instance.name) for instance in found))}. Keep one."
        for namespace, found in sorted(by_namespace.items(), key=lambda item: str(item[0]))
        if len(found) > 1
    )
    return errors


def empty_instance_warnings(instances: Iterable[InstanceFacts]) -> list[str]:
    """(f) an instance with no member."""
    return [
        f"VRF (f): ManagedVRF '{instance.name}' has no member device. Delete it, or re-run a generator that routes in "
        f"namespace '{instance.namespace}' (the reconcile sweep removes it)."
        for instance in instances
        if not instance.member_ids
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
    if facts.carries_vrfs or _decommissioned(facts):
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
    decommissioned = _decommissioned(facts)
    return [
        f"SVI (S2): svi interface '{svi.name}' on device '{facts.name}' "
        + (
            "is left on a decommissioned device"
            if decommissioned
            else "has no VLAN-domain row"
            if svi.row_id is None
            else "sits on a row the fabric does not route"
        )
        + ". The segment reconcile deletes it on its next run, or delete it."
        for svi in facts.svis
        if decommissioned or svi.row_id is None or svi.row_id in facts.unrouted_row_ids
    ]


def svi_vrf_errors(facts: DeviceVrfFacts) -> list[str]:
    """S3 ``svi.vrf`` is set (unless the row is ``default``) and is the instance of (the device's hosting deployment,
    the row's namespace), which the device is a member of."""
    rows = {row.id: row for row in facts.routed_rows}
    errors = []
    for svi in () if _decommissioned(facts) else facts.svis:
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
        elif svi.vrf_namespace != row.namespace:
            errors.append(
                f"SVI (S3): {where} is in VRF namespace '{svi.vrf_namespace}', its row routes in '{row.namespace}'. "
                "Re-run add_vxlan_segment for the segment."
            )
        elif svi.vrf_deployment_id != facts.hosting_id:
            errors.append(
                f"SVI (S3): {where} is in a ManagedVRF of another deployment than the device's hosting deployment "
                f"'{facts.hosting_name}'. Re-run add_vxlan_segment for the segment."
            )
        elif (member := _hosted(facts, row.namespace)) is None or member.id != svi.vrf_id:
            errors.append(
                f"SVI (S3): {where} points at a ManagedVRF the device is not a member of. "
                "Re-run add_vxlan_segment for the segment."
            )
    return errors


def svi_content_errors(facts: DeviceVrfFacts) -> list[str]:
    """S4 an SVI has no address and no capabilities (a decommissioned device's leftovers are S2's warning)."""
    if _decommissioned(facts):
        return []
    return [
        f"SVI (S4): svi interface '{svi.name}' on device '{facts.name}' has "
        + ("an address" if svi.has_address else "capabilities")
        + " (the anycast gateway is segment.gateway and a port carries the segment, not its SVI). Remove it."
        for svi in facts.svis
        if svi.has_address or svi.has_capabilities
    ]


def svi_domain_errors(facts: DeviceVrfFacts) -> list[str]:
    """S5 the SVI's row is one of the device's own VLAN-domain rows (a decommissioned device's leftovers: S2)."""
    if _decommissioned(facts):
        return []
    return [
        f"SVI (S5): svi interface '{svi.name}' on device '{facts.name}' is linked to a row outside the device's "
        "VLAN domain (its MLAG's when paired, else its own). Re-run add_vxlan_segment, which deletes it."
        for svi in facts.svis
        if svi.row_id is not None and svi.row_id not in facts.domain_row_ids
    ]


ERROR_RULES: tuple[_Rule, ...] = (
    missing_instance_errors,
    no_hosting_errors,
    membership_errors,
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
        cleaned = clean_data(data)
        for facts in (device_vrf_facts(device) for device in cleaned.get("DcimDevice") or []):
            for rule in ERROR_RULES:
                for message in rule(facts):
                    self.log_error(message=message)
            for rule in WARNING_RULES:
                for message in rule(facts):
                    self.log_warning(message=message)
        for message in empty_instance_warnings(all_instances(cleaned)):
            self.log_warning(message=message)
