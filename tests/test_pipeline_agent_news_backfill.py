# -*- coding: utf-8 -*-
"""Agent 模式下 news block 回填：避免误报 news_context_missing。"""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

from src.core.pipeline import StockAnalysisPipeline, _AGENT_NEWS_CONTEXT_TEXT
from src.services.analysis_context_builder import AnalysisContextBuilder
from src.schemas.analysis_context_pack import ContextFieldStatus


class AgentNewsContextTextTest(unittest.TestCase):
    """回填文案只吃 count 一个占位符，改文案时漏参数会在这里炸掉。"""

    def test_every_language_formats_with_count_only(self):
        for lang, template in _AGENT_NEWS_CONTEXT_TEXT.items():
            with self.subTest(lang=lang):
                self.assertIn("8", template.format(count=8))


class AgentNewsBackfillTest(unittest.TestCase):
    def _pipeline(self) -> StockAnalysisPipeline:
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.query_source = "system"
        return pipeline

    def test_missing_without_override(self):
        artifacts = self._pipeline()._build_agent_analysis_artifacts(
            code="002440",
            stock_name="闰土股份",
            market="cn",
            phase=None,
            initial_context={},
            fundamental_context=None,
            query_id="q-1",
        )
        block = AnalysisContextBuilder.build(artifacts).blocks["news"]
        self.assertEqual(block.status, ContextFieldStatus.MISSING)
        self.assertEqual(
            block.items["content"].missing_reason, "news_context_missing"
        )

    def test_override_marks_news_available(self):
        artifacts = self._pipeline()._build_agent_analysis_artifacts(
            code="002440",
            stock_name="闰土股份",
            market="cn",
            phase=None,
            initial_context={},
            fundamental_context=None,
            query_id="q-1",
            news_context_override="Agent 运行中检索到新闻",
            news_result_count=8,
        )
        block = AnalysisContextBuilder.build(artifacts).blocks["news"]
        self.assertEqual(block.status, ContextFieldStatus.AVAILABLE)
        self.assertEqual(block.metadata["news_result_count"], 8)

    def test_prefetched_news_context_wins_when_no_override(self):
        artifacts = self._pipeline()._build_agent_analysis_artifacts(
            code="002440",
            stock_name="闰土股份",
            market="cn",
            phase=None,
            initial_context={"news_context": "预取的东财新闻"},
            fundamental_context=None,
            query_id="q-1",
        )
        self.assertEqual(artifacts.news_context, "预取的东财新闻")


if __name__ == "__main__":
    unittest.main()
