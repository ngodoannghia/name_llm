# -*- coding: utf-8 -*-
"""Nạp skill ADK từ thư mục skills/ (mỗi agent chỉ nạp đúng 1 skill).

Module này thuộc tầng nạp skill. Nguyên tắc đã kiểm chứng từ source ADK
(xem plan §2.4 mục 4): skill là dữ liệu thụ động, framework không ép
agent nạp đúng skill ở đúng bước. Cách ép duy nhất là cấu trúc: mỗi
agent chỉ có SkillToolset chứa đúng 1 skill đó, và instruction của agent
ghi rõ tên skill phải nạp đầu tiên. Skill thiếu thì agent chạy không có
tool và log warning, không crash workflow (validator vẫn bắt lỗi sau đó).
"""

from __future__ import annotations

from pathlib import Path

from google.adk.skills import load_skill_from_dir
from google.adk.skills.models import Skill

from ._logging import get_logger

logger = get_logger(__name__)


def load_single_skill(
    skills_dir: str | Path, skill_name: str
) -> Skill | None:
  """Nạp một skill theo tên thư mục (tên skill phải trùng tên thư mục).

  Args:
    skills_dir: Thư mục cha chứa các skill (ví dụ .../adk_agent/skills).
    skill_name: Tên skill, trùng tên thư mục con (kebab-case).

  Returns:
    Đối tượng Skill khi nạp được, ngược lại None kèm log warning (agent
    sẽ chạy không có tool thay vì crash — validator vẫn kiểm chứng kết
    quả của agent ở bước sau).
  """
  skill_dir = Path(skills_dir) / skill_name
  skill_file = skill_dir / "SKILL.md"
  if not skill_file.is_file():
    logger.warning(
        "Không tìm thấy skill %s tại %s; agent sẽ chạy không có tool.",
        skill_name,
        skill_dir,
    )
    return None
  try:
    skill = load_skill_from_dir(skill_dir)
  except Exception as error:
    logger.warning(
        "Nạp skill %s thất bại (%s); agent sẽ chạy không có tool.",
        skill_name,
        error,
    )
    return None
  return skill
