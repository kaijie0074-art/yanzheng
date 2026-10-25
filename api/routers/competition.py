"""基于真实业务核心的赛事产品接口；不执行模型生成的工具或代码。"""

from __future__ import annotations

import os
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from starlette.concurrency import run_in_threadpool

from api.competition_service import (
    confirm_question,
    model_state,
    preparation_state,
    require_notebook,
    research_backend,
    research_state,
    store_for,
    sync_source,
)
from open_notebook.domain.notebook import Notebook
from research_app import (
    case_materials,
    decision,
    discovery,
    documents,
    evidence,
    experiment,
    paper_work,
    web_research,
)
from research_app.backends import BackendError
from research_app.export import ExportError, export_process
from research_app.workflow import SMS_QUESTION

router = APIRouter(prefix="/competition", tags=["research-workflow"])


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Question(Input):
    question: str = Field(min_length=1, max_length=2000)
    resources: str = Field(default="", max_length=4000)


class DiscoveryStart(Input):
    provider: Literal["openalex", "crossref"] = "openalex"


class LiteratureSearch(DiscoveryStart):
    query: str = Field(min_length=1, max_length=500)


class IncludePaper(Input):
    index: StrictInt = Field(ge=0, le=19)
    reason: str = Field(default="用户纳入研究资料", min_length=1, max_length=2000)
    fetch_fulltext: StrictBool = True


class Reading(Input):
    source_ids: list[str] = Field(min_length=1, max_length=8)
    question: str = Field(min_length=1, max_length=4000)
    rights_confirmed: StrictBool


class WebLookup(Input):
    question: str = Field(min_length=1, max_length=4000)
    consent: StrictBool


class Writing(Input):
    kind: Literal["outline", "draft", "revision"]
    instructions: str = Field(default="", max_length=8000)
    source_ids: list[str] = Field(default_factory=list, max_length=8)
    rights_confirmed: StrictBool = False
    parent_id: str | None = Field(default=None, max_length=128)


class WritingEdit(Input):
    markdown: str = Field(min_length=1, max_length=100000)
    title: str | None = Field(default=None, max_length=1000)


class Bibliography(Input):
    title: str = Field(min_length=1, max_length=1000)
    authors: list[str] = Field(max_length=100)
    year: StrictInt | None = Field(default=None, ge=1, le=9999)
    official_url: str = Field(max_length=2000)
    doi: str | None = Field(default=None, max_length=500)
    confirmed: StrictBool = False


class Page(Input):
    source_id: str = Field(min_length=1, max_length=128)
    page_number: StrictInt = Field(ge=1, le=100)


class Analysis(Input):
    pages: list[Page] = Field(min_length=1, max_length=100)
    rights_confirmed: StrictBool


class Review(Input):
    status: Literal[
        "unreviewed", "supported", "partially_supported", "unsupported", "insufficient"
    ]
    note: str = Field(max_length=8000)
    pdf_opened: StrictBool


class Decision(Input):
    candidate_run_id: str = Field(min_length=1, max_length=128)
    chosen_candidate: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=8000)
    evidence_ids: list[str] = Field(max_length=100)
    experiment_run_id: str | None = Field(default=None, max_length=128)
    final: StrictBool = False


class Freeze(Input):
    run_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=8000)
    acknowledge: StrictBool


class Run(Input):
    run_id: str = Field(min_length=1, max_length=128)


async def _run(function, *args, **kwargs):
    try:
        return await run_in_threadpool(partial(function, *args, **kwargs))
    except BackendError as exc:
        raise HTTPException(502, f"{exc.message}（{exc.code}）") from None
    except (
        discovery.DiscoveryConflict,
        paper_work.PaperWorkConflict,
        documents.DocumentConflict,
        web_research.WebResearchConflict,
    ) as exc:
        raise HTTPException(409, str(exc)) from None
    except (ValueError, ExportError) as exc:
        raise HTTPException(422, str(exc)) from None
    except OSError:
        raise HTTPException(
            500, "本地材料或记录无法安全读写；请检查磁盘与目录权限"
        ) from None


