# -*- coding: utf-8 -*-
"""KplSearchProvider 单元测试。

接入 KPL 的主因是 announcements 维度：它原先硬编码走 Bocha，因为 SearXNG 的
公告结果没有 publishedDate 会被 strict_freshness 全部过滤掉。Bocha token 过期
后该维度会直接归零，这些用例守住替换后的行为。
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

from src.search_service import (
    BaseSearchProvider,
    KplSearchProvider,
    SearchService,
    _is_kpl_market_roundup,
    _kpl_epoch_to_date,
    _normalize_kpl_stock_code,
)

# 取自 /news/company-news-report-list/600519 实测
ANNOUNCEMENTS = {"items": [
    {"id": 1, "timestamp": 1784000000, "title": "贵州茅台重大事项公告",
     "type_code": "0", "pdf_url": "https://example.com/a.pdf"},
    {"id": 2, "timestamp": 1783000000, "title": "贵州茅台2025年度股东会决议公告",
     "type_code": "0", "pdf_url": "https://example.com/b.pdf"},
]}

FLASHES = {"items": [
    {"type": 1, "time": 1784900000, "content": "【公告精选：某某回购】盘后汇总，涉及数十家公司"},
    {"type": 1, "time": 1784800000, "content": "多家科技龙头公司出手回购 年内已有超800家A股公司实施回购"},
    {"type": 1, "time": 1784700000, "content": "【7月22日股市避雷针】拟减持公司数量持平"},
]}


def _provider(payload=None, valid=True, side_effect=None):
    client = MagicMock()
    client.is_credential_valid.return_value = valid
    if side_effect is not None:
        client.get.side_effect = side_effect
    else:
        client.get.return_value = payload or {}
    return KplSearchProvider(client)


class TestKplProviderSelection(unittest.TestCase):
    """选择机制：只服务显式指定它的维度，不参与轮询。"""

    def test_kpl_is_preference_only(self) -> None:
        self.assertTrue(KplSearchProvider.preference_only)

    def test_general_engines_stay_in_rotation(self) -> None:
        """默认必须为 False，否则会把通用搜索引擎挤出轮询池。"""
        self.assertFalse(BaseSearchProvider.preference_only)

    def test_availability_follows_credential_probe(self) -> None:
        self.assertTrue(_provider(valid=True).is_available)
        self.assertFalse(_provider(valid=False).is_available)

    def test_probe_exception_marks_unavailable(self) -> None:
        client = MagicMock()
        client.is_credential_valid.side_effect = RuntimeError("boom")
        self.assertFalse(KplSearchProvider(client).is_available)

    def test_all_provider_loops_skip_preference_only(self) -> None:
        """回归：真实分析里 KPL 被通用「搜索股票新闻」路径当普通引擎调用过。

        那条路径不传 stock_code，KPL 只能报「仅支持 A 股 6 位代码，收到 None」。
        所有遍历 self._providers 的地方都必须跳过 preference_only，而不只是
        多维度检索那一处。
        """
        import inspect
        src = inspect.getsource(SearchService)
        loops = src.count("for provider in self._providers:")
        guards = src.count('getattr(provider, "preference_only", False)')
        self.assertEqual(guards, loops, "存在未加 preference_only 守卫的 provider 遍历")

    def test_preference_guard_tolerates_ducktyped_providers(self) -> None:
        """既有测试用 SimpleNamespace 造 provider，属性访问必须用 getattr。"""
        import inspect
        src = inspect.getsource(SearchService)
        self.assertNotIn("provider.preference_only", src)

    def test_key_health_circuit_breaker_bypassed(self) -> None:
        """KPL 的失败多是「本次不适用」，不该累积成 key 熔断。

        基类同一 key 累计 3 次错误就停止发放，会让 KPL 被永久禁用；它只有一个
        占位 key，真实可用性由凭证探针负责。
        """
        p = _provider(ANNOUNCEMENTS)
        for _ in range(5):
            p._record_error("local")
        self.assertEqual(p._get_next_key(), "local")
        r = p.search("q", max_results=2, stock_code="600519", dimension="announcements")
        self.assertTrue(r.success, "多次错误后仍应能正常取数")


class TestAnnouncementsDimension(unittest.TestCase):
    """公告维度 —— 替换 Bocha 的核心目标。"""

    def test_dimension_now_prefers_kpl(self) -> None:
        """回归保护：该维度原先写死 'bocha'，Bocha 到期即归零。"""
        import inspect
        src = inspect.getsource(SearchService)
        self.assertIn("'provider_preference': 'kpl'", src)
        self.assertNotIn("'provider_preference': 'bocha'", src)

    def test_announcements_mapped_with_dates_and_pdf(self) -> None:
        r = _provider(ANNOUNCEMENTS).search(
            "q", max_results=5, stock_code="600519", dimension="announcements")
        self.assertTrue(r.success)
        self.assertEqual(len(r.results), 2)
        first = r.results[0]
        self.assertEqual(first.title, "贵州茅台重大事项公告")
        self.assertEqual(first.url, "https://example.com/a.pdf")
        self.assertEqual(first.source, "开盘啦·公告")

    def test_every_announcement_carries_published_date(self) -> None:
        """strict_freshness 会丢弃没有日期的结果，缺日期等于该维度归零。"""
        r = _provider(ANNOUNCEMENTS).search(
            "q", max_results=5, stock_code="600519", dimension="announcements")
        for item in r.results:
            self.assertIsNotNone(item.published_date, f"{item.title} 缺日期")

    def test_respects_max_results(self) -> None:
        r = _provider(ANNOUNCEMENTS).search(
            "q", max_results=1, stock_code="600519", dimension="announcements")
        self.assertEqual(len(r.results), 1)

    def test_blank_titles_skipped(self) -> None:
        payload = {"items": [{"title": "  ", "timestamp": 1784000000}]}
        r = _provider(payload).search(
            "q", max_results=5, stock_code="600519", dimension="announcements")
        self.assertFalse(r.success)


class TestLatestNewsDimension(unittest.TestCase):
    def test_market_roundups_filtered_out(self) -> None:
        """全市场汇总条会罗列几十只无关股票，喂给 LLM 会稀释相关性。"""
        r = _provider(FLASHES).search(
            "q", max_results=5, stock_code="600519", dimension="latest_news")
        titles = " ".join(x.title for x in r.results)
        self.assertNotIn("公告精选", titles)
        self.assertNotIn("避雷针", titles)
        self.assertEqual(len(r.results), 1)


class TestKplProviderGuards(unittest.TestCase):
    def test_non_a_share_code_rejected(self) -> None:
        for bad in ("AAPL", "00700", None, ""):
            r = _provider(ANNOUNCEMENTS).search(
                "q", max_results=5, stock_code=bad, dimension="announcements")
            self.assertFalse(r.success, f"应拒绝 {bad!r}")

    def test_unsupported_dimension_rejected(self) -> None:
        """market_analysis 需要研报目标价/深度分析，KPL 覆盖不了。"""
        for dim in ("market_analysis", "risk_check", "earnings", "industry", None):
            r = _provider(ANNOUNCEMENTS).search(
                "q", max_results=5, stock_code="600519", dimension=dim)
            self.assertFalse(r.success, f"不应服务 {dim!r}")

    def test_upstream_error_becomes_failed_response(self) -> None:
        """异常必须转成 success=False 的响应，由上层继续降级。"""
        r = _provider(side_effect=RuntimeError("boom")).search(
            "q", max_results=5, stock_code="600519", dimension="announcements")
        self.assertFalse(r.success)
        self.assertIsNotNone(r.error_message)


class TestKplSearchHelpers(unittest.TestCase):
    def test_stock_code_normalization(self) -> None:
        self.assertEqual(_normalize_kpl_stock_code("600519"), "600519")
        self.assertEqual(_normalize_kpl_stock_code("sh600519"), "600519")
        for bad in (None, "", "AAPL", "00700", "1234567"):
            self.assertIsNone(_normalize_kpl_stock_code(bad), f"应拒绝 {bad!r}")

    def test_epoch_to_date(self) -> None:
        self.assertIsNotNone(_kpl_epoch_to_date(1784000000))
        self.assertEqual(len(_kpl_epoch_to_date(1784000000)), 10)
        for bad in (None, "", 0, -1, "abc"):
            self.assertIsNone(_kpl_epoch_to_date(bad), f"应拒绝 {bad!r}")

    def test_roundup_detection(self) -> None:
        self.assertTrue(_is_kpl_market_roundup("【公告精选：xxx】"))
        self.assertTrue(_is_kpl_market_roundup("【7月22日股市避雷针】"))
        self.assertFalse(_is_kpl_market_roundup("贵州茅台发布回购公告"))


if __name__ == "__main__":
    unittest.main()
