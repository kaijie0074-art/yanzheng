"""本机适配契约测试；伪网关仅用于测试协议，绝不注册为产品模型。"""

import asyncio
import os
import shlex
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import HumanMessage

from open_notebook.ai import omp
from open_notebook.ai.local_embedding import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    LocalEmbeddingModel,
)
from open_notebook.ai.omp import LocalModelError, OMPChatModel, OMPLanguageModel
from open_notebook.utils.error_classifier import classify_error


@pytest.fixture
def gateway(tmp_path):
    def create(mode="ok"):
        script = tmp_path / "gateway.py"
        pidfile = tmp_path / "pid"
        script.write_text(
            "import json, os, subprocess, sys, time\n"
            f"mode = {mode!r}\n"
            f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
            f"open({str(pidfile.with_name('supervisor-pid'))!r}, 'w').write(str(os.getppid()))\n"
            "if mode == 'descendant':\n"
            "    descendant = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"    open({str(pidfile.with_name('descendant-pid'))!r}, 'w').write(str(descendant.pid))\n"
            "assert sys.argv[1:] == ['auth-gateway', 'stdio']\n"
            "request = json.loads(sys.stdin.readline())\n"
            "assert sys.stdin.read() == ''\n"
            "assert request['body']['stream'] is False\n"
            "assert 'tools' not in request['body']\n"
            "print(json.dumps({'ready': True, 'version': 'test-fixture'}), flush=True)\n"
            "if mode in ('timeout', 'descendant'): time.sleep(30)\n"
            "if mode == 'stderr': sys.stderr.write('x' * (128 * 1024 + 1))\n"
            "if mode == 'stdout': print('x' * (2 * 1024 * 1024 + 1))\n"
            "body = {'model': request['body']['model'], 'choices': [{'message': {'role': 'assistant', 'content': '测试协议回答'}, 'finish_reason': 'stop'}], 'reasoning': 'must not persist'}\n"
            "if mode == 'model': body['model'] = 'other/model'\n"
            "if mode == 'tools': body['choices'][0]['message']['tool_calls'] = [{'id': 'tool'}]\n"
            "if mode == 'length': body['choices'][0]['finish_reason'] = 'length'\n"
            "if mode == 'empty': body['choices'][0]['message']['content'] = '  '\n"
            "response = {'id': request['id'], 'status': 200, 'body': body}\n"
            "if mode == 'id': response['id'] = 'wrong'\n"
            "if mode == 'quota': response['status'] = 429\n"
            "print(json.dumps(response), flush=True)\n"
            "if mode == 'duplicate': print(json.dumps(response), flush=True)\n"
            "if mode == 'exit': sys.exit(1)\n"
        )
        executable = tmp_path / "omp-fixture"
        executable.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n'
        )
        executable.chmod(0o700)
        return str(executable), pidfile

    return create


@pytest.mark.asyncio
async def test_gateway_protocol_returns_only_approved_metadata(gateway):
    executable, pidfile = gateway()
    result = await omp.gateway_complete(
        [{"role": "user", "content": "测试"}],
        model="test/model",
        _executable=executable,
    )
    assert result.content == "测试协议回答"
    assert result.model == "test/model"
    assert "reasoning" not in result.model_dump()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.with_name("supervisor-pid").read_text()), 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "model",
        "tools",
        "length",
        "empty",
        "id",
        "quota",
        "duplicate",
        "exit",
        "stdout",
        "stderr",
    ],
)
async def test_gateway_rejects_invalid_responses_without_retry(gateway, mode):
    executable, pidfile = gateway(mode)
    with pytest.raises(LocalModelError):
        await omp.gateway_complete(
            [{"role": "user", "content": "测试"}],
            model="test/model",
            _executable=executable,
        )
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@pytest.mark.asyncio
async def test_timeout_reaps_process(gateway):
    executable, pidfile = gateway("timeout")
    with pytest.raises(LocalModelError, match="时限"):
        await omp.gateway_complete(
            [{"role": "user", "content": "测试"}],
            model="test/model",
            _executable=executable,
            _timeout=0.5,
        )
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@pytest.mark.asyncio
async def test_cancellation_reaps_process(gateway):
    executable, pidfile = gateway("timeout")
    task = asyncio.create_task(
        omp.gateway_complete(
            [{"role": "user", "content": "测试"}],
            model="test/model",
            _executable=executable,
        )
    )
    async with asyncio.timeout(5):
        while not pidfile.exists() or not pidfile.read_text():
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@pytest.mark.asyncio
async def test_langchain_buffered_stream_and_esperanto(gateway, monkeypatch):
    executable, _ = gateway()
    original = omp.gateway_complete
    calls = []

    async def complete(messages, **kwargs):
        calls.append(messages)
        return await original(messages, **kwargs, _executable=executable)

    monkeypatch.setattr(omp, "gateway_complete", complete)
    model = OMPLanguageModel(model_name="test/model", max_tokens=4096)
    assert isinstance(model.to_langchain(), OMPChatModel)
    chunks = [
        chunk
        async for chunk in model.to_langchain().astream([HumanMessage(content="测试")])
    ]
    assert "".join(chunk.content for chunk in chunks) == "测试协议回答"
    assert chunks[0].response_metadata["streaming_mode"] == "buffered"
    assert len(calls) == 1
    response = await model.achat_complete([{"role": "user", "content": "标题"}])
    assert response.content == "测试协议回答"


