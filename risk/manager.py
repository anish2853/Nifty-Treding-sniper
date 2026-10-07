"""Module 5 - risk manager scaffold.

Phase A holds the flags only; there are no trades yet, so nothing enforces them.
Phase B's entry engine will consult entries_allowed() before firing alerts, and
register_result() (paper-mode accounting against CAPITAL) will set the shutdown /
pause flags with SHUTDOWN FOR DAY alerts. All thresholds are already pinned in
settings: DAILY_LOSS_CAP_PCT=5%, CONSECUTIVE_LOSS_PAUSE=3, PAUSE_DAYS=2.
"""
import logging

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(self):
        self.shutdown_for_day = False
        self.pause_until = None          # date before which entries stay suppressed
        self.consecutive_losses = 0

    def entries_allowed(self, today=None) -> tuple[bool, str]:
        if self.shutdown_for_day:
            return False, "SHUTDOWN FOR DAY (daily loss cap hit)"
        if self.pause_until and today and today < self.pause_until:
            return False, (f"paused until {self.pause_until} after "
                           f"{self.consecutive_losses} consecutive losses")
        return True, "ok"

    # Phase B (do not implement now):
    #   register_result(result_r) -> updates consecutive_losses; fires the daily-loss
    #   SHUTDOWN FOR DAY and the 3-loss 2-day-pause flags via journal + Telegram.
