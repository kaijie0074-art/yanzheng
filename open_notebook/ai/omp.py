"""OMP 本机账号的无工具 stdio 适配；不是 HTTP 服务或编码代理。

网关完成并通过校验后才产生一个 buffered 流块，不冒充实时 token 流。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from typing import Any
from uuid import uuid4

from esperanto import LanguageModel
from esperanto.common_types import ChatCompletion, ChatCompletionChunk, Model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from open_notebook.exceptions import ExternalServiceError
from research_app.backends import BackendError, _load_json, _status_error

DEFAULT_OMP_MODEL = "openai-codex/gpt-6.1-sol"
MODEL_PATTERN = re.compile(r"[A-Za-z0-9._/:\-@+]{1,256}")
INPUT_LIMIT = 1024 * 1024
STDOUT_LIMIT = 2 * 1024 * 1024
STDERR_LIMIT = 128 * 1024

# Only API/worker owns the liveness pipe's write end (CLOEXEC); even SIGKILL
# closes it. A stdlib supervisor owns and reaps the request's separate OMP
# process group, on EOF and on normal exit. No PID files or process scans.
_GATEWAY_SUPERVISOR = """
import os, signal, subprocess, sys, threading

parent_fd = int(sys.argv[1])
try:
    child = subprocess.Popen(
        [sys.argv[2], "auth-gateway", "stdio"],
        close_fds=True, start_new_session=True,
    )
except OSError:
    sys.exit(127)

def stop_child_group():
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

def watch_parent():
    try:
        while os.read(parent_fd, 1):
            pass
    finally:
        stop_child_group()

threading.Thread(target=watch_parent, daemon=True).start()
try:
    status = child.wait()
finally:
    stop_child_group()
