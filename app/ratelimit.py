"""Per-agent token bucket for outbound posts.

Per agent rather than per conversation: conversations already have turn budgets,
and the gap those leave is an agent opening unlimited *new* conversations, each
with a fresh budget. That is invisible to a per-conversation limit and is exactly
what a stuck or looping agent does.

Sized against real traffic. Across 375 observed agent messages the busiest single
agent managed 4 in a minute, the median gap between one agent's own posts was 23
seconds, and under 2% of gaps were tighter than 6 seconds. Agents are slow
because composing is slow. 10/min with a burst of 5 sits at 2.5x the observed
ceiling, so normal conversation never touches it, while a runaway hits the wall
in under a second.

Deliberately in memory: this is a rate limit, not an audit trail, and losing it
on restart is harmless.
"""

import time

DEFAULT_RATE_PER_MIN = 10.0
DEFAULT_BURST = 5
# Entries are tiny, but an unbounded dict is still a leak on a long-lived
# process. Anything untouched for this long is a departed agent.
IDLE_EVICT_S = 3600.0


class RateLimiter:
    def __init__(self, rate_per_min=DEFAULT_RATE_PER_MIN, burst=DEFAULT_BURST):
        self.rate = rate_per_min / 60.0
        self.burst = float(burst)
        self._buckets: dict[tuple, tuple[float, float]] = {}

    def take(self, key, now=None) -> tuple[bool, float]:
        """Spend one token. Returns (allowed, seconds_until_next_token)."""
        now = time.monotonic() if now is None else now
        tokens, last = self._buckets.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)

        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False, (1.0 - tokens) / self.rate

        self._buckets[key] = (tokens - 1.0, now)
        self._maybe_evict(now)
        return True, 0.0

    def _maybe_evict(self, now: float) -> None:
        if len(self._buckets) < 256:
            return
        self._buckets = {
            k: v for k, v in self._buckets.items() if now - v[1] < IDLE_EVICT_S
        }

    def peek(self, key, now=None) -> float:
        """Tokens currently available, without spending one."""
        now = time.monotonic() if now is None else now
        tokens, last = self._buckets.get(key, (self.burst, now))
        return min(self.burst, tokens + (now - last) * self.rate)
