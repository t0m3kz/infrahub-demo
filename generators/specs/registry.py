"""Self-registering spec registry and ordered reconciler for TopologyPlan."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from utils.limits import RECONCILE_CONCURRENCY

F = TypeVar("F", bound=Callable[..., Any])
ReconcileHandler = Callable[["PlanReconciler", list[Any], "ReconcileResult"], Awaitable[None]]


def imperative_flow(*, reason: str) -> Callable[[F], F]:
    """Explicitly mark a generator method that stays imperative because it waits on peer generator convergence."""

    def _decorator(fn: F) -> F:
        setattr(fn, "_imperative_flow_reason", reason)
        return fn

    return _decorator


@dataclass(frozen=True)
class RegisteredStep:
    order: int
    spec_type: type[Any]
    handler: ReconcileHandler


_REGISTRY: dict[type[Any], RegisteredStep] = {}


def register_spec(spec_type: type[Any], *, order: int) -> Callable[[ReconcileHandler], ReconcileHandler]:
    """Register a reconciler step for ``spec_type`` at explicit numeric ``order``."""

    def _decorator(fn: ReconcileHandler) -> ReconcileHandler:
        if spec_type in _REGISTRY:
            raise ValueError(f"Duplicate reconciler registration for {spec_type.__name__}")
        _REGISTRY[spec_type] = RegisteredStep(order=order, spec_type=spec_type, handler=fn)
        return fn

    return _decorator


def registered_steps() -> list[RegisteredStep]:
    """Return all registered steps sorted by explicit execution order."""
    return sorted(_REGISTRY.values(), key=lambda step: (step.order, step.spec_type.__name__))


@dataclass
class ReconcileResult:
    device_ids: dict[str, str] = field(default_factory=dict)
    interface_ids: dict[tuple[str, str], str] = field(default_factory=dict)
    interface_roles: dict[tuple[str, str], str] = field(default_factory=dict)
    node_ids: dict[tuple[str, str], str] = field(default_factory=dict)


@dataclass
class TopologyPlan:
    """Declarative plan of specs emitted by a generator before persistence."""

    specs: list[Any] = field(default_factory=list)

    def add(self, spec: Any) -> Any:
        self.specs.append(spec)
        return spec

    def extend(self, items: list[Any]) -> None:
        self.specs.extend(items)

    def by_type(self, spec_type: type[Any]) -> list[Any]:
        return [s for s in self.specs if isinstance(s, spec_type)]

    @property
    def devices(self) -> list[Any]:
        from generators.specs.devices import DeviceSpec

        return self.by_type(DeviceSpec)

    @property
    def interfaces(self) -> list[Any]:
        from generators.specs.devices import InterfaceSpec

        return self.by_type(InterfaceSpec)

    @property
    def ips(self) -> list[Any]:
        from generators.specs.devices import IpSpec

        return self.by_type(IpSpec)

    @property
    def cables(self) -> list[Any]:
        from generators.specs.cables import CableSpec

        return self.by_type(CableSpec)

    def add_device(self, spec: Any) -> str:
        self.add(spec)
        return spec.name

    def add_interface(self, spec: Any) -> str:
        self.add(spec)
        return spec.name

    def add_ip(self, spec: Any) -> None:
        self.add(spec)

    def add_cable(self, spec: Any) -> None:
        self.add(spec)


class PlanReconciler:
    """Executes registered spec steps in explicit ascending order (Cables always last at order=90)."""

    def __init__(self, generator: Any, concurrency: int = RECONCILE_CONCURRENCY) -> None:
        self.generator = generator
        self.client = generator.client
        self.sem = asyncio.Semaphore(concurrency)

    async def apply(self, plan: TopologyPlan) -> ReconcileResult:
        import generators.specs  # noqa: F401

        result = ReconcileResult()
        grouped: dict[type[Any], list[Any]] = defaultdict(list)
        for spec in plan.specs:
            if type(spec) not in _REGISTRY:
                raise ValueError(f"Unregistered spec type: {type(spec).__name__}")
            grouped[type(spec)].append(spec)

        for step in registered_steps():
            batch = grouped.get(step.spec_type)
            if batch:
                await step.handler(self, batch, result)
        return result
