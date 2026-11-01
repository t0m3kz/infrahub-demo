"""``utils/provider_services.py`` ``connection_provider``: the one derivation of a connection's provider."""

from __future__ import annotations

from typing import Any

import pytest

from utils.provider_services import DEFAULT_FLAVOUR, connection_flavour, connection_provider


def _prov(key: str, profile: str | None = None) -> dict[str, Any]:
    """A cleaned OrganizationProvider."""
    return {"id": f"id-{key}", "name": key, "connection_profile": profile or key}


def _owner(key: str) -> dict[str, Any]:
    """A cleaned device owner that is a provider."""
    return {**_prov(key), "typename": "OrganizationProvider"}


def _expected(key: str) -> dict[str, Any]:
    return {"id": f"id-{key}", "name": key, "connection_profile": key}


FAR = "id-far"
KIT_END = {"device": {"owner": _owner("kit"), "deployment": {"id": FAR}}}


@pytest.mark.parametrize(
    ("connection", "expected"),
    [
        pytest.param({"account": {"provider": _prov("acct")}, "far": {"id": FAR}}, "acct", id="account"),
        pytest.param({"far": {"id": FAR, "provider": _prov("isp")}}, "isp", id="far-provider"),
        pytest.param({"far": {"id": FAR, "parent": {"provider": _prov("saas")}}}, "saas", id="saas-parent"),
        pytest.param({"far": {"id": FAR, "account": {"provider": _prov("billed")}}}, "billed", id="billed-account"),
        pytest.param(
            {"far": {"id": FAR}, "carrier_circuits": [{"capabilities": [KIT_END]}]}, "kit", id="carrier-end-in-far"
        ),
        pytest.param({"far": {"id": FAR, "devices": [{"owner": _owner("kit")}]}}, "kit", id="far-device-owner"),
        pytest.param({"far": {"id": FAR, "appliances": [{"owner": _owner("kit")}]}}, "kit", id="far-appliance-owner"),
    ],
)
def test_each_rung_of_the_chain_names_the_provider(connection: dict[str, Any], expected: str) -> None:
    """Every rung, taken alone, resolves to its provider."""
    assert connection_provider(connection) == _expected(expected)


def test_the_chain_order_is_account_far_parent_billed_kit() -> None:
    """With every rung present the account wins; removing the top rung promotes the next."""
    connection: dict[str, Any] = {
        "account": {"provider": _prov("acct")},
        "far": {
            "id": FAR,
            "provider": _prov("isp"),
            "parent": {"provider": _prov("saas")},
            "account": {"provider": _prov("billed")},
            "devices": [{"owner": _owner("kit")}],
        },
    }
    seen = []
    for drop in (("account",), ("far", "provider"), ("far", "parent"), ("far", "account"), None):
        found = connection_provider(connection)
        seen.append(found["name"] if found else None)
        if drop:
            holder = connection[drop[0]] if len(drop) == 2 else connection
            holder.pop(drop[-1])
    assert seen == ["acct", "isp", "saas", "billed", "kit"]


def test_a_carrier_end_outside_the_far_facility_is_not_the_far_kit() -> None:
    """The kit is the provider's kit in the FAR facility: a near-side end owned by someone else is ignored."""
    near_end = {"device": {"owner": _owner("near"), "deployment": {"id": "id-near"}}}
    connection = {"far": {"id": FAR}, "carrier_circuits": [{"capabilities": [near_end]}]}
    assert connection_provider(connection) is None


def test_an_owner_that_is_not_a_provider_is_not_the_kit() -> None:
    """A customer owner of a far device names no provider."""
    customer = {"id": "id-c", "typename": "OrganizationCustomer"}
    assert connection_provider({"far": {"id": FAR, "devices": [{"owner": customer}]}}) is None


@pytest.mark.parametrize(
    "connection",
    [
        {},
        {"account": None, "far": None},
        {"account": {"provider": None}, "far": {}},
        {"account": {"provider": {"connection_profile": "aws"}}},
        {"far": "id-far"},
        {"far": {"id": FAR, "provider": "id-only"}},
    ],
)
def test_no_provider_found_is_none(connection: dict[str, Any]) -> None:
    """Empty slots, a provider without an id, or a bare id string name nothing."""
    assert connection_provider(connection) is None


def test_a_provider_without_a_profile_is_still_the_provider() -> None:
    """The provider is found; its flavour is the default."""
    provider = {"id": "id-p", "name": "p", "connection_profile": None}
    assert connection_provider({"account": {"provider": provider}}) == provider
    assert connection_flavour({"account": {"provider": provider}, "path_type": "dedicated"}) is DEFAULT_FLAVOUR


def test_the_flavour_follows_the_provider_profile_and_path_type() -> None:
    """The profile of the resolved provider, with the connection's path type, picks the row."""
    flavour = connection_flavour({"account": {"provider": _prov("a", "azure")}, "path_type": "dedicated"})
    assert (flavour.address_source, flavour.prefix_length) == ("provider_assigned", 30)
    assert connection_flavour({"path_type": "dedicated"}) is DEFAULT_FLAVOUR
