# -*- coding: utf-8 -*-
"""Package ``src`` của Giai đoạn 1 DataMate — tầng ingest.

Module này chỉ export nhẹ các tên public của tầng ingest để code khác
dùng một chỗ duy nhất (``from src.ingest import load_shop`` hay
``from src import load_shop`` đều được). Các module rulebook/evaluate
của agent khác sẽ tự export trong file của họ, không viết ở đây.

Việc import package sẽ kéo theo pandas (do ingest cần), nên môi trường
chạy phải có pandas; nếu thiếu thì lỗi ImportError sẽ nói rõ module
nào cần, thay vì im lặng trả về tên rỗng.
"""

from .ingest import (
    COLUMN_SYNONYMS,
    IngestResult,
    ShopProfile,
    detect_header_row,
    load_all_shops,
    load_shop,
    map_columns,
)
from .normalizers import parse_date, parse_money, parse_quantity
from .text_utils import (
    normalize_key,
    normalize_vn_display,
    strip_diacritics,
)

__all__ = [
    "COLUMN_SYNONYMS",
    "IngestResult",
    "ShopProfile",
    "detect_header_row",
    "load_all_shops",
    "load_shop",
    "map_columns",
    "parse_date",
    "parse_money",
    "parse_quantity",
    "normalize_key",
    "normalize_vn_display",
    "strip_diacritics",
]
