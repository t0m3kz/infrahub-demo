echo "Show cabling for $1"
poetry run infrahubctl render topology_clab name=$1 --branch $2


