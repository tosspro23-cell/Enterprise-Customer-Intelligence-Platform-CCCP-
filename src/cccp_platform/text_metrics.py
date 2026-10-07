"""Transcript-quality metrics for the voice path: word error rate of what
STT recognised against the scripted reference line. Stdlib only, so it is
unit-testable without the Speech SDK."""
from __future__ import annotations

import re

_WORD_RE = re.compile(r"[a-z0-9']+")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower().replace("’", "'"))


def word_error_rate(reference: str, hypothesis: str) -> float | None:
    """(substitutions + deletions + insertions) / reference words, case- and
    punctuation-insensitive. None when the reference has no words."""
    ref, hyp = _words(reference), _words(hypothesis)
    if not ref:
        return None
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return round(prev[-1] / len(ref), 3)
