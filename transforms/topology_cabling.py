import csv
from io import StringIO
from infrahub_sdk.transforms import InfrahubTransform


class TopologyCabling(InfrahubTransform):
    query = "topology_cabling"

    async def transform(self, data):
        # Create a StringIO object to hold CSV data
        csv_output = StringIO()
        csv_writer = csv.writer(csv_output)
        
        # Write CSV header
        csv_writer.writerow(['Source Device', 'Source Interface', 'Remote Device', 'Remote Interface'])
        
        seen_connections = set()  # Track connections we've already processed
        
        for device in data["TopologyDeployment"]["edges"][0]["node"]["devices"]["edges"]:
            source_device = device['node']['name']['value']
            
            for interface in device["node"]["interfaces"]["edges"]:
                connected = interface['node'].get('connector', {}).get('node')
                if not connected:
                    continue
                    
                source_interface = interface['node']['name']['value']
                remote_device, remote_interface = connected['hfid']
                
                # Create a unique identifier for this connection (sorted to handle duplicates)
                connection_key = tuple(sorted([
                    (source_device, source_interface),
                    (remote_device, remote_interface)
                ]))
                
                # Skip if we've seen this connection already
                if connection_key in seen_connections:
                    continue
                    
                # Add to our tracking set
                seen_connections.add(connection_key)
                
                # Write this row to CSV
                csv_writer.writerow([source_device, source_interface, remote_device, remote_interface])

        # Get the CSV data as a string
        csv_data = csv_output.getvalue()
        csv_output.close()
        
        return csv_data