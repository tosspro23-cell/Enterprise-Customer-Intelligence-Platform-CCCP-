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

Secrets for both Container Apps (Azure OpenAI key, Event Hubs connection
string, AI Search admin key, Redis password) are wired directly from
`listKeys()`/`listAdminKeys()` calls on the sibling resources in this
same file -- nothing is passed in from outside, and nothing is written to
disk or printed by a normal deployment.
