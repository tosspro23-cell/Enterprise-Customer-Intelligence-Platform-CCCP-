# Enterprise Customer Intelligence Platform (CCCP)

CCCP is a design and reference implementation for a contact-centre intelligence
platform: a shared backbone that connects live call events, customer data,
approved guidance, existing predictive models and bounded LLM components to
support real-time agent assistance, post-call analytics and a conversational
assistant for supervisors -- without letting the LLM decide facts, eligibility
or policy.

Design rule, applied everywhere in the architecture:

> **ML predicts · policy authorises · retrieval grounds · the LLM explains · the human decides.**

Full architecture: [docs/architecture.md](docs/architecture.md).

## What's implemented here

The repository contains one fully working, tested slice of the platform: the
**Commercial Decision Agent** -- the component responsible for recommending a
product to a contact-centre customer, given their history, the live call (if
any), the existing propensity model and the approved commercial guidance.

**All data is synthetic.** No real customer, model, or guidance system is used.

| Capability | Implementation | Authority |
|---|---|---|
| Sentiment trend over a customer's history | `insights.analyse_sentiment_trend` -- OLS slope + recent-vs-earlier delta over a rolling window | deterministic, over model-scored interactions |
| Key themes in a customer's interactions | `insights.identify_key_themes` -- recency-weighted taxonomy prevalence (`recurring` / `emerging`) | deterministic |
| Product recommendation from history + an existing predictive service | customer gates -> model score -> product eligibility -> required guidance | existing ML model + versioned policy |
| Natural-language explanation | narrator (LLM), validated against the grounded context; template fallback on any violation | LLM, explanation only |

The LLM never chooses the product, never sees raw transcripts or summaries,
and cannot introduce a number, product or claim that is not already in its
grounded context.

## Run it

```bash
python -m unittest discover -s tests -v     # 25 contract tests (agent + reference slice)
python evals/run_evals.py                   # 22 behavioural evals, writes evals/report/
python demo.py cust_001                      # golden scenario, full JSON incl. evidence + trace
python demo.py cust_001 -0.7                 # same customer, live call strongly negative -> deferred
```

Stdlib only (Python >= 3.11). Optional real LLM backend:
`pip install .[azure]`, set `AZURE_OPENAI_*` (see `adapters/azure_openai.py`),
then `python evals/run_evals.py --narrator azure --narrator-timeout 20`
(adversarial-narrator cases are skipped in that mode; `--narrator-timeout`
defaults to 2.5s, matching the real-time SLA -- raise it to evaluate output
quality on its own rather than against that budget). **This has been
validated against a live endpoint** -- see the next section.

## The reference slice: Workbench demo

The agent above is a decision service with no UI. To see it inside the flow
it's designed for -- a live call with rolling sentiment, a triggered
suggestion, a post-call record, and a supervisor asking a question -- run
the reference slice:

```bash
python apps/api/server.py
# then open http://127.0.0.1:8765/
```

This spins up a scripted call simulator (`src/cccp_platform/`) that feeds
the *real* `CommercialDecisionAgent` a `LiveCallSignal` turn by turn, exactly
as the real-time stream processor would (see
[docs/architecture.md](docs/architecture.md) §5). Two scripted calls are
included:

- **`golden_savings`** -- the customer opens angry about fees (live
  sentiment -0.6 -> `deferred`, model never scored), calms down, then asks
  about a savings product -> `recommended`, with the narrator's explanation,
  citation and full evidence chain shown live.
- **`suppressed_complaint`** -- an open complaint on file suppresses any
  commercial action for the whole call, even when the customer explicitly
  asks about a product (`suppressed`, model never called).

Each call ends with a post-call record written to a local SQLite store
(`var/workbench.db`, gitignored), and the page's "supervisor assistant" box
answers session questions (call outcome counts, average sentiment, guidance
lookups) against that store and the guidance index -- a minimal, explicitly
labelled stand-in for the router/tools design in docs/architecture.md §9,
not a production assistant.

