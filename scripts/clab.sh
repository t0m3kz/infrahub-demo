BRANCH=${2:-main}
echo "Show cabling for $1"
uv run infrahubctl render topology_clab name=$1 --branch $BRANCH


