# Combined Pydantic models from dc.py, pod.py, and rack.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, field_validator


def _unwrap_node(value: Any) -> Any:
    """Unwrap GraphQL ``{"node": X}`` wrapper, returning X (or None)."""
    if isinstance(value, dict) and "node" in value:
        return value.get("node")
    return value


def _templates_by_role(templates: list["DeviceRole"], role: str) -> list["DeviceRole"]:
    """Filter a fabric_templates list down to one role's positive-quantity entries."""
    return [t for t in templates if t.role == role and t.quantity > 0]


# Shared models
class Platform(BaseModel):
    id: str
    name: str | None = None


class DeviceType(BaseModel):
    id: str


class Owner(BaseModel):
    id: str


class Interface(BaseModel):
    name: str
    role: str | None = None
    interface_type: str | None = None
    description: str | None = None


class Template(BaseModel):
    id: str
    platform: Platform | None = None
    device_type: DeviceType | None = None
    owner: Owner | None = None
    interfaces: list[Interface] = []


class DeviceRole(BaseModel):
    role: str
    quantity: int
    template: Template | None = None
    device_type: DeviceType | None = None


class DeviceRack(BaseModel):
    """Rack information for device location."""

    id: str
    index: int
    row_index: int


class Device(BaseModel):
    name: str
    role: str | None = None
    rack: DeviceRack | None = None  # For leaf devices in mixed deployment
    interfaces: list[Interface] = []


class SpineCable(BaseModel):
    id: str
    name: str | None = None


class SpineInterface(BaseModel):
    id: str
    name: str
    cable: SpineCable | None = None

    @field_validator("cable", mode="before")
    @classmethod
    def unwrap_cable(cls, v: Any) -> Any:
        if isinstance(v, dict) and "node" in v:
            return v.get("node")
        return v


class SpineDevice(BaseModel):
    id: str
    name: str
    interfaces: list[SpineInterface] = []

    @property
    def cabled_port_names(self) -> set[str]:
        """Return set of interface names that already have a cable attached."""
        return {i.name for i in self.interfaces if i.cable}


class Pool(BaseModel):
    id: str
    name: str | None = None


