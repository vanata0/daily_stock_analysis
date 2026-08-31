# -*- coding: utf-8 -*-
"""KplHttpClient 单元测试：错误映射与凭证失效探针。

探针是 KPL 接入的安全底线——上游凭证过期时不报错、只静默返回空数据，
这些用例覆盖真实环境无法复现的失效路径。
"""

import importlib.util
import sys
import unittest
from datetime import date, datetime
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


def _probe(client, status: int, text: str = ""):
    """给 /health/credential 打桩：探针直接读 session.get 的状态码，不经过 client.get()。"""
    return patch.object(client._session, "get", return_value=_resp(status, text=text))


class TestKplConnectionReuse(unittest.TestCase):
    """本机回环调用不复用连接，避免空闲 socket 滞留在 CLOSE-WAIT。"""

    def test_session_disables_keep_alive(self) -> None:
        client = KplHttpClient()
        self.assertEqual(client._session.headers.get("Connection"), "close")


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
    """凭证失效探针 —— 打 /health/credential，不经过任何业务 Response Model。

    2026-08-11~12 的真实事故：旧探针打业务端点 /market-stats/mood-num-count，
    该端点因响应模型必填了一个客户端已改名的字段而稳定 500，把 KPL 全部接口
    一起拖进降级。/health/credential 是 kpl-unified-client 专为此开的探针端点，
    响应不经过业务模型，只用状态码传递语义（200/401/503）。
    """

    def test_200_is_valid(self) -> None:
        client = KplHttpClient()
        with _probe(client, 200):
            self.assertTrue(client.is_credential_valid())

    def test_401_on_trading_day_after_open_is_invalid(self) -> None:
        """开盘后仍 401 == 凭证失效。"""
        client = KplHttpClient()
        with _probe(client, 401, text="empty_response"), \
                patch.object(client, "_is_trading_day", return_value=True), \
                patch.object(client, "_is_before_market_open", return_value=False):
            self.assertFalse(client.is_credential_valid())

    def test_401_on_non_trading_day_stays_valid(self) -> None:
        """休市当天没有涨跌家数，不能据此判定凭证失效。"""
        client = KplHttpClient()
        with _probe(client, 401), patch.object(client, "_is_trading_day", return_value=False):
            self.assertTrue(client.is_credential_valid())

    def test_rate_limited_probe_does_not_mark_invalid(self) -> None:
        """限流不代表凭证失效，否则会因为并发把整个数据源误杀。"""
        client = KplHttpClient()
        with _probe(client, 429, text="too many"):
            self.assertTrue(client.is_credential_valid())

    def test_upstream_unreachable_marks_unavailable_without_alert(self) -> None:
        """503 是上游问题，判不可用但不是凭证失效，不应触发凭证失效告警。"""
        fired = []
        client = KplHttpClient(on_credential_expired=lambda r: fired.append(r))
        with _probe(client, 503, text="upstream_error"):
            self.assertFalse(client.is_credential_valid())
        self.assertEqual(fired, [])

    def test_request_failure_marks_unavailable(self) -> None:
        client = KplHttpClient()
        with patch.object(
            client._session, "get", side_effect=requests.exceptions.ConnectTimeout("boom")
        ):
            self.assertFalse(client.is_credential_valid())

    def test_unexpected_status_marks_unavailable(self) -> None:
        """既不是探针约定的三档状态码，也判不可用，不能当成 200 放行。"""
        client = KplHttpClient()
        with _probe(client, 500, text="Internal Server Error"):
            self.assertFalse(client.is_credential_valid())

    def test_probe_result_is_cached_within_ttl(self) -> None:
        client = KplHttpClient()
        with patch.object(client._session, "get", return_value=_resp(200)) as mocked:
            self.assertTrue(client.is_credential_valid())
            self.assertTrue(client.is_credential_valid())
            self.assertTrue(client.is_credential_valid())
        self.assertEqual(mocked.call_count, 1, "TTL 内应复用缓存，不重复探测")

    def test_force_bypasses_cache(self) -> None:
        client = KplHttpClient()
        with patch.object(client._session, "get", return_value=_resp(200)) as mocked:
            client.is_credential_valid()
            client.is_credential_valid(force=True)
        self.assertEqual(mocked.call_count, 2)

    def test_reset_probe_cache_forces_reprobe(self) -> None:
        client = KplHttpClient()
        with patch.object(client._session, "get", return_value=_resp(200)) as mocked:
            client.is_credential_valid()
            client.reset_probe_cache()
            client.is_credential_valid()
        self.assertEqual(mocked.call_count, 2)


