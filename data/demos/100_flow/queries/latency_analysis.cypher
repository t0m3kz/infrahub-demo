// Latency and path optimization analysis
// Updated to include cables, unified connectivity, and realistic latency modeling
// Compares different paths based on hop count, circuit types, and physical layer

// =============================================================================
// Query 1: Path Latency with Physical Layer Awareness
// =============================================================================
// Calculate estimated latency including cable propagation delay
MATCH path = (dc:Node {kind: "TopologyDataCenter", name: "DC1"})
  -[:HAS_ATTRIBUTE__CONNECTIVITY*1..10]-
  (cloud:Node {kind: "TopologyCloudRegion", name: "eu-central-1"})
WITH path,
     length(path) AS hops,
     [node IN nodes(path) WHERE node.kind IN ["TopologyCircuit", "TopologyVirtualLink"] | node] AS links
UNWIND links AS link
WITH path, hops,
     // Circuit type latency (physical layer)
     CASE link.circuit_type
       WHEN "dark_fiber" THEN 1.5      // Direct fiber: ~1.5ms per 300km
       WHEN "cross_connect" THEN 0.5   // Local cross-connect: <1ms
       WHEN "metro_ethernet" THEN 3.0  // Metro service: ~3ms overhead
       WHEN "mpls" THEN 5.0            // MPLS network: ~5ms
       WHEN "dia" THEN 10.0            // Internet: variable, assume 10ms
       ELSE 5.0
     END AS circuit_latency,
     // Virtual link type latency (overlay layer)
     CASE link.link_type
       WHEN "direct_connect_aws" THEN 2.0      // Direct Connect VIF: 1-3ms
       WHEN "express_route_azure" THEN 2.5     // ExpressRoute: 1-4ms
       WHEN "vxlan" THEN 0.5                   // VXLAN encap: minimal
       WHEN "gre" THEN 0.8                     // GRE tunnel: slight overhead
       WHEN "vpn_ipsec" THEN 15.0              // IPsec VPN: crypto overhead
       WHEN "sd_wan" THEN 5.0                  // SD-WAN: optimization overhead
       WHEN "transit_gateway_aws" THEN 1.0     // AWS TGW: <2ms
       ELSE 3.0
     END AS vlink_latency
WITH path, hops,
     sum(coalesce(circuit_latency, 0)) + sum(coalesce(vlink_latency, 0)) AS estimated_latency_ms,
     collect(DISTINCT {type: coalesce(link.circuit_type, link.link_type), name: coalesce(link.circuit_id, link.name)}) AS path_segments
RETURN hops AS Hops,
       round(estimated_latency_ms * 10) / 10 AS EstimatedLatency_ms,
       CASE
         WHEN estimated_latency_ms < 5 THEN "✓✓ Excellent (<5ms)"
         WHEN estimated_latency_ms < 10 THEN "✓ Good (<10ms)"
         WHEN estimated_latency_ms < 20 THEN "○ Acceptable (<20ms)"
         ELSE "⚠ High (>20ms)"
       END AS LatencyClass,
       path_segments AS PathSegments
ORDER BY estimated_latency_ms ASC
LIMIT 10;

// =============================================================================
// Query 2: Bandwidth Bottleneck Analysis with Cable Info
// =============================================================================
// Find minimum bandwidth across entire path including physical layer
MATCH path = (dc:Node {kind: "TopologyDataCenter"})
  -[:HAS_ATTRIBUTE__CONNECTIVITY*1..10]-
  (cloud:Node {kind: "TopologyCloudRegion"})
WITH path,
     [node IN nodes(path) WHERE node.kind IN ["TopologyCircuit", "TopologyVirtualLink"] | {
       name: coalesce(node.circuit_id, node.name),
       bandwidth: node.bandwidth,
       type: coalesce(node.circuit_type, node.link_type)
     }] AS segments
WITH path, segments,
     reduce(min_bw = 1000000, seg IN segments |
       CASE WHEN seg.bandwidth < min_bw THEN seg.bandwidth ELSE min_bw END
     ) AS bottleneck_mbps
UNWIND segments AS seg
WITH path, bottleneck_mbps,
     collect(DISTINCT CASE WHEN seg.bandwidth = bottleneck_mbps THEN seg.name ELSE NULL END) AS bottleneck_segments,
     segments
RETURN bottleneck_mbps AS BottleneckBandwidth_Mbps,
       CASE
         WHEN bottleneck_mbps >= 100000 THEN "✓✓ 100G+ capacity"
         WHEN bottleneck_mbps >= 10000 THEN "✓ 10G+ capacity"
         WHEN bottleneck_mbps >= 1000 THEN "○ 1G+ capacity"
         ELSE "⚠ <1G capacity"
       END AS CapacityClass,
       filter(seg IN bottleneck_segments WHERE seg IS NOT NULL) AS BottleneckLinks,
       [seg IN segments | seg.name] AS FullPath
