
echo "Load schema"
poetry run infrahubctl schema load model

echo "Waiting for schemas to be applied...."
sleep 30

echo "Load menu"
poetry run infrahubctl menu load menu

echo "Load initial data"
poetry run infrahubctl run bootstrap/bootstrap.py

echo "Add demo repository"
poetry run infrahubctl repository add DEMO https://github.com/t0m3kz/infrahub-demo.git --read-only