// ============================================================================
// OPERATIONAL END-TO-END CONNECTIVITY REPORT
// ============================================================================
// Complete visibility: devices, owners, circuits, costs, SLAs, security
// Use this for: troubleshooting, cost allocation, compliance, planning

// ============================================================================
// Query 1: Complete Path Inventory with Ownership and Contact Info
// ============================================================================
// Shows EVERYTHING: devices, circuits, virtual links, providers, owners
MATCH path = (dc:Node {kind: "TopologyDataCenter", name: "DC1"})
  -[:HAS_ATTRIBUTE__CONNECTIVITY*1..10]-
  (cloud:Node {kind: "TopologyCloudRegion", name: "eu-central-1"})
WITH path,
     [node IN nodes(path) WHERE node.kind IN ["TopologyCircuit", "TopologyVirtualLink"] | node] AS connectivity_nodes,
     [node IN nodes(path) WHERE node.kind IN ["TopologyDataCenter", "TopologyColocationZone", "TopologyCloudRegion"] | node] AS deployment_nodes
UNWIND connectivity_nodes AS conn
OPTIONAL MATCH (conn)-[:IS_RELATED__PROVIDER]->(provider:Node {kind: "OrganizationProvider"})
OPTIONAL MATCH (conn)-[:HAS_ATTRIBUTE__SOURCE_DEPLOYMENT]->(src_deploy:Node)
OPTIONAL MATCH (conn)-[:HAS_ATTRIBUTE__DESTINATION_DEPLOYMENT]->(dst_deploy:Node)
OPTIONAL MATCH (src_deploy)-[:IS_RELATED__LOCATION]->(src_loc:Node)
OPTIONAL MATCH (dst_deploy)-[:IS_RELATED__LOCATION]->(dst_loc:Node)
WITH conn, provider, src_deploy, dst_deploy, src_loc, dst_loc
RETURN
  // Component identification
  conn.kind AS ComponentType,
  coalesce(conn.circuit_id, conn.name) AS Identifier,

  // Ownership and responsibility
  coalesce(provider.name, "Internal") AS Provider,
  CASE
    WHEN provider.name IS NOT NULL THEN "External (Contact Provider)"
    ELSE "Internal (Contact Network Team)"
  END AS Responsibility,

  // Endpoints
  coalesce(src_deploy.name, "N/A") AS SourceDeployment,
  coalesce(dst_deploy.name, "N/A") AS DestinationDeployment,
  coalesce(src_loc.name, "N/A") AS SourceLocation,
  coalesce(dst_loc.name, "N/A") AS DestinationLocation,

  // Technical specs
  conn.bandwidth AS Bandwidth_Mbps,
  coalesce(conn.circuit_type, conn.link_type) AS ConnectionType,
  conn.status AS Status,

  // Security
  CASE
    WHEN conn.encryption = true THEN "✓ Encrypted"
    WHEN conn.link_type IN ["vpn_ipsec"] THEN "✓ Encrypted (VPN)"
    WHEN conn.circuit_type = "dark_fiber" THEN "○ Physical Security"
    ELSE "⚠ No Encryption"
  END AS SecurityStatus

ORDER BY ComponentType, Identifier;

// ============================================================================
// Query 2: Cost and SLA Summary by Provider
// ============================================================================
// Financial and contractual view - who provides what and at what cost
MATCH (conn:Node)
  WHERE conn.kind IN ["TopologyCircuit", "TopologyVirtualLink"]
