"""把服务端响应压成一行能看的摘要。

任务日志是给人看的，不是抓包窗口：整段 JSON 刷进去会把真正有用的那几行
（现在走到哪一步、服务端说了什么错）挤没。要看完整报文的场景已经有
``AUTH_HTTP_TRACE`` / ``AUTH_TRACE_DUMP`` 兜着，日志这边只留摘要。
"""

from __future__ import annotations

import json
from typing import Any

_MAX_SUMMARY = 160


def compact(text: Any, limit: int = _MAX_SUMMARY) -> str:
    """折成一行、掐到 limit 字符。"""
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"


def describe_error(text: Any, limit: int = _MAX_SUMMARY) -> str:
    """错误响应优先只取 message / code，取不到才退回截断原文。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return compact(raw, limit)
    if not isinstance(data, dict):
        return compact(raw, limit)

    error = data.get("error")
    if isinstance(error, dict):
        parts = [str(error.get("message") or "").strip(), str(error.get("code") or "").strip()]
    elif isinstance(error, str):
        parts = [error.strip(), str(data.get("code") or "").strip()]
    else:
        parts = [
            str(data.get("message") or data.get("detail") or "").strip(),
            str(data.get("code") or "").strip(),
        ]

    detail = " ".join(p for p in parts if p).strip()
    return compact(detail or raw, limit)


def describe_page(payload: Any) -> str:
    """从响应体里挑出 page.type / continue_url 这两个真正指示下一步的字段。"""
    if not isinstance(payload, dict):
        return ""
    page = payload.get("page")
    page_type = str((page or {}).get("type") or "").strip() if isinstance(page, dict) else ""
    continue_url = str(payload.get("continue_url") or "").strip()
    parts = []
    if page_type:
        parts.append(f"page={page_type}")
    if continue_url:
        parts.append(f"continue={continue_url}")
    return " ".join(parts)
