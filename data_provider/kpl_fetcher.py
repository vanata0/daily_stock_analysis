# -*- coding: utf-8 -*-
"""
KplFetcher —— 开盘啦（KPL）数据源
================================

通过本机常驻的 kpl-unified-client HTTP 服务获取 A 股数据，作为 Tushare
代理站下线后的主力替代源。

优先级：由 ``KPL_PRIORITY`` 控制，默认 -2（启用后领先 Tushare 接管 A 股行情）。

配置（.env）：
  KPL_ENABLED   = false                    是否启用；关闭时本 fetcher 完全不实例化
  KPL_API_BASE  = http://127.0.0.1:8010    kpl-unified-client 服务地址
  KPL_PRIORITY  = -2                       数据源优先级，越小越先尝试
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
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .base import (
    BaseFetcher,
    DataFetchError,
    STANDARD_COLUMNS,
    normalize_stock_code,
)
from .kpl_http import KplError, KplHttpClient, kpl_date_to_iso
from .realtime_types import RealtimeSource, UnifiedRealtimeQuote

logger = logging.getLogger(__name__)

# 上游 volume 按「手」计，DSA 标准列按「股」
_HAND_TO_SHARE = 100

# 涨停池按连板数分档查询，实测 5 板及以上通常为空，逐档聚合到此上限
_MAX_CONSECUTIVE_BOARD = 8


class KplFetcher(BaseFetcher):
    """开盘啦数据源（仅 A 股）。"""

    name = "KplFetcher"
    priority = -2

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
            on_credential_expired=_notify_credential_expired,
        )
        self.priority = resolved_priority if resolved_priority is not None else -2

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
    # 实时行情
    # ------------------------------------------------------------------

    def get_realtime_quote(self, stock_code: str, **_kwargs) -> Optional[UnifiedRealtimeQuote]:
        """获取 A 股实时行情。

        数据来自 /orderbook/{id}，是 KPL 单股字段最全的一条：除常规量价外
        还带 PE/PB、总市值/流通市值、涨跌停价与五档盘口。

        Returns:
            UnifiedRealtimeQuote；不可用或数据无效时返回 None（由上层降级）
        """
        if not self.is_available():
            logger.debug("[KplFetcher] 数据源不可用，跳过实时行情 %s", stock_code)
            return None

        code = normalize_stock_code(stock_code)
        if not code or not code.isdigit() or len(code) != 6:
            logger.debug("[KplFetcher] 实时行情仅支持 A 股 6 位代码，收到 %r", stock_code)
            return None

        try:
            data = self._client.get(f"/orderbook/{code}")
        except KplError as exc:
            logger.warning("[KplFetcher] 实时行情获取失败 %s: %s", stock_code, exc)
            return None

        price = _to_float(data.get("last"))
        if price is None or price <= 0:
            logger.debug("[KplFetcher] %s 实时行情无有效价格，跳过", stock_code)
            return None

        # ⚠️ orderbook 没有 volume 字段；实测 amount_wan 并非「成交额(万元)」而是
        # 「成交量(手)」——5 只标的上它与 /kline/daily 的 volume 逐一相等，且
        # turnover/(amount_wan*100) 均落在当日 low~high 内。字段名有误导性。
        volume_hand = _to_float(data.get("amount_wan"))
        quote = UnifiedRealtimeQuote(
            code=code,
            name=str(data.get("name") or "").strip(),
            source=RealtimeSource.KPL,
            market="cn",
            price=price,
            change_pct=_to_float(data.get("change_pct")),
            change_amount=_to_float(data.get("change")),
            volume=int(volume_hand * _HAND_TO_SHARE) if volume_hand else None,
            amount=_to_float(data.get("turnover")),
            volume_ratio=_to_float(data.get("vol_ratio")),
            turnover_rate=_to_float(data.get("turnover_ratio")),
            amplitude=_to_float(data.get("amplitude")),
            open_price=_to_float(data.get("open")),
            high=_to_float(data.get("high")),
            low=_to_float(data.get("low")),
            pre_close=_to_float(data.get("preclose")),
            # 优先 TTM 市盈率，缺失时退回静态市盈率
            pe_ratio=_to_float(data.get("ttm_pe")) or _to_float(data.get("pe")),
            pb_ratio=_to_float(data.get("pb")),
            total_mv=_to_float(data.get("total_mv")),
            circ_mv=_to_float(data.get("circ_mv")),
        )
        logger.debug(
            "[KplFetcher] %s 实时行情: price=%s pe=%s pb=%s",
            stock_code, quote.price, quote.pe_ratio, quote.pb_ratio,
        )
        return quote

    # ------------------------------------------------------------------
    # 个股辅助信息
    # ------------------------------------------------------------------

    def get_stock_name(self, stock_code: str) -> Optional[str]:
        """获取股票名称。"""
        if not self.is_available():
            return None
        code = normalize_stock_code(stock_code)
        if not code or not code.isdigit() or len(code) != 6:
            return None
        try:
            data = self._client.get(f"/orderbook/{code}")
        except KplError as exc:
            logger.warning("[KplFetcher] 获取股票名称失败 %s: %s", stock_code, exc)
            return None
        name = str(data.get("name") or "").strip()
        return name or None

    def get_research_reports(
        self, stock_code: str, max_count: int = 10
    ) -> List[Dict[str, Any]]:
        """获取券商研报列表。

        用 /research/research-field-list（50 条，带标题）而不是
        /research/research-field-excel——后者只有评级分布与 3 条明细且没有
        title，填不满 DSA 的研报条目契约。

        返回结构与 TushareFetcher.get_research_reports 对齐，便于
        research_report_fetcher 的降级链无差别消费；失败返回 []（fail-open）。
        """
        if not self.is_available():
            return []
        code = normalize_stock_code(stock_code)
        if not code or not code.isdigit() or len(code) != 6:
            return []

        try:
            payload = self._client.get(f"/research/research-field-list/{code}")
        except KplError as exc:
            logger.warning("[KplFetcher] 获取研报失败 %s: %s", stock_code, exc)
            return []

        records: List[Dict[str, Any]] = []
        seen = set()
        for item in payload.get("items") or []:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            date = _epoch_to_date(item.get("timestamp")) or ""
            key = (date, title)
            if key in seen:
                continue
            seen.add(key)
            records.append({
                "title": title,
                "date": date,
                "broker": str(item.get("broker") or "").strip(),
                "rating": str(item.get("rating") or "").strip(),
                "abstract": "",
                "analyst": "",
                "eps": None,
                "classify": "",
            })
            if len(records) >= max_count:
                break

        logger.info("[KplFetcher] 研报获取成功 %s: %d 条", stock_code, len(records))
        return records

    def get_belong_board(self, stock_code: str) -> Optional[List[Dict[str, Any]]]:
        """获取个股所属板块。

        方法名是单数 ``get_belong_board`` —— DataFetcherManager 用这个名字做
        hasattr 探测，管理器侧对外的方法才叫 ``get_belong_boards``。

        Returns:
            [{"name": 板块名, "code": 板块代码}, ...]；失败返回 None
        """
        if not self.is_available():
            return None
        code = normalize_stock_code(stock_code)
        if not code or not code.isdigit() or len(code) != 6:
            return None

        try:
            data = self._client.get(f"/plate-list/stock-sector-v2/{code}")
        except KplError as exc:
            logger.warning("[KplFetcher] 获取所属板块失败 %s: %s", stock_code, exc)
            return None

        boards: List[Dict[str, Any]] = []
        seen = set()
        for item in data.get("sectors") or []:
            name = str(item.get("sector_name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            board: Dict[str, Any] = {"name": name}
            board_code = str(item.get("sector_code") or "").strip()
            if board_code:
                board["code"] = board_code
            boards.append(board)

        if not boards:
            logger.debug("[KplFetcher] %s 无所属板块数据", stock_code)
            return None
        return boards

    # ------------------------------------------------------------------
    # 市场维度
    # ------------------------------------------------------------------

    def get_sector_rankings(self, n: int = 5) -> Optional[Tuple[List[Dict], List[Dict]]]:
        """获取板块涨跌排行。

        用 /plate-list/pc-plate-ranking 而不是 /plate-list/top-sectors：后者是
        「精选强势板块」，实测无论 page_size 传多大都只回 8~10 条，拿它的最低
        值当领跌是错的；前者是全市场排行（实测 200 条，涨跌幅 -4.74%~0.93%）。

        Returns:
            (领涨列表, 领跌列表)，元素为 {"name", "change_pct"}；失败返回 None
        """
        if not self.is_available():
            return None
        try:
            data = self._client.get("/plate-list/pc-plate-ranking", params={"page_size": 200})
        except KplError as exc:
            logger.warning("[KplFetcher] 获取板块排行失败: %s", exc)
            return None

        rows = []
        for item in data.get("items") or []:
            name = str(item.get("name") or "").strip()
            change_pct = _to_float(item.get("change_pct"))
            if name and change_pct is not None:
                rows.append({"name": name, "change_pct": change_pct})

        if not rows:
            logger.debug("[KplFetcher] 板块排行无有效数据")
            return None

        # 上游按强度排序而非涨跌幅，这里自行排序取两端
        ordered = sorted(rows, key=lambda x: x["change_pct"], reverse=True)
        top = ordered[:n]
        bottom = list(reversed(ordered[-n:])) if len(ordered) > n else list(reversed(ordered))
        logger.info("[KplFetcher] 板块排行获取成功: %d 个板块", len(rows))
        return top, bottom

    def get_market_stats(self) -> Optional[Dict[str, Any]]:
        """获取市场涨跌统计。

        用 /mood/market-daban-snapshot：它一条就覆盖契约要求的全部 6 个字段，
        而 /market-stats/mood-num-count 缺 flat_count、/market-stats/zhangfu-detail
        缺成交额。

        成交额单位换算依据：该端点的 ``market_turnover_wan`` 与
        ``mood-num-count.market_turnover`` 数值完全相同，而前者字段名显式带
        ``_wan``，故上游为「万元」；DSA 与 TickFlow 一致按「亿元」返回。

        Returns:
            {"up_count", "down_count", "flat_count", "limit_up_count",
             "limit_down_count", "total_amount"(亿元)}；失败返回 None
        """
        if not self.is_available():
            return None
        try:
            data = self._client.get("/mood/market-daban-snapshot")
        except KplError as exc:
            logger.warning("[KplFetcher] 获取大盘统计失败: %s", exc)
            return None

        up = _to_int(data.get("rising_count"))
        down = _to_int(data.get("falling_count"))
        if up is None and down is None:
            logger.debug("[KplFetcher] 大盘统计无涨跌家数，判为无效")
            return None

        turnover_wan = _to_float(data.get("market_turnover_wan"))
        stats = {
            "up_count": up or 0,
            "down_count": down or 0,
            "flat_count": _to_int(data.get("flat_count")) or 0,
            "limit_up_count": _to_int(data.get("limit_up_count")) or 0,
            "limit_down_count": _to_int(data.get("limit_down_count")) or 0,
            "total_amount": round(turnover_wan / 1e4, 2) if turnover_wan else 0.0,
        }
        logger.info(
            "[KplFetcher] 大盘统计: 涨%d 跌%d 平%d 成交%.0f亿",
            stats["up_count"], stats["down_count"], stats["flat_count"],
            stats["total_amount"],
        )
        return stats

    # 说明：未实现 get_main_indices。上游 /market-stats/global-index 只有海外指数
    # （道指/纳指/恒生/日经）、期货、商品与汇率，没有上证/深证/创业板等 A 股指数，
    # 无法满足 get_main_indices(region="cn") 的语义。

    def get_limit_up_pool(
        self,
        date: Optional[str] = None,
        n: int = 20,
    ) -> Optional[List[Dict[str, Any]]]:
        """获取涨停池与连板梯队。

        上游按连板数分档查询（``pid_type`` 即连板数，实测 1=首板、2=二板…，
        传 0 会 502），因此这里逐档拉取再聚合，按连板数从高到低返回。

        Args:
            date: YYYYMMDD；传入则查历史，否则取实时
            n: 返回条数上限
        """
        if not self.is_available():
            return None

        pool: List[Dict[str, Any]] = []
        for board in range(_MAX_CONSECUTIVE_BOARD, 0, -1):
            if len(pool) >= n:
                break
            try:
                if date:
                    payload = self._client.get(
                        "/limit-up/daily-limit-perf-history",
                        params={"date": date, "pid_type": board},
                    )
                else:
                    payload = self._client.get(
                        "/limit-up/realtime", params={"pid_type": board}
                    )
            except KplError as exc:
                # 单档失败不影响其它档位，继续聚合已拿到的部分
                logger.warning("[KplFetcher] 涨停池 %d 板档位获取失败: %s", board, exc)
                continue

            for item in payload.get("items") or []:
                code = str(item.get("code") or "").strip()
                if not code:
                    continue
                pool.append({
                    "code": code,
                    "name": str(item.get("name") or "").strip(),
                    "change_pct": _to_float(item.get("change_pct")),
                    "consecutive_days": item.get("consecutive_days"),
                    "reason": str(item.get("reason") or "").strip(),
                    "sector_name": str(item.get("sector_code") or "").strip(),
                    "seal_amount": _to_float(item.get("seal_amount")),
                    "turnover": _to_float(item.get("turnover")),
                    "circ_mv": _to_float(item.get("circ_mv")),
                    "last_price": _to_float(item.get("last_price")),
                })

        if not pool:
            logger.debug("[KplFetcher] 涨停池无数据 (date=%s)", date or "realtime")
            return None
        logger.info("[KplFetcher] 涨停池获取成功: %d 只", len(pool))
        return pool[:n]

    # 说明：未实现 get_concept_rankings。上游 /theme/concept-fengkou 返回的是
    # 题材热度分（score），不是涨跌幅；DSA 的契约要求 {"name", "change_pct"}，
    # 把热度分填进 change_pct 会让下游把它当涨跌幅解读。保持不实现，由
    # BaseFetcher 的默认 None 让管理器自动降级到其它数据源。

    # ------------------------------------------------------------------
    # 资源释放
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._client.close()


def _to_float(value: Any) -> Optional[float]:
    """把上游可能给出的 str/None/空串统一成 float，无法解析返回 None。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _to_int(value: Any) -> Optional[int]:
    """把上游可能给出的 str/float/None 统一成 int，无法解析返回 None。"""
    f = _to_float(value)
    return int(f) if f is not None else None