class TestPreMarketOpenWindow(unittest.TestCase):
    """交易日开盘前不得判定凭证失效。

    上游会在当天早盘前把上一交易日的涨跌家数清零，此时 rise/fall 全 0 是正常
    状态。实测 2026-07-28 09:01（交易日、未开盘）该端点返回 rise=0/fall=0 且
    errcode='0'，凭证完全有效。漏掉这一段会让每个交易日 09:30 之前 KPL 全线
    不可用，并且每天早上误发一次凭证失效告警。
    """

    def test_401_before_open_stays_valid(self) -> None:
        client = KplHttpClient()
        with _probe(client, 401), \
                patch.object(client, "_is_trading_day", return_value=True), \
                patch.object(client, "_is_before_market_open", return_value=True):
            self.assertTrue(client.is_credential_valid())

    def test_no_alert_before_open(self) -> None:
        fired = []
        c = KplHttpClient(on_credential_expired=lambda r: fired.append(r))
        with _probe(c, 401), \
                patch.object(c, "_is_trading_day", return_value=True), \
                patch.object(c, "_is_before_market_open", return_value=True):
            c.is_credential_valid()
        self.assertEqual(fired, [], "开盘前的空数据不能触发告警")

    def test_window_boundary(self) -> None:
        """09:30 连续竞价开始，此后空数据才是真失效信号。"""
        client = KplHttpClient()
        for hh, mm, expected in (
            (0, 0, True), (8, 40, True), (9, 24, True), (9, 29, True),
            (9, 30, False), (10, 0, False), (15, 30, False), (23, 59, False),
        ):
            with self.subTest(f"{hh:02d}:{mm:02d}"):
                self.assertEqual(
                    client._is_before_market_open(datetime(2026, 7, 28, hh, mm)),
                    expected,
                )

    def test_after_open_401_is_invalid(self) -> None:
        """开盘后仍然 401 才是真正的凭证失效。"""
        client = KplHttpClient()
        with _probe(client, 401), \
                patch.object(client, "_is_trading_day", return_value=True), \
                patch.object(client, "_is_before_market_open", return_value=False):
            self.assertFalse(client.is_credential_valid())


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


class TestCredentialExpiredCallback(unittest.TestCase):
    """凭证失效告警回调 —— 让静默失效变成能被人看到的事件。"""

    def _client(self, fired):
        return KplHttpClient(on_credential_expired=lambda reason: fired.append(reason))

    def test_callback_fires_on_trading_day_expiry(self) -> None:
        fired = []
        c = self._client(fired)
        with _probe(c, 401, text="empty_response"), \
                patch.object(c, "_is_trading_day", return_value=True), \
                patch.object(c, "_is_before_market_open", return_value=False):
            self.assertFalse(c.is_credential_valid())
        self.assertEqual(len(fired), 1)
        self.assertIn("empty_response", fired[0])

    def test_callback_not_fired_when_healthy(self) -> None:
        fired = []
        c = self._client(fired)
        with _probe(c, 200):
            c.is_credential_valid()
        self.assertEqual(fired, [])

    def test_callback_not_fired_on_non_trading_day(self) -> None:
        """休市空数据不是失效，不能误报。"""
        fired = []
        c = self._client(fired)
        with _probe(c, 401), patch.object(c, "_is_trading_day", return_value=False):
            c.is_credential_valid()
        self.assertEqual(fired, [])

    def test_callback_fires_once_across_ttl_expiry(self) -> None:
        """探针 TTL 每 5 分钟到期一次，不能每次都重复告警。"""
        fired = []
        c = self._client(fired)
        with _probe(c, 401), \
                patch.object(c, "_is_trading_day", return_value=True), \
                patch.object(c, "_is_before_market_open", return_value=False):
            for _ in range(4):
                c.is_credential_valid(force=True)
        self.assertEqual(len(fired), 1, "处于失效态时不应重复告警")

    def test_callback_exception_does_not_break_probe(self) -> None:
        """告警栈挂了不能影响数据源降级本身。"""
        def boom(_reason):
            raise RuntimeError("notification stack down")
        c = KplHttpClient(on_credential_expired=boom)
        with _probe(c, 401), \
                patch.object(c, "_is_trading_day", return_value=True), \
                patch.object(c, "_is_before_market_open", return_value=False):
            self.assertFalse(c.is_credential_valid())

    def test_no_callback_configured_is_safe(self) -> None:
        c = KplHttpClient()
        with _probe(c, 401), \
                patch.object(c, "_is_trading_day", return_value=True), \
                patch.object(c, "_is_before_market_open", return_value=False):
            self.assertFalse(c.is_credential_valid())


if __name__ == "__main__":
    unittest.main()
