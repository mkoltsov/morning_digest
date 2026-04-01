"""circuit_breaker.py — Retry + circuit breaker for unreliable web digest sources.

Usage:
    cb = CircuitBreaker("slickdeals")
    result = cb.call(fetch_fn, *args)

State is persisted in .circuit_state.json so open circuits survive restarts.
A circuit opens after FAILURE_THRESHOLD consecutive failures and auto-resets
after RESET_TIMEOUT seconds.
"""

import json
import time
import logging
from pathlib import Path
from functools import wraps

log = logging.getLogger(__name__)

_STATE_FILE = Path(__file__).parent / ".circuit_state.json"

FAILURE_THRESHOLD = 3     # failures before opening circuit
RESET_TIMEOUT     = 1800  # seconds before half-open retry (30 min)
MAX_RETRIES       = 2     # attempts per call before counting as failure
RETRY_DELAY       = 3     # seconds between retries


# ── State persistence ─────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_state(state: dict):
    try:
        _STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


# ── Circuit Breaker ───────────────────────────────────────────────────────────

class CircuitBreaker:
    """
    States:
      CLOSED  — normal operation, calls go through
      OPEN    — circuit tripped, calls skipped until RESET_TIMEOUT
      HALF    — one test call allowed to see if source recovered
    """

    def __init__(self, name: str):
        self.name = name

    def _get(self) -> dict:
        return _load_state().get(self.name, {
            "state": "CLOSED",
            "failures": 0,
            "last_failure": 0,
            "last_success": 0,
            "last_result": None,
        })

    def _set(self, data: dict):
        state = _load_state()
        state[self.name] = data
        _save_state(state)

    def call(self, fn, *args, **kwargs):
        """Call fn with retries. Returns (success: bool, result)."""
        data = self._get()
        now  = time.time()

        # OPEN — check if enough time passed to try again
        if data["state"] == "OPEN":
            if now - data["last_failure"] < RESET_TIMEOUT:
                mins = int((RESET_TIMEOUT - (now - data["last_failure"])) / 60)
                log.warning(f"[{self.name}] circuit OPEN, skipping (resets in ~{mins}m)")
                cached = data.get("last_result")
                if cached:
                    return True, cached + f'<li class="from"><em>(cached — source temporarily unavailable)</em></li>'
                return False, f"<ul><li><em>⚠️ {self.name} temporarily unavailable — will retry in ~{mins} min</em></li></ul>"
            else:
                log.info(f"[{self.name}] circuit HALF-OPEN, attempting recovery call")
                data["state"] = "HALF"
                self._set(data)

        # CLOSED or HALF — attempt with retries
        last_exc = None
        for attempt in range(1, MAX_RETRIES + 2):  # +2 = MAX_RETRIES retries after first try
            try:
                result = fn(*args, **kwargs)
                # Success — reset circuit
                data["state"]        = "CLOSED"
                data["failures"]     = 0
                data["last_success"] = now
                data["last_result"]  = result
                self._set(data)
                if attempt > 1:
                    log.info(f"[{self.name}] succeeded on attempt {attempt}")
                return True, result
            except Exception as e:
                last_exc = e
                if attempt <= MAX_RETRIES:
                    log.warning(f"[{self.name}] attempt {attempt} failed: {e} — retrying in {RETRY_DELAY}s")
                    time.sleep(RETRY_DELAY)

        # All attempts failed
        data["failures"]     += 1
        data["last_failure"]  = now
        log.error(f"[{self.name}] failed after {MAX_RETRIES + 1} attempts: {last_exc}")

        if data["failures"] >= FAILURE_THRESHOLD or data["state"] == "HALF":
            data["state"] = "OPEN"
            log.error(f"[{self.name}] circuit OPENED after {data['failures']} failures")

        self._set(data)

        cached = data.get("last_result")
        if cached:
            label_html = cached + '<li class="from"><em>(cached — live fetch failed)</em></li>'
            return False, label_html
        return False, f"<ul><li><em>Error: {last_exc}</em></li></ul>"
