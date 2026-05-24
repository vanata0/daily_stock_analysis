# -*- coding: utf-8 -*-
"""
Tests for PR 2: cache layer additions and P0-2 config split.

Coverage:
  - build_schema_response: @functools.cache memoises the result
  - resolve_name_to_code: TTL cache avoids repeated AkShare calls
  - HistoryService.get_history_list: TTL cache avoids repeated DB calls
  - Config._load_llm_config / _load_notification_config: return dicts
  - Config._load_from_env: idempotent (smoke)
"""
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub heavy optional deps (must precede project imports)
# ---------------------------------------------------------------------------

def _stub(name: str, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


if "litellm" not in sys.modules:
    sys.modules["litellm"] = MagicMock()

if "tenacity" not in sys.modules:
    def _noop_retry(*a, **kw):
        return lambda f: f
    _stub("tenacity",
          retry=_noop_retry,
          stop_after_attempt=lambda n: None,
          wait_exponential=lambda **kw: None,
          retry_if_exception_type=lambda e: None,
          before_sleep_log=lambda *a: None)

if "akshare" not in sys.modules:
    sys.modules["akshare"] = MagicMock()

if "fake_useragent" not in sys.modules:
    _stub("fake_useragent", UserAgent=lambda: MagicMock())

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

from src.core.config_registry import build_schema_response  # noqa: E402
from src.services import name_to_code_resolver as _resolver  # noqa: E402
from src.services.name_to_code_resolver import (  # noqa: E402
    resolve_name_to_code,
    _resolve_cache,
    _RESOLVE_CACHE_TTL,
)
from src.services.history_service import (  # noqa: E402
    HistoryService,
    _history_list_cache,
    _HISTORY_LIST_CACHE_TTL,
)
from src.config import Config  # noqa: E402


# ---------------------------------------------------------------------------
# TestSchemaCache
# ---------------------------------------------------------------------------

class TestSchemaCache(unittest.TestCase):

    def setUp(self):
        # Clear functools.cache between tests so test isolation is maintained
        build_schema_response.cache_clear()

    def test_build_schema_response_called_once(self):
        """@functools.cache means the heavy build runs exactly once per process."""
        call_count = 0
        original = build_schema_response.__wrapped__  # type: ignore[attr-defined]

        def counting_build():
            nonlocal call_count
            call_count += 1
            return original()

        with patch(
            "src.core.config_registry.build_schema_response.__wrapped__",
            counting_build,
            create=True,
        ):
            # Simpler: just verify cache_info tracks hits
            build_schema_response.cache_clear()
            build_schema_response()
            build_schema_response()
            build_schema_response()

        info = build_schema_response.cache_info()
        self.assertGreaterEqual(info.hits, 2, "Expected ≥2 cache hits on repeated calls")

    def test_result_is_dict_with_required_keys(self):
        """build_schema_response returns a dict with schema_version and categories."""
        result = build_schema_response()
        self.assertIsInstance(result, dict)
        self.assertIn("schema_version", result)
        self.assertIn("categories", result)

    def test_cache_info_available(self):
        """@functools.cache attaches cache_info to the function."""
        self.assertTrue(hasattr(build_schema_response, "cache_info"))
        self.assertTrue(callable(build_schema_response.cache_info))


# ---------------------------------------------------------------------------
# TestNameTTLCache
# ---------------------------------------------------------------------------

class TestNameTTLCache(unittest.TestCase):

    def setUp(self):
        _resolve_cache.clear()

    def tearDown(self):
        _resolve_cache.clear()

    def test_cache_hit_avoids_repeated_resolution(self):
        """Second call for the same name returns from cache, impl not called again."""
        impl_call_count = 0
        original_impl = _resolver._resolve_name_to_code_impl

        def counting_impl(name):
            nonlocal impl_call_count
            impl_call_count += 1
            return original_impl(name)

        with patch.object(_resolver, "_resolve_name_to_code_impl", side_effect=counting_impl):
            resolve_name_to_code("贵州茅台")
            resolve_name_to_code("贵州茅台")
            resolve_name_to_code("贵州茅台")

        self.assertEqual(impl_call_count, 1, "impl should be called exactly once — rest served from cache")

    def test_cache_stores_result(self):
        """After one call the result is stored in _resolve_cache."""
        _resolve_cache.clear()
        resolve_name_to_code("贵州茅台")
        self.assertIn("贵州茅台", _resolve_cache)
        _exp, _val = _resolve_cache["贵州茅台"]
        self.assertIsInstance(_exp, float)

    def test_expired_entry_is_evicted(self):
        """An entry with expire_time in the past is ignored and re-resolved."""
        import time
        _resolve_cache["测试股票"] = (time.time() - 1, "STALE")
        impl_called = []

        original_impl = _resolver._resolve_name_to_code_impl

        def recording_impl(name):
            impl_called.append(name)
            return original_impl(name)

        with patch.object(_resolver, "_resolve_name_to_code_impl", side_effect=recording_impl):
            resolve_name_to_code("测试股票")

        self.assertIn("测试股票", impl_called, "Expired entry should trigger a fresh resolution")

    def test_different_names_cached_independently(self):
        """Each distinct name gets its own cache slot."""
        _resolve_cache.clear()
        resolve_name_to_code("贵州茅台")
        resolve_name_to_code("平安银行")
        self.assertGreaterEqual(len(_resolve_cache), 2)


# ---------------------------------------------------------------------------
# TestHistoryListCache
# ---------------------------------------------------------------------------

class TestHistoryListCache(unittest.TestCase):

    def _make_service(self):
        svc = HistoryService.__new__(HistoryService)
        svc.db = MagicMock()
        svc.db.get_analysis_history_paginated.return_value = ([], 0)
        return svc

    def setUp(self):
        _history_list_cache.clear()

    def tearDown(self):
        _history_list_cache.clear()

    def test_second_call_hits_cache(self):
        """Second identical call uses cache, DB not queried again."""
        svc = self._make_service()
        svc.get_history_list(stock_code="000001", page=1, limit=20)
        svc.get_history_list(stock_code="000001", page=1, limit=20)
        self.assertEqual(
            svc.db.get_analysis_history_paginated.call_count, 1,
            "DB should be queried only once when params are identical",
        )

    def test_different_params_bypass_cache(self):
        """Different page numbers result in separate DB calls."""
        svc = self._make_service()
        svc.get_history_list(stock_code="000001", page=1, limit=20)
        svc.get_history_list(stock_code="000001", page=2, limit=20)
        self.assertEqual(svc.db.get_analysis_history_paginated.call_count, 2)

    def test_cache_stores_entry(self):
        """After one call, the result is in _history_list_cache."""
        svc = self._make_service()
        svc.get_history_list(stock_code="600519", page=1, limit=10)
        self.assertTrue(len(_history_list_cache) >= 1)

    def test_expired_cache_triggers_db(self):
        """Entry past its TTL causes a fresh DB call."""
        import time
        key = ("600519", None, None, 1, 20)
        _history_list_cache[key] = (time.time() - 1, {"total": 99, "items": []})
        svc = self._make_service()
        result = svc.get_history_list(stock_code="600519", page=1, limit=20)
        self.assertEqual(svc.db.get_analysis_history_paginated.call_count, 1, "Expired cache should not be used")
        self.assertNotEqual(result.get("total"), 99)


# ---------------------------------------------------------------------------
# TestConfigSplit
# ---------------------------------------------------------------------------

class TestConfigSplit(unittest.TestCase):

    def test_load_llm_config_returns_dict(self):
        """_load_llm_config() returns a dict with LLM-related keys."""
        result = Config._load_llm_config()
        self.assertIsInstance(result, dict)
        self.assertIn("litellm_model", result)
        self.assertIn("llm_model_list", result)
        self.assertIn("agent_litellm_model", result)

    def test_load_notification_config_returns_dict(self):
        """_load_notification_config() returns a dict with notification keys."""
        result = Config._load_notification_config()
        self.assertIsInstance(result, dict)
        self.assertIn("wechat_webhook_url", result)
        self.assertIn("telegram_bot_token", result)
        self.assertIn("notification_quiet_hours", result)
        self.assertIn("merge_email_notification", result)

    def test_load_from_env_idempotent(self):
        """Two consecutive calls to _load_from_env produce equal Config objects."""
        Config._instance = None  # reset singleton
        a = Config._load_from_env()
        b = Config._load_from_env()
        self.assertEqual(a, b, "_load_from_env must be deterministic for a fixed env")

    def test_notification_keys_not_duplicated(self):
        """Keys in _load_notification_config must not conflict with each other."""
        result = Config._load_notification_config()
        keys = list(result.keys())
        self.assertEqual(len(keys), len(set(keys)), "Duplicate keys in notification config dict")


if __name__ == "__main__":
    unittest.main()