sys.exit(status)
"""


class LocalModelError(ExternalServiceError, ValueError):
    """Stable, secret-free error; ValueError stops paid background retries."""


def validate_model_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not MODEL_PATTERN.fullmatch(value)
        or "/" not in value
    ):
        raise LocalModelError(
            "OMP模型须为明确的provider/model标识，不得包含空白或通配符。"
        )
    return value


def default_omp_model() -> str:
    return validate_model_name(os.environ.get("RESEARCH_OMP_MODEL", DEFAULT_OMP_MODEL))


def _messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not messages:
        raise LocalModelError("模型输入不能为空。")
    result = []
    for message in messages:
        if (
            message.get("role") not in {"system", "user", "assistant"}
            or message.get("tool_calls")
            or message.get("function_call")
        ):
            raise LocalModelError("本机模型只接受文本对话，不接受工具调用或工具结果。")
        content = message.get("content")
        if isinstance(content, list):
            if any(
                not isinstance(block, dict)
                or block.get("type") != "text"
                or not isinstance(block.get("text"), str)
                for block in content
            ):
                raise LocalModelError("本机账号适配仅支持文本，请先提取材料文字。")
            content = "\n".join(block["text"] for block in content)
        if not isinstance(content, str):
            raise LocalModelError("模型消息必须包含文本。")
        result.append({"role": message["role"], "content": content})
    return result


async def gateway_complete(
    messages: list[dict[str, Any]],
    *,
    model: str,
    max_tokens: int = 4096,
    _executable: str | None = None,
    _timeout: float = 180,
) -> ChatCompletion:
    """Single request, bounded pipes, deadline and cancellation-safe process-group cleanup."""
    model = validate_model_name(model)
    if type(max_tokens) is not int or not 1 <= max_tokens <= 32768:
        raise LocalModelError("输出token上限须在1至32768之间。")
    if not 0 < _timeout <= 180:
        raise LocalModelError("网关超时须在0至180秒之间。")
    request_id = str(uuid4())
    body = {
        "model": model,
        "messages": _messages(messages),
        "stream": False,
        "max_tokens": max_tokens,
    }
    wire = (
        json.dumps(
            {"id": request_id, "path": "/v1/chat/completions", "body": body},
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode()
    if len(wire) > INPUT_LIMIT:
        raise LocalModelError(
            "对话与材料超过1MiB输入上限，请缩小选择范围；未截断或调用模型。"
        )
    executable = _executable or shutil.which("omp")
    if not executable:
        raise LocalModelError(
            "未找到OMP，请先安装并在OMP本身登录；应用不读取账号token。"
        )

    async def bounded_read(
        stream: asyncio.StreamReader, limit: int, retain: bool
    ) -> bytes:
        chunks = []
        size = 0
        while chunk := await stream.read(65536):
            size += len(chunk)
            if size > limit:
                raise LocalModelError(
                    "网关输出超过安全上限，已停止；错误流不保存或显示。"
                )
            if retain:
                chunks.append(chunk)
        return b"".join(chunks)

    with tempfile.TemporaryDirectory(prefix="notebook-omp-") as cwd:
        parent_read, parent_write = os.pipe()
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    _GATEWAY_SUPERVISOR,
                    str(parent_read),
                    executable,
                    cwd=cwd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    pass_fds=(parent_read,),
                )
            finally:
                os.close(parent_read)
        except BaseException as error:
            os.close(parent_write)
            if isinstance(error, OSError):
                raise LocalModelError("无法启动OMP网关，请检查安装和权限。") from None
            raise
        tasks = []
        try:
            async with asyncio.timeout(_timeout):
                tasks = [
                    asyncio.create_task(
                        bounded_read(process.stdout, STDOUT_LIMIT, True)
                    ),
                    asyncio.create_task(
                        bounded_read(process.stderr, STDERR_LIMIT, False)
                    ),
                ]
                process.stdin.write(wire)
                await process.stdin.drain()
                process.stdin.close()
                stdout, _ = await asyncio.gather(*tasks)
                if await process.wait() != 0:
                    raise LocalModelError(
                        "OMP网关异常退出，请检查登录、额度或版本；未自动重试。"
                    )
        except TimeoutError:
            raise LocalModelError(
                "本机模型请求超过时限，已终止网关进程组；未自动重试。"
            ) from None
        except (BrokenPipeError, ConnectionResetError):
            raise LocalModelError(
                "OMP网关提前关闭输入；请检查网关版本与登录状态。"
            ) from None
        finally:
            os.close(parent_write)
            process.stdin.close()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await process.wait()

    try:
        lines = stdout.decode("utf-8", errors="strict").splitlines()
        if not stdout.endswith(b"\n") or len(lines) != 2:
            raise ValueError
        ready, response = (_load_json(line) for line in lines)
        if (
            not isinstance(ready, dict)
            or ready.get("ready") is not True
            or not isinstance(ready.get("version"), str)
            or not ready["version"].strip()
        ):
            raise ValueError
        if (
            not isinstance(response, dict)
            or response.get("id") != request_id
            or type(response.get("status")) is not int
            or "body" not in response
            or "encoding" in response
        ):
            raise ValueError
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise LocalModelError(
            "网关握手、请求id或响应数量不符合stdio协议；未使用结果。"
        ) from None
    try:
        _status_error(response["status"])
    except BackendError as error:
        raise LocalModelError(error.message) from None
    result = response["body"]
    if not isinstance(result, dict) or result.get("model") != model:
        raise LocalModelError(
            "实际模型与请求模型不符或缺失；已拒绝回退结果，请明确指定可用模型。"
        )
    choices = result.get("choices")
    if (
        not isinstance(choices, list)
        or len(choices) != 1
        or not isinstance(choices[0], dict)
    ):
        raise LocalModelError("网关必须返回唯一回答。")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise LocalModelError("网关响应缺少assistant消息。")
    if message.get("tool_calls") or message.get("function_call"):
        raise LocalModelError("模型返回工具调用，已拒绝；不会执行任何模型生成代码。")
    if message.get("refusal") or choice.get("finish_reason") == "content_filter":
        raise LocalModelError("模型拒答或被过滤，请检查输入；未自动重试。")
    if choice.get("finish_reason") != "stop":
        raise LocalModelError("模型输出被截断或未正常结束；未使用不完整回答。")
    if not isinstance(message.get("content"), str) or not message["content"].strip():
        raise LocalModelError("模型返回空文本；未自动重试。")
    raw_usage = result.get("usage")
    usage = None
    if isinstance(raw_usage, dict) and all(
        type(raw_usage.get(key)) is int and raw_usage[key] >= 0
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    ):
        usage = {
            key: raw_usage[key]
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
    # Reconstruct only approved fields: never retain reasoning, provider extras or account metadata.
    return ChatCompletion(
        id=request_id,
        model=model,
        provider="omp",
        created=int(time.time()),
        usage=usage,
        choices=[
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": message["content"]},
            }
        ],
    )


def _lc_messages(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    roles = {"human": "user", "ai": "assistant", "system": "system"}
    result = []
    for message in messages:
        if (
            message.type not in roles
            or getattr(message, "tool_calls", None)
            or message.additional_kwargs.get("tool_calls")
            or message.additional_kwargs.get("function_call")
        ):
            raise LocalModelError("本机模型不接受工具调用或非文本角色。")
        result.append({"role": roles[message.type], "content": message.content})
    return result


class OMPChatModel(BaseChatModel):
    model_name: str
    max_tokens: int = 4096

    @property
    def _llm_type(self) -> str:
        return "omp-stdio-buffered"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "transport": "stdio",
            "streaming_mode": "buffered",
        }

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if (
            stop
            or kwargs.get("tools")
            or kwargs.get("functions")
            or kwargs.get("tool_choice")
        ):
            raise LocalModelError("本机模型不支持stop序列或工具调用参数。")
        response = await gateway_complete(
            _lc_messages(messages),
            model=self.model_name,
            max_tokens=kwargs.get("max_tokens", self.max_tokens),
        )
        metadata = {
            "model_name": response.model,
            "provider": "omp",
            "finish_reason": "stop",
            "streaming_mode": "buffered",
        }
        if response.usage is not None:
            metadata["token_usage"] = response.usage.model_dump()
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content=response.content, response_metadata=metadata
                    )
                )
            ]
        )

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return asyncio.run(self._agenerate(messages, stop=stop, **kwargs))

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        result = await self._agenerate(messages, stop=stop, **kwargs)
        message = result.generations[0].message
        chunk = ChatGenerationChunk(
            message=AIMessageChunk(
                content=message.content, response_metadata=message.response_metadata
            )
        )
        if run_manager is not None:
            await run_manager.on_llm_new_token(str(message.content), chunk=chunk)
        yield chunk

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        result = self._generate(messages, stop=stop, **kwargs)
        message = result.generations[0].message
        chunk = ChatGenerationChunk(
            message=AIMessageChunk(
                content=message.content, response_metadata=message.response_metadata
            )
        )
        if run_manager is not None:
            run_manager.on_llm_new_token(str(message.content), chunk=chunk)
        yield chunk


class OMPLanguageModel(LanguageModel):
    @property
    def provider(self):
        return "omp"

    def _get_default_model(self):
        return default_omp_model()

    def _get_models(self):
        return [
            Model(
                id=self.model_name or self._get_default_model(), owned_by="OMP 本机账号"
            )
        ]

    def to_langchain(self):
        if self.tools or self.tool_choice not in (None, "none"):
            raise LocalModelError("OMP本机账号接入禁止工具调用。")
        return OMPChatModel(
            model_name=validate_model_name(
                self.model_name or self._get_default_model()
            ),
            max_tokens=self.max_tokens,
        )

    async def achat_complete(
        self,
        messages,
        stream=None,
        tools=None,
        tool_choice=None,
        parallel_tool_calls=None,
        validate_tool_calls=False,
        max_tokens=None,
        temperature=None,
        top_p=None,
    ):
        if (
            tools
            or self.tools
            or tool_choice not in (None, "none")
            or self.tool_choice not in (None, "none")
        ):
            raise LocalModelError("OMP本机账号接入禁止工具调用。")
        result = await gateway_complete(
            messages,
            model=self.model_name or self._get_default_model(),
            max_tokens=self.max_tokens if max_tokens is None else max_tokens,
        )
        if self.streaming if stream is None else stream:

            async def buffered():
                yield _completion_chunk(result)

            return buffered()
        return result

    def chat_complete(self, messages, stream=None, **kwargs):
        result = asyncio.run(self.achat_complete(messages, stream=False, **kwargs))
        if self.streaming if stream is None else stream:
            return iter([_completion_chunk(result)])
        return result


def _completion_chunk(result: ChatCompletion) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id=result.id,
        model=result.model,
        created=result.created or 0,
        choices=[
            {
                "index": 0,
                "finish_reason": "stop",
                "delta": {"role": "assistant", "content": result.content},
            }
        ],
    )
