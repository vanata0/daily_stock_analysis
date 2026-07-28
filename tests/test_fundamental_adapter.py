# -*- coding: utf-8 -*-
"""
Tests for fundamental adapter helpers.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.fundamental_adapter import (
    AkshareFundamentalAdapter,
    _build_dividend_payload,
    _extract_latest_row,
    _parse_dividend_plan_to_per_share,
    _prefixed_symbol,
    _recent_report_periods,
)


class TestFundamentalAdapter(unittest.TestCase):
    def test_parse_dividend_plan_to_per_share_supports_cn_patterns(self) -> None:
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("10派3元(含税)"), 0.3, places=6)
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("每10股派发2.5元"), 0.25, places=6)
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("每股派0.8元"), 0.8, places=6)
        self.assertIsNone(_parse_dividend_plan_to_per_share("仅送股，不现金分红"))

    def test_extract_latest_row_returns_none_when_code_mismatch(self) -> None:
        df = pd.DataFrame(
            {
                "股票代码": ["600000", "000001"],
                "值": [1, 2],
            }
        )
        row = _extract_latest_row(df, "600519")
        self.assertIsNone(row)

    def test_extract_latest_row_fallback_when_no_code_column(self) -> None:
        df = pd.DataFrame({"值": [1, 2]})
        row = _extract_latest_row(df, "600519")
        self.assertIsNotNone(row)
        self.assertEqual(row["值"], 1)

    def test_dragon_tiger_no_match_with_code_column_is_ok(self) -> None:
        adapter = AkshareFundamentalAdapter()
        df = pd.DataFrame(
            {
                "股票代码": ["600000"],
                "日期": ["2026-01-01"],
            }
        )
        with patch.object(adapter, "_call_df_candidates", return_value=(df, "stock_lhb_stock_statistic_em", [])):
            result = adapter.get_dragon_tiger_flag("600519")
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["is_on_list"])
        self.assertEqual(result["recent_count"], 0)

    def test_dragon_tiger_match_is_ok(self) -> None:
        adapter = AkshareFundamentalAdapter()
        today = pd.Timestamp.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "日期": [today],
            }
        )
        with patch.object(adapter, "_call_df_candidates", return_value=(df, "stock_lhb_stock_statistic_em", [])):
            result = adapter.get_dragon_tiger_flag("600519")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["is_on_list"])
        self.assertGreaterEqual(result["recent_count"], 1)

    def test_fundamental_bundle_includes_financial_report_and_dividend_payload(self) -> None:
        adapter = AkshareFundamentalAdapter()
        now = datetime.now()
        within_ttm = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        future_day = (now + timedelta(days=10)).strftime("%Y-%m-%d")
        old_day = (now - timedelta(days=500)).strftime("%Y-%m-%d")
        fin_df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "报告期": [within_ttm],
                "营业总收入": [1000.0],
                "归母净利润": [300.0],
                "经营活动产生的现金流量净额": [500.0],
                "净资产收益率": [18.2],
                "营业收入同比": [12.0],
                "净利润同比": [9.5],
            }
        )
        dividend_df = pd.DataFrame(
            {
                "股票代码": ["600519", "600519", "600519", "600519"],
                "除息日": [within_ttm, within_ttm, future_day, old_day],
                "分配方案": ["10派3元(含税)", "10派3元(含税)", "10派5元", "10派1元"],
            }
        )

        # 按接口名分派而不是按调用次序：业绩报表 / 十大股东那几路是按报告期逐个回退的，
        # 用 side_effect 序列会让「回退几期」这种实现细节把测试钉死。
        responses = {
            "stock_financial_abstract": (fin_df, "stock_financial_abstract", []),
            "stock_fhps_detail_em": (dividend_df, "stock_fhps_detail_em", []),
        }

        def fake_call_df_candidates(candidates):
            return responses.get(candidates[0][0], (None, None, []))

        with patch.object(
            adapter,
            "_call_df_candidates",
            side_effect=fake_call_df_candidates,
        ):
            result = adapter.get_fundamental_bundle("600519")

        financial_report = result["earnings"].get("financial_report", {})
        self.assertEqual(financial_report.get("report_date"), within_ttm)
        self.assertEqual(financial_report.get("revenue"), 1000.0)
        self.assertEqual(financial_report.get("net_profit_parent"), 300.0)
        self.assertEqual(financial_report.get("operating_cash_flow"), 500.0)
        self.assertEqual(financial_report.get("roe"), 18.2)

        dividend_payload = result["earnings"].get("dividend", {})
        events = dividend_payload.get("events", [])
        self.assertEqual(len(events), 2)  # duplicate + future day filtered
        self.assertEqual(dividend_payload.get("ttm_event_count"), 1)
        self.assertAlmostEqual(dividend_payload.get("ttm_cash_dividend_per_share"), 0.3, places=6)

    def test_build_dividend_payload_returns_empty_when_code_not_matched(self) -> None:
        now = datetime.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["000001"],
                "除息日": [now],
                "分配方案": ["10派3元(含税)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload, {})

    def test_build_dividend_payload_skips_after_tax_plan(self) -> None:
        now = datetime.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "除息日": [now],
                "分配方案": ["10派3元(税后)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload, {})

    def test_build_dividend_payload_ttm_window_boundary(self) -> None:
        now = datetime.now()
        day_365 = (now - timedelta(days=365)).strftime("%Y-%m-%d")
        day_366 = (now - timedelta(days=366)).strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519", "600519"],
                "除息日": [day_365, day_366],
                "分配方案": ["10派3元(含税)", "10派5元(含税)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload.get("ttm_event_count"), 1)
        self.assertAlmostEqual(payload.get("ttm_cash_dividend_per_share"), 0.3, places=6)


class TestReportPeriodHelpers(unittest.TestCase):
    def test_recent_report_periods_returns_reached_quarter_ends_newest_first(self) -> None:
        self.assertEqual(
            _recent_report_periods(count=4, now=datetime(2026, 7, 28)),
            ["20260630", "20260331", "20251231", "20250930"],
        )

    def test_recent_report_periods_excludes_future_quarter_ends(self) -> None:
        # 3 月 1 日时当年 Q1 尚未结束，最近的已到达报告期是上一年年报。
        periods = _recent_report_periods(count=2, now=datetime(2026, 3, 1))
        self.assertEqual(periods, ["20251231", "20250930"])

    def test_prefixed_symbol_covers_exchanges(self) -> None:
        self.assertEqual(_prefixed_symbol("600519"), "sh600519")
        self.assertEqual(_prefixed_symbol("000001"), "sz000001")
        self.assertEqual(_prefixed_symbol("830799"), "bj830799")
        self.assertEqual(_prefixed_symbol("sh600519"), "sh600519")


class TestFundamentalBundleEarningsFallback(unittest.TestCase):
    """业绩预告/快报是自愿披露，缺失时 earnings 必须由业绩报表兜底。

    回归的是这条链路：earnings 为空 -> 基本面块 partial -> 运行诊断报「部分降级」。
    """

    @staticmethod
    def _yjbb_row() -> pd.DataFrame:
        return pd.DataFrame(
            {
                "股票代码": ["600519"],
                "每股收益": [21.76],
                "营业总收入-营业总收入": [54702912385.23],
                "营业总收入-同比增长": [6.34],
                "净利润-净利润": [27242512886.45],
                "净利润-同比增长": [1.47],
                "净资产收益率": [10.57],
                "销售毛利率": [89.76],
                "最新公告日期": ["2026-04-25"],
            }
        )

    def _run_bundle(self, responses):
        adapter = AkshareFundamentalAdapter()

        def fake_call_df_candidates(candidates):
            return responses.get(candidates[0][0], (None, None, []))

        with patch.object(
            adapter,
            "_call_df_candidates",
            side_effect=fake_call_df_candidates,
        ), patch.object(adapter, "_merge_bundle_kpl", return_value=None):
            return adapter.get_fundamental_bundle("600519")

    def test_earnings_falls_back_to_yjbb_when_forecast_and_quick_are_absent(self) -> None:
        result = self._run_bundle({"stock_yjbb_em": (self._yjbb_row(), "stock_yjbb_em", [])})

        financial_report = result["earnings"].get("financial_report", {})
        self.assertEqual(financial_report.get("eps"), 21.76)
        self.assertEqual(financial_report.get("net_profit_parent"), 27242512886.45)
        self.assertEqual(financial_report.get("net_profit_yoy"), 1.47)
        self.assertEqual(financial_report.get("revenue"), 54702912385.23)
        self.assertEqual(financial_report.get("gross_margin"), 89.76)
        self.assertIn("earnings_report:stock_yjbb_em", result["source_chain"])

    def test_yjbb_does_not_overwrite_fields_from_financial_abstract(self) -> None:
        fin_df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "报告期": ["2026-03-31"],
                "营业总收入": [1000.0],
                "归母净利润": [300.0],
                "经营活动产生的现金流量净额": [500.0],
                "净资产收益率": [18.2],
            }
        )
        result = self._run_bundle({
            "stock_financial_abstract": (fin_df, "stock_financial_abstract", []),
            "stock_yjbb_em": (self._yjbb_row(), "stock_yjbb_em", []),
        })

        financial_report = result["earnings"].get("financial_report", {})
        # 财务指标那一路先写入的字段保留
        self.assertEqual(financial_report.get("revenue"), 1000.0)
        self.assertEqual(financial_report.get("net_profit_parent"), 300.0)
        self.assertEqual(financial_report.get("operating_cash_flow"), 500.0)
        self.assertEqual(financial_report.get("roe"), 18.2)
        # 业绩报表只补它没有的字段
        self.assertEqual(financial_report.get("eps"), 21.76)
        self.assertEqual(financial_report.get("gross_margin"), 89.76)

    def test_forecast_is_used_only_when_earnings_still_empty(self) -> None:
        forecast_df = pd.DataFrame({"股票代码": ["600519"], "预告": ["预增"]})
        result = self._run_bundle({"stock_yjyg_em": (forecast_df, "stock_yjyg_em", [])})

        self.assertEqual(result["earnings"].get("forecast_summary"), "预增")
        self.assertIn("earnings_forecast:stock_yjyg_em", result["source_chain"])


class TestCallDfCandidatesForStock(unittest.TestCase):
    def test_keeps_trying_until_a_candidate_contains_the_stock(self) -> None:
        """全市场表命中不等于这只票命中——不能在第一个非空表处就停下。"""
        adapter = AkshareFundamentalAdapter()
        other_stock_df = pd.DataFrame({"股票代码": ["000001"], "每股收益": [1.0]})
        target_df = pd.DataFrame({"股票代码": ["600519"], "每股收益": [21.76]})

        with patch.object(
            adapter,
            "_call_df_candidates",
            side_effect=[
                (other_stock_df, "stock_yjbb_em", []),
                (target_df, "stock_yjbb_em", []),
            ],
        ):
            row, source, errors = adapter._call_df_candidates_for_stock(
                [("stock_yjbb_em", {"date": "20260630"}), ("stock_yjbb_em", {"date": "20260331"})],
                "600519",
            )

        self.assertIsNotNone(row)
        self.assertEqual(row["每股收益"], 21.76)
        self.assertEqual(source, "stock_yjbb_em")
        self.assertEqual(errors, [])

    def test_returns_none_when_no_candidate_matches(self) -> None:
        adapter = AkshareFundamentalAdapter()
        other_stock_df = pd.DataFrame({"股票代码": ["000001"], "每股收益": [1.0]})

        with patch.object(
            adapter,
            "_call_df_candidates",
            side_effect=[(other_stock_df, "stock_yjbb_em", ["stock_yjbb_em:TypeError"])],
        ):
            row, source, errors = adapter._call_df_candidates_for_stock(
                [("stock_yjbb_em", {"date": "20260331"})],
                "600519",
            )

        self.assertIsNone(row)
        self.assertIsNone(source)
        self.assertEqual(errors, ["stock_yjbb_em:TypeError"])


class TestDfCache(unittest.TestCase):
    def setUp(self) -> None:
        AkshareFundamentalAdapter._df_cache.clear()

    def tearDown(self) -> None:
        AkshareFundamentalAdapter._df_cache.clear()

    def test_market_wide_table_is_fetched_once_and_reused(self) -> None:
        adapter = AkshareFundamentalAdapter()
        df = pd.DataFrame({"股票代码": ["600519"], "每股收益": [21.76]})
        fake_ak = type("FakeAk", (), {})()
        calls = []

        def fake_endpoint(**kwargs):
            calls.append(kwargs)
            return df

        fake_ak.stock_yjbb_em = fake_endpoint

        with patch.dict("sys.modules", {"akshare": fake_ak}):
            first, _, _ = adapter._call_df_candidates([("stock_yjbb_em", {"date": "20260331"})])
            second, _, _ = AkshareFundamentalAdapter()._call_df_candidates(
                [("stock_yjbb_em", {"date": "20260331"})]
            )

        self.assertEqual(len(calls), 1, "同一报告期的全市场表应只拉取一次")
        self.assertIs(first, second)

    def test_different_periods_are_cached_separately(self) -> None:
        adapter = AkshareFundamentalAdapter()
        fake_ak = type("FakeAk", (), {})()
        calls = []

        def fake_endpoint(**kwargs):
            calls.append(kwargs)
            return pd.DataFrame({"股票代码": ["600519"], "date": [kwargs.get("date")]})

        fake_ak.stock_yjbb_em = fake_endpoint

        with patch.dict("sys.modules", {"akshare": fake_ak}):
            adapter._call_df_candidates([("stock_yjbb_em", {"date": "20260331"})])
            adapter._call_df_candidates([("stock_yjbb_em", {"date": "20251231"})])

        self.assertEqual(len(calls), 2)

    def test_expired_entry_is_refetched(self) -> None:
        adapter = AkshareFundamentalAdapter()
        fake_ak = type("FakeAk", (), {})()
        calls = []

        def fake_endpoint(**kwargs):
            calls.append(kwargs)
            return pd.DataFrame({"股票代码": ["600519"]})

        fake_ak.stock_yjbb_em = fake_endpoint

        with patch.dict("sys.modules", {"akshare": fake_ak}):
            adapter._call_df_candidates([("stock_yjbb_em", {"date": "20260331"})])
            with patch.object(AkshareFundamentalAdapter, "_DF_CACHE_TTL_SECONDS", -1):
                adapter._call_df_candidates([("stock_yjbb_em", {"date": "20260331"})])

        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
