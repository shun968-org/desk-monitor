from __future__ import annotations

import time

import numpy as np
import pytest

from desk_monitor import detector
from desk_monitor.detector import Detection
from desk_monitor.fonts import get_font


def make_detector(input_size=256, conf=0.4, iou=0.45, max_det=10) -> detector.YoloDetector:
    """onnxruntime を使わずに前処理・後処理だけを試すための YoloDetector。"""
    d = detector.YoloDetector.__new__(detector.YoloDetector)
    d._input_h = d._input_w = input_size
    d.conf_threshold, d.iou_threshold, d.max_detections = conf, iou, max_det
    return d


def raw_output(boxes: list[tuple[float, float, float, float, int, float]]) -> np.ndarray:
    """(cx, cy, w, h, class_id, score) の並びから YOLO の出力 (1, 84, N) を作る。"""
    out = np.zeros((1, 84, len(boxes)), dtype=np.float32)
    for i, (cx, cy, w, h, cls, score) in enumerate(boxes):
        out[0, :4, i] = (cx, cy, w, h)
        out[0, 4 + cls, i] = score
    return out


def test_labels_cover_coco_classes():
    assert len(detector.COCO_LABELS_JA) == 80
    assert Detection(0, 0, 1, 1, 0.9, 0).label == '人'
    assert Detection(0, 0, 1, 1, 0.9, 99).label == '#99'


def test_preprocess_letterboxes_to_input_size():
    d = make_detector()
    rgb = np.full((120, 240, 3), 255, dtype=np.uint8)  # 横長の画像
    tensor, scale, pad_x, pad_y = d._preprocess(rgb)
    assert tensor.shape == (1, 3, 256, 256) and tensor.dtype == np.float32
    assert scale == pytest.approx(256 / 240)
    assert pad_x == 0 and pad_y == (256 - 128) // 2
    assert tensor[0, 0, 0, 0] == pytest.approx(114 / 255)  # 余白は灰色
    assert tensor[0, 0, 128, 128] == pytest.approx(1.0)


def test_postprocess_maps_back_to_image_coordinates():
    d = make_detector()
    # 240x240 の画像を 256 にすると scale=256/240、余白なし
    scale = 256 / 240
    out = raw_output([(128, 128, 64, 32, 15, 0.9)])
    [det] = d._postprocess(out, scale, 0, 0, 240, 240)
    assert det.class_id == 15 and det.label == '猫'
    assert det.score == pytest.approx(0.9)
    assert (det.x1, det.y1, det.x2, det.y2) == pytest.approx(
        (96 / scale, 112 / scale, 160 / scale, 144 / scale))


def test_postprocess_filters_by_confidence_and_clips():
    d = make_detector()
    out = raw_output([(10, 10, 100, 100, 0, 0.8), (100, 100, 10, 10, 1, 0.2)])
    [det] = d._postprocess(out, 1.0, 0, 0, 240, 240)
    assert det.x1 == 0 and det.y1 == 0  # 画像の外ははみ出さない
    assert d._postprocess(raw_output([(1, 1, 1, 1, 0, 0.1)]), 1.0, 0, 0, 240, 240) == []


def test_postprocess_limits_detection_count():
    d = make_detector(max_det=3)
    out = raw_output([(20 * i + 10, 50, 8, 8, 0, 0.5 + i / 100) for i in range(8)])
    dets = d._postprocess(out, 1.0, 0, 0, 240, 240)
    assert len(dets) == 3
    assert [x.score for x in dets] == sorted([x.score for x in dets], reverse=True)


def test_nms_suppresses_overlaps_only_within_class():
    boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [0, 0, 10, 10], [50, 50, 60, 60]],
                     dtype=np.float32)
    scores = np.array([0.9, 0.8, 0.7, 0.6], dtype=np.float32)
    class_ids = np.array([0, 0, 1, 0])
    assert detector._nms(boxes, scores, class_ids, 0.45) == [0, 2, 3]


