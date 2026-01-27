// End-to-end path discovery: DC1 → Equinix → AWS
// Updated to include cables, unified connectivity relationships, and service bindings
// Shows complete stack: Cable → Circuit → Service → Virtual Link → Service

// =============================================================================
// Query 1: Complete Path with All Layers (Cable → Circuit → Virtual Link)
// =============================================================================
// This query traverses the full connectivity stack including physical cables
MATCH path = (dc:Node {kind: "TopologyDataCenter", name: "DC1"})
  -[:HAS_ATTRIBUTE__CONNECTIVITY*1..10]-
  (cloud:Node {kind: "TopologyCloudRegion", name: "eu-central-1"})
RETURN path
ORDER BY length(path)
LIMIT 10;

// =============================================================================
// Query 2: Physical Layer - Show Cables Between Devices
// =============================================================================
// Find all physical cables and their endpoints
MATCH (cable:Node {kind: "DcimCable"})
OPTIONAL MATCH (cable)-[:HAS_ATTRIBUTE__ENDPOINTS]->(endpoint:Node {kind: "DcimPhysicalInterface"})
OPTIONAL MATCH (endpoint)-[:IS_RELATED__DEVICE]->(device:Node {kind: "DcimPhysicalDevice"})
OPTIONAL MATCH (cable)-[:IS_RELATED__DEPLOYMENT]->(deployment:Node)
WHERE deployment.kind IN ["TopologyDataCenter", "TopologyColocationZone"]
RETURN cable.name AS CableName,
       cable.type AS CableType,
       deployment.name AS DeploymentScope,
       collect(DISTINCT device.name) AS ConnectedDevices,
       collect(DISTINCT endpoint.name) AS ConnectedInterfaces
ORDER BY cable.name;

// =============================================================================
// Query 3: Circuit Layer - Show Circuits with Optional Cable Underlays
// =============================================================================
// Find all circuits (with or without physical cables)
MATCH (deployment:Node)
  WHERE deployment.kind IN ["TopologyDataCenter", "TopologyColocationZone", "TopologyCloudRegion"]
MATCH (deployment)-[:HAS_ATTRIBUTE__CONNECTIVITY]->(circuit:Node {kind: "TopologyCircuit"})
OPTIONAL MATCH (circuit)-[:HAS_ATTRIBUTE__A_SIDE_INTERFACE]->(a_iface:Node)-[:HAS_ATTRIBUTE__CABLE]->(cable:Node {kind: "DcimCable"})
OPTIONAL MATCH (circuit)-[:HAS_ATTRIBUTE__SOURCE_DEPLOYMENT]->(src:Node)
OPTIONAL MATCH (circuit)-[:HAS_ATTRIBUTE__DESTINATION_DEPLOYMENT]->(dst:Node)
RETURN circuit.circuit_id AS CircuitID,
       circuit.circuit_type AS CircuitType,
       circuit.bandwidth AS Bandwidth,
       circuit.status AS Status,
       deployment.name AS DeploymentScope,
       coalesce(src.name, "N/A") AS SourceDeployment,
       coalesce(dst.name, "N/A") AS DestDeployment,
       CASE WHEN cable IS NOT NULL THEN "✓ Has Cable" ELSE "○ Service Only" END AS PhysicalLayer
ORDER BY circuit.circuit_id;

// =============================================================================
// Query 4: Service Bindings - Show Interface-Level Services
// =============================================================================
// Find all circuit and virtual link services bound to interfaces
MATCH (device:Node {kind: "DcimPhysicalDevice"})
  -[:IS_RELATED__INTERFACES]->(iface:Node {kind: "DcimPhysicalInterface"})
  -[:IS_RELATED__INTERFACE_SERVICES]->(service:Node)