Everything in `apps/` and `src/cccp_platform/` is demo infrastructure
(stdlib `http.server`, in-memory event bus, SQLite). The only production
code path it exercises is `cccp_agent` itself.

## Validated against a live Azure OpenAI endpoint

The narrator was run against two real deployments on a dedicated Azure
OpenAI resource (not the bundled synthetic stub). Both results are real
measurements, not design estimates -- and one of them overturns a design
assumption rather than confirming it:

| Deployment | Role tested | Result |
|---|---|---|
| `gpt-5-mini` (reasoning model) | correctness of the generated explanation | **16/16 eval cases pass, decision accuracy 1.0, 0 policy violations** when given a generous timeout (`--narrator-timeout 20`) -- see `evals/report/eval_report_azure.md`. **Not viable for the real-time profile as configured**: it spends hidden "reasoning tokens" before writing any visible output (~550-650 tokens for this prompt), round-trip latency measured at 7-10.6s against the architecture's 3s real-time budget, and with the real-time profile's lean 400-token budget (docs/architecture.md §9.7) the reasoning alone can exhaust it and return **empty content with no error** -- a silent failure mode a non-reasoning model doesn't have. The agent's validator/timeout/fallback caught every one of these safely; nothing bad ever reached a result. |
| `gpt-4.1-mini` (non-reasoning model) | real-time latency budget | 3 identical calls measured **2.75s, 4.05s, 12.64s** -- network/provider latency variance alone put one of three calls over even a 3s timeout, which the agent correctly caught and fell back from. |

This is exactly the kind of number the architecture doc could only mark
**[Design]** before (§9.7, §15.2: "decide after the pilot measures actual
latency" / "provisioned throughput ... decide after the pilot"). It now has
one real data point: **a pay-as-you-go, non-provisioned deployment does not
reliably hit a 3-second real-time budget**, which is the concrete argument
for the provisioned-throughput design in §15.2, not just a theoretical one.

Reproduce: deploy any Azure OpenAI model, set `AZURE_OPENAI_*`, then
`python evals/run_evals.py --narrator azure --narrator-timeout 20` for
correctness, or call `CommercialDecisionAgent.run()` directly in a loop with
a realistic `narrator_timeout_s` to measure latency.

## Load-tested against the full real backend stack

Beyond the narrator, the hot-path dependencies the architecture names --
the event stream, hot call state and guidance retrieval -- were each stood
up for real (Azure Event Hubs, Azure Managed Redis, Azure AI Search) and
driven through `tools/loadtest.py`, which runs the scripted golden call
concurrently against all four real services (`src/cccp_platform/azure_backends/`)
and reports latency percentiles per stage. Full report:
`evals/report/loadtest_report.json`.

| Stage | p50 | p95 | concurrency tested |
|---|---|---|---|
| End-to-end call (full script, incl. narrator) | 31.6-32.0s | 34.0-37.3s | 1, 3, 6 |
| Narrator (`gpt-5-mini`) | 8.2-9.2s | 10.8-13.8s | 1, 3, 6 |
| Guidance search (Azure AI Search) | 1.5-1.6s | 2.1-2.6s | 1, 3, 6 |
| Redis round trip (hot state update) | 0.56s | 0.87s | 1, 3, 6 |
| Event Hub publish | 0.34s | 1.0-1.6s | 1, 3, 6 |

**What this does and doesn't show.** 0 errors across 18 calls (3 concurrency
levels x 6 calls each) -- every service absorbed 6 concurrent calls without
throttling or failing, and latency stayed essentially flat from concurrency
1 to 6. That flatness is itself the finding: **at this scale, none of these
managed services were the bottleneck** -- the client (this laptop, not an
Azure-region service) was, over the public internet. A same-region
Container App would see Redis round trips closer to low single-digit
milliseconds, not ~560ms; this setup cannot measure that, only confirm the
dependencies behave correctly under modest concurrency. It also does not
test the concurrency the platform would actually see in production (tens
of simultaneous calls, not six) -- that needs a same-region client and a
higher concurrency sweep, which is a natural next step, not something this
run claims to have done.

