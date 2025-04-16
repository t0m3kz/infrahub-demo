echo "Delete $1 branch"
uv run infrahubctl branch delete $1
sleep 10

echo "Create $1 branch"
uv run infrahubctl branch create $1

echo "Load initial data"
uv run infrahubctl run bootstrap/demo_$1.py --branch $1
