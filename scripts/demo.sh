echo "Delete $1 branch"
poetry run infrahubctl branch delete $1
sleep 10

echo "Create $1 branch"
poetry run infrahubctl branch create $1

echo "Load initial data"
poetry run infrahubctl run bootstrap/demo_$1.py --branch $1
