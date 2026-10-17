# CLAUDE.md

## このリポジトリ

Raspberry Pi(`ssh pi_a`、ホスト名 node-a-local)で動くデスクトップモニタ「desk-monitor」のソース一式。
Pi 上のホームディレクトリにあったファイルを `scp` で取り込んだもの。**実機の正は Pi 側**であり、
変更を反映するときは `scp` で Pi に戻す。

- `show_face.py`: GC9A01 ディスプレイ(240x240)に表情アニメーション(`faces/`)またはカメラ映像を表示する常駐スクリプト。
  systemd ユーザーサービス `desk-monitor-display`(`deploy/systemd/`)として動く。モード切替は SIGUSR1(face)/ SIGUSR2(camera)
- `object_detector.py`: camera モードの物体検出(YOLO11n / ONNX Runtime)。モデルは `scripts/export_yolo.sh` で生成し Pi の `~/models/` に置く
- `trend_monitor.py` / `trend_config.json`: Bluesky・Google Trends・RSS からトレンドを監視する
- `show_camera.py` / `show_image.py`: カメラ・画像表示のユーティリティ

## 依存関係

- `requirements.txt`: コンテナ・開発機でも入る依存
- `requirements-pi.txt`: Pi 実機専用(gpiozero, spidev, picamera2, onnxruntime 等)。表示サービスは `~/otomo_venv` で動く
- スマホアプリとの連携はない前提(mDNS / WebSocket の制御サーバと BLE プロビジョニングは削除済み)
- Python は Pi と同じ 3.13 系

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
- Pi 専用ハードウェア(SPI/GPIO/カメラ)はコンテナでは動かない。実機検証は `pi_a` で行う
- `ssh pi_a` / `scp` はホストの `~/.ssh` を引き継いで使う。`devcontainer.json` の `mounts` で
  `~/.ssh-host` に読み取り専用でマウントし、起動のたび(`postStartCommand`)に `sync-host-ssh.sh` が
  `~/.ssh` へコピーしてパーミッションを直す(直接マウントしないのは、所有者・モードのずれと `known_hosts` の追記のため)
  - ホスト側の鍵や config を変えたら、コンテナを再起動すれば反映される(rebuild は不要)
  - ホストに `~/.ssh` がないとコンテナが起動しない。`IdentityFile` は `~/.ssh/...` の形で書く。
    mDNS 名(`*.local`)が解決できなければ IP アドレスを使う
  - コンテナ内では Claude の sandbox(bubblewrap)が起動しないため、`ssh` / `scp` の実行には sandbox を外す承認が要る

## 注意

- `trend_config.json` 等に秘密情報は含まれていない。エージェントに使わせてよいトークン類だけを `.env`(git 管理外)に置く
