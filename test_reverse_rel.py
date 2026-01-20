#!/usr/bin/env python3
import asyncio
from infrahub_sdk import InfrahubClient

async def test_reverse_relationship():
    client = InfrahubClient()
    
    query = """
    query {
      ServiceLoadBalancerVIP(hostname__value: "www.demo.local", port__value: 443) {
        edges {
          node {
            hostname { value }
            port { value }
            backend_pool {
              node {
                name { value }
              }
            }
          }
        }
      }
    }
    """
    
    try:
        result = await client.execute_graphql(query=query, branch_name='test')
        print('✅ Reverse relationship works!')
        vip = result['ServiceLoadBalancerVIP']['edges'][0]['node']
        print(f"VIP: {vip['hostname']['value']}:{vip['port']['value']}")
        if vip.get('backend_pool'):
            pool = vip['backend_pool']['node']
            print(f"Backend Pool: {pool['name']['value']}")
        else:
            print('⚠️  No backend_pool field in response')
    except Exception as e:
        print(f'❌ Error: {e}')

asyncio.run(test_reverse_relationship())