WHERE service.kind IN ["ManagedCircuitService", "ManagedVirtualLinkService"]
OPTIONAL MATCH (service)-[:IS_RELATED__CIRCUIT]->(circuit:Node {kind: "TopologyCircuit"})
OPTIONAL MATCH (service)-[:IS_RELATED__VIRTUAL_LINK]->(vlink:Node {kind: "TopologyVirtualLink"})
RETURN device.name AS Device,
       iface.name AS Interface,
       service.kind AS ServiceType,
       CASE service.kind
         WHEN "ManagedCircuitService" THEN service.side
         WHEN "ManagedVirtualLinkService" THEN service.endpoint_type
         ELSE "N/A"
       END AS ServiceRole,
       coalesce(circuit.circuit_id, vlink.name, "N/A") AS BoundTo,
       service.status AS Status
ORDER BY device.name, iface.name;

// =============================================================================
// Query 5: Virtual Links with Underlay Circuits
// =============================================================================
// Show virtual links and their physical underlay circuits
MATCH (deployment:Node)
  WHERE deployment.kind IN ["TopologyDataCenter", "TopologyColocationZone", "TopologyCloudRegion"]
MATCH (deployment)-[:HAS_ATTRIBUTE__CONNECTIVITY]->(vlink:Node {kind: "TopologyVirtualLink"})
OPTIONAL MATCH (vlink)-[:HAS_ATTRIBUTE__UNDERLAY_CIRCUIT]->(circuit:Node {kind: "TopologyCircuit"})
OPTIONAL MATCH (vlink)-[:HAS_ATTRIBUTE__SOURCE_DEPLOYMENT]->(src:Node)
OPTIONAL MATCH (vlink)-[:HAS_ATTRIBUTE__DESTINATION_DEPLOYMENT]->(dst:Node)
RETURN vlink.name AS VirtualLink,
       vlink.link_type AS LinkType,
       vlink.bandwidth AS Bandwidth,
       vlink.encryption AS Encrypted,
       deployment.name AS DeploymentScope,
       coalesce(src.name, "N/A") AS SourceDeployment,
       coalesce(dst.name, "N/A") AS DestDeployment,
       CASE WHEN circuit IS NOT NULL THEN circuit.circuit_id ELSE "No Underlay" END AS UnderlayCircuit
ORDER BY vlink.name;

// =============================================================================
// Query 6: Complete Connectivity Stack - All Layers
// =============================================================================
// Shows full stack from cables to virtual links for a specific deployment
MATCH (deployment:Node {kind: "TopologyDataCenter", name: "DC1"})
OPTIONAL MATCH (deployment)-[:IS_RELATED__CABLES]->(cable:Node {kind: "DcimCable"})
OPTIONAL MATCH (deployment)-[:HAS_ATTRIBUTE__CONNECTIVITY]->(conn:Node)
  WHERE conn.kind IN ["TopologyCircuit", "TopologyVirtualLink"]
WITH deployment,
     count(DISTINCT cable) AS cable_count,
     count(DISTINCT CASE WHEN conn.kind = "TopologyCircuit" THEN conn END) AS circuit_count,
     count(DISTINCT CASE WHEN conn.kind = "TopologyVirtualLink" THEN conn END) AS vlink_count
RETURN deployment.name AS Deployment,
       cable_count AS PhysicalCables,
       circuit_count AS Circuits,
       vlink_count AS VirtualLinks,
       cable_count + circuit_count + vlink_count AS TotalConnectivity;

// =============================================================================
// Query 7: End-to-End Path with All Hops
// =============================================================================
// Detailed path showing every layer from DC to Cloud
MATCH path = (dc:Node {kind: "TopologyDataCenter", name: "DC1"})
  -[*1..15]-
  (cloud:Node {kind: "TopologyCloudRegion", name: "eu-central-1"})
WHERE ALL(node IN nodes(path) WHERE node.kind IN [
  "TopologyDataCenter", "TopologyColocationZone", "TopologyCloudRegion",
  "TopologyCircuit", "TopologyVirtualLink",
  "DcimCable", "DcimPhysicalInterface",
  "ManagedCircuitService", "ManagedVirtualLinkService"
])
WITH path,
     [node IN nodes(path) | node.kind + ": " + coalesce(node.name, node.circuit_id, "unnamed")] AS hop_details,
     length(path) AS hop_count
RETURN hop_count AS Hops,
       hop_details AS PathDetails
ORDER BY hop_count
LIMIT 5;
