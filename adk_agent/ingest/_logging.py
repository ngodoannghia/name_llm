# -*- coding: utf-8 -*-
"""Logger chuẩn cho package ingest.

Module này thuộc tầng tiện ích: mọi module khác lấy logger theo tên
module của chính nó, không dùng logger root và không dùng ``print``
trong code production.
"""

from __future__ import annotations

import logging


def get_logger(name: str) -> logging.Logger:
  """Trả về logger theo tên module.

  Args:
    name: Tên module, thường là ``__name__`` của module gọi.

  Returns:
    Logger đã đặt tên theo module, dùng chung cấu hình logging của tiến trình.
  """
  return logging.getLogger(name)
