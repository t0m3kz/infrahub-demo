"""Integration test — the 30_all compute layer, end to end.

test_59 proves the load converged and every generator fired. This module
proves the result is the fabric the data describes, all the way from a switch
port to a customer application:

    access-leaf port -> cable -> host NIC -> host -> VM -> component -> app

That chain is what the change-risk check walks (docs/change_risk.md), so each
link in it gets its own assertion here. In particular the *shape* of the
cabling matters, not just its existence: a DC host is dual-homed to two
distinct access-leafs, and the two hosts in a DC sit in different rack rows so
their access-leaf pairs are disjoint. A colocation cage host is dual-homed to
one switch — deliberately, as the shared-fate counter-example.

Runs against the branch test_59 builds; it never loads data of its own.
"""

import logging

import pytest
from infrahub_sdk import InfrahubClient

from .conftest import TestInfrahubDockerWithClient
from .test_constants import (
    ALL_DEMO_BRANCH,
    ALL_DEMO_CLOUD_APPLICATIONS,
    ALL_DEMO_COLO_HOST_SWITCHES,
    ALL_DEMO_COMPONENT_INSTANCE_COUNT,
    ALL_DEMO_DC_HOST_ROWS,
    ALL_DEMO_DC_NAMES,
    ALL_DEMO_DC_OVERLAY_ROLES,
    ALL_DEMO_DC_ROLE_COUNTS,
    ALL_DEMO_DC_UNDERLAY_ROLES,
    ALL_DEMO_EXPECTED_APPLICATIONS,
    ALL_DEMO_HOST_LINK_COUNT,
    ALL_DEMO_NO_COMPUTE_APPLICATIONS,
)
from .test_helpers import (
    compute_role_counts,
    compute_routing_summary,
    fetch_application_graph,
    fetch_dc_topology,
    fetch_endpoint_cabling,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 30_all: Compute Layer"


class TestAllDemoCompute(TestInfrahubDockerWithClient):
    """Verify the three fabrics, their hosts' cabling, and the app graph."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return ALL_DEMO_BRANCH

    # ------------------------------------------------------------------
    # The three fabrics
    # ------------------------------------------------------------------

    @pytest.mark.order(390)
    @pytest.mark.dependency(scope="session", name="all_demo_dc_topology", depends=["all_demo_inventory"])
    @pytest.mark.asyncio
    async def test_01_verify_dc_topologies(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify all three DCs generated the same, complete device set.

        The three topology files are deliberately identical in shape, so any
        divergence between DC10, DC11 and DC12 is a generator bug rather than a
        data difference — which makes an exact role count the right assertion
        here, not a lower bound.
        """
        logging.info("=== %s - Step 1: DC Topologies ===", SCENARIO_NAME)

        errors: list[str] = []
        for dc_name in ALL_DEMO_DC_NAMES:
            topology = await fetch_dc_topology(client=async_client_main, branch=scenario_branch, dc_name=dc_name)
            role_counts = compute_role_counts(topology["devices"])

            for role, expected in sorted(ALL_DEMO_DC_ROLE_COUNTS.items()):
                actual = role_counts.get(role, 0)
                if actual != expected:
                    errors.append(f"{dc_name} role '{role}': expected exactly {expected}, got {actual}")

            unexpected = sorted(set(role_counts) - set(ALL_DEMO_DC_ROLE_COUNTS))
            if unexpected:
                errors.append(f"{dc_name} has devices in unexpected role(s): {unexpected}")

            logging.info("%s: %d device(s) %s", dc_name, len(topology["devices"]), dict(sorted(role_counts.items())))

        assert not errors, f"30_all DC topologies are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

    @pytest.mark.order(391)
    @pytest.mark.dependency(scope="session", name="all_demo_dc_routing", depends=["all_demo_dc_topology"])
    @pytest.mark.asyncio
    async def test_02_verify_dc_routing(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify every fabric device routes, with the right protocol mix.

        All three DCs are ebgp-ebgp, so a single iBGP session anywhere means a
        generator picked the wrong strategy — and an OSPF process means it
        picked the wrong underlay entirely.
        """
        logging.info("=== %s - Step 2: DC Routing ===", SCENARIO_NAME)

        errors: list[str] = []
        for dc_name in ALL_DEMO_DC_NAMES:
            topology = await fetch_dc_topology(client=async_client_main, branch=scenario_branch, dc_name=dc_name)
            routing = compute_routing_summary(topology["devices"])

            if routing["bgp_count"] == 0:
                errors.append(f"{dc_name}: no BGP processes at all")
            if routing["ospf_count"] != 0:
                errors.append(f"{dc_name}: ebgp-ebgp fabric has {routing['ospf_count']} OSPF process(es)")
            if routing["bgp_breakdown"]["ibgp"] != 0:
                errors.append(f"{dc_name}: ebgp-ebgp fabric has {routing['bgp_breakdown']['ibgp']} iBGP session(s)")
            if routing["bgp_breakdown"]["ebgp"] == 0:
                errors.append(f"{dc_name}: no eBGP sessions")

            # Walk the *device* list, not the routing summary: a device with no
            # routing at all never appears in device_routing, so iterating that
            # would silently skip exactly the devices worth complaining about.
            device_routing = routing["device_routing"]
            for device in sorted(topology["devices"], key=lambda d: str(d.get("name") or "")):
                device_name = str(device.get("name") or "")
                role = str(device.get("role") or "unknown")
                if role not in ALL_DEMO_DC_UNDERLAY_ROLES:
                    continue

                info = device_routing.get(device_name)
                if info is None:
                    errors.append(
                        f"{dc_name}/{device_name} ({role}): no routing capabilities at all — the device "
                        "was created but its BGP process was never generated"
                    )
                    continue

                if not info["underlay_process"]:
                    errors.append(f"{dc_name}/{device_name} ({role}): no underlay BGP process")
                if info["underlay_peerings"] == 0:
                    errors.append(f"{dc_name}/{device_name} ({role}): 0 underlay peerings")
                if role in ALL_DEMO_DC_OVERLAY_ROLES:
                    if not info["overlay_process"]:
                        errors.append(f"{dc_name}/{device_name} ({role}): no overlay BGP process")
                    if info["overlay_peerings"] == 0:
                        errors.append(f"{dc_name}/{device_name} ({role}): 0 overlay peerings")

            logging.info(
                "%s routing: %d BGP process(es), %d session(s) (%s)",
                dc_name,
                routing["bgp_count"],
                routing["bgp_session_count"],
                routing["bgp_breakdown"],
            )

        assert not errors, f"30_all DC routing is wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

    # ------------------------------------------------------------------
    # Host cabling
    # ------------------------------------------------------------------

    @pytest.mark.order(392)
    @pytest.mark.dependency(scope="session", name="all_demo_endpoint_cabling", depends=["all_demo_dc_topology"])
    @pytest.mark.asyncio
    async def test_03_verify_endpoint_cabling(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify every application host is cabled the way its site demands.

        DC hosts: two links, two *distinct* access-leafs, and the pair used by
        the row-1 host disjoint from the row-2 host's pair.
        Cage hosts: two links onto the one cage switch, on distinct ports.
        """
        logging.info("=== %s - Step 3: Endpoint Cabling ===", SCENARIO_NAME)

        hosts = await fetch_endpoint_cabling(client=async_client_main, branch=scenario_branch)
        by_name = {host["name"]: host for host in hosts}

        errors: list[str] = []

        expected_hosts = set(ALL_DEMO_DC_HOST_ROWS) | set(ALL_DEMO_COLO_HOST_SWITCHES)
        missing = sorted(expected_hosts - set(by_name))
        if missing:
            errors.append(f"application host(s) absent or not role=endpoint: {missing}")

        # DC hosts — dual-homed to a distinct access-leaf pair per rack row.
        pairs_by_dc: dict[str, dict[int, tuple[str, ...]]] = {}
        for host_name, expected_row in sorted(ALL_DEMO_DC_HOST_ROWS.items()):
            host = by_name.get(host_name)
            if host is None:
                continue
            links = host["links"]
            if len(links) != ALL_DEMO_HOST_LINK_COUNT:
                errors.append(f"{host_name}: {len(links)} cabled link(s), expected {ALL_DEMO_HOST_LINK_COUNT}")
            if host["rack_row"] != expected_row:
                errors.append(f"{host_name}: in rack row {host['rack_row']}, expected {expected_row}")
            if host["rack_type"] != "compute":
                errors.append(f"{host_name}: rack_type '{host['rack_type']}', expected 'compute'")

            wrong_role = sorted({link["peer_device"] for link in links if link["peer_role"] != "access-leaf"})
            if wrong_role:
                errors.append(f"{host_name}: cabled to non-access-leaf device(s) {wrong_role}")

            peers = tuple(sorted({link["peer_device"] for link in links}))
            if len(peers) != ALL_DEMO_HOST_LINK_COUNT:
                errors.append(f"{host_name}: not dual-homed — both NICs land on {peers}")

            dc_key = host_name.split("-", 1)[0]
            pairs_by_dc.setdefault(dc_key, {})[expected_row] = peers

        for dc_key, rows in sorted(pairs_by_dc.items()):
            if len(rows) < 2:
                continue
            row_pairs = list(rows.values())
            shared = set(row_pairs[0]) & set(row_pairs[1])
            if shared:
                errors.append(
                    f"{dc_key}: row-1 and row-2 hosts share access-leaf(s) {sorted(shared)} — "
                    "the two rows must fail independently"
                )

        # Cage hosts — both NICs onto the single cage switch, distinct ports.
        for host_name, switch in sorted(ALL_DEMO_COLO_HOST_SWITCHES.items()):
            host = by_name.get(host_name)
            if host is None:
                continue
            links = host["links"]
            if len(links) != ALL_DEMO_HOST_LINK_COUNT:
                errors.append(f"{host_name}: {len(links)} cabled link(s), expected {ALL_DEMO_HOST_LINK_COUNT}")
            peers = sorted({link["peer_device"] for link in links})
            if peers != [switch]:
                errors.append(f"{host_name}: cabled to {peers}, expected only the cage switch '{switch}'")
            ports = {link["peer_interface"] for link in links}
            if len(ports) != len(links):
                errors.append(f"{host_name}: both NICs share a switch port ({sorted(ports)})")

        assert not errors, f"30_all endpoint cabling is wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("Cabling verified for %d application host(s)", len(expected_hosts))

    # ------------------------------------------------------------------
    # Application graph
    # ------------------------------------------------------------------

    @pytest.mark.order(393)
    @pytest.mark.dependency(scope="session", name="all_demo_app_graph", depends=["all_demo_endpoint_cabling"])
    @pytest.mark.asyncio
    async def test_04_verify_application_graph(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify each application resolves to components, segments and hosts.

        The assertion that matters most is the last one: every on-prem instance
        names its hosting_device, and a component's two instances never land on
        the same host. Without that edge the application layer is an island and
        no amount of fabric data tells you who an outage hits.
        """
        logging.info("=== %s - Step 4: Application Graph ===", SCENARIO_NAME)

        applications = await fetch_application_graph(client=async_client_main, branch=scenario_branch)
        by_name = {app["name"]: app for app in applications if app["name"] not in ALL_DEMO_NO_COMPUTE_APPLICATIONS}

        errors: list[str] = []

        missing = sorted(set(ALL_DEMO_EXPECTED_APPLICATIONS) - set(by_name))
        if missing:
            errors.append(f"application(s) missing: {missing}")
        unexpected = sorted(set(by_name) - set(ALL_DEMO_EXPECTED_APPLICATIONS))
        if unexpected:
            errors.append(f"unexpected application(s): {unexpected}")

        for app_name, (criticality, component_count) in sorted(ALL_DEMO_EXPECTED_APPLICATIONS.items()):
            app = by_name.get(app_name)
            if app is None:
                continue
            if app["criticality"] != criticality:
                errors.append(f"{app_name}: criticality '{app['criticality']}', expected '{criticality}'")
            if len(app["components"]) != component_count:
                errors.append(f"{app_name}: {len(app['components'])} component(s), expected {component_count}")

            cloud_native = app_name in ALL_DEMO_CLOUD_APPLICATIONS

            for component in app["components"]:
                label = f"{app_name}/{component['slug'] or '<unnamed>'}"
                if not component["component_type"]:
                    errors.append(f"{label}: no component_type")
                if not component["segment"]:
                    errors.append(f"{label}: not attached to a network segment")

                instances = component["instances"]
                if len(instances) != ALL_DEMO_COMPONENT_INSTANCE_COUNT:
                    errors.append(
                        f"{label}: {len(instances)} instance(s), expected {ALL_DEMO_COMPONENT_INSTANCE_COUNT}"
                    )

                if cloud_native:
                    non_cloud = sorted(i["name"] for i in instances if i["kind"] != "CloudInstance")
                    if non_cloud:
                        errors.append(f"{label}: cloud-native app has non-CloudInstance instance(s) {non_cloud}")
                    continue

                unhosted = sorted(i["name"] for i in instances if not i["host"])
                if unhosted:
                    errors.append(f"{label}: instance(s) with no hosting_device: {unhosted}")
                hosts = [i["host"] for i in instances if i["host"]]
                if len(hosts) > 1 and len(set(hosts)) == 1:
                    errors.append(f"{label}: both instances run on '{hosts[0]}' — the HA pair shares a host")
                wrong_role = sorted({i["host"] for i in instances if i["host"] and i["host_role"] != "endpoint"})
                if wrong_role:
                    errors.append(f"{label}: hosted on non-endpoint device(s) {wrong_role}")

        assert not errors, f"30_all application graph is wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        total_components = sum(len(app["components"]) for app in applications)
        logging.info("Application graph verified: %d app(s), %d component(s)", len(applications), total_components)
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
