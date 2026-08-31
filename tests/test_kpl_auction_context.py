# -*- coding: utf-8 -*-
"""KPL 盘前/盘后竞价聚合上下文测试。"""

import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from data_provider.kpl_fetcher import KplFetcher


def _afterhours_payload(session_date: str) -> dict:
    return {
        "configured": True,
        "stock_id": "603993",
        "date": session_date,
        "count": 2,
        "records": [
            {
                "time": "15:05:10",
                "price": 18.44,
                "raw_fields": {"3": 50, "5": 300},
            },
            {
                "time": "15:05:20",
                "price": 18.44,
                "raw_fields": {"4": 20, "5": 100},
            },
        ],
    }


def _fenbi_afterhours_payload(day: str) -> dict:
    return {
        "day": day,
        "total": 4,
        "items": [
            {
                "time": "09:25:01",
                "price": 19.0,
                "price_status": 2,
                "volume_hand": 15800,
                "order_count": 1241,
                "trade_side": 3,
                "reserved": 0,
                "amount": 30020000.0,
            },
            {
                "time": "15:05:03",
                "price": 18.41,
                "price_status": 0,
                "volume_hand": 389,
                "order_count": 0,
                "trade_side": 1,
                "reserved": 0,
                "amount": 716149.0,
            },
            {
                "time": "15:10:00",
                "price": 18.41,
                "price_status": 0,
                "volume_hand": 1,
                "order_count": 0,
                "trade_side": 0,
                "reserved": 0,
                "amount": 1841.0,
            },
            {
                "time": "15:20:42",
                "price": 18.41,
                "price_status": 0,
                "volume_hand": 8,
                "order_count": 0,
                "trade_side": 1,
                "reserved": 0,
                "amount": 14728.0,
            },
        ],
    }


def _premarket_payload(auction_date: str) -> dict:
    return {
        "code": "603993",
        "day": auction_date,
        "preclose": 18.41,
        "open": 18.41,
        "high": 18.8,
        "low": 18.18,
        "flag": 1,
        "bid_count": 2,
        "bid": [
            {"time": "09:15", "price": 18.8, "field_2": 0, "volume": 20},
            {"time": "09:25", "price": 18.41, "field_2": 1, "volume": 167},
        ],
    }


class TestKplAuctionContext(unittest.TestCase):
    def _fetcher(
        self,
        current_session_date: str,
        historical_date: str,
        today: str,
    ) -> KplFetcher:
        client = MagicMock()
        client.is_credential_valid.return_value = True

        def fake_get(path, params=None):
            if path == "/market-stats/afterhours-auction-trend":
                return _afterhours_payload(current_session_date)
            if path == "/kline/stock-fenbi2/603993":
                day = (params or {}).get("day")
                return (
                    _fenbi_afterhours_payload(day)
                    if day == historical_date
                    else {"day": day, "total": 0, "items": []}
                )
            if path == "/auction/stock-bid/603993":
                return _premarket_payload(today)
            raise AssertionError(f"unexpected path: {path}")

        client.get.side_effect = fake_get
        return KplFetcher(client=client)

    def test_before_afterhours_complete_uses_historical_fenbi(self) -> None:
        today = date.today().strftime("%Y%m%d")
        prev_day = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
        fetcher = self._fetcher(prev_day, prev_day, today)
        with patch.object(KplFetcher, "_is_afterhours_complete", return_value=False):
            ctx = fetcher.get_auction_context("603993")

        self.assertEqual(ctx["prev_afterhours"]["session_date"], prev_day)
        self.assertEqual(ctx["prev_afterhours"]["source"], "stock_fenbi2")
        self.assertEqual(ctx["prev_afterhours"]["total_volume_hand"], 398.0)
        self.assertEqual(ctx["prev_afterhours"]["buy_volume_hand"], 397.0)
        self.assertEqual(ctx["prev_afterhours"]["sell_volume_hand"], 1.0)
        self.assertEqual(ctx["today_afterhours"], {})
        self.assertEqual(ctx["today_premarket"]["final_volume_hand"], 167)
        self.assertEqual(ctx["today_premarket"]["bid_change_pct"], 0.0)
        self.assertAlmostEqual(
            ctx["today_premarket"]["estimated_turnover_amount"],
            167 * 18.41 * 100,
        )

    def test_after_afterhours_complete_keeps_prev_and_today(self) -> None:
        today = date.today().strftime("%Y%m%d")
        prev_day = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
        fetcher = self._fetcher(today, prev_day, today)
        with patch.object(KplFetcher, "_is_afterhours_complete", return_value=True):
            ctx = fetcher.get_auction_context("603993")

        self.assertEqual(ctx["prev_afterhours"]["session_date"], prev_day)
        self.assertEqual(ctx["today_afterhours"]["session_date"], today)
        self.assertEqual(ctx["today_afterhours"]["total_volume_hand"], 400.0)

    def test_no_data_returns_none(self) -> None:
        client = MagicMock()
        client.is_credential_valid.return_value = True

        def fake_get(path, params=None):
            if path == "/market-stats/afterhours-auction-trend":
                return {"configured": True, "count": 0, "records": []}
            if path == "/kline/stock-fenbi2/603993":
                return {"day": (params or {}).get("day"), "total": 0, "items": []}
            if path == "/auction/stock-bid/603993":
                return {"code": "603993", "day": "20260814", "bid": []}
            raise AssertionError(f"unexpected path: {path}")

        client.get.side_effect = fake_get
        fetcher = KplFetcher(client=client)
        self.assertIsNone(fetcher.get_auction_context("603993"))

    def test_unavailable_returns_none(self) -> None:
        client = MagicMock()
        client.is_credential_valid.return_value = False
        fetcher = KplFetcher(client=client)
        self.assertIsNone(fetcher.get_auction_context("603993"))


if __name__ == "__main__":
    unittest.main()
