# -*- coding: utf-8 -*-
"""Các callback guardrail cho agent: chặn tool sai, cắt ngắn, lỗi có cấu trúc.

Module này thuộc tầng bảo vệ. Nguyên tắc đã kiểm chứng từ source ADK
(xem plan §2.4 mục 5):
- ``before_tool`` trả non-None (kể cả dict rỗng) thì tool KHÔNG chạy:
  đây là blocker duy nhất, và là nơi duy nhất chặn được side-effect
  trước khi nó xảy ra.
- ``after_tool`` chỉ thay kết quả trả về, không undo được side-effect
  đã xảy ra nên không dùng nó để chặn.
- ``on_tool_error`` trả payload có cấu trúc để agent nhận lỗi rõ ràng
  thay vì workflow crash vì exception tool.
- ``after_model`` (guard JSON, thêm sau vụ biz_127 ngày 2026-11-01):
  JSON trả về mà không hợp ``output_schema`` (bị cắt cụt, thiếu field
  bắt buộc, sai kiểu) sẽ raise ValidationError ở bước
  ``__maybe_save_output_to_state`` của LlmAgent — bước này nằm NGOÀI
  khối try/except của model generation nên ``on_model_error`` KHÔNG
  bắt được nó, và exception sẽ giết cả workflow (shop FAILED, mọi kết
  quả bảng trước đó còn nằm trong state đều mất). Guard này chặn ở
  ``after_model`` — tức TRƯỚC bước save đó — bằng cách thay response
  lỗi thành một JSON sentinel hợp schema nhưng validator sẽ bác (xem
  ``_INVALID_JSON_SENTINELS``): lỗi JSON vì vậy trở thành một Verdict
  fail bình thường, đi vào retry/fallback như mọi lỗi validator khác,
  thay vì crash shop.
"""

from __future__ import annotations

import json
from typing import Any

from google.adk.models.llm_response import LlmResponse
from google.adk.tools import BaseTool
from google.adk.utils._schema_utils import validate_schema
from google.genai import types

from ._models import HeaderProposal, MergeProposal, ShopMeta, TableSchema

# Tên tool được phép chạy trong agent ingest. SkillToolset phơi 4 tool
# (đã kiểm chứng bằng spike G0): list_skills, load_skill,
# load_skill_resource và run_skill_script. Ba tool đầu là đọc skill
# (metadata/instruction/resource) nên được phép; run_skill_script bị
# chặn vì skill của dự án không có script thực thi nào, và chạy script
# là side-effect duy nhất mà SkillToolset có thể gây ra. Mọi tool khác
# (ghi file, chạy SQL, gọi shell) cũng bị chặn ở before_tool.
ALLOWED_TOOL_NAMES = {"list_skills", "load_skill", "load_skill_resource"}
# Độ dài tối đa cho một giá trị chuỗi trong kết quả tool sau khi cắt ngắn
# (tránh kết quả tool khổng lồ làm tràn context của lần gọi model sau).
RESULT_STRING_MAX_CHARS = 8000

# JSON sentinel dùng khi response của model không hợp output_schema.
# Mỗi sentinel đều HỢP schema (để bước save của ADK không raise) nhưng
# CHẮC CHẮN bị validator của bước đó bác, với lý do rõ ràng:
# - TableSchema: table_id lạ so với bản nháp -> validator so trường đã đo
#   sẽ fail (bảng sai, cột rỗng so với bản nháp).
# - MergeProposal: single lạ không có trong dữ liệu quét -> validator fail.
# - HeaderProposal: danh sách rỗng trong khi còn bảng mơ hồ -> fail.
# - ShopMeta: join trỏ tới bảng/cột không tồn tại -> validator tham chiếu
#   fail (sau đủ lượt thì rơi về fallback meta rỗng, shop vẫn tiếp tục).
# Sentinel KHÔNG được mang dữ liệu thật nào (để không bao giờ vô tình
# được publish nếu validator có lỗi và cho qua): mọi giá trị đều là chuỗi
# đánh dấu "__invalid_json__".
_INVALID_JSON_SENTINELS: dict[type, dict[str, Any]] = {
    TableSchema: {
        "table_id": "__invalid_json__",
        "sqlite_table": "__invalid_json__",
    },
    MergeProposal: {
        "groups": [],
        "singles": ["__invalid_json__"],
    },
    HeaderProposal: {
        "tables": [],
    },
    ShopMeta: {
        "join_hints": [
            {
                "from_table": "__invalid_json__",
                "from_column": "__invalid_json__",
                "to_table": "__invalid_json__",
                "to_column": "__invalid_json__",
            }
        ],
        "notes": [],
    },
}


