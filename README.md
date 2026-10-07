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

**Live demo, running on real Azure services (not a local stand-in):**
https://cccp-workbench-ui.thankfulcliff-c5f74fed.eastus2.azurecontainerapps.io/
-- every live run spends paid cloud calls, so starting one needs the demo
link that carries `?token=...` (ask the author); without it the page loads
but runs are refused. Pick a scenario, watch the "LIVE AZURE" badge (it
names the narrator deployment and its deadline) and the per-stage latency
panel fill in as the call actually hits Speech, Language, Event Hubs,
Redis, AI Search and Azure OpenAI. Decision sub-stages are marked
"replayed from trace": they ran inside one `agent.run()` call and are
revealed afterwards with their real durations. This is a time-boxed validation deployment (see
"Cost tracking" below), not a permanent service -- if the link is down,
the local Workbench (next section) is the same UI against local stand-ins.

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
python -m unittest discover -s tests -v     # 36 contract tests (agent + reference slice)
python evals/run_evals.py                   # 27 behavioural evals, writes var/eval-report/ (untracked)
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
[docs/architecture.md](docs/architecture.md) §5). Eleven scripted calls are
included in `data/calls/` (one per synthetic customer scenario); the two that
show the core flow best:

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

**Caveat on these latency numbers (found in review, not yet re-measured).**
They were taken with the OpenAI SDK's default `max_retries=2`, and the SDK
retries timeouts -- so a call could take up to ~3x its timeout plus
backoff. The 12.64s `gpt-4.1-mini` outlier is consistent with a retried
timeout rather than (only) provider latency; that is an inference, not
verified. The adapter now disables retries and enforces the timeout as an
end-to-end deadline (`AzureOpenAINarrator`, tested with a fake slow
provider). The figures above should be re-measured (n >= 30) before the
conclusion below is relied on.

This is exactly the kind of number the architecture doc could only mark
**[Design]** before (§9.7, §15.2: "decide after the pilot measures actual
latency" / "provisioned throughput ... decide after the pilot"). It now has
one real, small and possibly retry-inflated data point: **a pay-as-you-go,
non-provisioned deployment did not reliably hit a 3-second real-time budget
in these runs**, which is the concrete argument
for the provisioned-throughput design in §15.2, not just a theoretical one.

Reproduce: deploy any Azure OpenAI model, set `AZURE_OPENAI_*`, then
`python evals/run_evals.py --narrator azure --narrator-timeout 20` for
correctness, or call `CommercialDecisionAgent.run()` directly in a loop with
a realistic `narrator_timeout_s` to measure latency.

## Concurrency smoke test against the full real backend stack

Beyond the narrator, the hot-path dependencies the architecture names --
the event stream, hot call state and guidance retrieval -- were each stood
up for real (Azure Event Hubs, Azure Managed Redis, Azure AI Search) and
driven through `tools/loadtest.py`, which runs the scripted golden call
concurrently against all four real services (`src/cccp_platform/azure_backends/`)
and reports per-stage latency. Full report:
`evals/report/loadtest_report.json`. With 6 calls per level this is a
**smoke test, not a load test**: stages measured once per call have n = 6
per level, so only median and max are meaningful (a "p95" over 6 samples is
the maximum). Event publishes (n = 120) and Redis round trips (n = 36) have
enough samples for a p95.

| Stage | median | max (or p95 where n >= 30) | n per level | concurrency tested |
|---|---|---|---|---|
| End-to-end call (full script, incl. narrator) | 31.6-32.0s | max 34.0-37.3s | 6 | 1, 3, 6 |
| Narrator (`gpt-5-mini`) | 8.2-9.2s | max 10.8-13.8s | 6 | 1, 3, 6 |
| Guidance search (Azure AI Search) | 1.5-1.6s | max 2.1-2.6s | 6 | 1, 3, 6 |
| Redis round trip (hot state update) | 0.56s | p95 0.87s | 36 | 1, 3, 6 |
| Event Hub publish | 0.34s | p95 1.0-1.6s | 120 | 1, 3, 6 |

(The committed report files predate this relabelling: their `p95` field
for the n = 6 stages equals `max`. `tools/loadtest.py` now reports
min/median/max and only emits p95/p99 at n >= 30.)

**What this does and doesn't show.** 0 errors across 18 calls (3 concurrency
levels x 6 calls each) -- every service handled 6 concurrent calls without
throttling or failing. With n = 6 per level the medians look similar from
concurrency 1 to 6, but that sample is too small to claim latency is flat
or to locate a bottleneck; the large laptop-vs-Azure difference below is the
stronger signal that the client's network path dominated. It also does not test
the concurrency the platform would actually see in production (tens of
simultaneous calls, not six) -- that needs a higher concurrency sweep,
which is a natural next step, not something this run claims to have done.

