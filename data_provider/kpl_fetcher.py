# -*- coding: utf-8 -*-
"""
KplFetcher —— 开盘啦（KPL）数据源
================================

通过本机常驻的 kpl-unified-client HTTP 服务获取 A 股数据，作为 Tushare
代理站下线后的主力替代源。

优先级：由 ``KPL_PRIORITY`` 控制，默认 -1（启用后排在所有数据源最前）。

配置（.env）：
  KPL_ENABLED   = false                    是否启用；关闭时本 fetcher 完全不实例化
  KPL_API_BASE  = http://127.0.0.1:8010    kpl-unified-client 服务地址
  KPL_PRIORITY  = -1                       数据源优先级，越小越先尝试
  KPL_TIMEOUT   = 10                       单次请求超时秒数

不可用条件（任一满足即整体退出调用链，回落到其它数据源）：
  - KPL_ENABLED 未开启
  - 服务不可达
  - 凭证失效探针判定失败（详见 kpl_http.KplHttpClient.is_credential_valid）

日线口径说明：
  上游 /kline/daily/{id} 无参数、返回全量序列且**已前复权**，可直接用于技术
  指标计算；但不提供原始未复权价。volume 按「手」计，本模块按 DSA 标准换算
  为「股」；amount 已是「元」，不做换算。上游不返回涨跌幅，由收盘价推导。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import pandas as pd

from .base import (
    BaseFetcher,
    DataFetchError,
    STANDARD_COLUMNS,
    normalize_stock_code,
)
from .kpl_http import KplError, KplHttpClient, kpl_date_to_iso

logger = logging.getLogger(__name__)

# 上游 volume 按「手」计，DSA 标准列按「股」
_HAND_TO_SHARE = 100


class KplFetcher(BaseFetcher):
    """开盘啦数据源（仅 A 股）。"""

    name = "KplFetcher"
    priority = -1

    def __init__(
        self,
        api_base: Optional[str] = None,
        timeout: Optional[int] = None,
        priority: Optional[int] = None,
        client: Optional[KplHttpClient] = None,
    ) -> None:
        """构造 KplFetcher。

        Args:
            api_base: 服务地址；缺省读取全局配置
            timeout: 单次请求超时秒数；缺省读取全局配置
            priority: 数据源优先级；缺省读取全局配置
            client: 注入自定义 HTTP 客户端（测试用）
        """
        config = self._safe_config()

        resolved_base = api_base or getattr(config, "kpl_api_base", None)
        resolved_timeout = timeout if timeout is not None else getattr(config, "kpl_timeout", None)
        resolved_priority = priority if priority is not None else getattr(config, "kpl_priority", None)

        self._client = client or KplHttpClient(
            base_url=resolved_base or "http://127.0.0.1:8010",
            timeout=resolved_timeout or 10,
        )
        self.priority = resolved_priority if resolved_priority is not None else -1

    @staticmethod
    def _safe_config() -> Any:
        """读取全局配置；失败时返回 None，由调用方走默认值。"""
        try:
            from src.config import get_config

            return get_config()
        except Exception as exc:  # pragma: no cover - 配置缺失时不应阻断构造
            logger.debug("[KPL] 读取全局配置失败，改用默认值: %s", exc)
            return None

    # ------------------------------------------------------------------
    # 可用性
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """数据源是否可用。

        必须是普通方法而不是 property —— DataFetcherManager 的可用性探测用
        ``callable()`` 判定，写成 property 会让这里的检查被静默跳过。
        """
        return self._client.is_credential_valid()

    # ------------------------------------------------------------------
    # BaseFetcher 实现：日线
    # ------------------------------------------------------------------

    def _fetch_raw_data(
        self, stock_code: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        if not self.is_available():
            raise DataFetchError("KplFetcher: 数据源不可用（凭证探针未通过或服务不可达）")

        code = normalize_stock_code(stock_code)
        if not code or not code.isdigit() or len(code) != 6:
            raise DataFetchError(f"KplFetcher: 仅支持 A 股 6 位代码，收到 '{stock_code}'")

        try:
            payload = self._client.get(f"/kline/daily/{code}")
        except KplError as exc:
            raise DataFetchError(f"KplFetcher: 获取日线失败 ({stock_code}): {exc}") from exc

        days: List[Dict[str, Any]] = payload.get("days") or []
        if not days:
            raise DataFetchError(f"KplFetcher: {stock_code} 无日线数据")

        df = pd.DataFrame(days)
        df["date"] = df["date"].map(kpl_date_to_iso)
        df = df[df["date"].notna()]
        if df.empty:
            raise DataFetchError(f"KplFetcher: {stock_code} 日线日期全部无法解析")

        # 上游返回全量序列且不支持日期参数，这里按请求区间裁剪
        df = df[(df["date"] >= start_date[:10]) & (df["date"] <= end_date[:10])].copy()
        if df.empty:
            raise DataFetchError(
                f"KplFetcher: {stock_code} 在 {start_date[:10]}~{end_date[:10]} 无日线数据"
            )

        logger.debug(
            "[KplFetcher] %s: %d 行 (%s~%s)",
            stock_code, len(df), start_date[:10], end_date[:10],
        )
        return df

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """转成 DSA 标准列。

        上游字段：date, open, close, high, low, volume(手), amount(元),
                  turnover_pct, rights_event, state, state1, state_zt
        标准列：  date, open, high, low, close, volume(股), amount(元), pct_chg
        """
        df = df.copy()

        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        if "volume" in df.columns:
            df["volume"] = df["volume"] * _HAND_TO_SHARE

        # 上游不返回涨跌幅，由收盘价推导（首行无前收，保持 NaN 由后续清洗处理）
        if "close" in df.columns:
            df = df.sort_values("date")
            df["pct_chg"] = df["close"].pct_change() * 100

        df["code"] = normalize_stock_code(stock_code)
        keep = ["code"] + [c for c in STANDARD_COLUMNS if c in df.columns]
        return df[keep]

    # ------------------------------------------------------------------
    # 资源释放
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._client.close()
