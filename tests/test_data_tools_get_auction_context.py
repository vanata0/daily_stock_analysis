# -*- coding: utf-8 -*-
"""Contract tests for get_auction_context tool output."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.agent.tools.data_tools import _handle_get_auction_context


class _FakeKplFetcher:
    def get_auction_context(self, stock_code: str):
        return {
            "stock_code": stock_code,
            "prev_afterhours": {
                "session_date": "20260813",
                "total_volume_hand": 1118.0,
            },
            "today_premarket": {
                "auction_date": "20260814",
                "final_volume_hand": 167,
                "bid_change_pct": 0.0,
            },
            "today_afterhours": {},
        }


class _DummyManager:
    def __init__(self, fetcher=None):
        self.fetcher = fetcher

    def _get_fetcher_by_name(self, name: str):
        return self.fetcher


class TestGetAuctionContextContract(unittest.TestCase):
    def test_ok_response_shape(self) -> None:
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManager(_FakeKplFetcher()),
        ):
            result = _handle_get_auction_context("603993")

        self.assertEqual(result["stock_code"], "603993")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["prev_afterhours"]["total_volume_hand"], 1118.0)
        self.assertEqual(result["today_premarket"]["final_volume_hand"], 167)
        self.assertIn("today_afterhours", result)

    def test_not_supported_without_kpl(self) -> None:
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManager(None),
        ):
            result = _handle_get_auction_context("603993")

        self.assertEqual(result["status"], "not_supported")
        self.assertIn("note", result)

    def test_no_data(self) -> None:
        class _EmptyFetcher:
            def get_auction_context(self, stock_code: str):
                return None

        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManager(_EmptyFetcher()),
        ):
            result = _handle_get_auction_context("603993")

        self.assertEqual(result["status"], "no_data")
        self.assertIn("note", result)


if __name__ == "__main__":
    unittest.main()
