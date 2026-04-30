"""Unit tests for the within-channel scoring formula + cross-channel
composition. Pure compute; no DB or HTTP fixtures required.

Ports the inline heredoc tests run during Phase 4.1 implementation.
See bridge/app/matching/scoring.py and CLAUDE.md §13.2 / §13.3.
"""
import math

import pytest

from bridge.app.matching.scoring import (
    ChannelScore,
    compose,
    score_aggregate_channel,
    score_frame_channel,
    sharpen,
)


# --- sharpen --------------------------------------------------------------

class TestSharpen:
    def test_at_baseline_returns_zero(self):
        assert sharpen(0.5, baseline=0.5, gamma=2.0) == 0.0

    def test_below_baseline_returns_zero(self):
        assert sharpen(0.4, baseline=0.5, gamma=2.0) == 0.0

    def test_at_one_returns_one(self):
        assert sharpen(1.0, baseline=0.5, gamma=2.0) == 1.0

    def test_midpoint_with_gamma_two(self):
        # raw = (0.75 - 0.5) / 0.5 = 0.5; gamma=2 → 0.25
        assert math.isclose(sharpen(0.75, 0.5, 2.0), 0.25, abs_tol=1e-9)

    def test_gamma_higher_more_aggressive(self):
        # Same raw=0.5, gamma=3 → 0.125 (more aggressive than gamma=2 → 0.25)
        s2 = sharpen(0.75, 0.5, 2.0)
        s3 = sharpen(0.75, 0.5, 3.0)
        assert s3 < s2

    def test_baseline_one_no_underflow(self):
        # denom is clamped to epsilon; result must stay in [0, 1]
        v = sharpen(0.9, baseline=1.0, gamma=2.0)
        assert 0.0 <= v <= 1.0


# --- score_frame_channel --------------------------------------------------

class TestScoreFrameChannel:
    def test_real_match_scores_high(self):
        """5 consistent strong sims (0.85), q=0.7, c=1.0 → S > 0.5."""
        cs = score_frame_channel(
            per_image_max_sims=[0.85] * 5,
            qualities=[0.7] * 5,
            uniquenesses=[1.0] * 5,
            baseline=0.5, gamma=2.0, count_k=2.0,
        )
        assert cs.S > 0.5
        assert cs.E > 0.5
        assert cs.count_conf > 0.5

    def test_false_match_outlier_scores_below_real(self):
        """One outlier 1.0 + 4 noise sims at baseline (sharpens to 0)
        should score below the real-match case."""
        cs_real = score_frame_channel(
            [0.85] * 5, [0.7] * 5, [1.0] * 5, 0.5, 2.0, 2.0,
        )
        cs_fake = score_frame_channel(
            [1.0, 0.5, 0.5, 0.5, 0.5], [0.7] * 5, [1.0] * 5, 0.5, 2.0, 2.0,
        )
        assert cs_fake.S < cs_real.S
        # dist_q drops to 0.5 (only one nonzero m'); count_conf survives but E
        # is dampened.
        assert cs_fake.dist_q == 0.5

    def test_q_zero_on_outlier_collapses_to_zero(self):
        """A coincidental 1.0 sim on a q=0 image (e.g. credit screen)
        contributes 0; remaining 4 sims at noise floor sharpen to 0;
        S → ~0."""
        cs = score_frame_channel(
            [1.0, 0.5, 0.5, 0.5, 0.5],
            qualities=[0.0, 0.7, 0.7, 0.7, 0.7],
            uniquenesses=[1.0] * 5,
            baseline=0.5, gamma=2.0, count_k=2.0,
        )
        assert cs.S < 0.05

    def test_empty_input(self):
        cs = score_frame_channel([], [], [], 0.5, 2.0, 2.0)
        assert cs.S == 0.0
        assert cs.E == 0.0
        assert cs.m_primes == []

    def test_single_image_uses_conservative_dist_q(self):
        """N=1 has no distribution; dist_q falls back to 0.5."""
        cs = score_frame_channel(
            [0.9], [0.8], [1.0], 0.5, 2.0, 2.0,
        )
        assert cs.dist_q == 0.5

    def test_mismatched_input_lengths_returns_zero(self):
        # qualities length doesn't match sims; defensive return.
        cs = score_frame_channel(
            [0.85, 0.85], [0.7], [1.0, 1.0], 0.5, 2.0, 2.0,
        )
        assert cs.S == 0.0

    def test_all_zero_quality_collapses(self):
        """w_i = q*c = 0 for every i → contributions all zero → E = 0."""
        cs = score_frame_channel(
            [0.85] * 3, [0.0] * 3, [1.0] * 3, 0.5, 2.0, 2.0,
        )
        assert cs.E == 0.0
        assert cs.S == 0.0


