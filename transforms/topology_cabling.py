from infrahub_sdk.transforms import InfrahubTransform


class TopologyCabling(InfrahubTransform):
    query = "topology_cabling"

    async def transform(self, data: dict) -> str:
        # Create a list to hold CSV rows
        csv_rows = []

        # Add CSV header
        csv_rows.append("Source Device,Source Interface,Remote Device,Remote Interface")

        seen_connections = set()  # Track connections we've already processed

        for device in data["TopologyDeployment"]["edges"][0]["node"]["devices"][
            "edges"
        ]:
            source_device = device["node"]["name"]["value"]

            for interface in device["node"]["interfaces"]["edges"]:
                connected = interface["node"].get("connector", {}).get("node")
                if not connected:
                    continue

                source_interface = interface["node"]["name"]["value"]
                remote_device, remote_interface = connected["hfid"]

                # Create a unique identifier for this connection (sorted to handle duplicates)
                connection_key = tuple(
                    sorted(
                        [
                            (source_device, source_interface),
                            (remote_device, remote_interface),
                        ]
                    )
                )

                # Skip if we've seen this connection already
                if connection_key in seen_connections:
                    continue

                # Add to our tracking set
                seen_connections.add(connection_key)

                # Format this row and add to our list
                # Escape any commas in field values with quotes
                row = [source_device, source_interface, remote_device, remote_interface]
                escaped_row = [f'"{field}"' if "," in field else field for field in row]
                csv_rows.append(",".join(escaped_row))

        # Join all rows with newlines to create CSV string
        csv_data = "\n".join(csv_rows)

        return csv_data
