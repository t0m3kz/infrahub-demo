#!/bin/bash
# Quick check of DC11 cabling and devices

INFRAHUB_ADDRESS=${INFRAHUB_ADDRESS:-http://localhost:8000}
INFRAHUB_API_TOKEN=${INFRAHUB_API_TOKEN:-06438eb2-8019-4776-878c-0941b1f1d1ec}

echo "================================================================================"
echo "DC11 Pod 1 - Checking Devices and Cabling"
echo "================================================================================"

echo -e "\n📍 Checking Rack 1-2-05-B (Network rack with leafs)..."
curl -s -X POST "${INFRAHUB_ADDRESS}/graphql/dc11" \
  -H "Content-Type: application/json" \
  -H "X-INFRAHUB-KEY: ${INFRAHUB_API_TOKEN}" \
  -d '{"query": "query { DcimGenericDevice(location__name__value: \"AMS-1-Suite-1-Rack-1-2-05-B\") { edges { node { name { value } role { value } } } } }"}' \
  | python3 -m json.tool

echo -e "\n📍 Checking Rack 1-1-01-A (ToR-only rack)..."
curl -s -X POST "${INFRAHUB_ADDRESS}/graphql/dc11" \
  -H "Content-Type: application/json" \
  -H "X-INFRAHUB-KEY: ${INFRAHUB_API_TOKEN}" \
  -d '{"query": "query { DcimGenericDevice(location__name__value: \"AMS-1-Suite-1-Rack-1-1-01-A\") { edges { node { name { value } role { value } } } } }"}' \
  | python3 -m json.tool

echo -e "\n📍 Checking cables for rack2 ToRs..."
curl -s -X POST "${INFRAHUB_ADDRESS}/graphql/dc11" \
  -H "Content-Type: application/json" \
  -H "X-INFRAHUB-KEY: ${INFRAHUB_API_TOKEN}" \
  -d '{"query": "query { DcimCable(name__contains: \"rack2-tor\") { edges { node { name { value } endpoints { edges { node { ... on DcimInterface { device { node { name { value } } } name { value } } } } } } } } }"}' \
  | python3 -m json.tool | head -100

echo -e "\n📍 Checking cables for rack1 ToRs (if any)..."
curl -s -X POST "${INFRAHUB_ADDRESS}/graphql/dc11" \
  -H "Content-Type: application/json" \
  -H "X-INFRAHUB-KEY: ${INFRAHUB_API_TOKEN}" \
  -d '{"query": "query { DcimCable(name__contains: \"rack1-tor\") { edges { node { name { value } endpoints { edges { node { ... on DcimInterface { device { node { name { value } } } name { value } } } } } } } } }"}' \
  | python3 -m json.tool | head -100

echo -e "\n📍 Checking all leafs in Pod 1..."
curl -s -X POST "${INFRAHUB_ADDRESS}/graphql/dc11" \
  -H "Content-Type: application/json" \
  -H "X-INFRAHUB-KEY: ${INFRAHUB_API_TOKEN}" \
  -d '{"query": "query { DcimGenericDevice(role__value: \"leaf\") { edges { node { name { value } ... on DcimPhysicalDevice { rack { node { name { value } row { value } } } } } } } }"}' \
  | python3 -m json.tool

echo -e "\n================================================================================"
echo "✅ Check complete!"
echo "================================================================================"
