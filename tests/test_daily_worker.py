"""Unit tests for the daily-worker pieces: EOD accounting, feed-health alarms,
regime-state crash recovery, and journal dedup."""
import pytest

from engines.regime import RegimeClassifier, RegimeState
from journal import stats
from journal.store import JournalStore
from utils import ist_now
from watchdog import FeedHealth


@pytest.fixture()
def store(tmp_path):
    return JournalStore(db_path=tmp_path / "t.sqlite3")


def _seed_day(store):
    """Three closed paper trades today: +0.5R, -1.0R, +0.25R (+ one still open)."""
    for i, (rr, pnl) in enumerate([(0.5, 500.0), (-1.0, -1000.0), (0.25, 250.0)], 1):
        store.journal_event("SIGNAL", direction="LONG", strike=25000, spot=24950,
                            option_ltp=100.0 + i, score=85, trade_id=f"T{i}",
                            notes=f"signal {i}")
        store.journal_event("EXIT", direction="LONG", strike=25000, spot=24960,
                            option_ltp=110.0 + i, result_r=rr, exit_reason="test",
                            trade_id=f"T{i}", pnl_rupees=pnl, notes=f"exit {i}")
    store.journal_event("SIGNAL", direction="SHORT", strike=24900, spot=24950,
                        option_ltp=90.0, score=82, trade_id="T4", notes="signal 4")


def test_running_stats(store):
    _seed_day(store)
    s = stats.running_stats(store.conn)
    assert s["count"] == 3
    assert s["win_rate"] == "67%"
    assert s["expectancy"] == "-0.08R"          # (-0.25R / 3)
    assert s["streak"] == "W1"                  # last trade was a winner
    assert s["max_dd"] == pytest.approx(1.0)    # 0.5R peak -> -0.5R trough


def test_running_stats_empty(store):
    s = stats.running_stats(store.conn)
    assert s["count"] == 0 and s["win_rate"] == "—" and s["max_dd"] == 0.0


def test_closed_trades_join_entry_via_trade_id(store):
    _seed_day(store)
    trades = stats.closed_trades(store.conn, ist_now().date().isoformat())
    assert len(trades) == 3
    assert trades[0]["entry"] == 101.0 and trades[0]["exit"] == 111.0
    assert trades[0]["result_r"] == 0.5


def test_open_trades_square_off_scope(store):
    _seed_day(store)
    opened = stats.open_trades(store.conn, ist_now().date().isoformat())
    assert [t["trade_id"] for t in opened] == ["T4"]


def test_eod_body_contains_required_blocks(store):
    _seed_day(store)
    body = stats.build_eod_body(store, ist_now().date().isoformat(),
                                "NORMAL | gap +0.20%", ["option_chain: 5/5 OK"])
    assert "PAPER TRADES TODAY" in body
    assert "DAY TOTAL: -0.25R | Rs -250.00" in body
    assert "win rate 67%" in body and "max drawdown +1.00R" in body
    assert "REGIME TODAY: NORMAL" in body
    assert "option_chain: 5/5 OK" in body


def test_feed_health_alarms_once_per_streak():
    health = FeedHealth(threshold=3)
    assert health.record("option_chain", False) is None
    assert health.record("option_chain", False) is None
    assert health.record("option_chain", False) == "option_chain"   # 3rd -> alarm
    assert health.record("option_chain", False) is None             # already alarmed
    assert health.record("option_chain", True) is None              # recovery resets
    assert health.record("option_chain", False) is None
    assert health.record("option_chain", False) is None
    assert health.record("option_chain", False) == "option_chain"   # new streak alarms


def test_feed_health_quality_lines():
    health = FeedHealth(threshold=5)
    health.record("option_chain", True)
    health.record("option_chain", True)
    health.record("option_chain", False)
    health.record("gift", False)
    lines = health.quality_lines()
    assert "option_chain: 2/3 OK (1 missing)" in lines
    assert "gift: 0/1 OK (1 missing)" in lines


def test_regime_state_roundtrip():
    state = RegimeClassifier().classify_at_open(spot=100.8, pdc=100,
                                                us_direction=-1, event_day=False)
    RegimeClassifier().apply_pcr(state, 1.5)
    recovered = RegimeState.from_reasons(state.reasons())
    assert recovered.regime == state.regime
    assert recovered.low_confidence and recovered.mean_reversion
    assert recovered.gap_pct == pytest.approx(state.gap_pct)
    assert recovered.pcr == pytest.approx(1.5)


def test_journal_dedup_and_state_recovery(store):
    today = ist_now().date().isoformat()
    assert not store.has_journal_event(today, "pre-market factor report")
    store.journal_event("REGIME", regime="NORMAL", reasons={"gap_pct": 0.002},
                        notes="pre-market factor report")
    assert store.has_journal_event(today, "pre-market factor report")

    # pre-market rows are excluded from live-state recovery...
    assert store.latest_regime_state(today) is None
    # ...while classification rows are recoverable
    store.journal_event("REGIME", regime="GAP-UP-EXTENSION",
                        reasons={"regime": "GAP-UP-EXTENSION", "gap_pct": 0.008,
                                 "low_confidence": True},
                        notes="09:15 classification (first spot print)")
    recovered = store.latest_regime_state(today)
    assert recovered["regime"] == "GAP-UP-EXTENSION"