ORDER BY bottleneck_mbps DESC
LIMIT 10;

// =============================================================================
// Query 3: Path Type Classification (Physical vs Hybrid vs Virtual)
// =============================================================================
// Classify paths by their composition
MATCH path = (dc:Node {kind: "TopologyDataCenter", name: "DC1"})
  -[:HAS_ATTRIBUTE__CONNECTIVITY*1..10]-
  (cloud:Node {kind: "TopologyCloudRegion"})
WITH path,
     [node IN nodes(path) WHERE node.kind = "TopologyCircuit"] AS circuits,
     [node IN nodes(path) WHERE node.kind = "TopologyVirtualLink"] AS vlinks
WITH path,
     size(circuits) AS circuit_count,
     size(vlinks) AS vlink_count,
     circuits, vlinks
RETURN CASE
         WHEN circuit_count > 0 AND vlink_count = 0 THEN "Physical-Only"
         WHEN circuit_count = 0 AND vlink_count > 0 THEN "Virtual-Only"
         WHEN circuit_count > 0 AND vlink_count > 0 THEN "Hybrid (Physical+Virtual)"
         ELSE "Unknown"
       END AS PathType,
       circuit_count AS PhysicalHops,
       vlink_count AS VirtualHops,
       circuit_count + vlink_count AS TotalHops,
       [c IN circuits | c.circuit_id] AS CircuitIDs,
       [v IN vlinks | v.name] AS VirtualLinkNames
ORDER BY PathType, TotalHops
LIMIT 20;

// =============================================================================
// Query 4: Cable vs Circuit Mapping
// =============================================================================
// Show which circuits have physical cables vs service-only
MATCH (circuit:Node {kind: "TopologyCircuit"})
OPTIONAL MATCH (circuit)-[:HAS_ATTRIBUTE__A_SIDE_INTERFACE]->(a_iface:Node)-[:HAS_ATTRIBUTE__CABLE]->(cable:Node {kind: "DcimCable"})
OPTIONAL MATCH (circuit)-[:HAS_ATTRIBUTE__Z_SIDE_INTERFACE]->(z_iface:Node)-[:HAS_ATTRIBUTE__CABLE]->(cable)
OPTIONAL MATCH (circuit)-[:HAS_ATTRIBUTE__SOURCE_DEPLOYMENT]->(src:Node)
OPTIONAL MATCH (circuit)-[:HAS_ATTRIBUTE__DESTINATION_DEPLOYMENT]->(dst:Node)
WITH circuit, cable, src, dst,
     CASE
       WHEN cable IS NOT NULL THEN "✓ Has Physical Cable"
       WHEN circuit.circuit_type IN ["cross_connect", "dia"] THEN "○ Terminates at Provider"
       WHEN circuit.circuit_type = "metro_ethernet" THEN "○ Provider Service (No Cable)"
       ELSE "⚠ Missing Cable Info"
     END AS physical_layer_status
RETURN circuit.circuit_id AS CircuitID,
       circuit.circuit_type AS CircuitType,
       circuit.bandwidth AS Bandwidth_Mbps,
       coalesce(src.name, "N/A") AS Source,
       coalesce(dst.name, "N/A") AS Destination,
       physical_layer_status AS PhysicalLayerStatus,
       coalesce(cable.name, "N/A") AS CableName
ORDER BY physical_layer_status, circuit.circuit_type;

// =============================================================================
// Query 5: Path Preference Ranking (Multi-Factor Score)
// =============================================================================
// Combines latency, bandwidth, redundancy, and path type for optimal selection
MATCH path = (dc:Node {kind: "TopologyDataCenter", name: "DC1"})
  -[:HAS_ATTRIBUTE__CONNECTIVITY*1..10]-
  (cloud:Node {kind: "TopologyCloudRegion", name: "eu-central-1"})
WITH path,
     length(path) AS hops,
     [node IN nodes(path) WHERE node.kind IN ["TopologyCircuit", "TopologyVirtualLink"] | node] AS links
UNWIND links AS link
WITH path, hops,
     // Latency score (lower is better)
     sum(coalesce(
       CASE link.circuit_type
         WHEN "dark_fiber" THEN 1.5
         WHEN "cross_connect" THEN 0.5
         WHEN "metro_ethernet" THEN 3.0
         ELSE 5.0
       END,
       CASE link.link_type
         WHEN "direct_connect_aws" THEN 2.0
         WHEN "vxlan" THEN 0.5
         WHEN "vpn_ipsec" THEN 15.0
         ELSE 3.0
       END
     )) AS latency_score,
     // Bandwidth score (get minimum)
     min(link.bandwidth) AS min_bandwidth,
     // Collect path details
     collect({type: coalesce(link.circuit_type, link.link_type), name: coalesce(link.circuit_id, link.name)}) AS segments
