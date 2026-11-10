"""Unit tests for CheckSegmentPlacement."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from checks.segment_placement import CheckSegmentPlacement


def _check() -> Any:
    check = cast(Any, CheckSegmentPlacement.__new__(CheckSegmentPlacement))
    errors: list[str] = []
    warnings: list[str] = []
    check._captured_errors = errors
    check._captured_warnings = warnings
    check.log_error = lambda message: errors.append(message)
    check.log_info = lambda message: warnings.append(message)
    return check


class TestSegmentPlacementCheck:
    def test_vxlan_rejects_cloud_deployment(self) -> None:
        """VXLAN segment attached to cloud deployment should fail compatibility check."""
        check = _check()
        payload = {
            "ManagedVxlanSegment": [
                {
                    "name": "seg-vx-1",
                    "stretch_scope": "local",
                    "customer_deployments": [
                        {
                            "typename": "TopologyCustomerCloud",
                            "name": "C001-P-AWS",
                            "parent": {"typename": "TopologyCloudRegion", "name": "eu-central-1"},
                        }
                    ],
                    "segment_deployments": [],
                }
            ]
        }

        check.validate(payload)

        assert len(check._captured_errors) >= 1
        assert "unsupported deployment type" in check._captured_errors[0]

    def test_vxlan_local_requires_exactly_one_deployment(self) -> None:
        """stretch_scope=local must have exactly one customer deployment."""
        check = _check()
        payload = {
            "ManagedVxlanSegment": [
                {
                    "name": "seg-vx-2",
                    "stretch_scope": "local",
                    "customer_deployments": [
                        {
                            "typename": "TopologyCustomerDC",
                            "name": "C001-P-DC11",
                            "parent": {"typename": "TopologyDataCenter", "name": "DC11"},
                        },
                        {
                            "typename": "TopologyCustomerDC",
                            "name": "C001-P-DC12",
                            "parent": {"typename": "TopologyDataCenter", "name": "DC12"},
                        },
                    ],
                    "segment_deployments": [],
                }
            ]
        }

        check.validate(payload)

        assert any("stretch_scope=local" in msg for msg in check._captured_errors)

    def test_cloud_segment_must_attach_to_customer_cloud(self) -> None:
        """CloudNetworkSegment cannot attach to non-cloud customer deployments."""
        check = _check()
        payload = {
            "CloudNetworkSegment": [
                {
                    "name": "seg-cloud-1",
                    "customer_deployment": {"typename": "TopologyCustomerDC", "name": "C001-P-DC11"},
                }
            ]
        }

        check.validate(payload)

        assert len(check._captured_errors) == 1
        assert "must attach only to TopologyCustomerCloud" in check._captured_errors[0]

    def test_stretched_vxlan_without_global_intent_is_allowed(self) -> None:
        """Stretched scope can rely on SegmentDeployment VNI convergence without global hints."""
        check = _check()
        payload = {
            "ManagedVxlanSegment": [
                {
                    "name": "seg-vx-4",
                    "stretch_scope": "global",
                    "customer_deployments": [
                        {
                            "typename": "TopologyCustomerDC",
                            "name": "C001-P-DC11",
                            "parent": {"typename": "TopologyDataCenter", "name": "DC11"},
                        },
                        {
                            "typename": "TopologyCustomerDC",
                            "name": "C001-P-DC12",
                            "parent": {"typename": "TopologyDataCenter", "name": "DC12"},
                        },
                    ],
                    "segment_deployments": [],
                }
            ]
        }

        check.validate(payload)

        assert check._captured_errors == []


class TestOfficeSegments:
    """An office hosts its own VXLAN segments (it is TopologySegmentHosting), locally only."""

    @staticmethod
    def _segment(stretch_scope: str, deployments: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "ManagedVxlanSegment": [
                {
                    "name": "seg-office",
                    "environment": "p",
                    "stretch_scope": stretch_scope,
                    "customer_deployments": deployments,
                    "segment_deployments": [],
                }
            ]
        }

    # The segment_placement query selects no parent on an office (its parent is the campus label, which hosts
    # nothing), so the parent kind check is skipped.
    _OFFICE = {"typename": "TopologyCustomerOffice", "name": "C005-OFFICE"}

    def test_local_vxlan_segment_on_an_office_passes(self) -> None:
        check = _check()

        check.validate(self._segment("local", [self._OFFICE]))

        assert check._captured_errors == []

    def test_office_parent_is_not_checked_for_segment_hosting(self) -> None:
        """Even if a parent came back, the office is the hosting node: its parent is not judged."""
        check = _check()
        office = {**self._OFFICE, "parent": {"typename": "TopologyLabel", "name": "Campus and Office"}}

        check.validate(self._segment("local", [office]))

        assert check._captured_errors == []

    def test_stretching_a_segment_onto_an_office_is_an_error(self) -> None:
        """An office has no border gateway to stretch a segment through."""
        check = _check()
        dc = {
            "typename": "TopologyCustomerDC",
            "name": "C005-P-DC10",
            "environment": "p",
            "parent": {"typename": "TopologyDataCenter", "name": "DC10"},
        }

        check.validate(self._segment("global", [dc, self._OFFICE]))

        assert len(check._captured_errors) == 1
        assert "must be local" in check._captured_errors[0]
        assert "C005-OFFICE" in check._captured_errors[0]

    def test_other_footprints_still_need_a_segment_hosting_parent(self) -> None:
        """The exemption is the office's own: a DC footprint under a non-hosting parent is still an error."""
        check = _check()
        dc = {
            "typename": "TopologyCustomerDC",
            "name": "C005-P-DC10",
            "parent": {"typename": "TopologyLabel", "name": "Campus and Office"},
        }

        check.validate(self._segment("local", [dc]))

        assert any("not TopologySegmentHosting" in msg for msg in check._captured_errors)


