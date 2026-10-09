// The two hot-path resources that cost real money just for existing, 24/7,
// regardless of whether anyone is running a demo: Event Hubs and Azure
// Managed Redis (see README.md "Cost tracking" -- Redis ~0.68 EUR/day,
// Event Hubs ~0.32 EUR/day, together ~30 EUR/month of pure standing cost on
// top of Azure's cheapest available tier for each). Both hold only
// ephemeral per-call hot state -- a domain-event bus and a scratch KV store
// -- nothing in either is worth persisting between demo sessions, so unlike
// AI Search's guidance documents there's no re-seeding step needed after
// recreating them: an empty Redis/Event Hub is exactly the starting state a
// fresh call expects.
//
// Split out from main.bicep on purpose: this file's resources are meant to
// be destroyed and recreated routinely (see teardown-ephemeral.sh /
// provision-ephemeral.sh); main.bicep's resources (Azure OpenAI deployments,
// Speech/Language, the Container Apps themselves) are not -- redeploying
// those is slower, touches model quota, or would bounce the running demo
// for no reason. Same resource definitions as main.bicep's Event Hubs /
// Redis sections (kept in sync by hand -- there are only two resources
// here, a shared module would be more ceremony than the duplication it
// avoids).
//
// Usage: see provision-ephemeral.sh / teardown-ephemeral.sh, which also
// handle updating the two Container Apps' secrets and restarting them so
// the running processes actually pick up the new connection details --
// just deploying this file alone leaves the apps pointed at a deleted
// Redis/Event Hub.

@description('Base name used to derive resource names -- must match main.bicep\'s baseName so the Container Apps\' existing secret names line up.')
@minLength(6)
param baseName string = 'cccp-workbench'

@description('Region for Event Hubs. Must match where the Container Apps / rest of the stack expect it (main.bicep\'s regionPrimary).')
param regionPrimary string = 'eastus2'

@description('Region for Azure Managed Redis specifically (westus2 in the original deployment -- a different quota pool than regionPrimary/regionSecondary).')
param regionRedis string = 'westus2'

var names = {
  eventHubNamespace: '${baseName}-eh'
  redis: '${baseName}-redis'
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

// Send-only, scoped to the one hub -- the apps only publish.
resource eventHubSendRule 'Microsoft.EventHub/namespaces/eventhubs/authorizationRules@2024-01-01' = {
  parent: eventHub
  name: 'cccp-send'
  properties: {
    rights: ['Send']
  }
}

// ---------------------------------------------------------------- Azure Managed Redis
// EnterpriseCluster, not the default OSSCluster -- see main.bicep's comment
// on the same resource for why (OSSCluster's per-shard TLS cert mismatch).
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

output eventHubName string = eventHub.name
@secure()
output eventHubConnectionString string = eventHubSendRule.listKeys().primaryConnectionString
output redisHostName string = redis.properties.hostName
output redisPort string = '10000'
@secure()
output redisPassword string = redisDatabase.listKeys().primaryKey
