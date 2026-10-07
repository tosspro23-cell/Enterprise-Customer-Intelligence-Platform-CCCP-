# Infrastructure

`main.bicep` codifies the Azure resources behind the validation work in
[../README.md](../README.md) ("Validated against a live Azure OpenAI
endpoint" onward): Azure OpenAI, Event Hubs, Azure AI Search, Azure
Managed Redis, Azure Speech, Azure AI Language, and the two Container
Apps. It was written *after* those resources were created by hand in this
project's first cloud session and checked against them with `what-if` --
it did not exist when the resources did, which is exactly the gap it
closes: before this file, rebuilding the environment meant re-reading
conversation history for the right `az` commands and the fixes found
along the way (wrong Redis product, wrong clustering policy, missing
`--public-network-access`, wrong token param names on the reasoning
model...). Now it's one file.

## What this does not include

**The Container Apps environment itself.** This subscription's free-trial
tier allows one Container Apps environment per subscription, and another
project already has one. Both Container Apps in this file join that
existing environment via the `containerAppsEnvironmentId` parameter
rather than this file creating one. On a subscription without that
constraint, add a `Microsoft.App/managedEnvironments` resource and point
both apps at it instead of passing the parameter.

**A resource group.** This is a resource-group-scoped deployment
(`az deployment group ...`), so the target resource group must already
exist.

## Before you deploy

Read `main.bicep` top to bottom first -- several choices are pinned
because the obvious alternative failed during validation, and the
comments say why (Redis's clustering policy, the Basic vs Free AI Search
SKU, the region split across three regions because of per-service quota
limits on this subscription). A different subscription may not have the
same constraints; adjust the parameters, don't just delete the comments.

**If you're applying this against resources that already exist and are
running** (as opposed to a from-scratch environment), run `what-if`
first and read it. Most resource types here tolerate a re-apply fine;
Azure Managed Redis does not tolerate every property change in place
(clustering policy in particular is set at create time) and a mismatched
template can force a delete-and-recreate that `what-if` will call out
before it happens, not after.

```bash
az deployment group what-if \
  -g <resource-group> \
  -f infra/main.bicep \
  --parameters containerAppsEnvironmentId="<existing-environment-resource-id>"

az deployment group create \
  -g <resource-group> \
  -f infra/main.bicep \
  --parameters containerAppsEnvironmentId="<existing-environment-resource-id>"
```

Omit `containerAppsEnvironmentId` to deploy only the backend services
(Azure OpenAI, Event Hubs, AI Search, Redis, Speech, Language) without
either Container App.

## After you deploy

The Container Apps pull `ghcr.io/tosspro23-cell/cccp-workbench:latest`,
built by `.github/workflows/build-push-ghcr.yml` -- make sure that image
exists (push to `main` once, or trigger the workflow manually) before
the first deployment, and that the GHCR package is public or the
Container Apps have a pull credential for it (see the README's "镜像可见性"
discussion in this project's history -- this repo made it public since
the source is already public and the image bakes in no secrets).

Secrets for both Container Apps (Azure OpenAI key, a hub-scoped Send-only
Event Hubs connection string, an AI Search **query** key, Redis password,
plus Speech/Language keys for the UI app only) are wired directly from
`listKeys()`/`listQueryKeys()` calls on the sibling resources in this same
file -- nothing is written to disk or printed by a normal deployment.
Each app gets only the secrets it uses.

The one value passed in is the **demo token** that gates live runs on the
public Workbench (every run spends paid Speech/Language/OpenAI calls):

```bash
az deployment group create -g <resource-group> -f infra/main.bicep \
  --parameters containerAppsEnvironmentId="<id>" workbenchDemoToken="$(openssl rand -hex 16)"
```

Share the demo as `https://<workbenchUiFqdn>/?token=<that value>`. Without
the parameter the UI still loads but live runs return 503 (fail closed).
The UI app also gets `AZURE_OPENAI_DEPLOYMENT_REALTIME` (the non-reasoning
`narrator-realtime-v1` deployment) and `NARRATOR_DEADLINE_S` (parameter
`narratorDeadlineSeconds`, default 3.0); the headless app keeps the
reasoning deployment its committed load-test reports measured.

The headless load-test app (`cccp-workbench-app`) has **internal** ingress:
`/loadtest` has no auth of its own. Trigger it from inside the environment,
e.g. `az containerapp exec -n cccp-workbench-app -g <rg> --command "curl -XPOST localhost:8080/loadtest"`.

`search_guidance.py` ingest needs the Search **admin** key -- run it from an
operator shell (`az search admin-key show ...`), not from the apps.

Switching an existing deployment to this version changes secrets and the
headless app's ingress: run `what-if` first (see above).