OPTIONAL MATCH (conn)-[:IS_RELATED__PROVIDER]->(provider:Node {kind: "OrganizationProvider"})
OPTIONAL MATCH (conn)-[:HAS_ATTRIBUTE__SOURCE_DEPLOYMENT]->(src:Node)
OPTIONAL MATCH (conn)-[:HAS_ATTRIBUTE__DESTINATION_DEPLOYMENT]->(dst:Node)
WITH provider.name AS ProviderName,
     conn.kind AS ConnectionType,
     count(DISTINCT conn) AS ConnectionCount,
     sum(conn.bandwidth) AS TotalBandwidth_Mbps,
     collect(DISTINCT {
       id: coalesce(conn.circuit_id, conn.name),
       type: coalesce(conn.circuit_type, conn.link_type),
       bandwidth: conn.bandwidth,
       committed_rate: conn.committed_rate,
       install_date: conn.install_date,
       contract_end_date: conn.contract_end_date,
       source: src.name,
       destination: dst.name
     }) AS Circuits
RETURN
  coalesce(ProviderName, "Internal") AS Provider,
  ConnectionType,
  ConnectionCount AS Connections,
  TotalBandwidth_Mbps AS TotalBandwidth_Mbps,

  // Cost estimation (placeholder - integrate with actual pricing data)
  CASE ProviderName
    WHEN "AWS" THEN ConnectionCount * 50  // $50/connection/month estimate
    WHEN "Equinix" THEN ConnectionCount * 500 // $500/connection/month estimate
    ELSE 0
  END AS EstimatedMonthlyCost_USD,

  Circuits AS CircuitDetails
ORDER BY EstimatedMonthlyCost_USD DESC;

// ============================================================================
// Query 3: Device-Level Path View with Interface Details
// ============================================================================
// Shows actual devices and interfaces involved in the path
MATCH path = (dc:Node {kind: "TopologyDataCenter", name: "DC1"})
  -[*1..15]-
  (cloud:Node {kind: "TopologyCloudRegion", name: "eu-central-1"})
WITH path,
     [node IN nodes(path) WHERE node.kind = "DcimPhysicalDevice" | node] AS devices,
     [node IN nodes(path) WHERE node.kind IN ["TopologyCircuit", "TopologyVirtualLink"] | node] AS connections
UNWIND devices AS device
OPTIONAL MATCH (device)-[:IS_RELATED__INTERFACES]->(iface:Node {kind: "DcimPhysicalInterface"})
  -[:IS_RELATED__INTERFACE_SERVICES]->(service:Node)
WHERE service.kind IN ["ManagedCircuitService", "ManagedVirtualLinkService"]
OPTIONAL MATCH (service)-[:IS_RELATED__CIRCUIT]->(circuit:Node {kind: "TopologyCircuit"})
OPTIONAL MATCH (service)-[:IS_RELATED__VIRTUAL_LINK]->(vlink:Node {kind: "TopologyVirtualLink"})
OPTIONAL MATCH (device)-[:IS_RELATED__LOCATION]->(loc:Node)
OPTIONAL MATCH (device)-[:HAS_ATTRIBUTE__DEVICE_TYPE]->(dtype:Node)
RETURN DISTINCT
  device.name AS Device,
  device.role AS Role,
  coalesce(dtype.name, "Unknown") AS DeviceType,
  coalesce(loc.name, "Unknown") AS Location,
  collect(DISTINCT {
    interface: iface.name,
    service_type: service.kind,
    bound_to: coalesce(circuit.circuit_id, vlink.name),
    status: iface.status
  }) AS Interfaces
ORDER BY Device;

// ============================================================================
// Query 4: Physical Cable and Circuit Mapping
// ============================================================================
// Shows the complete physical layer: cables → circuits → services
MATCH (cable:Node {kind: "DcimCable"})
OPTIONAL MATCH (cable)-[:HAS_ATTRIBUTE__ENDPOINTS]->(endpoint:Node {kind: "DcimPhysicalInterface"})
OPTIONAL MATCH (endpoint)-[:IS_RELATED__DEVICE]->(device:Node {kind: "DcimPhysicalDevice"})
OPTIONAL MATCH (endpoint)-[:IS_RELATED__INTERFACE_SERVICES]->(service:Node {kind: "ManagedCircuitService"})
OPTIONAL MATCH (service)-[:IS_RELATED__CIRCUIT]->(circuit:Node {kind: "TopologyCircuit"})
OPTIONAL MATCH (circuit)-[:IS_RELATED__PROVIDER]->(provider:Node {kind: "OrganizationProvider"})
OPTIONAL MATCH (cable)-[:IS_RELATED__DEPLOYMENT]->(deployment:Node)
WITH cable, circuit, provider, deployment,
     collect(DISTINCT {device: device.name, interface: endpoint.name}) AS endpoints