@pytest.mark.asyncio
async def test_rejects_tool_and_oversized_input_before_process(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(LocalModelError):
        await omp.gateway_complete(
            [{"role": "tool", "content": "结果"}], model="test/model"
        )
    with pytest.raises(LocalModelError, match="输入上限"):
        await omp.gateway_complete(
            [{"role": "user", "content": "a" * omp.INPUT_LIMIT}], model="test/model"
        )
    create.assert_not_called()


def test_graph_error_preserves_no_paid_retry_marker():
    error = LocalModelError("本机模型未完成；未自动重试。")
    cls, message = classify_error(error)
    assert cls is LocalModelError
    assert issubclass(cls, ValueError)
    assert message == str(error)


@pytest.mark.asyncio
async def test_setup_is_idempotent_and_preserves_explicit_defaults(monkeypatch):
    from open_notebook.ai import local_model_service as service

    records = []
    defaults = SimpleNamespace(
        default_chat_model="model:user",
        default_transformation_model=None,
        large_context_model=None,
        default_embedding_model="model:user_embedding",
        update=AsyncMock(),
    )

    class FakeModel:
        def __init__(self, **values):
            self.__dict__.update(values)
            self.credential = None
            self.id = None

        async def save(self):
            self.id = f"model:{len(records)}"
            records.append(self)

        @staticmethod
        async def get_models_by_type(kind):
            return [record for record in records if record.type == kind]

    monkeypatch.setattr(service, "Model", FakeModel)
    monkeypatch.setattr(
        service.DefaultModels, "get_instance", AsyncMock(return_value=defaults)
    )
    monkeypatch.setattr(service.shutil, "which", lambda _: "/test/omp")
    monkeypatch.setattr(service, "embedding_readiness", lambda: (False, "未下载模型"))
    call = AsyncMock()
    monkeypatch.setattr(omp, "gateway_complete", call)
    first = await service.setup_local_models("test/model")
    second = await service.setup_local_models("test/model")
    assert len(records) == 2
    assert first["language_model_id"] == second["language_model_id"]
    assert defaults.default_chat_model == "model:user"
    assert defaults.default_embedding_model == "model:user_embedding"
    assert defaults.default_transformation_model is None
    assert defaults.large_context_model is None
    assert second["defaults_assigned"] == []
    call.assert_not_called()
    defaults.default_chat_model = None
    defaults.default_embedding_model = None
    third = await service.setup_local_models("test/model")
    assert third["defaults_assigned"] == [
        "default_chat_model",
        "default_embedding_model",
    ]
    assert defaults.default_chat_model == third["language_model_id"]
    assert defaults.default_embedding_model == third["local_embedding_model_id"]
    assert defaults.default_transformation_model is None
    assert defaults.large_context_model is None
    defaults.default_transformation_model = "model:explicit_transform"
    defaults.large_context_model = "model:explicit_long"
    await service.setup_local_models("test/model")
    assert defaults.default_transformation_model == "model:explicit_transform"
    assert defaults.large_context_model == "model:explicit_long"


@pytest.mark.skipif(
    os.environ.get("RUN_LOCAL_EMBEDDING_INTEGRATION") != "1",
    reason="需运维预下载真实ONNX权重；默认测试不联网",
)
def test_real_multilingual_embedding_is_deterministic_and_searchable():
    model = LocalEmbeddingModel(model_name=EMBEDDING_MODEL)
    texts = ["垃圾短信识别与文本分类", "垃圾短信识别与文本分类", "火星岩石与地质测量"]
    vectors = model.embed(texts)
    assert len(vectors) == 3
    assert all(len(vector) == EMBEDDING_DIMENSIONS for vector in vectors)
    assert vectors[0] == pytest.approx(vectors[1])
    query = model.to_langchain().embed_query(texts[0])
    import numpy as np

    scores = np.asarray(vectors) @ np.asarray(query)
    assert scores[0] > scores[2]
    assert query == pytest.approx(vectors[0])


@pytest.fixture
def window_backend(monkeypatch):
    """Real tokenizer and FastEmbed pooling, deterministic ONNX-session fixture."""
    import numpy as np
    from fastembed.text.pooled_embedding import PooledEmbedding
    from tokenizers import Tokenizer, models, pre_tokenizers, processors

    from open_notebook.ai import local_embedding as embedding

    tokenizer = Tokenizer(
        models.WordLevel(
            {
                "<pad>": 0,
                "<unk>": 1,
                "<s>": 2,
                "</s>": 3,
                "prefix": 4,
                "alpha": 5,
                "beta": 6,
            },
            unk_token="<unk>",
        )
    )
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.post_processor = processors.TemplateProcessing(
        single="<s> $A </s>", special_tokens=[("<s>", 2), ("</s>", 3)]
    )
    tokenizer.enable_truncation(max_length=128, stride=0)
    tokenizer.enable_padding(pad_id=0, pad_token="<pad>")
    window_tokenizer = Tokenizer.from_str(tokenizer.to_str())
    window_tokenizer.no_padding()

    class Backend:
        ONNX_OUTPUT_NAMES = None

        def __init__(self):
            self.tokenizer = tokenizer
            self.model = self
            self.batches = []
            self.fail = None

        def get_inputs(self):
            return [
                SimpleNamespace(name=name)
                for name in ("input_ids", "attention_mask", "token_type_ids")
            ]

        def _preprocess_onnx_input(self, inputs):
            return inputs

        def _run_model(self, inputs, output_names):
            ids, mask = inputs["input_ids"], inputs["attention_mask"]
            self.batches.append((ids.copy(), mask.copy()))
            if self.fail == "raise":
                raise RuntimeError("fixture inference failure")
            result = np.zeros((*ids.shape, EMBEDDING_DIMENSIONS), dtype=np.float32)
            for index, token in enumerate((4, 5, 6)):
                result[:, :, index] = ids == token
            if self.fail == "nan":
                result[:] = np.nan
            if self.fail == "zero":
                result[:] = 0
            if self.fail == "dimension":
                return result[:, :, :10]
            if self.fail == "missing":
                return result[:-1]
            return result

        def _post_process_onnx_output(self, output):
            return PooledEmbedding._post_process_onnx_output(self, output)

        mean_pooling = staticmethod(PooledEmbedding.mean_pooling)

    backend = Backend()
    monkeypatch.setattr(
        embedding, "prepare_embedding", lambda: SimpleNamespace(model=backend)
    )
    monkeypatch.setattr(embedding, "_window_tokenizer", window_tokenizer)
    return backend


def test_embedding_preserves_tail_order_dimensions_and_normalization(window_backend):
    import numpy as np

    model = LocalEmbeddingModel(model_name=EMBEDDING_MODEL)
    prefix = "prefix " * 252
    texts = [prefix + "alpha " * 126, prefix + "beta " * 126, "alpha"]
    vectors = np.asarray(model.to_langchain().embed_documents(texts))
    assert vectors.shape == (3, 384)
    assert np.linalg.norm(vectors, axis=1) == pytest.approx([1, 1, 1])
    assert vectors[0, :3] == pytest.approx(np.array([2, 1, 0]) / np.sqrt(5))
    assert vectors[1, :3] == pytest.approx(np.array([2, 0, 1]) / np.sqrt(5))
    assert vectors[2, :3] == pytest.approx([0, 1, 0])
    assert not np.allclose(vectors[0], vectors[1])
    for text, vector in zip(texts, vectors, strict=True):
        assert model.to_langchain().embed_query(text) == pytest.approx(vector)
    # Every content token reaches ONNX, including all overflowing windows.
    first_ids, first_mask = window_backend.batches[0]
    assert int(((first_ids >= 4) * first_mask).sum()) == 757
    assert first_ids.shape[1] <= 128


def test_embedding_weights_short_tail_by_effective_tokens(window_backend):
    import numpy as np

    vector = LocalEmbeddingModel().embed(["prefix " * 126 + "alpha " * 3])[0]
    # FastEmbed mean pooling includes the two special tokens; aggregation
    # weights only the 126/3 real content tokens, never padding or window count.
    expected = np.zeros(384)
    expected[:2] = [126 * (126 / 128), 3 * (3 / 5)]
    expected /= np.linalg.norm(expected)
    assert vector == pytest.approx(expected)


def test_embedding_bounds_window_batches_without_losing_input_order(window_backend):
    import numpy as np

    texts = ["prefix " * (126 * 35), "beta", "alpha " * 127]
    vectors = LocalEmbeddingModel().embed(texts)
    assert len(vectors) == 3
    assert [len(ids) for ids, _ in window_backend.batches] == [32, 6]
    assert vectors[0][:3] == pytest.approx([1, 0, 0])
    assert vectors[1][:3] == pytest.approx([0, 0, 1])
    assert vectors[2][:3] == pytest.approx([0, 1, 0])
    assert np.linalg.norm(vectors, axis=1) == pytest.approx([1, 1, 1])


@pytest.mark.parametrize("failure", ["raise", "nan", "zero", "dimension", "missing"])
def test_embedding_window_failure_never_returns_partial_success(
    window_backend, failure
):
    window_backend.fail = failure
    with pytest.raises(LocalModelError, match="推理失败"):
        LocalEmbeddingModel().embed(["prefix " * 500, "beta"])


@pytest.mark.asyncio
async def test_transformation_local_model_error_fails_once(monkeypatch):
    from surreal_commands.core.registry import registry
    from surreal_commands.core.retry import build_async_retry_instance

    from commands import source_commands

    monkeypatch.setattr(
        source_commands.Source,
        "get",
        AsyncMock(return_value=SimpleNamespace(full_text="text")),
    )
    monkeypatch.setattr(
        source_commands.Transformation,
        "get",
        AsyncMock(return_value=SimpleNamespace(model_id=None)),
    )
    invoke = AsyncMock(side_effect=LocalModelError("本机模型失败；未自动重试。"))
    monkeypatch.setattr(source_commands.transform_graph, "ainvoke", invoke)
    command = registry.get_command_by_id("open_notebook.run_transformation")
    retry = build_async_retry_instance(
        command.retry_config.model_copy(
            update={
                "wait_strategy": "fixed",
                "wait_time": 0,
                "wait_min": 0,
                "wait_max": 0,
            }
        )
    )
    with pytest.raises(LocalModelError):
        async for attempt in retry:
            with attempt:
                await source_commands.run_transformation_command(
                    source_commands.RunTransformationInput(
                        source_id="source:fixture",
                        transformation_id="transformation:fixture",
                    )
                )
    assert invoke.await_count == 1


@pytest.mark.asyncio
async def test_transformation_expected_validation_still_returns_failure(monkeypatch):
    from commands import source_commands

    monkeypatch.setattr(source_commands.Source, "get", AsyncMock(return_value=None))
    result = await source_commands.run_transformation_command(
        source_commands.RunTransformationInput(
            source_id="source:missing", transformation_id="transformation:fixture"
        )
    )
    assert result.success is False
    assert "not found" in result.error_message


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", [signal.SIGTERM, signal.SIGKILL])
async def test_gateway_parent_death_reaps_only_owned_processes(gateway, termination):
    executable, pidfile = gateway("descendant")
    program = (
        "import asyncio; from open_notebook.ai.omp import gateway_complete; "
        "asyncio.run(gateway_complete([{'role':'user','content':'fixture'}], "
        f"model='test/model', _executable={executable!r}))"
    )
    unrelated = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)", start_new_session=True
    )
    parent = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        program,
        cwd=Path(__file__).resolve().parents[1],
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    descendant_file = pidfile.with_name("descendant-pid")
    try:
        async with asyncio.timeout(10):
            while not descendant_file.exists() or not descendant_file.read_text():
                await asyncio.sleep(0.01)
        owned = [int(pidfile.read_text()), int(descendant_file.read_text())]
        parent.send_signal(termination)
        await parent.wait()
        async with asyncio.timeout(5):
            for pid in owned:
                while True:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    await asyncio.sleep(0.01)
        assert unrelated.returncode is None
        os.kill(unrelated.pid, 0)
    finally:
        if parent.returncode is None:
            parent.kill()
        await parent.wait()
        unrelated.kill()
        await unrelated.wait()


@pytest.mark.skipif(
    os.environ.get("RUN_LOCAL_EMBEDDING_INTEGRATION") != "1",
    reason="需运维预下载真实ONNX权重；默认测试不联网",
)
def test_real_multilingual_embedding_keeps_long_document_tail():
    import numpy as np

    prefix = "这是两篇材料共有的研究背景和公开说明。" * 80
    texts = [
        prefix + "垃圾短信识别与文本分类实验。" * 80,
        prefix + "火星岩石矿物成分与地质测量。" * 80,
    ]
    vectors = np.asarray(LocalEmbeddingModel().embed(texts))
    assert vectors.shape == (2, 384)
    assert np.linalg.norm(vectors, axis=1) == pytest.approx([1, 1])
    assert not np.allclose(vectors[0], vectors[1], atol=1e-4)
