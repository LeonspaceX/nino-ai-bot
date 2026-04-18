import base64
import hashlib
import json
import socket
import struct
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import websocket


class LiteToolcallError(Exception):
    pass


DEFAULT_CONNECT_TIMEOUT_SECONDS = 15
DEFAULT_PROMPT_TIMEOUT_SECONDS = 20
DEFAULT_RUN_TIMEOUT_SECONDS = 60
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 10
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 15


def _preview(value: str, limit: int = 160) -> str:
    text = (value or "").replace("\r", "\\r").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


@dataclass
class LiteToolcallServerConfig:
    name: str
    enabled: bool
    connection_mode: str
    url: str
    token: str
    connect_timeout_seconds: int
    prompt_timeout_seconds: int
    run_timeout_seconds: int
    heartbeat_interval_seconds: int
    heartbeat_timeout_seconds: int

    @classmethod
    def from_dict(cls, item: dict, defaults: dict | None = None):
        defaults = defaults or {}
        return cls(
            name=str(item.get("name", "")).strip(),
            enabled=bool(item.get("enabled", True)),
            connection_mode=str(item.get("connection_mode", "forward")).strip().lower() or "forward",
            url=str(item.get("url", "")).strip(),
            token=str(item.get("token", "")),
            connect_timeout_seconds=_positive_int(
                item.get("connect_timeout_seconds", defaults.get("connect_timeout_seconds")),
                DEFAULT_CONNECT_TIMEOUT_SECONDS,
            ),
            prompt_timeout_seconds=_positive_int(
                item.get("prompt_timeout_seconds", defaults.get("prompt_timeout_seconds")),
                DEFAULT_PROMPT_TIMEOUT_SECONDS,
            ),
            run_timeout_seconds=_positive_int(
                item.get("run_timeout_seconds", defaults.get("run_timeout_seconds")),
                DEFAULT_RUN_TIMEOUT_SECONDS,
            ),
            heartbeat_interval_seconds=_positive_int(
                item.get("heartbeat_interval_seconds", defaults.get("heartbeat_interval_seconds")),
                DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
            ),
            heartbeat_timeout_seconds=_positive_int(
                item.get("heartbeat_timeout_seconds", defaults.get("heartbeat_timeout_seconds")),
                DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
            ),
        )


class _RawWebSocket:
    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, conn: socket.socket):
        self.conn = conn
        self.conn.settimeout(DEFAULT_CONNECT_TIMEOUT_SECONDS)
        self.closed = False

    @classmethod
    def accept(cls, conn: socket.socket):
        instance = cls(conn)
        instance._handshake()
        return instance

    def _handshake(self):
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = self.conn.recv(4096)
            if not chunk:
                raise LiteToolcallError("WebSocket 握手失败。")
            buffer += chunk
        header_text = buffer.decode("utf-8", errors="ignore")
        key = ""
        for line in header_text.splitlines():
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
                break
        if not key:
            raise LiteToolcallError("WebSocket 握手缺少 Sec-WebSocket-Key。")
        accept = base64.b64encode(hashlib.sha1((key + self.GUID).encode("ascii")).digest()).decode("ascii")
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        )
        self.conn.sendall(response.encode("ascii"))

    def send(self, text: str):
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length <= 0xFFFF:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))
        self.conn.sendall(bytes(header) + payload)

    def recv(self) -> str:
        while True:
            first = self._read_exact(2)
            opcode = first[0] & 0x0F
            masked = bool(first[1] & 0x80)
            length = first[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length) if length else b""
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                self.closed = True
                raise LiteToolcallError("WebSocket 已关闭。")
            if opcode == 0x9:
                self._send_control(0xA, payload)
                continue
            if opcode == 0x1:
                return payload.decode("utf-8", errors="replace")

    def settimeout(self, timeout: float):
        self.conn.settimeout(timeout)

    def _send_control(self, opcode: int, payload: bytes):
        if len(payload) > 125:
            payload = payload[:125]
        self.conn.sendall(bytes([0x80 | opcode, len(payload)]) + payload)

    def _read_exact(self, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = self.conn.recv(size - len(data))
            if not chunk:
                raise LiteToolcallError("WebSocket 连接已断开。")
            data += chunk
        return data

    def close(self):
        self.closed = True
        try:
            self.conn.close()
        except Exception:
            pass


class _ReverseListener:
    def __init__(self, url: str, on_socket):
        parsed = urlparse(url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 8765
        self.on_socket = on_socket
        self._server_socket = None
        self._thread = None
        self._stopped = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stopped.clear()
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self.host, self.port))
        self._server_socket.listen(5)
        self._server_socket.settimeout(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self):
        self._stopped.set()
        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception:
                pass
            self._server_socket = None

    def _serve(self):
        while not self._stopped.is_set():
            try:
                conn, _ = self._server_socket.accept()
            except socket.timeout:
                continue
            except Exception:
                break
            try:
                ws = _RawWebSocket.accept(conn)
                self.on_socket(ws)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass


