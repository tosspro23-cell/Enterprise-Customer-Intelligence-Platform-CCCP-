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
python -m unittest discover -s tests -v     # 15 contract tests
python evals/run_evals.py                   # 22 behavioural evals, writes evals/report/
python demo.py cust_001                      # golden scenario, full JSON incl. evidence + trace
python demo.py cust_001 -0.7                 # same customer, live call strongly negative -> deferred
```

Stdlib only (Python >= 3.11). Optional real LLM backend:
`pip install .[azure]`, set `AZURE_OPENAI_*` (see `adapters/azure_openai.py`),
then `python evals/run_evals.py --narrator azure` (adversarial-narrator cases
are skipped in that mode; the Azure adapter has **not** been validated
against a live endpoint).

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
docs/            platform architecture (functional, technology, real-time, governance)
data/synthetic/  customers, interactions, model scores, catalog, guidance
evals/           cases.json (22 scenarios), run_evals.py, report/
tests/           unit / contract tests
```

## Claim discipline

Validated: policy/eval behaviour on synthetic scenarios with a deterministic
narrator stub (22/22 evals pass, 0 critical failures, 0 policy violations in
final text -- see `evals/report/`).
Not validated: real LLM quality, latency at scale, any production integration.
