# -*- coding: utf-8 -*-
"""Adapter tái sử dụng code phase1 mà không copy và không sửa phase1.

Vì sao cần adapter riêng thay vì ``import`` trực tiếp:
- Package này cũng tên là ``ingest`` nên ``import ingest`` sẽ đụng tên
  với module ``ingest.py`` của phase1 nếu cả hai cùng nằm trên sys.path.
- Các hàm đọc file của phase1 là private (tiền tố ``_``) nên khung mới
  không import chúng; khung mới tự đọc file bằng pandas/sqlite3 rồi chỉ
  gọi các API public của phase1.

Các API public được tái sử dụng (đã kiểm chứng ở phase1):
- ``detect_header_row``: chấm điểm và tìm dòng header thật trong tối đa
  15 dòng đầu (xử lý cả header 2 tầng và dòng tiêu đề).
- ``map_columns``: map cột theo ý nghĩa qua từ điển đồng nghĩa + kiểm
  chứng bằng mẫu giá trị thật (không đoán theo vị trí cột).
- ``parse_money`` / ``parse_date`` / ``parse_quantity``: parse tiền /
  ngày / số lượng, trả ``None`` khi không parse được (không gán 0, không
  đoán).
- ``strip_diacritics`` / ``normalize_key``: chuẩn hóa tiếng Việt để so
  sánh tên cột và tạo chữ ký cột cho kiểm tra gộp.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def _load_phase1_module(name: str, filename: str) -> Any:
  """Nạp một module phase1 dưới tên riêng để tránh đụng tên package.

  Args:
    name: Tên module riêng (ví dụ ``datamate_phase1_ingest``).
    filename: Tên file trong thư mục ``phase1/src``.

  Returns:
    Module đã nạp. Module phase1 tự fallback sang import tuyệt đối
    (``from normalizers import ...``) khi import tương đối thất bại, nên
    thư mục phase1/src được thêm tạm vào sys.path ở đây.
  """
  phase1_src = Path(__file__).resolve().parents[2] / "phase1" / "src"
  if str(phase1_src) not in sys.path:
    sys.path.insert(0, str(phase1_src))
  spec = importlib.util.spec_from_file_location(name, phase1_src / filename)
  if spec is None or spec.loader is None:
    raise RuntimeError(f"Không nạp được module phase1: {filename}")
  module = importlib.util.module_from_spec(spec)
  sys.modules[name] = module
  spec.loader.exec_module(module)
  return module


_phase1_ingest = _load_phase1_module("datamate_phase1_ingest", "ingest.py")
_phase1_normalizers = _load_phase1_module(
    "datamate_phase1_normalizers", "normalizers.py"
)
_phase1_text_utils = _load_phase1_module(
    "datamate_phase1_text_utils", "text_utils.py"
)

# Các hàm public của phase1 được export lại dưới tên rõ ràng để code
# ingest chỉ import từ adapter này, không import trực tiếp từ phase1.
detect_header_row = _phase1_ingest.detect_header_row
map_columns = _phase1_ingest.map_columns
parse_money = _phase1_normalizers.parse_money
parse_date = _phase1_normalizers.parse_date
parse_quantity = _phase1_normalizers.parse_quantity
strip_diacritics = _phase1_text_utils.strip_diacritics
normalize_key = _phase1_text_utils.normalize_key

__all__ = [
    "detect_header_row",
    "map_columns",
    "parse_money",
    "parse_date",
    "parse_quantity",
    "strip_diacritics",
    "normalize_key",
]