class LiteToolcallConnection:
    def __init__(self, config: LiteToolcallServerConfig):
        self.config = config
        self._lock = threading.RLock()
        self._ws = None
        self._reverse_listener = None
        self._connected = False
        self._authed = False
        self._prompt = None
        self._last_message_at = 0
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = None

    def ensure_connected(self):
        if self.config.connection_mode == "reverse":
            self._ensure_reverse_listener()
            self._wait_reverse_connected()
            return
        self._ensure_forward_connected()

    def start(self):
        if self.config.connection_mode == "reverse":
            self._ensure_reverse_listener()
            return
        with self._lock:
            self._ensure_forward_connected()

    def get_prompt(self) -> str:
        with self._lock:
            self.ensure_connected()
            if self._prompt is not None:
                return self._prompt
            print(f"[Lite Toolcall] 请求工具文档 {self.config.name}")
            self._send({"action": "get_prompt"})
            data = self._recv_response(
                lambda item: "prompt" in item,
                self.config.prompt_timeout_seconds,
                "get_prompt",
            )
            self._prompt = str(data.get("prompt", ""))
            print(f"[Lite Toolcall] 工具文档已获取 {self.config.name}: {len(self._prompt)} 字符")
            return self._prompt

    def run(self, raw: str) -> dict:
        with self._lock:
            self.ensure_connected()
            started_at = time.time()
            print(f"[Lite Toolcall] 调用开始 {self.config.name}: raw_len={len(raw or '')}, raw={_preview(raw)}")
            self._send({"action": "run", "raw": raw})
            data = self._recv_response(lambda item: "status" in item, self.config.run_timeout_seconds, "run")
            elapsed = time.time() - started_at
            result = str(data.get("result", ""))
            has_image = "是" if data.get("img_base64") else "否"
            print(
                f"[Lite Toolcall] 调用完成 {self.config.name}: "
                f"status={data.get('status')}, elapsed={elapsed:.2f}s, "
                f"result_len={len(result)}, image={has_image}"
            )
            return data

    def close(self):
        with self._lock:
            if self._reverse_listener:
                self._reverse_listener.stop()
                self._reverse_listener = None
            self._heartbeat_stop.set()
            if self._ws:
                try:
                    self._ws.close()
                except Exception:
                    pass
            self._ws = None
            self._connected = False
            self._authed = False

    def _ensure_forward_connected(self):
        if self._connected and self._ws is not None:
            return
        print(f"[Lite Toolcall] 正向连接 {self.config.name}: {self.config.url}")
        self._ws = websocket.create_connection(self.config.url, timeout=self.config.connect_timeout_seconds)
        self._connected = True
        self._auth_and_hello()
        print(f"[Lite Toolcall] 正向连接成功 {self.config.name}")

    def _ensure_reverse_listener(self):
        if self._reverse_listener is None:
            self._reverse_listener = _ReverseListener(self.config.url, self._on_reverse_socket)
            self._reverse_listener.start()
            print(f"[Lite Toolcall] 反向监听已启动 {self.config.name}: {self.config.url}")

    def _wait_reverse_connected(self):
        if self._connected and self._ws is not None:
            return
        deadline = time.time() + self.config.connect_timeout_seconds
        while time.time() < deadline:
            if self._connected and self._ws is not None:
                return
            time.sleep(0.1)
        raise LiteToolcallError(f"等待 Lite Toolcall 反向连接超时：{self.config.name}")

    def _on_reverse_socket(self, ws):
        with self._lock:
            if self._ws:
                try:
                    self._ws.close()
                except Exception:
                    pass
            self._ws = ws
            self._connected = True
            print(f"[Lite Toolcall] 收到反向连接 {self.config.name}")
            self._auth_and_hello()

    def _auth_and_hello(self):
        if self._authed:
            return
        self._send({"action": "auth", "token": self.config.token})
        hello = self._recv_response(
            lambda item: item.get("action") == "hello" or "status" in item,
            self.config.connect_timeout_seconds,
            "auth",
        )
        if hello.get("status") == 0:
            raise LiteToolcallError(str(hello.get("result", "Lite Toolcall 认证失败。")))
        self._send({"action": "hello", "name": "nino-ai-bot", "ver": "lite-toolcall"})
        self._authed = True
        self._start_heartbeat()
        print(f"[Lite Toolcall] 认证成功 {self.config.name}")

    def _start_heartbeat(self):
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def _heartbeat_loop(self):
        while not self._heartbeat_stop.wait(self.config.heartbeat_interval_seconds):
            try:
                with self._lock:
                    if not self._connected or self._ws is None:
                        continue
                    self._send({"action": "ping"})
                    self._recv_response(
                        lambda item: item.get("action") == "pong",
                        self.config.heartbeat_timeout_seconds,
                        "ping",
                    )
            except Exception as exc:
                print(f"[Lite Toolcall] 心跳失败 {self.config.name}: {exc}")
                self._mark_disconnected()

    def _send(self, payload: dict):
        if self._ws is None:
            raise LiteToolcallError("Lite Toolcall 未连接。")
        text = json.dumps(payload, ensure_ascii=False)
        self._ws.send(text)

    def _set_timeout(self, timeout: float):
        if self._ws is None:
            return
        try:
            self._ws.settimeout(max(0.1, timeout))
        except AttributeError:
            self._ws.sock.settimeout(max(0.1, timeout))

    def _mark_disconnected(self):
        self._connected = False
        self._authed = False
        ws = self._ws
        self._ws = None
        if ws:
            try:
                ws.close()
            except Exception:
                pass

    def _recv_response(self, predicate, timeout_seconds: int, label: str):
        if self._ws is None:
            raise LiteToolcallError("Lite Toolcall 未连接。")
        deadline = time.time() + timeout_seconds
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                self._mark_disconnected()
                raise LiteToolcallError(f"{label} 等待响应超时（{timeout_seconds}秒）。")
            self._set_timeout(remaining)
            try:
                raw = self._ws.recv()
            except (socket.timeout, TimeoutError, websocket.WebSocketTimeoutException):
                continue
            except Exception as exc:
                self._mark_disconnected()
                raise LiteToolcallError(f"{label} 接收响应失败：{exc}") from exc
            self._last_message_at = time.time()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise LiteToolcallError(f"{label} 收到无效 JSON：{_preview(raw)}") from exc
            if data.get("action") == "prompt_changed":
                self._prompt = None
                continue
            if data.get("action") == "disconnect":
                self._mark_disconnected()
                raise LiteToolcallError("Lite Toolcall 后端已永久断开。")
            if predicate(data):
                return data


