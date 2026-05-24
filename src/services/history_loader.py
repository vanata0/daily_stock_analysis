"""DB-first K-line history loader for Agent tools.

Provides:
- ContextVar-based frozen target_date propagation across threads
- ``load_history_df``: read from DB first, DataFetcherManager fallback
- Per-stock in-memory session cache: within one stock's analysis run, the
  first successful K-line fetch is cached so subsequent tool calls
  (analyze_trend, get_daily_history, etc.) skip both the DB round-trip
  and any network fetch.  The cache is cleared when
  ``reset_frozen_target_date`` is called at the end of each stock run.

Fixes #1066 – eliminates redundant HTTP requests per stock in Agent mode.
"""
from __future__ import annotations

import contextvars
import logging
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)
_CACHE_MIN_RECORDS = 30

# ---------------------------------------------------------------------------
# Frozen target date (ContextVar) – set once per stock in pipeline, read by
# all agent tool threads via copy_context().run().
# ---------------------------------------------------------------------------
_frozen_target_date: contextvars.ContextVar[Optional[date]] = contextvars.ContextVar(
    "_frozen_target_date", default=None,
)


def set_frozen_target_date(d: date) -> contextvars.Token:
    return _frozen_target_date.set(d)


def get_frozen_target_date() -> Optional[date]:
    return _frozen_target_date.get()


def reset_frozen_target_date(token: contextvars.Token) -> None:
    _frozen_target_date.reset(token)
    _clear_session_df_cache()


# ---------------------------------------------------------------------------
# Per-stock in-memory session cache
# Keyed by (normalized_code, frozen_target_date).  Within one stock analysis
# run every tool call that needs K-line data (get_daily_history, analyze_trend,
# etc.) shares the same frozen_target_date, so subsequent calls return the
# cached DataFrame immediately without hitting DB or network again.
# Different analysis days produce different keys, so no stale data concerns.
# No explicit clearing needed — entries age out naturally when the process
# restarts or frozen_target_date advances to the next trading day.
# ---------------------------------------------------------------------------
_session_df_cache: Dict[Tuple[str, Optional[date]], Tuple[pd.DataFrame, str]] = {}
_session_cache_lock = Lock()


def _clear_session_df_cache() -> None:
    """Clear the per-stock in-memory session cache (called by reset_frozen_target_date)."""
    with _session_cache_lock:
        _session_df_cache.clear()


def _session_cache_key(code: str) -> Tuple[str, Optional[date]]:
    return (code, _frozen_target_date.get())


def _session_cache_get(code: str) -> Optional[Tuple[pd.DataFrame, str]]:
    key = _session_cache_key(code)
    with _session_cache_lock:
        return _session_df_cache.get(key)


def _session_cache_set(code: str, df: pd.DataFrame, source: str) -> None:
    key = _session_cache_key(code)
    with _session_cache_lock:
        _session_df_cache[key] = (df, source)


# ---------------------------------------------------------------------------
# Internal DataFetcherManager singleton (fallback only)
# ---------------------------------------------------------------------------
_fetcher_singleton = None
_fetcher_lock = Lock()


def _get_fetcher_manager():
    global _fetcher_singleton
    if _fetcher_singleton is None:
        with _fetcher_lock:
            if _fetcher_singleton is None:
                from data_provider import DataFetcherManager
                _fetcher_singleton = DataFetcherManager()
    return _fetcher_singleton


# ---------------------------------------------------------------------------
# DB-first history loader
# ---------------------------------------------------------------------------
def _history_code_candidates(stock_code: str) -> Tuple[List[str], str]:
    from data_provider.base import canonical_stock_code, normalize_stock_code

    raw_code = str(stock_code or "").strip()
    normalized_code = canonical_stock_code(normalize_stock_code(raw_code))
    candidates: List[str] = []
    for candidate in (canonical_stock_code(raw_code), normalized_code):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates, normalized_code


