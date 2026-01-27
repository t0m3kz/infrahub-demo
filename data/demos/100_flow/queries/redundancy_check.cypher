// Redundancy check: Verify multiple paths exist between locations
// Updated to include cables, unified connectivity, and service bindings
// Ensures high availability with backup paths at every layer

// =============================================================================
// Query 1: Physical Cable Redundancy
// =============================================================================
// Check if physical cables have redundant paths
MATCH (dc:Node {kind: "TopologyDataCenter", name: "DC1"})-[:IS_RELATED__CABLES]->(cable:Node {kind: "DcimCable"})
OPTIONAL MATCH (cable)-[:HAS_ATTRIBUTE__ENDPOINTS]->(iface:Node {kind: "DcimPhysicalInterface"})
OPTIONAL MATCH (iface)-[:IS_RELATED__DEVICE]->(device:Node {kind: "DcimPhysicalDevice"})
WITH dc, count(DISTINCT cable) AS cable_count,
     collect(DISTINCT device.name) AS connected_devices
RETURN dc.name AS DataCenter,
       cable_count AS PhysicalCables,
       connected_devices AS ConnectedDevices,
       CASE
         WHEN cable_count >= 4 THEN "✓✓ Highly redundant (4+ cables)"
         WHEN cable_count >= 2 THEN "✓ Redundant (2+ cables)"
         WHEN cable_count = 1 THEN "⚠ Single cable - no redundancy"
         ELSE "✗ No cables"
       END AS CableRedundancy;

// =============================================================================
// Query 2: Circuit Redundancy Matrix
// =============================================================================
// Check redundant circuits between each deployment pair
MATCH (source:Node)
  WHERE source.kind IN ["TopologyDataCenter", "TopologyColocationZone"]
MATCH (dest:Node)
  WHERE dest.kind IN ["TopologyColocationZone", "TopologyCloudRegion"]
  AND source <> dest
OPTIONAL MATCH (source)-[:HAS_ATTRIBUTE__CONNECTIVITY]->(circuit:Node {kind: "TopologyCircuit"})
  -[:HAS_ATTRIBUTE__DESTINATION_DEPLOYMENT]->(dest)
WHERE circuit.status = "active"
WITH source, dest, count(DISTINCT circuit) AS circuit_count,
     collect(DISTINCT circuit.circuit_id) AS circuit_ids
RETURN source.name AS Source,
       dest.name AS Destination,
       circuit_count AS ActiveCircuits,
       circuit_ids AS CircuitIDs,
       CASE
         WHEN circuit_count = 0 THEN "✗ No connectivity"
         WHEN circuit_count = 1 THEN "⚠ Single circuit - no redundancy"
         WHEN circuit_count = 2 THEN "✓ Dual circuit (N+1)"
         ELSE "✓✓ Highly redundant (N+2 or more)"
       END AS RedundancyStatus
ORDER BY circuit_count DESC, source.name;

// =============================================================================
// Query 3: Virtual Link Redundancy for Cloud Connections
// =============================================================================
// Check redundant Direct Connect or VPN connections to cloud
MATCH (colo:Node)
  WHERE colo.kind = "TopologyColocationZone"
MATCH (cloud:Node {kind: "TopologyCloudRegion"})
OPTIONAL MATCH (colo)-[:HAS_ATTRIBUTE__CONNECTIVITY]->(vlink:Node {kind: "TopologyVirtualLink"})
  -[:HAS_ATTRIBUTE__DESTINATION_DEPLOYMENT]->(cloud)
WHERE vlink.link_type IN ["direct_connect_aws", "express_route_azure", "vpn_ipsec"]
WITH colo, cloud, vlink.link_type AS link_type,
     count(DISTINCT vlink) AS link_count,
     collect(DISTINCT vlink.name) AS link_names
RETURN colo.name AS Colocation,
       cloud.name AS CloudRegion,
       link_type AS LinkType,
       link_count AS ActiveLinks,
       link_names AS LinkNames,
       CASE
         WHEN link_count >= 2 THEN "✓ Redundant"
         WHEN link_count = 1 THEN "⚠ Single link"
         ELSE "✗ No links"
       END AS RedundancyStatus
ORDER BY link_count DESC;

// =============================================================================
// Query 4: Full Path Redundancy (End-to-End)
// =============================================================================
// Count distinct paths from DC to Cloud with circuit details
MATCH path = (dc:Node {kind: "TopologyDataCenter", name: "DC1"})
  -[:HAS_ATTRIBUTE__CONNECTIVITY*1..10]-
  (cloud:Node {kind: "TopologyCloudRegion", name: "eu-central-1"})
WITH path,
     [node IN nodes(path) WHERE node.kind IN ["TopologyCircuit", "TopologyVirtualLink"] | node.kind + ": " + coalesce(node.circuit_id, node.name)] AS path_components
WITH count(DISTINCT path) AS path_count,
     collect(DISTINCT path_components) AS all_paths
