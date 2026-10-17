"""リポジトリ(Pi では ~/desk-monitor)内の素材・設定・モデルの置き場所。

ソースは src/desk_monitor/ にあり、素材などはリポジトリ直下の assets/ config/ models/ にある。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FACES_DIR = ROOT / 'assets' / 'faces'
FONTS_DIR = ROOT / 'assets' / 'fonts'
CONFIG_DIR = ROOT / 'config'
MODELS_DIR = ROOT / 'models'
