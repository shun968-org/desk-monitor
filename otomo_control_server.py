#!/usr/bin/env python3
"""otomo-mobile 検証用の WiFi 経路(mDNS+WebSocket)スタブサーバー。

_otomo._tcp.local. を mDNS アドバタイズし、ws://<host>:8080/control で
docs/requirements.md 4.2 の JSON 制御フレームを受け付ける。

実ハードウェア（サーボ等）は接続されていないため、状態は内部変数のみで
シミュレートする。otomo-mobile クライアントの mDNS 発見・WebSocket 送受信を
実機(pi_a)相手に検証するための最小実装（本物の desk-companion 実装ではない）。

"mode" コマンド（face/camera）受信時は、GC9A01 ディスプレイを実際に
占有する show_face.py（常駐プロセス）へシグナルを送って表示を即時切替する
（requirements 3.3 「顔表示モード ↔ カメラ表示モードの切替」相当）。
プロセスの再起動は行わない（再起動するとカメラの再初期化等で数秒かかる
ため、show_face.py 側を「起動したら常駐し、シグナルでモードだけ切り替える」
設計にして切替を瞬時にしている）。

起動時には必ず、前回プロセスが残した show_face.py の孤児プロセスを
pkill で一掃してから新規に起動する（サーバーが異常終了すると子プロセスが
終了処理されず孤児化し、GPIO/SPI を専有し続けて新規プロセスの起動を
妨げ、mode 切替が一切効かなくなるため）。

使い方:
    python3 otomo_control_server.py

依存パッケージ:
    sudo apt install -y python3-websockets python3-zeroconf
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import socket
import sys

from websockets.asyncio.server import ServerConnection, serve
from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

SERVICE_TYPE = "_otomo._tcp.local."
INSTANCE_NAME = "otomo-9c3e"
PORT = 8080
CONTROL_PATH = "/control"

SHOW_FACE_SCRIPT = "/home/shun-asai/show_face.py"
SHOW_FACE_SOCKET = "/tmp/show_face.sock"
VALID_MODES = {"face", "camera"}
DEFAULT_MODE = "camera"
DISPLAY_STOP_TIMEOUT = 5.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("otomo_control_server")


MODE_SIGNAL = {"face": signal.SIGUSR1, "camera": signal.SIGUSR2}


class DisplayProcessManager:
    """GC9A01 を占有する show_face.py（常駐プロセス）を起動し、mode 切替は
    シグナル（SIGUSR1=face, SIGUSR2=camera）で行う。

    show_face.py はプロセス内でモードを切り替える設計のため、再起動
    （カメラの再初期化等で数秒かかる）を伴わず瞬時に切り替わる。
    プロセスが想定外に終了していた場合のみ起動し直す。
    """

    def __init__(self, script: str) -> None:
        self._script = script
        self._process: asyncio.subprocess.Process | None = None
        self._mode: str | None = None
        self._lock = asyncio.Lock()

    @property
    def mode(self) -> str | None:
        return self._mode

    async def switch_to(self, mode: str) -> None:
        if mode not in VALID_MODES:
            log.warning("unknown mode: %r", mode)
            return
        async with self._lock:
            if not self._is_running():
                await self._spawn(mode)
                return
            if mode == self._mode:
                return
            self._process.send_signal(MODE_SIGNAL[mode])
            self._mode = mode
            log.info("display mode switched to %s via signal (pid=%s)", mode, self._process.pid)

    async def _spawn(self, mode: str) -> None:
        # サーバーと同じ Python（venv）で起動し、venv に入れた onnxruntime を使わせる
        self._process = await asyncio.create_subprocess_exec(
            sys.executable,
            self._script,
            "--mode",
            mode,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._mode = mode
        log.info("display process started mode=%s (pid=%s)", mode, self._process.pid)

    async def stop(self) -> None:
        async with self._lock:
            process = self._process
            if process is None or process.returncode is not None:
                return
            process.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(process.wait(), timeout=DISPLAY_STOP_TIMEOUT)
            except asyncio.TimeoutError:
                log.warning("display process did not exit in time, killing it")
                process.kill()
                await process.wait()

    async def kill_stale_processes(self) -> None:
        """前回サーバープロセスが残した孤児の show_face.py を一掃する。

        サーバーが異常終了（systemd の Restart=on-failure 等）した場合、
        show_face.py の子プロセスは終了処理されずに孤児化し、GPIO/SPI を
        専有し続ける。これが残っていると新規起動した show_face.py が
        初期化に失敗し、mode 切替が一切効かなくなる（表示が固定される）
        ため、自プロセスがまだ何も起動していない起動時に必ず一掃する。
        """
        proc = await asyncio.create_subprocess_exec(
            "pkill",
            "-f",
            self._script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        await asyncio.sleep(0.5)

    def _is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None


display = DisplayProcessManager(SHOW_FACE_SCRIPT)


def _send_bubble(text: str, source: str, duration: int) -> None:
    """show_face.py の Unix ソケットに吹き出し通知を送る（同期・ブロッキング短時間）。"""
    payload = (
        json.dumps({"cmd": "bubble", "text": text, "source": source,
                    "duration": duration}, ensure_ascii=False)
        + "\n"
    )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(SHOW_FACE_SOCKET)
            s.sendall(payload.encode())
    except Exception as exc:
        log.warning("吹き出し送信失敗: %s", exc)


async def _handle_message(raw: str) -> None:
    msg = json.loads(raw)
    msg_type = msg.get("type")
    if msg_type == "mode":
        await display.switch_to(str(msg.get("value")))
    elif msg_type == "notify_test":
        # テスト用吹き出し通知
        # source: "bluesky" | "trends_rss"（省略時は "bluesky"）
        # text: 任意のテキスト（省略時はデフォルトメッセージ）
        # duration: 表示秒数（省略時は 10）
        source = str(msg.get("source", "bluesky"))
        text = str(msg.get("text") or f"テスト通知です [{source}]")
        duration = int(msg.get("duration", 10))
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _send_bubble, text, source, duration)
        log.info("notify_test sent: source=%s text=%r duration=%s", source, text, duration)
    else:
        log.warning("unknown message type: %r", msg_type)


async def handler(websocket: ServerConnection) -> None:
    if websocket.request.path != CONTROL_PATH:
        await websocket.close(code=1008, reason="unknown path")
        return

    log.info("client connected: %s", websocket.remote_address)
    try:
        async for raw in websocket:
            log.info("recv: %s", raw)
            await _handle_message(raw)
    finally:
        log.info("client disconnected: %s", websocket.remote_address)


def _local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


async def main() -> None:
    ip = _local_ip()
    hostname = socket.gethostname()

    azc = AsyncZeroconf()
    info = ServiceInfo(
        SERVICE_TYPE,
        f"{INSTANCE_NAME}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(ip)],
        port=PORT,
        server=f"{hostname}.local.",
        # iOS の Bonjour（NWBrowser）は RFC 6763 に従い TXT レコードを要求し、
        # TXT が無いとサービスを found として通知しない（Android の NsdManager は
        # TXT 無しでも報告する）。最低限の TXT を必ず広告する。実機の
        # desk-companion 側の mDNS 広告にも同様の TXT が必要。
        properties={"path": "/control"},
    )
    await azc.async_register_service(info)
    log.info("advertising %s at %s:%d", INSTANCE_NAME, ip, PORT)

    await display.kill_stale_processes()
    await display.switch_to(DEFAULT_MODE)
    try:
        async with serve(handler, "0.0.0.0", PORT):
            await asyncio.Future()
    finally:
        await display.stop()
        await azc.async_unregister_service(info)
        await azc.async_close()


if __name__ == "__main__":
    asyncio.run(main())
