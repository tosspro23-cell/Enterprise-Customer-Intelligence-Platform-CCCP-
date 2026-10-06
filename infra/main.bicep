// Infrastructure for the CCCP Azure validation deployment (see ../README.md
// "Validated against a live Azure OpenAI endpoint" and the sections after
// it). This codifies what was created by hand in this project's first
// cloud session, so a deleted or lost environment can be rebuilt from the
// repo instead of from conversation history -- that gap is exactly why
// this file exists.
//
// Deliberately NOT included: the Container Apps *environment* itself. This
// subscription's free-trial tier caps it at one environment per
// subscription, already used by another project, so the two Container Apps
// below join an existing environment via `containerAppsEnvironmentId`
// rather than this file creating one. A subscription without that
// constraint should add a Microsoft.App/managedEnvironments resource and
// point both Container Apps at it instead.
//
// Deploy (review the resource group's existing state first -- this targets
// a resource group, not a subscription, and some resources below, notably
// Redis, cannot be updated in place after creation; a mismatched re-apply
// can force a delete+recreate):
//   az deployment group what-if -g <resource-group> -f infra/main.bicep
//   az deployment group create  -g <resource-group> -f infra/main.bicep

@description('Base name used to derive resource names (kept short: Cognitive Services names must be globally unique).')
@minLength(6)
param baseName string = 'cccp-workbench'

@description('Region for Event Hubs, Speech and Language. These landed in eastus2 in the original deployment.')
param regionPrimary string = 'eastus2'

@description('Region for Azure OpenAI. Landed in westus because eastus2 Cognitive Services OpenAI quota was already used by another project on this subscription.')
param regionOpenAi string = 'westus'

@description('Region for AI Search and Redis. Landed in westus/westus2 for the same quota reason.')
param regionSecondary string = 'westus'

@description('Region for Azure Managed Redis specifically (westus2, not westus -- different quota pool).')
param regionRedis string = 'westus2'

@description('AI Search SKU. Free (F0) is one per subscription; this subscription\'s was already used elsewhere, so Basic was used instead -- Basic has a standing hourly cost Free does not. Set to "free" if a Free slot is available.')
@allowed(['free', 'basic'])
param searchSku string = 'basic'

@description('Full resource ID of an existing Container Apps managed environment to deploy into. Leave empty to skip both Container App resources (e.g. if deploying only the backend services).')
param containerAppsEnvironmentId string = ''

@description('Container image for both Container Apps (built by .github/workflows/build-push-ghcr.yml).')
param containerImage string = 'ghcr.io/tosspro23-cell/cccp-workbench:latest'

var names = {
  openai: '${baseName}-openai'
  eventHubNamespace: '${baseName}-eh'
  search: '${baseName}-search'
  redis: '${baseName}-redis'
  speech: '${baseName}-speech'
  language: '${baseName}-language'
  appHeadless: '${baseName}-app'
  appUi: '${baseName}-ui'
}

// ---------------------------------------------------------------- Azure OpenAI
resource openAi 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: names.openai
  location: regionOpenAi
  kind: 'OpenAI'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: names.openai
    publicNetworkAccess: 'Enabled'
  }
}

// Reasoning model: correct for post-call/assistant-style explanation quality,
// too slow for the real-time profile (see README "What this does and
// doesn't show" and docs/architecture.md §11) -- kept for that reason, not
// despite it.
resource narratorReasoning 'Microsoft.CognitiveServices/accounts/deployments@2023-05-01' = {
  parent: openAi
  name: 'narrator-v1'
  sku: { name: 'GlobalStandard', capacity: 10 }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-5-mini', version: '2025-08-07' }
  }
}

// Non-reasoning model: the real candidate for the real-time profile's 3s
// budget (still not reliably under it on a non-provisioned deployment --
// see the live-endpoint measurements in README.md).
resource narratorRealtime 'Microsoft.CognitiveServices/accounts/deployments@2023-05-01' = {
  parent: openAi
  name: 'narrator-realtime-v1'
  sku: { name: 'GlobalStandard', capacity: 10 }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-4.1-mini', version: '2025-04-14' }
  }
  dependsOn: [narratorReasoning] // deployments on one account are created serially
}

