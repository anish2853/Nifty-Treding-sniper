"""Unit tests for Phase 15: Zone-Ride Model (Consolidated Spec).

Validates:
1. Dustbin of unvalidated engines (ENG-05-TLFIB disabled, ENG-07-ZONERIDE enabled).
2. Live chain liquidity zone construction (Resistance top Call-OI, Support top Put-OI,
   width 10-30 pts, confluence, tested-today, STRONG tag).
3. Patience Gate & 2-candle confirmation (C1 off zone, C2 breaks liquidity edge with body >= 50%).
4. OI Velocity Gate (writers covering = fuel, writers stacking = live wall block).
5. Strike Selection & Premium Room Gate (delta 0.65-0.75 ITM, room >= 20 prem pts, SL beyond far edge max 33 pts).
6. The Ride & Structure Trailing (ratcheting higher lows/lower highs, no 5-pt scalps, optional 30% wall booking, EOD square-off).
7. Telegram alert formatting (spot target, premium target, delta, room-to-next-zone).
"""

from datetime import datetime
import pytest

import settings
from alerts import messages
from engines.candles import Candle
from engines.census import get_census, is_engine_approved
from engines.delta_execution import (
    calculate_zone_far_edge_sl,
    evaluate_premium_room_gate,
    select_delta_aware_strike,
)
from engines.zone_ride import (
    ActiveZoneRideTrade,
    LiquidityZone,
    ZoneRideEngine,
    ZoneRideSignal,
    build_liquidity_zones,
)

TODAY = datetime(2026, 10, 14, tzinfo=settings.IST)