**Two things broke before this worked, both instructive:**
- Classic "Azure Cache for Redis" is being retired; the create call must
  target **Azure Managed Redis** (`az redisenterprise`) instead -- which is
  what docs/architecture.md already specified, so this confirmed the
  design choice rather than changing it.
- Azure Managed Redis defaults to `OSSCluster` policy, which routes clients
  directly to internal shard IPs that don't match the cluster's TLS cert,
  and still requires Redis Cluster hash-tagged keys for any multi-key
  pipeline. Recreating the database with `EnterpriseCluster` policy (a
  proxy in front of the shards) fixed the TLS problem; the hash-tag
  requirement for multi-key atomicity remains either way and is reflected
  in `redis_state.py`.

Cost note: Event Hubs, Redis and AI Search (Basic -- this subscription's
free-tier AI Search quota was already used by another project) were
deleted immediately after this run; only the pay-as-you-go Azure OpenAI
resource was kept. None of this is covered by the automated test suite --
it requires live credentials and real resources, so it's validated by this
run and this report, not by CI.

## Layout

```
src/cccp_agent/
  domain.py      typed immutable domain contracts
  ports.py       interfaces to the customer store / propensity model / guidance index / narrator
  insights.py    sentiment trend + themes (deterministic)
  policy.py      commercial-policy-v1 (gates before scoring, eligibility after)
  narrative.py   narrator payload, output validator, template fallback
  agent.py       bounded workflow + evidence + trace spans + degradation
  adapters/      synthetic estate with fault injection; Azure OpenAI adapter
src/cccp_platform/
  events.py      the domain event envelope + sequencing
  call_state.py  hot call state (rolling sentiment, active themes)
  processor.py   scripted call simulator -> calls the real CommercialDecisionAgent
  postcall.py    builds the post-call analytical record
  store.py       SQLite-backed analytical store (production-shaped table names)
  assistant.py   minimal keyword-routed supervisor Q&A over the store + guidance index
  azure_backends/  real adapters: Event Hubs, Azure Managed Redis, Azure AI Search, a cloud-backed call runner
apps/api/server.py   stdlib HTTP backend for the Workbench (no third-party deps)
apps/web/            the Workbench page (vanilla HTML/CSS/JS)
tools/loadtest.py    concurrent load test against the real Azure backends
docs/            platform architecture (functional, technology, real-time, governance)
data/synthetic/  customers, interactions, model scores, catalog, guidance
data/calls/      the two scripted calls the Workbench runs
evals/           cases.json (22 scenarios), run_evals.py, report/
tests/           unit / contract tests (agent + reference slice)
```

## Claim discipline

Validated: policy/eval behaviour on synthetic scenarios with a deterministic
narrator stub (22/22 evals pass, 0 critical failures, 0 policy violations in
final text -- see `evals/report/eval_report_stub.md`); the reference slice's
event sequencing, trigger-to-decision wiring and post-call persistence
(25/25 unit/contract tests); narrator correctness against a live Azure
OpenAI endpoint (16/16 eval cases, decision accuracy 1.0 -- see
`evals/report/eval_report_azure.md`); the full hot-path dependency set
(Event Hubs, Azure Managed Redis, Azure AI Search, Azure OpenAI) running
for real and holding up under light concurrency (18/18 calls succeeded, 0
errors, at concurrency 1/3/6 -- see `evals/report/loadtest_report.json`
and the section above).

Not validated: latency or cost at production call volume -- 6-way
concurrency from a non-Azure client over the public internet, not tens of
concurrent calls from a same-region service, which is what the real-time
budget in docs/architecture.md §9.7 is actually about; Web PubSub or
Container Apps (still a local stdlib server and in-process push); any
production integration (identity/OBO, real guidance/transcript sources);
the demo supervisor router as a stand-in for a real language router.
