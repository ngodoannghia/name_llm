# -*- coding: utf-8 -*-
"""Package ingest ADK cho DataMate: workflow ingest một shop thành SQLite.

Public API chỉ export qua module này (xem CODE_DESIGN §4.2):
- IngestConfig: cấu hình ingest (giới hạn loop, mẫu, distinct, ...).
- build_ingest_workflow: ráp Workflow ingest (nơi duy nhất ráp graph).
- FakeLlm: model giả cho test và chạy thử luồng (không phải production).
- IngestReport: báo cáo kết quả ingest một shop.
- run_ingest_all: chạy ingest tuần tự cho nhiều shop (async, dùng trong
  driver hoặc script gọi trực tiếp).
"""

from __future__ import annotations

from ._config import IngestConfig
from ._models import IngestReport
from ._testing import FakeLlm
from ._driver import run_ingest_all
from ._workflow import build_ingest_workflow

__all__ = [
    "IngestConfig",
    "IngestReport",
    "FakeLlm",
    "build_ingest_workflow",
    "run_ingest_all",
]
