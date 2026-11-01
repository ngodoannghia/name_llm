# -*- coding: utf-8 -*-
"""Hàm dùng chung để dựng 3 agent ingest (mỗi agent một file riêng).

Module phụ này chỉ chứa logic dựng agent chung (model, skill, callback,
cấu hình sinh) để 3 file agent không lặp code. Mỗi agent vẫn có file
riêng, instruction riêng và output_schema riêng như CODE_DESIGN §3.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.tools.skill_toolset import SkillToolset
from google.genai import types

from .._callbacks import (
    after_tool_callback,
    before_tool_callback,
    make_after_model_json_guard,
    on_tool_error_callback,
)
from .._config import IngestConfig
from .._skills_loader import load_single_skill


def _read_skill_text(base_dir: Path, skill_name: str) -> str:
    """Đọc toàn bộ nội dung một skill (SKILL.md + mọi file references/).

    Dùng khi agent chạy ở chế độ inline_skill: nội dung skill được đưa
    thẳng vào instruction thay vì để model tự load bằng tool.

    Args:
      base_dir: Thư mục cha chứa các thư mục skill.
      skill_name: Tên skill (trùng tên thư mục).

    Returns:
      Chuỗi gồm SKILL.md rồi từng file references/, mỗi phần có tiêu đề
      rõ ràng để model phân biệt với instruction của agent.

    Raises:
      ValueError: Khi thư mục skill hoặc SKILL.md không tồn tại — agent
        không được chạy thiếu quy tắc của bước mình phụ trách.
    """
    skill_dir = base_dir / skill_name
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise ValueError(f"Không tìm thấy SKILL.md của skill {skill_name}.")
    parts = [f"NỘI DUNG SKILL {skill_name} (SKILL.md):\n{skill_md.read_text(encoding='utf-8')}"]
    refs_dir = skill_dir / "references"
    if refs_dir.is_dir():
        for ref_file in sorted(refs_dir.iterdir()):
            if ref_file.is_file():
                parts.append(
                    f"NỘI DUNG SKILL {skill_name} (references/{ref_file.name}):\n"
                    f"{ref_file.read_text(encoding='utf-8')}"
                )
    return "\n\n".join(parts)


def build_single_turn_agent(
    *,
    name: str,
    description: str,
    instruction: str,
    output_schema: Any,
    output_key: str,
    skill_name: str,
    config: IngestConfig,
    model: BaseLlm | str | None = None,
    skills_dir: str | Path | None = None,
    inline_skill: bool = False,
) -> LlmAgent:
  """Dựng một agent single_turn cho một bước ingest.

  Mặc định (inline_skill=False) agent có đúng một SkillToolset chứa đúng
  một skill (cách ép "đúng skill ở đúng bước" theo plan §2.4 mục 4).
  Khi inline_skill=True, nội dung skill được đưa thẳng vào instruction
  và agent không có tool nào — chế độ này là bắt buộc cho cả 3 agent
  ingest, vì test luồng thật ngày 2026-10-31 chứng minh model thật
  (gpt-4o-mini qua LiteLlm) rơi vào vòng lặp tool: SchemaAgent load
  cùng một tài nguyên 64 lần liên tiếp mà không bao giờ trả JSON, tới
  khi request vượt 128k token và lỗi ContextWindowExceeded. Inline skill
  loại bỏ hoàn toàn vòng lặp tool (model chỉ còn một việc: trả JSON
  theo output_schema), đổi lại mỗi request dài thêm khoảng 4–5k token
  — chấp nhận được so với cửa sổ 128k. Progressive disclosure của
  SkillToolset vì vậy được hy sinh có chủ ý ở tầng agent ingest.

  Output_schema bắt buộc để output luôn là JSON đúng schema (validator
  sẽ kiểm lại sau đó), và temperature 0 qua generate_content_config để
  kết quả tái lập được.

  Lưu ý đã kiểm chứng từ source ADK và từ vụ biz_127 ngày 2026-11-01:
  output_schema không tự sửa JSON sai. JSON sai schema (bị cắt cụt,
  thiếu field, sai kiểu) sẽ raise ValidationError ở bước
  ``__maybe_save_output_to_state`` — bước này nằm ngoài khối try/except
  của model generation nên ``on_model_error`` KHÔNG bắt được nó, và
  exception sẽ giết cả workflow. Vì vậy mọi agent ingest đều gắn thêm
  ``after_model`` guard (xem ``_callbacks.make_after_model_json_guard``):
  response lỗi được thay bằng JSON sentinel hợp schema nhưng validator
  sẽ bác, nên lỗi JSON trở thành một Verdict fail bình thường đi vào
  retry/fallback thay vì crash shop.

  Args:
    name: Tên agent (duy nhất trong workflow).
    description: Mô tả ngắn cho agent.
    instruction: Instruction tiếng Việt (placeholder chỉ đọc state, dạng
      {var?} optional, không bao giờ dùng {node_input}).
    output_schema: Model Pydantic của output (HeaderProposal /
      MergeProposal / ShopSchema).
    output_key: Key state để agent ghi output (phục vụ debug; validator
      đọc output trực tiếp của node, không đọc key này).
    skill_name: Tên skill duy nhất agent được nạp.
    config: Cấu hình ingest (model_id mặc định khi không truyền model).
    model: Instance BaseLlm (fake trong test, LiteLlm thật khi chạy
      thật) hoặc chuỗi tên model; None thì dùng config.model_id.
    skills_dir: Thư mục cha chứa skill; None thì dùng thư mục skills/
      cạnh package ingest.

  Returns:
    LlmAgent single_turn đã cấu hình xong.

  Raises:
    ValueError: Khi không có model nào (cả model truyền vào lẫn
      config.model_id đều rỗng) — fail fast lúc dựng workflow, không để
      workflow chạy rồi mới lỗi vì thiếu model.
  """
  resolved_model: BaseLlm | str | None = model
  if resolved_model is None:
    resolved_model = config.model_id or None
  if resolved_model is None:
    raise ValueError(
        f"Agent {name} không có model: truyền model vào hoặc đặt "
        "model_id trong IngestConfig."
    )
  base_dir = Path(skills_dir) if skills_dir is not None else (
      Path(__file__).resolve().parent.parent.parent / "skills"
  )
  tools: list[Any] = []
  final_instruction = instruction
  if inline_skill:
    # Skill đưa sẵn vào instruction, agent không có tool nào nên không
    # thể rơi vào vòng lặp load_skill_resource (xem docstring ở trên).
    final_instruction = (
        _read_skill_text(base_dir, skill_name) + "\n\n" + instruction
    )
  else:
    skill = load_single_skill(base_dir, skill_name)
    if skill is not None:
      tools.append(SkillToolset(skills=[skill]))
  return LlmAgent(
      name=name,
      description=description,
      model=resolved_model,
      instruction=final_instruction,
      tools=tools,
      output_schema=output_schema,
      output_key=output_key,
      # ADK 2.13 dùng field mode="single_turn" (không có tham số
      # single_turn riêng); workflow cũng tự đặt include_contents='none'
      # cho node single_turn, nhưng ở đây đặt tường minh cho rõ.
      mode="single_turn",
      include_contents="none",
      generate_content_config=types.GenerateContentConfig(temperature=0),
      before_tool_callback=before_tool_callback,
      after_tool_callback=after_tool_callback,
      on_tool_error_callback=on_tool_error_callback,
      after_model_callback=make_after_model_json_guard(output_schema),
  )
