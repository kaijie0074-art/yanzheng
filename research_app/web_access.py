"""无密钥的必应公开网页搜索与有界、无凭据的公网正文读取。

必应HTML不是稳定API；结构改变或访问挑战时明确失败，不自动切换服务。
网页是待核验的不可信材料，解析器只提取文字，不执行脚本或网页指令。
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import queue
import re
import socket
import ssl
import threading
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit, urlunsplit

import httpcore
import httpx

from .discovery import _bounded_bytes, _FetchError

SEARCH_URL = "https://www.bing.com/search"
SEARCH_HOSTS = frozenset({"www.bing.com", "cn.bing.com"})
SEARCH_PROVIDER = "bing"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARACTERS = 60000
REQUEST_TIMEOUT = 12.0
TRANSFER_SECONDS = 40.0
MAX_REDIRECTS = 4
MAX_DNS_LOOKUPS = 4
MAX_HTML_DEPTH = 128
MAX_HTML_NODES = 20000
_DNS_SLOTS = threading.BoundedSemaphore(MAX_DNS_LOOKUPS)
_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_IGNORED = frozenset(
    {
        "script",
        "style",
        "noscript",
        "svg",
        "canvas",
        "nav",
        "header",
        "footer",
        "form",
        "template",
        "iframe",
        "object",
    }
)
_BLOCKS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "main",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "tr",
        "blockquote",
        "pre",
        "br",
    }
)
_LOGIN_PATH = re.compile(
    r"(?:^|/)(?:login|signin|sign-in|log-in|oauth|authorize|sso)(?:/|$)", re.I
)
_CHALLENGE = re.compile(
    r"(?:verify (?:that )?you are human|checking your browser|just a moment|access denied|unusual traffic|enable javascript and cookies to continue|人机验证|安全验证|访问验证|请输入验证码|访问被拒绝)",
    re.I,
)
_LOGIN_WALL = re.compile(
    r"(?:(?:sign|log) in to (?:continue|read|access)|登录后(?:阅读|查看|访问)|请先登录|登录以继续|subscribe to (?:continue|read))",
    re.I,
)


class WebAccessError(ValueError):
    """仅暴露安全中文说明，不回传底层地址、代理或服务器错误正文。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_url(url: str) -> tuple[str, str]:
    try:
        if not isinstance(url, str) or not url or len(url) > 8192:
            raise ValueError("invalid URL")
        parts = urlsplit(url)
        host = parts.hostname
        if (
            parts.scheme != "https"
            or not host
            or parts.port not in {None, 443}
            or parts.username is not None
            or parts.password is not None
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
            or "\\" in url
            or "%" in host
        ):
            raise ValueError("unsafe URL")
        host = host.encode("idna").decode("ascii").lower()
        if host.endswith(".") or _LOGIN_PATH.search(unquote(parts.path)):
            raise ValueError("login or ambiguous host")
        if host.split(".")[0] in {
            "login",
            "signin",
            "accounts",
            "account",
            "auth",
            "sso",
        }:
            raise ValueError("login host")
        authority = f"[{host}]" if ":" in host else host
        # fragment不会发送给服务器；统一移除，避免同页重复以及含片段的跳转歧义。
        return urlunsplit(
            ("https", authority, parts.path or "/", parts.query, "")
        ), host
    except (ValueError, UnicodeError) as error:
        raise WebAccessError(
            "UNSAFE_URL",
            "仅允许无账户信息、标准443端口的HTTPS公开网页，不读取登录入口。",
        ) from error


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # 禁止IPv4映射、NAT64、6to4、Teredo和特殊用途段，防止地址转换抵达内网。
        return (
            ip.ipv4_mapped is None
            and ip in ipaddress.ip_network("2000::/3")
            and ip not in ipaddress.ip_network("2001::/23")
            and ip not in ipaddress.ip_network("2002::/16")
            and ip not in ipaddress.ip_network("3fff::/20")
        )
    return True


