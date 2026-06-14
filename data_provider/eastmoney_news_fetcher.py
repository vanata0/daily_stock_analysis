# -*- coding: utf-8 -*-
"""
东方财富新闻直连接口。

§5.1 个股新闻  — search-api-web.eastmoney.com（JSONP）
§5.3 全球资讯  — np-weblist.eastmoney.com（7×24 滚动）

风控策略（参考 a-stock-data SKILL §5.1 #18）：
  - 所有请求走 em_get()：全局限速 1s + 随机抖动，共享 Session
  - preTag/postTag 留空，避免 HTML 标记混入标题
  - 遇到风控（result 只有 passportWeb，无 cmsArticleWebOld）时静默返回 []，不重试
  - 调用方通过 fail-open 模式处理空结果，不应因此阻断主流程
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .eastmoney_http import em_get

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://search-api-web.eastmoney.com/search/jsonp"
_GLOBAL_NEWS_URL = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
_SEARCH_REFERER = "https://so.eastmoney.com/"
_GLOBAL_REFERER = "https://kuaixun.eastmoney.com/"

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text or "").strip()


def get_stock_news(code: str, page_size: int = 20) -> List[Dict[str, Any]]:
    """
    §5.1 东财个股新闻（按股票代码检索，JSONP）。

    Returns:
        [{title, content, time, source, url}, ...]  最新在前
        风控或网络异常时返回 []
    """
    cb = "jQuery_em_news"
    inner = json.dumps(
        {
            "uid": "",
            "keyword": code,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": page_size,
                    "preTag": "",
                    "postTag": "",
                }
            },
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    params = {"cb": cb, "param": inner}
    headers = {"Referer": _SEARCH_REFERER}

    try:
        resp = em_get(_SEARCH_URL, params=params, headers=headers, timeout=15)
        text = resp.text
        # JSONP 解包
        start = text.index("(") + 1
        end = text.rindex(")")
        data = json.loads(text[start:end])
    except Exception as exc:
        logger.warning("[东财个股新闻] %s 请求/解析失败: %s", code, exc)
        return []

    # 风控时只有 passportWeb，无 cmsArticleWebOld
    articles = (data.get("result") or {}).get("cmsArticleWebOld") or []
    if not articles:
        logger.warning("[东财个股新闻] %s 返回空（可能触发风控，下次重试）", code)
        return []

    rows: List[Dict[str, Any]] = []
    for a in articles:
        rows.append(
            {
                "title": _strip_tags(a.get("title", "")),
                "content": _strip_tags(a.get("content", ""))[:200],
                "time": a.get("date", ""),
                "source": a.get("mediaName", ""),
                "url": a.get("url", ""),
            }
        )
    logger.info("[东财个股新闻] %s 获取 %d 条", code, len(rows))
    return rows


def get_global_news(page_size: int = 30) -> List[Dict[str, Any]]:
    """
    §5.3 东财全球资讯 7×24 滚动快讯。

    Returns:
        [{title, summary, time}, ...]  最新在前
    """
    params = {
        "client": "web",
        "biz": "web_724",
        "fastColumn": "102",
        "sortEnd": "",
        "pageSize": str(page_size),
        "req_trace": str(uuid.uuid4()),
    }
    headers = {"Referer": _GLOBAL_REFERER}

    try:
        resp = em_get(_GLOBAL_NEWS_URL, params=params, headers=headers, timeout=10)
        data = resp.json()
    except Exception as exc:
        logger.warning("[东财全球资讯] 请求/解析失败: %s", exc)
        return []

    items = (data.get("data") or {}).get("fastNewsList") or []
    rows: List[Dict[str, Any]] = []
    for item in items:
        rows.append(
            {
                "title": item.get("title", ""),
                "summary": item.get("summary", "")[:200],
                "time": item.get("showTime", ""),
            }
        )
    logger.info("[东财全球资讯] 获取 %d 条", len(rows))
    return rows


def _parse_news_time(raw: str) -> Optional[datetime]:
    """尝试解析多种日期格式，返回 aware UTC datetime 或 None。"""
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:len(fmt)], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def format_stock_news_context(
    news_items: List[Dict[str, Any]],
    stock_name: str,
    code: str,
    max_items: int = 10,
    max_age_days: int = 7,
) -> str:
    """
    将个股新闻列表格式化为可直接注入 prompt 的 news_context 字符串。

    - 过滤超出 max_age_days 的旧闻（时间无法解析的保留）
    - 截取前 max_items 条
    - 格式与 format_intel_report 输出风格一致
    """
    from datetime import timedelta

    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(days=max_age_days)

    filtered: List[Dict[str, Any]] = []
    for item in news_items:
        dt = _parse_news_time(item.get("time", ""))
        if dt is not None and dt < cutoff:
            continue
        filtered.append(item)

    filtered = filtered[:max_items]
    if not filtered:
        return ""

    lines = [f"### 【最新消息】{stock_name}({code}) 近期新闻（东方财富）\n"]
    for item in filtered:
        t = item.get("time", "")[:16]  # YYYY-MM-DD HH:MM
        src = item.get("source", "")
        title = item.get("title", "")
        content = item.get("content", "")
        url = item.get("url", "")

        line = f"- [{t}]"
        if src:
            line += f"【{src}】"
        line += f" {title}"
        if content and content != title:
            line += f"\n  摘要：{content}"
        if url:
            line += f"\n  链接：{url}"
        lines.append(line)

    return "\n".join(lines)