# Pod layout capacity — same numbers TopologyPodDesign used to carry, now a
# plain dict keyed by TopologyPod.layout (a Dropdown) instead of a schema
# node/relationship. Physical row/rack layout is a fabric policy decision
# (like device density), not something derivable from LocationSuite —
# TopologyPod.suites (a separate, informational relationship) links a pod to
# its real physical suite(s) but isn't read by any of this sizing/offset logic.
POD_LAYOUTS: dict[str, dict[str, int]] = {
    "S_MIDDLE": {
        "rows": 2,
        "compute_racks_per_row": 8,
        "network_racks_per_row": 1,
        "max_leafs_per_network_rack": 4,
        "max_tors_per_network_rack": 4,
        "max_tors_per_compute_rack": 0,
        "max_spines_per_pod": 2,
        "max_border_leafs_per_pod": 1,
    },
    "S_TOR": {
        "rows": 2,
        # Hard-enforced by explicit spine/ToR port budget assumptions below.
        "compute_racks_per_row": 8,
        "network_racks_per_row": 0,
        "max_leafs_per_network_rack": 0,
        "max_tors_per_network_rack": 0,
        "max_tors_per_compute_rack": 2,
        "max_spines_per_pod": 4,
        "max_border_leafs_per_pod": 1,
        "spine_downlink_ports_per_spine": 32,
        "tor_uplinks_to_spine": 4,
        "reserved_spine_downlinks_per_spine": 0,
        "enforce_compute_racks_from_spine_budget": True,
    },
    "S_MIXED": {
        "rows": 2,
        # Hard-enforced by explicit spine/ToR port budget assumptions below.
        "compute_racks_per_row": 8,
        "network_racks_per_row": 1,
        "max_leafs_per_network_rack": 2,
        "max_tors_per_network_rack": 0,
        "max_tors_per_compute_rack": 2,
        "max_spines_per_pod": 4,
        "max_border_leafs_per_pod": 1,
        "spine_downlink_ports_per_spine": 32,
        "tor_uplinks_to_spine": 4,
        "reserved_spine_downlinks_per_spine": 0,
        "enforce_compute_racks_from_spine_budget": True,
    },
    "S_BORDER_SPINE_POD": {
        "rows": 1,
        "compute_racks_per_row": 0,
        "network_racks_per_row": 1,
        "max_leafs_per_network_rack": 8,
        "max_tors_per_network_rack": 0,
        "max_tors_per_compute_rack": 0,
        "max_spines_per_pod": 2,
        "max_border_leafs_per_pod": 0,
    },
    "M_MIXED": {
        "rows": 4,
        # Hard-enforced by explicit spine/ToR port budget assumptions below.
        "compute_racks_per_row": 8,
        "network_racks_per_row": 1,
        "max_leafs_per_network_rack": 2,
        "max_tors_per_network_rack": 0,
        "max_tors_per_compute_rack": 2,
        "max_spines_per_pod": 3,
        "max_border_leafs_per_pod": 1,
        "spine_downlink_ports_per_spine": 64,
        "tor_uplinks_to_spine": 3,
        "reserved_spine_downlinks_per_spine": 0,
        "enforce_compute_racks_from_spine_budget": True,
    },
    "M_MIDDLE": {
        "rows": 4,
        "compute_racks_per_row": 8,
        "network_racks_per_row": 1,
        "max_leafs_per_network_rack": 4,
        "max_tors_per_network_rack": 4,
        "max_tors_per_compute_rack": 0,
        "max_spines_per_pod": 3,
        "max_border_leafs_per_pod": 1,
    },
    "L_MIXED": {
        "rows": 8,
        # Hard-enforced by explicit spine/ToR port budget assumptions below.
        "compute_racks_per_row": 8,
        "network_racks_per_row": 1,
        "max_leafs_per_network_rack": 2,
        "max_tors_per_network_rack": 0,
        "max_tors_per_compute_rack": 2,
        "max_spines_per_pod": 4,
        "max_border_leafs_per_pod": 1,
        "spine_downlink_ports_per_spine": 64,
        "tor_uplinks_to_spine": 2,
        "reserved_spine_downlinks_per_spine": 0,
        "enforce_compute_racks_from_spine_budget": True,
    },
    "L_MIDDLE": {
        "rows": 8,
        "compute_racks_per_row": 8,
        "network_racks_per_row": 1,
        "max_leafs_per_network_rack": 4,
        "max_tors_per_network_rack": 4,
        "max_tors_per_compute_rack": 0,
        "max_spines_per_pod": 4,
        "max_border_leafs_per_pod": 1,
    },
}


