"""Unit tests for CheckReferenceFabric, fed canned reference_fabric query responses."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from checks.reference_fabric import CheckReferenceFabric


def _v(value: Any) -> dict[str, Any]:
    """A raw attribute value."""
    return {"value": value}


def _check() -> Any:
    """A check with log_error/log_info captured instead of sent to Infrahub."""
    check = cast(Any, CheckReferenceFabric.__new__(CheckReferenceFabric))
    check._captured_errors = []
    check._captured_infos = []
    check.log_error = lambda message: check._captured_errors.append(message)
    check.log_info = lambda message: check._captured_infos.append(message)
    return check


def _profile(
    name: str, platform: str, kind: str, min_release: str, device_types: tuple[str, ...], min_memory_gb: int | None
) -> dict[str, Any]:
    """One raw GroupPolicyProfileFields edge."""
    return {
        "node": {
            "name": _v(name),
            "group_policy_kind": _v(kind),
            "min_release": _v(min_release),
            "min_memory_gb": _v(min_memory_gb),
            "platform": {"node": {"name": _v(platform)}},
            "device_types": {"edges": [{"node": {"name": _v(item)}} for item in device_types]},
        }
    }


NXOS_ROW = _profile(
    "reference-cisco-nxos", "cisco_nxos", "vxlan_gpo", "10.6(1)F", ("N9K-C93180YC-FX3", "N9K-C9364C-GX"), 24
)
EOS_ROW = _profile("reference-arista-eos", "arista_eos", "segment_security", "4.36.2F", ("DCS-7280SR3-48YC8",), None)
ROWS = [NXOS_ROW, EOS_ROW]


def _vtep(
    name: str,
    *,
    role: str = "leaf",
    platform: str = "cisco_nxos",
    device_type: str = "N9K-C93180YC-FX3",
    memory_gb: int | None = 32,
    version: str | None = "10.6(3)F",
) -> dict[str, Any]:
    """One DeviceGroupPolicyFields edge, in the NX-OS row by default."""
    return {
        "node": {
            "name": _v(name),
            "role": _v(role),
            "platform": {"node": {"name": _v(platform)}},
            "device_type": {"node": {"name": _v(device_type), "memory_gb": _v(memory_gb)}},
            "software_image": {"node": {"version": _v(version)}} if version else {"node": None},
        }
    }


REFERENCE = [_vtep("lf-1"), _vtep("lf-2")]
LEGACY = [_vtep("al-1", role="access-leaf", device_type="N9K-C93180YC-EX", memory_gb=24, version="10.3(3)")]


def _segment(
    seg_id: str, kind: str = "ManagedVxlanSegment", *, sites: tuple[str, ...] = ("dc10",), stretch: str = "local"
) -> dict[str, Any]:
    """One microsegmented segment edge with its deployments (site id = lower-case DC name)."""
    node: dict[str, Any] = {
        "__typename": kind,
        "id": seg_id,
        "name": _v(seg_id),
        "isolation_mode": _v("microsegmented"),
    }
    if kind == "ManagedVxlanSegment":
        node["stretch_scope"] = _v(stretch)
        node["segment_deployments"] = {
            "edges": [{"node": {"deployment": {"node": {"id": site, "name": _v(site.upper())}}}} for site in sites]
        }
    return {"node": node}


def _tags(seg_id: str, count: int) -> list[dict[str, Any]]:
    """``count`` SecurityTag edges whose component lives in ``seg_id``."""
    return [
        {
            "node": {
                "id": f"{seg_id}-t{n}",
                "components": {"edges": [{"node": {"id": f"c{n}", "network_segment": {"node": {"id": seg_id}}}}]},
            }
        }
        for n in range(count)
    ]


def _run(
    sites: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]],
    *,
    rows: list[dict[str, Any]] | None = None,
    segments: list[dict[str, Any]] | None = None,
    tags: list[dict[str, Any]] | None = None,
) -> Any:
    """Validate a response with ``sites``: DC id -> (its own devices, its pod's devices)."""
    check = _check()
    check.validate(
        {
            "DcimGroupPolicyProfile": {"edges": ROWS if rows is None else rows},
            "TopologyDataCenter": {
                "edges": [
                    {"node": {"id": site, "name": _v(site.upper()), "devices": {"edges": own}}}
                    for site, (own, _pod) in sites.items()
                ]
            },
            "TopologyPod": {
                "edges": [
                    {"node": {"parent": {"node": {"id": site}}, "devices": {"edges": pod}}}
                    for site, (_own, pod) in sites.items()
                ]
            },
            "ManagedNetworkSegment": {"edges": segments or []},
            "SecurityTag": {"edges": tags or []},
        }
    )
    return check


def _warnings(check: Any) -> list[str]:
    """The infos that are warnings."""
    return [info for info in check._captured_infos if info.startswith("WARNING:")]


class TestProfileRows:
    """A platform without exactly one usable row is an error, and its fabrics are not reference."""

    def test_no_row_is_an_error_and_no_fabric_is_reference(self) -> None:
        """Fail closed on an empty profile."""
        check = _run({"dc10": ([], REFERENCE)}, rows=[])
        assert check._captured_errors == [
            "Reference fabric profile: no DcimGroupPolicyProfile row: no fabric is a reference fabric"
        ]
        assert check._captured_infos == [
            "DC10 is not a reference fabric: leaf lf-1 N9K-C93180YC-FX3: no group-policy profile for platform "
            "cisco_nxos (L0 only) (and 1 more)"
        ]

    def test_duplicate_platform_rows_are_an_error_and_that_platform_is_not_reference(self) -> None:
        """Two NX-OS rows: NX-OS fabrics fail closed, EOS keeps its row."""
        twin = _profile("twin", "cisco_nxos", "vxlan_gpo", "10.6(1)F", ("N9K-C93180YC-FX3",), 24)
        check = _run({"dc10": ([], REFERENCE)}, rows=[NXOS_ROW, twin, EOS_ROW])
        assert check._captured_errors == [
            "Reference fabric profile: platform cisco_nxos is in 2 group-policy profiles "
            "(reference-cisco-nxos, twin), expected one"
        ]
        assert check._captured_infos[0].startswith("DC10 is not a reference fabric:")


class TestSites:
    """One info per data center, never an error."""

    def test_reference_site_is_one_info_counting_pod_vteps_only(self) -> None:
        """The DC's border leaf and the pod's leafs are the VTEPs; the pod's spine and server are not."""
        own = [_vtep("bl-1", role="border-leaf", device_type="N9K-C9364C-GX")]
        pod = [*REFERENCE, _vtep("sp-1", role="spine", device_type="N9K-C9336C-FX2"), _vtep("srv-1", role="server")]
        check = _run({"dc10": (own, pod)})
        assert check._captured_errors == []
        assert check._captured_infos == ["DC10 is a reference fabric (vxlan_gpo, 3 VTEPs)"]

    def test_eos_reference_site_names_its_kind(self) -> None:
        """Arista 7280R3 on 4.36.2F is a segment_security reference fabric."""
        eos = [
            _vtep(f"lf-{n}", platform="arista_eos", device_type="DCS-7280SR3-48YC8", memory_gb=None, version="4.36.2F")
            for n in (1, 2)
        ]
        check = _run({"dc20": ([], eos)})
        assert check._captured_errors == []
        assert check._captured_infos == ["DC20 is a reference fabric (segment_security, 2 VTEPs)"]

    def test_today_three_legacy_dcs_are_three_infos_by_name(self) -> None:
        """AC2 on 30_all: three "not reference" infos, no error, no warning without GBP segments."""
        fx2 = [_vtep("bl-1", role="border-leaf", device_type="N9K-C9336C-FX2", version="10.3(3)")]
        check = _run({"dc12": ([], LEGACY), "dc10": (fx2, LEGACY), "dc11": ([], [])})
        assert check._captured_errors == []
        assert check._captured_infos == [
            "DC10 is not a reference fabric: access-leaf al-1 N9K-C93180YC-EX: device type not in profile "
            "reference-cisco-nxos (and 1 more)",
            "DC11 is not a reference fabric: no VTEPs",
            "DC12 is not a reference fabric: access-leaf al-1 N9K-C93180YC-EX: device type not in profile "
            "reference-cisco-nxos",
        ]

    def test_platform_without_a_row_is_outside_the_profile(self) -> None:
        """A SONiC fabric keeps L0 only and says so."""
        check = _run({"dc20": ([], [_vtep("lf-1", platform="sonic", device_type="7726-32X-O")])})
        assert check._captured_errors == []
        assert check._captured_infos == [
            "DC20 is not a reference fabric: leaf lf-1 7726-32X-O: no group-policy profile for platform sonic (L0 only)"
        ]

    def test_eos_on_the_demo_image_is_not_reference(self) -> None:
        """The demos' EOS 4.32.1F on 7050X3 is below the floor and not a listed type: the first cause is named."""
        check = _run(
            {"dc20": ([], [_vtep("lf-1", platform="arista_eos", device_type="DCS-7050CX3-32C-R", version="4.32.1F")])}
        )
        assert check._captured_infos == [
            "DC20 is not a reference fabric: leaf lf-1 DCS-7050CX3-32C-R: device type not in profile reference-arista-eos"
        ]


class TestSegments:
    """Segments that need group policy fail closed off the reference fabric; stretched tagged ones warn."""

    def test_gbp_segment_on_a_reference_site_passes(self) -> None:
        """Two tagged components, single site, every VTEP in the profile."""
        check = _run({"dc10": ([], REFERENCE)}, segments=[_segment("s")], tags=_tags("s", 2))
        assert check._captured_errors == []
        assert check._captured_infos == ["DC10 is a reference fabric (vxlan_gpo, 2 VTEPs)"]

    def test_gbp_segment_on_a_non_reference_site_is_an_error(self) -> None:
        """AC2: its intra-segment flows have no enforcement point."""
        check = _run({"dc10": ([], [*REFERENCE, *LEGACY])}, segments=[_segment("s")], tags=_tags("s", 2))
        assert check._captured_errors == [
            "Segment 's' needs group policy, but DC10 is not a reference fabric (access-leaf al-1 N9K-C93180YC-EX: "
            "device type not in profile reference-cisco-nxos): its intra-segment flows have no enforcement point"
        ]

    def test_gbp_segment_on_a_site_that_is_no_data_center_is_an_error(self) -> None:
        """A deployment outside TopologyDataCenter is never a reference fabric."""
        check = _run({"dc10": ([], REFERENCE)}, segments=[_segment("s", sites=("colo1",))], tags=_tags("s", 2))
        assert len(check._captured_errors) == 1
        assert "COLO1 is not a reference fabric (not a data center)" in check._captured_errors[0]

    def test_multisite_gbp_segment_is_an_error(self) -> None:
        """AC2: a GBP-required VXLAN segment must be single-site."""
        segment = _segment("s", sites=("dc10", "dc12"), stretch="dc_pair")
        check = _run({"dc10": ([], REFERENCE), "dc12": ([], REFERENCE)}, segments=[segment], tags=_tags("s", 2))
        assert check._captured_errors == [
            "Segment 's' needs group policy but is active in more than one site; it must be single-site"
        ]

    def test_gbp_segment_active_nowhere_is_an_error(self) -> None:
        """No site, no enforcement point."""
        check = _run({"dc10": ([], REFERENCE)}, segments=[_segment("s", sites=())], tags=_tags("s", 2))
        assert check._captured_errors == ["Segment 's' needs group policy but is active in no site"]

    def test_c013_vlan_only_gbp_segment_is_one_warning(self) -> None:
        """AC2 on 30_all: the VLAN-only segment is host-enforced, a warning, not an error."""
        check = _run({"dc11": ([], LEGACY)}, segments=[_segment("c013", "ManagedVlanSegment")], tags=_tags("c013", 2))
        assert check._captured_errors == []
        assert len(_warnings(check)) == 1
        assert _warnings(check)[0].startswith("WARNING: segment 'c013' needs group policy")
        assert "host-enforced" in _warnings(check)[0]

    def test_tagged_segment_stretched_onto_a_non_reference_site_warns(self) -> None:
        """c005 DC10 <-> DC12 once DC10 is the reference fabric: enforced on one side only."""
        segment = _segment("c005-app-backend-stretch-p", sites=("dc10", "dc12"), stretch="dc_pair")
        check = _run(
            {"dc10": ([], REFERENCE), "dc12": ([], LEGACY)},
            segments=[segment],
            tags=_tags("c005-app-backend-stretch-p", 1),
        )
        assert check._captured_errors == []
        assert _warnings(check) == [
            "WARNING: segment 'c005-app-backend-stretch-p' is stretched between reference fabric DC10 and "
            "non-reference DC12: group policy is enforced on the reference side only"
        ]

    def test_tagged_segment_stretched_between_two_legacy_sites_is_silent(self) -> None:
        """30_all today: neither side enforces, so nothing is enforced on one side only."""
        segment = _segment("s", sites=("dc10", "dc12"), stretch="dc_pair")
        check = _run({"dc10": ([], LEGACY), "dc12": ([], LEGACY)}, segments=[segment], tags=_tags("s", 1))
        assert check._captured_errors == []
        assert _warnings(check) == []

    def test_untagged_segment_is_ignored(self) -> None:
        """A stretched segment without tagged components carries no group, so no warning."""
        segment = _segment("s", sites=("dc10", "dc12"), stretch="dc_pair")
        check = _run({"dc10": ([], REFERENCE), "dc12": ([], LEGACY)}, segments=[segment])
        assert _warnings(check) == []


def _tenant_segment(
    seg_id: str,
    kind: str = "ManagedVxlanSegment",
    *,
    sites: tuple[str, ...] = ("dc10",),
    tag: str | None = None,
    namespace_type: str = "prod",
) -> dict[str, Any]:
    """One routed tenant segment edge (gateway in a ``namespace_type`` namespace), tagged or not.

    A VXLAN segment is active in ``sites`` through its segment deployments; a
    VLAN segment in the data center of its footprint (the first site).
    """
    node: dict[str, Any] = {
        "__typename": kind,
        "id": seg_id,
        "name": _v(seg_id),
        "isolation_mode": _v("normal"),
        "security_zone": {"node": {"name": _v("PROD-ZONE")}},
        "gateway": {
            "node": {
                "ip_prefix": {
                    "node": {"ip_namespace": {"node": {"name": _v("PROD"), "namespace_type": _v(namespace_type)}}}
                }
            }
        },
        "security_tag": {"node": {"id": tag, "name": _v(tag), "group_id": _v(100)}} if tag else {"node": None},
    }
    if kind == "ManagedVxlanSegment":
        node["stretch_scope"] = _v("local" if len(sites) < 2 else "dc_pair")
        node["segment_deployments"] = {
            "edges": [{"node": {"deployment": {"node": {"id": site, "name": _v(site.upper())}}}} for site in sites]
        }
    else:
        node["customer_deployment"] = {
            "node": {"id": f"fp-{seg_id}", "parent": {"node": {"id": sites[0], "__typename": "TopologyDataCenter"}}}
        }
    return {"node": node}


class TestSegmentTags:
    """A routed tenant segment on a reference fabric carries its group tag; a stretched tagged one warns."""

    @pytest.mark.parametrize("kind", ["ManagedVxlanSegment", "ManagedVlanSegment"])
    def test_untagged_tenant_segment_on_a_reference_fabric_is_an_error(self, kind: str) -> None:
        """Its hosts would be unclassified, outside every contract (a VLAN segment sits in its footprint's DC)."""
        check = _run({"dc10": ([], REFERENCE)}, segments=[_tenant_segment("c008-portal-p", kind)])
        assert check._captured_errors == [
            "Segment 'c008-portal-p' is a routed tenant segment on reference fabric DC10 without a group tag: "
            "its hosts would be unclassified"
        ]

    def test_tagged_tenant_segment_on_a_reference_fabric_passes(self) -> None:
        check = _run({"dc10": ([], REFERENCE)}, segments=[_tenant_segment("c008-portal-p", tag="c008-portal-p")])
        assert check._captured_errors == []
        assert _warnings(check) == []

    def test_untagged_tenant_segment_off_the_reference_fabrics_is_silent(self) -> None:
        """30_all today: no reference fabric, so an untagged segment is nobody's problem yet."""
        check = _run({"dc10": ([], LEGACY)}, segments=[_tenant_segment("c008-portal-p")])
        assert check._captured_errors == []

    def test_untagged_management_segment_on_a_reference_fabric_is_never_required(self) -> None:
        """The INFRA services segment (MANAGEMENT VRF) is not a tenant segment: no tag, no error."""
        segment = _tenant_segment("infra-core-dns-ntp-p", "ManagedVlanSegment", namespace_type="management")
        check = _run({"dc10": ([], REFERENCE)}, segments=[segment])
        assert check._captured_errors == []

    def test_segment_tag_stretched_onto_a_non_reference_site_warns(self) -> None:
        """Its DC12 half arrives as the policy-unaware group."""
        segment = _tenant_segment("c005-message-queue-stretch-p", sites=("dc10", "dc12"), tag="t-mq")
        check = _run({"dc10": ([], REFERENCE), "dc12": ([], LEGACY)}, segments=[segment])
        assert check._captured_errors == []
        assert _warnings(check) == [
            "WARNING: segment 'c005-message-queue-stretch-p' is stretched between reference fabric DC10 and "
            "non-reference DC12: group policy is enforced on the reference side only"
        ]

    def test_query_reads_every_segment_with_its_tag_and_sites(self, root_dir: Path) -> None:
        """No isolation filter: every routed tenant segment, through the builder's one fragment."""
        text = (root_dir / "queries/validation/reference_fabric.gql").read_text()
        assert "ManagedNetworkSegment {" in text
        assert "...SegmentGroupPolicyFields" in text
        assert "isolation_mode__value" not in text


def test_query_selects_pod_parent_typename() -> None:
    """The live server cannot resolve a pod's hierarchical parent without `__typename` ('Unable to identify the type')."""
    from pathlib import Path

    text = Path(__file__).resolve().parents[2].joinpath("queries/validation/reference_fabric.gql").read_text()
    pod_block = text.split("TopologyPod", 1)[1]

    assert "parent { node { id __typename } }" in pod_block
