# -*- coding: utf-8 -*-
"""Test tài nguyên skill: SKILL.md phải nêu tên đúng mọi file references.

Hồi quy cho lỗi test luồng thật ngày 2026-10-31: SchemaAgent đoán tên
file assets không tồn tại (template.json, notes.json...) và lặp hơn 250
lượt gọi load_skill_resource. SKILL.md phải nêu tên chính xác tài nguyên
của mình và cấm đoán tên file, để model không còn đường đoán mò.
"""

from __future__ import annotations

from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def test_moi_skill_neu_ten_dung_tai_nguyen() -> None:
  """Mỗi file trong references/ đều được nêu tên trong SKILL.md của skill đó."""
  skill_dirs = sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir())
  assert skill_dirs, "Không tìm thấy thư mục skill nào."
  for skill_dir in skill_dirs:
    skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    refs_dir = skill_dir / "references"
    assert refs_dir.is_dir(), f"Skill {skill_dir.name} thiếu thư mục references/."
    for ref_file in sorted(refs_dir.iterdir()):
      rel = f"references/{ref_file.name}"
      assert rel in skill_md, (
          f"SKILL.md của {skill_dir.name} không nêu tên tài nguyên {rel}; "
          "model sẽ đoán tên file và lặp gọi lỗi."
      )


def test_moi_skill_cam_doan_ten_file() -> None:
  """Mọi SKILL.md đều chứa quy tắc cấm đoán tên file tài nguyên."""
  for skill_dir in sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir()):
    skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "KHÔNG đoán tên file khác" in skill_md, (
        f"SKILL.md của {skill_dir.name} thiếu quy tắc cấm đoán tên file."
    )
