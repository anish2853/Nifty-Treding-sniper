"""Per-session unified ladder and MTF zone lifecycle."""
from engines.candles import orb_levels
from engines.level_decision import LevelDecisionEngine, LevelEvent
from engines.level_grid import build_level_grid
from engines.mtf_scalp import WAITING, confirm_zone


class LevelRuntime:
    def __init__(self):
        self.grid = []
        self.velocities = {}
        self.decision = LevelDecisionEngine()
        self.zones = {}

    def refresh_grid(self, market, day_state):
        spot = market.spot_value()
        self.grid = build_level_grid(
            spot=spot, pdh=market.pdh, pdl=market.pdl, pdc=market.pdc,
            orb=orb_levels(market.candles), candles=market.candles,
            vwap=day_state.vwap if day_state else None,
            liquidity=market.liquidity)
        return self.decision.approach_events(spot, self.grid)

    def minute_close(self, candle, previous, day_state):
        if day_state is None:
            return []
        events = self.decision.on_minute(
            candle, self.grid, day_state.state, previous=previous,
            broken_level=day_state.broken_level, vwap=day_state.vwap)
        for event in events:
            if event.zone is not None:
                self.zones[event.zone.zone_id] = event.zone
        return events

    def five_minute_close(self, candle, candles):
        events = self.decision.on_five_minute(candle, self.grid, candles)
        for zone in self.zones.values():
            if zone.status != WAITING:
                continue
            old_status = zone.status
            new_status = confirm_zone(zone, candle)
            if new_status != old_status:
                events.append(LevelEvent(
                    f"ZONE-{new_status}", zone.zone_id, "MTF zone", zone.trigger,
                    zone.direction, f"MTF zone {zone.zone_id} {new_status}", zone))
        return events

    def range_width(self, spot):
        if spot is None:
            return None
        supports = [level.price for level in self.grid
                    if level.side == "S" and level.strong and level.price <= spot]
        resistances = [level.price for level in self.grid
                       if level.side == "R" and level.strong and level.price >= spot]
        if not supports or not resistances:
            return None
        return min(resistances) - max(supports)

    def runway(self, spot, direction):
        """OI-smart runway (hotfix): only SIGNIFICANT walls count - writers
        actively adding (change-in-OI > 0) or STRONG-tagged levels. Minor
        swing levels with static OI are not walls and no longer block a
        big move (the 100-pt SHORT at 12:51 was blocked by a 10-pt swing).
        """
        if spot is None:
            return None, None
        def significant(level):
            velocity = self.velocities.get(level.price)
            if velocity is not None:
                return velocity >= settings.WALL_VELOCITY_MIN
            return (level.change_oi is None or level.change_oi > 0
                    or level.strong)
        if direction == "LONG":
            ahead = [level for level in self.grid if level.price > spot
                     and significant(level)]
        else:
            ahead = [level for level in self.grid if level.price < spot
                     and significant(level)]
        if not ahead:
            return None, None
        nearest = min(ahead, key=lambda level: abs(level.price - spot)) \
            if direction == "LONG" else max(ahead, key=lambda level: level.price)
        return abs(nearest.price - spot), nearest
