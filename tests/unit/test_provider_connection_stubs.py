"""add_provider_connection builds the Loopback0 and the BGP process of a provider stub that has none.

Generators/provider_stub.py: authored objects are reused, the address comes from Provider-Stub-Loopback-IPv4 with the
identifier ``stub-lo0__<device id>``, everything is untracked, the stub is locked one device at a time and its lock is
released before the rest of the run, and every refusal precedes the first write.
"""

from __future__ import annotations

from typing import Any

import pytest

from generators.logger import GeneratorError
from tests.unit import provider_connection_helpers as h
from tests.unit.provider_connection_helpers import dual_link_profile  # noqa: F401
from utils.limits import PROVIDER_STUB_LOOPBACK_POOL

pytestmark = pytest.mark.usefixtures("dual_link_profile")

STUB = "dev-AWS-SE"


def _processless(**kwargs: Any) -> dict[str, Any]:
    """A dedicated connection whose far stub has no BGP process yet, in the AWS AS."""
    kwargs.setdefault("provider_as", h.AWS_AS)
    return h.connection(stubs=[h.stub_device(processes=[])], **kwargs)


async def _run(payload: dict[str, Any], client: h.FakeClient | None = None) -> h.FakeClient:
    client = client or h.FakeClient()
    await h.make_generator(client).generate(payload)
    return client


def _vpn(**kwargs: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "path_type": "internet_vpn",
        "internet": [h.internet_carrier("edge-1")],
        "stubs": [h.stub_device(processes=[])],
        "provider_as": h.AWS_AS,
        "link_prefixes": [h.prefix("p-1", "169.254.9.0/30"), h.prefix("p-2", "169.254.10.0/30")],
    }
    return h.connection(**{**defaults, **kwargs})


class TestBuild:
    @pytest.mark.asyncio
    async def test_a_stub_without_a_process_gets_its_loopback_and_its_process_before_anything_else(self) -> None:
        client = await _run(_processless())

        (alloc,) = client.of("allocate")
        assert alloc[1]["identifier"] == f"stub-lo0__{STUB}"
        assert alloc[1]["prefix_length"] == 32
        assert alloc[1]["resource_pool"] is client.pool
        (_, lo) = client.of("save", "DcimVirtualInterface")[0]
        assert lo["data"]["name"] == "Loopback0" and lo["data"]["role"] == "loopback"
        assert lo["data"]["device"] == {"id": STUB}
        (_, bgp) = client.of("save", "ManagedBGP")[0]
        assert bgp["data"]["name"] == "AWS-SE-bgp"
        assert bgp["data"]["local_as"] == {"id": h.AWS_AS}
        assert bgp["data"]["router_id"] == {"id": lo["data"]["ip_address"]["id"]}
        assert bgp["data"]["capabilities"] == [{"id": STUB}]
        saved = [kind for kind, _ in client.of("save")]
        assert saved.index("ManagedBGP") < saved.index("TopologySessionCircuit")
        assert saved.index("ManagedBGP") < saved.index("DcimVirtualInterface", 1)

    @pytest.mark.asyncio
    async def test_the_stub_objects_are_untracked(self) -> None:
        client = await _run(_processless())

        for kind in ("ManagedBGP",):
            ((_, data),) = client.of("save", kind)
            assert data["update_group_context"] is False
            assert data["id"] not in client.group_context.related_node_ids
        lo = client.of("save", "DcimVirtualInterface")[0][1]
        assert lo["update_group_context"] is False and lo["id"] not in client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_the_stub_lock_is_released_before_the_port_locks(self) -> None:
        client = await _run(_processless())

        locks = [e for e in client.events if e.startswith(("lock:", "unlock:"))]
        assert locks[:4] == [
            f"lock:pc-near-{h.NEAR}",
            "lock:pc-conn-1",
            f"lock:pc-device-{STUB}",
            f"unlock:pc-device-{STUB}",
        ]
        assert not [e for e in locks if e.startswith("lock:vc-")]

    @pytest.mark.asyncio
    async def test_an_internet_vpn_builds_the_one_stub_once(self) -> None:
        client = await _run(_vpn())

        assert len(client.of("allocate")) == 1
        assert len(client.of("save", "ManagedBGP")) == 1
        assert len(client.of("save", "TopologySessionCircuit")) == 2


