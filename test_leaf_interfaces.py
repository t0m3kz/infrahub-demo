import asyncio

from infrahub_sdk import InfrahubClient


async def main():
    client = InfrahubClient()

    # Test 1: Query interfaces with cable__isnull filter
    query1 = """
    query GetLeafInterfaces($leaf_name: String!, $role: String!) {
        DcimPhysicalInterface(
            device__name__value: $leaf_name,
            role__value: $role,
            cable__isnull: true
        ) {
            edges {
                node {
                    name { value }
                }
            }
        }
    }
    """

    print("Test 1: Query with cable__isnull filter")
    try:
        result = await client.execute_graphql(
            query=query1,
            branch_name="dc8-complete-1761726763",
            variables={"leaf_name": "dc-8-pod-d1-leaf-01", "role": "customer"},
        )
        if result.get("errors"):
            print(f"  Error: {result['errors']}")
        else:
            count = len(result.get("DcimPhysicalInterface", {}).get("edges", []))
            print(f"  Found {count} interfaces")
    except Exception as e:
        print(f"  Exception: {e}")

    # Test 2: Query all interfaces on leaf (no cable filter)
    query2 = """
    query GetLeafInterfaces($leaf_name: String!, $role: String!) {
        DcimPhysicalInterface(
            device__name__value: $leaf_name,
            role__value: $role
        ) {
            edges {
                node {
                    name { value }
                }
            }
        }
    }
    """

    print("\nTest 2: Query without cable filter")
    try:
        result = await client.execute_graphql(
            query=query2,
            branch_name="dc8-complete-1761726763",
            variables={"leaf_name": "dc-8-pod-d1-leaf-01", "role": "customer"},
        )
        if result.get("errors"):
            print(f"  Error: {result['errors']}")
        else:
            edges = result.get("DcimPhysicalInterface", {}).get("edges", [])
            print(f"  Found {len(edges)} interfaces")
            for edge in edges[:3]:
                name = edge.get("node", {}).get("name", {}).get("value", "")
                print(f"    - {name}")
    except Exception as e:
        print(f"  Exception: {e}")


asyncio.run(main())