RETURN
  cable.name AS CableName,
  cable.type AS CableType,
  coalesce(deployment.name, "Unassigned") AS Deployment,
  endpoints AS ConnectedEndpoints,
  coalesce(circuit.circuit_id, "No Circuit") AS CircuitID,
  coalesce(circuit.circuit_type, "N/A") AS CircuitType,
  coalesce(provider.name, "Internal") AS Provider,

  // Ownership chain
  CASE
    WHEN cable.type = "smf" AND circuit.circuit_type = "dark_fiber"
      THEN "✓ Owned Infrastructure"
    WHEN circuit.circuit_type IN ["metro_ethernet", "mpls"]
      THEN "○ Provider Service (Physical cable owned by provider)"
    ELSE "⚠ Check ownership"
  END AS OwnershipStatus
ORDER BY cable.name;

// ============================================================================
// Query 5: Complete Operational Contact List
// ============================================================================
// Who to call when things break - organized by component and provider
MATCH (conn:Node)
  WHERE conn.kind IN ["TopologyCircuit", "TopologyVirtualLink"]
OPTIONAL MATCH (conn)-[:IS_RELATED__PROVIDER]->(provider:Node {kind: "OrganizationProvider"})
OPTIONAL MATCH (conn)-[:HAS_ATTRIBUTE__SOURCE_DEPLOYMENT]->(src:Node)
OPTIONAL MATCH (conn)-[:HAS_ATTRIBUTE__DESTINATION_DEPLOYMENT]->(dst:Node)
WITH provider.name AS ProviderName,
     conn.kind AS ComponentType,
     coalesce(conn.circuit_type, conn.link_type) AS ConnectionType,
     collect(DISTINCT {
       id: coalesce(conn.circuit_id, conn.name),
       source: src.name,
       destination: dst.name,
       status: conn.status
     }) AS Components
RETURN
  coalesce(ProviderName, "Internal Network Team") AS ResponsibleParty,

  // Contact information (placeholder - integrate with actual contact data)
  CASE ProviderName
    WHEN "AWS" THEN "Enterprise Support: 1-800-AWS-HELP"
    WHEN "Equinix" THEN "Customer Care: support@equinix.com"
    WHEN "Zscaler" THEN "Enterprise Support: support@zscaler.com"
    ELSE "Internal: network-ops@company.com"
  END AS ContactInfo,

  // Escalation path
  CASE ProviderName
    WHEN "AWS" THEN "TAM → Support Case → Premium Support"
    WHEN "Equinix" THEN "Customer Portal → Account Manager → NOC"
    ELSE "L1 NOC → L2 Network Engineering → L3 Architecture"
  END AS EscalationPath,

  ComponentType,
  ConnectionType,
  count(Components) AS ComponentCount,
  Components AS ComponentDetails
ORDER BY ResponsibleParty, ComponentType;

// ============================================================================
// Query 6: Security and Compliance View
// ============================================================================
// Data sovereignty, encryption, security zones
MATCH path = (dc:Node {kind: "TopologyDataCenter", name: "DC1"})
  -[:HAS_ATTRIBUTE__CONNECTIVITY*1..10]-
  (cloud:Node {kind: "TopologyCloudRegion"})
WITH path,
     [node IN nodes(path) WHERE node.kind IN ["TopologyCircuit", "TopologyVirtualLink"] | node] AS connections,
     [node IN nodes(path) WHERE node.kind IN ["TopologyDataCenter", "TopologyColocationZone", "TopologyCloudRegion"] | node] AS deployments
