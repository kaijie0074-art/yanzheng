"""非秘密模型配置；API Key只在调用者内存或专用环境变量中存在。"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

from .models import ModelConfig


class SettingsError(ValueError):
    """可向界面展示的固定配置错误，不包含用户输入或文件路径。"""


def resolve_api_key(
    input_key: str | None = None, *, environ: Mapping[str, str] | None = None
) -> str | None:
    """网页非空输入优先；空白输入使用RESEARCH_API_KEY，不读取其他凭据。"""
    supplied = (input_key or "").strip()
    if supplied:
        return supplied
    environment = os.environ if environ is None else environ
    return environment.get("RESEARCH_API_KEY", "").strip() or None


def _api_url_issue(value: str) -> str | None:
    if not value:
        return "API_BASE_URL_REQUIRED"
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        return "API_BASE_URL_INVALID"
    try:
        parsed = urlsplit(value)
        # 访问port也会验证非数字或越界端口。
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        return "API_BASE_URL_INVALID"
    if not host or port == 0:
        return "API_BASE_URL_INVALID"
    if (
        parsed.username is not None
        or parsed.password is not None
        or "?" in value
        or "#" in value
    ):
        return "API_BASE_URL_CREDENTIALS_OR_QUERY_FORBIDDEN"
    if parsed.scheme == "https":
        return None
    if parsed.scheme == "http" and host.lower() in {"localhost", "127.0.0.1", "::1"}:
        return None
    return "API_BASE_URL_HTTPS_REQUIRED"


def configuration_issue(config: ModelConfig) -> str | None:
    """返回阻止调用的固定错误类型；无鉴权本机API不要求Key。"""
    if config.backend == "local_account":
        return None
    issue = _api_url_issue(config.api_base_url)
    if issue:
        return issue
    if not config.api_model.strip():
        return "API_MODEL_REQUIRED"
    if config.api_model != config.api_model.strip() or any(
        ord(character) < 32 or ord(character) == 127 for character in config.api_model
    ):
        return "API_MODEL_INVALID"
    return None


def can_call_model(config: ModelConfig) -> bool:
    """供按钮disabled使用；只判断配置完整性，不产生网络请求。"""
    return configuration_issue(config) is None


def _validate_persistable(config: ModelConfig) -> ModelConfig:
    # 重新验证，防止调用方使用model_copy(update=...)绕过字段校验。
    validated = ModelConfig.model_validate(config.model_dump())
    # 允许保存尚未填完的API配置，但不能把URL凭据写进非秘密配置文件。
    if validated.api_base_url:
        issue = _api_url_issue(validated.api_base_url)
        if issue:
            raise SettingsError(issue)
    return validated


def load_config(data_dir: str | Path = "data") -> ModelConfig:
    """缺失文件返回默认配置；损坏或未知字段必须报错，不能悄悄重置。"""
    path = Path(data_dir) / "model_config.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ModelConfig()
    config = ModelConfig.model_validate_json(raw)
    return _validate_persistable(config)


def save_config(config: ModelConfig, data_dir: str | Path = "data") -> None:
    """原子保存ModelConfig；接口不接收API Key，也不读取任何环境变量。"""
    validated = _validate_persistable(config)
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "model_config.json"
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=".model_config-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(
                validated.model_dump(mode="json"), handle, ensure_ascii=False, indent=2
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