def _resolve(host: str, deadline: float) -> list:
    # 系统getaddrinfo不能取消；超时后仍占用槽位，直到真实解析返回才释放。
    remaining = min(REQUEST_TIMEOUT, deadline - time.monotonic())
    if remaining <= 0:
        raise WebAccessError("TIMEOUT", "网页读取超过总时限，已停止。")
    slots = _DNS_SLOTS
    if not slots.acquire(blocking=False):
        raise WebAccessError(
            "DNS_BUSY", "公开网页域名解析资源已占满，请等待已有解析结束后主动重试。"
        )
    result: queue.Queue = queue.Queue(maxsize=1)

    def lookup() -> None:
        try:
            try:
                result.put_nowait(
                    socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
                )
            except OSError:
                result.put_nowait(None)
        finally:
            slots.release()

    try:
        threading.Thread(target=lookup, daemon=True, name="web-research-dns").start()
    except RuntimeError as error:
        slots.release()
        raise WebAccessError(
            "DNS_BUSY", "无法启动受限域名解析，请稍后主动重试。"
        ) from error
    try:
        addresses = result.get(timeout=remaining)
    except queue.Empty as error:
        raise WebAccessError(
            "TIMEOUT", "公开网页域名解析超时，请稍后主动重试。"
        ) from error
    if not addresses:
        raise WebAccessError("DNS", "公开网页域名解析失败，请稍后主动重试。")
    return addresses


def _public_target(url: str, deadline: float) -> tuple[str, str, str]:
    safe, host = _safe_url(url)
    addresses = _resolve(host, deadline)
    ips = [address[4][0] for address in addresses]
    if not ips or any(not _is_public(ip) for ip in ips):
        raise WebAccessError(
            "UNSAFE_ADDRESS", "网页域名解析包含非公网或特殊用途地址，已阻止访问。"
        )
    ip = ips[0]
    authority = f"[{ip}]" if ":" in ip else ip
    parts = urlsplit(safe)
    return urlunsplit(("https", authority, parts.path, parts.query, "")), host, safe


def _io_timeout(deadline: float, timeout: float | None) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise httpcore.TimeoutException("公开网页读取超过绝对总时限")
    return min(remaining, REQUEST_TIMEOUT if timeout is None else timeout)


class _DeadlineStream(httpcore.NetworkStream):
    """每次底层I/O重新计算绝对剩余时间，超时立即关闭实际socket。"""

    def __init__(self, sock: socket.socket, deadline: float):
        self.sock = sock
        self.deadline = deadline

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        try:
            self.sock.settimeout(_io_timeout(self.deadline, timeout))
            data = self.sock.recv(max_bytes)
            _io_timeout(self.deadline, timeout)
            return data
        except (socket.timeout, httpcore.TimeoutException) as error:
            self.close()
            raise httpcore.ReadTimeout("公开网页读取超时") from error
        except OSError as error:
            self.close()
            raise httpcore.ReadError("公开网页读取失败") from error

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        view = memoryview(buffer)
        try:
            while view:
                self.sock.settimeout(_io_timeout(self.deadline, timeout))
                sent = self.sock.send(view)
                if sent == 0:
                    raise OSError("连接已关闭")
                view = view[sent:]
            _io_timeout(self.deadline, timeout)
        except (socket.timeout, httpcore.TimeoutException) as error:
            self.close()
            raise httpcore.WriteTimeout("公开网页请求写入超时") from error
        except OSError as error:
            self.close()
            raise httpcore.WriteError("公开网页请求写入失败") from error

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        try:
            self.sock.settimeout(_io_timeout(self.deadline, timeout))
            self.sock = ssl_context.wrap_socket(
                self.sock,
                server_hostname=server_hostname,
                do_handshake_on_connect=False,
            )
            self.sock.settimeout(_io_timeout(self.deadline, timeout))
            self.sock.do_handshake()
            _io_timeout(self.deadline, timeout)
            return self
        except (socket.timeout, httpcore.TimeoutException) as error:
            self.close()
            raise httpcore.ConnectTimeout("公开网页TLS握手超时") from error
        except OSError as error:
            self.close()
            raise httpcore.ConnectError("公开网页TLS握手失败") from error

    def close(self) -> None:
        self.sock.close()

    def get_extra_info(self, info: str):
        if info == "socket":
            return self.sock
        if info == "ssl_object":
            return self.sock if isinstance(self.sock, ssl.SSLSocket) else None
        return None


