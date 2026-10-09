#!/usr/bin/env bash
# Deletes Event Hubs + Redis (see infra/ephemeral.bicep's header) -- the two
# resources that otherwise cost real money just for existing, ~1.0 EUR/day
# combined, whether or not anyone is actually running a demo. Nothing in
# either is worth keeping between sessions: both hold only ephemeral
# per-call hot state, so there's nothing to back up first.
#
# After this, the live Workbench UI will error on any call that reaches the
# decision/sentiment pipeline (Redis/Event Hub are unreachable) until
# infra/provision-ephemeral.sh recreates them. /healthz itself will likely
# still return 200 -- it doesn't touch either dependency -- so don't use
# that alone to judge whether a demo will actually work.
#
# Usage: infra/teardown-ephemeral.sh
# Override the resource group with CCCP_RESOURCE_GROUP=... if needed.
set -euo pipefail

RG="${CCCP_RESOURCE_GROUP:-rg-cccp-workbench}"
EH_NAMESPACE="cccp-workbench-eh"
REDIS_NAME="cccp-workbench-redis"

echo "==> Deleting Event Hubs namespace $EH_NAMESPACE (also removes the 'calls' hub"
echo "    and its auth rule -- they live inside the namespace)"
az eventhubs namespace delete --name "$EH_NAMESPACE" --resource-group "$RG"

echo "==> Deleting Redis cluster $REDIS_NAME"
az redisenterprise delete --cluster-name "$REDIS_NAME" --resource-group "$RG" --yes

echo "==> Done. Run infra/provision-ephemeral.sh before the next demo session."
