# -*- coding: utf-8 -*-
"""KPL 基本面取数：资金流、成长/业绩/机构三块，及其降级链。

接入动机是**可持续性**：Tushare 代理站下线后 Mairuiapi 是唯一的个股资金流
来源，KPL 提供一条不依赖将失效凭证的通路。

⚠️ 不要把这次接入当成「KPL 口径比 Mairui 准」。曾用 Tushare ``moneyflow``
的特大单+大单之和做仲裁基准，得出 KPL 88.5% / Mairui 52.1% 的方向一致率；
但改用 Tushare 自己的 ``net_mf_amount`` 字段，结论完全反转
（KPL 61.5% / Mairui 77.1%）。同一个"基准"给出两个相反答案，说明基准的
主力口径定义本身未经校准，两组数字都不能作为准确性证据。扩样到 96 个样本
只是放大了同一个有偏方法。对拍脚本 scripts/check_capital_flow_parity.py
保留下来是为了在 Tushare 下线前留存基线，不是为了论证优劣。

下面的用例只锁定**可独立验证的事实**：端点语义、聚合算术、降级行为。
"""

import importlib.util
import sys
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

try:
    json_repair_available = importlib.util.find_spec("json_repair") is not None
except ValueError:
    json_repair_available = "json_repair" in sys.modules

if not json_repair_available and "json_repair" not in sys.modules:
    sys.modules["json_repair"] = MagicMock()

from data_provider.kpl_fetcher import KplFetcher
from data_provider.kpl_http import KplRequestError

# 取自 000977 实测（2026-07-20~24）：main_sell 上游已是负值。
# 日期必须相对今天生成：get_capital_flow() 从 date.today() 往前只回溯
# wanted*2 + _FLOW_LOOKBACK_SLACK 天，写死绝对日期的话 fixture 会随时间漂出
# 窗口，某天开始整组用例突然拿到 None（2026-09-01 实际发生过）。
_DAILY_VALUES = [
    (3_070_297_443, -3_205_910_638),
    (5_949_966_930, -6_136_657_829),
    (7_900_525_462, -7_947_982_527),
    (6_933_213_185, -6_544_114_644),
    (6_583_016_492, -6_079_754_787),
]
DAILY = {
    (date.today() - timedelta(days=i)).strftime("%Y%m%d"): pair
    for i, pair in enumerate(_DAILY_VALUES)
}
_THIRD_DAY = list(DAILY)[2]


def _fetcher(daily=None, valid=True, fail_days=()):
    daily = DAILY if daily is None else daily
    client = MagicMock()
    client.is_credential_valid.return_value = valid

    def fake_get(path, params=None):
        day = (params or {}).get("day")
        if day in fail_days:
            raise KplRequestError(f"{day} down")
        pair = daily.get(day)
        if not pair:
            return {"items": []}
        return {"items": [{"main_buy": pair[0], "main_sell": pair[1]}]}

    client.get.side_effect = fake_get
    return KplFetcher(client=client)


class TestCapitalFlowEndpointChoice(unittest.TestCase):
    def test_uses_chouma_history_not_main_monitor_trend(self) -> None:
        """回归：main-monitor-trend 声明了 date 但传任何值都返回同一份当日数据。

        实测 4 种日期格式 zjb 完全相同，用它做多日累计会把同一天叠加 N 遍。
        chouma-history 的 day 才真实生效。
        """
        f = _fetcher()
        f.get_capital_flow("000977", days=3)
        paths = [c.args[0] for c in f._client.get.call_args_list]
        self.assertTrue(any("chouma-history" in p for p in paths))
        self.assertTrue(any("stock-dp-realdata" in p for p in paths))
        self.assertFalse(any("main-monitor-trend" in p for p in paths))

    def test_queries_one_day_at_a_time(self) -> None:
        f = _fetcher()
        f.get_capital_flow("000977", days=5)
        days = [
            c.kwargs.get("params", {}).get("day")
            for c in f._client.get.call_args_list
            if "chouma-history" in c.args[0]
        ]
        self.assertEqual(len(days), len(set(days)), "同一天不应重复查询")


