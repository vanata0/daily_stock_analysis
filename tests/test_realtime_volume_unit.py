# -*- coding: utf-8 -*-
"""实时行情 volume 单位：UnifiedRealtimeQuote.volume 统一按「股」。

A 股行情源普遍按「手」报量（1 手 = 100 股），换算漏了或做反了都不会报错，
只会让成交量差 100 倍，静默污染量比、换手率与一切基于成交量的判断。

判据是可独立验证的算术：``amount / volume`` 反推出的均价必须落在当日价格
区间附近。实测中恒电气（002364）修复前 Tushare 与 Efinance 反推均价都是
3830 上下，而现价 38.28，恰好 100 倍；同一时刻 KPL 与 TickFlow 都落在 38.30。
"""

import importlib.util
import sys
import unittest
from unittest.mock import MagicMock

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

try:
    json_repair_available = importlib.util.find_spec("json_repair") is not None
except ValueError:
    json_repair_available = "json_repair" in sys.modules

if not json_repair_available and "json_repair" not in sys.modules:
    sys.modules["json_repair"] = MagicMock()

from data_provider.efinance_fetcher import (
    _realtime_volume_to_shares as ef_to_shares,
)
from data_provider.tushare_fetcher import (
    _realtime_volume_to_shares as ts_to_shares,
)


class TestRealtimeVolumeUnit(unittest.TestCase):
    """A 股按手报量需 ×100；港美股本就按股，不能重复放大。"""

    def _cases(self):
        return (("tushare", ts_to_shares), ("efinance", ef_to_shares))

    def test_a_share_converted_to_shares(self) -> None:
        for label, fn in self._cases():
            with self.subTest(label):
                self.assertEqual(fn(3478, "002364"), 347_800)

    def test_a_share_etf_also_converted(self) -> None:
        """ETF 同样按份额整百申报。"""
        for label, fn in self._cases():
            with self.subTest(label):
                self.assertEqual(fn(3478, "510300"), 347_800)

    def test_hk_and_us_left_alone(self) -> None:
        """港股与美股按股报量，再 ×100 会反向放大 100 倍。"""
        for label, fn in self._cases():
            for code in ("00700", "AAPL"):
                with self.subTest(f"{label}-{code}"):
                    self.assertEqual(fn(3478, code), 3478)

    def test_none_passes_through(self) -> None:
        for label, fn in self._cases():
            with self.subTest(label):
                self.assertIsNone(fn(None, "002364"))

    def test_implied_average_price_lands_in_range(self) -> None:
        """回归判据本身：换算后 amount/volume 必须接近现价。

        取自 002364 实测的一组真实数值。
        """
        price, amount, upstream_hands = 38.28, 996_306_389.0, 260_124
        for label, fn in self._cases():
            with self.subTest(label):
                volume = fn(upstream_hands, "002364")
                implied = amount / volume
                self.assertTrue(
                    0.5 * price < implied < 2 * price,
                    f"{label} 反推均价 {implied:.2f} 偏离现价 {price}",
                )


class TestTushareLegacyQuoteKeepsShares(unittest.TestCase):
    """旧版 ts.get_realtime_quotes 上游本就给「股」，不得再除以 100。"""

    def test_no_division_back_to_hands(self) -> None:
        import inspect

        from data_provider.tushare_fetcher import TushareFetcher

        src = inspect.getsource(TushareFetcher.get_realtime_quote)
        self.assertNotIn(
            "// 100",
            src,
            "旧版实时接口曾把上游的「股」除以 100 转成「手」，与 volume 的股口径相反",
        )


if __name__ == "__main__":
    unittest.main()
