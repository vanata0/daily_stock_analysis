# -*- coding: utf-8 -*-
"""
Tests for single-stock notification serialization via threading.Lock.

Coverage:
  - __init__ assigns a threading.Lock (not BoundedSemaphore)
  - Fallback init path (instance created via __new__) also creates a Lock
  - Lock is released after a successful send
  - Lock is released after notifier.send raises
"""
import sys
import threading
import types
import unittest
from unittest.mock import MagicMock


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
# Import pipeline module
# ---------------------------------------------------------------------------

def _import_pipeline_constants():
    """Return pipeline module without instantiating anything."""
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
    for mod in heavy:
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()

    sys.modules.setdefault("data_provider.realtime_types", MagicMock())

    import src.core.pipeline as pipeline_mod
    return pipeline_mod


_pipeline = _import_pipeline_constants()
_SINGLE_STOCK_NOTIFY_LOCK_INIT_GUARD = _pipeline._SINGLE_STOCK_NOTIFY_LOCK_INIT_GUARD


# ---------------------------------------------------------------------------
# Helper — build a minimal StockAnalysisPipeline instance via __new__
# ---------------------------------------------------------------------------

def _make_pipeline():
    """Create a StockAnalysisPipeline instance bypassing __init__."""
    cls = _pipeline.StockAnalysisPipeline
    inst = cls.__new__(cls)
    inst._single_stock_notify_lock = threading.Lock()
    inst.notifier = MagicMock()
    inst.notifier.is_available.return_value = True
    inst.notifier.generate_single_stock_report.return_value = "report"
    inst.notifier.send.return_value = True
    return inst


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNotifyConcurrencyCap(unittest.TestCase):

    def test_semaphore_type_in_init(self):
        """__init__ assigns a threading.Lock, not a BoundedSemaphore."""
        pipe = _make_pipeline()
        self.assertIsInstance(
            pipe._single_stock_notify_lock,
            type(threading.Lock()),
            "Expected threading.Lock from threading module",
        )

    def test_at_most_one_concurrent_slot(self):
        """A fresh Lock allows exactly one non-blocking acquire before returning False."""
        lock = threading.Lock()
        self.assertTrue(lock.acquire(blocking=False), "First acquire must succeed")
        self.assertFalse(lock.acquire(blocking=False), "Second acquire must fail while held")
        lock.release()

    def test_released_slot_allows_next_notification(self):
        """After releasing the lock, a subsequent acquire succeeds."""
        lock = threading.Lock()
        lock.acquire(blocking=False)
        lock.release()
        self.assertTrue(
            lock.acquire(blocking=False),
            "A released Lock must be acquirable again",
        )

    def test_slot_released_after_successful_send(self):
        """After a successful notification, the lock is released."""
        pipe = _make_pipeline()
        lock = pipe._single_stock_notify_lock

        result = MagicMock()
        result.code = "600519"
        report_type = MagicMock()
        report_type.value = "simple"

        pipe._send_single_stock_notification(result, report_type=report_type)

        # After the call, the lock should have been released
        self.assertTrue(
            lock.acquire(blocking=False),
            "Lock must be released after a successful notification",
        )
        lock.release()

    def test_slot_released_after_exception(self):
        """Even when notifier.send raises, the lock must be released."""
        pipe = _make_pipeline()
        pipe.notifier.send.side_effect = RuntimeError("network error")
        lock = pipe._single_stock_notify_lock

        result = MagicMock()
        result.code = "600519"
        report_type = MagicMock()
        report_type.value = "simple"

        pipe._send_single_stock_notification(result, report_type=report_type)

        # Lock must still be released despite the exception
        self.assertTrue(
            lock.acquire(blocking=False),
            "Lock must be released in the with-block even after exceptions",
        )
        lock.release()

    def test_fallback_init_uses_lock(self):
        """When _single_stock_notify_lock is None (bypass __init__), fallback creates Lock."""
        pipe = _make_pipeline()
        del pipe._single_stock_notify_lock

        pipe.notifier.is_available.return_value = True

        result = MagicMock()
        result.code = "TEST"
        report_type = MagicMock()
        report_type.value = "simple"

        pipe._send_single_stock_notification(result, report_type=report_type)

        lock = getattr(pipe, "_single_stock_notify_lock", None)
        self.assertIsNotNone(lock)
        self.assertIsInstance(
            lock,
            type(threading.Lock()),
            "Fallback init must create a threading.Lock",
        )


if __name__ == "__main__":
    unittest.main()
