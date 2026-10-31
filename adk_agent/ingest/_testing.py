# -*- coding: utf-8 -*-
"""Model giả cho test và chạy thử luồng: FakeLlm (test double, không phải
logic production).

FakeLlm là subclass BaseLlm trả response canned theo nội dung request:
- Request chứa bản nháp schema -> sinh ShopSchema từ bản nháp (mô tả
  chung chung, role_hint unknown, join_hints rỗng) để validator pass.
- Request chứa danh sách bảng mơ hồ -> sinh HeaderProposal từ header
  ứng viên.
- Request chứa tóm tắt bảng có chữ ký -> sinh MergeProposal dạng giữ
  riêng từng bảng.
- Ngoài ra hỗ trợ danh sách response canned theo thứ tự cho test đơn vị
  (ví dụ lần đầu trả header sai để test loop retry, lần sau trả đúng).

FakeLlm CHỈ dùng cho test và driver chế độ giả. Production bắt buộc dùng
LLM thật sinh schema và fail khi schema sai; fake này cố tình sinh schema
dễ pass để chứng minh luồng chạy được đầu-cuối, không phải để đánh giá
chất lượng schema.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types


def _strip_inline_skill(system_instruction: str) -> str:
  """Cắt phần skill inline khỏi system_instruction trước khi dispatch.

  Agent ingest chạy ở chế độ inline_skill nên system_instruction bắt đầu
  bằng toàn bộ SKILL.md + references/. Văn bản skill chứa đúng các từ
  khóa và ví dụ JSON mà dispatch dùng để nhận diện input thật
  (distinct_count, key "tables") nên không cắt sẽ nhận diện sai bước.
  Instruction thật của agent luôn bắt đầu bằng "Bạn là ...Agent", còn
  văn bản skill không chứa cụm này, nên cắt từ lần xuất hiện cuối của
  "\\n\\nBạn là " là an toàn.
  """
  marker = "\n\nBạn là "
  idx = system_instruction.rfind(marker)
  if idx >= 0:
    return system_instruction[idx + 2:]
  return system_instruction


def _request_text(request: LlmRequest) -> str:
  """Gộp toàn bộ text trong request thành một chuỗi để dispatch.

  Lưu ý quan trọng đã kiểm chứng bằng debug: instruction của agent
  (kể cả placeholder state đã thay thế như business_id) KHÔNG nằm
  trong request.contents mà nằm trong request.config.system_instruction,
  còn contents chỉ chứa user message (input của node). FakeLlm phải đọc
  cả hai nơi, nếu không sẽ không thấy business_id và bản nháp schema
  trong instruction. Phần skill inline (nếu có) được cắt trước khi trả
  về, vì ví dụ trong skill sẽ làm dispatch nhận diện sai bước.

  Args:
    request: LlmRequest ADK gửi cho model.

  Returns:
    Toàn bộ text từ instruction thật của agent và mọi content trong request.
  """
  texts: list[str] = []
  config = getattr(request, "config", None)
  system_instruction = getattr(config, "system_instruction", None) if config else None
  if isinstance(system_instruction, str):
    texts.append(_strip_inline_skill(system_instruction))
  elif system_instruction is not None:
    contents = (
        system_instruction
        if isinstance(system_instruction, list)
        else [system_instruction]
    )
    for content in contents:
      for part in getattr(content, "parts", None) or []:
        if part.text:
          texts.append(part.text)
  for content in request.contents or []:
    for part in content.parts or []:
      if part.text:
        texts.append(part.text)
  return "\n".join(texts)


def _extract_json_object_with_key(
    text: str, key: str
) -> dict | None:
  """Tìm JSON object đầu tiên trong text chứa key cho trước.

  Text của request gồm instruction + input nên JSON cần tìm nằm lẫn
  trong text. Hàm này quét từng vị trí "{" và thử parse bằng
  JSONDecoder.raw_decode, trả về object đầu tiên có key cần tìm.

  Args:
    text: Toàn bộ text của request.
    key: Key phải có trong object JSON cần tìm.

  Returns:
    Dict JSON tìm được, hoặc None nếu không parse được object nào.
  """
  decoder = json.JSONDecoder()
  for index, char in enumerate(text):
    if char != "{":
      continue
    try:
      obj, _end = decoder.raw_decode(text[index:])
    except (json.JSONDecodeError, ValueError):
      continue
    if isinstance(obj, dict) and key in obj:
      return obj
  return None


class FakeLlm(BaseLlm):
  """Model giả dispatch theo nội dung request (chỉ dùng cho test).

  Attributes:
    canned: Danh sách response canned theo thứ tự (mỗi phần tử là dict
      JSON hoặc chuỗi JSON); khi còn canned thì canned được ưu tiên hơn
      dispatch theo nội dung, để test đơn vị điều khiển được từng lần
      trả lời của model (sai rồi đúng, fail đủ 3 lần, ...).
  """

  model: str = "fake-llm"
  canned: list[object] = []

  def _schema_from_draft(self, text: str) -> dict | None:
    """Sinh ShopSchema từ bản nháp schema trong request.

    Args:
      text: Toàn bộ text của request.

    Returns:
      Dict ShopSchema, hoặc None nếu không tìm thấy bản nháp.
    """
    draft = _extract_json_object_with_key(text, "tables")
    if draft is None:
      return None
    tables = draft.get("tables")
    if not isinstance(tables, list):
      return None
    # business_id lấy từ cụm từ cố định trong instruction của SchemaAgent
    # (đã thay placeholder bằng id thật); fallback là mã biz_ đầu tiên.
    match = re.search(r"business_id là:\s*([A-Za-z0-9_]+)", text)
    if match:
      business_id = match.group(1)
    else:
      fallback = re.search(r"biz_\d+", text)
      business_id = fallback.group(0) if fallback else ""
    out_tables: list[dict] = []
    for table in tables:
      if not isinstance(table, dict):
        continue
      out_columns: list[dict] = []
      for col in table.get("columns", []):
        if not isinstance(col, dict):
          continue
        out_columns.append(
            {
                "original_name": col.get("original_name", ""),
                "sqlite_name": col.get("sqlite_name", ""),
                "type": col.get("type", "TEXT"),
                "description_vi": (
                    f"Cột {col.get('sqlite_name', '')} của bảng "
                    f"{table.get('sqlite_table', '')}."
                ),
                "role_hint": "unknown",
                "sample_values": col.get("sample_values", []),
                "distinct_count": col.get("distinct_count", 0),
                "distinct_values": col.get("distinct_values"),
                "nullable_rate": col.get("nullable_rate", 0.0),
            }
        )
      out_tables.append(
          {
              "table_id": table.get("table_id", ""),
              "original_names": table.get("original_names", []),
              "source_sheets": table.get("source_sheets", []),
              "sqlite_table": table.get("sqlite_table", ""),
              "row_count": table.get("row_count", 0),
              "description_vi": (
                  f"Bảng {table.get('sqlite_table', '')} của shop."
              ),
              "columns": out_columns,
              "date_range": table.get("date_range", {}),
          }
      )
    return {
        "business_id": business_id,
        "tables": out_tables,
        "join_hints": [],
        "notes": [],
    }

  def _header_from_ambiguous(self, text: str) -> dict | None:
    """Sinh HeaderProposal từ danh sách bảng mơ hồ trong request.

    Args:
      text: Toàn bộ text của request.

    Returns:
      Dict HeaderProposal, hoặc None nếu không tìm thấy danh sách.
    """
    payload = _extract_json_object_with_key(text, "ambiguous_tables")
    if payload is None:
      return None
    tables = payload.get("ambiguous_tables")
    if not isinstance(tables, list):
      return None
    return {
        "tables": [
            {
                "table_id": table.get("table_id", ""),
                # header_row lấy từ metadata quét (bảng CSV mơ hồ có
                # header_row >= 0; thiếu thì mặc định 0). TableHeader
                # bắt buộc có field này nên fake phải đưa vào.
                "header_row": table.get("header_row", 0),
                "columns": table.get("header", []),
            }
            for table in tables
            if isinstance(table, dict)
        ],
    }

  def _merge_from_summaries(self, text: str) -> dict | None:
    """Sinh MergeProposal dạng giữ riêng từ tóm tắt bảng trong request.

    Args:
      text: Toàn bộ text của request.

    Returns:
      Dict MergeProposal, hoặc None nếu không tìm thấy tóm tắt.
    """
    payload = _extract_json_object_with_key(text, "tables")
    if payload is None:
      return None
    tables = payload.get("tables")
    if not isinstance(tables, list):
      return None
    if not tables or not all(
        isinstance(table, dict) and "column_signature" in table
        for table in tables
    ):
      return None
    return {
        "groups": [
            {
                "tables": [table.get("table_id", "")],
                "evidence": "giữ riêng (fake)",
            }
            for table in tables
        ],
        "singles": [],
    }

  def _dispatch(self, text: str) -> dict:
    """Chọn response theo nội dung request (thứ tự ưu tiên cố định).

    Thứ tự quan trọng: bản nháp schema chứa key "tables" giống tóm tắt
    bảng, nên phải kiểm tra schema trước (nhận diện bằng distinct_count
    trong cột); tiếp theo là bảng mơ hồ, cuối cùng là tóm tắt bảng.

    Args:
      text: Toàn bộ text của request.

    Returns:
      Dict JSON trả về cho agent.

    Raises:
      ValueError: Khi request không khớp mẫu nào (test dùng FakeLlm phải
        gửi đúng một trong ba dạng input trên, hoặc dùng canned).
    """
    if "distinct_count" in text:
      schema = self._schema_from_draft(text)
      if schema is not None:
        return schema
    header = self._header_from_ambiguous(text)
    if header is not None:
      return header
    merge = self._merge_from_summaries(text)
    if merge is not None:
      return merge
    raise ValueError(
        "FakeLlm không dispatch được request (không thấy bản nháp schema, "
        "danh sách bảng mơ hồ hay tóm tắt bảng); test cần dùng canned."
    )

  async def generate_content_async(
      self, llm_request: LlmRequest, stream: bool = False
  ) -> AsyncGenerator[LlmResponse, None]:
    """Trả một response canned/dispatch dưới dạng LlmResponse.

    Args:
      llm_request: Request ADK gửi cho model.
      stream: FakeLlm không stream thật, luôn trả một response duy nhất.

    Yields:
      Một LlmResponse duy nhất chứa JSON text (agent sẽ parse theo
      output_schema của nó).
    """
    _ = stream
    if self.canned:
      item = self.canned.pop(0)
      text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
    else:
      text = json.dumps(self._dispatch(_request_text(llm_request)), ensure_ascii=False)
    yield LlmResponse(
        content=types.Content(
            role="model", parts=[types.Part(text=text)]
        )
    )
