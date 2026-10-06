# CCCP Architecture

This document describes the target architecture for the Enterprise Customer
Intelligence Platform (CCCP). It is the design behind the reference agent
implemented in this repository (see [../README.md](../README.md)); most of
the platform below is a design target, not yet built -- the status of each
part is called out explicitly rather than implied.

## 1. What CCCP is

CCCP is a shared backbone for a contact centre: it connects live call events,
customer data, approved guidance, existing predictive models, deterministic
business rules and bounded LLM components to support three things on one
platform instead of three disconnected ones:

1. **Real-time call assistance** -- live sentiment, theme detection and a
   triggered, grounded suggestion pushed to the agent desktop.
2. **Post-call intelligence** -- summary, tags, sentiment trajectory, an NPS
   proxy and quality signals, written to the analytical store; corpus-level
   topic discovery and guidance-gap analytics.
3. **A conversational assistant** -- a router and a small number of
   specialist agents with typed, read-only tools over the same data, guidance
   and decision services, for supervisors and analysts.

Design rule, enforced throughout:

> **ML predicts · rules authorise · retrieval grounds · the LLM interprets and explains · the human decides.**

**Integrate, don't replace.** The contact-centre platform stays the
telephony/recording system. The data warehouse stays the analytical system of
record. The existing ML estate stays authoritative for propensity/risk
scores. The document system holding approved guidance stays the source of
truth. CCCP is the layer that connects them, not a replacement for any of
them.

**Buy before build.** Commodity capability -- speech-to-text, PII redaction,
generic summarisation, vector search, an AI gateway, monitoring -- is
consumed as a managed service. Custom code is reserved for what actually
differentiates one deployment: the decision logic that combines live
context with an existing model and a rule set, the guidance-improvement
loop, and governed cross-source assistance.

## 2. Principles

1. **Architecture-first, implementation-backed.** Diagrams and code share
   contracts; nothing appears in the design just because it is a popular
   service.
2. **Separate intelligence responsibilities, explicitly.**

   | Mechanism | Owns | Never authoritative for |
   |---|---|---|
   | Traditional ML | propensity, churn/NPS risk, utterance classification | policy, permission |
   | Deterministic rules / analytics | eligibility, timing, permissions, KPI arithmetic | open-ended language |
   | Retrieval | approved guidance and interaction evidence | scores, policy override |
   | LLM / agents | interpretation, summarisation, bounded planning, explanation | product choice, eligibility, facts not in context |

3. **Source of truth vs. derived state.** The guidance system owns guidance;
   the warehouse/source systems own customer facts; the ML service owns the
   score; an LLM narrative is derived and must cite what it is based on.
4. **The human retains business authority.** The system recommends; the
   agent (or supervisor) acts.
5. **Fast path vs. slow path.** Cheap signals run continuously; expensive
   generation only runs on a trigger.
6. **Bounded tools.** Typed I/O, read-only by default, timeouts, tracing,
   explicit error contracts.
7. **Evidence before eloquence.** Every output can answer: which data, which
   model version, which rule, which guidance version, which prompt profile.
8. **Explicit degradation.** A dependency failure removes the dependent
   capability; it never triggers fabrication.
9. **Buy before build.** Managed/SaaS capability is the default; custom code
   must justify itself by differentiation or by a genuine capability gap.
10. **Authorisation is deterministic and enforced at the data layer.** No LLM
    decides who may see what.