// ---------------------------------------------------------------- Event Hubs
resource eventHubNamespace 'Microsoft.EventHub/namespaces@2024-01-01' = {
  name: names.eventHubNamespace
  location: regionPrimary
  sku: { name: 'Basic', tier: 'Basic' }
}

resource eventHub 'Microsoft.EventHub/namespaces/eventhubs@2024-01-01' = {
  parent: eventHubNamespace
  name: 'calls'
  properties: {
    partitionCount: 4
    messageRetentionInDays: 1 // max allowed on the Basic tier
  }
}

resource eventHubAuthRule 'Microsoft.EventHub/namespaces/AuthorizationRules@2024-01-01' existing = {
  parent: eventHubNamespace
  name: 'RootManageSharedAccessKey'
}

// ---------------------------------------------------------------- AI Search
resource search 'Microsoft.Search/searchServices@2023-11-01' = {
  name: names.search
  location: regionSecondary
  sku: { name: searchSku }
  properties: {
    hostingMode: 'default'
    partitionCount: 1
    replicaCount: 1
  }
}

// ---------------------------------------------------------------- Azure Managed Redis
// EnterpriseCluster, not the default OSSCluster: OSSCluster routes clients
// to internal per-shard IPs whose TLS cert doesn't cover those IPs, and a
// non-cluster-aware client can't follow its MOVED redirects either way.
// EnterpriseCluster proxies through a single endpoint with a cert that
// actually matches what a plain client connects to. See README.md
// "Two things broke before this worked" for the failure this avoids.
resource redis 'Microsoft.Cache/redisEnterprise@2024-09-01-preview' = {
  name: names.redis
  location: regionRedis
  sku: { name: 'Balanced_B0' }
  properties: {
    minimumTlsVersion: '1.2'
  }
}

resource redisDatabase 'Microsoft.Cache/redisEnterprise/databases@2024-09-01-preview' = {
  parent: redis
  name: 'default'
  properties: {
    clusteringPolicy: 'EnterpriseCluster'
    evictionPolicy: 'VolatileLRU'
    clientProtocol: 'Encrypted'
    accessKeysAuthentication: 'Enabled'
    port: 10000
  }
}

// ---------------------------------------------------------------- Speech + Language (both free tier)
resource speech 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: names.speech
  location: regionPrimary
  kind: 'SpeechServices'
  sku: { name: 'F0' }
  properties: {
    customSubDomainName: names.speech
  }
}

resource language 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: names.language
  location: regionPrimary
  kind: 'TextAnalytics'
  sku: { name: 'F0' }
  properties: {
    customSubDomainName: names.language
  }
}