def make_after_model_json_guard(output_schema: Any) -> Any:
  """Tạo callback after_model chặn JSON không hợp schema trước khi save.

  Callback này chạy trong llm flow, TRƯỚC bước
  ``__maybe_save_output_to_state`` của LlmAgent (nơi ADK gọi
  ``validate_schema`` và raise ValidationError nếu JSON lỗi). Nó kiểm
  tra response cuối của model bằng đúng hàm ``validate_schema`` mà ADK
  dùng: response hợp lệ thì giữ nguyên (trả None), response lỗi thì
  thay toàn bộ nội dung text bằng JSON sentinel của schema đó (xem
  ``_INVALID_JSON_SENTINELS``) để bước save không raise và validator
  của bước sẽ bác sentinel này như một lỗi thường, đi vào retry hoặc
  fallback thay vì giết cả workflow.

  Args:
    output_schema: Model Pydantic của output_schema mà agent dùng
      (TableSchema, MergeProposal, HeaderProposal hoặc ShopMeta).
      Schema nào không có trong bảng sentinel thì guard không làm gì
      (trả None mọi response) để không tự ý bịa sentinel cho schema lạ.

  Returns:
    Hàm callback after_model (nhận callback_context và llm_response,
    trả LlmResponse đã sửa khi JSON lỗi, None khi giữ nguyên). Callback
    bỏ qua response partial (streaming chưa xong — JSON dở dang lúc
    này là bình thường, chưa phải lỗi) và response không có text.
  """

  def after_model_json_guard(
      callback_context: object, llm_response: LlmResponse
  ) -> LlmResponse | None:
    """Kiểm tra một response cuối của model, thay bằng sentinel nếu lỗi.

    Args:
      callback_context: Context của callback (không dùng ngoài việc giữ
        đúng chữ ký callback của ADK).
      llm_response: Response vừa nhận từ model.

    Returns:
      None khi giữ nguyên response (hợp lệ, partial, hoặc không có
      text, hoặc schema không có sentinel); LlmResponse mới với nội
      dung là JSON sentinel khi text cuối không hợp output_schema.
    """
    _ = callback_context
    sentinel = _INVALID_JSON_SENTINELS.get(output_schema)
    if sentinel is None:
      return None
    if getattr(llm_response, "partial", False):
      return None
    content = getattr(llm_response, "content", None)
    parts = getattr(content, "parts", None) if content is not None else None
    if not parts:
      return None
    text = "".join(
        part.text
        for part in parts
        if getattr(part, "text", None) and not getattr(part, "thought", False)
    )
    if not text.strip():
      return None
    try:
      validate_schema(output_schema, text)
    except Exception:
      return llm_response.model_copy(
          update={
              "content": types.Content(
                  role="model",
                  parts=[
                      types.Part(
                          text=json.dumps(sentinel, ensure_ascii=False)
                      )
                  ],
              )
          }
      )
    return None

  return after_model_json_guard


def before_tool_callback(
    tool: BaseTool, args: dict, tool_context: object
) -> dict | None:
  """Chặn tool không được phép trước khi nó chạy.

  Args:
    tool: Tool mà agent định gọi.
    args: Đối số tool (dict).
    tool_context: Context của lần gọi tool (không dùng ở đây ngoài việc
      giữ đúng chữ ký callback của ADK).

  Returns:
    None khi tool được phép (cho tool chạy tiếp); dict lỗi khi bị chặn
    (trả non-None nên ADK bỏ qua tool và dùng dict này làm kết quả).
  """
  _ = args
  _ = tool_context
  name = getattr(tool, "name", "") or ""
  if name in ALLOWED_TOOL_NAMES:
    return None
  return {
      "error": (
          f"Tool {name!r} bị chặn bởi guardrail ingest: agent ingest chỉ "
          "được nạp skill, không được chạy tool khác."
      ),
      "blocked_tool": name,
  }


def after_tool_callback(
    tool: BaseTool, args: dict, tool_context: object, result: dict
) -> dict | None:
  """Cắt ngắn kết quả tool quá dài để tránh tràn context.

  Callback này không chặn gì cả: nó chỉ thay kết quả trả về bằng bản đã
  cắt ngắn khi có chuỗi quá dài. Trả None nghĩa là giữ nguyên kết quả.

  Args:
    tool: Tool vừa chạy xong.
    args: Đối số tool đã dùng.
    tool_context: Context của lần gọi tool.
    result: Kết quả tool trả về (dict).

  Returns:
    Dict đã cắt ngắn khi có chuỗi vượt ngưỡng, ngược lại None.
  """
  _ = tool
  _ = args
  _ = tool_context
  if not isinstance(result, dict):
    return None
  truncated: dict = {}
  changed = False
  for key, value in result.items():
    if isinstance(value, str) and len(value) > RESULT_STRING_MAX_CHARS:
      truncated[key] = (
          value[:RESULT_STRING_MAX_CHARS]
          + f"\n... [đã cắt ngắn từ {len(value)} ký tự]"
      )
      changed = True
    else:
      truncated[key] = value
  return truncated if changed else None


def on_tool_error_callback(
    tool: BaseTool, args: dict, tool_context: object, error: Exception
) -> dict | None:
  """Chuyển lỗi tool thành payload có cấu trúc cho agent xử lý tiếp.

  Args:
    tool: Tool vừa lỗi.
    args: Đối số tool đã dùng.
    tool_context: Context của lần gọi tool.
    error: Exception mà tool raise.

  Returns:
    Dict lỗi có cấu trúc (tool, args tóm tắt, thông điệp lỗi) để agent
    nhận được lỗi rõ ràng thay vì workflow crash.
  """
  _ = tool_context
  name = getattr(tool, "name", "") or ""
  return {
      "error": f"Tool {name!r} lỗi: {error}",
      "tool": name,
      "args_keys": sorted(args.keys()),
  }