class TestCapitalFlowAggregation(unittest.TestCase):
    def test_contract_keys(self) -> None:
        r = _fetcher().get_capital_flow("000977", days=5)
        self.assertEqual(
            set(r.keys()),
            {
                "main_buy",
                "main_sell",
                "main_net_inflow",
                "inflow_5d",
                "inflow_10d",
                "trade_date",
            },
        )

    def test_latest_day_net(self) -> None:
        """main_sell 上游已为负值，净额是直接相加而非相减。"""
        r = _fetcher().get_capital_flow("000977", days=5)
        self.assertEqual(r["main_net_inflow"], 3_070_297_443 - 3_205_910_638)
        self.assertEqual(r["main_buy"], 3_070_297_443)
        self.assertEqual(r["main_sell"], -3_205_910_638)

    def test_five_day_sum(self) -> None:
        r = _fetcher().get_capital_flow("000977", days=5)
        expected = sum(b + s for b, s in DAILY.values())
        self.assertAlmostEqual(r["inflow_5d"], expected, places=2)

    def test_inflow_10d_none_when_insufficient_history(self) -> None:
        """样本不足 10 天时必须给 None，不能用 5 天的和冒充 10 天。"""
        self.assertIsNone(_fetcher().get_capital_flow("000977", days=5)["inflow_10d"])

    def test_inflow_5d_none_when_fewer_than_five_days(self) -> None:
        two = dict(list(DAILY.items())[:2])
        r = _fetcher(two).get_capital_flow("000977", days=5)
        self.assertIsNone(r["inflow_5d"])
        self.assertIsNotNone(r["main_net_inflow"])

    def test_missing_days_skipped_without_aborting(self) -> None:
        """非交易日返回空 items，应跳过继续回溯而不是中断。"""
        r = _fetcher().get_capital_flow("000977", days=3)
        self.assertIsNotNone(r["main_net_inflow"])

    def test_single_day_failure_does_not_abort(self) -> None:
        r = _fetcher(fail_days={_THIRD_DAY}).get_capital_flow("000977", days=3)
        self.assertIsNotNone(r)

    def test_none_when_unavailable_or_empty(self) -> None:
        self.assertIsNone(_fetcher(valid=False).get_capital_flow("000977"))
        self.assertIsNone(_fetcher({}).get_capital_flow("000977"))

    def test_non_a_share_rejected(self) -> None:
        for bad in ("AAPL", "00700", ""):
            self.assertIsNone(_fetcher().get_capital_flow(bad))

    def test_realtime_snapshot_feeds_latest_main_fields(self) -> None:
        """当日主力买/卖/净应优先取 stock-dp-realdata 的实时快照。

        603993 实测：stock-dp-realdata 为 963386536 / -1026058993 /
        -62672457，而 chouma-history 的 20260813 收盘是另一组数值。
        """
        client = MagicMock()
        client.is_credential_valid.return_value = True

        def fake_get(path, params=None):
            if path.startswith("/big-money/stock-dp-realdata/"):
                return {
                    "main_buy": 963_386_536.0,
                    "main_sell": -1_026_058_993.0,
                    "main_net": -62_672_457.0,
                    "turnover": 2_924_987_147.0,
                }
            if path.startswith("/orderbook/"):
                return {"trade_date": "20260814"}
            day = (params or {}).get("day")
            pair = DAILY.get(day)
            if not pair:
                return {"items": []}
            return {"items": [{"main_buy": pair[0], "main_sell": pair[1]}]}

        client.get.side_effect = fake_get
        r = KplFetcher(client=client).get_capital_flow("603993", days=5)

        self.assertEqual(r["main_buy"], 963_386_536.0)
        self.assertEqual(r["main_sell"], -1_026_058_993.0)
        self.assertEqual(r["main_net_inflow"], -62_672_457.0)
        self.assertEqual(r["trade_date"], "20260814")


