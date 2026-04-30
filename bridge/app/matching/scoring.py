"""Within-channel scoring + cross-channel composition.

Implements CLAUDE.md §13.2 (within-channel) and §13.3
(cross-channel composition). Pure compute — no I/O. Inputs are arrays
of similarities, quality, uniqueness; output is bounded [0, 1].

Phase 4 ships channel A only. Channels B and C arrive in Phase 5.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


_EPS = 1e-9


@dataclass
class ChannelScore:
    """Result of within-channel scoring. The aggregate `S` is what feeds
    cross-channel composition; the components are surfaced for debug."""
    S: float
    E: float                # evidence-union (frame-level only; 0.0 for aggregate channels)
    count_conf: float       # 1 - exp(-Σw / k); 1.0 for aggregate channels
    dist_q: float           # 0.5 + 0.5 * H(m_i')/H_max; 1.0 for aggregate channels
    m_primes: list[float]   # sharpened per-image sims (frame-level) or [m_B'] (aggregate)


def sharpen(m: float, baseline: float, gamma: float) -> float:
    """Sharpen a similarity against a per-channel noise floor.

    `m_i' = max(0, (m - baseline) / max(eps, 1 - baseline)) ** gamma`

    A noise-floor sim becomes ~0; a strong sim is preserved (slightly
    diminished). Bounded [0, 1].
    """
    denom = max(_EPS, 1.0 - baseline)
    raw = (m - baseline) / denom
    if raw <= 0.0:
        return 0.0
    if raw >= 1.0:
        return 1.0
    return raw ** gamma


def score_frame_channel(
    per_image_max_sims: list[float],
    qualities: list[float],
    uniquenesses: list[float],
    baseline: float,
    gamma: float,
    count_k: float,
) -> ChannelScore:
    """Channel score for frame-level channels (A, C).

    `per_image_max_sims[i]` is `m_i = max_j sim(stash_j, ext_i)` — caller
    has already collapsed the M dimension. The three lists must be aligned
    by index `i`.

    Empty input → `S = 0`. Channels with all `q_i*c_i == 0` (every image
    fully suppressed) also produce `S = 0` since the evidence-union is
    `1 - prod(1 - 0) = 0`.
    """
    n = len(per_image_max_sims)
    if n == 0 or len(qualities) != n or len(uniquenesses) != n:
        return ChannelScore(S=0.0, E=0.0, count_conf=0.0, dist_q=0.5, m_primes=[])

    m_primes = [sharpen(m, baseline, gamma) for m in per_image_max_sims]
    weights = [q * c for q, c in zip(qualities, uniquenesses)]
    contributions = [w * mp for w, mp in zip(weights, m_primes)]

    # Evidence-union (soft-OR over weighted, sharpened contributions)
    log_one_minus = 0.0
    for c in contributions:
        # Clamp to [0, 1] before log1p to handle tiny float drift
        clamped = min(1.0, max(0.0, c))
        log_one_minus += math.log(max(_EPS, 1.0 - clamped))
    E = 1.0 - math.exp(log_one_minus)
    E = min(1.0, max(0.0, E))

    # Count saturation
    eff_n = sum(weights)
    count_conf = 1.0 - math.exp(-eff_n / max(_EPS, count_k))
    count_conf = min(1.0, max(0.0, count_conf))

    # Distribution shape (entropy of the sharpened m_i' as a distribution)
    nonzero = [mp for mp in m_primes if mp > _EPS]
    s = sum(nonzero)
    if len(nonzero) > 1 and s > _EPS:
        ps = [mp / s for mp in nonzero]
        H = -sum(p * math.log(max(_EPS, p)) for p in ps)
        H_max = math.log(len(nonzero))
        dist_q = 0.5 + 0.5 * (H / H_max if H_max > _EPS else 0.0)
        dist_q = min(1.0, max(0.5, dist_q))
    else:
        # 0 or 1 nonzero contributions → no distribution to evaluate.
        # Conservative neutral value (§3.2 step 6).
        dist_q = 0.5

    S = E * count_conf * dist_q
    return ChannelScore(S=S, E=E, count_conf=count_conf, dist_q=dist_q, m_primes=m_primes)


def score_aggregate_channel(
    sim: float,
    quality: float,
    baseline: float,
    gamma: float,
) -> ChannelScore:
    """Channel score for aggregate channels (B). Single similarity in,
    single contribution. No count saturation, no distribution term —
    the channel represents one summary, not a distribution.
    """
    m_prime = sharpen(sim, baseline, gamma)
    S = m_prime * quality
    S = min(1.0, max(0.0, S))
    return ChannelScore(S=S, E=0.0, count_conf=1.0, dist_q=1.0, m_primes=[m_prime])


def compose(
    channel_scores: dict[str, ChannelScore],
    min_contribution: float,
    bonus_per_extra: float,
) -> tuple[float, list[str]]:
    """Cross-channel composition (§3.3): `max(fired) + bonus * (n_fired - 1)`,
    capped at 1.0. Returns `(composite, fired_channel_names)`.
    """
    fired = [(name, cs.S) for name, cs in channel_scores.items() if cs.S >= min_contribution]
    if not fired:
        return 0.0, []
    top = max(s for _, s in fired)
    composite = top + bonus_per_extra * (len(fired) - 1)
    composite = min(1.0, max(0.0, composite))
    return composite, [name for name, _ in fired]
