"""VRF instances (ManagedVRF) match what each device routes in, and its segment SVIs match its VLAN rows.

Pure rules (a)-(i) and S1-S5 over ``DeviceVrfFacts``; see utils/vrf_demand.py and AGENTS.md "VRF instances".
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
    """The sources that make the device need an instance of ``namespace``."""
    inputs = facts.inputs
    sources = (
        ("a routed segment row of its VLAN domain", inputs.row_namespaces),
        ("an SVI", inputs.svi_namespaces),
        ("an address on one of its interfaces", inputs.address_namespaces),
        ("a firewall or load-balancer context leg on a service port", inputs.leg_namespaces),
    )
    return ", ".join(label for label, found in sources if namespace in found)


def _decommissioned(facts: DeviceVrfFacts) -> bool:
    """Whether the device is out of service, so only leftovers are reported."""
    return facts.inputs.status == DECOMMISSIONED_STATUS


def _hosted(facts: DeviceVrfFacts, namespace: str | None) -> InstanceFacts | None:
    """The device's membership in the instance of (hosting deployment, ``namespace``), or None."""
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
    """(b) a membership in an instance of a namespace the device does not need; all are stale when decommissioned."""
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
    """S1 each routed row of the device has a role-svi interface that is a member of it."""
    present = {row_id for svi in facts.svis for row_id in svi.row_ids}
    return [
        f"SVI (S1): device '{facts.name}' has no svi interface in its row of segment "
        f"'{row.segment_name}' (VLAN {row.vlan_id}, SVI '{name}'). Re-run add_vxlan_segment for the segment to "
        "converge it."
        for name, row in facts.desired_svis().items()
        if row.id not in present
    ]


def svi_orphan_warnings(facts: DeviceVrfFacts) -> list[str]:
    """S2 an SVI that is a member of no routed row of its device, or any SVI of a decommissioned device."""
    decommissioned = _decommissioned(facts)
    return [
        f"SVI (S2): svi interface '{svi.name}' on device '{facts.name}' "
        + (
            "is left on a decommissioned device"
            if decommissioned
            else "is a member of no routed VLAN row of its device"
        )
        + ". The VRF pass deletes it on its next run, or delete it."
        for svi in facts.svis
        if decommissioned or facts.svi_row(svi) is None
    ]


def interface_vrf_errors(facts: DeviceVrfFacts) -> list[str]:
    """(h) an interface is in at most one VRF instance."""
    return [
        f"VRF (h): interface '{name}' on device '{facts.name}' is in {len(vrfs)} VRF instances ({', '.join(vrfs)}); "
        "an interface routes in one. Remove it from all but one."
        for name, vrfs in sorted(facts.vrf_conflicts.items())
    ]


def missing_pbr_errors(facts: DeviceVrfFacts) -> list[str]:
    """(i) a device that steers has its ``ManagedPBR``."""
    if facts.pbr_ids or not facts.steers():
        return []
    return [
        f"PBR (i): device '{facts.name}' steers its tenant SVIs to a firewall but has no ManagedPBR, so its config "
        "renders no PBR. Re-run add_vxlan_segment for one of its segments, or add the capability."
    ]


def unneeded_pbr_warnings(facts: DeviceVrfFacts) -> list[str]:
    """(i) a ``ManagedPBR`` on a device that steers nothing."""
    if not facts.pbr_ids or facts.steers():
        return []
    return [f"PBR (i): device '{facts.name}' has a ManagedPBR but steers no SVI; the next reconcile deletes it."]


def svi_vrf_errors(facts: DeviceVrfFacts) -> list[str]:
    """S3 the SVI is in the instance of (hosting deployment, row namespace) that the device is a member of."""
    errors = []
    for svi in () if _decommissioned(facts) else facts.svis:
        row = facts.svi_row(svi)
        if row is None:
            continue
        where = f"svi interface '{svi.name}' on device '{facts.name}'"
        if svi.vrf_id is None:
            if row.namespace != DEFAULT_NAMESPACE:
                errors.append(
                    f"SVI (S3): {where} is in no VRF, its row routes in namespace '{row.namespace}'. "
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
    """S4 an SVI has no capability but its VRF and its row."""
    if _decommissioned(facts):
        return []
    return [
        f"SVI (S4): svi interface '{svi.name}' on device '{facts.name}' has a capability other than its VRF "
        "and its row (a port carries the segment, not its SVI). Remove it."
        for svi in facts.svis
        if svi.has_capabilities
    ]


def svi_address_warnings(facts: DeviceVrfFacts) -> list[str]:
    """S5 an SVI carries its segment's anycast gateway."""
    if _decommissioned(facts):
        return []
    return [
        f"SVI (S5): svi interface '{svi.name}' on device '{facts.name}' "
        + ("has no address" if svi.address_id is None else "has an address that is not")
        + f" the anycast gateway of segment '{row.segment_name}'. The VRF pass sets it on its next run "
        "(re-run add_vxlan_segment for the segment)."
        for svi in facts.svis
        if (row := facts.svi_row(svi)) is not None and svi.address_id != row.gateway_id
    ]


ERROR_RULES: tuple[_Rule, ...] = (
    missing_instance_errors,
    no_hosting_errors,
    membership_errors,
    namespace_errors,
    uncarried_instance_errors,
    interface_vrf_errors,
    missing_pbr_errors,
    svi_missing_errors,
    svi_vrf_errors,
    svi_content_errors,
)
WARNING_RULES: tuple[_Rule, ...] = (
    unneeded_instance_warnings,
    unneeded_pbr_warnings,
    svi_orphan_warnings,
    svi_address_warnings,
)


class CheckVrfInstances(WarningCheck):
    """Every device's ManagedVRF instances and segment SVIs match what it routes (rules above)."""

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