class TestCapitalFlowSemantics(unittest.TestCase):
    """锁定 2026-07-26 实测确认的上游语义。"""

    def test_takes_last_item_as_daily_close_total(self) -> None:
        """items 是当日 09:30~15:00 每 5 分钟的**累计值**，不是增量。

        实测 000977 单日 49 条、main_buy 单调递增，且末条与
        /big-money/stock-dp-realdata 的当日值逐位一致，故取 items[-1] 而非求和。
        """
        client = MagicMock()
        client.is_credential_valid.return_value = True
        cumulative = [
            {"main_buy": 1_000, "main_sell": -400, "turnover": 5_000},
            {"main_buy": 2_500, "main_sell": -900, "turnover": 9_000},
            {"main_buy": 3_000, "main_sell": -1_200, "turnover": 12_000},
        ]
        client.get.return_value = {"items": cumulative}
        r = KplFetcher(client=client).get_capital_flow("000977", days=1)
        self.assertEqual(r["main_net_inflow"], 3_000 - 1_200)
        self.assertEqual(r["main_buy"], 3_000)
        self.assertEqual(r["main_sell"], -1_200)

    def test_net_all_field_is_ignored(self) -> None:
        """上游 net_all 与 main_buy+main_sell 是两个不同口径，符号常相反。

        实测 000977 15:00: main_buy+main_sell = -135,613,195 而
        net_all = +335,735,894。误用 net_all 会让资金方向整体翻转。
        """
        client = MagicMock()
        client.is_credential_valid.return_value = True
        client.get.return_value = {
            "items": [{"main_buy": 100, "main_sell": -300, "net_all": 999_999}]
        }
        r = KplFetcher(client=client).get_capital_flow("000977", days=1)
        self.assertEqual(r["main_net_inflow"], -200)

    def test_no_main_coverage_is_not_zero_inflow(self) -> None:
        """有成交但主力买卖全 0 == 该标的无主力资金覆盖，必须跳过。

        实测北交所 920689 当日 turnover 14,501,862（与日线 amount 逐位一致）
        而 main_buy/main_sell 均为 0。当作真值 0 会用「零流入」冒充「无数据」，
        使下游判为中性而不是回落到其它数据源。
        """
        client = MagicMock()
        client.is_credential_valid.return_value = True
        client.get.return_value = {
            "items": [{"main_buy": 0, "main_sell": 0, "turnover": 14_501_862}]
        }
        self.assertIsNone(KplFetcher(client=client).get_capital_flow("920689", days=5))

    def test_genuine_zero_net_on_flat_day_is_kept(self) -> None:
        """买卖相抵得到净额 0 是真值，不能与「无覆盖」混为一谈。"""
        client = MagicMock()
        client.is_credential_valid.return_value = True
        client.get.return_value = {
            "items": [{"main_buy": 500, "main_sell": -500, "turnover": 9_000}]
        }
        r = KplFetcher(client=client).get_capital_flow("000977", days=1)
        self.assertIsNotNone(r)
        self.assertEqual(r["main_net_inflow"], 0)

    def test_lookback_window_survives_spring_festival(self) -> None:
        """10 个交易日最坏要跨 4 个周末(8 天)+ 春节连休(约 9 天)。

        余量不足会让 inflow_10d 静默变 None，而不是报错。
        """
        from data_provider import kpl_fetcher as K

        probe = K._MAX_FLOW_DAYS * 2 + K._FLOW_LOOKBACK_SLACK
        self.assertGreaterEqual(probe, 10 + 8 + 9)


