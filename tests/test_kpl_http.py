# -*- coding: utf-8 -*-
"""KplHttpClient 单元测试：错误映射与凭证失效探针。

探针是 KPL 接入的安全底线——上游凭证过期时不报错、只静默返回空数据，
这些用例覆盖真实环境无法复现的失效路径。
"""

import importlib.util
import sys
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

try:
    json_repair_available = importlib.util.find_spec("json_repair") is not None
except ValueError:
    json_repair_available = "json_repair" in sys.modules

if not json_repair_available and "json_repair" not in sys.modules:
    sys.modules["json_repair"] = MagicMock()

import requests

from data_provider.kpl_http import (
    KplHttpClient,
    KplRateLimitError,
    KplRequestError,
    kpl_date_to_iso,
)


def _resp(status: int = 200, payload=None, text: str = ""):
    """构造一个仿 requests.Response 对象。"""
    r = MagicMock()
    r.status_code = status
    r.text = text
    if payload is None:
        r.json.side_effect = ValueError("not json")
    else:
        r.json.return_value = payload
    return r


# 一个真实交易日的探针返回（数值取自 2026-07-24 实测）
HEALTHY_BREADTH = {"rise_count": 555, "fall_count": 4939, "limit_up_count": 40}
# 凭证失效时上游的表现：HTTP 200 + errcode=0 + 全空
EXPIRED_BREADTH = {"rise_count": 0, "fall_count": 0, "limit_up_count": 0}


class TestKplHttpErrorMapping(unittest.TestCase):
    """HTTP 层错误必须抛异常，不能伪装成空数据往下游传。"""

    def test_rate_limit_raises_dedicated_error(self) -> None:
        client = KplHttpClient()
        with patch.object(client._session, "get", return_value=_resp(429, text="too many")):
            with self.assertRaises(KplRateLimitError):
                client.get("/mood/realtime")

    def test_non_200_raises_request_error(self) -> None:
        client = KplHttpClient()
        with patch.object(client._session, "get", return_value=_resp(502, text="upstream_error")):
            with self.assertRaises(KplRequestError):
                client.get("/mood/realtime")

    def test_non_json_body_raises_request_error(self) -> None:
        client = KplHttpClient()
        with patch.object(client._session, "get", return_value=_resp(200, payload=None, text="<html>")):
            with self.assertRaises(KplRequestError):
                client.get("/mood/realtime")

    def test_network_exception_raises_request_error(self) -> None:
        client = KplHttpClient()
        with patch.object(
            client._session, "get", side_effect=requests.exceptions.ConnectTimeout("boom")
        ):
            with self.assertRaises(KplRequestError):
                client.get("/mood/realtime")

    def test_successful_get_returns_parsed_json(self) -> None:
        client = KplHttpClient()
        with patch.object(client._session, "get", return_value=_resp(200, payload={"ok": 1})):
            self.assertEqual(client.get("/mood/realtime"), {"ok": 1})