class TestTermination:
    """A segment terminated on a firewall context: the gateway is the pair's VIP inside the segment prefix, the
    footprints are DC ones, and the context serves each of them."""

    @staticmethod
    def _segment(
        *,
        gateway: str | None = "10.9.0.1/24",
        prefix: str | None = "10.9.0.0/24",
        served: tuple[str, ...] = ("dep-1",),
        deployments: tuple[tuple[str, str, str], ...] = (("TopologyCustomerDC", "C001-P-DC10", "dep-1"),),
        terminated: bool = True,
    ) -> dict[str, Any]:
        """The segment_placement query payload: ``deployments`` are (kind, name, id)."""
        return {
            "ManagedVxlanSegment": [
                {
                    "name": "seg-web",
                    "stretch_scope": "local",
                    "customer_deployments": [
                        {
                            "typename": kind,
                            "name": name,
                            "id": identifier,
                            "parent": {"typename": "TopologyDataCenter", "name": "DC10"},
                        }
                        for kind, name, identifier in deployments
                    ],
                    "segment_deployments": [],
                    "gateway": {"address": gateway, "ip_prefix": {"prefix": prefix}} if gateway else None,
                    "terminated_on": (
                        {
                            "id": "ctx-1",
                            "name": "shared-fw-prod",
                            "served_deployments": [{"id": identifier} for identifier in served],
                        }
                        if terminated
                        else None
                    ),
                }
            ]
        }

    def test_a_well_formed_termination_passes(self) -> None:
        """VIP in the prefix, one DC footprint, served by the context; a segment not terminated is not examined."""
        check = _check()

        check.validate(self._segment())
        check.validate(self._segment(terminated=False, gateway=None))

        assert check._captured_errors == []

    def test_no_gateway_is_an_error(self) -> None:
        """Terminated with no gateway: the pair has no VIP."""
        check = _check()

        check.validate(self._segment(gateway=None))

        assert len(check._captured_errors) == 1
        assert "has no gateway" in check._captured_errors[0]

    def test_a_gateway_outside_the_prefix_is_an_error(self) -> None:
        """The VIP must be an address of the segment's own prefix."""
        check = _check()

        check.validate(self._segment(gateway="10.8.0.1/24"))

        assert len(check._captured_errors) == 1
        assert "outside the segment prefix 10.9.0.0/24" in check._captured_errors[0]

    def test_a_colocation_footprint_is_an_error(self) -> None:
        """Only a DC footprint has a trunk of ours to the border leaf."""
        check = _check()

        check.validate(self._segment(deployments=(("TopologyCustomerColocation", "C001-P-FR", "dep-9"),)))

        assert any("only a TopologyCustomerDC can be terminated" in msg for msg in check._captured_errors)

    def test_a_footprint_the_context_does_not_serve_is_an_error(self) -> None:
        """The context holds none of that footprint's rules: refused."""
        check = _check()

        check.validate(self._segment(served=("dep-other",)))

        assert len(check._captured_errors) == 1
        assert "is not served by it" in check._captured_errors[0]

    def test_every_footprint_must_be_served(self) -> None:
        """A stretched segment names several DC footprints: each one must be served, one error per miss."""
        check = _check()
        payload = self._segment(
            deployments=(
                ("TopologyCustomerDC", "C001-P-DC10", "dep-1"),
                ("TopologyCustomerDC", "C001-P-DC11", "dep-2"),
            ),
        )
        payload["ManagedVxlanSegment"][0]["stretch_scope"] = "dc_pair"

        check.validate(payload)

        assert [msg for msg in check._captured_errors if "not served" in msg] == [
            "Segment 'seg-web' is terminated on context 'shared-fw-prod' but TopologyCustomerDC 'C001-P-DC11' "
            "is not served by it"
        ]


