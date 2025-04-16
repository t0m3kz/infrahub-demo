
echo "Load schema"
uv run infrahubctl schema load model --wait 20

echo "Load menu"
uv run infrahubctl menu load menu

echo "Load initial data"
uv run infrahubctl run bootstrap/bootstrap.py

# echo "Add demo repository"
# uv run infrahubctl repository add DEMO https://github.com/t0m3kz/infrahub-demo.git --ref main --read-only