class TestCapitalFlowFallbackChain(unittest.TestCase):
    """降级链：KPL → Mairuiapi → AkShare。"""

    def _adapter(self):
        from data_provider.fundamental_adapter import AkshareFundamentalAdapter

        a = AkshareFundamentalAdapter()
        a._kpl_fetcher = None
        return a

    def test_kpl_preferred_when_enabled(self) -> None:
        a = self._adapter()
        flow = {"main_net_inflow": 1.0, "inflow_5d": 2.0, "inflow_10d": 3.0, "trade_date": "20260724"}
        with patch.object(a, "_get_capital_flow_kpl", return_value=flow), \
                patch.object(a, "_get_capital_flow_mairui") as mairui:
            result = a.get_capital_flow("000977")
        self.assertIn("capital_stock:kpl", result["source_chain"])
        mairui.assert_not_called(), "KPL 有结果时不应再调用 Mairuiapi"

    def test_falls_back_to_mairui_when_kpl_absent(self) -> None:
        a = self._adapter()
        flow = {"main_net_inflow": 9.0, "inflow_5d": None, "inflow_10d": None}
        with patch.object(a, "_get_capital_flow_kpl", return_value=None), \
                patch.object(a, "_get_capital_flow_mairui", return_value=flow):
            result = a.get_capital_flow("000977")
        self.assertIn("capital_stock:mairui", result["source_chain"])
        self.assertEqual(result["stock_flow"]["main_net_inflow"], 9.0)

    def test_kpl_disabled_returns_none_without_touching_fetcher(self) -> None:
        """未启用 KPL 时不应构造 fetcher，保持「不配置也可运行」。"""
        from types import SimpleNamespace

        a = self._adapter()
        with patch("src.config.get_config", return_value=SimpleNamespace(kpl_enabled=False)):
            self.assertIsNone(a._get_capital_flow_kpl("000977"))
        self.assertIsNone(a._kpl_fetcher)

    def test_kpl_exception_is_swallowed(self) -> None:
        """资金流是增强信息，KPL 异常不能打断基本面链路。"""
        from types import SimpleNamespace

        a = self._adapter()
        with patch("src.config.get_config", return_value=SimpleNamespace(kpl_enabled=True)), \
                patch("data_provider.kpl_fetcher.KplFetcher", side_effect=RuntimeError("boom")):
            self.assertIsNone(a._get_capital_flow_kpl("000977"))


if __name__ == "__main__":
    unittest.main()