**That client-location guess was then actually tested, not just asserted**
-- see "Re-run from inside Azure" below -- and it was directionally right
but the specific number was off: Redis round trips landed around 134ms
from a same-cloud client, not the "low single-digit milliseconds" guessed
here, because the client (Container Apps, eastus2) and Redis (westus2)
ended up in different Azure regions, not the same one. Measure, don't
extrapolate -- this paragraph is left as a reminder of the gap between the
two.

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

## Re-run from inside Azure: infrastructure latency vs. model latency

The laptop-as-client caveat above was then actually tested. `apps/api/cloud_server.py`
wraps the same load test in a small HTTP service; `Dockerfile` containerises
it; a GitHub Actions workflow (`.github/workflows/build-push-ghcr.yml`)
builds and pushes it to `ghcr.io` (not Azure Container Registry -- avoids
ACR's standing per-day cost, the same tradeoff a prior project already
made); it runs on Azure Container Apps in `eastus2`, same region as Event
Hubs/Speech/Language, cross-region to Redis/AI Search/OpenAI in
`westus`/`westus2` (a subscription-wide cap of one Container Apps
environment on this free-trial account forced sharing an existing
environment from another project, in whichever region that already used).
"Same cloud, not fully same-region" -- not a clean controlled experiment,
but still a real location change to compare against the laptop run:

| Stage (p50) | From a laptop, over the internet | From Container Apps, inside Azure | Change |
|---|---|---|---|
| End-to-end call | 31.6-32.0s | 9.1-11.2s | **~3x faster** |
| Event Hub publish | ~340ms | **~5.7ms** | ~60x faster |
| Redis round trip | ~556-565ms | ~134ms | ~4x faster |
| Guidance search (AI Search) | 1.5-1.6s | ~310ms | ~5x faster |
| Narrator (`gpt-5-mini`) | 8.2-9.2s | 6.3-8.3s | **basically unchanged** |

Full report: `evals/report/loadtest_report_containerapps.json`. 0 errors
across another 18 calls.

**The point of this comparison isn't the infrastructure numbers -- it's
the one that didn't move.** Every network-bound call got dramatically
faster once the client stopped being a laptop on a home connection.
Narrator latency didn't, because it isn't network latency: a reasoning
model spends time generating hidden reasoning tokens before any visible
output, and that cost travels with the model, not the network path. Once
infrastructure placement is fixed, narrator time is still 60-80% of the
total call -- which is the same conclusion as the live-endpoint section
above (a non-reasoning model is the right choice for the real-time
profile), now reached two different ways instead of one.

Cost note: this is also, deliberately, the one component of this
exercise still running continuously rather than torn down right after
use -- see the cost-tracking section below for why and for how long.

## Cost tracking: a multi-day run on free-trial credit

This subscription is a genuine Azure Free Trial (`quotaId: FreeTrial_2014-09-01`,
spending limit on -- verified via `az rest`, not assumed) with a few days
of credit left. Per-request teardown (the pattern used for the first load
test above) was replaced with a deliberate multi-day run: Event Hubs,
Redis, AI Search, Speech, Language, Azure OpenAI and the Container App are
all being left running so real idle-plus-light-use cost over days, not
minutes, can be read back from Cost Management once it posts (billing
data lags; it was not yet available at write time). The spending-limit
protection means the realistic failure mode is the subscription being
disabled when credit runs out, not a surprise bill -- which is what makes
leaving this running for a few days a reasonable way to answer "what does
this actually cost," rather than a risk.

Two Container Apps now run in the shared environment: `cccp-workbench-app`
(headless, `apps/api/cloud_server.py` -- triggers/reports the load test
over a small JSON API; internal ingress only in `infra/main.bicep`, since
`/loadtest` spends paid calls and has no auth of its own) and `cccp-workbench-ui` (`apps/api/cloud_workbench_server.py`
-- the public link above). Same image, same secrets, different start
command (`--command`/`--args` on `az containerapp create`) -- no second
image or build pipeline needed for the second app.

None of this is covered by the automated test suite -- it requires live
credentials and real resources, so it's validated by these runs and these
reports, not by CI.

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
  text_metrics.py  word error rate of STT output vs. the scripted line
  assistant.py   minimal keyword-routed supervisor Q&A over the store + guidance index
  azure_backends/  real adapters: Event Hubs, Azure Managed Redis, Azure AI Search, Azure Speech
                   (TTS/STT round trip), Azure AI Language (sentiment), a cloud-backed call runner
