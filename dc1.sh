#!/bin/bash
# Test script for DC11 with new rack design architecture
# uv run infrahubctl branch create dc1
# uv run infrahubctl object load data/demos/01_data_center/dc1 --branch dc1
# uv run infrahubctl generator generate_dc name=DC1 --branch dc1

uv run infrahubctl generator generate_pod name=DC1-1-POD-1 --branch dc1
uv run infrahubctl generator generate_rack name=MUC-1-SUITE-1-R1-5 --branch dc1
uv run infrahubctl generator generate_rack name=MUC-1-SUITE-1-R2-5 --branch dc1
uv run infrahubctl generator generate_rack name=MUC-1-SUITE-1-R3-5 --branch dc1
uv run infrahubctl generator generate_rack name=MUC-1-SUITE-1-R4-5 --branch dc1
echo "✅ DC1 with racks generated in dc1 branch"



# uv run infrahubctl generator generate_pod name=DC1-1-POD-2 --branch dc1
# uv run infrahubctl generator generate_rack name=MUC-1-SUITE-2-R1-5 --branch dc1
# uv run infrahubctl generator generate_rack name=MUC-1-SUITE-2-R2-5 --branch dc1
# uv run infrahubctl generator generate_rack name=MUC-1-SUITE-2-R3-5 --branch dc1
# uv run infrahubctl generator generate_rack name=MUC-1-SUITE-2-R4-5 --branch dc1
# echo "✅ DC1 with racks generated in dc1 branch"

# uv run infrahubctl generator generate_pod name=DC1-1-POD-3 --branch dc1
# uv run infrahubctl generator generate_rack name=MUC-1-SUITE-3-R1-1 --branch dc1
# uv run infrahubctl generator generate_rack name=MUC-1-SUITE-3-R1-6 --branch dc1
# uv run infrahubctl generator generate_rack name=MUC-1-SUITE-3-R2-1 --branch dc1
# uv run infrahubctl generator generate_rack name=MUC-1-SUITE-3-R2-6 --branch dc1
# echo "✅ DC1 with racks generated in dc1 branch"