11. **Compliance by design.** Regulatory constraints shape the functional
    design (e.g. no inference of a worker's emotional state), not just the
    documentation written about it afterwards.
12. **No agent framework in the hot path.** The real-time path is a stream
    processor with one bounded LLM call, not an agent loop.

## 3. Functional architecture

```
Contact-centre platform
  ├─ telephony/media ──► Media Ingest & STT ──► Stream Processor (fast path)
  │                                                 │        │
  │                                                 ▼        ▼
  │                                        Domain event stream   Hot call state
  │                                                 │
  │                                                 ▼
  │                                           Trigger policy
  │                                        ┌────────┼────────┐
  │                                        ▼        ▼        ▼
  │                             Commercial Decision   Guidance   Suggestion
  │                             Service (rules + ML)  Retrieval  Composer (LLM)
  │                                        └────────┬────────┘
  │                                                  ▼
  │                                            Push channel ──► Agent desktop
  │
  └─ recordings + metadata ──► Post-call Enrichment ──► Analytical store
                                                     └─► Call evidence index
                                                           │
                                                           ▼
                                        Topic discovery & guidance-gap analytics
                                                           │
                                                           ▼
                                   Guidance-improvement review workflow ──► Guidance source of truth
                                                           │
                                                           ▼
                                                  Guidance ingestion (versioned index)

Conversational assistant: Teams/chat ──► Router agent ──► Specialist agents ──► Typed tool layer
                                                                                       │
                                                                     (same stores and services as above, via OBO identity)

Shared governance plane (used by all three paths):
  identity · AI gateway · evaluation harness · evidence model · telemetry · rule/version registries
```

### Component responsibilities

| Component | Responsibility | Used by |
|---|---|---|
| Media Ingest & STT | Receive audio/transcripts, per-channel STT, PII redaction, emit utterance events | real-time, post-call (transcript reuse) |
| Stream Processor | Ordering, idempotency, fast-path signals, state updates, trigger evaluation | real-time |
| Customer Context Service | Read-only, compact customer context; prefetch at call start | real-time, assistant |
| Commercial Decision Service | Rules + existing ML scores -> decision with evidence (implemented in this repo) | real-time, assistant |
| Guidance Retrieval | Versioned, filtered hybrid retrieval with citations | real-time, assistant, post-call adherence |
| Suggestion Composer | LLM generation in a fixed schema; validation; template fallback | real-time |
| Post-call Enrichment | Summary, tags, trajectory, NPS proxy, quality signals -> analytical store + index | post-call -> assistant |
| Topic discovery & guidance-gap analytics | Corpus clustering, LLM naming, analyst review, guidance proposals | post-call |
| Assistant router + agents | Intent routing, specialist reasoning, response composition | assistant |
| Typed tool layer | The only path from agents to data/services; identity-aware | assistant |
| Shared governance plane | Identity, AI gateway, prompt/rule/model registries, evals, traces, cost | all |

**Why one platform.** All three paths share the same customer context, rule
set, ML adapters, guidance index, evidence model and gateway. Three separate
applications would duplicate guidance ingestion, customer access logic,
model integration, security policy and evaluation suites -- and would let
the real-time copilot and the conversational assistant give different
answers to the same question. The paths stay separate only where latency and
execution characteristics actually differ.

## 4. Technology mapping (status: design target)

| Capability | Technology choice | Why |
|---|---|---|
| Media ingest | long-lived WebSocket service on a managed container platform | a serverless function is a poor fit for a long-lived bidirectional stream |
| Speech-to-text | the contact-centre platform's own transcript where quality/latency/languages are adequate; a managed STT service otherwise | avoid paying twice for STT; decide with a measured bake-off |
| PII redaction | a managed language-PII service + platform secure-pause for payment data | commodity, compliance-sensitive |
| Domain event stream | a managed event-streaming service, partitioned by call id | ordered per call, replayable, multiple consumers; carries domain events, never audio |
| Hot call state | a managed in-memory store (TTL per call) | sub-millisecond state that survives consumer rebalance; no operational database needed in the hot path |
| Stream processor & services | containerised services, scale-to-zero, autoscaled on stream lag | managed containers, no idle cost for non-hot services |
| Push to agent desktop | a managed WebSocket fan-out service -> embedded desktop widget | no self-managed realtime hub |
| LLM access | a managed LLM platform, tiered model profiles, behind a gateway | enterprise terms, data-residency control, quota management |
| AI gateway | an API-management layer with GenAI-aware policies | token quotas per use case, load balancing, circuit breaking, cost metrics |
| Guidance index | a hybrid (keyword + vector) search service, fed from the guidance source of truth | metadata filters, security trimming, citations |
| Call evidence index | the same search service, or the warehouse's native search if one copy of transcript data is preferred | trade-off is one copy + one governance model vs. shared tooling with guidance search |
| Analytical record | the existing data warehouse | stays the system of analytical record; semantic views for governed KPIs |
| Batch enrichment | an async/batch LLM API, orchestrated by the existing batch job scheduler | post-call is latency-tolerant; batch pricing is materially cheaper |
| Topic discovery | the existing ML compute platform (embeddings + clustering) | reuses existing skills and compute |
| Existing ML models | unchanged, served by the existing model-serving layer | reused through a typed adapter; feature schemas never change |
| Assistant channel | the existing chat platform's bot framework + SSO | native to the channel the client already uses |
| Agent runtime | a managed agent-hosting service; self-hosted orchestration kept as an explicit fallback | matches a managed/low-ops preference; tool contracts stay runtime-agnostic either way |
| Prompt / model / eval registry | the existing MLOps platform + repo-versioned prompts | no new registry product |
| Observability | the existing monitoring stack via OpenTelemetry (GenAI semantic conventions) | one trace across the stream, tools and LLM calls |

**Deliberately not used:** a work-queue service in the hot path (the event
stream already gives ordering, replay and multi-consumer fan-out); an
operational database for live state (the in-memory store covers hot state,
the warehouse covers analytics); an agent framework in the real-time path
(latency and determinism); a single vector store for everything (structured
facts stay in the warehouse).

## 5. Real-time call processing

### Modules

| # | Module | Responsibility |
|---|---|---|
| M1 | Telephony/media connector | Accept audio sessions, split customer/agent channels, handle reconnects |
| M2 | Streaming ASR client | Per-channel STT, partial + final hypotheses; partials drive UI only, finals drive logic |
| M3 | PII redactor | Redact card numbers, IDs, etc. on final utterances before persistence or any LLM call |
| M4 | Event publisher | Emit versioned domain events with a sequence number and trace id |
| M5 | Call state manager | Hot state; idempotency by event id; ordering by sequence number |
| M6 | Fast-path signal engine | Utterance sentiment, rolling sentiment, taxonomy theme, complaint/retention detectors -- small models, text-only, under ~150ms |
| M7 | Context prefetcher | On call start: customer profile, products, open cases, cached model scores -- avoids a warehouse query per utterance |
| M8 | Trigger & debounce policy | Decides when the slow path (decision + retrieval + LLM) runs |
| M9 | Guidance retriever | Filtered hybrid retrieval with version/effective-date filters; explicit "not found" |
| M10 | Commercial Decision Service | Customer gates -> model scores -> eligibility -> guidance requirement (the logic implemented in this repo) |
| M11 | Suggestion composer | LLM in a fixed schema; validator; template fallback |
| M12 | Agent desktop push | Per-session fan-out to an embedded widget |
| M13 | Feedback capture | Accept/dismiss/edit events from the agent, used as an online quality signal |
| M14 | Call finaliser | On call end, hand the transcript + state to post-call processing; release hot state |

### Event flow (abridged)

A call starts -> context is prefetched into hot state -> each final
utterance is redacted, published, and scored for sentiment/theme in the fast
path -> if a trigger fires (new high-priority theme, a sentiment threshold
crossed, a complaint-state change, an explicit agent request), the slow path
runs: the Commercial Decision Service decides, guidance is retrieved, the
LLM composes a suggestion against that grounded context, and the result is
pushed to the desktop. The call ending hands the transcript and final state
to post-call processing and releases hot state.

### Trigger policy

Run the slow path only when: a new high-priority theme appears; sentiment
crosses a threshold band; the complaint state changes; the customer asks a
policy/product question; the agent explicitly asks for a suggestion; or the
commercial state becomes eligible. A cooldown collapses redundant triggers to
the latest one per call.

### Degradation matrix

| Failure | Behaviour |
|---|---|
| STT/transcript lag | show "transcript delayed"; suspend triggers; no suggestion on stale state |
| Context/score service slow at call start | proceed without the commercial section; retry once in the background |
| Scoring model down | commercial status = `unavailable`; a score is never inferred |
| Guidance index down / zero results | "no approved guidance found"; no policy claim is made |
| LLM timeout / invalid output | show the retrieved guidance excerpt + deterministic alerts |
| Push channel disruption | client reconnects and replays last state from hot storage |
| Event backlog | prioritise state + sentiment; collapse suggestion triggers; post-call is unaffected |

## 6. Guidance ingestion and retrieval

Guidance documents are extracted from their source of truth, parsed into
sections, chunked with metadata (document id, version, effective/expiry
date, product/region/segment tags, approval status, ACL) and indexed into a
staging index. A retrieval regression suite (known questions -> expected
sections) runs automatically; on pass, an index alias is promoted and the
previous version is retained for rollback and traceability. Retrieval is
hybrid (keyword + vector) with semantic re-ranking, hard filters applied
before ranking, and an explicit `not_found` result rather than a guess.

## 7. Traditional ML integration

Existing propensity/risk models stay authoritative and are called through a
typed port with their own fixed feature schema. **Live-call context is never
injected into an approved model** -- that would silently shift its input
distribution without validation. Live context acts only in the rules layer
(timing and permission). If live-call signals prove predictive over time,
they become candidate features for a new, separately validated model
version, trained on the labelled data this platform produces.

NPS estimation is maturity-leveled explicitly (0: unlabelled heuristic proxy,
never shown as NPS; 1: a classifier validated on a labelled sample; 2: an
approved supervised model with calibration/drift monitoring), and every
dashboard stores which level produced a given number so levels are never
mixed silently.

## 8. Post-call intelligence

Flow: call end or recording available -> normalise turns (reusing the
real-time transcript when quality allows, batch STT otherwise) -> redact PII
-> in parallel, LLM structured extraction (summary, tags, actions, outcome)
and a sentiment-trajectory classifier -> validate against the source
evidence -> write facts to the analytical store and the call evidence index.
From there: corpus-level topic discovery (embeddings + clustering on
redacted summaries, reviewed by an analyst before a new theme enters the
taxonomy) and guidance-gap analytics (aggregated evidence -- never a single
call -- drives an LLM-drafted guidance proposal that a human content owner
reviews and publishes; this platform never writes to the guidance source of
truth automatically).

Agent service quality is a per-dimension profile with evidence turns, not
one opaque score, combining deterministic metrics (hold time, transfers,
mandatory disclosure) with a rubric-based LLM assessment validated against
human QA labels. It is positioned as a coaching aid with mandatory
supervisor review -- never an automated employment decision.

## 9. Conversational assistant

Multi-agent decomposition is used only where it buys something measurable:
a separate reasoning loop, separate data authority, or a separate evaluation
set. Everything else is a tool, not an agent.

| Component | Type | Why |
|---|---|---|
| Router | agent | classifies intent, plans multi-step requests, composes the final answer with citations |
| Customer Interaction agent | agent | multi-step: resolve customer -> fetch interactions -> aggregate -> summarise with evidence |
| Analytics agent | agent | plan -> select a governed metric -> query -> sanity-check -> explain; the most error-prone route, with its own eval set |
| Commercial agent | bounded workflow (agent) | the same decision logic as the Commercial Decision Service, with an LLM explanation |
| Guidance search | tool | a single retrieval call + citation; no reasoning loop |
| KPI lookup, customer profile, call search | tools | deterministic reads |

All tools are typed and read-only; none accepts free-form SQL, shell, or
URLs. Identity is propagated end-to-end: the assistant's own token is
exchanged on-behalf-of the user for every tool call, so **the data layer
enforces authorisation, not the router.** An unauthorised request returns no
rows, and the answer says so -- the router never makes an access decision.

## 10. Data architecture

| Class | Examples | Store |
|---|---|---|
| Hot operational | live call state, utterance buffer | managed in-memory store, short TTL |
| Event stream | domain events | managed event stream, short retention + optional capture |
| Raw artifacts | recordings, raw transcripts | object storage with a retention policy |
| Analytical | enrichment facts, KPIs | the existing data warehouse |
| Knowledge | guidance chunks, call evidence | hybrid search index(es), versioned |
| AI lifecycle | prompts, eval sets, eval runs, model profiles | version control + the existing MLOps platform |
| Telemetry | traces, metrics | the existing monitoring stack, pseudonymous |

## 11. Model strategy and AI gateway

Model profiles are capability-based, not hard-coded to a specific model
name: a fast/small profile for real-time suggestions and explanations
(provisioned capacity, strict output caps, streamed), a medium/strong batch
profile for post-call extraction, and a standard pay-as-you-go profile for
the assistant. Concrete model choices are made by evaluation at build time
(quality, structured-output reliability, latency, cost, regional
availability), not from a leaderboard.

The AI gateway is mandatory, not optional: per-profile token quotas and rate
limits, cost attribution per use case, a backend pool with failover,
circuit breaking, and request/response logging without payload content by
default. Real-time suggestion tokens dominate variable cost in volume, so the
trigger policy, prompt caching and a small real-time model are the primary
cost levers -- not the choice of model for the assistant.

**Reasoning models need their own line in this table, not a shared one.**
Measured against a live deployment (see [../README.md](../README.md#validated-against-a-live-azure-openai-endpoint)):
a reasoning-family model (`gpt-5-mini`) spends several hundred hidden
"reasoning" tokens before writing any visible output, which (a) makes
round-trip latency unsuitable for the real-time profile's 3s budget even
though output quality and grounding were correct in every case tested, and
(b) can silently return empty content -- not an error -- if the real-time
profile's lean token budget (§9.7) doesn't also cover that hidden spend. A
non-reasoning model is the correct default for the real-time and commercial
explanation profiles; a reasoning model is a legitimate choice for the
assistant or post-call synthesis profiles, where the latency budget can
absorb it. Even a non-reasoning model, pay-as-you-go and unprovisioned,
showed real latency variance across three otherwise-identical calls (2.75s,
4.05s, 12.64s) -- one data point for the provisioned-throughput argument in
§15.2, not a substitute for measuring it at the pilot's actual trigger rate.

## 12. LLMOps / lifecycle management

One release unit covers application code, prompt templates, the model
profile, tool schemas, the rule set version, the retrieval config and index
alias, the taxonomy version, the evaluation datasets, the safety
configuration and infrastructure-as-code. Every response trace records the
exact versions that served it.

Lifecycle: change -> PR -> unit/contract tests -> offline evals (critical
cases are a hard gate) -> security/IaC checks -> staging replay against
recorded calls -> human approval -> canary in production -> online
monitoring -> automatic rollback on regression, or full rollout -> feedback
and new labels feed the next round of evals.

Never automated: changing customer policy, publishing guidance, promoting a
retrained model, enabling a new offer, or changing eligibility rules. Those
always require a human approval step.

## 13. Security, privacy and regulatory design

**Security:** managed identity for service-to-service calls; the AI gateway
as the only egress to models; least-privilege, on-behalf-of access for
user-driven reads; tool allowlists; prompts separate instructions from data;
retrieved and transcript text is always treated as untrusted; output
validation; pseudonymous identifiers in telemetry; no payload logging by
default.

**Data protection:** a data-protection impact assessment before any pilot
that involves systematic monitoring of customers and employees;
minimisation by design (the Commercial Decision Agent never forwards free
text to the narrator, only structured, data-minimised facts); separate
retention policies per data class; an erasure-request event that fans out to
every store holding a subject's data, with a completion record per store.

**Regulatory design constraints:**

| Concern | Design response |
|---|---|
| No inference of an employee's emotional state | customer sentiment is computed from text, never from voice prosody; agent-side analysis is limited to behavioural/process metrics |
| AI used to evaluate worker performance is a sensitive use case | agent-quality output is a coaching aid with mandatory supervisor review; no automated employment decisions; workers are informed before deployment |
| Transparency | generated content is always labelled as such; a suggestion is never read verbatim to a customer without the agent's own judgement |

**Prompt injection:** transcripts and documents are untrusted by default.
The primary control is architectural, not a filter: the decision is made
*before* the LLM is ever called, the LLM receives only structured,
pre-approved facts (never a raw summary), and its output is validated
against that exact context -- so even a successful injection inside a
transcript has nothing it can change.

## 14. Reliability and observability

Failure classes handled explicitly: stream interruption, STT delay,
duplicate/out-of-order events, dependency timeouts, retrieval failure,
invalid LLM output, persistence failure, stale index, burst backlog.
Patterns: timeouts everywhere, bounded retries with jitter on idempotent
operations only, circuit breakers, idempotency keys, sequence numbers,
dead-letter handling for post-call jobs, explicit degraded-state UI, and
load/failure testing at multiples of expected peak.

Every trace is rooted at a call id and carries: media/STT spans, fast-path
inference spans (with model versions), context/score reads, rule
evaluations (rule set version, rule ids), retrieval (index alias, chunk
ids/versions), the LLM call (profile, tokens, latency, validation result),
the push + any agent feedback, and post-call enrichment. An evidence object
always names its kind, source system, source id/version and a short detail
string -- this is the same evidence model the Commercial Decision Agent
emits today (see `domain.Evidence` and the `evidence` field on
`DecisionResult`).

## 15. What is implemented vs. what is design

Two things are implemented and tested in this repository; everything else
in this document is a design target.

**The Commercial Decision Agent** (`src/cccp_agent/`, modules M10/M11
above, and the Commercial agent's tool in the assistant): given a customer,
their interaction history, an optional live-call signal, the existing
propensity model and the approved guidance index, it produces a
recommendation (or a reason it withheld one) with full evidence. See
`domain.py` (contracts), `ports.py` (interfaces), `insights.py`
(sentiment/theme analytics), `policy.py` (the versioned rule set),
`narrative.py` (LLM payload + validator + template fallback), `agent.py`
(the orchestrating workflow with tracing and degradation). The same `run()`
call is designed to be invoked from three places without any change to its
logic: module M10 in the real-time path (with a `LiveCallSignal` attached),
the Commercial agent's `commercial.decide` tool in the assistant, and the
post-call pipeline for outcome analytics -- which is the point of keeping
this logic in one tested module rather than duplicating it per channel.

**A reference vertical slice** (`src/cccp_platform/`, `apps/`): a scripted
call simulator standing in for M1-M9 (media ingest, STT, the fast-path
signal engine, hot state, the trigger policy), driving the real
`CommercialDecisionAgent` exactly as the production stream processor would;
a stdlib HTTP backend pushing the resulting events to a browser page (the
Workbench -- standing in for M12, the agent-desktop push, with the
Workbench itself playing the role the design always gave it: a reference/
demo surface, never the production UI); a post-call step that persists an
enrichment record to a local SQLite store using the same fact-table names
production would use; and a minimal keyword-routed Q&A standing in for the
assistant's router + tools (§9) -- real typed tools over the same store and
guidance index, but a keyword match instead of a hosted language-model
router, and labelled as such in its own answers.

See [../README.md](../README.md) for how to run both.

**A third piece swaps the event stream, hot state and guidance retrieval
for the real managed services** (`src/cccp_platform/azure_backends/`,
driven by `tools/loadtest.py`): real Event Hubs, real Azure Managed Redis,
real Azure AI Search, alongside the already-validated Azure OpenAI. This
is the one part of the reference slice that is not a stand-in -- it is the
actual production dependency, called from a demo client. See
[../README.md](../README.md#load-tested-against-the-full-real-backend-stack)
for what it found (every dependency held up under light concurrency; the
measured latency says more about this client not being co-located with
Azure than about the services themselves) and what it still doesn't prove
(production concurrency, from a same-region client).

**What this does not prove:** real-time latency at *production* volume
(tens of concurrent calls from a same-region service, not six from a
laptop), a real STT service, a real warehouse, a real agent runtime, or a
production authorisation/identity flow. The reference slice exists to make
the decision boundaries and the event/state model legible end to end, and
now to confirm the chosen managed services actually work the way §4 and
§8 assume -- not to validate production-scale technology choices on its own.

## 16. Key trade-offs

| Decision | Chosen | Alternative | Why |
|---|---|---|---|
| Intelligence split | mixed (ML / rules / retrieval / LLM) | LLM for everything | cost, latency, authority, auditability |
| Build scope | managed services + a small amount of differentiating code | a fully custom platform | matches a SaaS/low-ops preference |
| Real-time orchestration | a stream processor + one bounded LLM call | an agent loop | latency, determinism |
| STT | the contact-centre platform's transcript if adequate | always run your own STT | avoid paying twice |
| Hot state | an in-memory store | an operational database | latency, rebalance safety, fewer moving parts |
| Analytics in the assistant | governed semantic metrics | free text-to-SQL | correctness, authorisation |
| Delivery order | post-call before real-time | real-time first | lower integration risk, faster KPI value, builds the labelled corpus real-time needs |
| Sentiment | text-based classifiers | voice-prosody emotion inference | latency, cost, and the regulatory constraint in §13 |
| Commercial authority | the human agent decides | an autonomous commercial action | policy risk, customer impact |

## 17. Risks

| Risk | Mitigation |
|---|---|
| Real-time latency above budget | triggers, cached context, a small real-time model, a guidance-only fallback |
| Model cost above budget | trigger policy, prompt caching, batch pricing for post-call, a cost-per-call SLO |
| Stale or wrong guidance shown | versioned index, a promotion gate, a freshness SLI, mandatory citations |
| The LLM fabricates an offer or a number | the decision is made before the LLM runs; a tested validator + template fallback catch the rest |
| Prompt injection | no free text reaches the LLM where it isn't needed; read-only tools; the decision never depends on the LLM's output |
| Agent-quality analytics seen as surveillance | regulatory-aware design, coaching framing, early consultation with those affected |
| An NPS proxy is treated as a fact | the maturity level is stored and displayed with every estimate |
| Noisy topic clusters pollute the taxonomy | an analyst review gate before a new theme is accepted |

## 18. Glossary

OBO: on-behalf-of token flow. PTU: provisioned throughput. AHT: average
handling time. FCR: first-contact resolution. NBA: next-best action. DPIA:
data-protection impact assessment.
