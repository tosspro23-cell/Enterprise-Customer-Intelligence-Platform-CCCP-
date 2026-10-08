"""Real-time trigger detection: decides WHEN in a live call the commercial
decision engine (M10) should run, standing in for production module M8
(docs/architecture.md): "Trigger & debounce policy... Run the slow path
only when: a new high-priority theme appears; sentiment crosses a
threshold band; the complaint state changes; ...; a cooldown collapses
redundant triggers to the latest one per call."

Computed live, every customer turn, from the same rolling-sentiment /
active-theme state the call already maintains (call_state.py /
redis_state.py) -- not a per-turn `trigger: true` flag hand-authored into
the call script. A new scenario needs no trigger annotation: whatever
actually happens in the conversation decides when a decision gets checked,
the same way it would for a live, unscripted call.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TriggerEngine:
    """One instance per call. Each condition fires at most once per call (an
    edge, not a level) so a customer staying upset for five turns asks for
    one decision, not five -- this is the "cooldown collapses redundant
    triggers" half of the policy. The turn-count fallback is the other
    half: a slow-burn conversation with no sharp sentiment move or
    taxonomy-word hit still gets checked in on periodically, the same way a
    human agent would periodically consult the assistant rather than never
    asking at all.
    """
    negative_threshold: float = -0.40     # matches policy.PolicyConfig.live_negative_threshold (R2)
    recovery_threshold: float = 0.10
    fallback_every_turns: int = 2

    _seen_themes: set[str] = field(default_factory=set)
    _was_negative: bool = False
    _negative_fired: bool = False
    _recovery_fired: bool = False
    _turns_since_trigger: int = 0
    _has_triggered: bool = False   # the periodic fallback is a one-time safety
                                    # net, not a recurring timer -- see below

    def evaluate(self, rolling_sentiment: float | None, active_themes: list[str]) -> str | None:
        """Call once per customer turn, after this turn's sentiment/theme
        state is already folded into `rolling_sentiment`/`active_themes`.
        Returns a short machine-readable reason if a decision check should
        run now, else None."""
        self._turns_since_trigger += 1
        reason = self._check(rolling_sentiment, active_themes)
        if reason:
            self._turns_since_trigger = 0
            self._has_triggered = True
        return reason

    def _check(self, rolling_sentiment: float | None, active_themes: list[str]) -> str | None:
        # Always fold active themes into "seen" bookkeeping first, even on a
        # turn a *different* condition ends up explaining -- active_themes
        # is a rolling window (a theme stays listed for several turns after
        # it was detected, not just the one turn it first appeared), so
        # skipping this update on a turn that returns early would make an
        # already-seen theme look "new" again on some later turn.
        new_themes = [t for t in active_themes if t not in self._seen_themes]
        self._seen_themes.update(active_themes)

        if rolling_sentiment is not None:
            is_negative = rolling_sentiment <= self.negative_threshold
            if is_negative and not self._negative_fired:
                self._negative_fired = True
                self._was_negative = True
                return "sentiment_crossed_negative_threshold"
            if not is_negative and self._was_negative and rolling_sentiment >= self.recovery_threshold \
                    and not self._recovery_fired:
                self._recovery_fired = True
                return "sentiment_recovered"

        if new_themes:
            return f"new_theme:{new_themes[0]}"

        # Once *any* condition has fired, the call has been assessed at
        # least once -- that's the fallback's whole job. Re-checking every
        # N turns with no new signal from then on would just spend a real
        # ML/retrieval/narrator call on a conversation that hasn't actually
        # changed; further checks should come from a genuine edge above,
        # not the clock.
        if not self._has_triggered and self._turns_since_trigger >= self.fallback_every_turns:
            return "periodic_check"
        return None


def describe(reason: str) -> str:
    """A short human-readable gloss for a reason code, for the trace UI --
    so a viewer sees *why* this turn asked for a decision, not just that
    one happened."""
    if reason == "sentiment_crossed_negative_threshold":
        return "live sentiment just crossed the negative threshold"
    if reason == "sentiment_recovered":
        return "sentiment recovered after being negative"
    if reason == "periodic_check":
        return "no sharp signal yet -- periodic check-in"
    if reason.startswith("new_theme:"):
        return f"new topic detected: {reason.split(':', 1)[1]}"
    return reason
