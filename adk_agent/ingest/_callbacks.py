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
"""

from __future__ import annotations

from google.adk.tools import BaseTool

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
