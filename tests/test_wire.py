from __future__ import annotations

import socket
import threading

import pytest

from desk_monitor import wire


@pytest.mark.parametrize('host, expected', [
    ('192.168.10.113', True), ('10.0.0.5', True), ('172.16.1.1', True), ('127.0.0.1', True),
    ('8.8.8.8', False), ('2404:7a81::1', False), ('not-an-ip', False),
])
def test_is_lan_address(host, expected):
    assert wire.is_lan_address(host) is expected


@pytest.mark.parametrize('addr, expected', [
    ('192.168.10.111:8082', ('192.168.10.111', 8082)), ('host.local:1', ('host.local', 1))])
def test_parse_addr(addr, expected):
    assert wire.parse_addr(addr) == expected


@pytest.mark.parametrize('addr', ['192.168.10.111', ':8082', 'host:port', ''])
def test_parse_addr_rejects_invalid(addr):
    with pytest.raises(ValueError):
        wire.parse_addr(addr)


def test_token_matches():
    assert wire.token_matches('abc', 'abc')
    assert not wire.token_matches('abd', 'abc')
    assert not wire.token_matches(None, 'abc')
    assert not wire.token_matches('', '')  # 未設定のトークン同士は一致させない


def test_messages_roundtrip():
    a, b = socket.socketpair()
    with a, b:
        wire.send(a, {'type': 'frame', 'key': True}, b'\x00\x01payload')
        wire.send(a, {'type': 'status', 'text': '日本語'})
        reader = wire.Reader(b)
        assert reader.read() == ({'type': 'frame', 'key': True, 'size': 9}, b'\x00\x01payload')
        assert reader.read() == ({'type': 'status', 'text': '日本語', 'size': 0}, b'')
        a.close()
        with pytest.raises(EOFError):
            reader.read()


@pytest.mark.parametrize('raw', [b'not json\n', b'[1, 2]\n', b'{"size": -1}\n',
                                 b'{"size": "x"}\n', b'x' * (wire.MAX_HEADER + 10)])
def test_reader_rejects_bad_headers(raw):
    a, b = socket.socketpair()
    with a, b:
        a.sendall(raw)
        a.close()
        with pytest.raises((wire.ProtocolError, EOFError)):
            wire.Reader(b).read()


def test_reader_detects_truncated_payload():
    a, b = socket.socketpair()
    with a, b:
        a.sendall(b'{"size": 10}\nabc')
        a.close()
        with pytest.raises(EOFError):
            wire.Reader(b).read()


def _serve_once(token, client_host='127.0.0.1'):
    srv = socket.create_server(('127.0.0.1', 0))
    result = {}

    def run():
        conn, _ = srv.accept()
        with conn:
            result['accepted'] = wire.accept_hello(conn, client_host, token, timeout=2)
        srv.close()

    t = threading.Thread(target=run)
    t.start()
    return srv.getsockname(), t, result


def test_accept_hello_checks_token_and_lan():
    addr, t, result = _serve_once('secret')
    sock, _ = wire.connect(addr, 'secret', stream='mjpeg')
    t.join(timeout=3)
    sock.close()
    reader, hello = result['accepted']
    assert hello['stream'] == 'mjpeg'

    addr, t, result = _serve_once('secret')
    sock, _ = wire.connect(addr, 'wrong')
    t.join(timeout=3)
    sock.close()
    assert result['accepted'] is None

    addr, t, result = _serve_once('secret', client_host='8.8.8.8')
    sock, _ = wire.connect(addr, 'secret')
    t.join(timeout=3)
    sock.close()
    assert result['accepted'] is None


def test_accept_hello_rejects_non_hello():
    addr, t, result = _serve_once('secret')
    sock = socket.create_connection(addr)
    wire.send(sock, {'type': 'frame', 'token': 'secret'})
    t.join(timeout=3)
    sock.close()
    assert result['accepted'] is None
