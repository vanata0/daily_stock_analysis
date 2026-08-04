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


class TestKplFetcherStockInfo(unittest.TestCase):
    """个股辅助信息：股票名与所属板块。"""

    def _fetcher(self, payload, valid=True):
        c = MagicMock()
        c.is_credential_valid.return_value = valid
        c.get.return_value = payload
        return KplFetcher(client=c)

    def test_stock_name_extracted(self) -> None:
        f = self._fetcher({"code": "600519", "name": "贵州茅台", "last": 1297.41})
        self.assertEqual(f.get_stock_name("600519"), "贵州茅台")

    def test_stock_name_none_when_unavailable(self) -> None:
        f = self._fetcher({"name": "贵州茅台"}, valid=False)
        self.assertIsNone(f.get_stock_name("600519"))

    def test_stock_name_none_on_blank(self) -> None:
        f = self._fetcher({"code": "600519", "name": "  "})
        self.assertIsNone(f.get_stock_name("600519"))

    def test_belong_board_deliberately_not_implemented(self) -> None:
        """个股所属板块有意不接 KPL —— 管理器按 `get_belong_board`（单数）做
        hasattr 探测，不定义即自动回落 EfinanceFetcher 的东财口径。

        上游 /plate-list/stock-sector-v2 能返回数据，但按**板块当日涨跌幅降序**
        排列：让「今天哪个板块涨得多」决定「这只股票属于什么」，与所属板块的
        语义无关；涨跌幅盘中实时变化会让顺序跟着变，而下游 notification 只取
        belong_boards[:5]，分析结果因此不可复现。实测 002364（电源设备公司）
        KPL 前 5 是「拼多多概念/杭州/电源/光储充一体化/通信」，东财是
        「电力设备/其他电源设备Ⅲ/其他电源设备Ⅱ/浙江板块/换电概念」——44 条里
        只有「电气设备」沾行业分类，接入反而是降级。

        定义这个方法会立刻把东财顶掉，所以用测试把「不实现」钉住。
        """
        self.assertFalse(
            hasattr(KplFetcher, "get_belong_board"),
            "定义 get_belong_board 会让 KPL 顶掉东财的行业口径，见本用例说明",
        )


class TestKplFetcherResearchReports(unittest.TestCase):
    """研报 —— 接入 research_report_fetcher 的降级链首位。"""

    # 取自 /research/research-field-list/600519 实测
    PAYLOAD = {"items": [
        {"id": "446328", "timestamp": "1784736000", "title": "需求根基稳固，市场化定价持续兑现",
         "broker": "中邮证券", "rating": "买入"},
        {"id": "446109", "timestamp": "1784476800", "title": "飞天茅台年内二次提价",
         "broker": "群益证券", "rating": "中性"},
    ]}

    def _reports(self, payload=None, valid=True, max_count=10):
        c = MagicMock()
        c.is_credential_valid.return_value = valid
        c.get.return_value = self.PAYLOAD if payload is None else payload
        return KplFetcher(client=c).get_research_reports("600519", max_count=max_count)

    def test_uses_field_list_not_field_excel(self) -> None:
        """field-excel 只有评级分布和 3 条无标题明细，填不满研报条目契约。"""
        c = MagicMock()
        c.is_credential_valid.return_value = True
        c.get.return_value = self.PAYLOAD
        KplFetcher(client=c).get_research_reports("600519")
        self.assertIn("research-field-list", c.get.call_args[0][0])

    def test_contract_keys_match_tushare_shape(self) -> None:
        """降级链会无差别消费各源结果，键必须与 Tushare 实现一致。"""
        r = self._reports()[0]
        self.assertEqual(
            set(r.keys()),
            {"title", "date", "broker", "rating", "abstract", "analyst", "eps", "classify"},
        )

    def test_fields_mapped(self) -> None:
        r = self._reports()[0]
        self.assertEqual(r["title"], "需求根基稳固，市场化定价持续兑现")
        self.assertEqual(r["broker"], "中邮证券")
        self.assertEqual(r["rating"], "买入")
        self.assertEqual(len(r["date"]), 10)

    def test_ratings_are_chinese_like_tushare(self) -> None:
        """下游按 rating 文本统计 rating_summary，口径必须与既有源一致。"""
        self.assertEqual([r["rating"] for r in self._reports()], ["买入", "中性"])

    def test_duplicate_date_title_deduped(self) -> None:
        payload = {"items": [self.PAYLOAD["items"][0], dict(self.PAYLOAD["items"][0])]}
        self.assertEqual(len(self._reports(payload)), 1)

    def test_blank_titles_skipped(self) -> None:
        self.assertEqual(self._reports({"items": [{"title": "  ", "broker": "x"}]}), [])

    def test_respects_max_count(self) -> None:
        self.assertEqual(len(self._reports(max_count=1)), 1)

    def test_fail_open_returns_empty_list(self) -> None:
        """研报是增强信息，失败必须返回 [] 而不是抛异常打断基本面链路。"""
        self.assertEqual(self._reports(valid=False), [])
        c = MagicMock()
        c.is_credential_valid.return_value = True
        c.get.side_effect = KplRequestError("boom")
        self.assertEqual(KplFetcher(client=c).get_research_reports("600519"), [])


