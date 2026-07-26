# -*- coding: utf-8 -*-
"""KPL 个股资金流：取数实现与降级链。

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

# 取自 000977 实测：main_sell 上游已是负值
DAILY = {
    "20260724": (3_070_297_443, -3_205_910_638),
    "20260723": (5_949_966_930, -6_136_657_829),
    "20260722": (7_900_525_462, -7_947_982_527),
    "20260721": (6_933_213_185, -6_544_114_644),
    "20260720": (6_583_016_492, -6_079_754_787),
}


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
        self.assertTrue(all("chouma-history" in p for p in paths))
        self.assertFalse(any("main-monitor-trend" in p for p in paths))

    def test_queries_one_day_at_a_time(self) -> None:
        f = _fetcher()
        f.get_capital_flow("000977", days=5)
        days = [c.kwargs.get("params", {}).get("day") for c in f._client.get.call_args_list]
        self.assertEqual(len(days), len(set(days)), "同一天不应重复查询")


class TestCapitalFlowAggregation(unittest.TestCase):
    def test_contract_keys(self) -> None:
        r = _fetcher().get_capital_flow("000977", days=5)
        self.assertEqual(
            set(r.keys()),
            {"main_net_inflow", "inflow_5d", "inflow_10d", "trade_date"},
        )

    def test_latest_day_net(self) -> None:
        """main_sell 上游已为负值，净额是直接相加而非相减。"""
        r = _fetcher().get_capital_flow("000977", days=5)
        self.assertEqual(r["main_net_inflow"], 3_070_297_443 - 3_205_910_638)

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
        r = _fetcher(fail_days={"20260722"}).get_capital_flow("000977", days=3)
        self.assertIsNotNone(r)

    def test_none_when_unavailable_or_empty(self) -> None:
        self.assertIsNone(_fetcher(valid=False).get_capital_flow("000977"))
        self.assertIsNone(_fetcher({}).get_capital_flow("000977"))

    def test_non_a_share_rejected(self) -> None:
        for bad in ("AAPL", "00700", ""):
            self.assertIsNone(_fetcher().get_capital_flow(bad))


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