class PodLayout(BaseModel):
    """Resolved POD_LAYOUTS entry for one TopologyPod.layout value."""

    name: str
    rows: int
    compute_racks_per_row: int
    network_racks_per_row: int
    max_leafs_per_network_rack: int = 4
    max_tors_per_network_rack: int = 2
    max_tors_per_compute_rack: int = 1
    max_spines_per_pod: int = 2
    max_border_leafs_per_pod: int = 1
    spine_downlink_ports_per_spine: int | None = None
    tor_uplinks_to_spine: int | None = None
    reserved_spine_downlinks_per_spine: int = 0
    enforce_compute_racks_from_spine_budget: bool = False

    @property
    def computed_max_compute_racks_per_row(self) -> int | None:
        """Compute per-row max from explicit spine/ToR port budget assumptions.

        Returns None when budget assumptions are not provided for this layout.
        """
        if self.spine_downlink_ports_per_spine is None or self.tor_uplinks_to_spine is None:
            return None

        usable_downlinks_per_spine = max(
            0, self.spine_downlink_ports_per_spine - self.reserved_spine_downlinks_per_spine
        )
        total_usable_downlinks = usable_downlinks_per_spine * self.max_spines_per_pod
        links_per_compute_rack = self.max_tors_per_compute_rack * self.tor_uplinks_to_spine
        if links_per_compute_rack <= 0 or self.rows <= 0:
            return 0

        max_compute_racks_per_pod = total_usable_downlinks // links_per_compute_rack
        return max_compute_racks_per_pod // self.rows

    def validate_budget_consistency(self) -> None:
        """Fail fast when hard-enforced layout budget and declared maxima diverge."""
        if not self.enforce_compute_racks_from_spine_budget:
            return
        computed = self.computed_max_compute_racks_per_row
        if computed is None:
            raise ValueError(
                f"Layout {self.name}: enforce_compute_racks_from_spine_budget=True but spine budget fields are missing"
            )
        if self.compute_racks_per_row != computed:
            raise ValueError(
                f"Layout {self.name}: compute_racks_per_row={self.compute_racks_per_row} "
                f"does not match spine budget derived value={computed}"
            )

    @classmethod
    def from_name(cls, layout: str) -> "PodLayout":
        resolved = cls(name=layout, **POD_LAYOUTS[layout])
        resolved.validate_budget_consistency()
        return resolved


# Data Center size capacity — same numbers TopologyDataCenterDesign used to
# carry, now a plain dict keyed by TopologyDataCenter.size (a Dropdown)
# instead of a schema node/relationship.
DC_SIZE_LAYOUTS: dict[str, dict[str, int]] = {
    "S": {
        "max_pods": 4,
        "max_super_spines_per_fabric": 0,
        "max_hyper_spines_per_fabric": 0,
        "max_spines_per_pod": 2,
        "max_border_leafs_per_fabric": 0,
        "loopback_prefix_length": 120,
        "technical_prefix_length": 117,
        "management_prefix_length": 26,
    },
    "M": {
        "max_pods": 4,
        "max_super_spines_per_fabric": 0,
        "max_hyper_spines_per_fabric": 0,
        "max_spines_per_pod": 4,
        "max_border_leafs_per_fabric": 2,
        "loopback_prefix_length": 119,
        "technical_prefix_length": 115,
        "management_prefix_length": 25,
    },
    "L": {
        "max_pods": 8,
        "max_super_spines_per_fabric": 4,
        "max_hyper_spines_per_fabric": 0,
        "max_spines_per_pod": 4,
        "max_border_leafs_per_fabric": 2,
        "loopback_prefix_length": 118,
        "technical_prefix_length": 114,
        "management_prefix_length": 24,
    },
    "XL": {
        "max_pods": 16,
        "max_super_spines_per_fabric": 4,
        "max_hyper_spines_per_fabric": 2,
        "max_spines_per_pod": 4,
        "max_border_leafs_per_fabric": 4,
        "loopback_prefix_length": 117,
        "technical_prefix_length": 113,
        "management_prefix_length": 23,
    },
}


class DCSizeLayout(BaseModel):
    """Resolved DC_SIZE_LAYOUTS entry for one TopologyDataCenter.size value."""

    name: str
    max_pods: int = 2
    max_super_spines_per_fabric: int = 2
    max_hyper_spines_per_fabric: int = 0
    max_spines_per_pod: int = 4
    max_border_leafs_per_fabric: int = 4
    loopback_prefix_length: int = 23
    technical_prefix_length: int = 19
    management_prefix_length: int = 25

    @classmethod
    def from_name(cls, size: str) -> "DCSizeLayout":
        return cls(name=size, **DC_SIZE_LAYOUTS[size])


