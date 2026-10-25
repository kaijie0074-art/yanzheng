"""默认仅检查环境；只有显式--live才会发起一次可能计费的模型调用。"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from .backends import BackendError, build_backend
from .models import DiagnosticOutput, ModelConfig
from .settings import configuration_issue, load_config, resolve_api_key

_BACKEND_ERRORS = frozenset(
    {
        "CONFIGURATION",
        "GATEWAY_NOT_FOUND",
        "GATEWAY_START",
        "GATEWAY_PROTOCOL",
        "GATEWAY_EXIT",
        "OUTPUT_LIMIT",
        "TIMEOUT",
        "NETWORK",
        "AUTHENTICATION",
        "RATE_LIMIT",
        "REQUEST_REJECTED",
        "MODEL_UNAVAILABLE",
        "HTTP_ERROR",
        "RESPONSE_JSON",
        "RESPONSE_FORMAT",
        "MODEL_MISMATCH",
        "TOOL_CALL",
        "TRUNCATED",
        "REFUSAL",
        "CONTENT_JSON",
        "SCHEMA_VALIDATION",
        "INVALID_PAYLOAD",
    }
)
_SUCCESS_RESULTS = {"READY_NOT_CALLED", "SUCCESS"}


def _safe_model(value: Any, api_key: str | None) -> str | None:
    # 端点返回的model字段也不能成为输出任意正文、控制字符或已知Key的通道。
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9._/\-:@+]{1,256}", value
    ):
        return None
    if api_key and api_key in value:
        return None
    return value


def run_diagnostic(
    config: ModelConfig, *, live: bool = False, api_key: str | None = None
) -> dict[str, str | None]:
    """统一供CLI和设置页使用；返回值不含模型正文、异常原文、路径或usage。"""
    key = resolve_api_key(api_key)
    requested = (
        config.local_model if config.backend == "local_account" else config.api_model
    )
    result: dict[str, str | None] = {
        "backend": config.backend,
        "requested_model": _safe_model(requested, key),
        "actual_model": None,
        "result": "READY_NOT_CALLED",
    }
    issue = configuration_issue(config)
    if issue:
        result["result"] = issue
        return result
    if not live:
        if config.backend == "local_account" and shutil.which("omp") is None:
            result["result"] = "GATEWAY_NOT_FOUND"
        return result
    try:
        backend = build_backend(config, api_key=key)
        reply = backend.complete_json(
            system="这是连通性诊断。仅返回符合给定JSON Schema的JSON对象，不调用工具，不包含其他文本。",
            payload={"task": "请返回对象，其ok字段为true。"},
            schema=DiagnosticOutput,
        )
        result["actual_model"] = _safe_model(reply.actual_model, key)
        # 即便未来替换适配器，诊断本身也不把错误结构或错误模型标为成功。
        DiagnosticOutput.model_validate(reply.value.model_dump())
        if (
            reply.actual_model != requested
            or reply.requested_model != requested
            or reply.backend != config.backend
        ):
            result["result"] = "MODEL_MISMATCH"
        else:
            result["result"] = "SUCCESS"
    except BackendError as error:
        result["actual_model"] = _safe_model(error.actual_model, key)
        result["result"] = (
            error.code if error.code in _BACKEND_ERRORS else "BACKEND_ERROR"
        )
    except ValidationError:
        result["result"] = "SCHEMA_VALIDATION"
    except Exception:
        # 刻意不输出traceback/str(error)：上游异常可能包含HTTP头或本地账户路径。
        result["result"] = "DIAGNOSTIC_ERROR"
    return result


class _ArgumentError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse默认会复述未知参数；这里只输出固定类型，避免误传的Key落到日志。
        raise _ArgumentError


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(
        description="研证模型诊断。默认不调用模型；--live发起一次真实请求，可能产生费用。"
    )
    parser.add_argument("--backend", choices=("local_account", "openai_compatible"))
    parser.add_argument("--live", action="store_true", help="执行真实短JSON请求")
    parser.add_argument(
        "--data-dir", default="data", help="仅指定诊断读取配置的位置，不在输出中显示"
    )
    try:
        args = parser.parse_args(argv)
    except _ArgumentError:
        print(
            json.dumps(
                {
                    "backend": None,
                    "requested_model": None,
                    "actual_model": None,
                    "result": "ARGUMENT_ERROR",
                }
            )
        )
        return 2
    try:
        config = load_config(args.data_dir)
        if args.backend:
            config = ModelConfig.model_validate(
                {**config.model_dump(), "backend": args.backend}
            )
    except Exception:
        print(
            json.dumps(
                {
                    "backend": args.backend,
                    "requested_model": None,
                    "actual_model": None,
                    "result": "CONFIGURATION",
                }
            )
        )
        return 2
    result = run_diagnostic(config, live=args.live)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["result"] in _SUCCESS_RESULTS else 1


if __name__ == "__main__":
    raise SystemExit(main())
