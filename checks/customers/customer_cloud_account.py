"""Validate the cloud footprint of a customer: its account (CA1-CA3).

CA1 owner is the account's owner; CA2 segments use the account's networks; CA3 environment is the account's.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data

FOOTPRINT = "TopologyCustomerCloud"
SEGMENT = "CloudNetworkSegment"


def _ref(value: Any) -> str | None:
    """The id of a cleaned reference (clean_data collapses an object with only an ``id`` to the bare id)."""
    if isinstance(value, dict):
        value = value.get("id")
    return str(value) if value else None


def _name(node: Any) -> str:
    if isinstance(node, dict):
        return str(node.get("name") or node.get("id") or "<unnamed>")
    return str(node or "<unnamed>")


def _dicts(values: Any) -> list[dict[str, Any]]:
    return [value for value in values or [] if isinstance(value, dict)]


class CheckCustomerCloudAccount(InfrahubCheck):
    """A cloud footprint has the owner of its account and uses networks of that account (rules CA1, CA2 and CA3)."""

    query = "customer_cloud_account"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        footprints = {str(f["id"]): f for f in _dicts(cleaned.get(FOOTPRINT)) if f.get("id")}
        for footprint in footprints.values():
            self._validate_owner(footprint)
            self._validate_environment(footprint)
        for segment in _dicts(cleaned.get(SEGMENT)):
            self._validate_segment(segment, footprints)

    def _validate_owner(self, footprint: dict[str, Any]) -> None:
        """CA1."""
        account = footprint.get("account")
        owner = _ref(footprint.get("owner"))
        account_owner = _ref(account.get("owner")) if isinstance(account, dict) else None
        if isinstance(account, dict) and owner and account_owner and owner != account_owner:
            self.log_error(
                message=f"CA1: cloud footprint '{_name(footprint)}' is owned by "
                f"'{_org(footprint.get('owner'))}' but its account '{_name(account)}' by "
                f"'{_org(account.get('owner'))}': a footprint has the owner of its account.",
                object_id=footprint.get("id"),
                object_type=FOOTPRINT,
            )

    def _validate_environment(self, footprint: dict[str, Any]) -> None:
        """CA3."""
        account = footprint.get("account")
        environment = footprint.get("environment")
        account_environment = account.get("environment") if isinstance(account, dict) else None
        if environment and account_environment and environment != account_environment:
            self.log_error(
                message=f"CA3: cloud footprint '{_name(footprint)}' is '{environment}' but its account "
                f"'{_name(account)}' is '{account_environment}': a footprint has the environment of its account.",
                object_id=footprint.get("id"),
                object_type=FOOTPRINT,
            )

    def _validate_segment(self, segment: dict[str, Any], footprints: dict[str, dict[str, Any]]) -> None:
        """CA2."""
        footprint = footprints.get(_ref(segment.get("customer_deployment")) or "")
        network = segment.get("virtual_network")
        if not footprint or not isinstance(network, dict):
            return
        footprint_account = _ref(footprint.get("account"))
        network_account = _ref(network.get("account"))
        if footprint_account and network_account and footprint_account != network_account:
            self.log_error(
                message=f"CA2: cloud segment '{_name(segment)}' of footprint '{_name(footprint)}' uses virtual "
                f"network '{_name(network)}' of account '{_name(network.get('account'))}' but the footprint's "
                f"account is '{_name(footprint.get('account'))}'.",
                object_id=segment.get("id"),
                object_type=SEGMENT,
            )


def _org(owner: Any) -> str:
    if isinstance(owner, dict):
        return str(owner.get("org_id") or owner.get("id"))
    return str(owner)