class TestKplCredentialProbe(unittest.TestCase):
    """凭证失效探针 —— 本次接入的安全底线。"""

    def test_healthy_breadth_is_valid(self) -> None:
        client = KplHttpClient()
        with patch.object(client, "get", return_value=HEALTHY_BREADTH):
            self.assertTrue(client.is_credential_valid())

    def test_empty_breadth_on_trading_day_is_invalid(self) -> None:
        """交易日涨跌家数全 0 == 凭证失效（真实行情不可能出现）。"""
        client = KplHttpClient()
        with patch.object(client, "get", return_value=EXPIRED_BREADTH), \
                patch.object(client, "_is_trading_day", return_value=True):
            self.assertFalse(client.is_credential_valid())

    def test_empty_breadth_on_non_trading_day_stays_valid(self) -> None:
        """休市当天没有涨跌家数，不能据此判定凭证失效。"""
        client = KplHttpClient()
        with patch.object(client, "get", return_value=EXPIRED_BREADTH), \
                patch.object(client, "_is_trading_day", return_value=False):
            self.assertTrue(client.is_credential_valid())

    def test_rate_limited_probe_does_not_mark_invalid(self) -> None:
        """限流不代表凭证失效，否则会因为并发把整个数据源误杀。"""
        client = KplHttpClient()
        with patch.object(client, "get", side_effect=KplRateLimitError("429")):
            self.assertTrue(client.is_credential_valid())

    def test_request_failure_marks_unavailable(self) -> None:
        client = KplHttpClient()
        with patch.object(client, "get", side_effect=KplRequestError("connection refused")):
            self.assertFalse(client.is_credential_valid())

    def test_probe_result_is_cached_within_ttl(self) -> None:
        client = KplHttpClient()
        with patch.object(client, "get", return_value=HEALTHY_BREADTH) as mocked:
            self.assertTrue(client.is_credential_valid())
            self.assertTrue(client.is_credential_valid())
            self.assertTrue(client.is_credential_valid())
        self.assertEqual(mocked.call_count, 1, "TTL 内应复用缓存，不重复探测")

    def test_force_bypasses_cache(self) -> None:
        client = KplHttpClient()
        with patch.object(client, "get", return_value=HEALTHY_BREADTH) as mocked:
            client.is_credential_valid()
            client.is_credential_valid(force=True)
        self.assertEqual(mocked.call_count, 2)

    def test_reset_probe_cache_forces_reprobe(self) -> None:
        client = KplHttpClient()
        with patch.object(client, "get", return_value=HEALTHY_BREADTH) as mocked:
            client.is_credential_valid()
            client.reset_probe_cache()
            client.is_credential_valid()
        self.assertEqual(mocked.call_count, 2)

    def test_string_counts_are_tolerated(self) -> None:
        """上游历史上混用过 int 与字符串，解析不能因此判失效。"""
        client = KplHttpClient()
        with patch.object(client, "get", return_value={"rise_count": "555", "fall_count": "4939"}):
            self.assertTrue(client.is_credential_valid())


class TestTradingDayDetection(unittest.TestCase):
    """交易日判定 —— 决定空数据是否升级为凭证失效告警。"""

    def test_weekend_is_not_trading_day(self) -> None:
        client = KplHttpClient()
        with patch.object(client, "_get_holidays", return_value=set()):
            self.assertFalse(client._is_trading_day(date(2026, 7, 26)))  # 周日
            self.assertFalse(client._is_trading_day(date(2026, 7, 25)))  # 周六

    def test_weekday_not_in_holiday_list_is_trading_day(self) -> None:
        client = KplHttpClient()
        with patch.object(client, "_get_holidays", return_value={"2026-10-05"}):
            self.assertTrue(client._is_trading_day(date(2026, 7, 24)))  # 周五

    def test_weekday_in_holiday_list_is_not_trading_day(self) -> None:
        client = KplHttpClient()
        with patch.object(client, "_get_holidays", return_value={"2026-10-05"}):
            self.assertFalse(client._is_trading_day(date(2026, 10, 5)))  # 周一但放假

    def test_holiday_fetch_failure_assumes_trading_day(self) -> None:
        """节假日表取不到时保守当作交易日，避免掩盖真实的凭证过期。"""
        client = KplHttpClient()
        with patch.object(client, "_get_holidays", return_value=None):
            self.assertTrue(client._is_trading_day(date(2026, 7, 24)))

    def test_holidays_are_cached(self) -> None:
        client = KplHttpClient()
        payload = {"count": 2, "dates": ["2026-10-05", "2026-10-06"]}
        with patch.object(client, "get", return_value=payload) as mocked:
            first = client._get_holidays()
            second = client._get_holidays()
        self.assertEqual(first, {"2026-10-05", "2026-10-06"})
        self.assertEqual(second, first)
        self.assertEqual(mocked.call_count, 1)

    def test_malformed_holiday_payload_returns_none(self) -> None:
        client = KplHttpClient()
        with patch.object(client, "get", return_value={"dates": "not-a-list"}):
            self.assertIsNone(client._get_holidays())


class TestKplDateConversion(unittest.TestCase):
    """KPL 返回 YYYYMMDD，DSA 标准列要求 YYYY-MM-DD。"""

    def test_compact_date_converted(self) -> None:
        self.assertEqual(kpl_date_to_iso("20260724"), "2026-07-24")

    def test_already_iso_is_truncated(self) -> None:
        self.assertEqual(kpl_date_to_iso("2026-07-24T10:00:00"), "2026-07-24")

    def test_invalid_inputs_return_none(self) -> None:
        for bad in (None, "", "   ", "bad", "2026072", "20261332"):
            self.assertIsNone(kpl_date_to_iso(bad), f"应拒绝 {bad!r}")


if __name__ == "__main__":
    unittest.main()
