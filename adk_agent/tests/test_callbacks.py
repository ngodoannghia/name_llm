# -*- coding: utf-8 -*-
"""Test guard after_model chặn JSON không hợp schema (vụ biz_127).

Guard phải biến JSON lỗi/cụt thành sentinel hợp schema để validator bác
bằng Verdict fail bình thường, thay vì để ValidationError giết cả
workflow. Các test này gọi trực tiếp callback, không cần chạy workflow.
"""

from __future__ import annotations

import json

from google.adk.models.llm_response import LlmResponse
from google.adk.utils._schema_utils import validate_schema
from google.genai import types

from ingest import IngestConfig
from ingest._callbacks import (
    _INVALID_JSON_SENTINELS,
    make_after_model_json_guard,
)
from ingest.nodes._validate_nodes import (
    validate_header,
    validate_merge,
    validate_schema_meta,
    validate_schema_table,
)
from ingest._models import (
    HeaderProposal,
    MergeProposal,
    ShopMeta,
    ShopSchema,
    TableSchema,
)
from ingest.agents._schema_table_agent import build_schema_table_agent
from ingest import FakeLlm

CONFIG = IngestConfig()


def _response(text: str, partial: bool = False) -> LlmResponse:
  """Tạo LlmResponse model với một part text (dùng cho test guard)."""
  return LlmResponse(
      content=types.Content(role="model", parts=[types.Part(text=text)]),
      partial=partial,
  )


def test_guard_keeps_valid_json() -> None:
  """JSON hợp schema thì guard giữ nguyên (trả None)."""
  guard = make_after_model_json_guard(TableSchema)
  text = json.dumps(
      {"table_id": "don", "sqlite_table": "don", "columns": []},
      ensure_ascii=False,
  )
  assert guard(None, _response(text)) is None


def test_guard_replaces_truncated_json_with_sentinel() -> None:
  """JSON cụt (vụ biz_127) thì guard thay bằng sentinel hợp schema."""
  guard = make_after_model_json_guard(TableSchema)
  truncated = '{\n  "table_id": "thanh_toan",\n  "columns": [{"original_name": "'
  altered = guard(None, _response(truncated))
  assert altered is not None
  altered_text = "".join(
      part.text for part in altered.content.parts if part.text
  )
  parsed = json.loads(altered_text)
  assert parsed["table_id"] == "__invalid_json__"
  # Sentinel phải tự hợp schema (bước save của ADK không được raise).
  validate_schema(TableSchema, altered_text)


def test_guard_replaces_schema_mismatched_json() -> None:
  """JSON hợp cú pháp nhưng thiếu field bắt buộc cũng bị thay sentinel."""
  guard = make_after_model_json_guard(TableSchema)
  missing_field = json.dumps({"table_id": "don"}, ensure_ascii=False)
  altered = guard(None, _response(missing_field))
  assert altered is not None
  parsed = json.loads(
      "".join(part.text for part in altered.content.parts if part.text)
  )
  assert parsed["table_id"] == "__invalid_json__"


def test_guard_ignores_partial_responses() -> None:
  """Response partial (streaming chưa xong) thì guard không chạm vào."""
  guard = make_after_model_json_guard(TableSchema)
  truncated = '{\n  "table_id": "thanh_toan",\n  "columns": [{"original_name": "'
  assert guard(None, _response(truncated, partial=True)) is None


def test_guard_replaces_response_without_text_with_sentinel() -> None:
  """Response cuối không có text thì guard thay bằng sentinel.

  Hành vi cũ (giữ nguyên, trả None) đã bị sửa: response rỗng đi tiếp sẽ
  thành output None rồi function node coerce lỗi — đúng lớp crash vụ
  biz_127, chỉ khác trigger. Test này khóa hành vi mới.
  """
  guard = make_after_model_json_guard(TableSchema)
  empty = LlmResponse(
      content=types.Content(role="model", parts=[]),
  )
  altered = guard(None, empty)
  assert altered is not None
  altered_text = "".join(
      part.text for part in altered.content.parts if part.text
  )
  assert json.loads(altered_text)["table_id"] == "__invalid_json__"
  blank = _response("   ")
  altered_blank = guard(None, blank)
  assert altered_blank is not None
  blank_text = "".join(
      part.text for part in altered_blank.content.parts if part.text
  )
  assert json.loads(blank_text)["table_id"] == "__invalid_json__"