@dataclass(frozen=True)
class StandardProfileAssumptions:
    """Shared assumptions used by standard profile templates.

    These assumptions explain WHY the default profile numbers exist and provide
    one declarative place for future tuning.
    """

    oversubscription: str = "1:2"
    homing: Literal["single", "dual"] = "dual"
    spare_ratio: float = 0.20
    preferred_topology: Literal["middle_rack", "tor", "mixed"] = "mixed"
    suite_container_mode: bool = True


STANDARD_PROFILE_ASSUMPTIONS = StandardProfileAssumptions()


class DCProfile(BaseModel):
    """Resolved per-DC profile derived from a standard size template."""

    owner_kind: Literal["dc"] = "dc"
    owner_id: str
    owner_name: str
    profile_name: str
    profile_template: str
    oversubscription: str
    homing: Literal["single", "dual"]
    spare_ratio: float
    preferred_topology: Literal["middle_rack", "tor", "mixed"]
    max_pods: int
    max_super_spines_per_fabric: int
    max_hyper_spines_per_fabric: int
    max_spines_per_pod: int
    max_border_leafs_per_fabric: int
    loopback_prefix_length: int
    technical_prefix_length: int
    management_prefix_length: int

    @classmethod
    def from_dc(cls, *, dc_id: str, dc_name: str, size: str) -> "DCProfile":
        layout = DCSizeLayout.from_name(size)
        assumptions = STANDARD_PROFILE_ASSUMPTIONS
        return cls(
            owner_id=dc_id,
            owner_name=dc_name,
            profile_name=f"{dc_name}:{size}",
            profile_template=size,
            oversubscription=assumptions.oversubscription,
            homing=assumptions.homing,
            spare_ratio=assumptions.spare_ratio,
            preferred_topology=assumptions.preferred_topology,
            max_pods=layout.max_pods,
            max_super_spines_per_fabric=layout.max_super_spines_per_fabric,
            max_hyper_spines_per_fabric=layout.max_hyper_spines_per_fabric,
            max_spines_per_pod=layout.max_spines_per_pod,
            max_border_leafs_per_fabric=layout.max_border_leafs_per_fabric,
            loopback_prefix_length=layout.loopback_prefix_length,
            technical_prefix_length=layout.technical_prefix_length,
            management_prefix_length=layout.management_prefix_length,
        )


class PodProfile(BaseModel):
    """Resolved per-Pod profile derived from a standard layout template."""

    owner_kind: Literal["pod"] = "pod"
    owner_id: str
    owner_name: str
    owner_dc_id: str | None = None
    owner_dc_name: str | None = None
    profile_name: str
    profile_template: str
    deployment_type: Literal["middle_rack", "tor", "mixed"]
    oversubscription: str
    homing: Literal["single", "dual"]
    spare_ratio: float
    preferred_topology: Literal["middle_rack", "tor", "mixed"]
    suite_container_mode: bool
    rows: int
    compute_racks_per_row: int
    network_racks_per_row: int
    max_leafs_per_network_rack: int
    max_tors_per_network_rack: int
    max_tors_per_compute_rack: int
    max_spines_per_pod: int
    max_border_leafs_per_pod: int
    spine_downlink_ports_per_spine: int | None = None
    tor_uplinks_to_spine: int | None = None
    reserved_spine_downlinks_per_spine: int = 0
    enforce_compute_racks_from_spine_budget: bool = False

    @property
    def row_dependent_rack_slots_per_row(self) -> int:
        """Row-dependent rack slots used for deterministic ToR offset planning."""
        if self.deployment_type in ("tor", "mixed"):
            return self.compute_racks_per_row
        return 0

    @classmethod
    def from_pod(
        cls,
        *,
        pod_id: str,
        pod_name: str,
        layout_name: str,
        deployment_type: Literal["middle_rack", "tor", "mixed"],
        dc_id: str | None = None,
        dc_name: str | None = None,
    ) -> "PodProfile":
        layout = PodLayout.from_name(layout_name)
        assumptions = STANDARD_PROFILE_ASSUMPTIONS
        return cls(
            owner_id=pod_id,
            owner_name=pod_name,
            owner_dc_id=dc_id,
            owner_dc_name=dc_name,
            profile_name=f"{pod_name}:{layout_name}",
            profile_template=layout_name,
            deployment_type=deployment_type,
            oversubscription=assumptions.oversubscription,
            homing=assumptions.homing,
            spare_ratio=assumptions.spare_ratio,
            preferred_topology=assumptions.preferred_topology,
            suite_container_mode=assumptions.suite_container_mode,
            rows=layout.rows,
            compute_racks_per_row=layout.compute_racks_per_row,
            network_racks_per_row=layout.network_racks_per_row,
            max_leafs_per_network_rack=layout.max_leafs_per_network_rack,
            max_tors_per_network_rack=layout.max_tors_per_network_rack,
            max_tors_per_compute_rack=layout.max_tors_per_compute_rack,
            max_spines_per_pod=layout.max_spines_per_pod,
            max_border_leafs_per_pod=layout.max_border_leafs_per_pod,
            spine_downlink_ports_per_spine=layout.spine_downlink_ports_per_spine,
            tor_uplinks_to_spine=layout.tor_uplinks_to_spine,
            reserved_spine_downlinks_per_spine=layout.reserved_spine_downlinks_per_spine,
            enforce_compute_racks_from_spine_budget=layout.enforce_compute_racks_from_spine_budget,
        )


