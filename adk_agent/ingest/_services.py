# -*- coding: utf-8 -*-
"""Khởi tạo service cho workflow: session service và artifact service.

Module này thuộc tầng service. Session giữ state của shop (một session
mỗi shop); artifact service lưu artifact local. Driver mặc định dùng
InMemorySessionService (đủ cho chạy batch một lần); DatabaseSessionService
chỉ dùng khi cần lưu session bền vững qua nhiều lần chạy và yêu cầu URL
async driver + extra google-adk[db] (xem plan §2.4 mục 8).
"""

from __future__ import annotations

from pathlib import Path

from google.adk.artifacts.file_artifact_service import FileArtifactService
from google.adk.sessions import InMemorySessionService
from google.adk.sessions.database_session_service import DatabaseSessionService


def get_session_service(kind: str = "memory") -> object:
  """Tạo session service theo loại.

  Args:
    kind: "memory" (InMemorySessionService, mặc định) hoặc "database"
      (DatabaseSessionService với SQLite async local).

  Returns:
    Session service đã tạo.

  Raises:
    ValueError: Khi kind không phải một trong hai loại trên.
  """
  if kind == "memory":
    return InMemorySessionService()
  if kind == "database":
    # URL async driver là bắt buộc cho DatabaseSessionService (plan §2.4
    # mục 8); file sessions.db nằm trong thư mục làm việc của driver,
    # không nằm trong thư mục dữ liệu train.
    return DatabaseSessionService(db_url="sqlite+aiosqlite:///sessions.db")
  raise ValueError(f"Loại session service không hỗ trợ: {kind!r}.")


def get_artifact_service(root_dir: str | Path) -> FileArtifactService:
  """Tạo FileArtifactService lưu artifact local dưới root_dir.

  Args:
    root_dir: Thư mục gốc chứa artifact (sẽ được tạo nếu chưa có).

  Returns:
    FileArtifactService đã tạo.
  """
  return FileArtifactService(root_dir)
