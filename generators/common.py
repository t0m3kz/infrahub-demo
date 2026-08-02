from __future__ import annotations

import inspect
from typing import Any

from infrahub_sdk.generator import InfrahubGenerator
from infrahub_sdk.protocols import CoreGeneratorDefinition
from infrahub_sdk.task.models import TaskFilter, TaskState

from .border_services import BorderServicesMixin
from .cabling import CablingMixin
from .devices import DeviceMixin
from .helpers.common import retry_delay
from .logger import FailOnErrorLoggerMixin
from .pools import PoolMixin
from .routing import RoutingMixin

# Re-export TypedDicts so existing imports (from .common import DeviceOptions, ...) keep working
from .types import CablingOptions, ChainHop, DeviceOptions, RoutingOptions  # noqa: F401

_PARENT_WAIT_TIMEOUT = 1800  # 30 min, matches tasks/demo.py's own generator-wait timeout
_PARENT_WAIT_POLL_INTERVAL = 3
_IN_FLIGHT_STATES = [TaskState.PENDING, TaskState.RUNNING, TaskState.SCHEDULED]


class CommonGenerator(
    FailOnErrorLoggerMixin,
    PoolMixin,
    DeviceMixin,
    CablingMixin,
    BorderServicesMixin,
    RoutingMixin,
    InfrahubGenerator,
):
    """Extended InfrahubGenerator with helper methods for creating objects.

    deployment_id/fabric_name are required, set in generate(); pod_name is
    optional (pod/rack generators only). ``self.logger.error()`` raises
    GeneratorError (task fails); internal helpers raise RuntimeError for
    missing prerequisites; pure helpers raise ValueError for bad inputs;
    loop iterations warn+continue on one bad item unless it invalidates
    the whole output.

    Domain logic lives in dedicated mixins, grouped by responsibility rather
    than 1:1 with each method — see PoolMixin (resource pools + the lock that
    protects concurrent pool allocation), DeviceMixin (device creation),
    CablingMixin (device-to-device cabling), BorderServicesMixin
    (border-leaf/border-spine firewall/load-balancer provisioning, shared by
    dc.py and pod.py), and RoutingMixin (BGP/OSPF underlay+overlay). Fan-out/
    orchestration (run_generator, wait_for_parent_generator_and_refetch)
    stays here since it's generator-lifecycle plumbing, not domain logic.
    """

    deployment_id: str = ""
    fabric_name: str = ""
    pod_name: str | None = None

    @staticmethod
    def _retry_delay(base: float, attempt: int, cap: float = 20.0, jitter: float = 0.25) -> float:
        """Shared jittered exponential backoff for generator retry loops."""
        return retry_delay(base=base, attempt=attempt, cap=cap, jitter=jitter)

    @staticmethod
    async def _safe_rel_add(rel: Any, obj: Any) -> None:
        """Add relation peer while supporting sync and async add() implementations."""
        result = rel.add(obj)
        if inspect.isawaitable(result):
            await result

    async def _hydrate_fabric_templates(self, roles: list[Any]) -> None:
        """Populate legacy template payloads from device_type-only role entries.

        Bootstrap data now carries device_type as the canonical relation. The
        generators still need the full template payload to build interface
        layouts, so we resolve and cache the matching template object here.
        """
        if not roles:
            return

        from .models import DeviceRole, DeviceType, Interface, Owner, Platform, Template
        from .protocols import TemplateDcimPhysicalDevice

        template_cache: dict[str, Template] = getattr(self, "_device_type_template_cache", {})

        for entry in roles:
            if not isinstance(entry, DeviceRole):
                continue
            if entry.template is not None:
                continue
            if entry.device_type is None:
                self.logger.error(f"Fabric template entry for role={entry.role!r} is missing device_type")
                raise ValueError(f"Fabric template entry for role={entry.role!r} is missing device_type")

            device_type_id = entry.device_type.id
            if device_type_id not in template_cache:
                matches = await self.client.filters(
                    kind=TemplateDcimPhysicalDevice,
                    device_type__ids=[device_type_id],
                    include=["platform", "device_type", "interfaces", "owner"],
                )
                if not matches:
                    self.logger.error(f"No device template found for device_type {device_type_id!r}")
                    raise ValueError(f"No device template found for device_type {device_type_id!r}")

                template_obj = matches[0]
                platform_rel = getattr(template_obj, "platform", None)
                platform_peer = getattr(platform_rel, "peer", None) if platform_rel else None
                device_type_rel = getattr(template_obj, "device_type", None)
                device_type_peer = getattr(device_type_rel, "peer", None) if device_type_rel else None
                owner_rel = getattr(template_obj, "owner", None)
                owner_peer = getattr(owner_rel, "peer", None) if owner_rel else None
                interface_edges = getattr(getattr(template_obj, "interfaces", None), "edges", []) or []

                template_cache[device_type_id] = Template(
                    id=template_obj.id,
                    platform=Platform(id=platform_peer.id) if platform_peer else None,
                    device_type=DeviceType(id=device_type_peer.id if device_type_peer else device_type_id),
                    owner=Owner(id=owner_peer.id) if owner_peer else None,
                    interfaces=[
                        Interface(
                            name=getattr(getattr(edge.node, "name", None), "value", ""),
                            role=getattr(getattr(edge.node, "role", None), "value", None),
                        )
                        for edge in interface_edges
                        if getattr(edge, "node", None) is not None
                        and getattr(getattr(edge.node, "name", None), "value", "")
                    ],
                )

            entry.template = template_cache[device_type_id]

        self._device_type_template_cache = template_cache

    def _require_hydrated_template(self, role: Any, *, context: str) -> Any:
        """Return a hydrated template or fail with a real runtime error.

        ``assert`` is not acceptable here because generator validation must not
        disappear under optimized Python execution.
        """
        template = getattr(role, "template", None)
        if template is None:
            role_name = getattr(role, "role", "<unknown>")
            message = f"{context} entry for role={role_name!r} is missing hydrated template"
            self.logger.error(message)
            raise ValueError(message)
        return template

    async def run_generator(self, generator_name: str, node_ids: list[str], *, wait: bool = True) -> None:
        """Run another generator definition for the given nodes.

        Used for parent-to-child fan-out (DC -> pods, pod -> racks) instead of
        an `updated` event trigger. wait=True blocks until the child task
        completes, so it always sees the parent's just-written data.
        wait=False fires without blocking (a cascade generator's terminal
        fan-out, e.g. dc_pod_cascade -> pod_rack_cascade). No-op if node_ids
        is empty.
        """
        if not node_ids:
            return

        definition = await self.client.get(kind=CoreGeneratorDefinition, name__value=generator_name)
        wait_literal = "true" if wait else "false"
        mutation = f"""
        mutation($id: String!, $nodes: [String!]) {{
          CoreGeneratorDefinitionRun(
            data: {{ id: $id, nodes: $nodes }}
            wait_until_completion: {wait_literal}
          ) {{
            ok
            task {{
              id
            }}
          }}
        }}
        """
        result = await self.client.execute_graphql(
            query=mutation,
            variables={"id": definition.id, "nodes": node_ids},
            branch_name=self.branch_name,
        )
        ok = result.get("CoreGeneratorDefinitionRun", {}).get("ok", False)
        task_id = result.get("CoreGeneratorDefinitionRun", {}).get("task", {}).get("id")
        if not ok:
            self.logger.error(f"Nested run of generator '{generator_name}' for {node_ids} did not report ok=true")
        elif wait:
            self.logger.info(f"Generator '{generator_name}' completed for {len(node_ids)} node(s): {node_ids}")
        else:
            self.logger.info(
                f"Generator '{generator_name}' started for {len(node_ids)} node(s): {node_ids} (task={task_id})"
            )

    async def wait_for_parent_generator_and_refetch(self, generator_name: str, parent_id: str) -> dict | None:
        """If the parent's own bootstrap generator is currently running for
        parent_id, wait for it and return freshly re-collected data; else None.

        Used by a cascade generator that may run concurrently with the
        parent's own bootstrap (e.g. add_dc) before its writes have landed.
        Checks for the parent's own generator task (a different
        generator_definition, by title + related_node) — never this
        generator's own task, so it can't deadlock against itself.
        """
        in_flight = await self.client.task.filter(
            filter=TaskFilter(
                branch=self.branch_name,
                related_node__ids=[parent_id],
                state=_IN_FLIGHT_STATES,
            )
        )
        parent_task_title = f"Run generator {generator_name}"
        matching = [task for task in in_flight if task.title == parent_task_title]
        if not matching:
            return None

        self.logger.info(
            f"Parent generator '{generator_name}' is running for {parent_id} — waiting for it before proceeding"
        )
        for task in matching:
            await self.client.task.wait_for_completion(
                id=task.id, interval=_PARENT_WAIT_POLL_INTERVAL, timeout=_PARENT_WAIT_TIMEOUT
            )
        return await self.collect_data()
