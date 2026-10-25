"""显式调用与人工确认分离；修改问题保留历史并撤销旧正式决策。"""

from __future__ import annotations

import json

from pydantic import BaseModel

from .backends import BackendError
from .models import BriefContent, Clarification, ModelBackend, ModelReply, now
from .storage import Store, encoded, uid

PROMPT_VERSION = "research-evidence-2026-01"
SMS_QUESTION = "有限训练标注条件下，TF-IDF搭配朴素贝叶斯还是逻辑回归，更适合作为英文垃圾短信识别基线？"


def audited_call(
    store: Store,
    backend: ModelBackend,
    *,
    task: str,
    system: str,
    payload: dict,
    schema: type[BaseModel],
    input_refs: dict,
    study_revision: int | None = None,
) -> tuple[str, ModelReply]:
    run_id = uid()
    config = getattr(backend, "config", None)
    backend_name = getattr(config, "backend", "unknown")
    requested = (
        getattr(config, "local_model", "unknown")
        if backend_name == "local_account"
        else getattr(config, "api_model", "unknown")
    )
    started = now()
    try:
        reply = backend.complete_json(system=system, payload=payload, schema=schema)
    except BackendError as error:
        with store.connection() as db:
            db.execute(
                "INSERT INTO model_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    study_revision,
                    task,
                    backend_name,
                    requested,
                    error.actual_model,
                    started,
                    PROMPT_VERSION,
                    encoded(input_refs),
                    "failed",
                    None,
                    error.code,
                ),
            )
        raise
    except Exception:
        with store.connection() as db:
            db.execute(
                "INSERT INTO model_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    study_revision,
                    task,
                    backend_name,
                    requested,
                    None,
                    started,
                    PROMPT_VERSION,
                    encoded(input_refs),
                    "failed",
                    None,
                    "CALL_FAILED",
                ),
            )
        raise
    with store.connection() as db:
        db.execute(
            "INSERT INTO model_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                study_revision,
                task,
                reply.backend,
                reply.requested_model,
                reply.actual_model,
                reply.called_at,
                PROMPT_VERSION,
                encoded(input_refs),
                "success",
                reply.text,
                None,
            ),
        )
    return run_id, reply


def clarify(
    store: Store, backend: ModelBackend, interest: str, resources: str
) -> Clarification:
    if not interest.strip() or not resources.strip():
        raise ValueError("请输入研究兴趣与可用资源；空输入不会调用模型")
    _, reply = audited_call(
        store,
        backend,
        task="clarification",
        system="你是研究问题澄清助手。把兴趣改写为有边界、可验证的研究问题；生成最多3条英文文献检索式。不声称选题无人研究，不代写论文。用户必须编辑确认后才能检索。",
        payload={"interest": interest, "resources": resources},
        schema=Clarification,
        input_refs={"kind": "user_conditions"},
    )
    return Clarification.model_validate(reply.value.model_dump())


def confirm_brief(store: Store, brief: BriefContent | dict, queries: list[str]) -> dict:
    content = BriefContent.model_validate(brief).model_dump()
    if any(not value.strip() for value in content.values()):
        raise ValueError("请填写全部研究条件")
    queries = [query.strip() for query in queries if query.strip()]
    if not 1 <= len(queries) <= 3:
        raise ValueError("请确认1至3条检索式")
    latest = store.latest_brief()
    if latest and latest["content"] == content and latest["queries"] == queries:
        return latest
    revision = latest["revision"] + 1 if latest else 1
    with store.connection() as db:
        db.execute(
            "INSERT INTO briefs VALUES(?,?,?,?,?)",
            (uid(), revision, encoded(content), encoded(queries), now()),
        )
        db.execute(
            "UPDATE decisions SET status='draft', invalidated_at=? WHERE status='final'",
            (now(),),
        )
    return store.latest_brief()


def model_history(store: Store) -> list[dict]:
    rows = store.rows("SELECT * FROM model_runs ORDER BY called_at,id")
    for row in rows:
        row["input_refs"] = json.loads(row["input_refs"])
    return rows
