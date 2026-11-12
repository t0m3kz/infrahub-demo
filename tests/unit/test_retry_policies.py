"""The retry and polling policy is defined once in utils/limits.py, and every generator reads it from there."""

from __future__ import annotations

import ast
import importlib
import inspect
import re
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from generators.helpers.common import save_with_node_not_found_retry
from utils import limits
from utils.limits import (
    NODE_SAVE_RETRY,
    PARENT_GENERATOR_WAIT,
    RESOURCE_LOCK,
    WAIT_FOR_PEER_GENERATOR,
    PollPolicy,
    ResourceLockPolicy,
    RetryPolicy,
)

_GENERATORS = Path(__file__).resolve().parents[2] / "generators"

# The module that reads each policy -> the policy names it imports from utils.limits.
_USERS: dict[str, tuple[str, ...]] = {
    "generators.common": ("PARENT_GENERATOR_WAIT",),
    "generators.helpers.common": ("NODE_SAVE_RETRY",),
    "generators.connections": ("WAIT_FOR_PEER_GENERATOR",),
    "generators.lb_vip": ("LB_VIP_REREAD",),
    "generators.pools": ("RESOURCE_LOCK", "WAIT_FOR_PEER_GENERATOR"),
    "generators.rack": ("WAIT_FOR_PEER_GENERATOR",),
    "generators.routing": ("WAIT_FOR_PEER_GENERATOR",),
    "generators.segment_firewall": ("RULE_SAVE_ATTEMPTS",),
    "generators.topology.office_fabric": ("OFFICE_EDGE_WAIT",),
    "generators.topology.pod": ("BORDER_LEAF_WAIT", "WAIT_FOR_PEER_GENERATOR"),
    "generators.topology.pod_mesh": ("WAIT_FOR_PEER_GENERATOR",),
}

_RETRY_CONSTANT = re.compile(r"RETR|ATTEMPT|DELAY|INTERVAL|STALE|POLL")


class TestPolicyValues:
    """The relations the policies must keep to each other."""

    def test_a_waiter_outlasts_a_lock_that_is_still_to_become_stale(self) -> None:
        """A lock whose release failed stays until it is stale_after old; the waiters, backing off to the cap of
        generators/pools.py (10 s), must still be trying then, or every one of them fails on it."""
        waited = sum(min(RESOURCE_LOCK.delay * 2**attempt, 10.0) for attempt in range(RESOURCE_LOCK.attempts))

        assert waited > RESOURCE_LOCK.stale_after

    def test_policies_are_immutable(self) -> None:
        """A frozen policy cannot be changed by one caller for all (tests replace it with dataclasses.replace)."""
        with pytest.raises(FrozenInstanceError):
            setattr(WAIT_FOR_PEER_GENERATOR, "attempts", 1)

    def test_policy_kinds(self) -> None:
        """The lock policy is a retry policy with a stale age; the task poll is not a retry."""
        assert isinstance(RESOURCE_LOCK, ResourceLockPolicy)
        assert isinstance(RESOURCE_LOCK, RetryPolicy)
        assert isinstance(PARENT_GENERATOR_WAIT, PollPolicy)
        assert not isinstance(PARENT_GENERATOR_WAIT, RetryPolicy)


class TestModulesUseThePolicies:
    """The generators read the policies from utils/limits.py; none keeps a constant of its own."""

    @pytest.mark.parametrize("module_name", sorted(_USERS))
    def test_module_uses_the_shared_policy_objects(self, module_name: str) -> None:
        """The name in the generator module is the object defined in utils/limits.py, not a copy."""
        module = importlib.import_module(module_name)
        for name in _USERS[module_name]:
            assert getattr(module, name) is getattr(limits, name), f"{module_name}.{name}"

    def test_node_save_defaults_are_the_node_save_policy(self) -> None:
        """save_with_node_not_found_retry defaults to NODE_SAVE_RETRY (5 attempts, 2.0 s base)."""
        parameters = inspect.signature(save_with_node_not_found_retry).parameters

        assert parameters["max_attempts"].default == NODE_SAVE_RETRY.attempts
        assert parameters["base_delay"].default == NODE_SAVE_RETRY.delay

    def test_no_generator_defines_a_retry_constant_of_its_own(self) -> None:
        """A module-level number named like a retry knob in generators/ belongs in utils/limits.py section 4."""
        offenders: list[str] = []
        for path in sorted(_GENERATORS.rglob("*.py")):
            for node in ast.parse(path.read_text()).body:
                targets = (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                    if isinstance(node, ast.AnnAssign)
                    else []
                )
                value = getattr(node, "value", None)
                if not isinstance(value, ast.Constant) or not isinstance(value.value, (int, float)):
                    continue
                offenders.extend(
                    f"{path.relative_to(_GENERATORS.parent)}: {target.id}"
                    for target in targets
                    if isinstance(target, ast.Name) and _RETRY_CONSTANT.search(target.id)
                )

        assert offenders == []
