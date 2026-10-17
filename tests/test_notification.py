from __future__ import annotations

import json
import socket

from desk_monitor import notification
from helpers import wait_until


def test_socket_listener_receives_notifications(tmp_path):
    path = tmp_path / 'notify.sock'
    path.touch()  # 残っている古いソケットファイルは消して作り直す
    received = []
    notification.start_socket_listener(lambda text, source: received.append((text, source)), str(path))
    assert wait_until(lambda: path.is_socket())

    msg = {'cmd': 'notify', 'text': '台風', 'source': 'trends_rss'}
    payload = ('壊れた行\n' + json.dumps({'cmd': 'other'}) + '\n[1]\n'
               + json.dumps(msg, ensure_ascii=False) + '\n')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(path))
        s.sendall(payload.encode())
    assert wait_until(lambda: received)
    assert received == [('台風', 'trends_rss')]


def test_listener_survives_handler_errors(tmp_path):
    path = tmp_path / 'notify.sock'
    calls = []

    def handler(text, source):
        calls.append(text)
        raise RuntimeError('中継失敗')

    notification.start_socket_listener(handler, str(path))
    assert wait_until(lambda: path.is_socket())
    for text in ('1', '2'):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(path))
            s.sendall(json.dumps({'cmd': 'notify', 'text': text}).encode() + b'\n')
    assert wait_until(lambda: calls == ['1', '2'])
