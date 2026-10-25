"""本机模型配置只写公开元数据；不推理、不读取账号凭据、不覆盖已有默认模型。"""

from __future__ import annotations

import asyncio
import shutil

from open_notebook.ai.local_embedding import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    embedding_readiness,
)
from open_notebook.ai.models import DefaultModels, Model
from open_notebook.ai.omp import LocalModelError, default_omp_model, validate_model_name

_setup_lock = asyncio.Lock()


async def local_model_status() -> dict:
    defaults = await DefaultModels.get_instance()
    languages = await Model.get_models_by_type("language")
    current = next(
        (model for model in languages if model.id == defaults.default_chat_model), None
    )
    ready, error = await asyncio.to_thread(embedding_readiness)
    return {
        "available": shutil.which("omp") is not None,
        "configured": current is not None
        and current.provider == "omp"
        and not current.credential,
        "model": current.name
        if current is not None and current.provider == "omp"
        else default_omp_model(),
        "embedding_ready": ready,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dimensions": EMBEDDING_DIMENSIONS,
        "embedding_error": error,
        "embedding_default_id": defaults.default_embedding_model,
        "chat_default_id": defaults.default_chat_model,
        "streaming_mode": "buffered",
        "notice": "OMP通过本人现有登录调用远程模型，不是离线推理；回答完成后一次返回。向量在本机ONNX执行，查询与文档均不加前缀。",
    }


async def setup_local_models(model_name: str | None = None) -> dict:
    requested = validate_model_name(
        model_name if model_name is not None else default_omp_model()
    )
    if not shutil.which("omp"):
        raise LocalModelError(
            "未找到OMP，请先安装支持auth-gateway stdio的OMP；未更改默认模型。"
        )
    async with _setup_lock:
        languages = await Model.get_models_by_type("language")
        embeddings = await Model.get_models_by_type("embedding")
        language = next(
            (
                model
                for model in languages
                if model.provider == "omp"
                and model.name == requested
                and not model.credential
            ),
            None,
        )
        if language is None:
            language = Model(name=requested, provider="omp", type="language")
            await language.save()
        embedding = next(
            (
                model
                for model in embeddings
                if model.provider == "fastembed"
                and model.name == EMBEDDING_MODEL
                and not model.credential
            ),
            None,
        )
        if embedding is None:
            embedding = Model(
                name=EMBEDDING_MODEL, provider="fastembed", type="embedding"
            )
            await embedding.save()
        defaults = await DefaultModels.get_instance()
        changed = []
        preserved = []
        # Optional transformation/long-context slots keep None: their consumers
        # dynamically follow the current chat default, including future changes.
        for slot, value in (
            ("default_chat_model", language.id),
            ("default_embedding_model", embedding.id),
        ):
            if getattr(defaults, slot):
                preserved.append(slot)
            else:
                setattr(defaults, slot, value)
                changed.append(slot)
        # No default_tools_model: this provider deliberately cannot call tools.
        if changed:
            await defaults.update()
    status = await local_model_status()
    return {
        **status,
        "language_model_id": language.id,
        "local_embedding_model_id": embedding.id,
        "registered_model": requested,
        "defaults_assigned": changed,
        "defaults_preserved": preserved,
    }