class TestReuse:
    @pytest.mark.asyncio
    async def test_an_authored_loopback_is_reused_never_duplicated(self) -> None:
        client = h.FakeClient()
        client.loopbacks[STUB] = h.loopback("AWS-SE", "addr-authored")

        await _run(_processless(), client)

        assert client.of("allocate") == []
        assert not [d for _, d in client.of("save", "DcimVirtualInterface") if d["data"]["name"] == "Loopback0"]
        ((_, bgp),) = client.of("save", "ManagedBGP")
        assert bgp["data"]["router_id"] == {"id": "addr-authored"}

    @pytest.mark.asyncio
    async def test_an_authored_loopback_without_an_address_is_updated_in_place(self) -> None:
        client = h.FakeClient()
        client.loopbacks[STUB] = h.loopback("AWS-SE", None)

        await _run(_processless(), client)

        (alloc,) = client.of("allocate")
        lo = next(d for _, d in client.of("save", "DcimVirtualInterface") if d["data"]["name"] == "Loopback0")
        assert lo["data"]["id"] == "lo-AWS-SE"
        assert alloc[1]["identifier"] == f"stub-lo0__{STUB}"

    @pytest.mark.asyncio
    async def test_a_stub_that_got_its_process_meanwhile_is_left_alone(self) -> None:
        client = h.FakeClient()
        client.processes[STUB] = [h.process_node("AWS-SE-bgp")]

        await _run(_processless(), client)

        assert client.of("save", "ManagedBGP") == [] and client.of("allocate") == []

    @pytest.mark.asyncio
    async def test_a_second_connection_on_the_same_stub_builds_nothing(self) -> None:
        client = await _run(_processless())
        before = (len(client.of("allocate")), len(client.of("save", "ManagedBGP")))

        await _run(_processless(name="AWS-FR2-B"), client)

        assert before == (1, 1)
        assert (len(client.of("allocate")), len(client.of("save", "ManagedBGP"))) == before


class TestRefusals:
    @pytest.mark.asyncio
    async def test_a_process_of_another_as_found_under_the_lock_fails_before_any_write(self) -> None:
        client = h.FakeClient()
        client.processes[STUB] = [h.process_node("AWS-SE-bgp", "as-other")]

        with pytest.raises(GeneratorError, match="not in the connection's provider_as"):
            await _run(_processless(), client)

        assert client.of("save") == [] and client.of("allocate") == []

    @pytest.mark.asyncio
    async def test_an_authored_process_of_another_as_is_refused_by_the_plan(self) -> None:
        payload = h.connection(provider_as="as-other")

        with pytest.raises(GeneratorError, match="provider_as"):
            await _run(payload)

    @pytest.mark.asyncio
    async def test_a_processless_stub_needs_the_connections_provider_as(self) -> None:
        client = h.FakeClient()

        with pytest.raises(GeneratorError, match="no provider_as"):
            await _run(h.connection(stubs=[h.stub_device(processes=[])]), client)

        assert client.of("save") == [] and client.of("allocate") == []

    @pytest.mark.asyncio
    async def test_no_pool_fails_before_the_first_write(self) -> None:
        client = h.FakeClient()
        client.pool = None

        with pytest.raises(GeneratorError, match=PROVIDER_STUB_LOOPBACK_POOL):
            await _run(_processless(), client)

        assert client.of("save") == [] and client.of("allocate") == []

    @pytest.mark.asyncio
    async def test_no_pool_is_fine_when_the_loopback_is_authored(self) -> None:
        client = h.FakeClient()
        client.pool = None
        client.loopbacks[STUB] = h.loopback("AWS-SE")

        await _run(_processless(), client)

        assert len(client.of("save", "ManagedBGP")) == 1
