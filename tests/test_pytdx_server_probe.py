# -*- coding: utf-8 -*-
"""Pytdx 选服：连接成功不等于服务器可用。

通达信公开服务器里存在「僵尸节点」——TCP 与协议握手都正常，但任何 K 线
查询都返回空列表。2026-07-27 实测 6 台配置中 123.125.108.14 /
124.71.187.122 / 110.41.147.114 都是这种，只有 180.153.18.170 真正可用。

旧实现以 ``api.connect()`` 的返回值为唯一判据、成功即 break，于是永远锁死
在列表里第一个僵尸节点上，真正可用的节点排在后面也轮不到，对外表现是
「Pytdx 未查询到 <code> 的数据」——看起来像标的问题，实为选错了服务器。
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

from data_provider.base import DataFetchError
from data_provider.pytdx_fetcher import PytdxFetcher

ZOMBIE = ("1.1.1.1", 7709)
HEALTHY = ("2.2.2.2", 7709)
REFUSING = ("3.3.3.3", 7709)


def _api_factory(behaviour):
    """behaviour: {host: "healthy" | "zombie" | "refuse"}"""
    api = MagicMock()
    state = {"host": None}

    def connect(host, port, time_out=5):
        state["host"] = host
        return behaviour.get(host) != "refuse"

    def get_security_bars(*args, **kwargs):
        return [{"close": 1.0}] if behaviour.get(state["host"]) == "healthy" else []

    api.connect.side_effect = connect
    api.get_security_bars.side_effect = get_security_bars
    api.selected_host = state
    return api


class TestPytdxServerProbe(unittest.TestCase):
    def _run_session(self, hosts, behaviour):
        f = PytdxFetcher(hosts=hosts)
        api = _api_factory(behaviour)
        with patch.object(f, "_get_pytdx", return_value=lambda: api):
            with f._pytdx_session() as session:
                self.assertIs(session, api)
        return f, api

    def test_skips_zombie_and_picks_healthy(self) -> None:
        """僵尸节点排在前面时必须继续往后找，而不是锁死在它身上。"""
        f, api = self._run_session(
            [ZOMBIE, HEALTHY], {ZOMBIE[0]: "zombie", HEALTHY[0]: "healthy"}
        )
        self.assertEqual(f._hosts[f._current_host_idx], HEALTHY)

    def test_skips_multiple_zombies(self) -> None:
        f, _ = self._run_session(
            [ZOMBIE, REFUSING, ("4.4.4.4", 7709), HEALTHY],
            {ZOMBIE[0]: "zombie", REFUSING[0]: "refuse",
             "4.4.4.4": "zombie", HEALTHY[0]: "healthy"},
        )
        self.assertEqual(f._hosts[f._current_host_idx], HEALTHY)

    def test_all_zombies_raises_instead_of_silent_empty(self) -> None:
        """全是僵尸时必须报错让上层降级，不能连上后返回空数据。"""
        f = PytdxFetcher(hosts=[ZOMBIE, ("4.4.4.4", 7709)])
        api = _api_factory({ZOMBIE[0]: "zombie", "4.4.4.4": "zombie"})
        with patch.object(f, "_get_pytdx", return_value=lambda: api):
            with self.assertRaises(DataFetchError):
                with f._pytdx_session():
                    pass

    def test_healthy_first_short_circuits(self) -> None:
        """首台即健康时不应多探后面的服务器。"""
        f, api = self._run_session(
            [HEALTHY, ZOMBIE], {HEALTHY[0]: "healthy", ZOMBIE[0]: "zombie"}
        )
        self.assertEqual(f._hosts[f._current_host_idx], HEALTHY)
        self.assertEqual(api.connect.call_count, 1)

    def test_probe_exception_treated_as_unusable(self) -> None:
        """探针本身抛异常时按不可用处理，宁可多试一台。"""
        f = PytdxFetcher(hosts=[ZOMBIE, HEALTHY])
        api = _api_factory({ZOMBIE[0]: "zombie", HEALTHY[0]: "healthy"})

        calls = {"n": 0}
        original = api.get_security_bars.side_effect

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("protocol error")
            return original(*args, **kwargs)

        api.get_security_bars.side_effect = flaky
        with patch.object(f, "_get_pytdx", return_value=lambda: api):
            with f._pytdx_session():
                pass
        self.assertEqual(f._hosts[f._current_host_idx], HEALTHY)


if __name__ == "__main__":
    unittest.main()
