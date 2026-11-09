"""The DC, PAR cage and AWS scenarios terminate at located C900 workloads."""

from pathlib import Path
from typing import Any

import yaml

from tests.traversal.check_model import reach_jobs

_ROOT = Path(__file__).parents[2]
_DATA = _ROOT / "data/demos/00_full_topology"
_TENANT = _DATA / "20_C900/COLO-PAR/04_tenant"


def _items(filename: str, kind: str) -> list[dict[str, Any]]:
    if filename == "01_workload.yml":
        document = {"DcimPhysicalDevice": "00", "DcimPhysicalInterface": "01"}[kind]
        filename = f"01_workload_{document}_00_{kind}.yml"
    return [
        item
        for doc in yaml.safe_load_all((_TENANT / filename).read_text())
        if doc["spec"]["kind"] == kind
        for item in doc["spec"]["data"]
    ]


def test_colo_workload_and_footprint_have_the_same_owner_and_correct_places() -> None:
    """The footprint belongs to PAR metro, while the owned physical server belongs to its zone."""
    footprint = _items("00_customer_colocation.yml", "TopologyCustomerColocation")[0]
    server = _items("01_workload.yml", "DcimPhysicalDevice")[0]
    assert footprint == {
        "owner": "C900",
        "environment": "p",
        "infrastructure_orchestrator": "ansible_automation_platform",
        "parent": "PAR",
    }
    assert server["owner"] == footprint["owner"]
    assert server["deployment"] == "PAR-COLO1"
    assert server["rack"] == "par-colo1-rack-01"
    assert server["primary_address"]["data"]["address"] == "10.90.4.11/24"


def test_workload_gateway_is_the_cabled_port_and_no_existing_port_is_recabled() -> None:
    """The server's default route names the gateway at the far end of its one physical attachment."""
    server = _items("01_workload.yml", "DcimPhysicalDevice")[0]
    gateway = _items("01_workload.yml", "DcimPhysicalInterface")[0]
    cable = _items("02_cable.yml", "DcimCable")[0]
    route = _items("03_routes.yml", "ManagedStaticRoute")[0]
    assert gateway["device"] == "par-colo1-xfw-01" and gateway["name"] == "ethernet1/2"
    assert gateway["ip_address"]["data"]["address"] == "10.90.4.1/24"
    assert gateway["ip_address"]["data"]["ip_prefix"] == ["10.90.4.0/24", "default"]
    assert server["interfaces"][0]["data"]["ip_address"] == ["10.90.4.11/24", "default"]
    ends = {tuple(sorted(end["data"].items())) for end in cable["endpoints"]}
    assert ends == {
        (("device", "par-colo1-srv-01"), ("name", "eno1")),
        (("device", "par-colo1-xfw-01"), ("name", "ethernet1/2")),
    }
    assert route["next_hop"] == "10.90.4.1" and route["exported"] is False
    assert route["capabilities"] == [server["name"]]
    assert tuple(sorted(route["interface"]["data"].items())) in ends
    assert route["next_hop_interface"]["data"] == {"device": gateway["device"], "name": gateway["name"]}
    attached = [
        item["name"]
        for path in _DATA.rglob("*.yml")
        for doc in yaml.safe_load_all(path.read_text())
        if doc and doc["spec"]["kind"] == "DcimCable"
        for item in doc["spec"]["data"]
        for end in item["endpoints"]
        if end.get("data") == {"device": gateway["device"], "name": gateway["name"]}
    ]
    assert attached == [cable["name"]]


def test_c900_workload_cases_use_actual_endpoints_and_par_waypoints() -> None:
    """No golden enumeration is needed: the existing far-end runner checks one bounded witness per layer."""
    cases = yaml.safe_load((_ROOT / "tests/traversal/cases.yml").read_text())
    case = next(case for case in cases["cases"] if case.get("reachability"))
    cfg = case["reachability"]
    dc = "DcimVirtualDevice:ktw1-vm-01"
    colo = "DcimPhysicalDevice:par-colo1-srv-01"
    aws = "CloudInstance:c900-app-euc1-1"
    par = "DcimPhysicalDevice:par-colo1-edge-01"
    jobs = reach_jobs(cfg)
    for src, dst in [(dc, colo), (colo, aws), (dc, aws)]:
        assert ("l1", src, dst, par) in jobs
        assert ("l3", src, dst, None) in jobs
    instance = yaml.safe_load((_DATA / "20_C900/Cloud/02_tenant/02_instances.yml").read_text())["spec"]["data"][0]
    assert instance["name"] == "c900-app-euc1-1"
    assert instance["account"] == "c900-aws-prod"
    assert instance["network_segment"] == "c900-subnet-p"
    assert instance["private_ip"] == "10.200.1.11/24"
