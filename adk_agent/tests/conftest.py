# -*- coding: utf-8 -*-
"""Cấu hình pytest cho package ingest: chèn thư mục adk_agent vào sys.path
để test import được package ingest, và bật asyncio_mode auto để test
async không cần decorator từng hàm.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