class TestCloudSegmentDeployment:
    def test_cloud_segment_without_customer_deployment_is_an_error(self) -> None:
        """A cloud segment attached to no deployment is an error, not silently skipped."""
        check = _check()

        check.validate({"CloudNetworkSegment": [{"name": "c003-subnet-p", "customer_deployment": None}]})

        assert len(check._captured_errors) == 1
        assert "has no customer_deployment" in check._captured_errors[0]

    def test_cloud_segment_on_cloud_deployment_passes(self) -> None:
        """A cloud segment on TopologyCustomerCloud raises nothing."""
        check = _check()
        deployment = {"typename": "TopologyCustomerCloud", "name": "C003-P"}

        check.validate({"CloudNetworkSegment": [{"name": "c003-subnet-p", "customer_deployment": deployment}]})

        assert check._captured_errors == []


def _owner(org_id: str, kind: str = "OrganizationCustomer") -> dict[str, Any]:
    """An owner in the cleaned query shape."""
    return {"id": f"id-{org_id}", "typename": kind, "org_id": org_id}


def _footprint(
    kind: str, name: str, environment: str | None, owner: str, owner_kind: str = "OrganizationCustomer"
) -> dict[str, Any]:
    """A customer footprint the way the query selects it (environment only on DC/Colocation)."""
    footprint: dict[str, Any] = {
        "typename": kind,
        "name": name,
        "owner": _owner(owner, owner_kind),
        "parent": {"typename": "TopologyDataCenter", "name": "DC10"},
    }
    if environment is not None:
        footprint["environment"] = environment
    return footprint


def _vxlan(environment: str, owner: str, deployments: list[dict[str, Any]], zone: str | None = None) -> dict[str, Any]:
    """A local/stretched VXLAN segment payload with its environment, owner and zone."""
    return {
        "ManagedVxlanSegment": [
            {
                "name": "c005-web-p",
                "stretch_scope": "local" if len(deployments) == 1 else "dc_pair",
                "environment": environment,
                "owner": {"id": f"id-{owner}", "org_id": owner},
                "security_zone": {"name": zone} if zone else None,
                "customer_deployments": deployments,
                "segment_deployments": [],
            }
        ]
    }


class TestEnvironmentAgreement:
    """A segment shares the environment of every environment-scoped footprint it names; an office is PROD."""

    _DC_P = _footprint("TopologyCustomerDC", "C005-P-DC10", "p", "C005")
    _DC_D = _footprint("TopologyCustomerDC", "C005-D-DC12", "d", "C005")

    def test_matching_environment_passes(self) -> None:
        """Segment `p` on a `p` DC footprint raises nothing."""
        check = _check()

        check.validate(_vxlan("p", "C005", [self._DC_P]))

        assert check._captured_errors == []
        assert check._captured_warnings == []

    def test_environment_mismatch_on_a_dc_is_an_error(self) -> None:
        """A `d` segment on a `p` footprint lands in the wrong namespace and zone."""
        check = _check()

        check.validate(_vxlan("d", "C005", [self._DC_P]))

        assert len(check._captured_errors) == 1
        assert "environment 'd'" in check._captured_errors[0] and "'C005-P-DC10'" in check._captured_errors[0]

    def test_every_named_footprint_is_checked(self) -> None:
        """A stretched segment must match each footprint: only the mismatching one is reported."""
        check = _check()

        check.validate(_vxlan("p", "C005", [self._DC_P, self._DC_D]))

        assert len(check._captured_errors) == 1
        assert "C005-D-DC12" in check._captured_errors[0]

    def test_colocation_footprint_environment_is_checked(self) -> None:
        """Colocation footprints state an environment too."""
        check = _check()
        colo = _footprint("TopologyCustomerColocation", "C005-S-FR", "s", "C005")

        check.validate(_vxlan("p", "C005", [colo]))

        assert len(check._captured_errors) == 1

    def test_office_segment_must_be_prod(self) -> None:
        """An office has no environment: it is PROD, so a nonprod segment on it is an error."""
        check = _check()
        office = {"typename": "TopologyCustomerOffice", "name": "C005-OFFICE", "owner": _owner("C005")}

        check.validate(_vxlan("d", "C005", [office]))

        assert len(check._captured_errors) == 1
        assert "environment 'p'" in check._captured_errors[0] and "C005-OFFICE" in check._captured_errors[0]

    def test_prod_segment_on_an_office_passes(self) -> None:
        """A `p` segment on the office raises nothing."""
        check = _check()
        office = {"typename": "TopologyCustomerOffice", "name": "C005-OFFICE", "owner": _owner("C005")}

        check.validate(_vxlan("p", "C005", [office]))

        assert check._captured_errors == []

    def test_footprint_without_environment_is_skipped(self) -> None:
        """A footprint kind with no environment (cloud) has nothing to compare."""
        check = _check()
        cloud = {"typename": "TopologyCustomerCloud", "name": "C003-VAULTEX-AWS", "owner": _owner("C003")}

        check.validate(_vxlan("d", "C003", [cloud]))

        assert not any("shares the environment" in e for e in check._captured_errors)


