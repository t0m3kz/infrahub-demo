#!/bin/bash
# Test script for DC11 with new rack design architecture

set -e

echo "🧪 Testing DC11 Deployment with Rack Designs"
echo "=============================================="
echo ""

# Step 1: Load schemas
echo "📋 Step 1: Loading schemas..."
uv run infrahubctl schema load schemas/base --branch main
uv run infrahubctl schema load schemas/extensions --branch main
echo "✅ Schemas loaded"
echo ""

# Step 2: Load bootstrap data (includes rack designs)
echo "📋 Step 2: Loading bootstrap data..."
uv run infrahubctl object load data/bootstrap --branch main
echo "✅ Bootstrap data loaded"
echo ""

# Step 3: Create DC11 branch
echo "📋 Step 3: Creating dc11 branch..."
uv run infrahubctl branch create dc11 || echo "⚠️  Branch might already exist"
echo "✅ Branch ready"
echo ""

# Step 4: Load DC11 topology
echo "📋 Step 4: Loading DC11 topology..."
uv run infrahubctl object load data/demos/01_data_center/dc11 --branch dc11
echo "✅ DC11 topology loaded"
echo ""

# Step 5: Generate DC topology
echo "📋 Step 5: Generating DC topology..."
uv run infrahubctl run generators/generate_dc.py --branch dc11
echo "✅ DC generated"
echo ""

# Step 6: Generate Pod 1 (mixed deployment)
echo "📋 Step 6: Generating Pod 1 (mixed deployment)..."
uv run infrahubctl run generators/generate_pod.py --variables '{"name": "DC11-1-1"}' --branch dc11
echo "✅ Pod 1 generated"
echo ""

# Step 7: Generate Pod 2 (tor deployment)
echo "📋 Step 7: Generating Pod 2 (tor deployment)..."
uv run infrahubctl run generators/generate_pod.py --variables '{"name": "DC11-1-2"}' --branch dc11
echo "✅ Pod 2 generated"
echo ""

# Step 8: Generate Pod 3 (middle_rack deployment)
echo "📋 Step 8: Generating Pod 3 (middle_rack deployment)..."
uv run infrahubctl run generators/generate_pod.py --variables '{"name": "DC11-1-3"}' --branch dc11
echo "✅ Pod 3 generated"
echo ""

# Step 9: Generate racks with offset calculation test
echo "📋 Step 9: Generating racks (testing offset calculation)..."
echo ""
echo "  🔍 Expected offsets for Pod 1 (mixed deployment):"
echo "    - Row 1, Index 1 (tor-rack-2T):      offset = 0  (first rack)"
echo "    - Row 1, Index 5 (network-rack-2L-2T): offset = 2  (2 devices from rack 1)"
echo "    - Row 1, Index 11 (tor-rack-2T):     offset = 6  (2+4 devices from racks 1,5)"
echo "    - Row 2, Index 5 (network-rack-2L-2T): offset = 8  (2+4+2 devices)"
echo ""

# Generate all racks in Pod 1
for rack in "ams-1-s-1-1-1" "ams-1-s-1-1-5" "ams-1-s-1-1-11" "ams-1-s-1-2-5"; do
  echo "  📦 Generating rack: $rack"
  uv run infrahubctl run generators/generate_rack.py --variables "{\"name\": \"$rack\"}" --branch dc11
done

echo ""
echo "  🔍 Expected offsets for Pod 2 (tor deployment):"
echo "    - Row 1, Index 1 (tor-rack-2T): offset = 0"
echo "    - Row 1, Index 2 (tor-rack-2T): offset = 2"
echo "    - Row 2, Index 1 (tor-rack-2T): offset = 4"
echo "    - Row 3, Index 1 (tor-rack-2T): offset = 6"
echo ""

# Generate all racks in Pod 2
for rack in "ams-1-s-2-1-1" "ams-1-s-2-1-2" "ams-1-s-2-2-1" "ams-1-s-2-3-1"; do
  echo "  📦 Generating rack: $rack"
  uv run infrahubctl run generators/generate_rack.py --variables "{\"name\": \"$rack\"}" --branch dc11
done

echo ""
echo "  🔍 Expected offsets for Pod 3 (middle_rack deployment):"
echo "    - Row 1, Index 1 (network-rack-2L-2T): offset = 0"
echo "    - Row 2, Index 2 (network-rack-2L-2T): offset = 4"
echo ""

# Generate all racks in Pod 3
for rack in "ams-1-s-3-1-1" "ams-1-s-3-2-2"; do
  echo "  📦 Generating rack: $rack"
  uv run infrahubctl run generators/generate_rack.py --variables "{\"name\": \"$rack\"}" --branch dc11
done

echo ""
echo "✅ All racks generated"
echo ""

# Step 10: Summary
echo "🎉 DC11 Test Complete!"
echo "======================="
echo ""
echo "📊 Summary:"
echo "  - Pod 1 (mixed):      4 racks (2 network + 2 tor)"
echo "  - Pod 2 (tor):        4 racks (all tor, no local leafs)"
echo "  - Pod 3 (middle_rack): 2 racks (all network with local leafs)"
echo ""
echo "🔍 To verify offset calculations, check the generator logs above."
echo "   Look for: 'Calculated cabling offset=X for rack...'"
echo ""
echo "🌐 View in Infrahub UI: http://localhost:8000"
echo "   Branch: dc11"
echo ""
