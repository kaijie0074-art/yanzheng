"""通用科研工作台事实源；原生资料与按研究隔离的检索、读写和核验记录。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel

from open_notebook.ai.models import DefaultModels, Model, ModelManager
from open_notebook.config import UPLOADS_FOLDER
from open_notebook.domain.notebook import Asset, Notebook, Source
from open_notebook.exceptions import NotFoundError
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
from research_app.backends import (
    BackendError,
    _reply,
    _request_body,
)
from research_app.export import process_snapshot
from research_app.models import BriefContent, ModelConfig, now
from research_app.storage import Store
from research_app.workflow import SMS_QUESTION, confirm_brief

NOTEBOOK_ID = re.compile(r"notebook:[A-Za-z0-9_-]{1,128}")


def store_for(notebook_id: str) -> Store:
    if not NOTEBOOK_ID.fullmatch(notebook_id):
        raise HTTPException(400, "研究编号格式无效")
    root = Path(os.environ.get("RESEARCH_DATA_DIR", "data/competition")).resolve()
    bucket = root / hashlib.sha256(notebook_id.encode()).hexdigest()
    if bucket.is_symlink():
        raise HTTPException(400, "研究目录不能使用符号链接")
    store = Store(bucket)
    with store.connection() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS notebook_sources (source_id TEXT PRIMARY KEY REFERENCES sources(id), native_id TEXT NOT NULL UNIQUE)"
        )
    return store


async def require_notebook(notebook_id: str) -> Notebook:
    if not NOTEBOOK_ID.fullmatch(notebook_id):
        raise HTTPException(400, "研究编号格式无效")
    return await Notebook.get(notebook_id)


def confirm_question(store: Store, question: str, resources: str = "") -> dict:
    question = question.strip()
    if not question:
        raise ValueError("请先写下你要研究的问题")
    previous = store.latest_brief()
    content = (
        dict(previous["content"])
        if previous and previous["content"]["question"] == question
        else BriefContent(
            question=question,
            population_or_object="以本研究问题明确的对象为准；未明确的对象须在方案中列为待核实条件",
            constraints="仅依据本笔记本的合法材料；不将AI建议直接当作结论，不声称穷尽检索或普遍有效",
            available_resources=resources.strip()
            or "用户尚未提供额外资源；先核对本笔记本中的资料与可验证条件",
            validation_goal="核对原文、适用边界和人工判断后形成有依据的方案；未知事项保留为待验证",
        ).model_dump()
    )
    content.update(
        question=question,
        available_resources=resources.strip() or content["available_resources"],
    )
    return confirm_brief(store, content, [question])


async def sync_source(store: Store, notebook_id: str, source_id: str) -> str:
    """同步实际已取得的内容；摘要/书目不作为全文，不让远程URL触发抓取。"""
    row = store.get_source(source_id)
    if row["sha256"]:
        raw = documents.pdf_bytes(store, source_id)
        name = f"research-{hashlib.sha256(notebook_id.encode()).hexdigest()[:16]}-{row['sha256']}.pdf"
        root = Path(UPLOADS_FOLDER).resolve()
        root.mkdir(parents=True, exist_ok=True)
        path = root / name
        try:
            fd = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as handle:
                existing = handle.read(documents.MAX_PDF_BYTES + 1)
            if hashlib.sha256(existing).hexdigest() != row["sha256"]:
                raise ValueError("原生资料副本与核验材料不一致，已停止同步")
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
        asset = Asset(file_path=str(path))
        text = "\n\n".join(
            f"[PDF物理页 {page['page_number']}]\n{page['text']}"
            for page in store.get_pages(source_id)
        )
    else:
        details = discovery.source_details(store, source_id)
        abstract = details.get("abstract", "")
        level = "仅取得摘要，未阅读全文" if abstract else "仅书目信息，未取得摘要或全文"
        text = (
            f"【资料访问层级：{level}；书目未因此获得人工确认】\n"
            f"标题：{row['title']}\n作者：{'；'.join(row['authors'])}\n"
            f"年份：{row['year'] or '未知'}\n正式来源：{row['official_url']}\n"
            + (f"\n摘要原文：\n{abstract}" if abstract else "")
        )
        # 只同步已经取得的文本，原生处理器无需对资料URL再次联网抓取。
        asset = None
    linked = store.rows(
        "SELECT native_id FROM notebook_sources WHERE source_id=?", (source_id,)
    )
    if linked:
        try:
            native = await Source.get(linked[0]["native_id"])
        except NotFoundError:
            # Recreate only a deleted reading copy, never mask database failures.
            native = Source()
    else:
        native = Source()
    native.title = row["title"]
    native.asset = asset
    native.full_text = text
    await native.save()
    if not linked or native.id != linked[0]["native_id"]:
        await native.add_to_notebook(notebook_id)
        with store.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO notebook_sources VALUES(?,?)",
                (source_id, native.id),
            )
    return str(native.id)


class NativeResearchBackend:
    """研究请求统一复用产品模型适配，保留严格JSON与实际模型验证。"""

    def __init__(self, native, registered: Model):
        self.native = native
        self.requested = registered.name
        self.config = SimpleNamespace(
            backend="local_account"
            if registered.provider == "omp"
            else registered.provider,
            api_model=registered.name,
            local_model=registered.name,
        )

    def complete_json(self, *, system: str, payload: dict, schema: type[BaseModel]):
        from open_notebook.ai.omp import LocalModelError

        config = ModelConfig(backend="openai_compatible", api_model=self.requested)
        body = _request_body(config, self.requested, system, payload, schema)
        called_at = now()
        try:
            result = self.native.chat_complete(messages=body["messages"], stream=False)
        except BackendError:
            raise
        except LocalModelError as error:
            raise BackendError("LOCAL_MODEL_ERROR", str(error)) from None
        except Exception:
            logger.warning("Research model call failed; provider exception omitted")
            raise BackendError(
                "CALL_FAILED",
                "模型调用失败；没有自动重试，请检查已选模型、登录状态或API额度。",
            ) from None
        return _reply(
            result.model_dump(exclude_none=True),
            requested=self.requested,
            backend=self.config.backend,
            schema=schema,
            called_at=called_at,
        )


async def research_backend():
    defaults = await DefaultModels.get_instance()
    if not defaults.default_chat_model:
        raise HTTPException(409, "请先启用本机AI，或在模型设置中选择默认聊天模型")
    registered = await Model.get(defaults.default_chat_model)
    native = await ModelManager().get_model(
        registered.id, max_retries=0, timeout=180, max_tokens=4096
    )
    if native is None or registered.type != "language":
        raise HTTPException(409, "请选择一个可用的默认语言模型")
    return NativeResearchBackend(native, registered)


async def model_state() -> dict:
    defaults = await DefaultModels.get_instance()
    if not defaults.default_chat_model:
        return {
            "backend": "local_account",
            "model": os.environ.get("RESEARCH_OMP_MODEL", "openai-codex/gpt-6.1-sol"),
            "configured": False,
        }
    registered = await Model.get(defaults.default_chat_model)
    return {
        "backend": "local_account"
        if registered.provider == "omp"
        else registered.provider,
        "model": registered.name,
        "configured": registered.type == "language",
    }


def preparation_state(store: Store) -> dict:
    case_dir = store.data_dir / "case"
    error = None
    try:
        # Cache validation is read-only; GET never downloads or runs an experiment.
        if case_dir.is_symlink():
            raise case_materials.MaterialError("材料目录不是安全目录")
        prepared = case_materials._load_cache(case_dir) is not None
    except (case_materials.MaterialError, OSError):
        prepared = False
        error = "官方材料缓存未通过完整性校验，请检查材料后重新准备；已有证据和历史过程仍可查看。"
    manifest = None
    path = store.data_dir / "sms_manifest.json"
    try:
        if path.exists() or path.is_symlink():
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
                manifest = json.load(handle)
            if not isinstance(manifest, dict):
                raise ValueError("审计清单格式无效")
            with os.fdopen(
                os.open(case_dir / "SMSSpamCollection", os.O_RDONLY | os.O_NOFOLLOW),
                "rb",
            ) as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            if manifest.get("snapshot_sha256") != digest:
                raise ValueError("审计快照哈希不匹配")
    except (OSError, ValueError, TypeError):
        manifest = None
        error = error or "数据审计清单与当前材料不一致，请重新审计当前数据。"
    return {
        "prepared": prepared,
        "audited": bool(prepared and manifest and manifest.get("status") == "success"),
        "manifest": manifest,
        "error": error,
    }


def research_state(store: Store, notebook_id: str, model: dict) -> dict:
    brief = store.latest_brief()
    base = f"/api/competition/{quote(notebook_id, safe='')}"
    sources = []
    for source in store.list_sources():
        source = dict(source)
        details = discovery.source_details(store, source["id"])
        source["abstract"] = details.get("abstract", "")
        source["discovery_provider"] = details.get("provider")
        source["open_access_pdf_url"] = details.get("pdf_url")
        source["license"] = details.get("license")
        source["pages"] = store.get_pages(source["id"])
        source["pdf_url"] = (
            f"{base}/papers/{quote(source['id'], safe='')}"
            if source["sha256"]
            else None
        )
        linked = store.rows(
            "SELECT native_id FROM notebook_sources WHERE source_id=?", (source["id"],)
        )
        source["native_source_id"] = linked[0]["native_id"] if linked else None
        sources.append(source)
    by_id = {source["id"]: source for source in sources}
    cards = evidence.list_cards(store)
    source_checks: dict[str, bool] = {}
    for card in cards:
        source = by_id.get(card["content"]["source_id"], {})
        card["eligible"] = evidence.eligible_card(
            store, card["id"], source_checks=source_checks
        )
        card["source_title"] = source.get("title", "来源缺失")
        card["pdf_url"] = source.get("pdf_url")
        card["page_text"] = next(
            (
                page["text"]
                for page in source.get("pages", [])
                if page["page_number"] == card["content"]["page_number"]
            ),
            "",
        )
    experiments = []
    for row in experiment.list_runs(store.data_dir):
        try:
            summary = (
                experiment.verified_summary(row["run_id"], store.data_dir)
                if row["verified"]
                else None
            )
        except (ValueError, OSError):
            summary = None
        experiments.append(
            summary
            if summary is not None
            else {
                **row,
                "status": "unverified",
                "validation_aggregates": [],
                "test_aggregates": [],
                "integrity_error": "实验文件未通过当前完整性校验，不能用于正式决定；历史记录仍保留。",
            }
        )
    # The export's dynamic decision gate is the same authority in the UI and in downloads.
    snapshot = process_snapshot(store, source_checks=source_checks)
    for source in sources:
        source["integrity_error"] = (
            "当前原 PDF 缺失、损坏或哈希不符；关联证据不能作为正式论据。"
            if source_checks.get(source["id"]) is False
            else None
        )
    for card in cards:
        card["integrity_error"] = by_id.get(card["content"]["source_id"], {}).get(
            "integrity_error"
        )
    return {
        "notebook_id": notebook_id,
        "brief": brief,
        "sources": sources,
        "cards": cards,
        "candidate_runs": decision.list_candidate_runs(store),
        "decisions": snapshot["decisions"],
        "experiments": experiments,
        "sms_case": bool(brief and brief["content"]["question"] == SMS_QUESTION),
        "experiment_preparation": preparation_state(store),
        "model": model,
        "model_runs": snapshot["model_runs"],
        "discovery": discovery.get_state(store),
        "paper_work": paper_work.get_state(store),
        "web_research": web_research.get_state(store),
    }
