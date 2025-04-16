echo "Show cabling for $1"
poetry run infrahubctl generator create_dc name=$1 --branch $2