class TestKplFundamentalBundle(unittest.TestCase):
    """KPL 成长/业绩/机构三块取数。"""

    def _fetcher(self, finance=None, reminder=None, holders=None):
        client = MagicMock()
        client.is_credential_valid.return_value = True

        def fake_get(path, params=None):
            if "finance-info" in path:
                return finance if finance is not None else {}
            if "stock-big-reminder" in path:
                return reminder if reminder is not None else {}
            if "gudong-info-ten-by-date" in path:
                return holders if holders is not None else {}
            return {}

        client.get.side_effect = fake_get
        return KplFetcher(client=client)

    def test_growth_strips_percent_suffix(self) -> None:
        """上游把数值与单位放在同一字符串里（"-19.59%"），直接 float() 会失败。"""
        f = self._fetcher(finance={
            "reports": [{"YLNL_JZCSYL": "2.75%", "YLNL_XSMLL": "6.64%"}],
            "yoy_reports": [{"GJZB_YYSR": "-19.59%", "GJZB_JLR": "-35.02%"}],
        })
        g = f.get_fundamental_bundle("000977")["growth"]
        self.assertAlmostEqual(g["revenue_yoy"], -19.59)
        self.assertAlmostEqual(g["net_profit_yoy"], -35.02)
        self.assertAlmostEqual(g["roe"], 2.75)
        self.assertAlmostEqual(g["gross_margin"], 6.64)

    def test_growth_missing_field_stays_none_for_fallback(self) -> None:
        """银行股没有销售毛利率，该字段须留 None 让 AkShare 兜底，不能猜。"""
        f = self._fetcher(finance={
            "reports": [{"YLNL_JZCSYL": "2.21%"}],
            "yoy_reports": [{"GJZB_YYSR": "16.21%"}],
        })
        g = f.get_fundamental_bundle("601398")["growth"]
        self.assertIsNone(g["gross_margin"])
        self.assertAlmostEqual(g["revenue_yoy"], 16.21)

    def test_earnings_rejects_non_forecast_titles(self) -> None:
        """type=5 是财报公告混合流，不筛标题会把年报当成业绩预告。

        实测 6 只标的首条分别是业绩预告/季度报告/半年度报告摘要/H股公告/
        年度报告，只有 2/6 真是预告。
        """
        f = self._fetcher(reminder={"items": [
            {"title": "2025年年度报告", "date": "2026-04-28",
             "raw": {"type": 5, "tag": 1}},
            {"title": "贵州茅台2026年第一季度报告", "date": "2026-04-25",
             "raw": {"type": 5, "tag": 1}},
        ]})
        self.assertEqual(f.get_fundamental_bundle("600519")["earnings"], {})

    def test_earnings_accepts_real_forecast(self) -> None:
        f = self._fetcher(reminder={"items": [
            {"title": "2025年年度报告", "date": "2026-04-28", "raw": {"type": 5, "tag": 1}},
            {"title": "2026年半年度业绩预告", "date": "2026-07-08", "raw": {"type": 5, "tag": 1}},
        ]})
        e = f.get_fundamental_bundle("000977")["earnings"]
        self.assertIn("业绩预告", e["forecast_summary"])
        self.assertIn("2026-07-08", e["forecast_summary"])

    def test_earnings_ignores_non_financial_tag(self) -> None:
        """tag=2 是减持类公告，即使标题恰好含关键词也不能当业绩预告。"""
        f = self._fetcher(reminder={"items": [
            {"title": "关于持股5%以上股东减持股份的预披露公告", "date": "2026-07-01",
             "raw": {"type": 5, "tag": 2}},
        ]})
        self.assertEqual(f.get_fundamental_bundle("000977")["earnings"], {})

    def test_holder_change_is_percent_not_shares(self) -> None:
        """上游 change_from_last 是较上期变化百分比，不是万股。

        已用相邻两期持股交叉验算：3526.77→3383.30 万股（-143.47 万股）对应
        -4.07，正是 -4.07%。按万股解读会得到完全错误的量级。
        """
        f = self._fetcher(holders={"items": [
            {"holding_shares_wan": "3383.30", "change_from_last": "-4.07"},
        ]})
        i = f.get_fundamental_bundle("000977")["institution"]
        # 上期 = 3383.30 / (1 - 0.0407) = 3526.77，合计口径下变化率仍是 -4.07%
        self.assertAlmostEqual(i["top10_holder_change"], -4.07, places=2)

    def test_holder_change_handles_unchanged_and_new_entries(self) -> None:
        """取值有三种形态：数值 / "不变" / "新进"（上期持股为 0）。"""
        f = self._fetcher(holders={"items": [
            {"holding_shares_wan": "1000", "change_from_last": "不变"},
            {"holding_shares_wan": "100", "change_from_last": "新进"},
        ]})
        i = f.get_fundamental_bundle("000977")["institution"]
        # 上期合计 1000，本期合计 1100 → +10%
        self.assertAlmostEqual(i["top10_holder_change"], 10.0, places=4)

    def test_holder_falls_back_to_earlier_quarter(self) -> None:
        """最新报告期常未披露（半年报要到 8 月底），须往前找到有数据的一期。"""
        client = MagicMock()
        client.is_credential_valid.return_value = True
        calls = []

        def fake_get(path, params=None):
            if "gudong-info-ten-by-date" not in path:
                return {}
            day = (params or {}).get("day")
            calls.append(day)
            if len(calls) == 1:
                return {"items": []}      # 最新一期尚未披露
            return {"items": [{"holding_shares_wan": "500", "change_from_last": "不变"}]}

        client.get.side_effect = fake_get
        i = KplFetcher(client=client).get_fundamental_bundle("000977")["institution"]
        self.assertIsNotNone(i.get("top10_holder_change"))
        self.assertGreaterEqual(len(calls), 2, "首期为空时必须继续回溯")
        self.assertEqual(i["top10_report_date"], calls[1])

    def test_unavailable_returns_none(self) -> None:
        client = MagicMock()
        client.is_credential_valid.return_value = False
        self.assertIsNone(KplFetcher(client=client).get_fundamental_bundle("000977"))

    def test_non_a_share_rejected(self) -> None:
        for bad in ("AAPL", "00700", ""):
            self.assertIsNone(self._fetcher().get_fundamental_bundle(bad))

    def test_single_endpoint_failure_does_not_lose_other_blocks(self) -> None:
        """三块各自独立取数，一块失败不能拖掉其它两块。"""
        client = MagicMock()
        client.is_credential_valid.return_value = True

        def fake_get(path, params=None):
            if "finance-info" in path:
                raise KplRequestError("finance down")
            if "stock-big-reminder" in path:
                return {"items": [{"title": "2026年半年度业绩预告", "date": "2026-07-08",
                                   "raw": {"type": 5, "tag": 1}}]}
            return {"items": [{"holding_shares_wan": "500", "change_from_last": "不变"}]}

        client.get.side_effect = fake_get
        b = KplFetcher(client=client).get_fundamental_bundle("000977")
        self.assertEqual(b["growth"], {})
        self.assertIn("业绩预告", b["earnings"]["forecast_summary"])
        self.assertIsNotNone(b["institution"]["top10_holder_change"])