# Data Center Design model (fabric-wide architectural principles)
class RoutingArchitectureMixin(BaseModel):
    """routing_strategy/underlay_protocol and their derived properties.

    Lives directly on the DC instance (and its Pod/Rack parent projections)
    rather than on TopologyDataCenterDesign — the Design node is a pure
    size/capacity template (S/M/L/XL), routing style is a free per-DC choice
    independent of size. See DCModel/PodParent/RackParent.
    """

    routing_strategy: str = "ebgp-ebgp"
    underlay_protocol: str = "ipv6"

    @property
    def is_ipv6(self) -> bool:
        return self.underlay_protocol == "ipv6"

    @property
    def is_dual_stack(self) -> bool:
        return self.underlay_protocol == "dual_stack"

    @property
    def p2p_ipv6(self) -> bool:
        """Whether P2P fabric links use IPv6 addressing."""
        return self.underlay_protocol in ("ipv6", "dual_stack")

    @property
    def p2p_addressing(self) -> str:
        """P2P link prefix: /31 for IPv4, /127 for IPv6/dual-stack."""
        return "/127" if self.p2p_ipv6 else "/31"


# DC model
class DCPod(BaseModel):
    id: str


class DCModel(RoutingArchitectureMixin):
    id: str
    name: str
    index: int
    size: str
    naming_convention: str = "standard"
    fabric_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    spine_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    connectivity_mode: Literal["pbr", "inline"] = "pbr"
    management_mode: Literal["fully_managed", "managed_by_controller"] = "fully_managed"
    fabric_templates: list[DeviceRole] = []
    loopback_pool: Pool | None = None
    technical_pool: Pool | None = None
    management_pool: Pool | None = None
    fabric_asn_pool: Pool | None = None
    children: list[DCPod] = []

    @property
    def profile(self) -> DCProfile:
        return DCProfile.from_dc(dc_id=self.id, dc_name=self.name, size=self.size)

    @property
    def design(self) -> DCSizeLayout:
        return DCSizeLayout.from_name(self.size)

    @property
    def is_managed_by_controller(self) -> bool:
        return self.management_mode == "managed_by_controller"

    @field_validator(
        "loopback_pool",
        "technical_pool",
        "management_pool",
        "fabric_asn_pool",
        mode="before",
    )
    @classmethod
    def extract_node(cls, value: Any) -> Any | None:
        unwrapped = _unwrap_node(value)
        if isinstance(unwrapped, dict) and unwrapped.get("id") is None:
            return None
        return unwrapped

    @property
    def super_spine_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "super-spine")

    @property
    def hyper_spine_templates(self) -> list[DeviceRole]:
        """DC-level hyper-spine tier — only present in XL fabrics with
        design.max_hyper_spines_per_fabric > 0 (see role: hyper-spine in
        schemas/extensions/topology/topology_dc.yml). Sits above super-spine,
        full-mesh cabled/routed by dc.py itself (unlike super-spine, which is
        cabled by pod.py against its own pod-scoped spines)."""
        return _templates_by_role(self.fabric_templates, "hyper-spine")

    @property
    def border_leaf_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "border-leaf")

    @property
    def firewall_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "firewall")

    @property
    def load_balancer_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "load-balancer")