class TestKplFetcherSectorRankings(unittest.TestCase):
    """行业板块排行 —— 上游按板块代码排序，必须自行按涨跌幅重排。"""

    # 刻意让代码序与涨跌幅序不一致，才能验证重排真的发生了
    ROWS = {"items": [
        {"sector_code": "881001", "sector_name": "芯片",   "change_pct": -1.906},
        {"sector_code": "881250", "sector_name": "并购重组", "change_pct": -2.257},
        {"sector_code": "881900", "sector_name": "中船系",  "change_pct": 0.925},
        {"sector_code": "881800", "sector_name": "铀矿",    "change_pct": -4.739},
        {"sector_code": "881700", "sector_name": "工业气体", "change_pct": 0.193},
    ]}

    def _fetcher(self, payload=None, valid=True):
        c = MagicMock()
        c.is_credential_valid.return_value = valid
        c.get.return_value = payload if payload is not None else self.ROWS
        return KplFetcher(client=c)

    def test_uses_weight_performance_realtime_endpoint(self) -> None:
        """必须打 weight-performance-realtime。

        /plate-list/pc-plate-ranking 是行业+概念+地域混杂的「板块」大杂烩
        （实测出现过「杭州」这类地域标签），用来标「行业板块」会掺入非行业
        标签；weight-performance-realtime 是固定 30 个申万二级行业的干净列表。
        """
        f = self._fetcher()
        f.get_sector_rankings(2)
        called = f._client.get.call_args[0][0]
        self.assertIn("weight-performance-realtime", called)

    def test_top_sorted_desc_by_change_pct(self) -> None:
        top, _ = self._fetcher().get_sector_rankings(3)
        self.assertEqual([r["name"] for r in top], ["中船系", "工业气体", "芯片"])

    def test_bottom_sorted_asc_by_change_pct(self) -> None:
        """领跌应从最差开始，方便下游直接展示。"""
        _, bottom = self._fetcher().get_sector_rankings(3)
        self.assertEqual([r["name"] for r in bottom], ["铀矿", "并购重组", "芯片"])

    def test_contract_keys_only(self) -> None:
        top, bottom = self._fetcher().get_sector_rankings(2)
        for row in top + bottom:
            self.assertEqual(set(row.keys()), {"name", "change_pct"})

    def test_rows_missing_change_pct_dropped(self) -> None:
        payload = {"items": [
            {"sector_name": "有效", "change_pct": 1.0},
            {"sector_name": "无涨跌幅"},
            {"sector_name": "", "change_pct": 2.0},
        ]}
        top, _ = self._fetcher(payload).get_sector_rankings(5)
        self.assertEqual([r["name"] for r in top], ["有效"])

    def test_none_when_unavailable_or_empty(self) -> None:
        self.assertIsNone(self._fetcher(valid=False).get_sector_rankings(3))
        self.assertIsNone(self._fetcher({"items": []}).get_sector_rankings(3))


