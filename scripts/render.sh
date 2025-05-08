echo "Show config for $1"
uv run infrahubctl render $1 device=$2 --branch $3


