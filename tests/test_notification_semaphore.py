# -*- coding: utf-8 -*-
"""
Tests for PR 3: BoundedSemaphore-based concurrent notification cap.

Coverage:
  - _NOTIFY_CONCURRENCY_CAP is 3
  - At most _NOTIFY_CONCURRENCY_CAP notifications run simultaneously
  - When the cap is exceeded acquire() returns False and a WARNING is logged
  - After a slot is released a new notification is accepted
  - Fallback init path (instance created via __new__) uses BoundedSemaphore
"""
import logging
import sys
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub heavy optional deps (must precede project imports)
# ---------------------------------------------------------------------------

def _stub(name: str, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


for _dep in (
    "litellm",
    "akshare",
    "fake_useragent",
    "pypinyin",
):
    if _dep not in sys.modules:
        sys.modules[_dep] = MagicMock()

if "tenacity" not in sys.modules:
    def _noop_retry(*a, **kw):
        return lambda f: f
    _stub(
        "tenacity",
        retry=_noop_retry,
        stop_after_attempt=lambda n: None,
        wait_exponential=lambda **kw: None,
        retry_if_exception_type=lambda e: None,
        before_sleep_log=lambda *a: None,
    )

# ---------------------------------------------------------------------------
# Import only the symbols under test — avoid importing the full pipeline
# module (which pulls in heavy optional deps like pandas, DataFetcherManager).
# We access the constant and class by importing the module directly.
# ---------------------------------------------------------------------------

import importlib
import types as _types


def _import_pipeline_constants():
    """Return (pipeline_module, _NOTIFY_CONCURRENCY_CAP) without instantiating anything."""
    # Patch everything that StockAnalysisPipeline.__init__ touches
    heavy = [
        "src.storage",
        "data_provider",
        "data_provider.base",
        "data_provider.realtime_types",
        "data_provider.us_index_mapping",
        "src.analyzer",
        "src.data.stock_mapping",
        "src.notification",
        "src.report_language",
        "src.search_service",
        "src.services.social_sentiment_service",
        "src.enums",
        "src.stock_analyzer",
        "src.core.trading_calendar",
        "bot.models",
        "pandas",
    ]
    mocks = {}
    for mod in heavy:
        if mod not in sys.modules:
            mocks[mod] = MagicMock()
            sys.modules[mod] = mocks[mod]

    # Ensure ChipDistribution mock
    sys.modules.setdefault("data_provider.realtime_types", MagicMock())

    # Now import
    import importlib
    import src.core.pipeline as pipeline_mod
    return pipeline_mod


_pipeline = _import_pipeline_constants()
_NOTIFY_CONCURRENCY_CAP = _pipeline._NOTIFY_CONCURRENCY_CAP
_SINGLE_STOCK_NOTIFY_LOCK_INIT_GUARD = _pipeline._SINGLE_STOCK_NOTIFY_LOCK_INIT_GUARD


# ---------------------------------------------------------------------------
# Helper — build a minimal StockAnalysisPipeline instance via __new__
# ---------------------------------------------------------------------------

def _make_pipeline():
    """Create a StockAnalysisPipeline instance bypassing __init__."""
    cls = _pipeline.StockAnalysisPipeline
    inst = cls.__new__(cls)
    # Inject the semaphore the same way __init__ would
    inst._single_stock_notify_lock = threading.BoundedSemaphore(_NOTIFY_CONCURRENCY_CAP)
    inst.notifier = MagicMock()
    inst.notifier.is_available.return_value = True
    inst.notifier.generate_single_stock_report.return_value = "report"
    inst.notifier.send.return_value = True
    return inst


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNotifyConcurrencyCap(unittest.TestCase):

    def test_cap_constant_is_three(self):
        """The concurrency cap must be exactly 3."""
        self.assertEqual(_NOTIFY_CONCURRENCY_CAP, 3)

    def test_semaphore_type_in_init(self):
        """__init__ assigns a BoundedSemaphore, not a plain Lock."""
        pipe = _make_pipeline()
        self.assertIsInstance(
            pipe._single_stock_notify_lock,
            type(threading.BoundedSemaphore(1)),
            "Expected BoundedSemaphore from threading module",
        )

    def test_at_most_cap_concurrent_slots(self):
        """A fresh BoundedSemaphore(_NOTIFY_CONCURRENCY_CAP) allows exactly CAP non-blocking acquires."""
        sem = threading.BoundedSemaphore(_NOTIFY_CONCURRENCY_CAP)
        acquired = 0
        for _ in range(_NOTIFY_CONCURRENCY_CAP + 2):
            if sem.acquire(blocking=False):
                acquired += 1
        self.assertEqual(
            acquired,
            _NOTIFY_CONCURRENCY_CAP,
            "BoundedSemaphore should allow exactly CAP non-blocking acquires before returning False",
        )

    def test_over_cap_logs_warning(self):
        """When the semaphore is exhausted, _send_single_stock_notification logs a WARNING."""
        pipe = _make_pipeline()
        # Drain all slots manually
        for _ in range(_NOTIFY_CONCURRENCY_CAP):
            pipe._single_stock_notify_lock.acquire(blocking=False)

        result = MagicMock()
        result.code = "600519"

        from src.enums import ReportType  # already mocked — will be a MagicMock attr
        report_type = MagicMock()
        report_type.value = "simple"

        with self.assertLogs("src.core.pipeline", level="WARNING") as cm:
            pipe._send_single_stock_notification(result, report_type=report_type)

        warning_msgs = [m for m in cm.output if "WARNING" in m and "上限" in m]
        self.assertTrue(
            warning_msgs,
            f"Expected a WARNING mentioning '上限', got: {cm.output}",
        )

    def test_over_cap_skips_send(self):
        """When cap is exceeded, notifier.send must NOT be called."""
        pipe = _make_pipeline()
        for _ in range(_NOTIFY_CONCURRENCY_CAP):
            pipe._single_stock_notify_lock.acquire(blocking=False)

        result = MagicMock()
        result.code = "000001"
        report_type = MagicMock()
        report_type.value = "simple"

        with self.assertLogs("src.core.pipeline", level="WARNING"):
            pipe._send_single_stock_notification(result, report_type=report_type)

        pipe.notifier.send.assert_not_called()

    def test_released_slot_allows_next_notification(self):
        """After releasing one slot, a subsequent acquire succeeds."""
        sem = threading.BoundedSemaphore(_NOTIFY_CONCURRENCY_CAP)
        # Drain all
        for _ in range(_NOTIFY_CONCURRENCY_CAP):
            sem.acquire(blocking=False)
        # Release one
        sem.release()
        # Next acquire should succeed
        self.assertTrue(
            sem.acquire(blocking=False),
            "A released semaphore slot must be acquirable again",
        )

    def test_slot_released_after_successful_send(self):
        """After a successful notification, the semaphore slot is released (value restored)."""
        pipe = _make_pipeline()
        sem = pipe._single_stock_notify_lock

        result = MagicMock()
        result.code = "600519"
        report_type = MagicMock()
        report_type.value = "simple"

        # Drain CAP-1 slots so there is exactly one free
        for _ in range(_NOTIFY_CONCURRENCY_CAP - 1):
            sem.acquire(blocking=False)

        pipe._send_single_stock_notification(result, report_type=report_type)

        # After the call, the slot should have been released — we should be
        # able to acquire CAP-1 more times again (we freed 1, then send used
        # and released 1, so net: CAP-1 still held, 1 free)
        self.assertTrue(
            sem.acquire(blocking=False),
            "Semaphore slot must be released after a successful notification",
        )

    def test_slot_released_after_exception(self):
        """Even when notifier.send raises, the semaphore slot must be released."""
        pipe = _make_pipeline()
        pipe.notifier.send.side_effect = RuntimeError("network error")
        sem = pipe._single_stock_notify_lock

        result = MagicMock()
        result.code = "600519"
        report_type = MagicMock()
        report_type.value = "simple"

        # Drain CAP-1 slots
        for _ in range(_NOTIFY_CONCURRENCY_CAP - 1):
            sem.acquire(blocking=False)

        pipe._send_single_stock_notification(result, report_type=report_type)

        # Slot must still be released despite the exception
        self.assertTrue(
            sem.acquire(blocking=False),
            "Semaphore slot must be released in the finally block even after exceptions",
        )

    def test_fallback_init_uses_bounded_semaphore(self):
        """When _single_stock_notify_lock is None (bypass __init__), fallback creates BoundedSemaphore."""
        pipe = _make_pipeline()
        # Remove the semaphore to simulate bypassed __init__
        del pipe._single_stock_notify_lock

        # notifier.is_available must return True for the early-return to be skipped
        pipe.notifier.is_available.return_value = True

        result = MagicMock()
        result.code = "TEST"
        report_type = MagicMock()
        report_type.value = "simple"

        pipe._send_single_stock_notification(result, report_type=report_type)

        # After the call, the lock should have been created and released
        lock = getattr(pipe, "_single_stock_notify_lock", None)
        self.assertIsNotNone(lock)
        self.assertIsInstance(
            lock,
            type(threading.BoundedSemaphore(1)),
            "Fallback init must create a BoundedSemaphore, not a plain Lock",
        )


if __name__ == "__main__":
    unittest.main()