def test_guard_empty_response_sentinel_fails_validator() -> None:
  """Sentinel từ response rỗng đi qua validator thì thành Verdict fail."""
  guard = make_after_model_json_guard(TableSchema)
  empty = LlmResponse(
      content=types.Content(role="model", parts=[]),
  )
  altered = guard(None, empty)
  assert altered is not None
  altered_text = "".join(
      part.text for part in altered.content.parts if part.text
  )
  sentinel_model = TableSchema.model_validate_json(altered_text)
  draft = {
      "table_id": "don",
      "original_names": [],
      "source_sheets": [],
      "sqlite_table": "don",
      "row_count": 1,
      "columns": [],
  }
  event = validate_schema_table(
      sentinel_model, schema_table_drafts=[draft], schema_table_index=0
  )
  assert event.output["status"] == "fail"


def test_guard_sentinels_for_other_schemas() -> None:
  """Mỗi schema còn lại đều có sentinel riêng, hợp schema của nó."""
  cases = [
      (MergeProposal, '{\n  "groups": [{"tables": ["don"], '),
      (HeaderProposal, '{\n  "tables": [{"table_id": "don", '),
      (ShopMeta, '{\n  "join_hints": [{"from_table": "don", '),
  ]
  for schema, truncated in cases:
    guard = make_after_model_json_guard(schema)
    altered = guard(None, _response(truncated))
    assert altered is not None
    altered_text = "".join(
        part.text for part in altered.content.parts if part.text
    )
    # Sentinel phải tự hợp schema.
    validate_schema(schema, altered_text)


def test_guard_ignores_unknown_schema() -> None:
  """Schema không có trong bảng sentinel thì guard không làm gì."""
  guard = make_after_model_json_guard(ShopSchema)
  truncated = '{\n  "business_id": "biz_127",\n  "tables": [{"table_id": "'
  assert guard(None, _response(truncated)) is None


def test_guard_sentinels_rejected_by_validators() -> None:
  """Bất biến: sentinel của cả 4 schema đều bị validator bác.

  Test cũ chỉ assert sentinel hợp schema nên lỗ header (sentinel rỗng
  được validator cho qua) mới lọt qua. Test này khóa đầu còn lại của
  bất biến: sentinel phải fail ở validator, không chỉ hợp schema.
  """
  table_sentinel = TableSchema.model_validate(
      _INVALID_JSON_SENTINELS[TableSchema]
  )
  draft = {
      "table_id": "don",
      "original_names": [],
      "source_sheets": [],
      "sqlite_table": "don",
      "row_count": 1,
      "columns": [],
  }
  event = validate_schema_table(
      table_sentinel, schema_table_drafts=[draft], schema_table_index=0
  )
  assert event.output["status"] == "fail"

  merge_sentinel = MergeProposal.model_validate(
      _INVALID_JSON_SENTINELS[MergeProposal]
  )
  raw = [{"table_id": "don", "column_signature": "ma|tien"}]
  event = validate_merge(merge_sentinel, raw)
  assert event.output["status"] == "fail"

  header_sentinel = HeaderProposal.model_validate(
      _INVALID_JSON_SENTINELS[HeaderProposal]
  )
  raw_header = [
      {
          "table_id": "don",
          "header": ["Mã đơn", "Tổng tiền"],
          "header_row": 0,
          "ambiguous": True,
          "candidate_rows": [["Mã đơn", "Tổng tiền"]],
      }
  ]
  event = validate_header(header_sentinel, raw_header)
  assert event.output["status"] == "fail"

  # Đề xuất rỗng cũng phải fail khi còn bảng mơ hồ (luật mới).
  event = validate_header(HeaderProposal(tables=[]), raw_header)
  assert event.output["status"] == "fail"

  meta_sentinel = ShopMeta.model_validate(_INVALID_JSON_SENTINELS[ShopMeta])
  results = [
      {
          "sqlite_table": "don",
          "columns": [{"sqlite_name": "ma", "role_hint": "order_id"}],
      }
  ]
  event = validate_schema_meta(meta_sentinel, schema_table_results=results)
  assert event.output["status"] == "fail"


def test_agents_have_after_model_json_guard() -> None:
  """Agent ingest phải gắn guard after_model (không chỉ validator)."""
  agent = build_schema_table_agent(CONFIG, model=FakeLlm())
  assert agent.after_model_callback is not None