WITH path, hops, latency_score, min_bandwidth, segments,
     // Calculate composite score (lower is better for latency, higher for bandwidth)
     // Normalize: latency penalty (×10) vs bandwidth bonus (÷1000)
     (latency_score * 10) - (min_bandwidth / 1000) AS composite_score
RETURN round(latency_score * 10) / 10 AS Latency_ms,
       min_bandwidth AS MinBandwidth_Mbps,
       hops AS Hops,
       round(composite_score * 10) / 10 AS Score,
       CASE
         WHEN composite_score < 50 THEN "✓✓ Optimal"
         WHEN composite_score < 100 THEN "✓ Good"
         WHEN composite_score < 200 THEN "○ Acceptable"
         ELSE "⚠ Suboptimal"
       END AS Recommendation,
       segments AS PathSegments
ORDER BY composite_score ASC
LIMIT 5;

// =============================================================================
// Query 6: Geographic Distance vs Actual Latency
// =============================================================================
// Compare paths by deployment locations (requires location metadata)
MATCH path = (source:Node)
  -[:HAS_ATTRIBUTE__CONNECTIVITY*1..10]-
  (dest:Node)
WHERE source.kind IN ["TopologyDataCenter", "TopologyColocationZone"]
  AND dest.kind IN ["TopologyColocationZone", "TopologyCloudRegion"]
OPTIONAL MATCH (source)-[:IS_RELATED__LOCATION]->(src_loc:Node)
OPTIONAL MATCH (dest)-[:IS_RELATED__LOCATION]->(dst_loc:Node)
WITH path, source, dest, src_loc, dst_loc,
     [node IN nodes(path) WHERE node.kind IN ["TopologyCircuit", "TopologyVirtualLink"] | node.bandwidth] AS bandwidths
WITH source.name AS Source,
     dest.name AS Destination,
     coalesce(src_loc.name, "Unknown") AS SourceLocation,
     coalesce(dst_loc.name, "Unknown") AS DestLocation,
     reduce(min_bw = 1000000, bw IN bandwidths | CASE WHEN bw < min_bw THEN bw ELSE min_bw END) AS min_bandwidth,
     size([node IN nodes(path) WHERE node.kind IN ["TopologyCircuit", "TopologyVirtualLink"]]) AS hop_count
RETURN Source,
       SourceLocation,
       Destination,
       DestLocation,
       hop_count AS Hops,
       min_bandwidth AS MinBandwidth_Mbps,
       CASE
         WHEN SourceLocation = DestLocation THEN "Local (Same Location)"
         WHEN SourceLocation IN ["Frankfurt", "fra"] AND DestLocation IN ["Amsterdam", "ams"] THEN "Regional (350km)"
         WHEN SourceLocation IN ["Frankfurt", "fra"] AND DestLocation IN ["Dublin", "dub"] THEN "Inter-Regional (1400km)"
         ELSE "Unknown Distance"
       END AS GeographicScope
ORDER BY hop_count, min_bandwidth DESC;

  (cloud:Node {kind: "TopologyCloudRegion", name: "eu-central-1"})
WITH path,
     length(path) AS hops,
     [node IN nodes(path) WHERE node.kind IN ["TopologyCircuit", "TopologyVirtualLink"] | node] AS links
WITH path, hops, links,
     reduce(min_bw = 1000000, link IN links | CASE WHEN link.bandwidth < min_bw THEN link.bandwidth ELSE min_bw END) AS min_bandwidth,
     reduce(latency = 0, link IN links | latency + CASE
       WHEN link.circuit_type = "dark_fiber" THEN 1
       WHEN link.link_type = "direct_connect_aws" THEN 2
       WHEN link.link_type = "vpn_ipsec" THEN 10
       ELSE 5
     END) AS estimated_latency,
     size([link IN links WHERE link.status = "active"]) AS active_links
WITH path, hops, min_bandwidth, estimated_latency, active_links,
     // Scoring: higher is better
     (min_bandwidth / 1000) * 0.4 +  // 40% weight on bandwidth
     (100 / estimated_latency) * 0.4 +  // 40% weight on low latency
     (active_links * 10) * 0.2  // 20% weight on active links
     AS score
RETURN score AS PreferenceScore,
       min_bandwidth AS MinBandwidth,
       estimated_latency AS EstLatency,
       hops AS Hops,
       CASE
         WHEN score > 50 THEN "★★★ Preferred"
         WHEN score > 30 THEN "★★ Good"
         ELSE "★ Backup"
       END AS PathRating,
       [node IN nodes(path) WHERE node.kind IN ["TopologyCircuit", "TopologyVirtualLink"] | node.name] AS PathDetails
ORDER BY score DESC
LIMIT 10;
