# -*- coding: utf-8 -*-
"""KplFetcher 单元测试：可用性契约、日线取数与标准列换算。"""

import importlib.util
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

try:
    json_repair_available = importlib.util.find_spec("json_repair") is not None
except ValueError:
    json_repair_available = "json_repair" in sys.modules

if not json_repair_available and "json_repair" not in sys.modules:
    sys.modules["json_repair"] = MagicMock()

from data_provider.base import DataFetchError
from data_provider.kpl_fetcher import KplFetcher
from data_provider.kpl_http import KplRequestError


def _client(days=None, valid=True, get_side_effect=None):
    """构造一个仿 KplHttpClient。"""
    c = MagicMock()
    c.is_credential_valid.return_value = valid
    if get_side_effect is not None:
        c.get.side_effect = get_side_effect
    else:
        c.get.return_value = {"stock_id": "600519", "count": len(days or []), "days": days or []}
    return c


# 取自 2026-07-23/24 实测：volume 按「手」，amount 按「元」
REAL_DAYS = [
    {"date": "20260723", "open": 1299.8, "close": 1292.01, "high": 1303.0,
     "low": 1285.43, "volume": 33917, "amount": 4392505599, "turnover_pct": 0.27},
    {"date": "20260724", "open": 1305.0, "close": 1297.41, "high": 1309.21,
     "low": 1286.2, "volume": 35698, "amount": 4622242878, "turnover_pct": 0.29},
]


class TestKplFetcherAvailability(unittest.TestCase):
    def test_is_available_is_a_plain_method_not_property(self) -> None:
        """必须是普通方法。

        DataFetcherManager 用 callable() 探测可用性，写成 @property 会让
        整个检查被静默跳过（StockNewAPIFetcher / ScreenerDBFetcher 踩过）。
        """
        attr = getattr(KplFetcher, "is_available", None)
        self.assertFalse(isinstance(attr, property), "is_available 不能是 property")
        self.assertTrue(callable(attr))

    def test_is_available_delegates_to_credential_probe(self) -> None:
        c = _client(valid=False)
        f = KplFetcher(client=c)
        self.assertFalse(f.is_available())
        c.is_credential_valid.assert_called()

    def test_priority_read_from_config(self) -> None:
        cfg = SimpleNamespace(kpl_api_base="http://x", kpl_timeout=5, kpl_priority=7)
        with patch.object(KplFetcher, "_safe_config", staticmethod(lambda: cfg)):
            self.assertEqual(KplFetcher(client=_client()).priority, 7)

    def test_explicit_priority_overrides_config(self) -> None:
        cfg = SimpleNamespace(kpl_api_base="http://x", kpl_timeout=5, kpl_priority=7)
        with patch.object(KplFetcher, "_safe_config", staticmethod(lambda: cfg)):
            self.assertEqual(KplFetcher(priority=3, client=_client()).priority, 3)


class TestKplFetcherFetchRaw(unittest.TestCase):
    def test_unavailable_source_raises(self) -> None:
        f = KplFetcher(client=_client(valid=False))
        with self.assertRaises(DataFetchError):
            f._fetch_raw_data("600519", "2026-07-01", "2026-07-24")

    def test_non_a_share_code_rejected(self) -> None:
        f = KplFetcher(client=_client(days=REAL_DAYS))
        for bad in ("AAPL", "00700", "hk00700"):
            with self.assertRaises(DataFetchError, msg=f"应拒绝 {bad}"):
                f._fetch_raw_data(bad, "2026-07-01", "2026-07-24")

    def test_upstream_error_becomes_data_fetch_error(self) -> None:
        """上游异常必须转成 DataFetchError，让 manager 记降级而不是崩溃。"""
        f = KplFetcher(client=_client(get_side_effect=KplRequestError("boom")))
        with self.assertRaises(DataFetchError):
            f._fetch_raw_data("600519", "2026-07-01", "2026-07-24")

    def test_empty_days_raises(self) -> None:
        f = KplFetcher(client=_client(days=[]))
        with self.assertRaises(DataFetchError):
            f._fetch_raw_data("600519", "2026-07-01", "2026-07-24")

    def test_range_outside_data_raises(self) -> None:
        f = KplFetcher(client=_client(days=REAL_DAYS))
        with self.assertRaises(DataFetchError):
            f._fetch_raw_data("600519", "2020-01-01", "2020-01-31")

    def test_range_filter_applied(self) -> None:
        """上游不支持日期参数，必须由本地按区间裁剪。"""
        f = KplFetcher(client=_client(days=REAL_DAYS))
        df = f._fetch_raw_data("600519", "2026-07-24", "2026-07-24")
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["date"], "2026-07-24")

    def test_unparsable_dates_dropped(self) -> None:
        days = [{"date": "bad", "open": 1, "close": 1, "high": 1, "low": 1,
                 "volume": 1, "amount": 1}]
        f = KplFetcher(client=_client(days=days))
        with self.assertRaises(DataFetchError):
            f._fetch_raw_data("600519", "2026-07-01", "2026-07-24")


class TestKplFetcherNormalize(unittest.TestCase):
    """标准列换算 —— 量纲错误会直接污染下游技术指标。"""

    def setUp(self) -> None:
        self.fetcher = KplFetcher(client=_client(days=REAL_DAYS))
        raw = self.fetcher._fetch_raw_data("600519", "2026-07-01", "2026-07-24")
        self.df = self.fetcher._normalize_data(raw, "600519")

    def test_standard_columns_present(self) -> None:
        for col in ("code", "date", "open", "high", "low", "close", "volume", "amount", "pct_chg"):
            self.assertIn(col, self.df.columns)

    def test_volume_converted_from_hand_to_share(self) -> None:
        """上游按「手」，DSA 标准列按「股」。"""
        self.assertEqual(self.df.iloc[-1]["volume"], 35698 * 100)

    def test_amount_not_rescaled(self) -> None:
        """上游 amount 已是「元」，重复换算会放大 1000 倍。"""
        self.assertEqual(self.df.iloc[-1]["amount"], 4622242878)

    def test_average_price_within_daily_range(self) -> None:
        """amount/volume 必须落在当日 low~high 内，这是量纲自洽的硬判据。"""
        row = self.df.iloc[-1]
        avg = row["amount"] / row["volume"]
        self.assertGreaterEqual(avg, row["low"])
        self.assertLessEqual(avg, row["high"])

    def test_pct_chg_derived_from_close(self) -> None:
        """上游不返回涨跌幅，必须由收盘价推导。"""
        expected = (1297.41 - 1292.01) / 1292.01 * 100
        self.assertAlmostEqual(self.df.iloc[-1]["pct_chg"], expected, places=6)

    def test_first_row_pct_chg_is_nan(self) -> None:
        """首行没有前收盘，应为 NaN 而不是 0（0 会被误读成平盘）。"""
        self.assertTrue(pd.isna(self.df.iloc[0]["pct_chg"]))

    def test_date_normalized_to_iso(self) -> None:
        self.assertEqual(self.df.iloc[-1]["date"], "2026-07-24")

    def test_code_column_filled(self) -> None:
        self.assertEqual(self.df.iloc[-1]["code"], "600519")


class TestKplFetcherRegistration(unittest.TestCase):
    """注册契约：默认关闭时不得实例化。"""

    def test_market_support_limited_to_cn(self) -> None:
        from data_provider.base import DataFetcherManager

        self.assertEqual(
            DataFetcherManager._DAILY_MARKET_FETCHER_SUPPORT.get("KplFetcher"), {"cn"}
        )


if __name__ == "__main__":
    unittest.main()