class _DeadlineBackend(httpcore.NetworkBackend):
    def __init__(self, deadline: float):
        self.deadline = deadline

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.NetworkStream:
        # 上游只传已经核验的数字IP；直接socket.connect，不再次进入DNS。
        address = ipaddress.ip_address(host)
        sock = socket.socket(
            socket.AF_INET6 if address.version == 6 else socket.AF_INET,
            socket.SOCK_STREAM,
        )
        try:
            sock.settimeout(_io_timeout(self.deadline, timeout))
            for option in socket_options or ():
                sock.setsockopt(*option)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if local_address is not None:
                sock.bind((local_address, 0))
            sock.connect((str(address), port))
            _io_timeout(self.deadline, timeout)
            return _DeadlineStream(sock, self.deadline)
        except (socket.timeout, httpcore.TimeoutException) as error:
            sock.close()
            raise httpcore.ConnectTimeout("公开网页连接超时") from error
        except OSError as error:
            sock.close()
            raise httpcore.ConnectError("公开网页连接失败") from error


class _CoreResponseStream(httpx.SyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    def __iter__(self):
        yield from self.stream

    def close(self) -> None:
        self.stream.close()


class _DeadlineTransport(httpx.BaseTransport):
    def __init__(self, deadline: float):
        self.pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            network_backend=_DeadlineBackend(deadline),
            max_connections=1,
            max_keepalive_connections=0,
            http1=True,
            http2=False,
            retries=0,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.pool.handle_request(
            httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
        )
        return httpx.Response(
            response.status,
            headers=response.headers,
            stream=_CoreResponseStream(response.stream),
            extensions=response.extensions,
        )

    def close(self) -> None:
        self.pool.close()


def _fetch(
    url: str, client: httpx.Client | None, *, search: bool = False
) -> tuple[bytes, str, str]:
    deadline = time.monotonic() + TRANSFER_SECONDS
    for hop in range(MAX_REDIRECTS + 1):
        safe, host = _safe_url(url)
        if search and host not in SEARCH_HOSTS:
            raise WebAccessError("REDIRECT", "搜索服务跳转到未授权站点，未继续查询。")
        pinned, host, safe = _public_target(safe, deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WebAccessError("TIMEOUT", "网页读取超过总时限，已停止。")
        # 每一跳独立建连：即便CDN IP相同，也重新校验本跳TLS原域名。
        context = (
            nullcontext(client)
            if client is not None
            else httpx.Client(
                trust_env=False,
                follow_redirects=False,
                transport=_DeadlineTransport(deadline),
            )
        )
        try:
            with context as http:
                # 不使用client.build_request，避免继承默认Cookie、认证头或查询参数。
                request = httpx.Request(
                    "GET",
                    pinned,
                    headers={
                        "Host": host,
                        "Accept": "text/html, text/plain;q=0.9, application/xhtml+xml;q=0.8",
                        "Accept-Encoding": "identity",
                        "User-Agent": "ResearchEvidenceWorkbench/1.0",
                        "Connection": "close",
                    },
                    extensions={
                        "sni_hostname": host,
                        "timeout": httpx.Timeout(
                            min(REQUEST_TIMEOUT, remaining)
                        ).as_dict(),
                    },
                )
                response = http.send(
                    request, stream=True, follow_redirects=False, auth=None
                )
                try:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or hop == MAX_REDIRECTS:
                            raise WebAccessError(
                                "REDIRECT", "网页跳转缺少地址或次数超限，已停止。"
                            )
                        url = urljoin(safe, location)
                        continue
                    if response.status_code == 429:
                        raise WebAccessError(
                            "RATE_LIMIT",
                            "公开服务限制请求（429），请稍后主动重试；没有自动更换服务。",
                        )
                    if response.status_code in {401, 403}:
                        raise WebAccessError(
                            "ACCESS_DENIED",
                            "站点拒绝公开访问或要求登录，未绕过访问限制。",
                        )
                    if response.status_code != 200:
                        raise WebAccessError(
                            "HTTP",
                            f"公开网页返回HTTP {response.status_code}，未取得正文。",
                        )
                    content_type = response.headers.get("content-type", "")
                    mime = content_type.partition(";")[0].strip().lower()
                    if mime not in {"text/html", "application/xhtml+xml", "text/plain"}:
                        raise WebAccessError(
                            "NOT_WEBPAGE",
                            "地址未返回HTML或纯文本网页；PDF、二进制和未知类型不进入联网回答。",
                        )
                    data = _bounded_bytes(response, MAX_RESPONSE_BYTES, deadline)
                    return data, safe, content_type
                finally:
                    response.close()
        except WebAccessError:
            raise
        except _FetchError as error:
            raise WebAccessError(error.code, str(error)) from error
        except (httpx.TimeoutException, httpcore.TimeoutException) as error:
            raise WebAccessError(
                "TIMEOUT", "公开网页请求超时，请稍后主动重试。"
            ) from error
        except (
            httpx.HTTPError,
            httpcore.NetworkError,
            httpcore.ProtocolError,
            OSError,
        ) as error:
            raise WebAccessError(
                "NETWORK", "公开网页连接失败，未取得可读材料；没有自动重试或更换服务。"
            ) from error
    raise WebAccessError("REDIRECT", "网页跳转次数超限，已停止。")


def _decode(data: bytes, content_type: str) -> str:
    if (
        not data
        or data.lstrip().startswith((b"%PDF-", b"PK\x03\x04", b"\x89PNG", b"GIF8"))
        or b"\x00" in data
    ):
        raise WebAccessError(
            "NOT_WEBPAGE", "响应为空或实际为二进制文件，未作为网页正文。"
        )
    match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type, re.I)
    if match is None:
        match = re.search(
            r"charset\s*=\s*[\"']?([\w-]+)", data[:4096].decode("ascii", "ignore"), re.I
        )
    encoding = match.group(1).lower() if match else "utf-8"
    if encoding not in {
        "utf-8",
        "utf8",
        "us-ascii",
        "ascii",
        "gbk",
        "gb2312",
        "gb18030",
        "big5",
        "iso-8859-1",
        "windows-1252",
    }:
        raise WebAccessError(
            "ENCODING", "网页声明了不支持的文字编码，未猜测或伪造正文。"
        )
    try:
        text = data.decode(encoding, errors="strict")
    except UnicodeError as error:
        raise WebAccessError(
            "ENCODING", "网页文字编码损坏或与声明不符，未生成正文。"
        ) from error
    if any(ord(char) < 32 and char not in "\r\n\t" for char in text):
        raise WebAccessError("NOT_WEBPAGE", "响应含二进制控制字符，未作为网页正文。")
    return text


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class _LimitedHTMLParser(HTMLParser):
    """在任何深栈扫描之前限制解析工作量；超限不返回部分结果。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.node_count = 0

    def _consume_node(self, depth: int = 0) -> None:
        self.node_count += 1
        if depth > MAX_HTML_DEPTH or self.node_count > MAX_HTML_NODES:
            raise WebAccessError(
                "HTML_LIMIT",
                "网页HTML嵌套或节点数量超过安全上限，已停止解析，不使用残缺正文。",
            )

    def handle_comment(self, data):
        self._consume_node()

    def handle_decl(self, decl):
        self._consume_node()

    def handle_pi(self, data):
        self._consume_node()

    def unknown_decl(self, data):
        self._consume_node()


class _PageText(_LimitedHTMLParser):
    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, bool]] = []
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.blocked = False
        self.password = False

    def handle_starttag(self, tag, attrs):
        self._consume_node(len(self.stack) + (tag not in _VOID))
        attributes = dict(attrs)
        parent_hidden = bool(self.stack and self.stack[-1][1])
        style = re.sub(r"\s+", "", attributes.get("style") or "").lower()
        hidden = (
            parent_hidden
            or tag in _IGNORED
            or tag == "head"
            or "hidden" in attributes
            or attributes.get("aria-hidden") == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        )
        marker = " ".join((attributes.get("id") or "", attributes.get("class") or ""))
        if re.search(
            r"(?:g-recaptcha|h-captcha|cf-chl|cf-turnstile|challenge-form|captcha-container)",
            marker,
            re.I,
        ):
            self.blocked = True
        if tag == "input" and (attributes.get("type") or "").lower() == "password":
            self.password = True
        if tag in _BLOCKS and not hidden:
            self.parts.append("\n")
        if tag not in _VOID:
            self.stack.append((tag, hidden))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        self._consume_node()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                hidden = self.stack[index][1]
                del self.stack[index:]
                if tag in _BLOCKS and not hidden:
                    self.parts.append("\n")
                break

    def handle_data(self, data):
        self._consume_node()
        if any(tag == "title" for tag, _ in self.stack):
            self.title_parts.append(data)
        elif not self.stack or not self.stack[-1][1]:
            self.parts.append(data)

    @property
    def title(self) -> str:
        return _clean("".join(self.title_parts))[:1000]

    @property
    def text(self) -> str:
        return "\n".join(
            line for part in "".join(self.parts).splitlines() if (line := _clean(part))
        )

    def require_readable(self, *, minimum: int = 30) -> None:
        text = self.text
        if (
            self.blocked
            or _CHALLENGE.search(self.title)
            or _CHALLENGE.search(text[:4000])
        ):
            raise WebAccessError(
                "CHALLENGE", "站点返回验证码或访问挑战，未将其当作搜索结果或正文。"
            )
        if (
            self.password
            or re.search(
                r"^(?:sign in|log in|登录|登陆)(?:\s|$|[-—|])", self.title, re.I
            )
            or _LOGIN_WALL.search(text[:4000])
        ):
            raise WebAccessError(
                "LOGIN_REQUIRED",
                "网页要求登录，未将登录表单当作正文，也不会继承登录态。",
            )
        if not re.search(r"\w", text) or len(text) < minimum:
            raise WebAccessError(
                "EMPTY_PAGE", "网页没有足够可读正文，可能仅为脚本页面；未声称已读全文。"
            )


class _BingResults(_LimitedHTMLParser):
    """只接受b_results内b_algo条目的h2标题链接，不把导航广告当搜索。"""

    def __init__(self):
        super().__init__()
        self.stack: list[str] = []
        self.results_depth: int | None = None
        self.item_depth: int | None = None
        self.h2_depth: int | None = None
        self.paragraph_depth: int | None = None
        self.link_depth: int | None = None
        self.item: dict | None = None
        self.items: list[dict] = []
        self.saw_results = False
        self.no_results = False
        self.query: str | None = None

    def handle_starttag(self, tag, attrs):
        self._consume_node(len(self.stack) + (tag not in _VOID))
        attributes = dict(attrs)
        if tag not in _VOID:
            self.stack.append(tag)
        depth = len(self.stack)
        if tag == "input" and attributes.get("name") == "q":
            self.query = attributes.get("value")
        if attributes.get("id") == "b_results":
            self.results_depth = depth
            self.saw_results = True
        if (
            self.results_depth is not None
            and "b_no" in (attributes.get("class") or "").split()
        ):
            self.no_results = True
        if (
            self.results_depth is not None
            and tag == "li"
            and "b_algo" in (attributes.get("class") or "").split()
        ):
            self.item_depth = depth
            self.item = {"url": "", "title_parts": [], "snippet_parts": []}
        if self.item is not None:
            if tag == "h2":
                self.h2_depth = depth
            if tag == "a" and self.h2_depth is not None and not self.item["url"]:
                self.item["url"] = attributes.get("href") or ""
                self.link_depth = depth
            if tag == "p":
                self.paragraph_depth = depth
                self.item["snippet_parts"].append(" ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        self._consume_node()
        if self.item is not None and not any(tag in _IGNORED for tag in self.stack):
            if self.link_depth is not None:
                self.item["title_parts"].append(data)
            if self.paragraph_depth is not None:
                self.item["snippet_parts"].append(data)

    def handle_endtag(self, tag):
        self._consume_node()
        if tag not in self.stack:
            return
        index = len(self.stack) - 1 - self.stack[::-1].index(tag)
        depth = index + 1
        if self.item_depth is not None and depth <= self.item_depth:
            if self.item is not None:
                self.items.append(self.item)
            self.item = None
            self.item_depth = None
        for field in ("h2_depth", "paragraph_depth", "link_depth", "results_depth"):
            current = getattr(self, field)
            if current is not None and depth <= current:
                setattr(self, field, None)
        del self.stack[index:]


def _result_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.hostname in SEARCH_HOSTS and parts.path == "/ck/a":
        encoded = parse_qs(parts.query).get("u", [""])[0]
        if not encoded.startswith("a1"):
            raise WebAccessError("SEARCH_FORMAT", "搜索服务返回无法识别的链接格式。")
        payload = encoded[2:]
        try:
            value = base64.b64decode(
                payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True
            ).decode("utf-8")
        except (ValueError, UnicodeError) as error:
            raise WebAccessError(
                "SEARCH_FORMAT", "搜索服务返回损坏的链接格式。"
            ) from error
    return _safe_url(value)[0]


def search_web(query: str, *, client: httpx.Client | None = None) -> dict:
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > 500
        or any(ord(char) < 32 for char in query)
    ):
        raise WebAccessError(
            "INVALID_QUERY", "公开网页检索式须为1至500字符，不能含控制字符。"
        )
    query = query.strip()
    searched_at = _now()
    data, _, content_type = _fetch(
        SEARCH_URL + "?" + urlencode({"q": query}), client, search=True
    )
    if content_type.partition(";")[0].strip().lower() not in {
        "text/html",
        "application/xhtml+xml",
    }:
        raise WebAccessError(
            "SEARCH_FORMAT", "搜索服务没有返回预期网页结果，未猜测结果。"
        )
    text = _decode(data, content_type)
    page = _PageText()
    page.feed(text)
    page.close()
    page.require_readable(minimum=1)
    parser = _BingResults()
    parser.feed(text)
    parser.close()
    if not parser.saw_results or (not parser.items and not parser.no_results):
        raise WebAccessError(
            "SEARCH_FORMAT",
            "搜索服务页面结构变化或没有可识别结果；未将普通网页当作搜索成功。",
        )
    if parser.query is None or _clean(parser.query) != _clean(query):
        raise WebAccessError(
            "SEARCH_FORMAT", "搜索服务未回显本次检索式，无法确认结果属于当前查询。"
        )
    results = []
    seen = set()
    for item in parser.items:
        title = _clean("".join(item["title_parts"]))
        if not title or not item["url"]:
            continue
        try:
            url = _result_url(item["url"])
        except (WebAccessError, ValueError):
            continue
        if url in seen:
            continue
        seen.add(url)
        results.append(
            {
                "url": url,
                "title": title[:1000],
                "snippet": _clean("".join(item["snippet_parts"]))[:2000],
            }
        )
        if len(results) == 5:
            break
    if parser.items and not results:
        raise WebAccessError(
            "SEARCH_FORMAT", "搜索结果没有可用的公开HTTPS标题链接，未生成替代来源。"
        )
    return {
        "provider": SEARCH_PROVIDER,
        "query": query,
        "searched_at": searched_at,
        "results": results,
    }


def read_page(url: str, *, client: httpx.Client | None = None) -> dict:
    original, _ = _safe_url(url)
    retrieved_at = _now()
    data, final_url, content_type = _fetch(original, client)
    decoded = _decode(data, content_type)
    if content_type.partition(";")[0].strip().lower() == "text/plain":
        text = decoded.strip()
        title = ""
        if _CHALLENGE.search(text[:4000]):
            raise WebAccessError("CHALLENGE", "站点返回访问挑战，未将其作为正文。")
        if _LOGIN_WALL.search(text[:4000]):
            raise WebAccessError(
                "LOGIN_REQUIRED", "站点要求登录或订阅后阅读，未将访问提示作为正文。"
            )
        if len(text) < 30 or not re.search(r"\w", text):
            raise WebAccessError("EMPTY_PAGE", "网页没有足够可读正文，未声称已读全文。")
    else:
        parser = _PageText()
        parser.feed(decoded)
        parser.close()
        parser.require_readable()
        text, title = parser.text, parser.title
    return {
        "url": original,
        "final_url": final_url,
        "title": title,
        "text": text[:MAX_TEXT_CHARACTERS],
        "extracted_characters": len(text),
        "truncated": len(text) > MAX_TEXT_CHARACTERS,
        "retrieved_at": retrieved_at,
        "content_sha256": hashlib.sha256(data).hexdigest(),
    }
