# -*- coding: utf-8 -*-
"""
Shared rate-limited Eastmoney HTTP client.

All direct Eastmoney API calls (datacenter-web, push2, reportapi, etc.)
MUST route through em_get() to avoid IP blocks.  This module owns one
process-wide session and one rate-limit clock so callers can't accidentally
race past the throttle.

Anti-block strategy (community-measured thresholds, 2026-05):
  - Min interval between requests: 1.0 s + random 50–400 ms jitter
  - Single shared session with Keep-Alive (no per-call TCP handshake)
  - Standard browser UA + endpoint-appropriate Referer
  - Never run concurrent Eastmoney requests (handled by the per-process lock)

Configurable via env:
  EM_DATACENTER_MIN_INTERVAL   float, seconds (default 1.0)
                               Set to 1.5–2.0 for batch / screening jobs.
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# Process-wide shared state — one session, one clock, one lock
_EM_SESSION = requests.Session()
_EM_SESSION.headers.update({"User-Agent": _UA})
_em_last_call: float = 0.0
_em_lock = threading.Lock()

EM_MIN_INTERVAL: float = float(os.getenv("EM_DATACENTER_MIN_INTERVAL", "1.0"))


def em_get(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 15,
    **kwargs: Any,
) -> requests.Response:
    """Rate-limited Eastmoney HTTP GET (process-wide serialised).

    Acquires the global lock before every request so concurrent callers are
    queued rather than bypassing the throttle.  The lock is held only for the
    duration of the sleep + network call, then released.
    """
    global _em_last_call
    with _em_lock:
        elapsed = time.time() - _em_last_call
        wait = EM_MIN_INTERVAL - elapsed
        if wait > 0:
            time.sleep(wait + random.uniform(0.05, 0.4))
        try:
            return _EM_SESSION.get(
                url, params=params, headers=headers, timeout=timeout, **kwargs
            )
        finally:
            _em_last_call = time.time()


def em_datacenter(
    report_name: str,
    columns: str = "ALL",
    filter_str: str = "",
    page_size: int = 50,
    sort_columns: str = "",
    sort_types: str = "-1",
) -> List[Dict[str, Any]]:
    """Query Eastmoney datacenter-web unified endpoint.

    All 龙虎榜 / 解禁 / 融资融券 / 大宗交易 / 股东户数 / 分红 calls share
    this helper, which already applies rate-limiting via em_get().

    Returns:
        List of row dicts from result.data, or [] on any failure.
    """
    params: Dict[str, Any] = {
        "reportName": report_name,
        "columns": columns,
        "filter": filter_str,
        "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumns": sort_columns,
        "sortTypes": sort_types,
        "source": "WEB",
        "client": "WEB",
    }
    try:
        resp = em_get(_DATACENTER_URL, params=params, timeout=15)
        data = resp.json()
        result = data.get("result") or {}
        rows = result.get("data") or []
        return rows if isinstance(rows, list) else []
    except Exception as exc:
        logger.warning("[em_datacenter] %s query failed: %s", report_name, exc)
        return []
