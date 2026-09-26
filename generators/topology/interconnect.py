"""InterconnectRequestGenerator — automates inter-topology circuit creation.

Today, TopologyPhysicalCircuit/TopologyVirtualCircuit objects (connections
between DCs, cloud regions, colocation zones) are 100% hand-authored;
generators/topology/circuit.py only validates them, never creates one. This
generator reads an opt-in TopologyInterconnectRequest and creates the
resulting circuit(s) — additive, coexisting with hand-authored circuits.

connection_kind=virtual: fully automated, reconciled every run. One
TopologyVirtualCircuit per redundancy leg, upserted by deterministic name.
Allocating a `vni`/`tunnel_id` here does not by itself imply a working
overlay — for link_type=vxlan a real inter-site EVPN control-plane (or a
static flood-list) is a separate, downstream concern this generator does not
address.

connection_kind=physical_stub: creates a status=provisioning TopologyPhysicalCircuit
shell (locations + circuit_type + placeholder circuit_id only) and hands off
to ops — never re-touched. Idempotency is anchored on a COUNT of existing
stubs already linked via resulting_circuits, not on circuit_id: ops is
expected to rename circuit_id to the real provider value once the circuit is
ordered, and circuit_id is this kind's uniqueness constraint, so re-deriving
and re-upserting by the same placeholder after a rename would create a
duplicate. A redundancy_count decrease never auto-deletes an existing stub —
only warns; deleting a possibly-already-billed real circuit is not something
a generator should do unattended.

Single provider per request is a known gap for true diverse-path/
diverse-carrier physical redundancy (same-provider "redundant" dark fiber
does not protect against a single carrier-hotel/conduit failure) — the
workaround is one TopologyInterconnectRequest per provider.

No geography-based auto-pairing (e.g. "same metro"): TopologyDataCenter has
no direct location/metro relationship in this schema (only transitively via
Pod->Suite->Facility->Metro), so every pair is named explicitly on the
request instead.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..protocols import TopologyPhysicalCircuit, TopologyVirtualCircuit
from .circuit import VirtualCircuitGenerator

_VNI_LINK_TYPES = {"vxlan"}
_GRE_KEY_LINK_TYPES = {"gre"}
_VNI_POOL_NAME = "GLOBAL-INTERCONNECT-VNI"
_GRE_KEY_POOL_NAME = "GLOBAL-INTERCONNECT-GRE-KEY"


def _sorted_interfaces(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic ordering for index-pairing endpoint_a/b interfaces —
    Infrahub many-relationships have no guaranteed order."""
    return sorted(edges, key=lambda i: ((i.get("device") or {}).get("name") or "", i.get("name") or ""))