UNWIND connections AS conn
OPTIONAL MATCH (conn)-[:HAS_ATTRIBUTE__SOURCE_DEPLOYMENT]->(src:Node)-[:IS_RELATED__LOCATION]->(src_loc:Node)
OPTIONAL MATCH (conn)-[:HAS_ATTRIBUTE__DESTINATION_DEPLOYMENT]->(dst:Node)-[:IS_RELATED__LOCATION]->(dst_loc:Node)
WITH conn, src, dst, src_loc, dst_loc
RETURN
  coalesce(conn.circuit_id, conn.name) AS ConnectionID,
  conn.kind AS Type,

  // Geographic compliance
  coalesce(src_loc.name, "Unknown") AS SourceLocation,
  coalesce(dst_loc.name, "Unknown") AS DestLocation,
  CASE
    WHEN src_loc.name IN ["Frankfurt", "Amsterdam"] AND dst_loc.name IN ["Frankfurt", "Amsterdam", "Dublin"]
      THEN "✓ EU Data Residency Compliant"
    WHEN src_loc.name IN ["Frankfurt", "Amsterdam"] OR dst_loc.name IN ["Frankfurt", "Amsterdam"]
      THEN "⚠ Cross-border (EU → Non-EU or vice versa)"
    ELSE "⚠ Unknown Jurisdiction"
  END AS DataSovereigntyStatus,

  // Encryption compliance
  CASE
    WHEN conn.encryption = true THEN "✓ Encrypted in transit"
    WHEN conn.link_type = "vpn_ipsec" THEN "✓ IPsec encrypted"
    WHEN conn.circuit_type = "dark_fiber" THEN "○ Physical security (consider MACSec)"
    ELSE "✗ No encryption - evaluate risk"
  END AS EncryptionStatus,

  // Connection type security posture
  CASE
    WHEN conn.circuit_type IN ["dark_fiber", "cross_connect"] THEN "Private (Dedicated)"
    WHEN conn.circuit_type IN ["metro_ethernet", "mpls"] THEN "Shared Infrastructure (Provider Isolation)"
    WHEN conn.circuit_type = "dia" THEN "Public Internet (VPN Required)"
    WHEN conn.link_type IN ["direct_connect_aws"] THEN "Private (Dedicated to Cloud)"
    ELSE "Unknown"
  END AS NetworkSegmentation
ORDER BY DataSovereigntyStatus, EncryptionStatus;

// ============================================================================
// Query 7: Capacity Planning and Utilization View
// ============================================================================
// Bandwidth allocation, bottlenecks, upgrade candidates
MATCH path = (dc:Node {kind: "TopologyDataCenter", name: "DC1"})
  -[:HAS_ATTRIBUTE__CONNECTIVITY*1..10]-
  (cloud:Node {kind: "TopologyCloudRegion", name: "eu-central-1"})
WITH path,
     [node IN nodes(path) WHERE node.kind IN ["TopologyCircuit", "TopologyVirtualLink"] |
       {type: coalesce(node.circuit_type, node.link_type),
        bandwidth: node.bandwidth,
        committed_rate: node.committed_rate,
        name: coalesce(node.circuit_id, node.name)}
     ] AS segments
WITH segments,
     reduce(min_bw = 1000000, seg IN segments | CASE WHEN seg.bandwidth < min_bw THEN seg.bandwidth ELSE min_bw END) AS bottleneck_bw
