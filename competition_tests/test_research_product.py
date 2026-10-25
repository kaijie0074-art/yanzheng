"""隔离的合成文献验证真实业务链；不联网、不制作或确认用户研究成果。"""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from docx import Document
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api import competition_service
from api.routers import competition
from research_app import discovery, paper_work
from research_app.models import ModelReply, now

A = "notebook:paper_a"
B = "notebook:paper_b"
ABSTRACT = "Evidence supports only the provided sample."
DOI = "10.5555/isolated-paper"


class PaperBackend:
    config = SimpleNamespace(backend="local_account", local_model="controlled/model")

    def __init__(self, source_id):
        self.source_id = source_id
        self.calls = 0

    def complete_json(self, *, schema, **kwargs):
        self.calls += 1
        citation = {"source_id": self.source_id, "page_number": None, "quote": ABSTRACT}
        if schema is paper_work.ReadingOutput:
            value = {
                "findings": [{**citation, "text": "摘要仅支持其提供的样本范围。"}],
                "limitations": ["仅有摘要，不能外推。"],
            }
        else:
            value = {
                "title": "文献范围与局限",
                "sections": [
                    {
                        "heading": "相关工作",
                        "paragraphs": [
                            {
                                "text": "该文献的结论仅支持提供的样本范围，需要进一步核对正文。",
                                "citations": [citation],
                            }
                        ],
                    }
                ],
            }
        parsed = schema.model_validate(value)
        return ModelReply(
            value=parsed,
            requested_model="controlled/model",
            actual_model="controlled/model",
            backend="controlled",
            usage=None,
            called_at=now(),
            text=json.dumps(value, ensure_ascii=False),
        )


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))

    async def notebook_exists(identifier):
        if identifier not in {A, B}:
            raise HTTPException(404, "研究不存在")
        return SimpleNamespace(id=identifier, name="Isolated paper", save=AsyncMock())

    async def model_state():
        return {
            "backend": "controlled",
            "model": "controlled/model",
            "configured": True,
        }

    monkeypatch.setattr(competition, "require_notebook", notebook_exists)
    monkeypatch.setattr(competition, "model_state", model_state)
    monkeypatch.setattr(
        competition, "sync_source", AsyncMock(return_value="source:isolated")
    )
    for identifier in (A, B):
        competition_service.confirm_question(
            competition_service.store_for(identifier), "公开文献的结论有哪些适用范围？"
        )
    article = {
        "id": "https://openalex.org/W123456789",
        "title": "Isolated synthetic research paper",
        "doi": f"https://doi.org/{DOI}",
        "publication_year": 2024,
        "authorships": [{"author": {"display_name": "Public Author"}}],
        "abstract_inverted_index": {
            word: [index] for index, word in enumerate(ABSTRACT.split())
        },
        "best_oa_location": None,
        "primary_location": {"landing_page_url": f"https://doi.org/{DOI}"},
    }
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"results": [article]})
        )
    ) as http:
        run = discovery.search(
            competition_service.store_for(A), "evidence sample boundaries", client=http
        )
    app = FastAPI()
    app.include_router(competition.router, prefix="/api")
    client = TestClient(app)
    included = client.post(
        f"/api/competition/{A}/discovery/runs/{run['id']}/include",
        json={"index": 0, "fetch_fulltext": False},
    )
    assert included.status_code == 200, included.text
    source_id = included.json()["source_id"]
    backend = PaperBackend(source_id)
    monkeypatch.setattr(
        competition, "research_backend", AsyncMock(return_value=backend)
    )
    return client, source_id, backend, run["id"]


