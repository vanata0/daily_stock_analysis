# -*- coding: utf-8 -*-
"""
Tests for AkshareFetcher local CYQ chip-distribution fallback.
Covers _compute_chip_from_kline edge cases and get_chip_distribution fallback strategy.
"""

import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# Stub heavy optional dependencies that may not be installed in the test env
def _stub(name: str, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m

if "litellm" not in sys.modules:
    sys.modules["litellm"] = MagicMock()

if "tenacity" not in sys.modules:
    def _noop_retry(*a, **kw):
        return lambda f: f
    _stub("tenacity",
          retry=_noop_retry,
          stop_after_attempt=lambda n: None,
          wait_exponential=lambda **kw: None,
          retry_if_exception_type=lambda e: None,
          before_sleep_log=lambda *a: None)

if "akshare" not in sys.modules:
    sys.modules["akshare"] = MagicMock()

if "fake_useragent" not in sys.modules:
    _stub("fake_useragent", UserAgent=lambda: MagicMock())

import pandas as pd

from data_provider.akshare_fetcher import AkshareFetcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_records(days: int, price_range: tuple = (10.0, 12.0), turnover: float = 0.02) -> list:
    """Build synthetic daily K-line records for testing."""
    lo_price, hi_price = price_range
    step = (hi_price - lo_price) / max(days - 1, 1)
    records = []
    for i in range(days):
        mid = lo_price + step * i
        spread = max((hi_price - lo_price) * 0.01, 0.0)  # zero spread when range is zero
        records.append({
            "date": f"2024-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            "open": round(mid - spread * 0.5, 4),
            "close": round(mid + spread * 0.5, 4),
            "high": round(mid + spread, 4),
            "low": round(mid - spread, 4),
            "hsl": turnover,
        })
    return records


# ---------------------------------------------------------------------------
# _compute_chip_from_kline
# ---------------------------------------------------------------------------

class TestComputeChipFromKline(unittest.TestCase):

    def test_normal_input_returns_valid_chip(self):
        """Standard K-line input produces all chip fields within expected ranges."""
        records = _make_records(days=20, price_range=(10.0, 12.0), turnover=0.02)
        result = AkshareFetcher._compute_chip_from_kline(records)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["profit_ratio"], 0.0)
        self.assertLessEqual(result["profit_ratio"], 1.0)
        self.assertGreater(result["avg_cost"], 0)
        self.assertLessEqual(result["cost_90_low"], result["cost_90_high"])

    def test_fewer_than_5_records_returns_none(self):
        """Fewer than 5 records → None (insufficient data guard)."""
        records = _make_records(days=4)
        self.assertIsNone(AkshareFetcher._compute_chip_from_kline(records))

    def test_empty_records_returns_none(self):
        """Empty list → None."""
        self.assertIsNone(AkshareFetcher._compute_chip_from_kline([]))

    def test_maxprice_equals_minprice_returns_none(self):
        """Zero price range (涨跌停 / suspended) triggers early return, no ZeroDivisionError."""
        records = _make_records(days=10, price_range=(10.0, 10.0))
        self.assertIsNone(AkshareFetcher._compute_chip_from_kline(records))

    def test_zero_turnover_days_no_crash(self):
        """Zero-turnover days don't crash the algorithm."""
        records = _make_records(days=10, price_range=(10.0, 12.0))
        for r in records[::2]:  # every other day has zero turnover
            r["hsl"] = 0.0
        result = AkshareFetcher._compute_chip_from_kline(records)
        self.assertIsNotNone(result)

    def test_one_bar_board_day_no_crash(self):
        """涨跌停 single-bar day (hi == lo) doesn't crash the triangular distribution."""
        records = _make_records(days=10, price_range=(10.0, 12.0))
        records[-1]["high"] = records[-1]["low"] = records[-1]["close"] = 11.5
        result = AkshareFetcher._compute_chip_from_kline(records)
        self.assertIsNotNone(result)


# ---------------------------------------------------------------------------
# get_chip_distribution fallback strategy
# ---------------------------------------------------------------------------

class TestGetChipDistributionFallback(unittest.TestCase):

    def setUp(self):
        self.fetcher = AkshareFetcher.__new__(AkshareFetcher)
        # Provide the minimal attributes the method relies on
        self.fetcher._circuit_breaker = MagicMock()
        self.fetcher._circuit_breaker.allow_request.return_value = True

    def test_api_failure_triggers_local_fallback(self):
        """When AKShare API raises, local calculation is used and source='local'."""
        # akshare is imported inside the method as `import akshare as ak`,
        # so we patch via sys.modules rather than the module attribute.
        sys.modules["akshare"].stock_cyq_em.side_effect = Exception("API error")
        kline = _make_records(20)
        with patch.object(self.fetcher, "_fetch_kline_for_chip", return_value=kline), \
             patch.object(self.fetcher, "_set_random_user_agent"), \
             patch.object(self.fetcher, "_enforce_rate_limit"):
            result = self.fetcher.get_chip_distribution("000001")
        self.assertIsNotNone(result)
        self.assertEqual(result.source, "local")

    def test_empty_api_response_triggers_local_fallback(self):
        """When AKShare API returns empty DataFrame, local calculation is used."""
        sys.modules["akshare"].stock_cyq_em.side_effect = None
        sys.modules["akshare"].stock_cyq_em.return_value = pd.DataFrame()
        kline = _make_records(20)
        with patch.object(self.fetcher, "_fetch_kline_for_chip", return_value=kline), \
             patch.object(self.fetcher, "_set_random_user_agent"), \
             patch.object(self.fetcher, "_enforce_rate_limit"):
            result = self.fetcher.get_chip_distribution("000001")
        self.assertIsNotNone(result)
        self.assertEqual(result.source, "local")


if __name__ == "__main__":
    unittest.main()
