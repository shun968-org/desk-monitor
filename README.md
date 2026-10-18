# desk-monitor

Raspberry Pi 2 台と開発 PC で動くデスクトップモニタ。カメラの映像を円形 LCD に映し、開発 PC で物体検出した結果を枠として重ねる。
構成の判断は [ADR-0001](docs/adr/0001-split-camera-display-and-inference-hub.md)。

![Raspberry Pi 2 台(ケース入り)と円形 LCD・カメラモジュール](docs/hardware.jpg)

![ダッシュボード: 検出枠つきのカメラ映像と観光地の地図(設置場所が分かる地図と観光地のラベルはぼかしている)](docs/dashboard.jpg)

## システム構成

```mermaid
flowchart LR
    subgraph pi_b["pi_b(role: camera)"]
        cam["ov5647 カメラ"]
        camsrv["camera_server.py<br/>desk-monitor-camera<br/>:8082"]
        cam --> camsrv
    end

    subgraph pi_a["pi_a(role: display)"]
        display["display.py<br/>desk-monitor-display<br/>(制御口 control.py :8083)"]
        lcd["円形 LCD<br/>GC9A01 240x240"]
        trend["trend.py<br/>desk-monitor-trend"]
        trend -- "通知<br/>(Unix ソケット)" --> display
        display -- SPI --> lcd
    end

    subgraph hub_pc["開発 PC(role: hub、Docker)"]
        hub["hub.py<br/>H.264 復号(PyAV)"]
        detector["detector.py<br/>D-FINE-L Objects365<br/>(ONNX Runtime)"]
        db[("hub.sqlite3<br/>検出・通知の履歴")]
        dash["ダッシュボード<br/>127.0.0.1:8090"]
        hub <--> detector
        hub --> db
        hub --> dash
    end

    browser["ブラウザ<br/>(開発 PC 上)"]
    ext["外部データ<br/>OpenStreetMap / 地理院タイル / 気象庁"]
    sns["Bluesky / Google Trends / RSS"]

    camsrv -- "MJPEG 320x240" --> display
    camsrv -- "H.264 1280x960" --> hub
    hub -- "検出結果(0〜1 座標)・モード切替" --> display
    display -- "状態・トレンド通知" --> hub
    dash --> browser
    ext --> hub
    ext --> browser
    sns --> trend
```

- 機器間の接続(:8082 / :8083)は `wire.py` の形式で、共有トークンで認証し、プライベート IP 以外からの接続は拒否する。
  pi_a・ハブがそれぞれ接続しにいく側で、トークンと接続先は各機器の `~/.config/desk-monitor/desk-monitor.env` に置く
  (`scripts/setup-secrets.sh` で作って配る)
- ハブが止まっていても、pi_a は pi_b の映像を枠なしで LCD に出す
- トレンド監視の通知は LCD には出さず、表示プロセスがハブに中継してダッシュボードにトースト表示する
- ダッシュボードは開発 PC の 127.0.0.1 だけで待ち受ける。地図レイヤ(`layers.py`)と簡易 AR(`ar.py`)のデータは、
  ハブが外部から取得してキャッシュするものと、ブラウザが直接読むもの(地理院タイルなど)がある

| 機器 | role | サービス | 反映 |
|---|---|---|---|
| pi_a | display | `desk-monitor-display` / `desk-monitor-trend`(systemd ユーザーサービス) | `scripts/deploy.sh pi_a` |
| pi_b | camera | `desk-monitor-camera`(systemd ユーザーサービス) | `scripts/deploy.sh pi_b` |
| 開発 PC | hub | `hub`(`deploy/hub/compose.yaml`) | `scripts/deploy-hub.sh` |
