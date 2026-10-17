# CLAUDE.md

## このリポジトリ

Raspberry Pi と開発 PC で動くデスクトップモニタ「desk-monitor」のソース一式。機器は 3 台で、役割(role)が違う
(構成の判断は [ADR-0001](docs/adr/0001-split-camera-display-and-inference-hub.md))。

| 機器 | role | 内容 | サービス |
|---|---|---|---|
| `pi_a`(node-a-local、192.168.10.113) | display | 円形 LCD(GC9A01)の表示とトレンド監視 | `desk-monitor-display` / `desk-monitor-trend` |
| `pi_b`(node-b-local、192.168.10.111) | camera | カメラ(ov5647)の映像を H.264 / MJPEG で配信 | `desk-monitor-camera` |
| 開発 PC(この PC) | hub | 物体検出(YOLO11m、入力 640)とダッシュボード(http://127.0.0.1:8090/) | Docker(`deploy/hub/`) |

映像は pi_b → pi_a(MJPEG 320x240)と pi_b → ハブ(H.264 640x480)に流れ、検出結果(全視野に対する 0〜1 の座標)は
ハブ → pi_a に流れる。ハブが止まっていても LCD は枠なしで映像を出す。

**正はこのリポジトリ**であり、Pi への反映は `scripts/deploy.sh [host] [role]`(`pi_a` は display、`pi_b` は camera が既定)、
ハブは `scripts/deploy-hub.sh` で行う。Pi は rsync で `~/desk-monitor/` に配置し、(display のみ)`~/desk-monitor/.venv` を
更新して systemd ユーザーサービスを再起動する。Pi 上のファイルを直接編集・`scp` しない(次の deploy で上書き・削除される)。
ログは Pi では `journalctl _SYSTEMD_USER_UNIT=desk-monitor-display.service`(`journalctl --user` が使えない)、
ハブは `scripts/deploy-hub.sh logs -f hub`。

機器間の接続は共有トークンで認証し、プライベート IP 以外からの接続は拒否する(`wire.py`)。トークンと接続先は各機器の
`~/.config/desk-monitor/desk-monitor.env`(600)にあり、リポジトリには置かない。作り直しと配布は `scripts/setup-secrets.sh`
(Pi の IP が変わったときも再実行する)。

ソースは `src/desk_monitor/` パッケージ。systemd からは `PYTHONPATH=src` で `python -m desk_monitor.<module>` として起動する。

- `display.py`(pi_a): 表示プロセスのエントリポイント。GC9A01(240x240)に表情アニメーションまたはカメラ映像を表示する。
  モード切替は SIGUSR1(face)/ SIGUSR2(camera)か、ハブのダッシュボードから(テスト通知の機能はない)
  - `remote_camera.py`(pi_b の MJPEG を受ける)、`camera.py`(この Pi に直接付けたカメラ。`to_lcd` で全視野の中央を切り出す)、
    `control.py`(ハブからの検出結果・操作を受ける制御口 8083)、`lcd.py`(GC9A01 ドライバ)、`face.py`(表情)、
    `notification.py`(トレンド監視の通知を受ける Unix ソケット。LCD には出さずハブに中継する)、`fonts.py`、`paths.py`、`sysstat.py`(温度・CPU)
- `camera_server.py`(pi_b): ハードウェアエンコーダで H.264 / MJPEG を作り、ポート 8082 で配る。picamera2 は apt、venv は使わない。
  映像の向きは `--vflip` / `--hflip`
- `hub.py`(開発 PC): H.264 を復号(PyAV)して推論し(`detector.py`、ONNX Runtime)、結果を pi_a に送る。
  ダッシュボードは 127.0.0.1 だけで待ち受け、履歴は `~/.local/share/desk-monitor/hub.sqlite3`。
  モデルは YOLO11m(入力 640、`models/yolo11m.onnx`、git 管理外)。`scripts/export_yolo.sh` で作り、`DESK_MONITOR_MODEL` で切り替えられる
  (開発 PC の CPU 4 スレッドで n 49ms / s 103ms / m 273ms / l 339ms。精度と頻度の兼ね合いで m)
- `trend.py`(pi_a): Bluesky・Google Trends・RSS からトレンドを監視し、表示プロセス経由でハブに通知を送る(ダッシュボードにトースト表示。円形 LCD には出さない)。
  設定は `config/trend_config.json`
- `wire.py`: 機器間の通信(JSON ヘッダ + ペイロード、hello でトークン認証)
- `tools/show_camera.py` / `tools/show_image.py`: カメラ・画像表示の単発ユーティリティ(表示サービスを止めてから使う)
- 素材は `assets/`(`faces/` の表情画像、`fonts/` の IPAex ゴシックとライセンス)
- ハードウェア依存(spidev / gpiozero / picamera2 / onnxruntime / av)の import は使う箇所まで遅延させ、
  それ以外のモジュールは開発機・CI で import できるようにしておく

## 依存関係

- `requirements.txt`: コンテナ・開発機でも入る依存
- `requirements-dev.txt`: テスト用(pytest など。ハブの依存も含む)。Pi には入れない
- `requirements-pi.txt`: 表示用の Pi(pi_a)専用(gpiozero, spidev 等)。pi_a の両サービスは `~/desk-monitor/.venv` で動く
- `requirements-hub.txt`: ハブ(onnxruntime, av)。`deploy/hub/Dockerfile` で入れる
- スマホアプリとの連携はない前提(mDNS / WebSocket の制御サーバと BLE プロビジョニングは削除済み)
- Python は Pi と同じ 3.13 系

## テスト

- `tests/` に pytest の単体テストがある。Pi 専用パッケージ(spidev / gpiozero / picamera2)は `tests/conftest.py` などで偽モジュールに置き換え、ハードウェアなしで動かす
- 実行: `pip install -r requirements-dev.txt && python -m pytest`(設定は `pyproject.toml`)。ホストの Python に依存を入れたくなければ
  `docker run --rm -v "$PWD:/src:ro" python:3.13-slim sh -c 'cp -r /src /w && cd /w && pip install -q -r requirements-dev.txt && python -m pytest'`
- CI(`.github/workflows/ci.yml`)でテストを実行し、カバレッジを SonarCloud に渡す
- 実機でしか確かめられないこと(LCD の表示、カメラ、推論速度、温度)は、`scripts/deploy.sh` で pi_a に配置して確認する

## 開発規約

- ブランチ・コミット・PR の規約は [CONTRIBUTING.md](CONTRIBUTING.md)。lefthook と commitlint で機械的に強制される
- ADR は `docs/adr/`(`managing-adrs` スキル)。要件・設計ドキュメントは `managing-notion-docs` スキル
- Notion(`ntn`)の認証と権限制御は [docs/notion-agent-auth.md](docs/notion-agent-auth.md)。`NOTION_API_TOKEN`(最小権限の内部インテグレーション)を使い、`ntn login` / `ntn auth` は使わない

## Dev container

- `.devcontainer/` に定義。`claude` CLI は `wrap-claude-cli.sh` により
  `.devcontainer/claude-strict-network-settings.json`(`sandbox.network.strictAllowlist`)付きで起動する。
  `strictAllowlist` はコミット済みの `.claude/settings.json` からは効かないため、この方式を取っている
- ネットワークの許可ドメインは `.claude/settings.json` の `sandbox.network.allowedDomains`
- 機密(`.ssh`, `.aws`, `.credentials*`, `secrets/`)は sandbox と permissions の両方で読み取りを拒否している
- `.env` はエージェントが読める(編集は拒否)。**運用ルール: `.env` にはエージェント向けの認証情報(`NOTION_API_TOKEN`)だけを書く。
  人間用・本番用の資格情報は置かない**。これがセキュリティの前提
- Pi 専用ハードウェア(SPI/GPIO/カメラ)はコンテナでは動かない。実機検証は `pi_a`(LCD)と `pi_b`(カメラ)で行う
- `ssh pi_a` / `scp` はホストの `~/.ssh` を引き継いで使う。`devcontainer.json` の `mounts` で
  `~/.ssh-host` に読み取り専用でマウントし、起動のたび(`postStartCommand`)に `sync-host-ssh.sh` が
  `~/.ssh` へコピーしてパーミッションを直す(直接マウントしないのは、所有者・モードのずれと `known_hosts` の追記のため)
  - ホスト側の鍵や config を変えたら、コンテナを再起動すれば反映される(rebuild は不要)
  - ホストに `~/.ssh` がないとコンテナが起動しない。`IdentityFile` は `~/.ssh/...` の形で書く。
    mDNS 名(`*.local`)が解決できなければ IP アドレスを使う
  - コンテナ内では Claude の sandbox(bubblewrap)が起動しないため、`ssh` / `scp` の実行には sandbox を外す承認が要る

## 注意

- `trend_config.json` 等に秘密情報は含まれていない。エージェントに使わせてよいトークン類だけを `.env`(git 管理外)に置く