# Pod model
class PodParent(RoutingArchitectureMixin):
    id: str
    devices: list[Device]
    name: str
    index: int
    # DC may have zero super-spine fabric_templates entries, never a null list
    fabric_templates: list[DeviceRole] = []
    size: str
    naming_convention: str = "standard"
    fabric_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    spine_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    connectivity_mode: Literal["pbr", "inline"] = "pbr"
    management_mode: Literal["fully_managed", "managed_by_controller"] = "fully_managed"
    fabric_asn_pool: Pool | None = None
    management_pool: Pool | None = None

    @property
    def design(self) -> DCSizeLayout:
        return DCSizeLayout.from_name(self.size)

    @property
    def is_managed_by_controller(self) -> bool:
        return self.management_mode == "managed_by_controller"

    @field_validator("management_pool", "fabric_asn_pool", mode="before")
    @classmethod
    def extract_parent_node(cls, value: Any) -> Any | None:
        unwrapped = _unwrap_node(value)
        if isinstance(unwrapped, dict) and unwrapped.get("id") is None:
            return None
        return unwrapped

    @property
    def super_spine_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "super-spine")

    @property
    def border_leaf_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "border-leaf")

    @property
    def firewall_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "firewall")

    @property
    def load_balancer_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "load-balancer")


class PodModel(BaseModel):
    """Pod instance model with capacity calculated from its layout.

    Pod makes DEPLOYMENT DECISIONS within constraints:
    - amount_of_spines: Actual spine count (default=4, constrained by design.max_spines_per_pod)
    - deployment_type: explicit Dropdown (middle_rack/tor/mixed) instead of derived

    Physical row/rack-count and device-density are CALCULATED from layout
    (see POD_LAYOUTS):
    - max_leafs_per_row: layout.network_racks_per_row × max_leafs_per_network_rack
    - max_tors_per_row: layout.compute_racks_per_row × max_tors_per_compute_rack
    """

    id: str
    name: str
    index: int
    deployment_type: Literal["middle_rack", "tor", "mixed"]
    layout: str

    leaf_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"

    @property
    def profile(self) -> PodProfile:
        return PodProfile.from_pod(
            pod_id=self.id,
            pod_name=self.name,
            layout_name=self.layout,
            deployment_type=self.deployment_type,
            dc_id=self.parent.id,
            dc_name=self.parent.name,
        )

    @property
    def design(self) -> PodLayout:
        return PodLayout.from_name(self.layout)

    @property
    def max_leafs_per_row(self) -> int:
        """Calculate maximum leafs per row from layout physical capacity."""
        return self.design.network_racks_per_row * self.design.max_leafs_per_network_rack

    @property
    def max_tors_per_row(self) -> int:
        """Calculate maximum ToRs per row from layout physical capacity."""
        return self.design.compute_racks_per_row * self.design.max_tors_per_compute_rack

    spine_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    rack_numbering_start_index: int = 1
    leaf_link_numbering_start: int = 1
    spine_link_numbering_start: int = 1
    tor_link_numbering_start: int = 1
    # pod.py validates non-empty at generate() time; the list itself is never null
    fabric_templates: list[DeviceRole] = []
    parent: PodParent
    loopback_pool: Pool | None = None
    prefix_pool: Pool | None = None
    asn_pool: Pool | None = None

    @field_validator("loopback_pool", "prefix_pool", "asn_pool", mode="before")
    @classmethod
    def handle_empty_node(cls, v: Any) -> Any:
        return _unwrap_node(v)

    @property
    def spine_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "spine")

    @property
    def border_spine_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "border-spine")

    @property
    def firewall_templates(self) -> list[DeviceRole]:
        """This pod's own firewall(s) — only used in border-spine micro-fabric
        mode (see border_spine_templates); a pod with a plain spine tier has
        no firewall of its own, that lives DC-wide on dc.py's border-leaf."""
        return _templates_by_role(self.fabric_templates, "firewall")

    @property
    def load_balancer_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "load-balancer")

    @property
    def spine_slot_templates(self) -> list[DeviceRole]:
        """Whichever of spine/border-spine fills this pod's spine slot — the
        two are mutually exclusive per pod (see role: border-spine in
        schemas/extensions/topology/topology_dc.yml)."""
        return self.spine_templates or self.border_spine_templates

    @property
    def spine_slot_role(self) -> Literal["spine", "border-spine"]:
        return "border-spine" if self.border_spine_templates else "spine"

    @property
    def rack_numbering_base_offset(self) -> int:
        return max(0, self.rack_numbering_start_index - 1)

    @property
    def leaf_link_base_offset(self) -> int:
        return max(0, self.leaf_link_numbering_start - 1)

    @property
    def spine_link_base_offset(self) -> int:
        return max(0, self.spine_link_numbering_start - 1)

    @property
    def tor_link_base_offset(self) -> int:
        return max(0, self.tor_link_numbering_start - 1)


