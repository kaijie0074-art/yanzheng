"""服务器内运行：绑定既有私有GPT代理，不读取或复制OAuth凭据。"""

from __future__ import annotations

import argparse
import json
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
API = "http://127.0.0.1:5055/api"
GATEWAY = "http://127.0.0.1:18080/v1"
EMBEDDING = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def private_text(path: Path) -> str:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise ValueError("配置文件必须为仅所有者可读的普通文件")
        return handle.read(65536)


def request(url: str, token: str, payload: dict | None = None, method: str = "GET"):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    with OPENER.open(req, timeout=30) as response:
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("配置响应超限")
        return json.loads(raw)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", required=True, help="已完成真实推理验证的代理模型标识"
    )
    args = parser.parse_args()
    values = dict(
        line.split("=", 1)
        for line in private_text(Path("/etc/yanzheng/runtime.env")).splitlines()
        if line and not line.startswith("#")
    )
    password = values["OPEN_NOTEBOOK_PASSWORD"]
    gateway_key = json.loads(
        private_text(Path("/etc/kaijie-codex-proxy/community-key.json"))
    )["key"]
    available = request(GATEWAY + "/models", gateway_key)
    if args.model not in {model["id"] for model in available["data"]}:
        raise ValueError("请求的模型不在代理当前模型列表中")
    credentials = request(API + "/credentials", password)
    credential = next(
        (item for item in credentials if item["name"] == "研证服务器私有GPT"), None
    )
    if credential:
        if (
            credential["provider"] != "openai_compatible"
            or credential["base_url"] != GATEWAY
        ):
            raise ValueError("同名凭据配置不同，停止自动覆盖")
    else:
        credential = request(
            API + "/credentials",
            password,
            {
                "name": "研证服务器私有GPT",
                "provider": "openai_compatible",
                "modalities": ["language"],
                "base_url": GATEWAY,
                "api_key": gateway_key,
            },
            "POST",
        )
    models = request(API + "/models", password)
    defaults = request(API + "/models/defaults", password)
    assignments = {}
    for name, provider, kind, linked, slot in (
        (
            args.model,
            "openai_compatible",
            "language",
            credential["id"],
            "default_chat_model",
        ),
        (EMBEDDING, "fastembed", "embedding", None, "default_embedding_model"),
    ):
        model = next(
            (
                item
                for item in models
                if item["name"] == name
                and item["provider"] == provider
                and item["type"] == kind
            ),
            None,
        )
        if model and model.get("credential") != linked:
            raise ValueError("同名模型绑定了其他凭据，停止自动覆盖")
        if model is None:
            model = request(
                API + "/models",
                password,
                {
                    "name": name,
                    "provider": provider,
                    "type": kind,
                    "credential": linked,
                },
                "POST",
            )
        if not defaults.get(slot):
            assignments[slot] = model["id"]
    if assignments:
        request(API + "/models/defaults", password, assignments, "PUT")
    print(
        json.dumps(
            {
                "registered_model": args.model,
                "defaults_assigned": sorted(assignments),
                "model_call_performed": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as error:
        raise SystemExit(f"模型初始化失败，HTTP {error.code}；响应正文不输出") from None
    except Exception as error:
        raise SystemExit(
            f"模型初始化失败：{type(error).__name__}；未输出秘密或原始异常"
        ) from None