class TestKplFetcherConceptRankings(unittest.TestCase):
    """概念/题材排行 —— theme-list 是独立编号体系，语义与板块排行不同。"""

    ROWS = {"configured": True, "items": [
        {"id": "25", "name": "AI硬件", "ratio": 5.6915},
        {"id": "164", "name": "核电", "ratio": 1.8424},
        {"id": "84", "name": "人形机器人", "ratio": -2.4469},
        {"id": "213", "name": "预制菜", "ratio": -4.739},
        {"id": "189", "name": "商业航天", "ratio": 0.193},
    ]}

    def _fetcher(self, payload=None, valid=True):
        c = MagicMock()
        c.is_credential_valid.return_value = valid
        c.get.return_value = payload if payload is not None else self.ROWS
        return KplFetcher(client=c)

    def test_uses_theme_list_endpoint(self) -> None:
        f = self._fetcher()
        f.get_concept_rankings(2)
        called = f._client.get.call_args[0][0]
        self.assertIn("theme-list", called)

    def test_top_sorted_desc_by_change_pct(self) -> None:
        top, _ = self._fetcher().get_concept_rankings(3)
        self.assertEqual([r["name"] for r in top], ["AI硬件", "核电", "商业航天"])

    def test_bottom_sorted_asc_by_change_pct(self) -> None:
        _, bottom = self._fetcher().get_concept_rankings(3)
        self.assertEqual([r["name"] for r in bottom], ["预制菜", "人形机器人", "商业航天"])

    def test_contract_keys_only(self) -> None:
        top, bottom = self._fetcher().get_concept_rankings(2)
        for row in top + bottom:
            self.assertEqual(set(row.keys()), {"name", "change_pct"})

    def test_placeholder_rows_with_null_fields_dropped(self) -> None:
        """真实接口首条常是全字段为 null 的占位行，必须被过滤掉。"""
        payload = {"configured": True, "items": [
            {"id": None, "name": None, "ratio": None},
            {"id": "1", "name": "有效题材", "ratio": 1.0},
        ]}
        top, _ = self._fetcher(payload).get_concept_rankings(5)
        self.assertEqual([r["name"] for r in top], ["有效题材"])

    def test_none_when_not_configured(self) -> None:
        """Socket 签名未配置时接口返回 configured=false，判为不可用。"""
        payload = {"configured": False, "items": []}
        self.assertIsNone(self._fetcher(payload).get_concept_rankings(3))

    def test_none_when_unavailable_or_empty(self) -> None:
        self.assertIsNone(self._fetcher(valid=False).get_concept_rankings(3))
        self.assertIsNone(self._fetcher({"configured": True, "items": []}).get_concept_rankings(3))

class TestKplFetcherMarketStats(unittest.TestCase):
    """大盘统计 —— 成交额单位换算错了会让下游读数差 10000 倍。"""

    # 取自 /mood/market-daban-snapshot 实测（2026-07-24）
    SNAPSHOT = {
        "rising_count": 555, "falling_count": 4939, "flat_count": 31,
        "limit_up_count": 40, "limit_down_count": 24,
        "market_turnover_wan": 193114002,
    }

    def _stats(self, payload=None, valid=True):
        c = MagicMock()
        c.is_credential_valid.return_value = valid
        c.get.return_value = self.SNAPSHOT if payload is None else payload
        return KplFetcher(client=c).get_market_stats()

    def test_contract_keys_present(self) -> None:
        s = self._stats()
        self.assertEqual(
            set(s.keys()),
            {"up_count", "down_count", "flat_count",
             "limit_up_count", "limit_down_count", "total_amount"},
        )

    def test_counts_mapped(self) -> None:
        s = self._stats()
        self.assertEqual(s["up_count"], 555)
        self.assertEqual(s["down_count"], 4939)
        self.assertEqual(s["flat_count"], 31)
        self.assertEqual(s["limit_up_count"], 40)
        self.assertEqual(s["limit_down_count"], 24)

    def test_turnover_converted_wan_to_yi(self) -> None:
        """上游为万元，DSA 与 TickFlow 一致按亿元返回。"""
        self.assertEqual(self._stats()["total_amount"], 19311.4)

    def test_turnover_magnitude_is_plausible(self) -> None:
        """A 股单日成交额落在 5000~30000 亿区间，可挡住量级错误。"""
        self.assertTrue(5000 <= self._stats()["total_amount"] <= 30000)

    def test_missing_turnover_defaults_to_zero(self) -> None:
        payload = dict(self.SNAPSHOT)
        payload.pop("market_turnover_wan")
        self.assertEqual(self._stats(payload)["total_amount"], 0.0)

    def test_none_when_no_breadth(self) -> None:
        """连涨跌家数都没有说明数据无效，返回 None 触发降级。"""
        self.assertIsNone(self._stats({"market_turnover_wan": 1}))

    def test_none_when_unavailable(self) -> None:
        self.assertIsNone(self._stats(valid=False))

    def test_none_on_upstream_error(self) -> None:
        c = MagicMock()
        c.is_credential_valid.return_value = True
        c.get.side_effect = KplRequestError("boom")
        self.assertIsNone(KplFetcher(client=c).get_market_stats())

    def test_main_indices_intentionally_not_implemented(self) -> None:
        """上游 global-index 只有海外指数/期货/商品/汇率，无 A 股指数。"""
        c = MagicMock()
        c.is_credential_valid.return_value = True
        self.assertIsNone(KplFetcher(client=c).get_main_indices("cn"))