class TestOwnerAgreement:
    """A segment of another owner than its footprint is a warning, never an error."""

    _DC = _footprint("TopologyCustomerDC", "C006-P-DC11", "p", "C006")

    def test_same_owner_is_silent(self) -> None:
        """Segment and footprint of one owner raise nothing."""
        check = _check()

        check.validate(_vxlan("p", "C006", [self._DC]))

        assert check._captured_errors == []
        assert check._captured_warnings == []

    def test_owner_mismatch_warns(self) -> None:
        """Another owner is a WARNING-prefixed info line and does not fail the check."""
        check = _check()

        check.validate(_vxlan("p", "C005", [self._DC]))

        assert check._captured_errors == []
        assert len(check._captured_warnings) == 1
        assert check._captured_warnings[0].startswith("WARNING: ")
        assert "'C005'" in check._captured_warnings[0] and "'C006'" in check._captured_warnings[0]

    def test_exchange_zone_segment_is_exempt(self) -> None:
        """The DC-local MANAGEMENT services of another owner are shared by design."""
        check = _check()

        check.validate(_vxlan("p", "INFRA", [self._DC], zone="MANAGEMENT"))

        assert check._captured_warnings == []

    def test_provider_owned_footprint_is_exempt(self) -> None:
        """A footprint owned by a provider (an operator hub) hosts customers' segments."""
        check = _check()
        hub = _footprint("TopologyCustomerDC", "P027-P-DC10", "p", "P027", "OrganizationProvider")

        check.validate(_vxlan("p", "C005", [hub]))

        assert check._captured_warnings == []

    def test_tenant_zone_is_not_exempt(self) -> None:
        """A PROD segment of another owner still warns."""
        check = _check()

        check.validate(_vxlan("p", "C005", [self._DC], zone="PROD"))

        assert len(check._captured_warnings) == 1


class TestQueryShape:
    """The agreement rules read fields the query must select."""

    _QUERY = (Path(__file__).parents[2] / "queries" / "validation" / "segment_placement.gql").read_text()

    def test_environment_and_owner_are_selected_on_the_segment(self) -> None:
        """The segment block selects the segment's environment, owner and zone."""
        block = self._QUERY[self._QUERY.index("ManagedVxlanSegment") : self._QUERY.index("CloudNetworkSegment")]
        assert "environment { value }" in block
        assert "owner { node { id org_id { value } } }" in block
        from tests.unit.query_shape_helpers import query_tree

        fields = query_tree(Path(__file__).parents[2] / "queries/validation/segment_placement.gql")
        assert {"id", "name", "vrf_type", "l3_vni"} <= set(
            fields["ManagedVxlanSegment"]["edges"]["node"]["security_zone"]["node"]
        )

    def test_footprint_environment_comes_from_the_scoped_generic(self) -> None:
        """Only TopologyEnvironmentScoped carries environment; the removed TopologyCustomer.environment is not read."""
        assert "... on TopologyEnvironmentScoped { environment { value } }" in self._QUERY
        assert "... on TopologyCustomer { environment" not in self._QUERY

    def test_the_termination_rules_read_their_fields(self) -> None:
        """The gateway address and prefix, and the context with the footprints it serves, are selected."""
        block = self._QUERY[self._QUERY.index("ManagedVxlanSegment") : self._QUERY.index("CloudNetworkSegment")]
        assert "gateway { node { address { value } ip_prefix { node { prefix { value } } } } }" in block
        assert "terminated_on { node { id name { value } served_deployments { edges { node { id } } } } }" in block
