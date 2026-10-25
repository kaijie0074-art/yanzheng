"""消费者可见的隔离、人工门禁、当前快照下载与本机跨站保护。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Event
from threading import local as thread_local
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from reportlab.pdfgen.canvas import Canvas

import research_app.export as research_export
from api import competition_service
from api.routers import competition
from research_app.documents import import_pdf
from research_app.workflow import SMS_QUESTION, confirm_brief

A = "notebook:research_a"
B = "notebook:research_b"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))

    async def notebook_exists(identifier):
        # The DB existence boundary is separate from the real per-notebook stores.
        if identifier not in {A, B}:
            from fastapi import HTTPException

            raise HTTPException(404, "研究不存在")
        return SimpleNamespace(
            id=identifier, name="Isolated research", save=AsyncMock()
        )

    async def configured_model():
        return {
            "backend": "local_account",
            "model": "provider/model",
            "configured": True,
        }

    monkeypatch.setattr(competition, "require_notebook", notebook_exists)
    monkeypatch.setattr(competition, "model_state", configured_model)
    app = FastAPI()
    app.include_router(competition.router, prefix="/api")
    return TestClient(app)


def pdf_bytes():
    buffer = BytesIO()
    canvas = Canvas(buffer, invariant=True)
    canvas.drawString(50, 750, "Only the source notebook may read this original paper.")
    canvas.save()
    return buffer.getvalue()


def test_notebook_cannot_read_another_notebooks_paper(client):
    store = competition_service.store_for(A)
    source = import_pdf(store, pdf_bytes(), title="Source A")
    good = client.get(f"/api/competition/{A}/papers/{source['id']}")
    assert good.status_code == 200
    assert good.content == pdf_bytes()
    denied = client.get(f"/api/competition/{B}/papers/{source['id']}")
    assert denied.status_code == 422
    assert b"%PDF" not in denied.content
    assert competition_service.store_for(B).list_sources() == []


def test_sms_formal_decision_requires_confirmed_experiment_before_save(client):
    store = competition_service.store_for(A)
    competition_service.confirm_question(store, SMS_QUESTION)
    response = client.post(
        f"/api/competition/{A}/decisions",
        json={
            "candidate_run_id": "not-an-existing-run",
            "chosen_candidate": "word_nb",
            "reason": "Reviewed choice",
            "evidence_ids": [],
            "final": True,
        },
    )
    assert response.status_code == 422
    assert store.rows("SELECT * FROM decisions") == []


@pytest.mark.parametrize("suffix", ["prepare", "audit", "compare"])
def test_fixed_sms_experiment_rejects_unrelated_research(client, suffix):
    store = competition_service.store_for(A)
    competition_service.confirm_question(store, "How should rainfall be measured?")
    response = client.post(f"/api/competition/{A}/experiments/{suffix}", json={})
    assert response.status_code == 409
    assert not (store.data_dir / "case").exists()
    assert not (store.data_dir / "experiments").exists()


def test_human_acknowledgement_does_not_coerce_string_true(client):
    response = client.post(
        f"/api/competition/{A}/cards/nonexistent/review",
        json={
            "status": "supported",
            "note": "Read source",
            "pdf_opened": "true",
        },
    )
    assert response.status_code == 422


def test_export_download_is_regenerated_after_question_changes(client):
    store = competition_service.store_for(A)
    competition_service.confirm_question(store, "First research question")
    first = client.get(f"/api/competition/{A}/exports/process.json")
    assert first.status_code == 200
    assert first.json()["current_revision"] == 1
    competition_service.confirm_question(store, "Revised research question")
    second = client.get(f"/api/competition/{A}/exports/process.json")
    assert second.status_code == 200
    snapshot = second.json()
    assert snapshot["current_revision"] == 2
    assert snapshot["briefs"][-1]["content"]["question"] == "Revised research question"
    assert snapshot["status"] == "incomplete"
    assert "no-store" in second.headers["cache-control"]
    assert str(store.data_dir).encode() not in second.content


def test_export_route_cannot_download_raw_papers_or_account_configuration(client):
    response = client.get(f"/api/competition/{A}/exports/model_config.json")
    assert response.status_code == 404
    response = client.get(f"/api/competition/{A}/exports/paper.pdf")
    assert response.status_code == 404


def test_local_boundary_blocks_cross_site_posts_and_rebinding(monkeypatch):
    from api.main import app

    monkeypatch.setenv("RESEARCH_LOCAL_ONLY", "1")
    local = TestClient(app, base_url="http://127.0.0.1:5055")
    response = local.post(
        "/api/competition/notebooks",
        json={},
        headers={"Origin": "https://attacker.example"},
    )
    assert response.status_code == 403
    response = local.post(
        "/api/competition/notebooks", json={}, headers={"Host": "attacker.example"}
    )
    assert response.status_code == 403
    # A legitimate request reaches request validation, without touching the DB.
    response = local.post(
        "/api/competition/notebooks",
        json={},
        headers={"Origin": "http://127.0.0.1:3000"},
    )
    assert response.status_code == 422


def test_damaged_material_cache_keeps_research_readable(client):
    store = competition_service.store_for(A)
    competition_service.confirm_question(store, SMS_QUESTION)
    source = import_pdf(store, pdf_bytes(), title="Existing research paper")
    case = store.data_dir / "case"
    case.mkdir()
    raw = b"ham\tisolated test material\n"
    (case / "SMSSpamCollection").write_bytes(raw)
    # A partial cache without its download manifest is explicitly untrusted.
    response = client.get(f"/api/competition/{A}")
    assert response.status_code == 200
    state = response.json()
    assert [item["id"] for item in state["sources"]] == [source["id"]]
    assert state["experiment_preparation"]["prepared"] is False
    assert state["experiment_preparation"]["audited"] is False
    assert state["experiment_preparation"]["error"] is not None
    assert (case / "SMSSpamCollection").read_bytes() == raw
    assert not (store.data_dir / "experiments").exists()


def test_malformed_audit_manifest_is_not_a_broken_research_page(client):
    store = competition_service.store_for(A)
    competition_service.confirm_question(store, SMS_QUESTION)
    (store.data_dir / "sms_manifest.json").write_text("[]", encoding="utf-8")
    response = client.get(f"/api/competition/{A}")
    assert response.status_code == 200
    preparation = response.json()["experiment_preparation"]
    assert preparation["manifest"] is None
    assert preparation["audited"] is False
    assert preparation["error"] is not None


def test_new_question_does_not_inherit_the_sms_research_object(client):
    store = competition_service.store_for(A)
    initial = competition_service.confirm_question(store, SMS_QUESTION)["content"]
    initial["population_or_object"] = "SMS Spam Collection 中的英文短信"
    old = confirm_brief(store, initial, [SMS_QUESTION])
    question = "How should a field study measure plant growth?"
    response = client.post(
        f"/api/competition/{A}/brief",
        json={"question": question, "resources": "Field observations"},
    )
    assert response.status_code == 200
    content = response.json()["content"]
    assert content["question"] == question
    assert content["population_or_object"] != old["content"]["population_or_object"]
    assert content["available_resources"] == "Field observations"


def test_concurrent_download_cannot_read_another_requests_older_snapshot(
    client, monkeypatch
):
    store = competition_service.store_for(A)
    competition_service.confirm_question(store, "Original question")
    first_taken, release_first, first_written = Event(), Event(), Event()
    caller = thread_local()
    real_snapshot = research_export.process_snapshot
    real_export = competition.export_process

    def snapshot_with_controlled_order(store, **kwargs):
        snapshot = real_snapshot(store, **kwargs)
        caller.revision = snapshot["current_revision"]
        if caller.revision == 1:
            first_taken.set()
            assert release_first.wait(30), "newer snapshot did not release older export"
        return snapshot

    def export_with_controlled_order(store, output):
        result = real_export(store, output)
        if caller.revision == 1:
            first_written.set()
        else:
            # The newer file exists before the old request finishes writing.
            release_first.set()
            assert first_written.wait(30), "older export did not finish"
        return result

    monkeypatch.setattr(
        research_export, "process_snapshot", snapshot_with_controlled_order
    )
    monkeypatch.setattr(competition, "export_process", export_with_controlled_order)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.get, f"/api/competition/{A}/exports/process.json")
        try:
            assert first_taken.wait(30), "older snapshot was not captured"
            competition_service.confirm_question(store, "Revised question")
            second = client.get(f"/api/competition/{A}/exports/process.json")
        finally:
            release_first.set()
        older = first.result(timeout=30)
    assert older.status_code == second.status_code == 200
    assert older.json()["current_revision"] == 1
    assert second.json()["current_revision"] == 2
    assert second.json()["briefs"][-1]["content"]["question"] == "Revised question"
