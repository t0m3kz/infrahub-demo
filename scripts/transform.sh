echo "Show cabling for $1"
# Use main branch if no branch is specified
BRANCH=${2:-main}
uv run infrahubctl transform $1 --branch $BRANCH


