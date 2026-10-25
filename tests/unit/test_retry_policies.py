"""The retry and polling policy is defined once in utils/limits.py, and every generator reads it from there.

The values are pinned: they were the module constants before they moved, and a change here changes how long a
generator run waits for another one.
"""

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
    BORDER_LEAF_WAIT,
    LB_VIP_REREAD,
    NODE_SAVE_RETRY,
    OFFICE_EDGE_WAIT,
    PARENT_GENERATOR_WAIT,
    RESOURCE_LOCK,
    RULE_SAVE_ATTEMPTS,
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
    """Every policy keeps the value its module constant had before it moved to utils/limits.py."""

    @pytest.mark.parametrize(
        ("policy", "attempts", "delay"),
        [
            pytest.param(WAIT_FOR_PEER_GENERATOR, 10, 3.0, id="wait-for-peer-generator"),
            pytest.param(NODE_SAVE_RETRY, 5, 2.0, id="node-save-retry"),
            pytest.param(LB_VIP_REREAD, 12, 5.0, id="lb-vip-reread"),
            pytest.param(OFFICE_EDGE_WAIT, 8, 3.0, id="office-edge-wait"),
            pytest.param(BORDER_LEAF_WAIT, 3, 2.0, id="border-leaf-wait"),
            pytest.param(RESOURCE_LOCK, 20, 2.0, id="resource-lock"),
        ],
    )
    def test_retry_policy_values(self, policy: RetryPolicy, attempts: int, delay: float) -> None:
        """A retry policy is (attempts, base delay)."""
        assert (policy.attempts, policy.delay) == (attempts, delay)

    def test_resource_lock_is_stale_after_five_minutes(self) -> None:
        """A lock older than 300 s is treated as abandoned."""
        assert RESOURCE_LOCK.stale_after == 300

    def test_parent_generator_wait_polls_every_three_seconds_for_half_an_hour(self) -> None:
        """The wait for a parent generator run in flight: interval 3 s, timeout 1800 s."""
        assert (PARENT_GENERATOR_WAIT.interval, PARENT_GENERATOR_WAIT.timeout) == (3, 1800)

    def test_rule_save_attempts(self) -> None:
        """A colliding policy rule index is retried up to five times."""
        assert RULE_SAVE_ATTEMPTS == 5

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