def _epoch_to_date(value: Any) -> Optional[str]:
    """秒级 epoch 转 ``YYYY-MM-DD``；无法解析返回 None。"""
    if value in (None, "", 0):
        return None
    try:
        ts = int(float(value))
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return None

def _notify_credential_expired(reason: str) -> None:
    """KPL 凭证失效时推送系统错误通知。

    走 DSA 既有的 ``system_error`` 路由，渠道由 NOTIFICATION_SYSTEM_ERROR_CHANNELS
    控制——未配置则只留日志，符合「不配置也可运行」。dedup_key 固定，让通知层
    自带的去重与冷却接管，避免每次探针 TTL 到期都重复推送。

    整体 fail-open：通知栈不可用不能影响数据源降级本身。
    """
    try:
        from src.notification import NotificationService

        content = (
            "## ⚠️ KPL 数据源凭证失效\n\n"
            f"{reason}\n\n"
            "**影响**：KPL 已自动退出数据源调用链，A 股行情/板块/资讯将回落到"
            "东财、AkShare 等既有数据源，分析不会中断，但数据质量可能下降。\n\n"
            "**处理**：重新抓包更新 kpl-unified-client 的 `.env`"
            "（KPL_USER_ID / KPL_TOKEN / KPL_DEVICE_ID），然后重启该服务。"
        )
        NotificationService().send(
            content,
            route_type="system_error",
            severity="error",
            dedup_key="kpl_credential_expired",
            cooldown_key="kpl_credential_expired",
        )
        logger.info("[KplFetcher] 已推送凭证失效告警")
    except Exception as exc:
        logger.warning("[KplFetcher] 推送凭证失效告警失败: %s", exc)
