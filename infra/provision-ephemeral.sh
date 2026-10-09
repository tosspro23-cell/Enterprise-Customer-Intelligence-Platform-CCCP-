#!/usr/bin/env bash
# Recreates Event Hubs + Redis (infra/ephemeral.bicep -- see that file's
# header for why these two and not the rest of the stack), points both
# Container Apps at the new instances, and restarts them so the running
# processes actually pick up the fresh connection details -- updating a
# Container App secret alone does not do that; only a new revision does.
#
# Run this before a demo session, after running teardown-ephemeral.sh to
# tear the previous session's instances down. Timed end to end on a real
# run (2026-10-09): ~7m20s for the bicep deployment itself (almost all of
# it Redis), another ~2-3 min for the secret update + restart + health
# check below -- call it ~10 minutes total, so start it ahead of when you
# actually need the demo, not as someone is about to watch. Teardown is
# faster, ~4-5 min.
#
# Usage: infra/provision-ephemeral.sh
# Override the resource group with CCCP_RESOURCE_GROUP=... if needed.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

RG="${CCCP_RESOURCE_GROUP:-rg-cccp-workbench}"
EH_NAMESPACE="cccp-workbench-eh"
EH_HUB="calls"
EH_RULE="cccp-send"
REDIS_NAME="cccp-workbench-redis"
APPS=("cccp-workbench-app" "cccp-workbench-ui")

echo "==> Deploying Event Hubs + Redis into $RG (this is the slow step -- Redis"
echo "    provisioning alone commonly takes several minutes; be patient)"
az deployment group create \
  --resource-group "$RG" \
  --template-file ephemeral.bicep \
  --name "ephemeral-$(date +%s)" \
  -o none

echo "==> Reading back connection details"
EH_CONN=$(az eventhubs eventhub authorization-rule keys list \
  --resource-group "$RG" --namespace-name "$EH_NAMESPACE" --eventhub-name "$EH_HUB" \
  --authorization-rule-name "$EH_RULE" --query primaryConnectionString -o tsv)
REDIS_HOST=$(az resource show --resource-group "$RG" --name "$REDIS_NAME" \
  --resource-type "Microsoft.Cache/redisEnterprise" --query properties.hostName -o tsv)
REDIS_PASSWORD=$(az redisenterprise database list-keys \
  --resource-group "$RG" --cluster-name "$REDIS_NAME" \
  --query primaryKey -o tsv)

if [[ -z "$EH_CONN" || -z "$REDIS_HOST" || -z "$REDIS_PASSWORD" ]]; then
  echo "!! One or more values came back empty -- not updating app secrets with" >&2
  echo "   partial data. Re-run this script, or check the deployment above." >&2
  exit 1
fi

SUFFIX="eph$(date +%H%M%S)"
for app in "${APPS[@]}"; do
  echo "==> Updating secrets on $app"
  az containerapp secret set --name "$app" --resource-group "$RG" --secrets \
    "eventhub-connection-string=$EH_CONN" \
    "redis-host=$REDIS_HOST" \
    "redis-password=$REDIS_PASSWORD" \
    -o none
  echo "==> Restarting $app with a new revision so it picks up the new secrets"
  az containerapp update --name "$app" --resource-group "$RG" --revision-suffix "$SUFFIX" -o none
done

echo "==> Waiting for the public UI to come back healthy"
UI_FQDN=$(az containerapp show --name cccp-workbench-ui --resource-group "$RG" \
  --query "properties.configuration.ingress.fqdn" -o tsv)
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "https://$UI_FQDN/healthz" || true)
  if [[ "$code" == "200" ]]; then
    echo "==> Healthy: https://$UI_FQDN/healthz -> 200"
    exit 0
  fi
  sleep 5
done
echo "!! Did not see a 200 from /healthz after ~2.5 minutes -- check the revision" >&2
echo "   logs (az containerapp logs show --name cccp-workbench-ui -g $RG)." >&2
exit 1
