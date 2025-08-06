echo "Generating $1 Data Center"
uv run infrahubctl check validate_$1 device=$2 --branch $3