# Rack model
class RackParent(RoutingArchitectureMixin):
    id: str
    name: str
    index: int
    size: str
    naming_convention: str = "standard"
    fabric_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    management_pool: Pool | None = None
    connectivity_mode: Literal["pbr", "inline"] = "pbr"
    management_mode: Literal["fully_managed", "managed_by_controller"] = "fully_managed"

    @property
    def design(self) -> DCSizeLayout:
        return DCSizeLayout.from_name(self.size)

    @property
    def is_managed_by_controller(self) -> bool:
        return self.management_mode == "managed_by_controller"

    @field_validator("management_pool", mode="before")
    @classmethod
    def extract_rack_parent_node(cls, value: Any) -> Any | None:
        unwrapped = _unwrap_node(value)
        if isinstance(unwrapped, dict) and unwrapped.get("id") is None:
            return None
        return unwrapped


class RackPod(BaseModel):
    id: str
    name: str
    index: int
    parent: RackParent
    leaf_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    spine_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    mlag_create: Literal["no", "back-to-back", "virtual"] = "no"
    rack_numbering_start_index: int = 1
    leaf_link_numbering_start: int = 1
    spine_link_numbering_start: int = 1
    tor_link_numbering_start: int = 1
    loopback_pool: Pool | None = None
    prefix_pool: Pool | None = None
    asn_pool: Pool | None = None
    deployment_type: Literal["middle_rack", "tor", "mixed"]
    layout: str
    # pod.py validates non-empty at generate() time; the list itself is never null
    fabric_templates: list[DeviceRole] = []
    # Spine devices with cable info (from GQL query)
    devices: list[SpineDevice] = []

    @property
    def profile(self) -> PodProfile:
        return PodProfile.from_pod(
            pod_id=self.id,
            pod_name=self.name,
            layout_name=self.layout,
            deployment_type=self.deployment_type,
            dc_id=self.parent.id,
            dc_name=self.parent.name,
        )

    @property
    def design(self) -> PodLayout:
        return PodLayout.from_name(self.layout)

    @property
    def spine_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "spine")

    @property
    def border_spine_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "border-spine")

    @property
    def spine_slot_templates(self) -> list[DeviceRole]:
        """Whichever of spine/border-spine fills this pod's spine slot — see
        PodModel.spine_slot_templates for why the two are mutually exclusive."""
        return self.spine_templates or self.border_spine_templates

    @property
    def spine_slot_role(self) -> Literal["spine", "border-spine"]:
        return "border-spine" if self.border_spine_templates else "spine"

    @property
    def rack_numbering_base_offset(self) -> int:
        return max(0, self.rack_numbering_start_index - 1)

    @property
    def leaf_link_base_offset(self) -> int:
        return max(0, self.leaf_link_numbering_start - 1)

    @property
    def spine_link_base_offset(self) -> int:
        return max(0, self.spine_link_numbering_start - 1)

    @property
    def tor_link_base_offset(self) -> int:
        return max(0, self.tor_link_numbering_start - 1)

    @field_validator("loopback_pool", "prefix_pool", "asn_pool", mode="before")
    @classmethod
    def handle_empty_node(cls, v: Any) -> Any:
        return _unwrap_node(v)


