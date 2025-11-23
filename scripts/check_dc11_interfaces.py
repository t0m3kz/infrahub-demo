#!/usr/bin/env python3
"""Quick script to check interface roles and availability for DC11 Pod 1 racks."""

from infrahub_sdk import InfrahubClientSync

client = InfrahubClientSync()

# Query to get all interface roles in use
query = """
query GetInterfaceRoles {
    DcimInterface {
        edges {
            node {
                role { value }
            }
        }
    }
}
"""

print("=" * 80)
print("Checking Interface Roles")
print("=" * 80)

result = client.execute_graphql(query=query, branch_name="dc11")
interfaces = result.get("DcimInterface", {}).get("edges", [])

# Collect unique roles
roles = set()
for iface in interfaces:
    role = iface.get("node", {}).get("role", {}).get("value")
    if role:
        roles.add(role)

print(f"\nUnique interface roles found: {sorted(roles)}")

# Now check leaf devices and their interfaces
leaf_query = """
query GetLeafInterfaces {
    DcimGenericDevice(role__value: "leaf") {
        edges {
            node {
                name { value }
                ... on DcimPhysicalDevice {
                    rack {
                        node {
                            name { value }
                            row { value }
                        }
                    }
                }
                interfaces {
                    count
                    edges {
                        node {
                            name { value }
                            role { value }
                            ... on DcimPhysicalInterface {
                                cable {
                                    node { id }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
"""

print("\n" + "=" * 80)
print("Checking Leaf Devices and Interfaces")
print("=" * 80)

result = client.execute_graphql(query=leaf_query, branch_name="dc11")
leafs = result.get("DcimGenericDevice", {}).get("edges", [])

if not leafs:
    print("\n⚠️  No leaf devices found! Generate network racks first.")
else:
    for leaf_edge in leafs:
        leaf = leaf_edge["node"]
        leaf_name = leaf["name"]["value"]
        rack = leaf.get("rack", {}).get("node", {})
        rack_name = rack.get("name", {}).get("value", "Unknown")
        row = rack.get("row", {}).get("value", "Unknown")

        print(f"\n📍 Leaf: {leaf_name}")
        print(f"   Rack: {rack_name} (Row {row})")

        interfaces = leaf.get("interfaces", {}).get("edges", [])

        # Group by role
        by_role = {}
        for iface_edge in interfaces:
            iface = iface_edge["node"]
            role = iface.get("role", {}).get("value", "no-role")
            if role not in by_role:
                by_role[role] = {"total": 0, "available": 0, "used": 0}

            by_role[role]["total"] += 1

            cable = iface.get("cable")
            if (
                cable is not None
                and isinstance(cable, dict)
                and cable.get("node", {}).get("id")
            ):
                by_role[role]["used"] += 1
            else:
                by_role[role]["available"] += 1

        print("   Interfaces by role:")
        for role, counts in sorted(by_role.items()):
            print(
                f"     - {role}: {counts['total']} total ({counts['available']} available, {counts['used']} used)"
            )

# Check ToR devices
tor_query = """
query GetTorInterfaces {
    DcimGenericDevice(role__value: "tor") {
        edges {
            node {
                name { value }
                ... on DcimPhysicalDevice {
                    rack {
                        node {
                            name { value }
                            row { value }
                        }
                    }
                }
                interfaces {
                    count
                    edges {
                        node {
                            name { value }
                            role { value }
                            ... on DcimPhysicalInterface {
                                cable {
                                    node { id }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
"""

print("\n" + "=" * 80)
print("Checking ToR Devices and Interfaces")
print("=" * 80)

result = client.execute_graphql(query=tor_query, branch_name="dc11")
tors = result.get("DcimGenericDevice", {}).get("edges", [])

if not tors:
    print("\n⚠️  No ToR devices found!")
else:
    for tor_edge in tors:
        tor = tor_edge["node"]
        tor_name = tor["name"]["value"]
        rack = tor.get("rack", {}).get("node", {})
        rack_name = rack.get("name", {}).get("value", "Unknown")
        row = rack.get("row", {}).get("value", "Unknown")

        print(f"\n📍 ToR: {tor_name}")
        print(f"   Rack: {rack_name} (Row {row})")

        interfaces = tor.get("interfaces", {}).get("edges", [])

        # Group by role
        by_role = {}
        for iface_edge in interfaces:
            iface = iface_edge["node"]
            role = iface.get("role", {}).get("value", "no-role")
            if role not in by_role:
                by_role[role] = {"total": 0, "available": 0, "used": 0}

            by_role[role]["total"] += 1

            cable = iface.get("cable")
            if (
                cable is not None
                and isinstance(cable, dict)
                and cable.get("node", {}).get("id")
            ):
                by_role[role]["used"] += 1
            else:
                by_role[role]["available"] += 1

        print("   Interfaces by role:")
        for role, counts in sorted(by_role.items()):
            print(
                f"     - {role}: {counts['total']} total ({counts['available']} available, {counts['used']} used)"
            )

print("\n" + "=" * 80)
print("Summary")
print("=" * 80)
print(f"Leaf devices: {len(leafs)}")
print(f"ToR devices: {len(tors)}")
print(f"Interface roles in use: {', '.join(sorted(roles))}")
print("\n✅ Query complete!")
