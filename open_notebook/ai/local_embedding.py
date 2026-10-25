"""真实 FastEmbed ONNX 多语言向量；不使用哈希、伪向量或外部推理API。"""

from __future__ import annotations

import asyncio
import importlib.util
import threading
from pathlib import Path

import numpy as np
from esperanto import EmbeddingModel
from esperanto.common_types import Model
from langchain_core.embeddings import Embeddings

from open_notebook.ai.omp import LocalModelError
from open_notebook.config import DATA_FOLDER

EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_DIMENSIONS = 384
EMBEDDING_WINDOW = 128  # Includes the tokenizer's special tokens.
_CACHE = Path(DATA_FOLDER) / "fastembed"
_model = None
_window_tokenizer = None
_model_lock = threading.Lock()
_inference_lock = threading.Lock()


def embedding_installed() -> bool:
    return importlib.util.find_spec("fastembed") is not None


def prepare_embedding(*, allow_download: bool = False):
    """初始化模型但不推理。只有运维显式传 True 时才允许下载公开权重。"""
    global _model, _window_tokenizer
    with _model_lock:
        if _model is not None:
            return _model
        if not embedding_installed():
            raise LocalModelError(
                "尚未安装fastembed>=0.8,<1，请完成本地向量模型依赖安装。"
            )
        from fastembed import TextEmbedding

        try:
            model = TextEmbedding(
                model_name=EMBEDDING_MODEL,
                cache_dir=str(_CACHE),
                threads=2,
                providers=["CPUExecutionProvider"],
                local_files_only=not allow_download,
            )
            from tokenizers import Tokenizer

            # Clone once: never mutate FastEmbed's tokenizer, or rebuild it per
            # request. Overflow encodings retain original token IDs losslessly.
            tokenizer = Tokenizer.from_str(model.model.tokenizer.to_str())
            if tokenizer.truncation["max_length"] != EMBEDDING_WINDOW:
                raise ValueError("unexpected model window")
            tokenizer.no_padding()
            tokenizer.enable_truncation(max_length=EMBEDDING_WINDOW, stride=0)
            _window_tokenizer = tokenizer
            _model = model
        except Exception:
            raise LocalModelError(
                "本地多语言向量模型尚未就绪；请由本机运维显式下载并检查ONNX权重，未使用替代向量。"
            ) from None
        return _model


def embedding_readiness() -> tuple[bool, str | None]:
    try:
        prepare_embedding()
        return True, None
    except LocalModelError as error:
        return False, str(error)


def _window_vectors(model, encodings):
    """Use FastEmbed's actual ONNX session and pooling on already-tokenized windows."""
    from fastembed.common.onnx_model import OnnxOutputContext

    width = max(len(encoding.ids) for encoding in encodings)
    pad_id = model.model.tokenizer.padding["pad_id"]
    ids = np.full((len(encodings), width), pad_id, dtype=np.int64)
    mask = np.zeros_like(ids)
    for row, encoding in enumerate(encodings):
        size = len(encoding.ids)
        if not 0 < size <= EMBEDDING_WINDOW:
            raise ValueError("invalid window")
        ids[row, :size] = encoding.ids
        mask[row, :size] = encoding.attention_mask
    backend = model.model
    names = {item.name for item in backend.model.get_inputs()}
    inputs = {"input_ids": ids}
    if "attention_mask" in names:
        inputs["attention_mask"] = mask
    if "token_type_ids" in names:
        inputs["token_type_ids"] = np.zeros_like(ids)
    inputs = backend._preprocess_onnx_input(inputs)
    output = backend._run_model(inputs, backend.ONNX_OUTPUT_NAMES)
    vectors = np.asarray(
        list(
            backend._post_process_onnx_output(
                OnnxOutputContext(
                    model_output=output, attention_mask=mask, input_ids=ids
                )
            )
        ),
        dtype=np.float32,
    )
    if (
        vectors.shape != (len(encodings), EMBEDDING_DIMENSIONS)
        or not np.isfinite(vectors).all()
    ):
        raise ValueError("invalid window embeddings")
    if (np.linalg.norm(vectors, axis=1) <= 0).any():
        raise ValueError("zero window embedding")
    return vectors


def _embed_complete_texts(model, texts):
    # Keep only one input's overflow encodings and at most 32 ONNX windows in
    # flight. Accumulate by original index; no window becomes an extra document.
    totals = np.zeros((len(texts), EMBEDDING_DIMENSIONS), dtype=np.float64)
    batch, owners, weights = [], [], []

    def flush():
        vectors = _window_vectors(model, batch)
        for owner, weight, vector in zip(owners, weights, vectors, strict=True):
            totals[owner] += vector * weight
        batch.clear()
        owners.clear()
        weights.clear()

    for owner, text in enumerate(texts):
        encoded = _window_tokenizer.encode(text)
        for window in (encoded, *encoded.overflowing):
            weight = sum(
                active and not special
                for active, special in zip(
                    window.attention_mask, window.special_tokens_mask, strict=True
                )
            )
            if weight <= 0:
                raise ValueError("empty token window")
            batch.append(window)
            owners.append(owner)
            weights.append(weight)
            if len(batch) == LocalEmbeddingModel.MAX_BATCH_SIZE:
                flush()
    if batch:
        flush()
    norms = np.linalg.norm(totals, axis=1, keepdims=True)
    if not np.isfinite(totals).all() or (norms <= 0).any():
        raise ValueError("invalid aggregate embedding")
    # Dividing by total weight is unnecessary before L2 normalization.
    return (totals / norms).tolist()


class LocalEmbeddingModel(EmbeddingModel):
    """384维多语言向量；128-token窗口完整覆盖输入后加权聚合，不截掉后文。"""

    MAX_BATCH_SIZE = 32

    @property
    def provider(self):
        return "fastembed"

    def _get_default_model(self):
        return EMBEDDING_MODEL

    def _get_models(self):
        return [
            Model(
                id=EMBEDDING_MODEL,
                owned_by="sentence-transformers / 本地ONNX",
                type="embedding",
            )
        ]

    def embed(self, texts: list[str], **kwargs) -> list[list[float]]:
        if self.model_name not in (None, EMBEDDING_MODEL):
            raise LocalModelError(
                "本地embedding仅支持已固定的多语言MiniLM模型；请勿混用向量空间。"
            )
        if not isinstance(texts, list) or any(
            not isinstance(text, str) or not text.strip() for text in texts
        ):
            raise LocalModelError("向量输入须为非空文本列表。")
        if not texts:
            return []
        if sum(len(text.encode("utf-8")) for text in texts) > 2 * 1024 * 1024:
            raise LocalModelError("单批向量输入超过2MiB，请分批处理。")
        model = prepare_embedding()
        try:
            with _inference_lock:
                return _embed_complete_texts(model, texts)
        except Exception:
            raise LocalModelError(
                "本地ONNX向量推理失败或维度异常，未保存伪造向量。"
            ) from None

    async def aembed(self, texts: list[str], **kwargs) -> list[list[float]]:
        return await asyncio.to_thread(self.embed, texts, **kwargs)

    def to_langchain(self):
        return LocalLangChainEmbeddings(self)


class LocalLangChainEmbeddings(Embeddings):
    def __init__(self, model: LocalEmbeddingModel):
        self.model = model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.model.embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self.model.embed([text])[0]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self.model.aembed(texts)

    async def aembed_query(self, text: str) -> list[float]:
        return (await self.model.aembed([text]))[0]
