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


@dataclass
class LiteToolcallServerConfig:
    name: str
    enabled: bool
    connection_mode: str
    url: str
    token: str

    @classmethod
    def from_dict(cls, item: dict):
        return cls(
            name=str(item.get("name", "")).strip(),
            enabled=bool(item.get("enabled", True)),
            connection_mode=str(item.get("connection_mode", "forward")).strip().lower() or "forward",
            url=str(item.get("url", "")).strip(),
            token=str(item.get("token", "")),
        )


class _RawWebSocket:
    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, conn: socket.socket):
        self.conn = conn
        self.conn.settimeout(15)
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
            self._send({"action": "get_prompt"})
            data = self._recv_response(lambda item: "prompt" in item)
            self._prompt = str(data.get("prompt", ""))
            return self._prompt

    def run(self, raw: str) -> dict:
        with self._lock:
            self.ensure_connected()
            self._send({"action": "run", "raw": raw})
            return self._recv_response(lambda item: "status" in item)

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
        self._ws = websocket.create_connection(self.config.url, timeout=15)
        self._connected = True
        self._auth_and_hello()

    def _ensure_reverse_listener(self):
        if self._reverse_listener is None:
            self._reverse_listener = _ReverseListener(self.config.url, self._on_reverse_socket)
            self._reverse_listener.start()

    def _wait_reverse_connected(self):
        if self._connected and self._ws is not None:
            return
        deadline = time.time() + 15
        while time.time() < deadline:
            if self._connected and self._ws is not None:
                return
            time.sleep(0.1)
        raise LiteToolcallError(f"等待 Lite Toolcall 反向连接超时：{self.config.name}")

    def _on_reverse_socket(self, ws):
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._ws = ws
        self._connected = True
        self._auth_and_hello()

    def _auth_and_hello(self):
        if self._authed:
            return
        self._send({"action": "auth", "token": self.config.token})
        hello = self._recv_response(lambda item: item.get("action") == "hello" or "status" in item)
        if hello.get("status") == 0:
            raise LiteToolcallError(str(hello.get("result", "Lite Toolcall 认证失败。")))
        self._send({"action": "hello", "name": "nino-ai-bot", "ver": "lite-toolcall"})
        self._authed = True
        self._start_heartbeat()

    def _start_heartbeat(self):
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def _heartbeat_loop(self):
        while not self._heartbeat_stop.wait(10):
            try:
                with self._lock:
                    if not self._connected or self._ws is None:
                        continue
                    self._send({"action": "ping"})
                    self._recv_response(lambda item: item.get("action") == "pong")
            except Exception as exc:
                print(f"[Lite Toolcall] 心跳失败 {self.config.name}: {exc}")
                self._connected = False
                self._authed = False
                try:
                    if self._ws:
                        self._ws.close()
                except Exception:
                    pass
                self._ws = None

    def _send(self, payload: dict):
        if self._ws is None:
            raise LiteToolcallError("Lite Toolcall 未连接。")
        text = json.dumps(payload, ensure_ascii=False)
        self._ws.send(text)

    def _recv_response(self, predicate):
        if self._ws is None:
            raise LiteToolcallError("Lite Toolcall 未连接。")
        while True:
            raw = self._ws.recv()
            self._last_message_at = time.time()
            data = json.loads(raw)
            if data.get("action") == "prompt_changed":
                self._prompt = None
                continue
            if data.get("action") == "disconnect":
                self.close()
                raise LiteToolcallError("Lite Toolcall 后端已永久断开。")
            if predicate(data):
                return data


class LiteToolcallManager:
    def __init__(self, agent_config: dict):
        self._connections = {}
        self._lock = threading.Lock()
        for item in agent_config.get("servers", []) or []:
            if not isinstance(item, dict):
                continue
            config = LiteToolcallServerConfig.from_dict(item)
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
            return {"result": f"[调用失败] Lite Toolcall 调用失败：{exc}", "status": 0}

    def close(self):
        with self._lock:
            for connection in self._connections.values():
                connection.close()
