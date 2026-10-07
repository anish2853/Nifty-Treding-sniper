"""Self-restart watchdog helpers (Prompt 2).

FeedHealth tracks consecutive failures per data source. At FEED_FAILURE_ALERT_
THRESHOLD (5) in a row it emits ONE alert per outage streak and the pipeline keeps
running with that factor MISSING - a single feed must never take the bot down.
It also keeps per-source cycle counts for the EOD data-quality block.
"""
import logging
from collections import defaultdict

log = logging.getLogger(__name__)


class FeedHealth:
    def __init__(self, threshold: int = 5):
        self.threshold = threshold
        self._streak = defaultdict(int)
        self._alarmed = set()
        self._total = defaultdict(int)
        self._missing = defaultdict(int)

    def record(self, source: str, ok: bool) -> str | None:
        """Record one check. Returns an alert message exactly when a failure streak
        reaches the threshold (once per streak); None otherwise."""
        self._total[source] += 1
        if ok:
            if self._streak[source] >= self.threshold:
                log.info("Feed %s recovered after %d consecutive failures",
                         source, self._streak[source])
                self._alarmed.discard(source)
            self._streak[source] = 0
            return None
        self._missing[source] += 1
        self._streak[source] += 1
        if self._streak[source] == self.threshold and source not in self._alarmed:
            self._alarmed.add(source)
            return source  # caller turns this into messages.feed_alert(source, threshold)
        return None

    def quality_lines(self) -> list:
        """'source: ok/total OK (n missing)' lines for the EOD summary."""
        lines = []
        for source in sorted(set(self._total)):
            total, missing = self._total[source], self._missing[source]
            if missing == 0:
                lines.append(f"{source}: {total}/{total} OK")
            else:
                lines.append(f"{source}: {total - missing}/{total} OK ({missing} missing)")
        return lines