# --- score_aggregate_channel ---------------------------------------------

class TestScoreAggregateChannel:
    def test_arithmetic_matches_formula(self):
        # m' = ((0.7 - 0.5) / 0.5) ** 2 = 0.4 ** 2 = 0.16
        # S  = 0.16 * 0.6 = 0.096
        cs = score_aggregate_channel(0.7, quality=0.6, baseline=0.5, gamma=2.0)
        assert math.isclose(cs.S, 0.096, abs_tol=1e-3)
        assert math.isclose(cs.m_primes[0], 0.16, abs_tol=1e-9)

    def test_below_baseline_returns_zero(self):
        cs = score_aggregate_channel(0.4, 0.6, 0.5, 2.0)
        assert cs.S == 0.0
        assert cs.m_primes[0] == 0.0

    def test_unit_inputs(self):
        # sim=1, quality=1, baseline=0 → m'=1, S=1
        cs = score_aggregate_channel(1.0, 1.0, 0.0, 2.0)
        assert cs.S == 1.0


# --- compose --------------------------------------------------------------

class TestCompose:
    def _cs(self, S: float) -> ChannelScore:
        return ChannelScore(S=S, E=0.0, count_conf=1.0, dist_q=1.0, m_primes=[])

    def test_no_channel_fires_returns_zero(self):
        composite, fired = compose(
            {"phash": self._cs(0.1), "tone": self._cs(0.05)},
            min_contribution=0.3, bonus_per_extra=0.1,
        )
        assert composite == 0.0
        assert fired == []

    def test_single_channel_fires_no_bonus(self):
        composite, fired = compose(
            {"phash": self._cs(0.7), "tone": self._cs(0.05)},
            min_contribution=0.3, bonus_per_extra=0.1,
        )
        assert composite == 0.7
        assert fired == ["phash"]

    def test_two_channels_fire_one_bonus(self):
        composite, fired = compose(
            {"phash": self._cs(0.7), "tone": self._cs(0.5)},
            min_contribution=0.3, bonus_per_extra=0.1,
        )
        # max(0.7, 0.5) + 0.1 * (2 - 1) = 0.8
        assert math.isclose(composite, 0.8, abs_tol=1e-9)
        assert set(fired) == {"phash", "tone"}

    def test_three_channels_fire_two_bonuses(self):
        composite, fired = compose(
            {"phash": self._cs(0.7), "tone": self._cs(0.5), "color_hist": self._cs(0.4)},
            min_contribution=0.3, bonus_per_extra=0.1,
        )
        # 0.7 + 0.1 * (3 - 1) = 0.9
        assert math.isclose(composite, 0.9, abs_tol=1e-9)
        assert len(fired) == 3

    def test_composite_capped_at_one(self):
        composite, _ = compose(
            {"a": self._cs(0.95), "b": self._cs(0.95), "c": self._cs(0.95)},
            min_contribution=0.3, bonus_per_extra=0.5,
        )
        # 0.95 + 0.5 * 2 = 1.95 → capped to 1.0
        assert composite == 1.0
