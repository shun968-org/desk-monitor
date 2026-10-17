"""機器間(カメラ・表示・ハブ)の TCP 通信の共通部分。

1 メッセージ = 1 行の JSON ヘッダ + ヘッダの size バイトのペイロード(省略時 0)。
接続した側は最初に {"type": "hello", "token": ...} を送り、受けた側はトークンが
一致しなければ切断する。どの待ち受けも、接続元がプライベート IP(LAN)でなければ拒否する。

トークンは環境変数 DESK_MONITOR_TOKEN で渡す(各機器の ~/.config/desk-monitor/desk-monitor.env。
リポジトリには置かない。scripts/setup-secrets.sh で作る)。
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import socket

MAX_HEADER = 4096
MAX_PAYLOAD = 4 << 20


class ProtocolError(Exception):
    pass


def is_lan_address(host: str) -> bool:
    """LAN(プライベート IP・ループバック)からの接続か。"""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback


def parse_addr(addr: str) -> tuple[str, int]:
    host, sep, port = addr.rpartition(':')
    if not sep or not host:
        raise ValueError(f'host:port の形式で指定してください: {addr!r}')
    return host, int(port)


def token_matches(given: object, expected: str) -> bool:
    return isinstance(given, str) and bool(expected) and hmac.compare_digest(
        given.encode(), expected.encode())


def encode(header: dict, payload: bytes = b'') -> bytes:
    body = json.dumps({**header, 'size': len(payload)}, ensure_ascii=False).encode()
    return body + b'\n' + payload


def send(sock: socket.socket, header: dict, payload: bytes = b'') -> None:
    sock.sendall(encode(header, payload))


class Reader:
    """ソケットからメッセージを 1 つずつ読む。接続が切れたら EOFError。

    socket.makefile は使わない(ソケットを閉じても makefile が fd を持ち続け、相手に切断が伝わらないため)。
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = bytearray()

    def _fill(self) -> None:
        chunk = self._sock.recv(65536)
        if not chunk:
            raise EOFError
        self._buf += chunk

    def read(self) -> tuple[dict, bytes]:
        while (end := self._buf.find(b'\n')) < 0:
            if len(self._buf) > MAX_HEADER:
                raise ProtocolError('header too long')
            self._fill()
        if end > MAX_HEADER:
            raise ProtocolError('header too long')
        line = bytes(self._buf[:end])
        try:
            header = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProtocolError(str(exc)) from exc
        if not isinstance(header, dict):
            raise ProtocolError('header is not an object')
        size = header.get('size', 0)
        if not isinstance(size, int) or not 0 <= size <= MAX_PAYLOAD:
            raise ProtocolError(f'invalid size: {size!r}')
        while len(self._buf) < end + 1 + size:
            self._fill()
        payload = bytes(self._buf[end + 1:end + 1 + size])
        del self._buf[:end + 1 + size]
        return header, payload


def connect(addr: tuple[str, int], token: str, timeout: float = 10.0,
            **hello: object) -> tuple[socket.socket, Reader]:
    """接続して hello を送る。以後の読み書きのタイムアウトは timeout 秒。"""
    sock = socket.create_connection(addr, timeout=5.0)
    try:
        sock.settimeout(timeout)
        send(sock, {'type': 'hello', 'token': token, **hello})
    except OSError:
        sock.close()
        raise
    return sock, Reader(sock)


def accept_hello(sock: socket.socket, client_host: str, token: str,
                 timeout: float = 10.0) -> tuple[Reader, dict] | None:
    """待ち受け側: LAN からの接続で hello のトークンが一致すれば (Reader, hello)、そうでなければ None。"""
    if not is_lan_address(client_host):
        return None
    sock.settimeout(timeout)
    reader = Reader(sock)
    try:
        hello, _ = reader.read()
    except (OSError, EOFError, ProtocolError):
        return None
    if hello.get('type') != 'hello' or not token_matches(hello.get('token'), token):
        return None
    return reader, hello
