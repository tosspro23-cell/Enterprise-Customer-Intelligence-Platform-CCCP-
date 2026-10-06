"""Reference vertical slice: a scripted call simulator wired to the real
`cccp_agent.CommercialDecisionAgent`, with post-call persistence and a
minimal supervisor assistant.

Nothing here reimplements the decision logic -- it feeds the same agent a
`LiveCallSignal` the way the real-time stream processor (module M10 in
docs/architecture.md) would, so the demo exercises production decision code,
not a simplified stand-in for it.

This package simulates the infrastructure around that decision (event bus,
hot call state, hand-off to post-call) with stdlib-only, in-process
equivalents, each labelled with what it stands in for in production.
"""