// ---------------------------------------------------------------- Container Apps
// Both apps share one image; which server.py variant runs is just the
// start command -- see README.md "Two Container Apps now run...".
var sharedSecrets = [
  { name: 'azure-openai-endpoint', value: openAi.properties.endpoint }
  { name: 'azure-openai-deployment', value: narratorReasoning.name }
  { name: 'azure-openai-api-key', value: openAi.listKeys().key1 }
  { name: 'azure-openai-api-version', value: '2024-10-21' }
  { name: 'eventhub-connection-string', value: eventHubAuthRule.listKeys().primaryConnectionString }
  { name: 'eventhub-name', value: eventHub.name }
  { name: 'azure-search-endpoint', value: 'https://${search.name}.search.windows.net' }
  { name: 'azure-search-api-key', value: search.listAdminKeys().primaryKey }
  { name: 'azure-search-index', value: 'guidance' }
  { name: 'redis-host', value: redis.properties.hostName }
  { name: 'redis-port', value: '10000' }
  { name: 'redis-password', value: redisDatabase.listKeys().primaryKey }
  { name: 'azure-speech-key', value: speech.listKeys().key1 }
  { name: 'azure-speech-region', value: regionPrimary }
  { name: 'azure-language-endpoint', value: language.properties.endpoint }
  { name: 'azure-language-key', value: language.listKeys().key1 }
]
var sharedEnvVars = [
  { name: 'AZURE_OPENAI_ENDPOINT', secretRef: 'azure-openai-endpoint' }
  { name: 'AZURE_OPENAI_DEPLOYMENT', secretRef: 'azure-openai-deployment' }
  { name: 'AZURE_OPENAI_API_KEY', secretRef: 'azure-openai-api-key' }
  { name: 'AZURE_OPENAI_API_VERSION', secretRef: 'azure-openai-api-version' }
  { name: 'EVENTHUB_CONNECTION_STRING', secretRef: 'eventhub-connection-string' }
  { name: 'EVENTHUB_NAME', secretRef: 'eventhub-name' }
  { name: 'AZURE_SEARCH_ENDPOINT', secretRef: 'azure-search-endpoint' }
  { name: 'AZURE_SEARCH_API_KEY', secretRef: 'azure-search-api-key' }
  { name: 'AZURE_SEARCH_INDEX', secretRef: 'azure-search-index' }
  { name: 'REDIS_HOST', secretRef: 'redis-host' }
  { name: 'REDIS_PORT', secretRef: 'redis-port' }
  { name: 'REDIS_PASSWORD', secretRef: 'redis-password' }
  // Used by cccp-workbench-ui (the voice pipeline) and harmless-but-unused on
  // cccp-workbench-app (the headless load-test runner never calls Speech/Language).
  { name: 'AZURE_SPEECH_KEY', secretRef: 'azure-speech-key' }
  { name: 'AZURE_SPEECH_REGION', secretRef: 'azure-speech-region' }
  { name: 'AZURE_LANGUAGE_ENDPOINT', secretRef: 'azure-language-endpoint' }
  { name: 'AZURE_LANGUAGE_KEY', secretRef: 'azure-language-key' }
]

resource appHeadless 'Microsoft.App/containerApps@2024-03-01' = if (!empty(containerAppsEnvironmentId)) {
  name: names.appHeadless
  location: regionPrimary
  properties: {
    environmentId: containerAppsEnvironmentId
    configuration: {
      ingress: { external: true, targetPort: 8080 }
      secrets: sharedSecrets
    }
    template: {
      containers: [
        {
          name: 'cccp-workbench'
          image: containerImage
          resources: { cpu: json('0.5'), memory: '1.0Gi' }
          env: sharedEnvVars
          // default CMD in the Dockerfile: apps/api/cloud_server.py (headless load-test trigger)
        }
      ]
      scale: { minReplicas: 0, maxReplicas: 1 }
    }
  }
}

resource appUi 'Microsoft.App/containerApps@2024-03-01' = if (!empty(containerAppsEnvironmentId)) {
  name: names.appUi
  location: regionPrimary
  properties: {
    environmentId: containerAppsEnvironmentId
    configuration: {
      ingress: { external: true, targetPort: 8080 }
      secrets: sharedSecrets
    }
    template: {
      containers: [
        {
          name: 'cccp-workbench-ui'
          image: containerImage
          command: ['python3']
          args: ['apps/api/cloud_workbench_server.py']
          resources: { cpu: json('0.5'), memory: '1.0Gi' }
          env: sharedEnvVars
        }
      ]
      scale: { minReplicas: 0, maxReplicas: 1 }
    }
  }
}

output openAiEndpoint string = openAi.properties.endpoint
output eventHubNamespaceName string = eventHubNamespace.name
output searchEndpoint string = 'https://${search.name}.search.windows.net'
output redisHostName string = redis.properties.hostName
output workbenchUiFqdn string = appUi.?properties.?configuration.?ingress.?fqdn ?? ''
