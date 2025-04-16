echo "Show cabling for $1"
uv run infrahubctl transform topology_cabling name=$1 --branch $2


