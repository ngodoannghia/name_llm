# -*- coding: utf-8 -*-
"""Test agent, callback guardrail và nạp skill."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ingest._callbacks import (
    after_tool_callback,
    before_tool_callback,
    on_tool_error_callback,
)
from ingest._config import IngestConfig
from ingest._skills_loader import load_single_skill
from ingest._testing import FakeLlm
from ingest.agents._header_agent import build_header_agent
from ingest.agents._merge_agent import build_merge_agent
from ingest.agents._schema_meta_agent import build_schema_meta_agent
from ingest.agents._schema_table_agent import build_schema_table_agent
from ingest._models import HeaderProposal, MergeProposal, ShopMeta, TableSchema

CONFIG = IngestConfig()
SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def test_agents_have_inline_skill_no_tools_and_schema() -> None:
  """Bốn agent đều single_turn, include_contents none, đúng output_schema.

  Agent ingest không có tool nào: skill được inline vào instruction để
  model thật không thể rơi vào vòng lặp load_skill_resource (lỗi test
  luồng thật 2026-10-31: 64 lần load cùng file rồi vượt cửa sổ context).
  Schema tách thành 2 agent: một bảng mỗi lần (TableSchema) và meta
  cấp shop (ShopMeta), để không còn lần viết toàn bộ schema một lúc.
  """
  fake = FakeLlm()
  cases = [
      (build_header_agent(CONFIG, model=fake), "header_agent", HeaderProposal, "header_proposal", "datamate-header"),
      (build_merge_agent(CONFIG, model=fake), "merge_agent", MergeProposal, "merge_proposal", "datamate-merge"),
      (build_schema_table_agent(CONFIG, model=fake), "schema_table_agent", TableSchema, "schema_table_proposal", "datamate-schema"),
      (build_schema_meta_agent(CONFIG, model=fake), "schema_meta_agent", ShopMeta, "schema_meta", "datamate-schema"),
  ]
  for agent, name, schema, key, skill_name in cases:
    assert agent.name == name
    assert agent.output_schema is schema
    assert agent.output_key == key
    assert agent.include_contents == "none"
    assert str(agent.mode) == "single_turn"
    assert len(agent.tools) == 0
    assert f"NỘI DUNG SKILL {skill_name}" in agent.instruction


def test_agent_instruction_names_its_skill() -> None:
  """Instruction mỗi agent ghi rõ tên skill đã inline."""
  fake = FakeLlm()
  assert "datamate-header" in build_header_agent(CONFIG, model=fake).instruction
  assert "datamate-merge" in build_merge_agent(CONFIG, model=fake).instruction
  assert "datamate-schema" in build_schema_table_agent(CONFIG, model=fake).instruction
  assert "datamate-schema" in build_schema_meta_agent(CONFIG, model=fake).instruction


def test_agent_without_model_raises() -> None:
  """Dựng agent không model và không model_id thì fail fast."""
  with pytest.raises(ValueError, match="không có model"):
    build_header_agent(IngestConfig(model_id=""))


def test_load_single_skill_all_four() -> None:
  """Nạp được cả 4 skill, tên skill trùng tên thư mục."""
  for name in ("datamate-header", "datamate-merge", "datamate-cleaning", "datamate-schema"):
    skill = load_single_skill(SKILLS_DIR, name)
    assert skill is not None
    assert skill.name == name


def test_load_single_skill_missing_returns_none() -> None:
  """Skill thiếu thì trả None (agent chạy không tool, không crash)."""
  assert load_single_skill(SKILLS_DIR, "khong-co-skill") is None


def test_before_tool_allows_skill_tools_blocks_others() -> None:
  """before_tool cho qua tool đọc skill, chặn tool ghi/script."""
  ctx = SimpleNamespace()
  for allowed in ("list_skills", "load_skill", "load_skill_resource"):
    assert before_tool_callback(SimpleNamespace(name=allowed), {}, ctx) is None
  for blocked in ("write_file", "run_skill_script", "run_sql"):
    assert before_tool_callback(SimpleNamespace(name=blocked), {}, ctx) is not None


def test_after_tool_truncates_long_strings() -> None:
  """after_tool cắt chuỗi dài, giữ nguyên kết quả ngắn."""
  ctx = SimpleNamespace()
  tool = SimpleNamespace(name="load_skill")
  assert after_tool_callback(tool, {}, ctx, {"a": "ngắn"}) is None
  truncated = after_tool_callback(tool, {}, ctx, {"a": "x" * 9000})
  assert truncated is not None and len(truncated["a"]) < 9000


def test_on_tool_error_structured_payload() -> None:
  """on_tool_error trả dict có tool và thông điệp lỗi."""
  payload = on_tool_error_callback(
      SimpleNamespace(name="load_skill"), {"k": 1}, SimpleNamespace(), RuntimeError("lỗi")
  )
  assert payload["tool"] == "load_skill"
  assert "lỗi" in payload["error"]
  assert payload["args_keys"] == ["k"]
