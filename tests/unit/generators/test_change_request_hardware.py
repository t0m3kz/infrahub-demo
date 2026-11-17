"""Affected devices are hardware: a virtual device counts as its host, an appliance as nothing; the host's customer
owner is an affected customer.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.generators import test_change_request_generator as base
from utils.change_rules import ArtifactChange, affected_customers, hardware_changes, hosting_device_ids
from utils.data_cleaning import clean_data

_v = base._v
_edges = base._edges


def _row(device_id: str, name: str, **extra: Any) -> dict[str, Any]:
    """A raw ``DcimDeviceManaged`` row of change_request_facts.gql."""
    return {"id": device_id, "__typename": "Dcim", "name": _v(name), "role": _v("leaf"), **extra}


def physical(device_id: str, name: str, owner: str = "OrganizationProvider", owner_id: str = "o1") -> dict[str, Any]:
    """A physical device row (the ``hardware`` alias is set) with its owner."""
    return _row(
        device_id,
        name,
        hardware=device_id,
        owner={"node": {"__typename": owner, "id": owner_id, "name": _v(f"{owner}-{owner_id}")}},
    )


def virtual(device_id: str, name: str, host: str | None) -> dict[str, Any]:
    """A virtual device row hosted on ``host`` (or on nothing)."""
    return _row(device_id, name, hosting_device={"node": {"id": host}} if host else {"node": None})


def appliance(device_id: str, name: str) -> dict[str, Any]:
    """A provider-hosted appliance: neither physical nor hosted."""
    return _row(device_id, name, deployment={"node": {"name": _v("region")}})


def _artifact(artifact_id: str, target_id: str, target: str) -> ArtifactChange:
    return ArtifactChange(artifact_id, "updated", target_id, target, "cfg", "n", "o", "s")


def _rows(*rows: dict[str, Any]) -> list[dict[str, Any]]:
    return clean_data({"DcimDeviceManaged": _edges(*rows)})["DcimDeviceManaged"]


class TestHardwareChanges:
    """hardware_changes maps the artifact targets to the hardware they touch."""

    def test_a_physical_target_is_kept(self) -> None:
        """A physical device stays itself."""
        rows = _rows(physical("p1", "leaf-1"))

        changes, hardware = hardware_changes([_artifact("a1", "p1", "leaf-1")], rows)

        assert [(c.target_id, c.target) for c in changes] == [("p1", "leaf-1")]
        assert [r["id"] for r in hardware] == ["p1"]

    def test_a_virtual_target_is_its_host_and_two_vms_of_one_host_are_one(self) -> None:
        """Both VMs map to host h1 (its row came from the second query); the host appears once, with both configs."""
        rows = _rows(virtual("v1", "vm-1", "h1"), virtual("v2", "vm-2", "h1"), physical("h1", "host-1"))

        changes, hardware = hardware_changes([_artifact("a1", "v1", "vm-1"), _artifact("a2", "v2", "vm-2")], rows)

        assert [c.target for c in changes] == ["host-1", "host-1"]
        assert [c.id for c in changes] == ["a1", "a2"]
        assert [r["id"] for r in hardware] == ["h1"]
        assert hosting_device_ids(rows) == {"h1"}

    def test_an_appliance_and_an_unhosted_virtual_device_are_left_out(self) -> None:
        """No hardware behind them: no device, no artifact."""
        rows = _rows(appliance("c1", "cloud-edge"), virtual("v1", "vm-1", None), physical("p1", "leaf-1"))
        artifacts = [
            _artifact("a1", "c1", "cloud-edge"),
            _artifact("a2", "v1", "vm-1"),
            _artifact("a3", "p1", "leaf-1"),
        ]

        changes, hardware = hardware_changes(artifacts, rows)

        assert [c.id for c in changes] == ["a3"]
        assert [r["id"] for r in hardware] == ["p1"]

    def test_a_host_that_was_not_read_is_left_out(self) -> None:
        """A VM whose host row is missing yields nothing rather than a wrong device."""
        changes, hardware = hardware_changes([_artifact("a1", "v1", "vm-1")], _rows(virtual("v1", "vm-1", "h1")))

        assert (changes, hardware) == ([], [])


class TestHardwareOwners:
    """The owner of the affected hardware is an affected customer when it is an OrganizationCustomer."""

    def test_a_customer_owned_host_is_listed_and_a_provider_is_not(self) -> None:
        """The host of a VM is read as a row: its customer owner counts, a provider owner does not."""
        rows = _rows(
            virtual("v1", "vm-1", "h1"),
            physical("h1", "host-1", owner="OrganizationCustomer", owner_id="c9"),
            physical("p1", "leaf-1", owner="OrganizationProvider", owner_id="isp"),
        )

        assert affected_customers({"DcimDeviceManaged": rows}) == {"c9": "OrganizationCustomer-c9"}


@pytest.mark.parametrize("hosted", [True, False])
def test_the_generator_lists_hardware_only(monkeypatch: pytest.MonkeyPatch, hosted: bool) -> None:
    """Targets: a leaf, two VMs of host h1 (a customer's) and an appliance; the request gets leaf and host only,
    and the host's owner is a customer; the host is read by a second facts call."""
    artifacts = {
        "a1": base.ARTIFACTS["a1"],
        "a2": {**base.ARTIFACTS["a2"], "target": {"id": "v1", "kind": "DcimVirtualDevice", "display_label": "vm-1"}},
        "a3": {
            **base.ARTIFACTS["a2"],
            "id": "a3",
            "target": {"id": "v2", "kind": "DcimVirtualDevice", "display_label": "vm-2"},
        },
        "a4": {
            **base.ARTIFACTS["a2"],
            "id": "a4",
            "target": {"id": "c1", "kind": "CloudNetworkAppliance", "display_label": "cloud-edge"},
        },
    }
    host = physical("h1", "host-1", owner="OrganizationCustomer", owner_id="c9")
    answers = {
        "d1": base._device("d1", "dc1-leaf-01", "dc1-leaf-02"),
        "v1": virtual("v1", "vm-1", "h1" if hosted else None),
        "v2": virtual("v2", "vm-2", "h1" if hosted else None),
        "c1": appliance("c1", "cloud-edge"),
        "h1": host,
    }

    class Client(base.FakeClient):
        async def execute_graphql(self, query: str, branch_name: str | None = None, variables: Any = None) -> Any:
            result = await super().execute_graphql(query, branch_name, variables)
            if "change_request_facts" in query:
                return {"DcimDeviceManaged": _edges(*(answers[i] for i in variables["device_ids"]))}
            return result

    monkeypatch.setattr(base, "ARTIFACTS", artifacts)
    client = Client()

    base._run(client, base.request_row(), monkeypatch)

    facts_calls = [v["device_ids"] for op, _, v in client.calls if op == "change_request_facts"]
    adds = {d["name"]: sorted(n["id"] for n in d["nodes"]) for d in client.mutations("EdgeAdd")}
    if hosted:
        assert facts_calls == [["c1", "d1", "v1", "v2"], ["h1"]]
        assert adds == {"affected_devices": ["d1", "h1"], "affected_customers": ["c9", "cust-c005"]}
    else:
        assert facts_calls == [["c1", "d1", "v1", "v2"]]
        assert adds == {"affected_devices": ["d1"], "affected_customers": ["cust-c005"]}
