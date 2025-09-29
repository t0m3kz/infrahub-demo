BRANCH=${3:-main}
echo "Generating $1 Data Center"
uv run infrahubctl generator create_$1 name=$2 --branch $BRANCH


