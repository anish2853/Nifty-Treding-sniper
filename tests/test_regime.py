"""Pins the regime classifier's gap bands and flags (Module 2)."""
from engines.regime import Regime, RegimeClassifier

C = RegimeClassifier()


def test_gap_bands():
    s = C.classify_at_open(spot=100.2, pdc=100, us_direction=1, event_day=False)
    assert s.regime == Regime.NORMAL and not s.low_confidence       # +0.2% -> NORMAL
    s = C.classify_at_open(spot=100.45, pdc=100, us_direction=1, event_day=False)
    assert s.regime == Regime.NORMAL  # 0.3-0.6% band: no spec restriction -> NORMAL
    s = C.classify_at_open(spot=100.7, pdc=100, us_direction=1, event_day=False)
    assert s.regime == Regime.GAP_UP_EXTENSION                      # > +0.6%
    s = C.classify_at_open(spot=99.3, pdc=100, us_direction=1, event_day=False)
    assert s.regime == Regime.GAP_DOWN                              # < -0.6%


def test_low_confidence_opposite_us_direction():
    s = C.classify_at_open(spot=100.8, pdc=100, us_direction=-1, event_day=False)
    assert s.regime == Regime.GAP_UP_EXTENSION and s.low_confidence
    s = C.classify_at_open(spot=100.2, pdc=100, us_direction=-1, event_day=False)
    # within the NORMAL band the gap has no definite direction -> no flag
    assert not s.low_confidence


def test_pcr_context_and_mean_reversion():
    s = C.classify_at_open(spot=100, pdc=100, us_direction=1, event_day=False)
    C.apply_pcr(s, 1.3)
    assert s.pcr_tilt == "BULLISH" and not s.mean_reversion
    C.apply_pcr(s, 1.5)                       # > 1.4 -> mean-reversion regime
    assert s.mean_reversion
    C.apply_pcr(s, 0.65)                      # < 0.7 -> mean-reversion regime
    assert s.pcr_tilt == "BEARISH" and s.mean_reversion
    C.apply_pcr(s, 0.9)
    assert s.pcr_tilt == "NEUTRAL" and not s.mean_reversion


def test_vix_spike_flag():
    s = C.classify_at_open(spot=100, pdc=100, us_direction=1, event_day=False)
    assert not C.apply_vix(s, 12.0, 12.0) and not s.vix_spike
    assert C.apply_vix(s, 13.1, 12.0) and s.vix_spike          # +9.2% -> spike onset
    assert C.apply_vix(s, 12.3, 12.0) and not s.vix_spike      # back below -> offset


def test_event_day_is_no_trade():
    s = C.classify_at_open(spot=105, pdc=100, us_direction=1, event_day=True)
    assert s.regime == Regime.NO_TRADE_DAY


def test_missing_pdc_defers_classification():
    s = C.classify_at_open(spot=100.8, pdc=None, us_direction=1, event_day=False)
    assert s.gap_pct is None and s.regime == Regime.NORMAL