def _coerce_bar_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return date.min
    if hasattr(value, "date"):
        try:
            coerced = value.date()
            return coerced if isinstance(coerced, date) else date.min
        except Exception:
            return date.min
    return date.min


def _bar_date(bar: Any) -> date:
    row_date = _coerce_bar_date(getattr(bar, "date", None))
    if row_date != date.min:
        return row_date
    if hasattr(bar, "to_dict"):
        try:
            return _coerce_bar_date((bar.to_dict() or {}).get("date"))
        except Exception:
            return date.min
    return date.min


def _select_best_bars(db, stock_code: str, start: date, end: date) -> Tuple[Optional[str], list]:
    candidates, normalized_code = _history_code_candidates(stock_code)
    best_code = None
    best_bars = []
    best_key = None

    for candidate in candidates:
        bars = list(db.get_data_range(candidate, start, end) or [])
        if not bars:
            continue
        latest_date = max(_bar_date(bar) for bar in bars)
        key = (latest_date, len(bars), candidate == normalized_code)
        if best_key is None or key > best_key:
            best_key = key
            best_code = candidate
            best_bars = bars

    return best_code, best_bars


def load_history_df(
    stock_code: str,
    days: int = 60,
    target_date: Optional[date] = None,
) -> Tuple[Optional[pd.DataFrame], str]:
    """Load K-line history, DB first with DataFetcherManager fallback.

    Returns ``(df, source)`` where *source* is ``"db_cache"`` on DB hit or the
    actual provider name on network fallback.  Returns ``(None, "none")`` when
    both paths fail.

    Within one stock analysis run the first successful result is stored in the
    per-stock in-memory session cache.  Subsequent calls for the same stock
    (e.g. analyze_trend running after get_daily_history) return the cached
    DataFrame immediately, avoiding redundant DB round-trips and network
    fetches.  The cache is cleared by reset_frozen_target_date() at the end of
    each stock run.
    """
    from data_provider.base import normalize_stock_code as _norm
    _norm_code = _norm(str(stock_code or "").strip())

    # --- 0. In-memory session cache (fastest path) -------------------------
    cached = _session_cache_get(_norm_code)
    if cached is not None:
        cached_df, cached_source = cached
        if cached_df is not None and not cached_df.empty:
            logger.debug(
                "load_history_df(%s): session-cache hit (source=%s, rows=%d)",
                stock_code, cached_source, len(cached_df),
            )
            return cached_df, cached_source

    from src.storage import get_db

    # Resolve effective end date
    if target_date is not None:
        end = target_date
    else:
        frozen = get_frozen_target_date()
        end = frozen if frozen else date.today()

    # Calendar-day buffer: ~1.8x trading days + margin for long holidays
    start = end - timedelta(days=int(days * 1.8) + 10)

    # --- 1. DB lookup (canonical code, then prefix-stripped fallback) ------
    try:
        db = get_db()
        _code, bars = _select_best_bars(db, stock_code, start, end)
        required_records = max(min(days, _CACHE_MIN_RECORDS), 1)
        latest_date = max((_bar_date(bar) for bar in bars), default=date.min)
        if bars and latest_date >= end and len(bars) >= required_records:
            df = pd.DataFrame([b.to_dict() for b in bars])
            logger.debug(
                "load_history_df(%s): %d bars from DB (requested %d)",
                stock_code, len(df), days,
            )
            _session_cache_set(_norm_code, df, "db_cache")
            return df, "db_cache"
    except Exception as e:
        logger.debug("load_history_df(%s): DB read failed: %s", stock_code, e)

    # --- 2. Network fallback via singleton DataFetcherManager -------------
    try:
        manager = _get_fetcher_manager()
        df, source = manager.get_daily_data(stock_code, days=days)
        if df is not None and not df.empty:
            _session_cache_set(_norm_code, df, source)
            return df, source
    except Exception as e:
        logger.warning("load_history_df(%s): DataFetcherManager failed: %s", stock_code, e)

    return None, "none"