class InterconnectRequestGenerator(CommonGenerator):
    """add_interconnect — reconciles a TopologyInterconnectRequest into
    TopologyVirtualCircuit (fully automated) or TopologyPhysicalCircuit
    (planned stub) objects."""

    graphql_root_key = "TopologyInterconnectRequest"

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        requests = cleaned.get(self.graphql_root_key, [])
        if not requests:
            self.logger.error(f"No {self.graphql_root_key} data in GraphQL response")
            return

        request = requests[0]
        request_id: str = request.get("id", "")
        request_name: str = request.get("name", "")
        if not request_id or not request_name:
            self.logger.error("InterconnectRequest missing id or name — cannot proceed")
            return

        location_a = request.get("location_a") or {}
        location_b = request.get("location_b") or {}
        provider = request.get("provider") or {}
        if not location_a.get("id") or not location_b.get("id") or not provider.get("id"):
            self.logger.error(f"InterconnectRequest {request_name}: missing location_a/location_b/provider")
            return

        connection_kind = request.get("connection_kind")
        redundancy_count = int(request.get("redundancy_count") or 1)

        if connection_kind == "virtual":
            await self._reconcile_virtual(request, request_name, location_a, location_b, provider, redundancy_count)
        elif connection_kind == "physical_stub":
            await self._reconcile_physical_stub(
                request, request_id, request_name, location_a, location_b, provider, redundancy_count
            )
        else:
            self.logger.error(f"InterconnectRequest {request_name}: unknown connection_kind={connection_kind!r}")

    # ------------------------------------------------------------------
    # connection_kind=virtual — fully automated, reconciled every run
    # ------------------------------------------------------------------

    async def _reconcile_virtual(
        self,
        request: dict[str, Any],
        request_name: str,
        location_a: dict[str, Any],
        location_b: dict[str, Any],
        provider: dict[str, Any],
        redundancy_count: int,
    ) -> None:
        link_type = request.get("link_type")
        if not link_type:
            self.logger.error(f"InterconnectRequest {request_name}: connection_kind=virtual requires link_type")
            return

        pairs = list(
            zip(
                _sorted_interfaces(request.get("endpoint_a_interfaces") or []),
                _sorted_interfaces(request.get("endpoint_b_interfaces") or []),
            )
        )
        if not pairs:
            self.logger.warning(
                f"InterconnectRequest {request_name}: no endpoint_a_interfaces/endpoint_b_interfaces pairs "
                "available — skipping virtual circuit creation"
            )
            return

        legs_to_build = min(redundancy_count, len(pairs))
        if legs_to_build < redundancy_count:
            self.logger.warning(
                f"InterconnectRequest {request_name}: redundancy_count={redundancy_count} but only "
                f"{len(pairs)} interface pair(s) available — building {legs_to_build}"
            )

        owner = request.get("owner") or {}
        transport_mode = VirtualCircuitGenerator._infer_transport_mode(link_type)

        for idx in range(legs_to_build):
            leg_name = f"{request_name}-{idx + 1:02d}"
            iface_a, iface_b = pairs[idx]

            leg_data: dict[str, Any] = {
                "name": leg_name,
                "description": request.get("description") or f"Auto-generated from {request_name}",
                "bandwidth": request.get("bandwidth"),
                "status": "active",
                "link_type": link_type,
                "transport_mode": transport_mode,
                "provider": {"id": provider["id"]},
                "locations": [{"id": location_a["id"]}, {"id": location_b["id"]}],
                "interface_capabilities": [{"id": iface_a["id"]}, {"id": iface_b["id"]}],
            }
            if owner.get("id"):
                leg_data["owner"] = {"id": owner["id"]}

            if link_type in _VNI_LINK_TYPES:
                leg_data["vni"] = await self._pool_allocation(_VNI_POOL_NAME, f"{leg_name}-vni")
            elif link_type in _GRE_KEY_LINK_TYPES:
                leg_data["tunnel_id"] = await self._pool_allocation(_GRE_KEY_POOL_NAME, f"{leg_name}-tunnel_id")

            try:
                circuit_obj = await self.client.create(kind=TopologyVirtualCircuit, data=leg_data)
                await circuit_obj.save(allow_upsert=True)
                self.logger.info(f"InterconnectRequest {request_name}: upserted virtual circuit leg '{leg_name}'")
            except Exception as exc:
                self.logger.error(f"InterconnectRequest {request_name}: failed to upsert leg '{leg_name}': {exc}")

    async def _pool_allocation(self, pool_name: str, identifier: str) -> dict[str, Any] | None:
        try:
            pool = await self.client.get(kind="CoreNumberPool", name__value=pool_name)
        except Exception as exc:
            self.logger.warning(f"Could not find pool '{pool_name}': {exc}")
            return None
        return {"from_pool": {"id": pool.id}, "identifier": identifier}

    # ------------------------------------------------------------------
    # connection_kind=physical_stub — planned stub, create-once, never
    # re-touched. Idempotency anchored on a COUNT of existing stubs already
    # in resulting_circuits, never on circuit_id (ops renames it once the
    # real circuit exists).
    # ------------------------------------------------------------------

    async def _reconcile_physical_stub(
        self,
        request: dict[str, Any],
        request_id: str,
        request_name: str,
        location_a: dict[str, Any],
        location_b: dict[str, Any],
        provider: dict[str, Any],
        redundancy_count: int,
    ) -> None:
        circuit_type = request.get("circuit_type")
        if not circuit_type:
            self.logger.error(
                f"InterconnectRequest {request_name}: connection_kind=physical_stub requires circuit_type"
            )
            return

        resulting = request.get("resulting_circuits") or []
        existing_stub_count = sum(1 for c in resulting if c.get("typename") == "TopologyPhysicalCircuit")

        shortfall = redundancy_count - existing_stub_count
        if shortfall <= 0:
            if shortfall < 0:
                self.logger.warning(
                    f"InterconnectRequest {request_name}: has {existing_stub_count} planned leg(s) but "
                    f"redundancy_count={redundancy_count} — excess stub(s) are not auto-deleted, "
                    "decommission manually if no longer needed"
                )
            else:
                self.logger.info(
                    f"InterconnectRequest {request_name}: already has {existing_stub_count}/{redundancy_count} "
                    "planned leg(s) — nothing to do"
                )
            return

        owner = request.get("owner") or {}
        new_stub_ids: list[dict[str, str]] = []
        for offset in range(shortfall):
            leg_index = existing_stub_count + offset + 1
            placeholder_id = f"{request_name}-{leg_index:02d}-PLANNED"

            stub_data: dict[str, Any] = {
                "circuit_id": placeholder_id,
                "name": placeholder_id,
                "description": f"{request_name}-leg-{leg_index:02d}",
                "circuit_type": circuit_type,
                # "provisioning" (not a bespoke "planned" value — TopologyCircuit.status
                # has no such choice) is the existing status this schema already uses
                # for "not live yet".
                "status": "provisioning",
                "bandwidth": request.get("bandwidth"),
                "provider": {"id": provider["id"]},
                "locations": [{"id": location_a["id"]}, {"id": location_b["id"]}],
            }
            if owner.get("id"):
                stub_data["owner"] = {"id": owner["id"]}

            try:
                stub_obj = await self.client.create(kind=TopologyPhysicalCircuit, data=stub_data)
                # update_group_context=False: this is a real-world object with
                # provider lead time — a later run must never delete/retouch
                # it even if redundancy_count drops (see the shortfall<0
                # warn-only branch above).
                await stub_obj.save(allow_upsert=True, update_group_context=False)
                new_stub_ids.append({"id": stub_obj.id})
                self.logger.info(
                    f"InterconnectRequest {request_name}: created planned physical circuit stub '{placeholder_id}'"
                )
            except Exception as exc:
                self.logger.error(
                    f"InterconnectRequest {request_name}: failed to create planned stub '{placeholder_id}': {exc}"
                )

        if not new_stub_ids:
            return

        try:
            request_obj = await self.client.create(
                kind="TopologyInterconnectRequest",
                data={"id": request_id, "resulting_circuits": [*[{"id": c["id"]} for c in resulting], *new_stub_ids]},
            )
            await request_obj.save(allow_upsert=True)
        except Exception as exc:
            self.logger.error(f"InterconnectRequest {request_name}: failed to record resulting_circuits: {exc}")
