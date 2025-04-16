echo "Generating $1 Data Center"
uv run infrahubctl generator create_dc name=$1 --branch $2


