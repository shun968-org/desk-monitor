"""トレンド監視(trend.py)からの通知を受ける Unix ソケット(表示プロセスの中で動かす)。

通知は円形 LCD には出さず、ハブ(開発 PC)に中継してダッシュボードにトースト表示する(control.py)。

通信プロトコル(改行区切り JSON):
  {"cmd": "notify", "text": "...", "source": "bluesky"|"trends_rss"}
"""
from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Callable


def start_socket_listener(on_notify: Callable[[str, str], None], sock_path: str) -> None:
    """Unix ドメインソケットを別スレッドで待機し、通知を受けるたびに on_notify(text, source) を呼ぶ。"""
    path = Path(sock_path)

    def _serve() -> None:
        if path.exists():
            path.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
            srv.bind(str(path))
            srv.listen(8)
            srv.settimeout(1.0)
            while True:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                try:
                    raw = conn.recv(4096).decode(errors='replace')
                    for line in raw.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(msg, dict) and msg.get('cmd') == 'notify':
                            try:
                                on_notify(str(msg.get('text', '')), str(msg.get('source', '')))
                            except Exception:
                                pass  # 中継先の不調で受信を止めない
                finally:
                    conn.close()

    threading.Thread(target=_serve, daemon=True, name='notify-listener').start()