apps/api/server.py        stdlib HTTP backend for the Workbench (no third-party deps)
apps/api/cloud_server.py  thin HTTP wrapper to trigger/fetch the load test from inside Azure
apps/web/            the Workbench page (vanilla HTML/CSS/JS)
tools/loadtest.py    concurrent load test against the real Azure backends
Dockerfile, .github/workflows/build-push-ghcr.yml   builds the load-test image, pushes to ghcr.io
infra/main.bicep     every Azure resource above, as code (see infra/README.md)
docs/            platform architecture (functional, technology, real-time, governance)
data/synthetic/  customers, interactions, model scores, catalog, guidance
data/calls/      the 11 scripted calls the Workbench runs
evals/           cases.json (27 scenarios), run_evals.py, report/
tests/           unit / contract tests (agent + reference slice)
```

## Claim discipline

Validated: policy/eval behaviour on synthetic scenarios with a deterministic
narrator stub (27/27 evals pass, 0 critical failures, 0 policy violations in
final text -- see `evals/report/eval_report_stub.md`); the reference slice's
event sequencing, trigger-to-decision wiring and post-call persistence
(36/36 unit/contract tests); narrator correctness against a live Azure
OpenAI endpoint (16/16 eval cases on eval suite v1.0.0, decision accuracy 1.0 -- see
`evals/report/eval_report_azure.md`); the full hot-path dependency set
(Event Hubs, Azure Managed Redis, Azure AI Search, Azure OpenAI) running
for real and holding up in a concurrency smoke test (n = 6 calls per
level, so no tail-latency claim), twice -- once from a
laptop (`evals/report/loadtest_report.json`), once from inside Azure
(`evals/report/loadtest_report_containerapps.json`), 36/36 calls
succeeded combined, 0 errors; the real-time audio/sentiment path with
real services (Azure TTS -> Azure STT -> Azure AI Language sentiment)
producing the same decision outcomes as the hand-labelled script did --
which validates the sentiment path, **not** theme tagging: live themes feed
no policy gate, and replaying the keyword tagger over the 49 scripted
customer turns still disagrees with the hand labels on 15 (19 before it
was switched to whole-word matching); those runs also used
`recognize_once()`, since replaced by continuous recognition (multi-
sentence turns could be truncated) and not yet re-run; a container image
built by CI and deployed to Azure Container Apps.

Azure AI Search, specifically, is validated as a managed dependency on the
hot path (connectivity, latency, filtering) -- it is used as a filtered
document store with `search_text="*"` and no ranking, so nothing here
measures retrieval quality.

Not validated: semantic faithfulness of narrator text beyond what a lexical
validator can see -- it rejects unsupported numbers (digits and spelled-out
percentages), other products' names, uncited output and listed prohibited
phrases, but a paraphrased promise ("you will definitely earn more") still
passes it; latency or cost at production call volume (tens of
concurrent calls, not six, which is what the real-time budget in
docs/architecture.md §9.7 is actually about); a true same-region
deployment (the Container Apps run landed in a different Azure region
from Redis/AI Search/OpenAI, not the same one -- see the comparison
section above); Web PubSub; a trained theme classifier (still a keyword
match, see `sentiment_language.py`); any production integration
(identity/OBO, a real telephony/transcript source rather than
TTS-synthesised audio, real guidance ingestion from SharePoint); the demo
supervisor router as a stand-in for a real language router; real call
audio of any kind -- every "call" here is scripted text, optionally
voiced by TTS, never a live conversation; narrator latency with retries
disabled and an end-to-end deadline (re-measure at n >= 30 still to do).

## Deliberate deviations from the target security model

Acceptable for a synthetic-data validation deployment, not for production
(docs/architecture.md describes the target):

- **Key-based auth everywhere** (OpenAI, Search, Redis, Speech, Language,
  Event Hubs SAS) instead of managed identity + RBAC.
- **Public network access** on every service; no private endpoints / VNet.
- **`GlobalStandard` deployments in US regions**, not a data-zone/regional
  deployment matched to where customer data would have to stay.
- **Shared-token gate on the public Workbench**, not real user auth
  (Easy Auth / Entra ID); rate limits are in-process and per replica.

Already narrowed in `infra/main.bicep`: Event Hubs uses a hub-scoped
**Send-only** rule (was the namespace `RootManageSharedAccessKey`); the apps
get an AI Search **query** key (was the admin key); each app receives only
the secrets it uses; the headless load-test app has internal-only ingress.
