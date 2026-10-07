"""Pytest smoke tests for config transforms.

Each configs/{name}/input.json + output.txt pair becomes one parametrized test.
The directory name prefix determines which transform class to use.

Usage:
    uv run pytest tests/smoke/ -v
    uv run pytest tests/smoke/ -k arista_eos  # filter by platform
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from transforms.cloud.vpc_terraform import CloudVpcTerraform
from transforms.config.access_leaf import AccessLeaf
from transforms.config.border_leaf import BorderLeaf
from transforms.config.edge import Edge
from transforms.config.firewall import Firewall
from transforms.config.l2_leaf import L2Leaf
from transforms.config.leaf import Leaf
from transforms.config.proxy import Proxy
from transforms.config.spine import Spine
from transforms.config.super_spine import SuperSpine
from transforms.config.tor import ToR

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIGS_DIR = Path(__file__).resolve().parent / "configs"

# Sorted longest-first so "super_spine" matches before "spine", "border_leaf" before "leaf",
# "l2_leaf"/"access_leaf" before "leaf"
_PREFIX_TO_CLASS: list[tuple[str, type]] = sorted(
    [
        ("super_spine", SuperSpine),
        ("border_leaf", BorderLeaf),
        ("cloud_terraform", CloudVpcTerraform),
        ("firewall", Firewall),
        ("spine", Spine),
        ("proxy", Proxy),
        ("l2_leaf", L2Leaf),
        ("access_leaf", AccessLeaf),
        ("leaf", Leaf),
        ("tor", ToR),
        ("edge", Edge),
    ],
    key=lambda t: len(t[0]),
    reverse=True,
)


def _run_transform(transform_cls: type, data: dict) -> str:
    """Run a transform against data using a mock client (no server needed)."""
    mock_client = MagicMock()
    mock_client.clone.return_value = mock_client
    mock_client.schema = MagicMock()
    mock_client.schema.get = AsyncMock(return_value=MagicMock())
    mock_client.execute_graphql = AsyncMock(side_effect=Exception("no server"))

    instance = transform_cls(
        client=mock_client,
        infrahub_node=MagicMock(),
        root_directory=str(PROJECT_ROOT),
    )
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(instance.transform(data))
    finally:
        loop.close()


def _fixture_params() -> list:
    if not CONFIGS_DIR.exists():
        return []
    params = []
    for d in sorted(CONFIGS_DIR.iterdir()):
        if not d.is_dir():
            continue
        input_file = d / "input.json"
        output_file = d / "output.txt"
        if not input_file.exists() or not output_file.exists():
            continue
        for prefix, cls in _PREFIX_TO_CLASS:
            if d.name.startswith(prefix + "_"):
                params.append(pytest.param(cls, d, id=d.name))
                break
    return params


@pytest.mark.parametrize("transform_cls,fixture_dir", _fixture_params())
def test_config_transform_matches_fixture(transform_cls: type, fixture_dir: Path) -> None:
    """Transform output must exactly match the saved fixture."""
    input_data = json.loads((fixture_dir / "input.json").read_text())
    is_sonic = "_sonic_" in fixture_dir.name or "_dell_sonic_" in fixture_dir.name
    expected = (fixture_dir / "output.txt").read_text()
    actual = _run_transform(transform_cls, input_data)
    if is_sonic:
        configdb = json.loads(actual)
        assert isinstance(configdb["DEVICE_METADATA"], dict)
        assert "NTP_SERVER" in configdb
        assert "SYSLOG_SERVER" in configdb
        assert not any(name.startswith("PortChannel") for name in configdb["INTERFACE"])
        if fixture_dir.name.endswith("_mlag"):
            domain = configdb["MCLAG_DOMAIN"]["1"]
            assert domain["source_ip"] == "10.254.0.0"
            assert domain["peer_ip"] == "10.254.0.1"
            assert domain["peer_link"] == "PortChannel100"
            # The control SVI is merged into the shared VLAN tables, not
            # rendered as a routed INTERFACE.
            assert configdb["VLAN"]["Vlan4094"]["vlanid"] == "4094"
            assert "Vlan4094|10.254.0.0/31" in configdb["VLAN_INTERFACE"]
            assert "Vlan4094|PortChannel100" in configdb["VLAN_MEMBER"]
            assert "Vlan4094" not in configdb["INTERFACE"]
            assert "1|PortChannel101" in configdb["MCLAG_INTERFACE"]
            assert "PortChannel100|Ethernet33" in configdb["PORTCHANNEL_MEMBER"]
            assert "PortChannel101|Ethernet10" in configdb["PORTCHANNEL_MEMBER"]
    assert actual == expected