def make_candle(hh: int, mm: int, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(
        start=TODAY.replace(hour=hh, minute=mm, second=0, microsecond=0),
        open=o,
        high=h,
        low=l,
        close=c,
        interval_minutes=5,
    )


class MockChainRow:
    def __init__(
        self,
        strike: float,
        expiry: str,
        ce_oi: float = 0.0,
        pe_oi: float = 0.0,
        ce_change_oi: float = 0.0,
        pe_change_oi: float = 0.0,
        ce_ltp: float = 100.0,
        pe_ltp: float = 100.0,
        ce_iv: float = 15.0,
        pe_iv: float = 15.0,
    ):
        self.strike = strike
        self.expiry = expiry
        self.ce_oi = ce_oi
        self.pe_oi = pe_oi
        self.ce_change_oi = ce_change_oi
        self.pe_change_oi = pe_change_oi
        self.ce_ltp = ce_ltp
        self.pe_ltp = pe_ltp
        self.ce_iv = ce_iv
        self.pe_iv = pe_iv


class MockChainSnapshot:
    def __init__(self, rows: list[MockChainRow], nearest_expiry: str = "2026-10-15"):
        self.rows = rows
        self.nearest_expiry = nearest_expiry
        self.trade_expiry = nearest_expiry


# --- 1. DUSTBIN & CENSUS GOVERNANCE ---

def test_census_phase15_1_restoration_and_bonus():
    census = get_census()
    census_dict = {e.engine_id: e for e in census}

    # ENG-05-TLFIB must be RESTORED / ENABLED in Phase 15.1
    assert "ENG-05-TLFIB" in census_dict
    tlfib = census_dict["ENG-05-TLFIB"]
    assert tlfib.approved is True
    assert tlfib.execution_status == "ENABLED"
    assert is_engine_approved("ENG-05-TLFIB") is True

    # ENG-07-ZONERIDE must be DEMOTED to bonus score only (not execution)
    assert "ENG-07-ZONERIDE" in census_dict
    zride = census_dict["ENG-07-ZONERIDE"]
    assert zride.approved is False
    assert zride.execution_status == "DEMOTED_BONUS"
    assert is_engine_approved("ENG-07-ZONERIDE") is False


# --- 2. LIQUIDITY ZONES CONSTRUCTION ---

def test_build_liquidity_zones_construction():
    expiry = "2026-10-15"
    rows = [
        MockChainRow(strike=22300, expiry=expiry, pe_oi=180_000, pe_change_oi=-5000),  # Strong Put OI
        MockChainRow(strike=22400, expiry=expiry, ce_oi=50_000, pe_oi=40_000),
        MockChainRow(strike=22500, expiry=expiry, ce_oi=210_000, ce_change_oi=-10000),  # Strong Call OI
    ]
    chain = MockChainSnapshot(rows=rows, nearest_expiry=expiry)

    # Candles that tested 22300 (wick into 22300 +/- 10) and 22500
    candles = [
        make_candle(9, 15, 22350, 22360, 22295, 22340),  # low 22295 touches 22300 zone
        make_candle(9, 20, 22340, 22420, 22330, 22410),
        make_candle(9, 25, 22410, 22505, 22400, 22480),  # high 22505 touches 22500 zone
    ]

    resistances, supports = build_liquidity_zones(
        chain_snapshot=chain,
        spot=22400.0,
        candles=candles,
        pdh=22510.0,  # Confluence with 22500
        pdl=22290.0,  # Confluence with 22300
    )

    assert len(resistances) >= 1
    res = resistances[0]
    assert res.anchor_strike == 22500
    assert res.zone_type == "RESISTANCE"
    assert res.zone_low == 22490.0
    assert res.zone_high == 22510.0
    assert res.width == 20.0
    assert res.far_edge == 22510.0
    assert res.liquidity_edge == 22490.0
    assert res.strong is True
    assert "PDH" in res.confluence_factors

    assert len(supports) >= 1
    sup = supports[0]
    assert sup.anchor_strike == 22300
    assert sup.zone_type == "SUPPORT"
    assert sup.zone_low == 22290.0
    assert sup.zone_high == 22310.0
    assert sup.width == 20.0
    assert sup.far_edge == 22290.0
    assert sup.liquidity_edge == 22310.0
    assert sup.strong is True
    assert "PDL" in sup.confluence_factors


# --- 3. PATIENCE GATE & 2-CANDLE CONFIRMATION ---

def test_patience_gate_and_confirmation_long():
    engine = ZoneRideEngine()
    expiry = "2026-10-15"
    rows = [
        MockChainRow(strike=22300, expiry=expiry, pe_oi=150_000, pe_change_oi=-10_000, ce_ltp=150.0),
        MockChainRow(strike=22350, expiry=expiry, pe_oi=200_000, pe_change_oi=-25_000, ce_ltp=120.0),
        MockChainRow(strike=22550, expiry=expiry, ce_oi=250_000, ce_change_oi=-15_000, ce_ltp=30.0),
    ]
    chain = MockChainSnapshot(rows=rows, nearest_expiry=expiry)

    # Candle 1: Enters support zone and closes off zone bullishly
    c1 = make_candle(9, 30, 22348, 22360, 22345, 22358)  # touches [22340, 22360], closes bullish at 22358
    # Candle 2: Breaks liquidity edge (22360) with strong body >= 50%
    # range: high 22375, low 22350 (25 pts). body: open 22352, close 22372 (20 pts, 80% body >= 50%)
    c2 = make_candle(9, 35, 22352, 22375, 22350, 22372)

    candles = [
        make_candle(9, 15, 22380, 22400, 22370, 22390),
        make_candle(9, 20, 22390, 22410, 22380, 22400),
        make_candle(9, 25, 22400, 22405, 22365, 22370),
        c1,
        c2,
    ]

    sig = engine.evaluate_entry(
        candles=candles,
        chain_snapshot=chain,
        spot=22372.0,
        trend="UPTREND",
        capital=100_000.0,
        expiry=expiry,
    )

    assert sig is not None
    assert sig.direction == "LONG"
    assert sig.anchor_zone.anchor_strike == 22350
    assert sig.entry_spot == 22372.0
    # SL sits beyond far edge (22340 - 3.0 = 22337), capped at max 33 pts: 22372 - 33 = 22339.0
    assert sig.sl_spot == 22339.0
    assert sig.risk_spot_pts == 33.0
    assert sig.delta >= 0.60


def test_confirmation_fails_if_weak_candle_body():
    """Candle 2 breaks liquidity edge but body is < 50% (indecisive doji) -> REJECT."""
    engine = ZoneRideEngine()
    expiry = "2026-10-15"
    rows = [
        MockChainRow(strike=22350, expiry=expiry, pe_oi=200_000, pe_change_oi=-10_000, ce_ltp=120.0),
        MockChainRow(strike=22550, expiry=expiry, ce_oi=250_000, ce_change_oi=-15_000, ce_ltp=30.0),
    ]
    chain = MockChainSnapshot(rows=rows, nearest_expiry=expiry)

    # Candle 1: touches [22340, 22360]
    c1 = make_candle(9, 30, 22370, 22375, 22345, 22355)
    # Candle 2: crosses liquidity edge 22360, but weak doji body:
    # high 22380, low 22350 (range 30 pts). open 22361, close 22363 (body 2 pts -> 6.7% < 50%)
    c2 = make_candle(9, 35, 22361, 22380, 22350, 22363)

    candles = [c1, c2]
    sig = engine.evaluate_entry(
        candles=candles,
        chain_snapshot=chain,
        spot=22363.0,
        trend="UPTREND",
        capital=100_000.0,
        expiry=expiry,
    )

    assert sig is None, "Weak body candle breaking edge should be blocked by impulse body gate"


# --- 4. OI VELOCITY GATE ---

def test_oi_velocity_stacking_blocks_trade():
    """Writers stacking (+95k OI change) at opposing wall -> live wall block."""
    engine = ZoneRideEngine()
    expiry = "2026-10-15"
    rows = [
        # Call writers heavily stacking above -> live wall
        MockChainRow(strike=22350, expiry=expiry, pe_oi=200_000, pe_change_oi=-10_000),
        MockChainRow(strike=22450, expiry=expiry, ce_oi=250_000, ce_change_oi=95_000),  # Stacking!
    ]
    chain = MockChainSnapshot(rows=rows, nearest_expiry=expiry)

    c1 = make_candle(9, 30, 22370, 22375, 22345, 22355)
    c2 = make_candle(9, 35, 22352, 22375, 22350, 22372)

    sig = engine.evaluate_entry(
        candles=[c1, c2],
        chain_snapshot=chain,
        spot=22372.0,
        trend="UPTREND",
        capital=100_000.0,
        expiry=expiry,
    )

    assert sig is None, "Call writers stacking at opposing wall must block LONG trade"


# --- 5. STRIKE SELECTION & PREMIUM ROOM GATE ---

def test_delta_and_premium_room_gate():
    # 1. Delta strike selection
    rows = [
        MockChainRow(strike=22300, expiry="2026-10-15", ce_ltp=185.0),
        MockChainRow(strike=22350, expiry="2026-10-15", ce_ltp=145.0),
        MockChainRow(strike=22400, expiry="2026-10-15", ce_ltp=110.0),
    ]
    chain = MockChainSnapshot(rows=rows, nearest_expiry="2026-10-15")

    res = select_delta_aware_strike(
        spot=22380.0,
        direction="LONG",
        chain_snapshot=chain,
        target_delta=0.70,
        target_delta_min=0.65,
        target_delta_max=0.75,
        expiry="2026-10-15",
    )
    strike = res["strike"]
    opt_type = res["option_type"]
    delta = res["delta"]
    prem = res["entry_prem"]
    assert opt_type == "CE"
    assert 0.60 <= delta <= 0.75
    assert strike in (22300, 22350)  # Slightly ITM

    # 2. Premium room gate passes when room >= 20 prem pts
    res_pass = evaluate_premium_room_gate(
        room_spot_pts=70.0,
        delta=0.70,
        min_prem_pts=20.0,
    )
    assert res_pass["passed"] is True
    assert res_pass["room_spot_pts"] == 70.0
    assert res_pass["prem_room"] == pytest.approx(49.0, abs=1.0)

    # 3. Premium room gate fails when opposing wall is too tight (< 20 prem pts)
    res_tight = evaluate_premium_room_gate(
        room_spot_pts=15.0,
        delta=0.70,
        min_prem_pts=20.0,
    )
    assert res_tight["passed"] is False
    assert res_tight["prem_room"] < 20.0


def test_zone_far_edge_sl_and_hard_cap():
    # Support bounce: zone [22340, 22360], entry 22365
    res = calculate_zone_far_edge_sl(
        entry_spot=22365.0,
        zone_far_edge=22340.0,
        direction="LONG",
        buffer_pts=3.0,
        max_spot_pts=33.0,
    )
    assert res["sl_spot"] == 22337.0
    assert res["risk_pts"] == 28.0

    # Test hard cap at 33 pts if zone far edge is too deep
    res_capped = calculate_zone_far_edge_sl(
        entry_spot=22400.0,
        zone_far_edge=22340.0,  # 60 pts away
        direction="LONG",
        buffer_pts=3.0,
        max_spot_pts=33.0,
    )
    assert res_capped["risk_pts"] == 33.0
    assert res_capped["sl_spot"] == 22367.0


# --- 6. THE RIDE & 5M STRUCTURAL TRAILING ---

def test_structure_trailing_and_wall_booking():
    engine = ZoneRideEngine()
    anchor = LiquidityZone("SUP-22350", "SUPPORT", 22350, 22340, 22360, 200_000, -10_000, ["PDL"], True, True)
    opposing = LiquidityZone("RES-22480", "RESISTANCE", 22480, 22470, 22490, 250_000, 10_000, ["PDH"], True, True)

    sig = ZoneRideSignal(
        trade_id="ZR-L-001",
        direction="LONG",
        anchor_zone=anchor,
        opposing_zone=opposing,
        entry_candle=make_candle(9, 35, 22355, 22375, 22350, 22370),
        entry_spot=22370.0,
        sl_spot=22337.0,
        risk_spot_pts=33.0,
        room_to_opposing_spot_pts=100.0,
        prem_room=70.0,
        strike=22350,
        option_type="CE",
        option_symbol="NIFTY 22350 CE",
        expiry="2026-10-15",
        delta=0.70,
        entry_prem=150.0,
        sl_prem=126.9,
        spot_target=22470.0,
        prem_target=220.0,
    )

    trade = engine.open_position(sig)
    assert engine.active_trade is not None
    assert engine.active_trade.current_sl_spot == 22337.0

    # Candle 1: Forms higher low at 22360 -> SL ratchets to 22358.0 (low - 2.0)
    c1 = make_candle(9, 40, 22370, 22395, 22360, 22390)
    engine.update_position(c1, spot=22390.0)
    assert engine.active_trade.current_sl_spot == 22358.0
    assert not engine.active_trade.opposing_wall_booked

    # Candle 2: Touches major opposing wall (22470) -> triggers 30% wall booking alert
    c2 = make_candle(9, 45, 22390, 22475, 22385, 22472)
    action, reason = engine.update_position(c2, spot=22472.0)
    assert action == "WALL_BOOKING"
    assert engine.active_trade.opposing_wall_booked is True

    # Candle 3: Violates trailing structure by closing below current_sl_spot (22370 entry tightened stop)
    c3 = make_candle(9, 50, 22460, 22460, 22340, 22350)
    action_exit, reason_exit = engine.update_position(c3, spot=22350.0)
    assert action_exit == "EXIT"
    assert "TRAIL" in reason_exit or "SL" in reason_exit
    assert len(engine.closed_trades) == 1
    assert engine.closed_trades[0].realized_r > 0.0


def test_square_off_at_1510():
    engine = ZoneRideEngine()
    anchor = LiquidityZone("SUP-22350", "SUPPORT", 22350, 22340, 22360, 200_000, -10_000, [], True, True)

    sig = ZoneRideSignal(
        trade_id="ZR-L-002",
        direction="LONG",
        anchor_zone=anchor,
        opposing_zone=None,
        entry_candle=make_candle(10, 0, 22360, 22370, 22355, 22365),
        entry_spot=22365.0,
        sl_spot=22337.0,
        risk_spot_pts=28.0,
        room_to_opposing_spot_pts=50.0,
        prem_room=35.0,
        strike=22350,
        option_type="CE",
        option_symbol="NIFTY 22350 CE",
        expiry="2026-10-15",
        delta=0.70,
        entry_prem=140.0,
        sl_prem=120.4,
        spot_target=22415.0,
        prem_target=175.0,
    )
    engine.open_position(sig)

    # Candle at 15:10 normal day -> square-off
    c_eod = make_candle(15, 10, 22380, 22385, 22375, 22380)
    action, reason = engine.update_position(c_eod, spot=22380.0, is_trend_day=False)
    assert action == "EXIT"
    assert reason == "SQUARE_OFF_1510"


# --- 7. TELEGRAM ALERT FORMATTING ---

def test_telegram_zone_ride_alerts():
    entry_card = messages.format_zone_ride_entry_alert(
        trade_id="ZR-L-001",
        direction="LONG",
        anchor_zone_name="SUPPORT 22350",
        anchor_zone_range="22340.0 – 22360.0",
        entry_spot=22370.0,
        initial_sl_spot=22337.0,
        risk_pts=33.0,
        opposing_zone_name="RESISTANCE 22480",
        room_spot_pts=100.0,
        room_prem_pts=70.0,
        spot_target=22470.0,
        prem_target=220.0,
        strike=22350,
        option_type="CE",
        option_symbol="NIFTY 22350 CE",
        entry_prem=150.0,
        sl_prem=126.9,
        delta=0.70,
    )
    assert "ZONE-RIDE" in entry_card
    assert "BUY CE" in entry_card
    assert "Target Delta: 0.70" in entry_card
    assert "Premium ~₹220.0" in entry_card
    assert "Room to Next Zone: 100.0 spot pts" in entry_card

    exit_card = messages.format_zone_ride_exit_alert(
        trade_id="ZR-L-001",
        direction="LONG",
        option_symbol="NIFTY 22350 CE",
        entry_spot=22370.0,
        exit_spot=22450.0,
        entry_prem=150.0,
        exit_prem=206.0,
        realized_r=2.42,
        exit_reason="5M_STRUCTURE_TRAIL",
    )
    assert "ZONE-RIDE TRADE CLOSED" in exit_card
    assert "+2.42 R" in exit_card
    assert "5M_STRUCTURE_TRAIL" in exit_card

    wall_card = messages.format_zone_ride_wall_booking_alert(
        trade_id="ZR-L-001",
        direction="LONG",
        option_symbol="NIFTY 22350 CE",
        entry_prem=150.0,
        wall_prem=215.0,
        entry_spot=22370.0,
        current_spot=22475.0,
        opposing_wall_name="RESISTANCE 22480",
    )
    assert "MAJOR OPPOSING WALL" in wall_card
    assert "30% Booked" in wall_card