class TestKplFetcherLimitUpPool(unittest.TestCase):
    """涨停池 —— 上游按连板数分档，需逐档聚合。"""

    BOARDS = {
        4: [{"code": "002879", "name": "长缆科技", "consecutive_days": 4, "reason": "智能电网"}],
        3: [{"code": "000595", "name": "新能股份", "consecutive_days": 3, "reason": "绿色电力"}],
        2: [{"code": "000533", "name": "顺钠股份", "consecutive_days": 2, "reason": "智能电网"}],
        1: [{"code": "002374", "name": "中锐股份", "consecutive_days": 1, "reason": "并购重组"}],
    }

    def _client(self, boards=None, valid=True, fail_boards=()):
        boards = self.BOARDS if boards is None else boards
        c = MagicMock()
        c.is_credential_valid.return_value = valid

        def fake_get(path, params=None):
            pid = (params or {}).get("pid_type")
            if pid in fail_boards:
                raise KplRequestError(f"board {pid} down")
            return {"pid_type": pid, "items": boards.get(pid, [])}

        c.get.side_effect = fake_get
        return c

    def test_boards_aggregated_high_to_low(self) -> None:
        pool = KplFetcher(client=self._client()).get_limit_up_pool(n=10)
        self.assertEqual([p["consecutive_days"] for p in pool], [4, 3, 2, 1])

    def test_respects_result_limit(self) -> None:
        pool = KplFetcher(client=self._client()).get_limit_up_pool(n=2)
        self.assertEqual(len(pool), 2)
        self.assertEqual(pool[0]["consecutive_days"], 4)

    def test_single_board_failure_does_not_abort(self) -> None:
        """某一档位失败不应丢掉其它档位已取到的数据。"""
        pool = KplFetcher(client=self._client(fail_boards={3})).get_limit_up_pool(n=10)
        days = [p["consecutive_days"] for p in pool]
        self.assertIn(4, days)
        self.assertIn(2, days)
        self.assertNotIn(3, days)

    def test_date_switches_to_history_endpoint(self) -> None:
        c = self._client()
        KplFetcher(client=c).get_limit_up_pool(date="20260724", n=5)
        paths = [call.args[0] for call in c.get.call_args_list]
        self.assertTrue(all("daily-limit-perf-history" in p for p in paths))

    def test_realtime_endpoint_without_date(self) -> None:
        c = self._client()
        KplFetcher(client=c).get_limit_up_pool(n=5)
        paths = [call.args[0] for call in c.get.call_args_list]
        self.assertTrue(all("limit-up/realtime" in p for p in paths))

    def test_none_when_unavailable_or_empty(self) -> None:
        self.assertIsNone(KplFetcher(client=self._client(valid=False)).get_limit_up_pool())
        self.assertIsNone(KplFetcher(client=self._client(boards={})).get_limit_up_pool())


class TestKplFetcherRegistration(unittest.TestCase):
    """注册契约：默认关闭时不得实例化。"""

    def test_market_support_limited_to_cn(self) -> None:
        from data_provider.base import DataFetcherManager

        self.assertEqual(
            DataFetcherManager._DAILY_MARKET_FETCHER_SUPPORT.get("KplFetcher"), {"cn"}
        )


if __name__ == "__main__":
    unittest.main()
