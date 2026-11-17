"""CheckCustomerCloudAccount: rules CA1 (owner is the account's) and CA2 (segments use the account's networks)."""

from __future__ import annotations

from typing import Any, cast

from checks.customers.customer_cloud_account import CheckCustomerCloudAccount


def _org(org_id: str | None) -> dict[str, Any]:
    return {"node": {"id": f"id-{org_id}", "org_id": {"value": org_id}} if org_id else None}


def _account(name: str, owner: str | None, environment: str | None = "p") -> dict[str, Any]:
    return {
        "node": {
            "id": f"id-{name}",
            "name": {"value": name},
            "environment": {"value": environment},
            "owner": _org(owner),
        }
    }


def _footprint(
    owner: str | None = "C003",
    account: str = "acct-1",
    account_owner: str | None = "C003",
    environment: str | None = "p",
    account_environment: str | None = "p",
) -> dict[str, Any]:
    return {
        "id": "id-fp",
        "name": {"value": "C003-CLOUD"},
        "environment": {"value": environment},
        "owner": _org(owner),
        "account": _account(account, account_owner, account_environment),
    }


def _segment(network_account: str = "acct-1", footprint: str | None = "id-fp") -> dict[str, Any]:
    return {
        "id": "id-seg",
        "name": {"value": "c003-web"},
        "customer_deployment": {"node": {"id": footprint, "name": {"value": "C003-CLOUD"}} if footprint else None},
        "virtual_network": {
            "node": {
                "id": "id-vnet",
                "name": {"value": "vpc-1"},
                "account": {"node": {"id": f"id-{network_account}", "name": {"value": network_account}}},
            }
        },
    }


def _run(footprints: list[dict[str, Any]], segments: list[dict[str, Any]]) -> list[str]:
    check = cast(Any, CheckCustomerCloudAccount.__new__(CheckCustomerCloudAccount))
    errors: list[dict[str, Any]] = []
    check.log_error = lambda **kwargs: errors.append(kwargs)
    check.validate(
        {
            "TopologyCustomerCloud": {"edges": [{"node": f} for f in footprints]},
            "CloudNetworkSegment": {"edges": [{"node": s} for s in segments]},
        }
    )
    for error in errors:
        assert error["object_id"] and error["object_type"]
    return [error["message"] for error in errors]


class TestCa1Owner:
    def test_owner_of_the_account_passes(self) -> None:
        assert _run([_footprint()], []) == []

    def test_other_owner_fails(self) -> None:
        errors = _run([_footprint(account_owner="C004")], [])
        assert len(errors) == 1 and errors[0].startswith("CA1: cloud footprint 'C003-CLOUD' is owned by 'C003'")
        assert "account 'acct-1' by 'C004'" in errors[0]

    def test_missing_owner_on_either_side_is_not_judged(self) -> None:
        assert _run([_footprint(owner=None)], []) == []
        assert _run([_footprint(account_owner=None)], []) == []


class TestCa2Segments:
    def test_network_of_the_account_passes(self) -> None:
        assert _run([_footprint()], [_segment()]) == []

    def test_network_of_another_account_fails(self) -> None:
        errors = _run([_footprint()], [_segment("acct-2")])
        assert len(errors) == 1
        assert errors[0].startswith("CA2: cloud segment 'c003-web' of footprint 'C003-CLOUD' uses virtual network")
        assert "account 'acct-2'" in errors[0] and "footprint's account is 'acct-1'" in errors[0]

    def test_segment_of_no_footprint_is_not_judged(self) -> None:
        assert _run([_footprint()], [_segment("acct-2", footprint=None)]) == []


class TestEnvironment:
    """CA3: the footprint states the environment of its account."""

    def test_same_environment_passes(self) -> None:
        assert _run([_footprint(environment="n", account_environment="n")], []) == []

    def test_other_environment_fails_and_names_both(self) -> None:
        errors = _run([_footprint(environment="p", account_environment="n")], [])

        assert len(errors) == 1 and "CA3" in errors[0] and "'p'" in errors[0] and "'n'" in errors[0]

    def test_a_missing_environment_on_either_side_is_not_judged(self) -> None:
        assert _run([_footprint(environment=None)], []) == []
        assert _run([_footprint(account_environment=None)], []) == []
