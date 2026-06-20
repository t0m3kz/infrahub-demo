from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader

# Mapping for fields
RACK_KIND: str = "LocationRack"
RACK_NAME: str = "name"
RACK_HEIGHT: str = "height"

DEVICE_RELATIONSHIP: str = "devices"
DEVICE_POSITION: str = "position"
DEVICE_NAME: str = "name"
DEVICE_RACK_FACE: str = "rack_face"
DEVICE_COLOR_ATTRIBUTE: str = "status"

DEVICE_TYPE_RELATIONSHIP: str = "device_type"
DEVICE_TYPE_HEIGHT: str = "height"
DEVICE_TYPE_FULL_DEPTH: str = "full_depth"


# Template path (relative to repository root)
TEMPLATE_DIR: str = "templates"

# SVG parameters
U_HEIGHT: int = 20
COLUMN_WIDTH: int = 250
LABEL_COLUMN_WIDTH: int = 50
HORIZONTAL_PADDING: int = 100
VERTICAL_HORIZONTAL_PADDING: int = 50


class RackElevation(InfrahubTransform):
    query = "rack_elevation_query"

    async def transform(self, data: dict) -> str:
        # Get rack related informations

        rack_dict: dict = data[RACK_KIND]["edges"][0]["node"]
        rack_name: str = rack_dict[RACK_NAME]["value"]
        rack_height: int = rack_dict[RACK_HEIGHT]["value"]

        devices: list[dict] = []
        for device in rack_dict[DEVICE_RELATIONSHIP]["edges"]:
            device = device["node"]

            # Skip devices without position or device type
            if not device[DEVICE_POSITION]["value"]:
                continue

            if not device[DEVICE_TYPE_RELATIONSHIP]["node"]:
                continue

            device_type: dict = device[DEVICE_TYPE_RELATIONSHIP]["node"]

            # Add device to list
            devices.append(
                {
                    "name": device[DEVICE_NAME]["value"],
                    "position": device[DEVICE_POSITION]["value"],
                    "rack_face": device[DEVICE_RACK_FACE]["value"],
                    "color": device[DEVICE_COLOR_ATTRIBUTE]["color"],
                    "height": device_type[DEVICE_TYPE_HEIGHT]["value"],
                    "device_type": device_type["display_label"],
                    "is_full_depth": device_type[DEVICE_TYPE_FULL_DEPTH]["value"],
                }
            )

        return self.generate_svg(rack_name, rack_height, devices)

    def generate_svg(self, rack_name: str, rack_height: int, devices: list[dict]) -> str:
        # Calculate dimensions and positions on an x, y graph
        total_width: int = HORIZONTAL_PADDING + COLUMN_WIDTH + LABEL_COLUMN_WIDTH + COLUMN_WIDTH + HORIZONTAL_PADDING
        total_height: int = VERTICAL_HORIZONTAL_PADDING + (rack_height * U_HEIGHT) + VERTICAL_HORIZONTAL_PADDING
        front_x: int = HORIZONTAL_PADDING
        label_x: int = HORIZONTAL_PADDING + COLUMN_WIDTH
        rear_x: int = HORIZONTAL_PADDING + COLUMN_WIDTH + LABEL_COLUMN_WIDTH
        rack_top_y: int = VERTICAL_HORIZONTAL_PADDING
        label_center_x: int = label_x + LABEL_COLUMN_WIDTH // 2

        # Calculate device positions and sizes
        for device in devices:
            y_size: int = device["height"] * U_HEIGHT
            y_position: int = rack_top_y + (rack_height - device["position"] - device["height"] + 1) * U_HEIGHT
            connector_y_size: int = min(14, y_size - 6)

            device["y_position"] = y_position
            device["y_size"] = y_size
            device["connector_y_size"] = connector_y_size
            device["connector_y_position"] = y_position + (y_size - connector_y_size) / 2

        # Render SVG using Jinja2 template
        env = Environment(
            loader=FileSystemLoader(f"{self.root_directory}/{TEMPLATE_DIR}"),
            autoescape=False,
        )

        template = env.get_template("rack.j2")

        return template.render(
            rack_name=rack_name,
            rack_height=rack_height,
            total_width=total_width,
            total_height=total_height,
            front_x=front_x,
            rear_x=rear_x,
            label_center_x=label_center_x,
            label_column_width=LABEL_COLUMN_WIDTH,
            column_width=COLUMN_WIDTH,
            u_height=U_HEIGHT,
            title_height=VERTICAL_HORIZONTAL_PADDING,
            rack_top_y=rack_top_y,
            devices=devices,
        )
