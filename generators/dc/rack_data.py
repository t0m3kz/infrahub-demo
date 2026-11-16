"""The shapes of a rack's query payload (``rack.gql``) after ``clean_data``: no runtime validation, a missing key raises
KeyError at its first read."""

from __future__ import annotations

from typing import Any, Literal

from typing_extensions import TypedDict


class TopologyRackParentData(TypedDict, total=False):
    """Nested TopologyDataCenter projection under TopologyPod.parent (rack.gql RackPodFields)."""

    id: str
    name: str
    index: int
    naming_convention: str
    fabric_interface_sorting_method: str
    connectivity_mode: str
    routing_strategy: str
    underlay_protocol: str
    management_pool: dict[str, Any] | None
    fabric_asn_pool: dict[str, Any] | None
    fabric_controllers: list[dict[str, Any]]
    security_manager_controllers: list[dict[str, Any]]
    lb_manager_controllers: list[dict[str, Any]]


class TopologyRackPodRackData(TypedDict):
    """One rack of the pod (``racks`` of the RackPodFields fragment in rack.gql)."""

    id: str
    name: str
    index: int
    row_index: int
    rack_type: str


class TopologyRackPodData(TypedDict, total=False):
    """Nested TopologyPod projection under LocationRack.deployment (rack.gql RackPodFields)."""

    id: str
    name: str
    index: int
    design: dict[str, Any]
    parent: TopologyRackParentData
    fabric_templates: list[dict[str, Any]]
    leaf_interface_sorting_method: str
    spine_interface_sorting_method: str
    mlag_create: Literal["no", "back-to-back", "virtual"]
    loopback_pool: dict[str, Any] | None
    prefix_pool: dict[str, Any] | None
    racks: list[TopologyRackPodRackData]


class TopologyRackParentSuiteData(TypedDict, total=False):
    """Nested LocationSuite projection under LocationRack.parent."""

    id: str | None
    index: int
    name: str | None
    shortname: str | None


class TopologyRackData(TypedDict, total=False):
    """One clean_data()-processed LocationRack entry; a missing key raises at first read."""

    id: str
    name: str
    index: int
    rack_type: str
    row_index: int
    parent: TopologyRackParentSuiteData
    leafs: list[dict[str, Any]]
    tors: list[dict[str, Any]]
    l2_leafs: list[dict[str, Any]]
    access_leafs: list[dict[str, Any]]
    deployment: TopologyRackPodData