UNWIND segments AS seg
WITH bottleneck_bw, seg
RETURN
  seg.name AS Component,
  seg.type AS Type,
  seg.bandwidth AS Bandwidth_Mbps,
  seg.committed_rate AS CommittedRate_Mbps,

  // Bottleneck identification
  CASE
    WHEN seg.bandwidth = bottleneck_bw THEN "⚠ BOTTLENECK"
    WHEN seg.bandwidth < bottleneck_bw * 2 THEN "○ Near bottleneck"
    ELSE "✓ Good capacity"
  END AS CapacityStatus,

  // Utilization estimate (placeholder - integrate with actual monitoring)
  CASE
    WHEN seg.type IN ["dark_fiber", "metro_ethernet"] THEN "25-40%"
    WHEN seg.type IN ["cross_connect", "direct_connect_aws"] THEN "60-80%"
    WHEN seg.type = "dia" THEN "80-95%"
    ELSE "Unknown"
  END AS EstimatedUtilization,

  // Upgrade recommendation
  CASE
    WHEN seg.bandwidth = bottleneck_bw AND seg.bandwidth < 10000
      THEN "Consider upgrading to 10G+"
    WHEN seg.bandwidth = bottleneck_bw AND seg.bandwidth < 100000
      THEN "Consider upgrading to 100G"
    ELSE "Current capacity sufficient"
  END AS Recommendation
ORDER BY seg.bandwidth ASC;

// ============================================================================
// Query 8: Single Consolidated Report (Executive Summary)
// ============================================================================
// One query to rule them all - complete path summary
MATCH path = (dc:Node {kind: "TopologyDataCenter", name: "DC1"})
  -[:HAS_ATTRIBUTE__CONNECTIVITY*1..10]-
  (cloud:Node {kind: "TopologyCloudRegion", name: "eu-central-1"})
WITH path,
     length(path) AS hop_count,
     [node IN nodes(path) WHERE node.kind = "DcimPhysicalDevice" | node.name] AS devices,
     [node IN nodes(path) WHERE node.kind IN ["TopologyCircuit", "TopologyVirtualLink"] | node] AS connections
WITH path, hop_count, devices, connections,
     [conn IN connections | conn.bandwidth] AS bandwidths,
     [conn IN connections WHERE conn.encryption = true OR conn.link_type = "vpn_ipsec"] AS encrypted_segments,
     [conn IN connections | coalesce(conn.circuit_type, conn.link_type)] AS connection_types
OPTIONAL MATCH (conn IN connections)-[:IS_RELATED__PROVIDER]->(provider:Node {kind: "OrganizationProvider"})
WITH path, hop_count, devices, connections, bandwidths, encrypted_segments, connection_types,
     collect(DISTINCT provider.name) AS providers,
     reduce(min_bw = 1000000, bw IN bandwidths | CASE WHEN bw < min_bw THEN bw ELSE min_bw END) AS min_bandwidth
RETURN
  "DC1 → AWS eu-central-1" AS PathName,
  hop_count AS TotalHops,
  size(devices) AS DevicesInPath,
  size(connections) AS ConnectionsInPath,

  // Performance summary
  min_bandwidth AS Bottleneck_Mbps,
  CASE
    WHEN min_bandwidth >= 100000 THEN "100G capable"
    WHEN min_bandwidth >= 10000 THEN "10G capable"
    ELSE "< 10G"
  END AS PathCapacity,

  // Security summary
  size(encrypted_segments) AS EncryptedSegments,
  size(connections) - size(encrypted_segments) AS UnencryptedSegments,
  CASE
    WHEN size(encrypted_segments) = size(connections) THEN "✓ Fully encrypted"
    WHEN size(encrypted_segments) > 0 THEN "⚠ Partially encrypted"
    ELSE "✗ No encryption"
  END AS SecurityPosture,

  // Provider involvement
  size(providers) AS ProviderCount,
  providers AS Providers,

  // Connection diversity
  size([t IN connection_types WHERE t = "dark_fiber"]) AS DarkFiberSegments,
  size([t IN connection_types WHERE t IN ["cross_connect", "direct_connect_aws"]]) AS CloudOnrampSegments,
  size([t IN connection_types WHERE t IN ["vxlan", "gre", "vpn_ipsec"]]) AS OverlaySegments
ORDER BY hop_count ASC
LIMIT 3;
