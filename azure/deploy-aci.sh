#!/usr/bin/env bash
# Deploys the bot to Azure Container Instances (ACI) — the simplest way to run
# one always-on container cheaply. Requires the Azure CLI (az) to be logged in
# (`az login`) and a container image pushed to a registry (see README.md).
#
# Usage:
#   DISCORD_TOKEN=xxx ./azure/deploy-aci.sh <resource-group> <image> [location]
#
# Example:
#   DISCORD_TOKEN=xxx ./azure/deploy-aci.sh vcbot-rg myregistry.azurecr.io/vcdiscordbot:latest eastus

set -euo pipefail

RESOURCE_GROUP="${1:?Usage: $0 <resource-group> <image> [location]}"
IMAGE="${2:?Usage: $0 <resource-group> <image> [location]}"
LOCATION="${3:-eastus}"
CONTAINER_NAME="vcdiscordbot"

if [[ -z "${DISCORD_TOKEN:-}" ]]; then
  echo "Error: set the DISCORD_TOKEN environment variable before running this script." >&2
  exit 1
fi

az group create --name "$RESOURCE_GROUP" --location "$LOCATION" >/dev/null

# 0.5 vCPU / 0.5 GB is the smallest size ACI allows and is plenty for a bot
# that just relays gateway events — this is what keeps the monthly bill low.
az container create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$CONTAINER_NAME" \
  --image "$IMAGE" \
  --cpu 0.5 \
  --memory 0.5 \
  --os-type Linux \
  --restart-policy Always \
  --environment-variables DATABASE_PATH=/app/data/tempvc.db \
  --secure-environment-variables DISCORD_TOKEN="$DISCORD_TOKEN"

echo "Container group '$CONTAINER_NAME' created in resource group '$RESOURCE_GROUP'."
echo "Tail logs with: az container logs --resource-group $RESOURCE_GROUP --name $CONTAINER_NAME --follow"
