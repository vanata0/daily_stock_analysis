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


class TestKplFetcherRealtimeQuote(unittest.TestCase):
    """实时行情映射 —— KPL 置于最高优先级，字段错了会直接污染全局。"""

    # 取自 /orderbook/600519 实测（2026-07-24）
    ORDERBOOK = {
        "code": "600519", "name": "贵州茅台", "trade_date": "20260724",
        "preclose": 1292.01, "open": 1305, "high": 1309.21, "low": 1286.2,
        "last": 1297.41, "change": 5.4, "change_pct": 0.42, "amplitude": 1.78,
        "turnover_ratio": 0.29, "vol_ratio": 0.52, "amount_wan": 35698,
        "turnover": 4622242878, "avg_px": 1294.785, "entrust_rate": -36.88,
        "up_limit": 1421.21, "down_limit": 1162.81,
        "pe": 14.89, "ttm_pe": 19.6, "jt_pe": 19.7, "pb": 6.87,
        "circ_mv": 1621868369953, "total_mv": 1621868369953,
    }

    def _quote(self, overrides=None, valid=True):
        payload = dict(self.ORDERBOOK)
        if overrides:
            payload.update(overrides)
        c = MagicMock()
        c.is_credential_valid.return_value = valid
        c.get.return_value = payload
        return KplFetcher(client=c).get_realtime_quote("600519")

    def test_core_price_fields_mapped(self) -> None:
        q = self._quote()
        self.assertEqual(q.price, 1297.41)
        self.assertEqual(q.change_pct, 0.42)
        self.assertEqual(q.change_amount, 5.4)
        self.assertEqual(q.open_price, 1305)
        self.assertEqual(q.high, 1309.21)
        self.assertEqual(q.low, 1286.2)
        self.assertEqual(q.pre_close, 1292.01)

    def test_source_and_market_tagged(self) -> None:
        from data_provider.realtime_types import RealtimeSource

        q = self._quote()
        self.assertEqual(q.source, RealtimeSource.KPL)
        self.assertEqual(q.market, "cn")
        self.assertEqual(q.name, "贵州茅台")

    def test_volume_comes_from_amount_wan_as_hands(self) -> None:
        """回归保护：orderbook 无 volume 字段。

        `amount_wan` 名字像「成交额(万元)」，实测却是「成交量(手)」——它与
        /kline/daily 的 volume 在 5 只标的上逐一相等，且
        turnover/(amount_wan*100) 都落在当日 low~high 内。若误当成交额使用，
        量纲会整体错乱。
        """
        q = self._quote()
        self.assertEqual(q.volume, 35698 * 100)

    def test_amount_uses_turnover_in_yuan(self) -> None:
        q = self._quote()
        self.assertEqual(q.amount, 4622242878)

    def test_average_price_within_daily_range(self) -> None:
        """量纲自洽硬判据：成交额/成交量必须落在当日价格区间内。"""
        q = self._quote()
        avg = q.amount / q.volume
        self.assertGreaterEqual(avg, q.low)
        self.assertLessEqual(avg, q.high)

    def test_valuation_fields_prefer_ttm_pe(self) -> None:
        """现有免费源常缺 PE/PB，这是 KPL 的主要增益，需优先取 TTM。"""
        q = self._quote()
        self.assertEqual(q.pe_ratio, 19.6)
        self.assertEqual(q.pb_ratio, 6.87)
        self.assertEqual(q.total_mv, 1621868369953)
        self.assertEqual(q.circ_mv, 1621868369953)

    def test_pe_falls_back_to_static_when_ttm_missing(self) -> None:
        q = self._quote({"ttm_pe": None})
        self.assertEqual(q.pe_ratio, 14.89)

    def test_quote_satisfies_manager_acceptance_checks(self) -> None:
        q = self._quote()
        self.assertTrue(q.has_basic_data())
        self.assertTrue(q.has_volume_data())

    def test_unavailable_source_returns_none(self) -> None:
        self.assertIsNone(self._quote(valid=False))

    def test_invalid_price_returns_none(self) -> None:
        """价格缺失/为零时返回 None，让上层继续降级而不是接受坏数据。"""
        for bad in (None, 0, "", "abc"):
            self.assertIsNone(self._quote({"last": bad}), f"应拒绝 last={bad!r}")

    def test_upstream_error_returns_none(self) -> None:
        c = MagicMock()
        c.is_credential_valid.return_value = True
        c.get.side_effect = KplRequestError("boom")
        self.assertIsNone(KplFetcher(client=c).get_realtime_quote("600519"))

    def test_non_a_share_code_returns_none(self) -> None:
        c = MagicMock()
        c.is_credential_valid.return_value = True
        c.get.return_value = self.ORDERBOOK
        self.assertIsNone(KplFetcher(client=c).get_realtime_quote("AAPL"))


class TestKplFetcherRegistration(unittest.TestCase):
    """注册契约：默认关闭时不得实例化。"""

    def test_market_support_limited_to_cn(self) -> None:
        from data_provider.base import DataFetcherManager

        self.assertEqual(
            DataFetcherManager._DAILY_MARKET_FETCHER_SUPPORT.get("KplFetcher"), {"cn"}
        )


if __name__ == "__main__":
    unittest.main()
