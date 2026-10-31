"""checks/bgp_closure.py and CheckBgpExpectations: the closure rules E1 to E5 on canned, already cleaned query data."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from checks import bgp_expectations
from checks.bgp_closure import Finding, analyse
from checks.bgp_expectations import CheckBgpExpectations

POOL = "10.250.0.0/24"


def _interface(name: str, role: str, address: str, namespace: str = "default") -> dict[str, Any]:
    return {"name": name, "role": role, "ip_address": {"address": address, "ip_namespace": {"name": namespace}}}


def _device(
    name: str,
    role: str,
    loopback: str,
    *,
    platform: str = "cisco_nxos",
    enforce: bool = False,
    pool: str | None = POOL,
    interfaces: tuple[dict[str, Any], ...] = (),
    namespaces: tuple[str, ...] = (),
) -> dict[str, Any]:
    """A cleaned DcimDevice node of the bgp_expectations query."""
    deployment: dict[str, Any] = {"routing_filter_enforce": enforce}
    if pool:
        deployment["loopback_pool"] = {"resources": [{"prefix": pool}]}
    return {
        "typename": "DcimPhysicalDevice",
        "name": name,
        "role": role,
        "platform": {"name": platform},
        "deployment": deployment,
        "shared_capabilities": [{"typename": "ManagedVRF", "namespace": {"name": ns}} for ns in namespaces],
        "interfaces": [_interface("loopback0", "loopback", loopback), *interfaces],
    }


def _peering(
    name: str, a: tuple[str, str], b: tuple[str, str], *, ttl: int = 1, role: str = "underlay"
) -> dict[str, Any]:
    """A cleaned ManagedBGPPeering between two (device, address) ends."""
    ends = [(a, 65001), (b, 65100)]
    return {
        "name": name,
        "peering_role": role,
        "session_type": "EBGP",
        "ttl": ttl,
        "capabilities": [
            {
                "name": "Ethernet1",
                "ip_address": {"address": address, "ip_namespace": {"name": "default"}},
                "device": {"typename": "DcimPhysicalDevice", "name": device},
            }
            for (device, address), _ in ends
        ],
        "bgp_processes": [
            {"process_role": "underlay", "local_as": {"asn": asn}, "capabilities": [{"name": device}]}
            for (device, _), asn in ends
        ],
    }


def _cleaned(devices: list[dict[str, Any]], peerings: list[dict[str, Any]], **roots: Any) -> dict[str, Any]:
    return {"DcimDevice": devices, "ManagedBGPPeering": peerings, **roots}


def _underlay_fabric(**leaf_overrides: Any) -> dict[str, Any]:
    leaf = _device("leaf1", "leaf", "10.250.0.11/32", **leaf_overrides)
    spine = _device("spine1", "spine", "10.250.0.1/32")
    return _cleaned([leaf, spine], [_peering("p1", ("leaf1", "10.1.0.1/31"), ("spine1", "10.1.0.0/31"))])


def _rules(findings: list[Finding]) -> list[str]:
    return sorted({f.rule for f in findings})


def test_a_consistent_underlay_has_no_finding_but_an_unprotected_evpn_free_session_is_clean() -> None:
    """Leaf and spine share the pool: what each announces the other accepts, every loopback is in the pool."""
    assert analyse(_underlay_fabric()) == []


def test_e1_a_leaf_on_another_pool_than_its_spine_is_reported_both_ways() -> None:
    """The spine relays 10.250.0.0/24 but the leaf accepts only its own pool: E1 names the announcer and the filterer."""
    findings = analyse(_underlay_fabric(pool="10.251.0.0/24"))
    e1 = [f for f in findings if f.rule == "E1"]
    assert {f.severity for f in e1} == {"warning"}
    assert any("spine1 announces 10.250.0.0/24 le 32, which leaf1 would filter out" in f.message for f in e1)


def test_e1_is_an_error_where_the_receiving_deployment_enforces() -> None:
    """An enforcing deployment would drop the routes for real: error, not warning."""
    findings = analyse(_underlay_fabric(pool="10.251.0.0/24", enforce=True))
    assert {f.severity for f in findings if f.rule == "E1"} == {"error"}


def test_e2_a_loopback_outside_the_pool_reaches_no_further_than_its_first_hop() -> None:
    """leaf1 loopback 10.99.0.1 is outside 10.250.0.0/24: the spine's underlay inbound does not accept it."""
    cleaned = _underlay_fabric()
    cleaned["DcimDevice"][0]["interfaces"][0]["ip_address"]["address"] = "10.99.0.1/32"
    findings = analyse(cleaned)
    assert any(f.rule == "E2" and "10.99.0.1/32" in f.message and "spine1" in f.message for f in findings)


