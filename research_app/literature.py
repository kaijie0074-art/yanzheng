"""Crossref书目检索与人工筛选；元数据命中不等于读过全文或完成核验。"""

from __future__ import annotations

import json
import re
from contextlib import nullcontext
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .models import now
from .storage import Store, encoded, uid

CROSSREF_WORKS = "https://api.crossref.org/works"
SEARCH_PARAMS = {"rows": 20, "select": "DOI,title,author,published,URL"}
ERROR_MESSAGES = {
    "RATE_LIMIT": "Crossref请求受限（429），请稍后主动重试；这不代表文献不存在。",
    "NETWORK": "无法连接Crossref或请求超时，请检查网络后主动重试。",
    "HTTP": "Crossref返回非成功状态，无法据此判断文献是否存在。",
    "NOT_FOUND": "Crossref未返回该DOI的记录，请核对DOI和正式来源页面。",
    "INVALID_RESPONSE": "Crossref响应格式异常，无法将其作为有效书目信息。",
}


def _get(
    url: str, *, params: dict | None = None, client: httpx.Client | None = None
) -> tuple[Any, str | None]:
    # 不接受调用者指定端点，不跟随重定向，不自动重试。
    context = (
        nullcontext(client)
        if client is not None
        else httpx.Client(timeout=30, trust_env=False)
    )
    try:
        with context as http:
            response = http.get(url, params=params, timeout=30, follow_redirects=False)
            try:
                raw = response.json()
                # SQLite中的JSON与导出件均禁止NaN/Infinity。
                encoded(raw)
            except (ValueError, UnicodeError):
                raw = response.text
                if response.is_success:
                    return raw, "INVALID_RESPONSE"
            if response.status_code == 429:
                return raw, "RATE_LIMIT"
            if response.status_code == 404:
                return raw, "NOT_FOUND"
            if not response.is_success:
                return raw, "HTTP"
            return raw, None
    except httpx.RequestError:
        # 异常文本可能含代理配置等本机细节，不写入研究记录。
        return None, "NETWORK"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _official_url(value: Any) -> str:
    value = _text(value)
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme in {"https", "http"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            return value
    except ValueError:
        pass
    return ""


def _normalize(item: dict) -> dict:
    titles = item.get("title")
    title = (
        next((_text(value) for value in titles if _text(value)), "")
        if isinstance(titles, list)
        else _text(titles)
    )
    authors = []
    entries = item.get("author")
    if isinstance(entries, list):
        for author in entries:
            if not isinstance(author, dict):
                continue
            name = " ".join(
                filter(None, (_text(author.get("given")), _text(author.get("family"))))
            )
            name = name or _text(author.get("name"))
            if name:
                authors.append(name)
    year = None
    published = item.get("published")
    if isinstance(published, dict):
        dates = published.get("date-parts")
        if (
            isinstance(dates, list)
            and dates
            and isinstance(dates[0], list)
            and dates[0]
        ):
            candidate = dates[0][0]
            if type(candidate) is int and 1 <= candidate <= 9999:
                year = candidate
    doi = _text(item.get("DOI")) or None
    official_url = _official_url(item.get("URL"))
    if not official_url and doi:
        official_url = "https://doi.org/" + quote(doi, safe="/")
    return {
        "title": title,
        "authors": authors,
        "year": year,
        "doi": doi,
        "official_url": official_url,
        "access_level": "metadata",
    }


def _decode_run(row: dict) -> dict:
    result = dict(row)
    result["raw"] = json.loads(result.pop("raw_json"))
    result["screening"] = json.loads(result.pop("screening_json"))
    result["results"] = [
        {
            key: item[key]
            for key in (
                "title",
                "authors",
                "year",
                "doi",
                "official_url",
                "access_level",
            )
        }
        for item in result["screening"]
    ]
    if result["status"] == "error":
        result["message"] = ERROR_MESSAGES.get(
            result["error_type"], "Crossref查询失败。"
        )
    elif result["status"] == "pending":
        result["message"] = "请求已记录，尚未获得完整响应；请检查是否中断。"
    elif not result["results"]:
        result["message"] = "本次查询未返回匹配条目，不代表文献不存在；可以调整检索式。"
    else:
        result["message"] = "Crossref仅提供书目元数据，请对照正式页面人工核验。"
    return result


def get_search_runs(store: Store) -> list[dict]:
    """刷新页面只回读记录，不产生网络请求。"""
    return [
        _decode_run(row)
        for row in store.rows("SELECT * FROM search_runs ORDER BY requested_at,id")
    ]


def search_crossref(
    store: Store, query: str, *, client: httpx.Client | None = None
) -> dict:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("请输入非空检索式")
    brief = store.latest_brief()
    if brief is None or not brief.get("confirmed_at"):
        raise ValueError("请先人工确认研究问题，再执行检索")
    run_id = uid()
    with store.connection() as db:
        db.execute(
            "INSERT INTO search_runs(id,study_revision,query,requested_at,status,raw_json,screening_json,error_type) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, brief["revision"], query, now(), "pending", "null", "[]", None),
        )
    raw, error = _get(
        CROSSREF_WORKS,
        params={**SEARCH_PARAMS, "query.bibliographic": query},
        client=client,
    )
    screening = []
    if error is None:
        message = (
            raw.get("message")
            if isinstance(raw, dict) and raw.get("status") == "ok"
            else None
        )
        items = message.get("items") if isinstance(message, dict) else None
        if not isinstance(items, list) or any(
            not isinstance(item, dict) for item in items
        ):
            error = "INVALID_RESPONSE"
        else:
            screening = [
                {**_normalize(item), "include": None, "reason": "", "source_id": None}
                for item in items
            ]
    with store.connection() as db:
        db.execute(
            "UPDATE search_runs SET status=?,raw_json=?,screening_json=?,error_type=? WHERE id=?",
            (
                "error" if error else "success",
                encoded(raw),
                encoded(screening),
                error,
                run_id,
            ),
        )
    return _decode_run(store.rows("SELECT * FROM search_runs WHERE id=?", (run_id,))[0])


def screen_result(
    store: Store, run_id: str, index: int, *, include: bool, reason: str
) -> str | None:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("纳入或排除文献必须填写理由")
    if type(include) is not bool or type(index) is not int or index < 0:
        raise ValueError("筛选选项或条目序号无效")
    reason = reason.strip()
    # 来源创建与筛选更新同一事务提交，重复点击或并发请求不会生成重复来源。
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM search_runs WHERE id=?", (run_id,)).fetchone()
        if row is None or row["status"] != "success":
            raise ValueError("成功的检索记录不存在")
        screening = json.loads(row["screening_json"])
        if index >= len(screening):
            raise ValueError("检索结果条目不存在")
        item = screening[index]
        source_id = item["source_id"]
        if include:
            if not item["title"] or not item["official_url"]:
                raise ValueError(
                    "条目缺少标题或正式来源URL，请对照正式页面补全后手工录入"
                )
            if source_id is None and item["doi"]:
                existing = db.execute(
                    "SELECT id FROM sources WHERE doi=? COLLATE NOCASE ORDER BY created_at,id LIMIT 1",
                    (item["doi"],),
                ).fetchone()
                source_id = existing["id"] if existing else None
            if source_id is None:
                source_id = uid()
                db.execute(
                    "INSERT INTO sources(id,title,authors,year,doi,official_url,access_level,inclusion_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        source_id,
                        item["title"],
                        encoded(item["authors"]),
                        item["year"],
                        item["doi"],
                        item["official_url"],
                        "metadata",
                        reason,
                        now(),
                    ),
                )
        if source_id is not None:
            db.execute(
                "UPDATE sources SET inclusion_reason=?,exclusion_reason=? WHERE id=?",
                (reason if include else "", "" if include else reason, source_id),
            )
        item.update(include=include, reason=reason, source_id=source_id)
        db.execute(
            "UPDATE search_runs SET screening_json=? WHERE id=?",
            (encoded(screening), run_id),
        )
    return source_id if include else None


def verify_doi(doi: str, *, client: httpx.Client | None = None) -> dict:
    """只查询Crossref固定端点；成功仅是书目建议，不更新人工确认状态。"""
    if not isinstance(doi, str) or not re.fullmatch(
        r"10\.\d{4,9}/[^\s\x00-\x1f\x7f]+", doi.strip(), re.ASCII
    ):
        raise ValueError("请输入完整DOI（例如10.1234/example），而不是任意网页URL")
    doi = doi.strip()
    raw, error = _get(CROSSREF_WORKS + "/" + quote(doi, safe=""), client=client)
    result = None
    if error is None:
        message = (
            raw.get("message")
            if isinstance(raw, dict) and raw.get("status") == "ok"
            else None
        )
        if (
            not isinstance(message, dict)
            or _text(message.get("DOI")).casefold() != doi.casefold()
        ):
            error = "INVALID_RESPONSE"
        else:
            result = _normalize(message)
    return {
        "status": "error" if error else "success",
        "error_type": error,
        "message": ERROR_MESSAGES[error]
        if error
        else "已取得Crossref书目建议，仍须人工对照正式页面确认。",
        "result": result,
        "raw": raw,
    }