class FakeDetector:
    def __init__(self, result=None, error=None) -> None:
        self.result = result or []
        self.error = error
        self.calls = 0

    def detect(self, rgb):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def _wait_until(cond, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_worker_returns_latest_and_drops_stale(blank_frame):
    det = Detection(1, 2, 3, 4, 0.9, 0)
    worker = detector.DetectionWorker(FakeDetector([det]), min_interval=0.0)
    assert worker.latest(time.time()) == []
    worker.submit(blank_frame)
    assert _wait_until(lambda: worker.latest(time.time()) == [det])
    assert worker.latest(time.time() + 10) == []  # 古い結果は出さない


def test_worker_calls_result_callback(blank_frame):
    det = Detection(1, 2, 3, 4, 0.9, 0)
    results = []
    worker = detector.DetectionWorker(FakeDetector([det]),
                                      on_result=lambda frame, dets: results.append((frame, dets)))
    worker.submit(blank_frame)
    assert _wait_until(lambda: results)
    assert results[0][0] is blank_frame and results[0][1] == [det]


def test_worker_survives_callback_errors(blank_frame):
    def boom(frame, dets):
        raise RuntimeError('callback failed')
    fake = FakeDetector()
    worker = detector.DetectionWorker(fake, on_result=boom)
    worker.submit(blank_frame)
    assert _wait_until(lambda: fake.calls >= 1)
    worker.submit(blank_frame)
    assert _wait_until(lambda: fake.calls >= 2)  # 例外のあとも推論を続ける


def test_worker_stale_window_scales_with_interval():
    worker = detector.DetectionWorker(FakeDetector(), min_interval=2.0)
    assert worker._stale_sec == 4.0


def test_worker_survives_detection_errors(blank_frame, monkeypatch):
    monkeypatch.setattr(detector.time, 'sleep', lambda s: None)
    fake = FakeDetector(error=RuntimeError('推論失敗'))
    worker = detector.DetectionWorker(fake, min_interval=0.0)
    worker.submit(blank_frame)
    assert _wait_until(lambda: fake.calls >= 1)
    assert worker.latest(time.time()) == []


@pytest.mark.parametrize('x, y, w, h', [(0, 100, 50, 15), (230, 100, 50, 15), (0, 10, 40, 15)])
def test_clamp_into_circle_keeps_label_visible(x, y, w, h):
    nx, ny = detector._clamp_into_circle(x, y, w, h, 240)
    assert ny == y
    r = 118
    for cx in (nx, nx + w):
        for cy in (y, y + h):
            assert (cx - 120) ** 2 + (cy - 120) ** 2 <= r * r + 1e-6


def test_clamp_outside_circle_is_unchanged():
    assert detector._clamp_into_circle(5, 0, 20, 10, 240) == (5, 0)


def test_to_region_maps_and_clips_to_lcd_square():
    dets = [
        Detection(100, 100, 300, 200, 0.9, 0),   # 中央の正方形の中
        Detection(0, 50, 120, 100, 0.8, 1),      # 左端がはみ出す
        Detection(0, 0, 60, 40, 0.7, 2),         # 正方形の外(LCD には出ない)
    ]
    mapped = detector.to_region(dets, 80, 0, 480, 240)
    assert [(d.class_id, d.x1, d.y1, d.x2, d.y2) for d in mapped] == [
        (0, 10.0, 50.0, 110.0, 100.0),
        (1, 0.0, 25.0, 20.0, 50.0),
    ]
    assert mapped[0].score == 0.9


def test_draw_detections_without_circle_clamp():
    full = np.zeros((480, 640, 3), dtype=np.uint8)
    det = Detection(0, 300, 100, 400, 0.9, 0)
    out = detector.draw_detections(full, [det], get_font(16), circular=False)
    assert out.shape == full.shape
    assert out[299, 1].any()  # ラベルは枠の左上にそのまま置かれる(円の中へずらさない)


def test_draw_detections(blank_frame):
    assert detector.draw_detections(blank_frame, [], get_font(13)) is blank_frame
    out = detector.draw_detections(
        blank_frame, [Detection(60, 80, 180, 200, 0.87, 0)], get_font(13))
    assert out.shape == blank_frame.shape
    assert out[140, 60].any()  # 枠の左辺
    assert not out[140, 120].any()  # 枠の内側は塗らない