def test_e3_a_connected_prefix_that_is_no_prefix_of_its_namespace_is_reported() -> None:
    """A VRF address whose network is not an IpamPrefix of the namespace: its type-5 route would be filtered."""
    overlay = _peering("o1", ("leaf1", "10.250.0.11/32"), ("spine1", "10.250.0.1/32"), ttl=255, role="overlay")
    leaf = _device(
        "leaf1",
        "leaf",
        "10.250.0.11/32",
        namespaces=("PROD",),
        interfaces=(_interface("Ethernet1.100", "customer", "10.77.0.1/30", "PROD"),),
    )
    spine = _device("spine1", "spine", "10.250.0.1/32")
    roots = {"environment_prefixes_prod": [{"prefix": "10.100.0.0/24", "ip_namespace": {"name": "PROD"}}]}
    findings = analyse(_cleaned([leaf, spine], [overlay], **roots))
    e3 = [f for f in findings if f.rule == "E3"]
    assert len(e3) == 1
    assert "10.77.0.0/30 is no prefix of PROD" in e3[0].message
    known = analyse(
        _cleaned(
            [leaf, spine],
            [overlay],
            environment_prefixes_prod=[{"prefix": "10.77.0.0/30", "ip_namespace": {"name": "PROD"}}],
        )
    )
    assert [f for f in known if f.rule == "E3"] == []


def test_e4_a_transit_session_on_a_device_that_is_no_default_exit() -> None:
    """The ISP session is in INTERNET but the device is no border gateway of an INTERNET instance."""
    transit = _peering("t1", ("edge1", "10.255.9.0/31"), ("isp1", "10.255.9.1/31"))
    for end in transit["capabilities"]:
        end["ip_address"]["ip_namespace"] = {"name": "INTERNET", "namespace_type": "internet"}
    edge = _device("edge1", "edge", "10.250.0.21/32")
    isp = _device("isp1", "edge", "10.250.0.99/32")
    findings = analyse(_cleaned([edge, isp], [transit]))
    assert any(f.rule == "E4" and "edge1" in f.message and "INTERNET" in f.message for f in findings)


def test_e5_a_session_the_query_data_cannot_derive_is_reported_unprotected() -> None:
    """No loopback pool in the deployment: the underlay families are not derivable, never a deny-all."""
    findings = analyse(_underlay_fabric(pool=None))
    e5 = [f for f in findings if f.rule == "E5"]
    assert any("leaf1" in f.message and "cannot be derived" in f.message for f in e5)
    assert _rules(findings) == ["E5"]


def test_e5_an_evpn_session_on_a_platform_without_an_evpn_filter() -> None:
    """EOS renders no EVPN filter: the overlay session is reported even though the model derives one."""
    overlay = _peering("o1", ("leaf1", "10.250.0.11/32"), ("spine1", "10.250.0.1/32"), ttl=255, role="overlay")
    leaf = _device("leaf1", "leaf", "10.250.0.11/32", platform="arista_eos", namespaces=("PROD",))
    spine = _device("spine1", "spine", "10.250.0.1/32", platform="arista_eos")
    roots = {"environment_prefixes_prod": [{"prefix": "10.100.0.0/24", "ip_namespace": {"name": "PROD"}}]}
    findings = analyse(_cleaned([leaf, spine], [overlay], **roots))
    assert any(f.rule == "E5" and "arista_eos renders no EVPN filter" in f.message for f in findings)


def test_a_peering_with_a_device_the_query_did_not_return_is_skipped() -> None:
    """A far end that is not in the device list (a cloud stub) yields no end, not an exception."""
    cleaned = _underlay_fabric()
    cleaned["DcimDevice"].pop()
    assert [f for f in analyse(cleaned) if f.rule == "E1"] == []


def test_the_check_logs_errors_as_errors_and_warnings_as_warning_info_lines(monkeypatch: Any) -> None:
    """An error finding blocks (log_error); a warning is an info line prefixed WARNING (WarningCheck)."""
    findings = [Finding("error", "E1", "a"), Finding("warning", "E5", "b")]
    monkeypatch.setattr(bgp_expectations, "analyse", lambda cleaned: findings)
    monkeypatch.setattr(bgp_expectations, "clean_data", lambda data: data)
    check = cast(Any, CheckBgpExpectations.__new__(CheckBgpExpectations))
    errors: list[str] = []
    infos: list[str] = []
    check.log_error = lambda message: errors.append(message)
    check.log_info = lambda message: infos.append(message)
    check.validate({})
    assert errors == ["E1: a"]
    assert infos == ["WARNING: E5: b", "2 finding(s) over the BGP sessions"]


def test_the_query_selects_what_the_closure_reads(root_dir: Path) -> None:
    """Peerings with both ends and processes, device facts, and the shared roots the transforms use."""
    query = (root_dir / "queries/validation/bgp_expectations.gql").read_text()
    for needed in (
        "ManagedBGPPeering",
        "bgp_processes",
        "...EvpnRtAsOnDeploymentFields",
        "shared_capabilities",
        'DcimVirtualInterface(role__value: "multisite-vip")',
        "environment_prefixes_prod",
        "environment_prefixes_internet",
        "ManagedVRF",
        "ManagedVxlanSegment",
        "platform { node { name { value } } }",
    ):
        assert needed in query