class LiteToolcallManager:
    def __init__(self, agent_config: dict):
        self._connections = {}
        self._lock = threading.Lock()
        timeout_defaults = {
            "connect_timeout_seconds": agent_config.get("connect_timeout_seconds"),
            "prompt_timeout_seconds": agent_config.get("prompt_timeout_seconds"),
            "run_timeout_seconds": agent_config.get("run_timeout_seconds"),
            "heartbeat_interval_seconds": agent_config.get("heartbeat_interval_seconds"),
            "heartbeat_timeout_seconds": agent_config.get("heartbeat_timeout_seconds"),
        }
        for item in agent_config.get("servers", []) or []:
            if not isinstance(item, dict):
                continue
            config = LiteToolcallServerConfig.from_dict(item, timeout_defaults)
            if config.enabled and config.name and config.url:
                self._connections[config.name] = LiteToolcallConnection(config)

    def get_prompts(self) -> dict[str, str]:
        prompts = {}
        for name, connection in self._connections.items():
            try:
                prompts[name] = connection.get_prompt()
            except Exception as exc:
                prompts[name] = f"Lite Toolcall 服务不可用：{exc}"
        return prompts

    def start_all(self):
        for name, connection in self._connections.items():
            try:
                connection.start()
                print(f"[Lite Toolcall] 已连接/监听：{name}")
            except Exception as exc:
                print(f"[Lite Toolcall] 启动连接失败 {name}: {exc}")

    def run(self, server_name: str, raw: str) -> dict:
        connection = self._connections.get(server_name)
        if connection is None:
            return {"result": f"[调用失败] 未找到 Lite Toolcall 服务：{server_name}", "status": 0}
        try:
            return connection.run(raw)
        except Exception as exc:
            print(f"[Lite Toolcall] 调用失败 {server_name}: {exc}")
            return {"result": f"[调用失败] Lite Toolcall 调用失败：{exc}", "status": 0}

    def close(self):
        with self._lock:
            for connection in self._connections.values():
                connection.close()
