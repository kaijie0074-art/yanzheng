"""合成公开网页验证联网回答边界；不访问网络、不修改用户研究。"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api import competition_service
from api.routers import competition
from research_app import web_access, web_research
from research_app.backends import BackendError
from research_app.export import process_snapshot
from research_app.models import ModelReply, now
from research_app.storage import Store

QUOTE = "Readers should verify primary sources and distinguish facts from inference."
SNIPPET = "Search snippets alone prove all future research claims."
URL = "https://example.org/guidance"


class ControlledBackend:
    config = SimpleNamespace(backend="local_account", local_model="controlled/model")

    def __init__(
        self,
        *,
        quote=QUOTE,
        text="该指南要求核对原始来源并区分事实与推断。",
        on_plan=None,
        on_answer=None,
        page_id=None,
        limitations=None,
    ):
        self.calls = []
        self.quote = quote
        self.text = text
        self.on_plan = on_plan
        self.on_answer = on_answer
        self.page_id = page_id
        self.limitations = (
            ["仅提供网页片段，需要人工判断推论是否成立。"]
            if limitations is None
            else limitations
        )

    def complete_json(self, *, system, payload, schema):
        self.calls.append({"schema": schema, "payload": payload, "system": system})
        if schema is web_research.QueryPlan:
            if self.on_plan:
                self.on_plan()
            value = {"queries": ["primary source verification guidance"]}
        else:
            if self.on_answer:
                self.on_answer()
            value = {
                "findings": [
                    {
                        "text": self.text,
                        "page_id": self.page_id or payload["pages"][0]["page_id"],
                        "quote": self.quote,
                    }
                ],
                "limitations": self.limitations,
            }
        parsed = schema.model_validate(value)
        return ModelReply(
            value=parsed,
            requested_model="controlled/model",
            actual_model="controlled/model",
            backend="controlled",
            usage=None,
            called_at=now(),
            text=json.dumps(value),
        )


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    store = Store(tmp_path / "research")
    competition_service.confirm_question(store, "如何判断公开资料的可信程度？")
    searches, reads = [], []

    def search(query):
        searches.append(query)
        return {
            "provider": "bing",
            "query": query,
            "searched_at": now(),
            "results": [
                {"url": URL, "title": "Public source guidance", "snippet": SNIPPET}
            ],
        }

    def read(url):
        reads.append(url)
        return {
            "url": url,
            "final_url": url,
            "title": "Public source guidance",
            "text": QUOTE,
            "extracted_characters": len(QUOTE),
            "truncated": False,
            "retrieved_at": now(),
            "content_sha256": "a" * 64,
        }

    monkeypatch.setattr(web_access, "search_web", search)
    monkeypatch.setattr(web_access, "read_page", read)
    return store, searches, reads


def lookup(store, backend, **kwargs):
    return web_research.run(
        store, backend, question="如何核对网络资料？", consent=True, **kwargs
    )


def test_actual_page_excerpt_not_search_snippet_supports_persisted_answer(workspace):
    store, searches, reads = workspace
    backend = ControlledBackend()
    report = lookup(store, backend)
    assert report["findings"][0]["quote"] == QUOTE
    assert backend.calls[1]["payload"]["pages"][0]["excerpt"] == QUOTE
    assert SNIPPET not in json.dumps(backend.calls[1]["payload"])
    assert searches and reads == [URL]
    reopened = web_research.get_state(Store(store.data_dir))["runs"][0]
    assert reopened["findings"] == report["findings"]
    assert reopened["pages"][0]["final_url"] == URL
    audit = process_snapshot(store)
    refs = audit["model_runs"][1]["input_refs"]
    assert (
        refs["web_pages"][0]["excerpt_sha256"]
        == hashlib.sha256(QUOTE.encode()).hexdigest()
    )
    assert audit["web_research_records"][0]["pages"][0]["prepared_characters"] == len(
        QUOTE
    )
    assert audit["web_research_records"][0]["pages"][0][
        "model_input_characters"
    ] == len(QUOTE)
    assert QUOTE not in json.dumps(audit)
    assert len(report["model_run_ids"]) == 2
    assert store.list_sources() == []  # 网页不冒充带页码的论文或人审资料。


@pytest.mark.parametrize("consent", [False, 1, "true"])
def test_no_network_or_model_before_explicit_consent(workspace, consent):
    store, searches, reads = workspace
    backend = ControlledBackend()
    with pytest.raises(ValueError, match="同意"):
        web_research.run(store, backend, question="公开资料", consent=consent)
    assert not searches and not reads and not backend.calls
    assert web_research.get_state(store)["runs"] == []


@pytest.mark.parametrize("quote", [SNIPPET, "Readers can trust every source."])
def test_search_snippet_or_changed_quote_cannot_become_a_web_citation(workspace, quote):
    store, _, _ = workspace
    with pytest.raises(ValueError, match="连续原句"):
        lookup(store, ControlledBackend(quote=quote))
    record = web_research.get_state(store)["runs"][0]
    assert record["status"] == "error" and record["findings"] == []
    assert record["pages"][0]["excerpt"] == QUOTE
    calls = store.rows("SELECT status,error_type FROM model_runs ORDER BY called_at,id")
    assert calls[-1] == {"status": "failed", "error_type": "CONTENT_VALIDATION"}


def test_numbers_not_in_same_quote_are_not_accepted(workspace):
    store, _, _ = workspace
    with pytest.raises(ValueError, match="数字"):
        lookup(store, ControlledBackend(text="该指南在2026年验证了99%的成功率。"))
    assert web_research.get_state(store)["runs"][0]["findings"] == []


def test_quote_outside_actual_input_window_is_not_accepted(workspace, monkeypatch):
    store, _, _ = workspace
    text = "Unrelated content. " * 500 + QUOTE
    monkeypatch.setattr(
        web_access,
        "read_page",
        lambda url: {
            "url": url,
            "final_url": url,
            "title": "Long page",
            "text": text,
            "extracted_characters": len(text),
            "truncated": False,
            "retrieved_at": now(),
            "content_sha256": "b" * 64,
        },
    )
    with pytest.raises(ValueError, match="连续原句"):
        lookup(store, ControlledBackend())
    page = web_research.get_state(store)["runs"][0]["pages"][0]
    assert len(page["excerpt"]) == 6000 and page["truncated"]
    assert QUOTE not in page["excerpt"]


def test_unread_or_unknown_page_cannot_be_cited(workspace):
    store, _, _ = workspace
    with pytest.raises(ValueError, match="连续原句"):
        lookup(store, ControlledBackend(page_id="unknown-page"))
    assert web_research.get_state(store)["runs"][0]["findings"] == []


def test_search_failure_is_recorded_not_treated_as_no_relevant_evidence(
    workspace, monkeypatch
):
    store, _, reads = workspace
    backend = ControlledBackend()

    def blocked(query):
        raise web_access.WebAccessError(
            "RATE_LIMIT", "公开搜索服务限流，请稍后主动重试。"
        )

    monkeypatch.setattr(web_access, "search_web", blocked)
    report = lookup(store, backend)
    assert report["status"] == "error" and report["findings"] == []
    assert report["queries"][0]["status"] == "error"
    assert "限流" in report["queries"][0]["message"]
    assert not reads and len(backend.calls) == 1


def test_no_search_hits_does_not_ask_model_to_answer_from_memory(
    workspace, monkeypatch
):
    store, _, reads = workspace
    backend = ControlledBackend()
    monkeypatch.setattr(
        web_access,
        "search_web",
        lambda query: {
            "provider": "bing",
            "query": query,
            "searched_at": now(),
            "results": [],
        },
    )
    report = lookup(store, backend)
    assert report["status"] == "error" and report["queries"][0]["status"] == "success"
    assert not reads and len(backend.calls) == 1


def test_page_block_keeps_search_results_without_claiming_page_was_read(
    workspace, monkeypatch
):
    store, _, _ = workspace
    backend = ControlledBackend()

    def blocked(url):
        raise web_access.WebAccessError("BLOCKED", "页面要求登录或访问验证，未读取。")

    monkeypatch.setattr(web_access, "read_page", blocked)
    report = lookup(store, backend)
    assert report["queries"][0]["results"][0]["url"] == URL
    assert report["pages"][0]["status"] == "error"
    assert not report["pages"][0]["excerpt"]
    assert report["findings"] == [] and len(backend.calls) == 1


@pytest.mark.parametrize("stage", ["plan", "answer"])
def test_revision_change_never_attaches_answer_to_new_question(workspace, stage):
    store, searches, _ = workspace

    def change():
        competition_service.confirm_question(store, "这是不同的新研究问题")

    backend = ControlledBackend(**{f"on_{stage}": change})
    with pytest.raises(web_research.WebResearchConflict):
        lookup(store, backend)
    record = web_research.get_state(store)["runs"][0]
    assert record["study_revision"] == 1 and store.latest_brief()["revision"] == 2
    assert record["status"] == "stale" and record["findings"] == []
    assert all(
        call["status"] == "failed" and call["error_type"] == "INPUT_CHANGED"
        for call in store.rows("SELECT status,error_type FROM model_runs")
    )
    if stage == "plan":
        assert searches == []


def test_answer_backend_failure_preserves_real_pages_and_failed_call(workspace):
    store, _, _ = workspace

    def fail():
        raise BackendError("REMOTE_TIMEOUT", "模型服务响应超时")

    with pytest.raises(BackendError):
        lookup(store, ControlledBackend(on_answer=fail))
    record = web_research.get_state(store)["runs"][0]
    assert record["status"] == "error" and record["pages"][0]["excerpt"] == QUOTE
    assert len(record["model_run_ids"]) == 2
    assert (
        store.rows("SELECT error_type FROM model_runs ORDER BY called_at,id")[-1][
            "error_type"
        ]
        == "REMOTE_TIMEOUT"
    )


def test_api_state_and_export_are_isolated_per_research(
    workspace, tmp_path, monkeypatch
):
    _, _, _ = workspace
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "api"))
    a, b = "notebook:web_a", "notebook:web_b"

    async def exists(identifier):
        if identifier not in {a, b}:
            raise HTTPException(404, "研究不存在")
        return SimpleNamespace(id=identifier)

    monkeypatch.setattr(competition, "require_notebook", exists)
    monkeypatch.setattr(
        competition,
        "model_state",
        AsyncMock(
            return_value={
                "configured": True,
                "backend": "controlled",
                "model": "controlled/model",
            }
        ),
    )
    backend = ControlledBackend()
    monkeypatch.setattr(
        competition, "research_backend", AsyncMock(return_value=backend)
    )
    for identifier in (a, b):
        competition_service.confirm_question(
            competition_service.store_for(identifier), "如何核对公开网页？"
        )
    app = FastAPI()
    app.include_router(competition.router, prefix="/api")
    client = TestClient(app)
    assert (
        client.post(
            f"/api/competition/{a}/web-research",
            json={"question": "查公开来源", "consent": False},
        ).status_code
        == 422
    )
    assert not backend.calls
    response = client.post(
        f"/api/competition/{a}/web-research",
        json={"question": "查公开来源", "consent": True},
    )
    assert response.status_code == 200, response.text
    report = response.json()
    state_a = client.get(f"/api/competition/{a}").json()
    state_b = client.get(f"/api/competition/{b}").json()
    assert state_a["web_research"]["runs"][0]["findings"][0]["quote"] == QUOTE
    assert state_b["web_research"]["runs"] == [] and state_b["model_runs"] == []
    assert client.get("/api/competition/notebook:missing").status_code == 404
    assert report["id"] not in json.dumps(
        process_snapshot(competition_service.store_for(b))
    )
    assert state_a["cards"] == [] and state_a["sources"] == []


def test_limitations_cannot_bypass_unsourced_number_guard(workspace):
    store, _, _ = workspace
    with pytest.raises(ValueError, match="数字"):
        lookup(store, ControlledBackend(limitations=["相关研究已证实99%的准确率。"]))
    record = web_research.get_state(store)["runs"][0]
    assert record["status"] == "error" and record["findings"] == []


def test_revision_during_page_read_does_not_claim_prepared_excerpt_was_model_input(
    workspace, monkeypatch
):
    store, _, _ = workspace
    original = web_access.read_page

    def changed(url):
        material = original(url)
        competition_service.confirm_question(store, "网页取得后切换到不同研究问题")
        return material

    monkeypatch.setattr(web_access, "read_page", changed)
    with pytest.raises(web_research.WebResearchConflict):
        lookup(store, ControlledBackend())
    record = process_snapshot(store)["web_research_records"][0]
    assert record["pages"][0]["prepared_characters"] == len(QUOTE)
    assert record["pages"][0]["model_input_characters"] == 0
    assert record["answer_model_run_id"] is None
    assert "sent_characters" not in record["pages"][0]


def test_unicode_codepoint_coverage_matches_model_input_budget(workspace, monkeypatch):
    store, _, _ = workspace
    text = QUOTE + "𝑥" * 6500
    monkeypatch.setattr(
        web_access,
        "read_page",
        lambda url: {
            "url": url,
            "final_url": url,
            "title": "Math guidance",
            "text": text,
            "extracted_characters": len(text),
            "truncated": False,
            "retrieved_at": now(),
            "content_sha256": "c" * 64,
        },
    )
    backend = ControlledBackend()
    report = lookup(store, backend)
    assert len(backend.calls[1]["payload"]["pages"][0]["excerpt"]) == 6000
    assert report["pages"][0]["prepared_characters"] == 6000
    assert (
        web_research.get_state(Store(store.data_dir))["runs"][0]["pages"][0][
            "prepared_characters"
        ]
        == 6000
    )
    assert (
        process_snapshot(store)["web_research_records"][0]["pages"][0][
            "model_input_characters"
        ]
        == 6000
    )