# Spine and leaf devices queried separately when needed (on-demand for specific deployment types)


class LocationSuiteModel(BaseModel):
    """LocationSuite model for rack parent hierarchy."""

    index: int  # Required for device naming
    id: str | None = None
    name: str | None = None
    shortname: str | None = None
    suite_name: str | None = None


class RackModel(BaseModel):
    id: str
    name: str
    index: int
    rack_type: str
    row_index: int
    parent: LocationSuiteModel
    leafs: list[DeviceRole] = []
    tors: list[DeviceRole] = []
    l2_leafs: list[DeviceRole] = []
    access_leafs: list[DeviceRole] = []
    pod: RackPod


# Endpoint connectivity model
class Cable(BaseModel):
    """Cable reference (just ID — detailed data queried dynamically)."""

    id: str


class EndpointInterface(BaseModel):
    """Interface on endpoint device."""

    id: str
    name: str
    interface_type: str | None = None
    role: str | None = None
    status: str | None = None
    cable: Cable | None = None

    @field_validator("cable", mode="before")
    @classmethod
    def handle_null_cable(cls, v: Any) -> Any:
        return _unwrap_node(v)


class RackDevice(BaseModel):
    """Device in rack (ToR or Leaf) with interfaces."""

    id: str
    name: str
    role: str | None = None
    rack_row_index: int | None = None
    interfaces: list[EndpointInterface] = []


class EndpointDataCenter(BaseModel):
    """Data center information for deployment context."""

    id: str
    name: str


class EndpointPod(BaseModel):
    """Pod information for deployment context."""

    id: str
    name: str
    deployment_type: Literal["middle_rack", "tor", "mixed"]
    index: int
    parent: EndpointDataCenter


class EndpointRack(BaseModel):
    """Rack containing endpoint device."""

    id: str
    name: str
    index: int
    row_index: int
    rack_type: str
    pod: EndpointPod
    devices: list[RackDevice] = []  # Leafs and ToRs in same rack


class EndpointDevice(BaseModel):
    """Endpoint device (server) to be connected."""

    id: str
    name: str
    role: str | None = None
    rack: EndpointRack | None = None
    interfaces: list[EndpointInterface] = []


class EndpointModel(BaseModel):
    """Complete endpoint connectivity model."""

    endpoint: EndpointDevice


# Endpoint connectivity models
@dataclass(frozen=True)
class ConnectionFingerprint:
    """Unique identifier for a server-to-switch connection.

    Provides idempotency by uniquely identifying each connection regardless
    of execution order or multiple generator runs.
    """

    server_name: str
    server_interface: str
    switch_name: str
    switch_interface: str

    def __hash__(self) -> int:
        return hash((self.server_name, self.server_interface, self.switch_name, self.switch_interface))

    def __repr__(self) -> str:
        return f"{self.server_name}:{self.server_interface} → {self.switch_name}:{self.switch_interface}"