async def _store(notebook_id: str):
    await require_notebook(notebook_id)
    return await _run(store_for, notebook_id)


def _sms(store):
    brief = store.latest_brief()
    if not brief or brief["content"]["question"] != SMS_QUESTION:
        raise HTTPException(
            409, "固定SMS实验只适用于公开SMS案例，不可作为其他研究问题的实验结论"
        )
    return brief


@router.post("/notebooks", status_code=201)
async def create_research(body: Question):
    if not body.question.strip():
        raise HTTPException(422, "请先写下你要研究的问题")
    notebook = Notebook(
        name=body.question.strip()[:150],
        description="研证科研工作台：AI文献发现、资料阅读、论文辅助写作与引用追溯",
    )
    await notebook.save()
    store = await _run(store_for, str(notebook.id))
    await _run(confirm_question, store, body.question, body.resources)
    return {"notebook_id": notebook.id}


@router.get("/{notebook_id}")
async def get_research(notebook_id: str):
    store = await _store(notebook_id)
    return await _run(research_state, store, notebook_id, await model_state())


@router.post("/{notebook_id}/brief")
async def update_brief(notebook_id: str, body: Question):
    store = await _store(notebook_id)
    brief = await _run(confirm_question, store, body.question, body.resources)
    notebook = await require_notebook(notebook_id)
    notebook.name = body.question.strip()[:150]
    await notebook.save()
    return brief


@router.post("/{notebook_id}/web-research")
async def lookup_public_web(notebook_id: str, body: WebLookup):
    store = await _store(notebook_id)
    if body.consent is not True:
        raise HTTPException(422, "请先同意本次问题检索及网页片段发送给当前模型。")
    return await _run(
        web_research.run,
        store,
        await research_backend(),
        **body.model_dump(),
    )


@router.post("/{notebook_id}/discovery/start")
async def start_discovery(notebook_id: str, body: DiscoveryStart):
    store = await _store(notebook_id)
    backend = await research_backend()
    plan = await _run(discovery.generate_plan, store, backend)
    first_match = None
    for query in plan["queries"]:
        run = await _run(
            discovery.search,
            store,
            query,
            provider=body.provider,
            study_revision=plan["study_revision"],
        )
        if first_match is None and run["status"] == "success" and run["results"]:
            first_match = run["id"]
    if first_match is not None:
        await _run(discovery.rank_results, store, backend, first_match)
    return await _run(discovery.get_state, store)


@router.post("/{notebook_id}/discovery/search")
async def search_literature(notebook_id: str, body: LiteratureSearch):
    store = await _store(notebook_id)
    return await _run(discovery.search, store, body.query, provider=body.provider)


@router.post("/{notebook_id}/discovery/runs/{run_id}/rank")
async def rank_literature(notebook_id: str, run_id: str):
    store = await _store(notebook_id)
    return await _run(discovery.rank_results, store, await research_backend(), run_id)


@router.post("/{notebook_id}/discovery/runs/{run_id}/include")
async def include_literature(notebook_id: str, run_id: str, body: IncludePaper):
    store = await _store(notebook_id)
    source = await _run(
        discovery.include_result, store, run_id, body.index, reason=body.reason
    )
    result = (
        await _run(discovery.fetch_fulltext, store, source["id"])
        if body.fetch_fulltext
        else {
            "source_id": source["id"],
            "status": source["access_level"],
            "message": "书目及可用摘要已加入资料库；本次未请求获取开放全文。",
        }
    )
    await sync_source(store, notebook_id, result["source_id"])
    return result


@router.post("/{notebook_id}/sources/{source_id}/fetch-fulltext")
async def fetch_open_paper(notebook_id: str, source_id: str):
    store = await _store(notebook_id)
    result = await _run(discovery.fetch_fulltext, store, source_id)
    await sync_source(store, notebook_id, result["source_id"])
    return result


@router.post("/{notebook_id}/reading")
async def read_papers(notebook_id: str, body: Reading):
    store = await _store(notebook_id)
    return await _run(
        paper_work.read_sources,
        store,
        await research_backend(),
        source_ids=body.source_ids,
        question=body.question,
        rights_confirmed=body.rights_confirmed,
    )


