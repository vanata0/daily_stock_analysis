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

探针端点 GET /health/credential（kpl-unified-client 专为下游探测开的健康检查，
不经过任何业务 Response Model）：
  - 200 可用
  - 401 凭证问题（上游真打了一次最轻接口，拿到的关键字段全空）
  - 503 上游不可达
  - 401 时仍需排除两种误报窗口，逻辑与旧探针一致：非交易日不做判定（休市本就
    没有涨跌家数）；交易日 09:30 之前同样不做判定（上游会在早盘前把上一交易日
    的涨跌家数清零，此时全 0 是正常状态）——漏掉这段会让每个交易日开盘前 KPL
    全线不可用并误发告警

  ⚠️ 之前的探针打 /market-stats/mood-num-count 这个业务端点，2026-08-11 17:52~
  08-12 01:15 该端点因响应模型必填了一个客户端已改名的字段而稳定 500，
  is_available() 被拖到全线 False，KPL 所有接口一起短路回落到其它数据源
  （见 kpl-unified-client commit 908ac80/bb2ff66）。/health/credential 不经过
  业务模型，字段语义订正不会再影响这条探针。

探测结果按 TTL 缓存，不会每次取数都打一次探针。

上游错误码约定（见 kpl-unified-client/docs/howto-handle-errors-and-rate-limits.md，
适用于本模块 get() 打的其它业务端点，不适用于 /health/credential 自身的三档状态码）：
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
from typing import Any, Callable, Dict, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "http://127.0.0.1:8010"
DEFAULT_TIMEOUT = 10

# 凭证探针结果缓存时长；探针本身只有一次轻量请求，5 分钟足以在
# 凭证过期后较快暴露，又不会给每次取数都增加往返。
_PROBE_TTL_SECONDS = 300

# A 股连续竞价开始时刻。此前涨跌家数为 0 属正常（集合竞价 09:25 才出结果，
# 上游在早盘前会把上一交易日的数值清零），不能据此判定凭证失效。
_MARKET_OPEN_HHMM = (9, 30)


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
        on_credential_expired: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        Args:
            on_credential_expired: 探针判定凭证失效时回调，入参为可读原因。
                本类只负责判定与触发，具体怎么告警由上层决定，避免 transport
                依赖通知栈。回调异常不会影响探针结论。
        """
        self._base_url = (base_url or DEFAULT_API_BASE).rstrip("/")
        self._timeout = timeout if timeout and timeout > 0 else DEFAULT_TIMEOUT
        self._probe_ttl = probe_ttl_seconds
        self._on_credential_expired = on_credential_expired
        # requests.Session 承载公共 header 与重试配置；DSA 是多线程取数，
        # Session 本身线程安全，但探针缓存需要自己加锁。
        self._session = requests.Session()
        # 显式关闭连接复用：KPL 服务是本机回环（127.0.0.1），keep-alive 省下的
        # 握手成本可忽略；而上游 uvicorn 的 keep-alive 超时会主动 FIN 掉空闲连接，
        # urllib3 连接池感知不到，socket 会停在 CLOSE-WAIT 直到该槽位被再次复用。
        # 实测 2026-08-18 一波 502 之后 9 条连接滞留了近 8 小时。
        self._session.headers["Connection"] = "close"
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
        url = f"{self._base_url}/health/credential"
        try:
            resp = self._session.get(url, timeout=self._timeout)
        except requests.exceptions.RequestException as exc:
            logger.error("[KPL] 探针请求失败，判定不可用: %s", exc)
            return False

        if resp.status_code == 200:
            logger.debug("[KPL] 凭证探针通过")
            return True

        if resp.status_code == 429:
            # 限流不代表凭证失效，保持可用并让调用方自行重试
            logger.warning("[KPL] 探针被限流，暂按可用处理: %s", resp.text[:120])
            return True

        if resp.status_code == 503:
            # 上游不可达，与凭证无关，不触发凭证失效告警
            logger.warning("[KPL] 探针判定上游不可达: %s", resp.text[:160])
            return False

        if resp.status_code != 401:
            logger.error(
                "[KPL] 探针返回异常状态码 %s: %s", resp.status_code, resp.text[:160]
            )
            return False

        # 401：探针认为拿不到数据，先排除非交易日，避免把休市误判成凭证失效
        if not self._is_trading_day():
            logger.info("[KPL] 探针返回 401 但今日非交易日，不判定为凭证失效")
            return True

        # 再排除交易日的开盘前时段：上游会在当天早盘前把上一交易日的涨跌家数
        # 清零，此时全空是正常状态而非凭证失效。实测 2026-07-28 09:01
        # （交易日、未开盘）该端点返回 rise=0/fall=0 且 errcode='0'，凭证完全
        # 有效。漏掉这一段会让每个交易日 9:30 之前 KPL 全线不可用并误发告警。
        if self._is_before_market_open():
            logger.info("[KPL] 探针返回 401 但尚未开盘，不判定为凭证失效")
            return True

        reason = (
            f"探针判定凭证失效：{resp.text[:160]}。"
            "请重新抓包更新 kpl-unified-client 的 .env"
        )
        logger.error("[KPL] 凭证疑似失效：%s", reason)
        self._fire_credential_expired(reason)
        return False

    def _fire_credential_expired(self, reason: str) -> None:
        """通知上层凭证已失效。

        只在「上一次判定为可用」时触发，避免探针 TTL 每 5 分钟到期后
        反复告警；回调本身异常一律吞掉并记日志，不能影响探针结论。
        """
        callback = self._on_credential_expired
        if callback is None:
            return
        with self._lock:
            was_ok = self._probe_ok
        if was_ok is False:
            return  # 已经处于失效态，不重复告警
        try:
            callback(reason)
        except Exception as exc:
            logger.warning("[KPL] 凭证失效回调执行失败: %s", exc)

    def _is_before_market_open(self, now: Optional[datetime] = None) -> bool:
        """当前是否处于交易日的开盘前时段（A 股 09:30 连续竞价开始之前）。

        涨跌家数要到集合竞价结束（09:25）后才有意义，上游则在早盘前就把上一
        交易日的数值清零，因此 09:30 之前的全 0 不能作为凭证失效的判据。
        """
        now = now or datetime.now()
        return (now.hour, now.minute) < _MARKET_OPEN_HHMM

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
