# -*- coding: utf-8 -*-
"""Test FakeLlm: dispatch theo nội dung request và canned theo thứ tự."""

from __future__ import annotations

import json

from google.adk.models.llm_request import LlmRequest
from google.genai import types

from ingest._testing import FakeLlm


def _request_with_text(text: str) -> LlmRequest:
  """Tạo LlmRequest giả lập chỉ chứa một user content mang text cho trước."""
  return LlmRequest(
      model="fake-llm",
      contents=[types.Content(role="user", parts=[types.Part(text=text)])],
  )


async def _first_response_text(fake: FakeLlm, text: str) -> str:
  """Chạy FakeLlm một lượt và trả text của response đầu tiên."""
  responses = []
  async for response in fake.generate_content_async(_request_with_text(text)):
    responses.append(response)
  assert len(responses) == 1
  return responses[0].content.parts[0].text


async def test_fake_dispatch_header() -> None:
  """FakeLlm sinh HeaderProposal từ JSON bảng mơ hồ."""
  text = json.dumps(
      {"ambiguous_tables": [{"table_id": "don", "header": ["Mã đơn", "Tổng tiền"]}]},
      ensure_ascii=False,
  )
  parsed = json.loads(await _first_response_text(FakeLlm(), text))
  assert parsed["tables"][0]["table_id"] == "don"
  assert parsed["tables"][0]["columns"] == ["Mã đơn", "Tổng tiền"]


async def test_fake_dispatch_merge() -> None:
  """FakeLlm sinh MergeProposal giữ riêng từ JSON tóm tắt bảng."""
  text = json.dumps(
      {"tables": [
          {"table_id": "don", "column_signature": "ma|tien", "row_count": 2},
          {"table_id": "khach", "column_signature": "ten", "row_count": 3},
      ]},
      ensure_ascii=False,
  )
  parsed = json.loads(await _first_response_text(FakeLlm(), text))
  assert len(parsed["groups"]) == 2
  assert parsed["singles"] == []


async def test_fake_dispatch_schema() -> None:
  """FakeLlm sinh ShopSchema từ bản nháp schema (giữ mẫu/distinct)."""
  text = (
      "Shop đang chạy có business_id là: biz_test\n"
      + json.dumps(
          {"tables": [
              {
                  "table_id": "don",
                  "original_names": ["don"],
                  "source_sheets": ["don"],
                  "sqlite_table": "don",
                  "row_count": 2,
                  "description_vi": "",
                  "columns": [
                      {
                          "original_name": "ma",
                          "sqlite_name": "ma",
                          "type": "TEXT",
                          "description_vi": "",
                          "role_hint": "unknown",
                          "sample_values": ["DH1"],
                          "distinct_count": 2,
                          "distinct_values": ["DH1", "DH2"],
                          "nullable_rate": 0.0,
                      }
                  ],
                  "date_range": {},
              }
          ]},
          ensure_ascii=False,
      )
  )
  parsed = json.loads(await _first_response_text(FakeLlm(), text))
  assert parsed["business_id"] == "biz_test"
  assert parsed["tables"][0]["columns"][0]["sample_values"] == ["DH1"]
  assert parsed["tables"][0]["columns"][0]["distinct_values"] == ["DH1", "DH2"]


async def test_fake_canned_in_order() -> None:
  """FakeLlm trả canned theo đúng thứ tự rồi mới dispatch."""
  fake = FakeLlm(canned=[{"canned": 1}, {"canned": 2}])
  first = json.loads(await _first_response_text(fake, "bất kỳ"))
  second = json.loads(await _first_response_text(fake, "bất kỳ"))
  assert first == {"canned": 1}
  assert second == {"canned": 2}


async def test_fake_dispatch_table() -> None:
  """FakeLlm sinh TableSchema từ bản nháp MỘT bảng (giữ giá trị đo)."""
  text = json.dumps(
      {"table": {
          "table_id": "don",
          "original_names": ["don"],
          "source_sheets": ["don"],
          "sqlite_table": "don",
          "row_count": 2,
          "description_vi": "",
          "columns": [
              {
                  "original_name": "ma",
                  "sqlite_name": "ma",
                  "type": "TEXT",
                  "description_vi": "",
                  "role_hint": "unknown",
                  "sample_values": ["DH1"],
                  "distinct_count": 2,
                  "distinct_values": ["DH1", "DH2"],
                  "nullable_rate": 0.0,
              }
          ],
          "date_range": {},
      }},
      ensure_ascii=False,
  )
  parsed = json.loads(await _first_response_text(FakeLlm(), text))
  assert parsed["sqlite_table"] == "don"
  assert parsed["columns"][0]["sample_values"] == ["DH1"]
  assert parsed["columns"][0]["distinct_values"] == ["DH1", "DH2"]


async def test_fake_dispatch_meta() -> None:
  """FakeLlm sinh ShopMeta rỗng từ tóm tắt meta."""
  text = json.dumps(
      {"tables_summary": [
          {"sqlite_table": "don",
           "columns": [{"sqlite_name": "ma", "role_hint": "order_id"}]},
      ]},
      ensure_ascii=False,
  )
  parsed = json.loads(await _first_response_text(FakeLlm(), text))
  assert parsed == {"join_hints": [], "notes": []}
