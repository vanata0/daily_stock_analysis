# -*- coding: utf-8 -*-
"""
KPL (开盘啦) HTTP 客户端
=======================

对接本机常驻的 kpl-unified-client HTTP 服务（默认 http://127.0.0.1:8010），
为 KplFetcher 提供统一的请求、错误映射与**凭证失效探针**。

为什么需要凭证探针
------------------
KPL 的 UserID/Token/DeviceID 来自抓包，会过期。过期后上游**不报错**，而是
所有接口返回 HTTP 200 + errcode=0 + 空数组。对无人值守的定时分析来说，这种
静默失效会直接产出垃圾报告。

本模块用「金丝雀端点 + 语义非空断言」把静默失效转成显式不可用：
  - 探针端点 /market-stats/mood-num-count
  - 判据 rise_count == 0 且 fall_count == 0
    （A 股任一交易日两者之和都在 4000 以上，同时为 0 只可能是凭证失效或上游故障）
  - 非交易日不做判定（休市本就没有涨跌家数），避免把节假日误判成失效

探测结果按 TTL 缓存，不会每次取数都打一次探针。

上游错误码约定（见 kpl-unified-client/docs/howto-handle-errors-and-rate-limits.md）：
  401 KPLAuthError    —— 凭证缺失，服务启动期就会 fail fast，运行期理论上不出现
  429 本地限流桶拦截
  502 upstream_error  —— errcode != 0（最常见 1020 参数错误），与凭证无关
  503 network_error   —— 网络/超时/非 2xx
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "http://127.0.0.1:8010"
DEFAULT_TIMEOUT = 10

# 凭证探针结果缓存时长；探针本身只有一次轻量请求，5 分钟足以在
# 凭证过期后较快暴露，又不会给每次取数都增加往返。
_PROBE_TTL_SECONDS = 300

# 探针判据：A 股任一交易日 rise_count + fall_count 都远超此值，
# 低于它说明拿到的是空数据而非真实行情。
_MIN_TRADING_BREADTH = 100


class KplError(Exception):
    """KPL 请求失败的基类。"""


class KplRequestError(KplError):
    """网络不可达、超时或上游返回非 2xx。"""


class KplRateLimitError(KplError):
    """被 kpl 服务的本地限流桶拦截（HTTP 429）。"""


class KplCredentialExpiredError(KplError):
    """探针判定凭证已失效（接口静默返回空数据）。"""


class KplHttpClient:
    """kpl-unified-client HTTP 服务的轻量客户端（线程安全）。"""

    def __init__(
        self,
        base_url: str = DEFAULT_API_BASE,
        timeout: int = DEFAULT_TIMEOUT,
        probe_ttl_seconds: int = _PROBE_TTL_SECONDS,
    ) -> None:
        self._base_url = (base_url or DEFAULT_API_BASE).rstrip("/")
        self._timeout = timeout if timeout and timeout > 0 else DEFAULT_TIMEOUT
        self._probe_ttl = probe_ttl_seconds
        # requests.Session 复用连接；DSA 是多线程取数，Session 本身线程安全，
        # 但探针缓存需要自己加锁。
        self._session = requests.Session()
        self._lock = threading.Lock()
        self._probe_ok: Optional[bool] = None
        self._probe_at: float = 0.0
        self._holiday_cache: Optional[set] = None

    @property
    def base_url(self) -> str:
        return self._base_url

    # ------------------------------------------------------------------
    # 基础请求
    # ------------------------------------------------------------------

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """发起 GET 请求并返回解析后的 JSON。

        失败一律抛异常（不返回 None），由调用方决定降级，避免把失败
        伪装成空数据继续往下游传。
        """
        url = f"{self._base_url}/{path.lstrip('/')}"
        try:
            resp = self._session.get(url, params=params, timeout=self._timeout)
        except requests.exceptions.RequestException as exc:
            raise KplRequestError(f"KPL 请求失败 {path}: {exc}") from exc

        if resp.status_code == 429:
            raise KplRateLimitError(f"KPL 限流 {path}: {resp.text[:120]}")
        if resp.status_code != 200:
            raise KplRequestError(
                f"KPL HTTP {resp.status_code} {path}: {resp.text[:160]}"
            )

        try:
            return resp.json()
        except ValueError as exc:
            raise KplRequestError(f"KPL 返回非 JSON {path}: {resp.text[:120]}") from exc

    # ------------------------------------------------------------------
    # 凭证失效探针
    # ------------------------------------------------------------------

    def is_credential_valid(self, force: bool = False) -> bool:
        """探测凭证是否仍然有效（结果按 TTL 缓存）。

        Args:
            force: 跳过缓存强制重新探测

        Returns:
            True 表示可用；False 表示判定为凭证失效或上游不可达
        """
        now = time.time()
        with self._lock:
            if (
                not force
                and self._probe_ok is not None
                and (now - self._probe_at) < self._probe_ttl
            ):
                return self._probe_ok

        ok = self._run_probe()

        with self._lock:
            self._probe_ok = ok
            self._probe_at = time.time()
        return ok

    def _run_probe(self) -> bool:
        try:
            data = self.get("/market-stats/mood-num-count")
        except KplRateLimitError as exc:
            # 限流不代表凭证失效，保持可用并让调用方自行重试
            logger.warning("[KPL] 探针被限流，暂按可用处理: %s", exc)
            return True
        except KplError as exc:
            logger.error("[KPL] 探针请求失败，判定不可用: %s", exc)
            return False

        rise = _as_int(data.get("rise_count"))
        fall = _as_int(data.get("fall_count"))
        breadth = rise + fall

        if breadth >= _MIN_TRADING_BREADTH:
            logger.debug("[KPL] 凭证探针通过 (rise=%d fall=%d)", rise, fall)
            return True

        # 空数据：先排除非交易日，避免把休市误判成凭证失效
        if not self._is_trading_day():
            logger.info(
                "[KPL] 探针返回空数据但今日非交易日，不判定为凭证失效 "
                "(rise=%d fall=%d)", rise, fall,
            )
            return True

        logger.error(
            "[KPL] 凭证疑似失效：交易日探针涨跌家数为空 (rise=%d fall=%d)。"
            "上游接口在凭证过期时会静默返回空数据，请重新抓包更新 kpl-unified-client 的 .env",
            rise, fall,
        )
        return False

    def _is_trading_day(self, day: Optional[date] = None) -> bool:
        """判断是否 A 股交易日（周末 + KPL 节假日表）。"""
        day = day or date.today()
        if day.weekday() >= 5:
            return False

        holidays = self._get_holidays()
        if holidays is None:
            # 拿不到节假日表时保守认为是交易日，让空数据继续触发失效告警，
            # 避免因为节假日表获取失败而掩盖真实的凭证过期。
            return True
        return day.isoformat() not in holidays

    def _get_holidays(self) -> Optional[set]:
        with self._lock:
            if self._holiday_cache is not None:
                return self._holiday_cache

        try:
            data = self.get("/market-stats/holiday")
        except KplError as exc:
            logger.warning("[KPL] 获取节假日表失败: %s", exc)
            return None

        dates = data.get("dates")
        if not isinstance(dates, list):
            logger.warning("[KPL] 节假日表格式异常: %r", type(dates).__name__)
            return None

        parsed = {str(d)[:10] for d in dates if d}
        with self._lock:
            self._holiday_cache = parsed
        return parsed

    def reset_probe_cache(self) -> None:
        """清空探针与节假日缓存（供测试与配置变更后调用）。"""
        with self._lock:
            self._probe_ok = None
            self._probe_at = 0.0
            self._holiday_cache = None

    def close(self) -> None:
        try:
            self._session.close()
        except Exception as exc:  # pragma: no cover - 关闭失败不影响主流程
            logger.debug("[KPL] 关闭 session 异常: %s", exc)


def _as_int(value: Any) -> int:
    """把上游可能给出的 str/float/None 统一成 int，无法解析按 0 处理。"""
    if value is None:
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def kpl_date_to_iso(value: Any) -> Optional[str]:
    """KPL 的 ``YYYYMMDD`` 日期转 ``YYYY-MM-DD``；无法解析返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "-" in text:
        return text[:10]
    if len(text) == 8 and text.isdigit():
        try:
            return datetime.strptime(text, "%Y%m%d").date().isoformat()
        except ValueError:
            return None
    return None
