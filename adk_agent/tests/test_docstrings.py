# -*- coding: utf-8 -*-
"""Test quét AST: mọi module/class/hàm trong package ingest phải có
docstring tiếng Việt có dấu, và mọi field Pydantic phải có attribute
docstring (quy tắc CODE_DESIGN §4.4, thực thi bằng test thay vì nhắc miệng).

Quy tắc quét:
- Mọi file .py trong ingest/ (kể cả __init__ và module private) phải có
  module docstring.
- Mọi class và mọi hàm (kể cả hàm lồng trong hàm khác, kể cả private)
  phải có docstring chứa ít nhất một ký tự tiếng Việt có dấu.
- Mọi field của class kế thừa BaseModel phải có attribute docstring:
  chuỗi literal đứng ngay sau câu lệnh gán field trong thân class.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "ingest"
VIETNAMESE_DIACRITIC = re.compile(r"[àáảãạăắằẳẵặâấầẩẫậèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợùúủũụưứừửữựỳýỷỹỵđÀÁẢÃẠĂẮẰẲẴẶÂẤẦẨẪẬÈÉẺẼẸÊẾỀỂỄỆÌÍỈĨỊÒÓỎÕỌÔỐỒỔỖỘƠỚỜỞỠỢÙÚỦŨỤƯỨỪỬỮỰỲÝỶỸỴĐ]")


def _iter_python_files() -> list[Path]:
  """Liệt kê mọi file .py trong package ingest (trừ __pycache__)."""
  return sorted(
      path
      for path in PACKAGE_DIR.rglob("*.py")
      if "__pycache__" not in path.parts
  )


def _has_vietnamese(docstring: str | None) -> bool:
  """Kiểm tra docstring có chứa ký tự tiếng Việt có dấu hay không."""
  return docstring is not None and VIETNAMESE_DIACRITIC.search(docstring) is not None


def test_every_module_has_vietnamese_docstring() -> None:
  """Mọi module trong ingest/ phải có module docstring tiếng Việt."""
  missing = []
  for path in _iter_python_files():
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstring = ast.get_docstring(tree)
    if not _has_vietnamese(docstring):
      missing.append(str(path))
  assert not missing, f"Module thiếu docstring tiếng Việt: {missing}"


def test_every_class_and_function_has_vietnamese_docstring() -> None:
  """Mọi class/hàm trong ingest/ phải có docstring tiếng Việt có dấu."""
  missing = []
  for path in _iter_python_files():
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
      if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        docstring = ast.get_docstring(node)
        if not _has_vietnamese(docstring):
          missing.append(f"{path}:{node.lineno} {node.name}")
  assert not missing, f"Class/hàm thiếu docstring tiếng Việt: {missing}"


def test_every_pydantic_field_has_attribute_docstring() -> None:
  """Mọi field Pydantic phải có attribute docstring ngay sau câu gán."""
  missing = []
  for path in _iter_python_files():
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
      if not isinstance(node, ast.ClassDef):
        continue
      base_names = []
      for base in node.bases:
        if isinstance(base, ast.Name):
          base_names.append(base.id)
        elif isinstance(base, ast.Attribute):
          base_names.append(base.attr)
      if "BaseModel" not in base_names:
        continue
      body = node.body
      for index, stmt in enumerate(body):
        targets = []
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
          # model_config là cấu hình class của Pydantic, không phải field
          # dữ liệu nên không cần attribute docstring.
          if stmt.target.id == "model_config":
            continue
          targets = [stmt.target.id]
        elif isinstance(stmt, ast.Assign):
          targets = [
              target.id for target in stmt.targets if isinstance(target, ast.Name)
              and target.id != "model_config"
          ]
        if not targets:
          continue
        next_stmt = body[index + 1] if index + 1 < len(body) else None
        has_doc = (
            isinstance(next_stmt, ast.Expr)
            and isinstance(next_stmt.value, ast.Constant)
            and isinstance(next_stmt.value.value, str)
            and _has_vietnamese(next_stmt.value.value)
        )
        if not has_doc:
          missing.append(f"{path}:{node.name}.{targets[0]}")
  assert not missing, f"Field thiếu attribute docstring: {missing}"
