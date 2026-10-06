"""Real Azure-backed implementations of the ports cccp_agent and
cccp_platform already define as protocols -- not a new architecture, just
swapping the synthetic/in-process stand-ins for the managed services
docs/architecture.md names for each one:

  search_guidance.AzureSearchGuidanceIndex  -> GuidanceIndexPort via Azure AI Search
  event_hub_bus.EventHubSink/EventHubReader -> the domain event stream via Event Hubs
  redis_state.RedisCallState                -> hot call state via Azure Cache for Redis

Nothing in cccp_agent changes to use these -- that is the point of the
ports/adapters boundary. Requires `pip install azure-search-documents
azure-eventhub redis` and the AZURE_SEARCH_*, EVENTHUB_*, REDIS_* env vars
(see .env.azure, not committed).
"""
