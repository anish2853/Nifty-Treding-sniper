"""Unit tests for the NSE option chain parser and the OI snapshot differ.

The fixture is a handcrafted but schema-faithful sample of
GET /api/option-chain-indices?symbol=NIFTY, designed to these exact totals:
  all expiries:      sum CE OI = 1,600,000   sum PE OI = 2,045,000
  nearest expiry:    sum CE OI = 1,245,000   sum PE OI = 1,560,000
  max CE OI @ 25000 (465,000) / max PE OI @ 24900 (545,000)
"""
import copy
import json
from datetime import date
from pathlib import Path

import pytest

from data_sources.nse_chain import parse_option_chain
from engines.oi_engine import FLAT, FRESH_WRITING, UNWINDING, classify, diff_snapshots

FIXTURE = Path(__file__).parent / "fixtures" / "nse_chain_sample.json"
TODAY = date(2026, 10, 5)  # pin 'today' so nearest-expiry selection is deterministic


@pytest.fixture(scope="module")
def raw_chain():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def snapshot(raw_chain):
    return parse_option_chain(raw_chain, today=TODAY)


def test_underlying_timestamp_nearest_expiry(snapshot):
    assert snapshot.underlying == pytest.approx(24950.15)
    assert snapshot.raw_ts == "05-Oct-2026 09:15:00"
    assert snapshot.chain_ts is not None
    assert snapshot.nearest_expiry == "06-Oct-2026"
    assert set(snapshot.expiry_dates) == {"06-Oct-2026", "13-Oct-2026"}


def test_row_count_and_single_sided_strikes(snapshot):
    # 6 strikes on the nearest expiry + 2 on the next; PE-only / CE-only rows tolerated
    assert len(snapshot.rows) == 8
    pe_only = next(r for r in snapshot.rows
                   if r.strike == 24700 and r.expiry == "06-Oct-2026")
    assert pe_only.ce_oi is None
    assert pe_only.pe_oi == 85000
    ce_only = next(r for r in snapshot.rows if r.strike == 25200)
    assert ce_only.pe_oi is None
    assert ce_only.ce_oi == 145000


def test_per_strike_fields(snapshot):
    row = next(r for r in snapshot.rows
               if r.strike == 25000 and r.expiry == "06-Oct-2026")
    assert row.ce_oi == 465000
    assert row.ce_change_oi == 65000
    assert row.ce_ltp == pytest.approx(118.9)
    assert row.ce_iv == pytest.approx(13.2)
    assert row.pe_oi == 480000
    assert row.pe_change_oi == -38000
    assert row.pe_ltp == pytest.approx(96.4)


def test_pcr_total_across_all_expiries(snapshot):
    assert snapshot.total_call_oi == pytest.approx(1_600_000)
    assert snapshot.total_put_oi == pytest.approx(2_045_000)
    assert snapshot.pcr_total == pytest.approx(2_045_000 / 1_600_000)


def test_pcr_nearest_expiry(snapshot):
    assert snapshot.pcr_nearest == pytest.approx(1_560_000 / 1_245_000)


def test_max_oi_strikes(snapshot):
    assert snapshot.max_call_oi_strike == 25000
    assert snapshot.max_put_oi_strike == 24900
    assert snapshot.max_call_oi_strike_nearest == 25000
    assert snapshot.max_put_oi_strike_nearest == 24900


def test_parse_rejects_broken_payloads():
    with pytest.raises(ValueError):
        parse_option_chain({})
    with pytest.raises(ValueError):
        parse_option_chain({"records": {}})
    with pytest.raises(ValueError):
        parse_option_chain({"records": {"data": []}})


def test_snapshot_diff_fresh_writing_vs_unwinding(raw_chain):
    raw2 = copy.deepcopy(raw_chain)
    # Call writers piling on at 25000: day change-in-OI grows 65000 -> 95000;
    # put unwinding accelerates at the same strike: -38000 -> -58000.
    # A brand-new strike (25300) appears -> no snapshot delta yet.
    for entry in raw2["records"]["data"]:
        if entry["strikePrice"] == 25000 and entry["expiryDate"] == "06-Oct-2026":
            entry["CE"]["changeinOpenInterest"] = 95000
            entry["PE"]["changeinOpenInterest"] = -58000
    raw2["records"]["data"].append({
        "strikePrice": 25300,
        "expiryDate": "06-Oct-2026",
        "CE": {"openInterest": 50000, "changeinOpenInterest": 10000,
               "lastPrice": 5.1, "impliedVolatility": 12.8, "totalTradedVolume": 9000},
    })
    snap1 = parse_option_chain(raw_chain, today=TODAY)
    snap2 = parse_option_chain(raw2, today=TODAY)

    diff = diff_snapshots(snap1.rows, snap2.rows,
                          prev_underlying=snap1.underlying,
                          curr_underlying=snap2.underlying)
    by_key = {(d.expiry, d.strike): d for d in diff.deltas}

    call_25000 = by_key[("06-Oct-2026", 25000)]
    assert call_25000.ce_delta_snap == pytest.approx(30000)
    assert classify(call_25000.ce_delta_snap) == FRESH_WRITING
    assert call_25000.pe_delta_snap == pytest.approx(-20000)
    assert classify(call_25000.pe_delta_snap) == UNWINDING

    new_strike = by_key[("06-Oct-2026", 25300)]
    assert new_strike.ce_delta_snap is None
    assert classify(new_strike.ce_delta_snap) == FLAT