@router.post("/{notebook_id}/writing", status_code=201)
async def generate_manuscript(notebook_id: str, body: Writing):
    store = await _store(notebook_id)
    return await _run(
        paper_work.generate_writing,
        store,
        await research_backend(),
        **body.model_dump(),
    )


@router.put("/{notebook_id}/writing/{draft_id}", status_code=201)
async def save_manuscript(notebook_id: str, draft_id: str, body: WritingEdit):
    store = await _store(notebook_id)
    return await _run(paper_work.save_writing, store, draft_id, **body.model_dump())


@router.get("/{notebook_id}/writing/{draft_id}/export")
async def export_manuscript(
    notebook_id: str, draft_id: str, format: Literal["md", "docx", "bib"] = "docx"
):
    store = await _store(notebook_id)
    raw = await _run(paper_work.export_writing, store, draft_id, format)
    media_types = {
        "md": "text/markdown; charset=utf-8",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "bib": "application/x-bibtex; charset=utf-8",
    }
    return Response(
        raw,
        media_type=media_types[format],
        headers={
            "Content-Disposition": f'attachment; filename="research-draft.{format}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{notebook_id}/sources", status_code=201)
async def upload_paper(
    notebook_id: str, file: UploadFile = File(...), title: str = Form("")
):
    store = await _store(notebook_id)
    try:
        raw = await file.read(documents.MAX_PDF_BYTES + 1)
    finally:
        await file.close()
    if len(raw) > documents.MAX_PDF_BYTES:
        raise HTTPException(413, "每个PDF最多20MiB；不会把大文件自动切片发送给模型")
    source = await _run(
        documents.import_pdf, store, raw, title=title.strip() or "导入PDF（待核对书目）"
    )
    source["native_source_id"] = await sync_source(store, notebook_id, source["id"])
    return source


@router.put("/{notebook_id}/sources/{source_id}")
async def update_bibliography(notebook_id: str, source_id: str, body: Bibliography):
    store = await _store(notebook_id)
    await _run(
        store.update_source_metadata,
        source_id,
        title=body.title,
        authors=body.authors,
        year=body.year,
        official_url=body.official_url,
        doi=body.doi,
    )
    await _run(store.set_bibliography, source_id, body.confirmed)
    await sync_source(store, notebook_id, source_id)
    return store.get_source(source_id)


@router.get("/{notebook_id}/papers/{source_id}")
async def open_paper(notebook_id: str, source_id: str):
    store = await _store(notebook_id)
    raw = await _run(documents.pdf_bytes, store, source_id)
    return Response(
        raw,
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'inline; filename="paper.pdf"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{notebook_id}/analyze")
async def analyze(notebook_id: str, body: Analysis):
    store = await _store(notebook_id)
    backend = await research_backend()
    return await _run(
        evidence.analyze_pages,
        store,
        backend,
        [(page.source_id, page.page_number) for page in body.pages],
        rights_confirmed=body.rights_confirmed,
    )


@router.post("/{notebook_id}/cards/{card_id}/review")
async def review(notebook_id: str, card_id: str, body: Review):
    store = await _store(notebook_id)
    return await _run(
        evidence.review_card,
        store,
        card_id,
        body.status,
        body.note,
        pdf_opened=body.pdf_opened,
    )


@router.post("/{notebook_id}/candidates")
async def candidates(notebook_id: str):
    store = await _store(notebook_id)
    brief = store.latest_brief()
    return await _run(
        decision.generate_candidates,
        store,
        await research_backend(),
        sms_case=bool(brief and brief["content"]["question"] == SMS_QUESTION),
    )


@router.post("/{notebook_id}/decisions", status_code=201)
async def save_decision(notebook_id: str, body: Decision):
    store = await _store(notebook_id)
    if body.final:
        brief = store.latest_brief()
        if (
            brief
            and brief["content"]["question"] == SMS_QUESTION
            and not body.experiment_run_id
        ):
            raise HTTPException(
                422,
                "SMS正式决定必须关联已人工冻结、完成保留集确认的实验；目前可保存草稿",
            )
    return await _run(decision.save_decision, store, **body.model_dump())


@router.post("/{notebook_id}/experiments/prepare")
async def prepare(notebook_id: str):
    store = await _store(notebook_id)
    _sms(store)
    manifest = await _run(case_materials.prepare_materials, store.data_dir)
    for name in ("P12-2018.pdf", "E17-2068.pdf"):
        paper = manifest["files"][name]
        raw = await _run((store.data_dir / "case" / name).read_bytes)
        source = await _run(
            documents.import_pdf,
            store,
            raw,
            title=paper["title"],
            authors=paper["authors"],
            year=paper["year"],
            official_url=paper["url"],
        )
        await sync_source(store, notebook_id, source["id"])
    return {
        "prepared": True,
        "notice": "官方材料已导入；书目、发送片段授权和原文支持关系仍须本人确认。短信正文不发送模型。",
    }


@router.post("/{notebook_id}/experiments/audit")
async def audit(notebook_id: str):
    store = await _store(notebook_id)
    _sms(store)
    return await _run(experiment.audit, store.data_dir)


@router.post("/{notebook_id}/experiments/compare")
async def compare(notebook_id: str):
    store = await _store(notebook_id)
    _sms(store)
    if not preparation_state(store)["audited"]:
        raise HTTPException(409, "请先准备固定材料并完成当前快照的数据审计")
    return await _run(experiment.compare, store.data_dir)


@router.post("/{notebook_id}/experiments/freeze")
async def freeze(notebook_id: str, body: Freeze):
    store = await _store(notebook_id)
    brief = _sms(store)
    if body.acknowledge is not True:
        raise HTTPException(422, "请本人确认已阅读验证结果及局限，再填写冻结理由")
    return await _run(
        experiment.freeze_selection,
        body.run_id,
        reason=body.reason,
        study_revision=brief["revision"],
        data_dir=store.data_dir,
    )


@router.post("/{notebook_id}/experiments/confirm")
async def confirm(notebook_id: str, body: Run):
    store = await _store(notebook_id)
    brief = _sms(store)
    summary = await _run(experiment.verified_summary, body.run_id, store.data_dir)
    if summary.get("frozen_selection", {}).get("study_revision") != brief["revision"]:
        raise HTTPException(
            422, "该实验未对当前研究问题版本完成人工冻结，不能运行保留集确认"
        )
    return await _run(experiment.confirm, store.data_dir, body.run_id)


EXPORT_NAMES = {
    "process.json": "application/json",
    "evidence.csv": "text/csv",
    "process.pdf": "application/pdf",
}


def _export_snapshot(store, name: str | None = None):
    # Each request owns its output until bytes are read; concurrent exports cannot
    # replace a fresh incomplete download with another request's older formal file.
    with TemporaryDirectory(prefix="export-", dir=store.data_dir) as directory:
        result = export_process(store, Path(directory))
        if name is None:
            return result, None
        if name not in result["files"]:
            raise HTTPException(409, "该格式尚不能导出，请查看过程导出的前置说明")
        fd = os.open(Path(directory) / name, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            return result, handle.read()


@router.post("/{notebook_id}/exports")
async def create_exports(notebook_id: str):
    store = await _store(notebook_id)
    result, _ = await _run(_export_snapshot, store)
    base = f"/api/competition/{quote(notebook_id, safe='')}/exports"
    return {
        **result,
        "files": [{"name": name, "url": f"{base}/{name}"} for name in result["files"]],
    }


@router.get("/{notebook_id}/exports/{name}")
async def download_export(notebook_id: str, name: str):
    store = await _store(notebook_id)
    if name not in EXPORT_NAMES:
        raise HTTPException(404, "导出文件不存在")
    # Revalidate now and return only this request's private snapshot.
    _, raw = await _run(_export_snapshot, store, name)
    return Response(
        raw,
        media_type=EXPORT_NAMES[name],
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
