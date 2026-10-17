"""YOLO (ONNX) による物体検出と、検出枠・ラベルの描画。

display.py の camera モードから使う。推論は別スレッド (DetectionWorker) で
最新フレームに対して繰り返し実行し、表示ループは直近の検出結果を重ねて描くだけ
にすることで、推論が遅い Raspberry Pi 3 でも表示のフレームレートを落とさない。

モデルは Ultralytics YOLO11n を入力 256x256 で ONNX に書き出したものを想定する
（出力 (1, 84, N): 先頭 4 行が cx, cy, w, h、残り 80 行が COCO 各クラスのスコア）。
書き出し手順は scripts/export_yolo.sh を参照。

依存パッケージ:
    pip install onnxruntime  # apt の python3-onnxruntime は Pi 3 で SIGILL になるため使わない
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# COCO 80 クラスの日本語名（モデルのクラス ID 順）
COCO_LABELS_JA = [
    '人', '自転車', '車', 'バイク', '飛行機', 'バス', '電車', 'トラック', 'ボート', '信号機',
    '消火栓', '一時停止標識', 'パーキングメーター', 'ベンチ', '鳥', '猫', '犬', '馬', '羊', '牛',
    '象', '熊', 'シマウマ', 'キリン', 'リュック', '傘', 'ハンドバッグ', 'ネクタイ', 'スーツケース', 'フリスビー',
    'スキー板', 'スノーボード', 'ボール', '凧', '野球バット', '野球グローブ', 'スケートボード', 'サーフボード', 'テニスラケット', 'ボトル',
    'ワイングラス', 'カップ', 'フォーク', 'ナイフ', 'スプーン', 'ボウル', 'バナナ', 'りんご', 'サンドイッチ', 'オレンジ',
    'ブロッコリー', 'にんじん', 'ホットドッグ', 'ピザ', 'ドーナツ', 'ケーキ', '椅子', 'ソファ', '鉢植え', 'ベッド',
    'テーブル', 'トイレ', 'テレビ', 'ノートPC', 'マウス', 'リモコン', 'キーボード', 'スマホ', '電子レンジ', 'オーブン',
    'トースター', 'シンク', '冷蔵庫', '本', '時計', '花瓶', 'はさみ', 'ぬいぐるみ', 'ドライヤー', '歯ブラシ',
]

# 枠の色（クラス ID で巡回させる。暗い映像上でも見える彩度の高い色）
_PALETTE = [
    (255, 56, 56), (255, 157, 151), (255, 112, 31), (255, 178, 29), (207, 210, 49),
    (72, 249, 10), (146, 204, 23), (61, 219, 134), (26, 147, 52), (0, 212, 187),
    (44, 153, 168), (0, 194, 255), (52, 69, 147), (100, 115, 255), (0, 24, 236),
    (132, 56, 255), (82, 0, 133), (203, 56, 255), (255, 149, 200), (255, 55, 199),
]

_PAD_VALUE = 114  # レターボックスの余白色（Ultralytics の学習時と同じ灰色）


@dataclass(frozen=True)
class Detection:
    x1: float
    y1: float
    x2: float
    y2: float
    score: float
    class_id: int

    @property
    def label(self) -> str:
        if 0 <= self.class_id < len(COCO_LABELS_JA):
            return COCO_LABELS_JA[self.class_id]
        return f'#{self.class_id}'


class YoloDetector:
    """YOLO の ONNX モデルで 1 フレームを推論する。"""

    def __init__(self, model_path: Path, conf_threshold: float = 0.4,
                 iou_threshold: float = 0.45, max_detections: int = 10,
                 num_threads: int = 3) -> None:
        import onnxruntime as ort  # 検出を使うときだけ必要なので遅延 import

        opts = ort.SessionOptions()
        # 4 コアのうち 1 つは表示ループ（SPI 転送）に残す
        opts.intra_op_num_threads = num_threads
        opts.inter_op_num_threads = 1
        # 既定ではワーカースレッドが待機中もスピンして CPU を使い続け、推論を間引いても
        # 発熱が下がらないため、待機中は眠らせる
        opts.add_session_config_entry('session.intra_op.allow_spinning', '0')
        self._session = ort.InferenceSession(
            str(model_path), sess_options=opts, providers=['CPUExecutionProvider'],
        )
        inp = self._session.get_inputs()[0]
        self._input_name = inp.name
        self._input_h, self._input_w = int(inp.shape[2]), int(inp.shape[3])
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self.max_detections = max_detections

    def detect(self, rgb: np.ndarray) -> list[Detection]:
        """rgb: (H, W, 3) uint8 の RGB 画像。座標は rgb の画素座標で返す。"""
        tensor, scale, pad_x, pad_y = self._preprocess(rgb)
        out = self._session.run(None, {self._input_name: tensor})[0]
        return self._postprocess(out, scale, pad_x, pad_y, rgb.shape[1], rgb.shape[0])

    def _preprocess(self, rgb: np.ndarray) -> tuple[np.ndarray, float, int, int]:
        """アスペクト比を保って縮小し、余白を灰色で埋めて入力サイズに合わせる。"""
        h, w = rgb.shape[:2]
        scale = min(self._input_w / w, self._input_h / h)
        new_w, new_h = round(w * scale), round(h * scale)
        if (new_w, new_h) != (w, h):
            rgb = np.asarray(Image.fromarray(rgb).resize((new_w, new_h), Image.BILINEAR))
        pad_x = (self._input_w - new_w) // 2
        pad_y = (self._input_h - new_h) // 2
        canvas = np.full((self._input_h, self._input_w, 3), _PAD_VALUE, dtype=np.uint8)
        canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = rgb
        tensor = canvas.transpose(2, 0, 1)[np.newaxis].astype(np.float32) / 255.0
        return tensor, scale, pad_x, pad_y

    def _postprocess(self, out: np.ndarray, scale: float, pad_x: int, pad_y: int,
                     img_w: int, img_h: int) -> list[Detection]:
        preds = out[0].T  # (N, 4 + クラス数)
        class_scores = preds[:, 4:]
        class_ids = class_scores.argmax(axis=1)
        scores = class_scores[np.arange(len(class_ids)), class_ids]
        keep = scores >= self.conf_threshold
        if not keep.any():
            return []
        preds, class_ids, scores = preds[keep], class_ids[keep], scores[keep]

        cx, cy, bw, bh = preds[:, 0], preds[:, 1], preds[:, 2], preds[:, 3]
        boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1)
        # レターボックスを外して元画像の座標に戻す
        boxes -= np.array([pad_x, pad_y, pad_x, pad_y], dtype=np.float32)
        boxes /= scale
        boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, img_w - 1)
        boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, img_h - 1)

        idx = _nms(boxes, scores, class_ids, self.iou_threshold)[:self.max_detections]
        return [
            Detection(*map(float, boxes[i]), float(scores[i]), int(class_ids[i]))
            for i in idx
        ]


def _nms(boxes: np.ndarray, scores: np.ndarray, class_ids: np.ndarray,
         iou_threshold: float) -> list[int]:
    """クラスごとの NMS。クラス ID 分だけ座標をずらし、異なるクラス同士は抑制しない。"""
    offset = class_ids[:, np.newaxis].astype(np.float32) * 4096.0
    b = boxes + offset
    areas = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        rest = order[1:]
        xx1 = np.maximum(b[i, 0], b[rest, 0])
        yy1 = np.maximum(b[i, 1], b[rest, 1])
        xx2 = np.minimum(b[i, 2], b[rest, 2])
        yy2 = np.minimum(b[i, 3], b[rest, 3])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        iou = inter / (areas[i] + areas[rest] - inter + 1e-6)
        order = rest[iou < iou_threshold]
    return keep


class DetectionWorker:
    """最新フレームを別スレッドで推論し、直近の検出結果を保持する。

    推論中に届いたフレームは最新の 1 枚だけを残して捨てる（遅れを溜めない）。
    推論は ONNX Runtime 内で GIL を解放するため、表示ループは止まらない。
    """

    # これより古い検出結果は表示しない（camera モードから戻った直後などの残像防止）。
    # 推論間隔が長いと次の結果が届く前に消えて点滅するため、間隔の 2 倍は保持する
    STALE_SEC = 1.5

    def __init__(self, detector: YoloDetector, min_interval: float = 0.0) -> None:
        self._detector = detector
        # 推論の開始間隔の下限（秒）。連続推論による発熱で Pi がスロットリングしないよう間引く
        self._min_interval = min_interval
        self._stale_sec = max(self.STALE_SEC, 2 * min_interval)
        self._cond = threading.Condition()
        self._frame: np.ndarray | None = None
        self._detections: list[Detection] = []
        self._detected_at = 0.0
        self.inference_sec = 0.0
        threading.Thread(target=self._loop, daemon=True, name='detector').start()

    def submit(self, rgb: np.ndarray) -> None:
        with self._cond:
            self._frame = rgb
            self._cond.notify()

    def latest(self, t: float) -> list[Detection]:
        with self._cond:
            if t - self._detected_at > self._stale_sec:
                return []
            return self._detections

    def _loop(self) -> None:
        last_start = 0.0
        while True:
            wait = last_start + self._min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            with self._cond:
                while self._frame is None:
                    self._cond.wait()
                frame, self._frame = self._frame, None
            start = last_start = time.time()
            try:
                detections = self._detector.detect(frame)
            except Exception as exc:  # 推論の失敗で表示ごと止めない
                print(f'物体検出に失敗しました: {exc}', flush=True)
                time.sleep(1.0)
                continue
            now = time.time()
            with self._cond:
                self._detections = detections
                self._detected_at = now
                self.inference_sec = now - start


def _clamp_into_circle(x: float, y: float, w: float, h: float,
                       size: int) -> tuple[float, float]:
    """矩形 (x, y, w, h) を円形ディスプレイの表示範囲に収まるよう左右にずらす。"""
    r = size / 2 - 2
    c = size / 2
    # 矩形の上辺・下辺のうち中心から遠い方で、使える横幅が決まる
    dy = max(abs(y - c), abs(y + h - c))
    if dy >= r:
        return x, y
    half = math.sqrt(r * r - dy * dy)
    lo, hi = c - half, c + half - w
    if lo <= hi:
        x = min(max(x, lo), hi)
    return x, y


def draw_detections(arr: np.ndarray, detections: list[Detection],
                    font: ImageFont.ImageFont) -> np.ndarray:
    """検出枠と「ラベル 信頼度%」を arr に描いて返す。"""
    if not detections:
        return arr
    img = Image.fromarray(arr)
    draw = ImageDraw.Draw(img)
    size = arr.shape[1]
    for det in detections:
        color = _PALETTE[det.class_id % len(_PALETTE)]
        draw.rectangle([det.x1, det.y1, det.x2, det.y2], outline=color, width=2)

        text = f'{det.label} {det.score * 100:.0f}%'
        tx1, ty1, tx2, ty2 = draw.textbbox((0, 0), text, font=font)
        tw, th = tx2 - tx1 + 6, ty2 - ty1 + 4
        # 枠の上に置き、画面上端にかかるなら枠の内側に置く
        lx, ly = det.x1, det.y1 - th
        if ly < 0:
            lx, ly = det.x1 + 2, det.y1 + 2
        lx, ly = _clamp_into_circle(lx, ly, tw, th, size)
        draw.rectangle([lx, ly, lx + tw, ly + th], fill=color)
        # 明るい背景色には黒文字、暗い背景色には白文字
        luminance = 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]
        text_color = (0, 0, 0) if luminance > 150 else (255, 255, 255)
        draw.text((lx + 3 - tx1, ly + 2 - ty1), text, fill=text_color, font=font)
    return np.asarray(img, dtype=np.uint8)