class TestBundleMergePrefersKplKeepsAkshare(unittest.TestCase):
    """合并策略：KPL 优先补空，AkShare 结果完整保留。"""

    def _adapter(self):
        from data_provider.fundamental_adapter import AkshareFundamentalAdapter

        a = AkshareFundamentalAdapter()
        a._kpl_fetcher = None
        return a

    def test_kpl_only_fills_missing_fields(self) -> None:
        """AkShare 已有值的字段不被 KPL 覆盖。"""
        a = self._adapter()
        result = {
            "growth": {"revenue_yoy": 11.0, "roe": None},
            "earnings": {}, "institution": {}, "source_chain": [], "errors": [],
        }
        kpl = {"growth": {"revenue_yoy": 99.0, "roe": 2.75},
               "earnings": {}, "institution": {}}
        with patch.object(a, "_get_fundamental_bundle_kpl", return_value=kpl):
            a._merge_bundle_kpl("000977", result)
        self.assertEqual(result["growth"]["revenue_yoy"], 11.0, "不得覆盖 AkShare 已有值")
        self.assertEqual(result["growth"]["roe"], 2.75, "空字段应由 KPL 补上")
        self.assertIn("bundle:kpl", result["source_chain"])

    def test_kpl_absent_leaves_akshare_result_untouched(self) -> None:
        a = self._adapter()
        result = {"growth": {"revenue_yoy": 11.0}, "earnings": {}, "institution": {},
                  "source_chain": [], "errors": []}
        with patch.object(a, "_get_fundamental_bundle_kpl", return_value=None):
            a._merge_bundle_kpl("000977", result)
        self.assertEqual(result["growth"], {"revenue_yoy": 11.0})
        self.assertNotIn("bundle:kpl", result["source_chain"])

    def test_kpl_disabled_does_not_construct_fetcher(self) -> None:
        from types import SimpleNamespace

        a = self._adapter()
        with patch("src.config.get_config", return_value=SimpleNamespace(kpl_enabled=False)):
            self.assertIsNone(a._get_fundamental_bundle_kpl("000977"))
        self.assertIsNone(a._kpl_fetcher)

    def test_kpl_exception_is_swallowed(self) -> None:
        from types import SimpleNamespace

        a = self._adapter()
        with patch("src.config.get_config", return_value=SimpleNamespace(kpl_enabled=True)), \
                patch("data_provider.kpl_fetcher.KplFetcher", side_effect=RuntimeError("boom")):
            self.assertIsNone(a._get_fundamental_bundle_kpl("000977"))


