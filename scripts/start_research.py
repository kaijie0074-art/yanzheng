"""单入口本机运行器：不需要Docker，不改全局配置，不读取OMP账户凭据。"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
VERSION = "2.7.0"
ASSETS = {
    ("Darwin", "arm64"): (
        "darwin-arm64",
        "0594367f1e5fd5054441e4b18f9ef857dc5775383ff19533c0ec4fbb8d2d9cb3",
    ),
    ("Darwin", "x86_64"): (
        "darwin-amd64",
        "d2d8955ff5ce3c5cd24eeffb51dd3ff0a1006224f1b0c0f597f522dee87fb829",
    ),
    ("Linux", "x86_64"): (
        "linux-amd64",
        "c5f53be46e287d15302d3467c629ad8df016c96fcf15451077633949dadb2028",
    ),
    ("Linux", "aarch64"): (
        "linux-arm64",
        "cebf189d06baed895bce02db64f42a136edfa440ad38a56725db4d410cbf317e",
    ),
}
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def say(text: str):
    print(text, flush=True)


def prepare_environment() -> dict[str, str]:
    RUNTIME.mkdir(exist_ok=True)
    path = ROOT / ".env"
    if path.is_symlink():
        raise RuntimeError(".env不能是符号链接；未修改现有文件")
    if not path.exists():
        content = {
            "OPEN_NOTEBOOK_ENCRYPTION_KEY": secrets.token_hex(32),
            "SURREAL_URL": "ws://127.0.0.1:8000/rpc",
            "SURREAL_USER": "research",
            "SURREAL_PASSWORD": secrets.token_hex(24),
            "SURREAL_NAMESPACE": "research_notebook",
            "SURREAL_DATABASE": "research_notebook",
            "CORS_ORIGINS": "http://127.0.0.1:3000,http://localhost:3000",
            "RESEARCH_LOCAL_ONLY": "1",
            "RESEARCH_OMP_MODEL": "openai-codex/gpt-6.1-sol",
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS": "1",
            "OPEN_NOTEBOOK_MAX_UPLOAD_SIZE_MB": "21",
            "API_URL": "http://127.0.0.1:3000",
            "INTERNAL_API_URL": "http://127.0.0.1:5055",
            "NO_PROXY": "localhost,127.0.0.1,::1",
        }
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("# 本机随机生成；禁止提交或分享，不包含OMP登录凭据\n")
            handle.write("".join(f"{key}={value}\n" for key, value in content.items()))
    from dotenv import dotenv_values

    config = {
        str(key): str(value)
        for key, value in dotenv_values(path).items()
        if value is not None
    }
    if config.get("SURREAL_URL") != "ws://127.0.0.1:8000/rpc":
        raise RuntimeError(
            "一键本机入口要求SURREAL_URL=ws://127.0.0.1:8000/rpc；外部数据库请使用上游独立服务启动方式"
        )
    if not config.get("SURREAL_PASSWORD") or not config.get(
        "OPEN_NOTEBOOK_ENCRYPTION_KEY"
    ):
        raise RuntimeError(".env缺少数据库密码或加密密钥；未覆盖已有设置")
    environment = {**os.environ, **config}
    environment.update(
        RESEARCH_LOCAL_ONLY="1",
        CORS_ORIGINS="http://127.0.0.1:3000,http://localhost:3000",
        API_URL="http://127.0.0.1:3000",
        INTERNAL_API_URL="http://127.0.0.1:5055",
    )
    environment["SURREAL_PASS"] = environment["SURREAL_PASSWORD"]
    return environment


def prepare_database() -> Path:
    asset = ASSETS.get((platform.system(), platform.machine()))
    if not asset:
        raise RuntimeError(
            "该本机入口支持macOS/Linux的arm64和x86_64；其他系统请使用上游源码/容器方式，勿运行不匹配的二进制"
        )
    binary = RUNTIME / "surreal"
    if binary.is_symlink():
        raise RuntimeError("数据库程序不能是符号链接")
    if binary.is_file():
        result = subprocess.run(
            [str(binary), "version"], capture_output=True, text=True, check=True
        )
        if f" {VERSION} " not in f" {result.stdout.strip()} ":
            raise RuntimeError("现有隔离数据库程序版本不匹配；未覆盖")
        return binary
    name = f"surreal-v{VERSION}.{asset[0]}.tgz"
    archive = RUNTIME / "downloads" / name
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        say("首次准备官方SurrealDB 2.7.0（约25MB，保存在本工程）…")
        url = f"https://github.com/surrealdb/surrealdb/releases/download/v{VERSION}/{name}"
        with HTTP.open(url, timeout=120) as response, archive.open("xb") as handle:
            shutil.copyfileobj(response, handle)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != asset[1]:
        raise RuntimeError("数据库下载的SHA-256与官方发布记录不一致，未执行")
    with tarfile.open(archive, "r:gz") as package:
        member = package.getmember("surreal")
        if not member.isfile() or member.size > 100 * 1024 * 1024:
            raise RuntimeError("数据库发布包成员无效，未解包")
        with package.extractfile(member) as source, binary.open("xb") as target:
            shutil.copyfileobj(source, target)
    binary.chmod(0o700)
    return binary


def port_busy(port: int) -> bool:
    with socket.socket() as connection:
        connection.settimeout(0.3)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def wait_ready(url: str, process: subprocess.Popen, label: str, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{label}启动失败；请查看.runtime/logs中的同名日志")
        try:
            with HTTP.open(url, timeout=3) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.3)
    raise RuntimeError(f"{label}未在限定时间就绪；请查看.runtime/logs中的同名日志")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="启动研证完整本机产品；Ctrl+C安全关闭全部本次启动的服务"
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="只准备本工程配置和数据库程序，不启动服务",
    )
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument(
        "--dev", action="store_true", help="开发模式；正常使用默认采用生产构建"
    )
    args = parser.parse_args()
    os.chdir(ROOT)
    environment = prepare_environment()
    database = prepare_database()
    if args.prepare_only:
        say("本机隔离配置与官方数据库已准备；未启动服务、未调用AI")
        return 0
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("需要Node.js 20.9或更新版本（含npm），请安装后重开本入口")
    for port in (8000, 5055, 3000):
        if port_busy(port):
            raise RuntimeError(
                f"本机端口{port}已被占用；请关闭已有研证窗口或对应服务后重开，不会擅自结束其他程序"
            )
    frontend = ROOT / "frontend"
    package_hash = hashlib.sha256(
        (frontend / "package-lock.json").read_bytes()
    ).hexdigest()
    marker = RUNTIME / "npm-lock.sha256"
    if (
        not (frontend / "node_modules").is_dir()
        or not marker.exists()
        or marker.read_text() != package_hash
    ):
        say("准备锁定的网页依赖…")
        subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=frontend, check=True)
        marker.write_text(package_hash)
    # This is a local ONNX model, not a paid language-model request.
    say("检查本地多语言检索模型（首次下载约220MB；不发送文档）…")
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from open_notebook.ai.local_embedding import prepare_embedding; prepare_embedding(allow_download=True)",
        ],
        cwd=ROOT,
        env=environment,
        check=True,
        timeout=600,
    )
    if not args.dev:
        build_hash = hashlib.sha256((ROOT / "frontend/package-lock.json").read_bytes())
        for path in (
            frontend / "package.json",
            frontend / "next.config.ts",
            frontend / "tsconfig.json",
        ):
            build_hash.update(str(path.relative_to(frontend)).encode())
            build_hash.update(path.read_bytes())
        for path in sorted((frontend / "src").rglob("*")):
            if path.is_file():
                build_hash.update(str(path.relative_to(frontend)).encode())
                build_hash.update(path.read_bytes())
        marker = RUNTIME / "frontend-build.sha256"
        digest = build_hash.hexdigest()
        if (
            not (frontend / ".next/BUILD_ID").is_file()
            or not marker.exists()
            or marker.read_text() != digest
        ):
            say("构建可直接使用的网页…")
            subprocess.run(
                [npm, "run", "build"], cwd=frontend, env=environment, check=True
            )
            marker.write_text(digest)
    children: list[subprocess.Popen] = []
    handles = []
    logs = RUNTIME / "logs"
    logs.mkdir(exist_ok=True)

    def launch(label, command, cwd=ROOT):
        log = (logs / f"{label}.log").open("wb")
        handles.append(log)
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        children.append(process)
        return process

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        (ROOT / "data").mkdir(exist_ok=True)
        say("启动本机数据库…")
        db = launch(
            "database",
            [
                str(database),
                "start",
                "--no-banner",
                "--bind",
                "127.0.0.1:8000",
                "--deny-guests",
                "--deny-scripting",
                "--deny-net",
                "--log",
                "warn",
                "surrealkv://data/surreal",
            ],
        )
        wait_ready("http://127.0.0.1:8000/health", db, "database")
        say("启动研究API与处理队列…")
        api = launch(
            "api",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "5055",
            ],
        )
        wait_ready("http://127.0.0.1:5055/health", api, "api")
        launch(
            "worker",
            [
                str(ROOT / ".venv/bin/surreal-commands-worker"),
                "--import-modules",
                "commands",
                "--max-tasks",
                "1",
            ],
        )
        if shutil.which("omp"):
            request = urllib.request.Request(
                "http://127.0.0.1:5055/api/local-model/setup",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            if environment.get("OPEN_NOTEBOOK_PASSWORD"):
                request.add_header(
                    "Authorization", "Bearer " + environment["OPEN_NOTEBOOK_PASSWORD"]
                )
            with HTTP.open(request, timeout=30) as response:
                response.read()
        command = (
            [npm, "run", "dev", "--", "--hostname", "127.0.0.1", "--port", "3000"]
            if args.dev
            else [
                npm,
                "exec",
                "--",
                "next",
                "start",
                "--hostname",
                "127.0.0.1",
                "--port",
                "3000",
            ]
        )
        web = launch("web", command, frontend)
        wait_ready("http://127.0.0.1:3000/research", web, "web", timeout=180)
        say("研证已就绪 http://127.0.0.1:3000/research")
        say("请保留本窗口；按Ctrl+C关闭。材料只保存在本工程data中，不要提交到GitHub。")
        if not args.no_open:
            webbrowser.open("http://127.0.0.1:3000/research")
        while True:
            for child in children:
                if child.poll() is not None:
                    raise RuntimeError(
                        "一个产品服务已退出；已关闭其余本次启动的服务，请查看.runtime/logs"
                    )
            time.sleep(1)
    except KeyboardInterrupt:
        say("正在安全关闭本次启动的服务…")
    finally:
        for child in reversed(children):
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        for handle in handles:
            handle.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        say(f"未能启动：{error}")
        raise SystemExit(1)