RETURN path_count AS TotalPaths,
       CASE
         WHEN path_count >= 4 THEN "✓✓ Highly redundant (4+ paths)"
         WHEN path_count >= 2 THEN "✓ Redundant (2+ paths)"
         WHEN path_count = 1 THEN "⚠ Single path - no redundancy"
         ELSE "✗ No paths"
       END AS RedundancyStatus,
       all_paths AS PathDetails;

// =============================================================================
// Query 5: Check Failed/Down Components
// =============================================================================
// Identify any circuits or links not in active status
MATCH (conn:Node)
  WHERE conn.kind IN ["TopologyCircuit", "TopologyVirtualLink"]
  AND conn.status <> "active"
OPTIONAL MATCH (conn)-[:HAS_ATTRIBUTE__SOURCE_DEPLOYMENT]->(src:Node)
OPTIONAL MATCH (conn)-[:HAS_ATTRIBUTE__DESTINATION_DEPLOYMENT]->(dst:Node)
RETURN conn.kind AS ComponentType,
       coalesce(conn.circuit_id, conn.name) AS Identifier,
       conn.status AS Status,
       coalesce(src.name, "N/A") AS Source,
       coalesce(dst.name, "N/A") AS Destination,
       "⚠ Component not active - verify backup paths" AS Alert
ORDER BY conn.kind, Status;

// =============================================================================
// Query 6: Service Binding Redundancy
// =============================================================================
// Check redundant service bindings per interface
MATCH (device:Node {kind: "DcimPhysicalDevice"})
  -[:IS_RELATED__INTERFACES]->(iface:Node {kind: "DcimPhysicalInterface"})
  -[:IS_RELATED__INTERFACE_SERVICES]->(service:Node)
WHERE service.kind IN ["ManagedCircuitService", "ManagedVirtualLinkService"]
WITH device, count(DISTINCT service) AS service_count,
     count(DISTINCT CASE WHEN service.kind = "ManagedCircuitService" THEN service END) AS circuit_services,
     count(DISTINCT CASE WHEN service.kind = "ManagedVirtualLinkService" THEN service END) AS vlink_services
RETURN device.name AS Device,
       service_count AS TotalServices,
       circuit_services AS CircuitServices,
       vlink_services AS VirtualLinkServices,
       CASE
         WHEN service_count >= 4 THEN "✓✓ Highly loaded"
         WHEN service_count >= 2 THEN "✓ Normal"
         WHEN service_count = 1 THEN "○ Low utilization"
         ELSE "⚠ No services"
       END AS UtilizationStatus
ORDER BY service_count DESC;

// =============================================================================
// Query 7: Single Point of Failure Detection
// =============================================================================
// Find deployments with only one connection to another deployment
MATCH (source:Node)
  WHERE source.kind IN ["TopologyDataCenter", "TopologyColocationZone", "TopologyCloudRegion"]
MATCH (dest:Node)
  WHERE dest.kind IN ["TopologyDataCenter", "TopologyColocationZone", "TopologyCloudRegion"]
  AND source <> dest
OPTIONAL MATCH (source)-[:HAS_ATTRIBUTE__CONNECTIVITY]->(conn:Node)-[:HAS_ATTRIBUTE__DESTINATION_DEPLOYMENT]->(dest)
WHERE conn.kind IN ["TopologyCircuit", "TopologyVirtualLink"]
WITH source, dest, count(DISTINCT conn) AS conn_count
WHERE conn_count = 1
RETURN source.name AS Source,
       dest.name AS Destination,
       conn_count AS Connections,
       "⚠ SPOF: Single connection - consider adding redundancy" AS Alert
ORDER BY source.name;

// =============================================================================
// Query 8: Full Redundancy Report Summary
// =============================================================================
// Comprehensive redundancy status across all layers
MATCH (dc:Node {kind: "TopologyDataCenter", name: "DC1"})
OPTIONAL MATCH (dc)-[:IS_RELATED__CABLES]->(cable:Node {kind: "DcimCable"})
OPTIONAL MATCH (dc)-[:HAS_ATTRIBUTE__CONNECTIVITY]->(circuit:Node {kind: "TopologyCircuit"})
OPTIONAL MATCH (dc)-[:HAS_ATTRIBUTE__CONNECTIVITY]->(vlink:Node {kind: "TopologyVirtualLink"})
WITH dc,
     count(DISTINCT cable) AS cables,
     count(DISTINCT circuit) AS circuits,
     count(DISTINCT vlink) AS vlinks
RETURN dc.name AS Deployment,
       cables AS PhysicalCables,
       circuits AS Circuits,
       vlinks AS VirtualLinks,
       CASE
         WHEN cables >= 4 AND circuits >= 4 AND vlinks >= 4 THEN "✓✓ Highly redundant across all layers"
         WHEN cables >= 2 AND circuits >= 2 AND vlinks >= 2 THEN "✓ Redundant across all layers"
         WHEN cables = 0 OR circuits = 0 OR vlinks = 0 THEN "⚠ Missing connectivity in some layers"
         ELSE "○ Partial redundancy"
       END AS OverallRedundancy;