class TestKplChipDistribution(unittest.TestCase):
    """KPL 筹码分布（持仓成本分布）。

    ⚠️ 上游是**本地计算**：开盘啦对成本分布既无 HTTP 也无 Socket 接口，该端点
    用同族算法（三角形分布+换手衰减）基于日线复现。因此它不受凭证过期影响，
    但也不是官方数据。

    与另两源三方对拍（2026-07-27 收盘时点）显示三方均自洽，本源与 AkShare
    趋同而 Tushare 离群；因为都是算法输出、无绝对真值，这里不断言谁更准，
    只锁定单位换算与契约。
    """

    SAMPLE = {
        "stock_id": "002364", "close": 40.0, "avg_cost": 48.865, "profit_pct": 24.11,
        "range_90": {"low": 37.042, "high": 58.213, "concentration": 22.23},
        "range_70": {"low": 38.722, "high": 56.019, "concentration": 18.26},
        "day_count": 1000,
    }

    def _fetcher(self, chip=None, kline=None, valid=True):
        client = MagicMock()
        client.is_credential_valid.return_value = valid

        def fake_get(path, params=None):
            if "stock-chip-distribution" in path:
                if chip is None:
                    raise KplRequestError("chip down")
                return chip
            if "kline/daily" in path:
                return kline if kline is not None else {"days": [{"date": "20260727"}]}
            return {}

        client.get.side_effect = fake_get
        return KplFetcher(client=client)

    def test_percent_fields_converted_to_ratio(self) -> None:
        """上游 profit_pct / concentration 是百分数，契约要求 0~1 小数。

        直接透传会让下游把 24.11 当成 2411% 的获利盘。
        """
        c = self._fetcher(self.SAMPLE).get_chip_distribution("002364")
        self.assertAlmostEqual(c.profit_ratio, 0.2411, places=4)
        self.assertAlmostEqual(c.concentration_90, 0.2223, places=4)
        self.assertAlmostEqual(c.concentration_70, 0.1826, places=4)

    def test_price_fields_passed_through(self) -> None:
        c = self._fetcher(self.SAMPLE).get_chip_distribution("002364")
        self.assertAlmostEqual(c.avg_cost, 48.865)
        self.assertAlmostEqual(c.cost_90_low, 37.042)
        self.assertAlmostEqual(c.cost_90_high, 58.213)
        self.assertAlmostEqual(c.cost_70_low, 38.722)
        self.assertAlmostEqual(c.cost_70_high, 56.019)

    def test_source_is_kpl(self) -> None:
        """ChipDistribution.source 默认值是 "akshare"，不显式赋值会让来源追溯失真。"""
        self.assertEqual(self._fetcher(self.SAMPLE).get_chip_distribution("002364").source, "kpl")

    def test_date_filled_from_kline(self) -> None:
        """上游只回 close 不回日期，需补上数据日期供报告显示新鲜度。"""
        c = self._fetcher(self.SAMPLE).get_chip_distribution("002364")
        self.assertEqual(c.date, "2026-07-27")

    def test_date_failure_does_not_lose_chip(self) -> None:
        """补日期是附加动作，失败不能拖掉筹码本身。"""
        client = MagicMock()
        client.is_credential_valid.return_value = True

        def fake_get(path, params=None):
            if "stock-chip-distribution" in path:
                return self.SAMPLE
            raise KplRequestError("kline down")

        client.get.side_effect = fake_get
        c = KplFetcher(client=client).get_chip_distribution("002364")
        self.assertIsNotNone(c)
        self.assertEqual(c.date, "")
        self.assertAlmostEqual(c.avg_cost, 48.865)

    def test_decay_not_exposed(self) -> None:
        """decay 有意不透传：0.8~1.2 会让获利比在 0.197~0.284 间摆动（差 45%）。

        暴露成可调项会让同一标的不同调用给出不同结论。
        """
        f = self._fetcher(self.SAMPLE)
        f.get_chip_distribution("002364")
        for call in f._client.get.call_args_list:
            self.assertNotIn("decay", (call.kwargs.get("params") or {}))
            self.assertNotIn("decay", call.args[0])

    def test_invalid_avg_cost_returns_none(self) -> None:
        for bad in ({**SAMPLE_ZERO}, {**SAMPLE_ZERO, "avg_cost": None}):
            self.assertIsNone(self._fetcher(bad).get_chip_distribution("002364"))

    def test_none_when_unavailable_or_upstream_error(self) -> None:
        self.assertIsNone(self._fetcher(self.SAMPLE, valid=False).get_chip_distribution("002364"))
        self.assertIsNone(self._fetcher(None).get_chip_distribution("002364"))

    def test_non_a_share_rejected(self) -> None:
        for bad in ("AAPL", "00700", ""):
            self.assertIsNone(self._fetcher(self.SAMPLE).get_chip_distribution(bad))


SAMPLE_ZERO = {"avg_cost": 0, "profit_pct": 0, "range_90": {}, "range_70": {}}
