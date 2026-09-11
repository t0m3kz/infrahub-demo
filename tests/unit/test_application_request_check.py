"""Unit tests for application deployment request validation."""

from __future__ import annotations

from unittest.mock import MagicMock

from checks.application_request import CheckApplicationDeploymentRequest


def _check(data: dict) -> MagicMock:
    check = CheckApplicationDeploymentRequest.__new__(CheckApplicationDeploymentRequest)
    check.log_error = MagicMock()
    check.validate(data)
    return check.log_error


def test_draft_request_is_not_validated_as_provisionable() -> None:
    errors = _check({"AppDeploymentRequest": [{"name": "draft", "status": "draft"}]})
    errors.assert_not_called()


def test_approved_request_requires_components_and_fqdn() -> None:
    errors = _check(
        {
            "AppDeploymentRequest": [
                {
                    "name": "checkout",
                    "status": "approved",
                    "customer": {"id": "customer-1"},
                    "components": [],
                }
            ]
        }
    )
    assert errors.call_count == 2


def test_private_access_endpoint_requires_allowed_group() -> None:
    errors = _check(
        {
            "AppDeploymentRequest": [
                {
                    "name": "checkout",
                    "status": "approved",
                    "application_fqdn": "checkout.example.com",
                    "customer": {"id": "customer-1"},
                    "components": [
                        {
                            "endpoints": [
                                {
                                    "id": "endpoint-1",
                                    "endpoint_type": "private_access",
                                    "fqdn": "checkout.internal.example.com",
                                    "access_profile": {"allowed_groups": []},
                                }
                            ]
                        }
                    ],
                }
            ]
        }
    )
    assert errors.call_count == 1


def test_approved_existing_target_requires_target_owner_approval() -> None:
    errors = _check(
        {
            "AppDeploymentRequest": [
                {
                    "name": "checkout",
                    "status": "approved",
                    "application_fqdn": "checkout.example.com",
                    "customer": {"id": "customer-1"},
                    "components": [{"endpoints": []}],
                    "dependencies": [
                        {
                            "name": "checkout-to-auth",
                            "business_justification": "Login token validation",
                            "access_status": "approved",
                            "approved_by": {"id": "customer-1"},
                            "existing_target": {"parent": {"parent": {"parent": {"owner": {"id": "customer-2"}}}}},
                        }
                    ],
                }
            ]
        }
    )
    assert errors.call_count == 1


def test_dependency_requires_exactly_one_target() -> None:
    errors = _check(
        {
            "AppDeploymentRequest": [
                {
                    "name": "checkout",
                    "status": "approved",
                    "application_fqdn": "checkout.example.com",
                    "customer": {"id": "customer-1"},
                    "components": [{"endpoints": []}],
                    "dependencies": [
                        {
                            "name": "missing-target",
                            "business_justification": "Required connectivity",
                            "access_status": "pending",
                        }
                    ],
                }
            ]
        }
    )
    assert errors.call_count == 1
