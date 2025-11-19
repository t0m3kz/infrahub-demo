from __future__ import annotations

from .common import CommonGenerator


class RackGenerator(CommonGenerator):
    """Generator for creating rack infrastructure based on fabric templates."""

    async def generate(self, data: dict) -> None:
        """Generate rack topology with special handling for OOB and console devices."""
        try:
            deployment_list = self.clean_data(data).get("LocationRack", [])
            if not deployment_list:
                self.logger.error("No Rack Deployment data found in GraphQL response")
                return

            self.data = deployment_list[0]
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        self.logger.info(f"Generating topology for rack {self.data.get('name')}")
        dc = self.data.get("pod", {}).get("parent", {})
        design = dc.get("design_pattern", {})
        pod = self.data.get("pod", {})
        pod_name = pod.get("name", "").lower()
        fabric_name = dc.get("name", "").lower()
        indexes: list[int] = [
            dc.get("index", 1),
            pod.get("index", 1),
            self.data.get("index", 1),
        ]

        for template in self.data.get("fabric_templates", []):
            # Skip OOB and console devices for cabling
            role = template.get("role", "")
            if template.get("role", "") in ["oob", "console"]:
                await self.create_devices(
                    deployment_id=pod.get("id"),
                    device_role=role,
                    amount=template.get("quantity", 1),
                    template=template.get("template", {}),
                    naming_convention=design.get("naming_convention", "standard").lower(),
                    options={
                        "pod_name": pod_name,
                        "fabric_name": dc.get("name", "").lower(),
                        "indexes": indexes,
                        "allocate_loopback": False,
                    },
                )
            else:
                # Create leaf devices and collect them for cabling
                leafs = await self.create_devices(
                    deployment_id=pod.get("id"),
                    device_role=role,
                    amount=template.get("quantity", 1),
                    template=template.get("template", {}),
                    naming_convention=design.get("naming_convention", "standard").lower(),
                    options={
                        "pod_name": pod_name,
                        "fabric_name": fabric_name,
                        "indexes": indexes,
                        "allocate_loopback": True,
                        "rack": self.data.get("id", ""),
                    },
                )

                leaf_interfaces = [
                    interface.get("name")
                    for interface in (
                        template.get("template", {}).get("interfaces") or []
                    )
                ]
                spine_devices = [
                    device.get("name") for device in (pod.get("devices") or [])
                ]
                spine_interfaces = [
                    interface.get("name")
                    for interface in (
                        pod.get("spine_template", {}).get("interfaces") or []
                    )
                ]

                await self.create_cabling(
                    bottom_devices=leafs,
                    bottom_interfaces=leaf_interfaces,
                    top_devices=spine_devices,
                    top_interfaces=spine_interfaces,
                    strategy="rack",
                    options={
                        "cabling_offset": (
                            (self.data.get("index", 1) - 1)
                            * design.get("maximum_rack_leafs", 2)
                        ),
                        "top_sorting": pod.get(
                            "spine_interface_sorting_method", "bottom_up"
                        ),
                        "bottom_sorting": pod.get(
                            "spine_interface_sorting_method", "bottom_up"
                        ),
                        "pool": f"{pod_name}-technical-pool",
                    },
                )