def draft(client, source_id):
    response = client.post(
        f"/api/competition/{A}/writing",
        json={
            "kind": "draft",
            "instructions": "写一段相关工作并说明适用边界",
            "source_ids": [source_id],
            "rights_confirmed": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_unreviewed_abstract_can_support_a_traceable_editable_draft(workspace):
    client, source_id, _, _ = workspace
    report = client.post(
        f"/api/competition/{A}/reading",
        json={
            "source_ids": [source_id],
            "question": "这个结论能外推吗？",
            "rights_confirmed": True,
        },
    )
    assert report.status_code == 200, report.text
    assert report.json()["coverage"][0]["level"] == "abstract"
    assert report.json()["coverage"][0]["pages"] == []
    original = draft(client, source_id)
    assert original["references"][0]["doi"] == DOI
    assert original["references"][0]["bibliography_status"] == "unverified"
    body = (
        original["markdown"].split(paper_work.REFERENCE_MARKER)[0]
        + "\n\n人工补充：需要取得正文后检查研究设计。"
    )
    edited_response = client.put(
        f"/api/competition/{A}/writing/{original['id']}", json={"markdown": body}
    )
    assert edited_response.status_code == 201, edited_response.text
    edited = edited_response.json()
    assert edited["id"] != original["id"]
    assert edited["parent_id"] == original["id"]
    state = client.get(f"/api/competition/{A}").json()
    assert state["sources"][0]["bibliography_status"] == "unverified"
    assert state["cards"] == []
    assert state["decisions"] == []
    assert [item["kind"] for item in state["paper_work"]["writing_drafts"]] == [
        "draft",
        "manual",
    ]
    assert state["paper_work"]["writing_drafts"][0]["markdown"] == original["markdown"]
    word = client.get(f"/api/competition/{A}/writing/{edited['id']}/export?format=docx")
    assert word.status_code == 200
    document_text = "\n".join(
        paragraph.text for paragraph in Document(BytesIO(word.content)).paragraphs
    )
    assert "人工补充：需要取得正文后检查研究设计。" in document_text
    assert DOI in document_text
    bibliography = client.get(
        f"/api/competition/{A}/writing/{edited['id']}/export?format=bib"
    )
    assert bibliography.status_code == 200
    assert DOI in bibliography.text
    audit = client.get(f"/api/competition/{A}/exports/process.json").json()
    assert audit["literature_searches"][0]["results"][0]["source_id"] == source_id
    assert audit["reading_records"][0]["coverage"][0]["level"] == "abstract"
    assert audit["writing_records"][-1]["parent_id"] == original["id"]
    assert "markdown" not in audit["writing_records"][-1]
    assert "output_text" not in audit["model_runs"][0]
    refs = audit["model_runs"][0]["input_refs"]
    assert refs["source_ids"] == [source_id]
    assert refs["pages"] == [{"source_id": source_id, "page_number": None}]
    assert refs["coverage"][0]["level"] == "abstract"
    assert (
        refs["excerpts"][0]["text_sha256"]
        == hashlib.sha256(ABSTRACT.encode()).hexdigest()
    )
    assert "text" not in refs["excerpts"][0]


def test_changing_paper_identity_does_not_reuse_previous_abstract(workspace):
    client, source_id, backend, _ = workspace
    changed = client.put(
        f"/api/competition/{A}/sources/{source_id}",
        json={
            "title": "Another paper",
            "authors": ["Another author"],
            "year": 2025,
            "doi": "10.5555/another-paper",
            "official_url": "https://doi.org/10.5555/another-paper",
        },
    )
    assert changed.status_code == 200, changed.text
    state = client.get(f"/api/competition/{A}").json()
    assert state["sources"][0]["abstract"] == ""
    assert state["sources"][0]["open_access_pdf_url"] is None
    assert state["sources"][0]["access_level"] == "metadata"
    reading = client.post(
        f"/api/competition/{A}/reading",
        json={
            "source_ids": [source_id],
            "question": "摘要说明了什么？",
            "rights_confirmed": True,
        },
    )
    assert reading.status_code == 422
    assert backend.calls == 0


def test_project_and_revision_boundaries_prevent_reusing_other_drafts(workspace):
    client, source_id, backend, run_id = workspace
    original = draft(client, source_id)
    other_import = client.post(
        f"/api/competition/{B}/discovery/runs/{run_id}/include",
        json={"index": 0, "fetch_fulltext": False},
    )
    assert other_import.status_code == 422
    other_export = client.get(
        f"/api/competition/{B}/writing/{original['id']}/export?format=md"
    )
    assert other_export.status_code == 422
    other_read = client.post(
        f"/api/competition/{B}/reading",
        json={
            "source_ids": [source_id],
            "question": "借用其他研究资料",
            "rights_confirmed": True,
        },
    )
    assert other_read.status_code == 409
    assert backend.calls == 1
    assert competition_service.store_for(B).list_sources() == []
    update = client.post(
        f"/api/competition/{A}/brief", json={"question": "另一个新的研究问题"}
    )
    assert update.status_code == 200
    stale_save = client.put(
        f"/api/competition/{A}/writing/{original['id']}",
        json={"markdown": "不应保存成新问题的成果。"},
    )
    assert stale_save.status_code == 409
    current = client.get(f"/api/competition/{A}").json()
    assert current["brief"]["revision"] == 2
    assert [
        item["study_revision"] for item in current["paper_work"]["writing_drafts"]
    ] == [1]
    assert backend.calls == 1
