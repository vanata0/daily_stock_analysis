# -*- coding: utf-8 -*-
"""
Unit tests for ScreenerDBFetcher — lock-conflict degradation behaviour.

TC1: _open_con() raises duckdb.IOException → DataFetchError is raised,
     _available remains True (可重试，不永久禁用).

TC2: DataFetchError from ScreenerDBFetcher triggers fallback to the next
     fetcher in the DataFetcherManager chain (降级链完整).
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pandas as pd

from data_provider.base import DataFetchError, DataFetcherManager
from data_provider.screener_db_fetcher import ScreenerDBFetcher


# ── helpers ──────────────────────────────────────────────────────────


def _make_fetcher(*, db_exists: bool = True) -> ScreenerDBFetcher:
    """Return a ScreenerDBFetcher whose DB path existence is controlled."""
    f = ScreenerDBFetcher()
    f._db_path = "/fake/market.duckdb"
    with patch("pathlib.Path.exists", return_value=db_exists):
        pass  # just set up the path
    return f


def _minimal_df() -> pd.DataFrame:
    """Minimal K-line DataFrame a fallback fetcher might return."""
    return pd.DataFrame(
        {
            "code":   ["000001"],
            "date":   ["2026-01-02"],
            "open":   [10.0],
            "high":   [11.0],
            "low":    [9.5],
            "close":  [10.5],
            "volume": [1_000_000.0],
            "amount": [10_500_000.0],
        }
    )


# ── TC1: _open_con IOException → DataFetchError, _available stays True ──


class TestOpenConLockConflict(unittest.TestCase):
    """TC1 — DuckDB lock conflict maps to DataFetchError without disabling the fetcher."""

    def _get_duckdb_ioerror(self):
        """Return a real duckdb.IOException if duckdb is importable, else a generic IOError."""
        try:
            import duckdb
            # Construct a real IOException by attempting to trigger one on a dummy path
            exc = duckdb.IOException("Could not set lock on file: Conflicting lock")
            return exc
        except (ImportError, AttributeError):
            return IOError("Conflicting lock is held")

    def test_lock_conflict_raises_data_fetch_error(self):
        """_open_con IOException is wrapped in DataFetchError."""
        fetcher = _make_fetcher()
        lock_exc = self._get_duckdb_ioerror()

        with patch.object(fetcher, "_open_con", side_effect=lock_exc), \
             patch("pathlib.Path.exists", return_value=True), \
             patch.object(
                 type(fetcher), "is_available", new_callable=PropertyMock, return_value=True
             ):
            with self.assertRaises(DataFetchError) as ctx:
                fetcher._fetch_raw_data("000001", "2026-01-01", "2026-12-31")

        self.assertIn("无法连接", str(ctx.exception))

    def test_available_remains_true_after_lock_conflict(self):
        """_available is NOT set to False on a lock conflict — fetcher stays retriable."""
        fetcher = _make_fetcher()
        lock_exc = self._get_duckdb_ioerror()

        # Simulate a successful is_available check (file exists, duckdb importable)
        with patch("pathlib.Path.exists", return_value=True):
            _ = fetcher.is_available  # sets _available = True

        # Now simulate a lock conflict on the next query
        with patch.object(fetcher, "_open_con", side_effect=lock_exc), \
             patch.object(
                 type(fetcher), "is_available", new_callable=PropertyMock, return_value=True
             ):
            try:
                fetcher._fetch_raw_data("000001", "2026-01-01", "2026-12-31")
            except DataFetchError:
                pass

        # _available must NOT have been flipped to False
        # (it should still be True, meaning the fetcher can be retried)
        self.assertIsNot(fetcher._available, False)

    def test_file_not_found_sets_available_false(self):
        """DB 文件不存在时 _available 才变 False（对照组）。"""
        fetcher = _make_fetcher(db_exists=False)
        with patch("pathlib.Path.exists", return_value=False):
            result = fetcher.is_available
        self.assertFalse(result)
        self.assertFalse(fetcher._available)


# ── TC2: DataFetcherManager fallback chain ───────────────────────────


class _AlwaysAvailableFetcher:
    """Stub fetcher that always succeeds — stands in for AkShare/Tushare."""

    name = "StubFetcher"
    priority = 10

    def __init__(self, return_df: pd.DataFrame):
        self._df = return_df
        self.call_count = 0

    @property
    def is_available(self) -> bool:
        return True

    def get_daily_data(self, stock_code: str, start_date: str, end_date: str, **kw) -> pd.DataFrame:
        self.call_count += 1
        return self._df


class _AlwaysFailFetcher:
    """Stub fetcher that always raises DataFetchError."""

    name = "FailFetcher"
    priority = -1

    @property
    def is_available(self) -> bool:
        return True

    def get_daily_data(self, stock_code: str, start_date: str, end_date: str, **kw) -> pd.DataFrame:
        raise DataFetchError("FailFetcher: simulated lock conflict")


class TestManagerFallbackChain(unittest.TestCase):
    """TC2 — DataFetchError triggers fallback to the next fetcher."""

    def _make_manager_with_two_fetchers(
        self, primary_fetcher, fallback_fetcher
    ) -> DataFetcherManager:
        """Build a minimal DataFetcherManager from an explicit fetcher list."""
        mgr = DataFetcherManager.__new__(DataFetcherManager)
        # Minimal init — only what get_daily_data uses
        from threading import RLock
        mgr._fetchers = sorted([primary_fetcher, fallback_fetcher], key=lambda f: f.priority)
        mgr._fetchers_lock = RLock()
        mgr._fetchers_by_name = {f.name: f for f in mgr._fetchers}
        mgr._fetcher_call_locks = {}
        mgr._fetcher_call_locks_lock = RLock()
        mgr._stock_name_cache = {}
        mgr._stock_name_cache_lock = RLock()
        mgr._tickflow_fetcher = None
        mgr._tickflow_api_key = None
        mgr._tickflow_lock = RLock()
        mgr._fundamental_cache = {}
        mgr._fundamental_cache_lock = RLock()
        from threading import BoundedSemaphore
        mgr._fundamental_timeout_worker_limit = 1
        mgr._fundamental_timeout_slots = BoundedSemaphore(1)
        return mgr

    def test_screener_lock_falls_back_to_next_fetcher(self):
        """Manager skips ScreenerDBFetcher on DataFetchError and tries the stub fallback."""
        fallback = _AlwaysAvailableFetcher(_minimal_df())
        primary = _AlwaysFailFetcher()

        mgr = self._make_manager_with_two_fetchers(primary, fallback)

        with patch.object(mgr, "_get_fetchers_snapshot", return_value=mgr._fetchers), \
             patch.object(
                 DataFetcherManager,
                 "_filter_fetchers_by_capability",
                 return_value=mgr._fetchers,
             ), \
             patch.object(
                 DataFetcherManager,
                 "_call_fetcher_method",
                 side_effect=lambda f, method, **kw: f.get_daily_data(**kw),
             ):
            result, source = mgr.get_daily_data("000001", "2026-01-01", "2026-12-31")

        self.assertEqual(source, "StubFetcher")
        self.assertEqual(fallback.call_count, 1)
        self.assertFalse(result.empty)

    def test_all_fetchers_fail_raises_data_fetch_error(self):
        """When all fetchers in chain fail, DataFetchError is raised."""
        primary = _AlwaysFailFetcher()
        # Second fetcher also fails
        secondary = _AlwaysFailFetcher()
        secondary.name = "FailFetcher2"
        secondary.priority = 5

        mgr = self._make_manager_with_two_fetchers(primary, secondary)

        with patch.object(mgr, "_get_fetchers_snapshot", return_value=mgr._fetchers), \
             patch.object(
                 DataFetcherManager,
                 "_filter_fetchers_by_capability",
                 return_value=mgr._fetchers,
             ), \
             patch.object(
                 DataFetcherManager,
                 "_call_fetcher_method",
                 side_effect=lambda f, method, **kw: f.get_daily_data(**kw),
             ):
            with self.assertRaises(DataFetchError):
                mgr.get_daily_data("000001", "2026-01-01", "2026-12-31")


if __name__ == "__main__":
    unittest.main()
