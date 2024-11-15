"""Infrastructure generator."""

import logging
from ipaddress import IPv4Network
from typing import Dict, List
import pathlib
from infrahub_sdk.node import InfrahubNode
from infrahub_sdk.batch import InfrahubBatch
from infrahub_sdk.generator import InfrahubGenerator
from infrahub_sdk.exceptions import GraphQLError, ValidationError


class DCTopologyGenerator(InfrahubGenerator):
    """Generate topology."""

    async def _create_in_batch(
        self, kind: str, data_list: list,
    ) -> None:
        """Create objects of a specific kind and store in local store."""
        batch = await self.client.create_batch()
        for data in data_list:
            try:
                obj = await self.client.create(kind=kind, data=data.get("payload"))
                batch.add(task=obj.save, allow_upsert=True, node=obj)
                if data.get("store_key"):
                    self.client.store.set(key=data.get("store_key"), node=obj)
            except GraphQLError as exc:
                self.client.log.debug(f"- Creation failed due to {exc}")
        try:
            async for node, _ in batch.execute():
                object_reference = node.hfid[0] if node.hfid else node.display_label
                self.client.log.info(
                    f"- Created [{node.get_kind()}] '{object_reference}'"
                    if object_reference
                    else f"- Created [{node.get_kind()}]"
                )
        except ValidationError as exc:
            self.client.log.debug(f"- Creation failed due to {exc}")

    async def _create(self, kind: str, data: dict, store_key: str = None) -> None:
        """Create objects of a specific kind and store in local store."""
        try:
            obj = await self.client.create(kind=kind, data=data)
            await obj.save(allow_upsert=True)
            if store_key:
                self.client.store.set(key=store_key, node=obj)
        except GraphQLError as exc:
            self.client.log.debug(f"- Creation failed due to {exc}")

    async def _create_device(self, topology_name: str, data: list) -> None:
        """Create objects of a specific kind and store in local store."""
        switches_list = []
        firewall_list = []
        for device in data:
            for item in range(1, device["node"]["quantity"]["value"] + 1):
                site = topology_name.lower()
                role = device["node"]["role"]["value"]
                _data = {
                        "payload": {
                            "name": f"{site}-{role}-{str(item).zfill(2)}",
                            "device_type": device["node"]["device_type"]["node"]["id"],
                            # Here we're using hfid to get platform and location from store
                            "platform": device["node"]["device_type"]["node"][
                                "platform"
                            ]["node"]["id"],
                            "status": "active",
                            "role": role if role != "firewall" else "edge_firewall",
                            "location": self.store.get(key=topology_name).id,
                        },
                        "store_key": f"{site}-{role}-{str(item).zfill(2)}",
                    }
                if role == "firewall":
                    firewall_list.append(_data)
                else:
                    switches_list.append(_data)
        if firewall_list:
            await self._create_in_batch(
                kind="DcimFirewall", data_list=firewall_list
            )
        if switches_list:
            await self._create_in_batch(
                kind="DcimDevice", data_list=switches_list
            )
        # self._create_in_batch
        # import json

        # print(json.dumps(device_list, indent=4))

    async def generate(self, data: dict) -> None:
        """Generate topology."""
        # self.client.log = logging.Logger
        topology = data["TopologyDataCenter"]["edges"][0]["node"]
        # (topology)
        await self._create(
            kind="LocationBuilding",
            data={
                "name": topology["name"]["value"],
                "shortname": topology["name"]["value"],
                "parent": topology["location"]["node"]["id"],
            },
            store_key=topology["name"]["value"],
        )
        print(topology["name"]["value"])
        # print(topology["design"]["node"]["elements"]["edges"])
        await self._create_device(
            topology["name"]["value"], topology["design"]["node"]["elements"]["edges"]
        )
