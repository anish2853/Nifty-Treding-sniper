"""Live wiring helpers: MTF zone fire, ladder targets, journal viz payloads."""
from __future__ import annotations

import logging

import settings
from engines.mtf_scalp import CONFIRMED, confirmation_is_fresh, geometry_ok

log = logging.getLogger(__name__)

MTF_FAMILIES = frozenset({"MTF-SCALP", "LEVEL-FADE", "LEVEL-CONT", "COIL-SNIPE"})


def is_mtf_family(family: str) -> bool:
    return family in MTF_FAMILIES


def compute_mtf_tgt1(entry: float, sl: float, direction: str, grid) -> float:
    """First book level: nearer of +1.5R or the next ladder level ahead."""
    one_r = abs(entry - sl)
    if direction == "LONG":
        r_level = entry + settings.MTF_TARGET_R * one_r
        ahead = [level.price for level in grid if level.price > entry]
        ladder = min(ahead) if ahead else None
        if ladder is None:
            return r_level
        return min(ladder, r_level)
    r_level = entry - settings.MTF_TARGET_R * one_r
    ahead = [level.price for level in grid if level.price < entry]
    ladder = max(ahead) if ahead else None
    if ladder is None:
        return r_level
    return max(ladder, r_level)


def journal_viz(store, today_iso: str, viz_type: str, payload: dict, note: str = "") -> None:
    marker = f"viz:{viz_type}:{payload.get('id', payload.get('zone_id', note))}"
    if store.has_journal_event(today_iso, marker[:80]):
        return
    store.journal_event("SKIP", notes=marker[:120], reasons={"viz": viz_type, **payload})


def zone_viz_payload(zone) -> dict:
    return {
        "id": zone.zone_id,
        "zone_id": zone.zone_id,
        "status": zone.status,
        "direction": zone.direction,
        "trigger": zone.trigger,
        "stop": zone.stop,
        "formed_at": zone.formed_at.isoformat(timespec="seconds"),
        "confirm_time": zone.confirm_time.isoformat(timespec="seconds")
        if zone.confirm_time else None,
        "entry_spot": zone.entry_spot,
    }


def coil_viz_payload(coil) -> dict:
    return coil.as_reasons()


def mtf_entry_allowed(session, family: str) -> str | None:
    """Return suppression reason or None if the setup may fire."""
    from utils import ist_now

    if ist_now().time() >= settings.LAST_ENTRY_TIME:
        return "no new entries after 14:30"
    day = session.day_state
    if day is None:
        return "day state unavailable"
    spot = session.market.spot_value()
    width = session.level_runtime.range_width(spot)
    if width is not None and width < settings.RANGE_WIDTH_MIN_PTS:
        return (f"range-width gate: strong S-R span {width:.0f} pts "
                f"< {settings.RANGE_WIDTH_MIN_PTS}")
    if family == "MTF-SCALP" and day.state not in ("TREND-UP", "TREND-DOWN"):
        return "MTF pullback requires TREND state"
    if family == "LEVEL-FADE" and day.state != "RANGE":
        return "LEVEL-FADE only on RANGE days"
    if day.broken_level is not None:
        if not (settings.ENTRY_EVAL_START <= ist_now().time()
                <= settings.LAST_ENTRY_TIME):
            return "outside structure window 09:30-14:30"
        if not day.structure_intact:
            return "structure broken - state re-evaluating"
    return None


def ready_confirmed_zone(zone, now) -> bool:
    return (zone.status == CONFIRMED and geometry_ok(zone)
            and confirmation_is_fresh(zone, now))


def event_family(event) -> str | None:
    kind = event.kind
    if kind == "MTF-ZONE":
        return "MTF-SCALP"
    if kind == "FADE-ZONE":
        return "LEVEL-FADE"
    if kind == "CONTINUATION":
        return "LEVEL-CONT"
    return None
