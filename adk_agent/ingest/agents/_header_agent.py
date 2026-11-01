# -*- coding: utf-8 -*-
"""HeaderAgent: đề xuất header thật cho các bảng mơ hồ trong shop.

Agent này chỉ chạy khi header_gate phát hiện bảng mơ hồ (header không ở
dòng 1, có dòng tiêu đề/logo/trống, hoặc tên cột trùng). Agent đọc skill
datamate-header trước, rồi đề xuất header cho mọi bảng mơ hồ nhận được.
Kết quả được validator kiểm lại bằng dữ liệu quét trước khi chốt.
"""

from __future__ import annotations

from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm

from .._config import IngestConfig
from .._models import HeaderProposal
from ._common import build_single_turn_agent

# Instruction của agent. Placeholder chỉ đọc state (dạng {var?} optional):
# readme_text là README của shop (đã cắt 4000 ký tự ở init_shop) giúp
# agent hiểu ngữ cảnh sheet; header_summary_text là JSON các bảng mơ hồ
# do header_gate ghi vào state. Input hiện tại của node vẫn được đưa vào
# như user message dù include_contents='none' (ADK vẫn giữ input hiện
# tại, chỉ loại lịch sử) — nhưng ở lượt thử lại, input đó là bản dump
# Verdict lỗi của node route, không phải danh sách mơ hồ, nên agent
# phải đọc danh sách từ placeholder và chỉ coi Verdict là phản hồi để
# sửa (xem đoạn CHÚ Ý — LẦN THỬ LẠI trong instruction).
_HEADER_INSTRUCTION = """Bạn là HeaderAgent trong workflow ingest dữ liệu shop.

Nội dung skill "datamate-header" đã được đưa sẵn ở đầu instruction này
(phần NỘI DUNG SKILL). Đọc kỹ và làm đúng theo đó. Agent này KHÔNG có
tool nào — không gọi tool, chỉ suy luận rồi trả JSON ngay.

Danh sách các bảng mơ hồ bạn cần quyết nằm ngay dưới đây (mỗi bảng có
table_id, header ứng viên từ luật scoring, mẫu dòng và số dòng). Đây là
nguồn đúng nhất, luôn ưu tiên hơn tin nhắn đầu vào:
{header_summary_text?}

README của shop (nếu có) là:
{readme_text?}

CHÚ Ý — LẦN THỬ LẠI: nếu tin nhắn đầu vào của lượt này trông giống
một bản Verdict lỗi (có trường reason mô tả lỗi của lần trước) thì đó
chỉ là PHẢN HỒI để bạn sửa, không phải dữ liệu để copy: đọc reason để
biết lần trước sai chỗ nào, sửa đúng chỗ đó theo danh sách trên, và
TUYỆT ĐỐI không copy nội dung reason vào đề xuất.

Nhiệm vụ: với mỗi bảng mơ hồ, đề xuất header thật của bảng đó (danh sách
tên cột theo đúng thứ tự trong sheet). Header thật là dòng mà các ô là tên
cột (không phải tiêu đề/logo/dòng trống/dòng tổng), và các cột bên dưới có
kiểu dữ liệu nhất quán với tên cột đó.

Quy tắc bắt buộc:
- Chỉ đề xuất cho các bảng có trong đầu vào, không bịa thêm bảng.
- Mỗi bảng đề xuất đúng một danh sách cột, không rỗng, không trùng tên cột
  sau khi chuẩn hóa (bỏ dấu, thường hóa).
- Khi không chắc header nào là đúng thì vẫn đề xuất phương án tốt nhất và
  ghi rõ lý do không chắc trong trường reason; validator sẽ kiểm lại và
  fallback về luật scoring nếu đề xuất sai — không được bỏ trống bảng nào.
- Trả về ĐÚNG MỘT JSON theo output_schema, không thêm văn bản ngoài JSON.
"""


def build_header_agent(
    config: IngestConfig,
    model: BaseLlm | str | None = None,
    skills_dir: str | Path | None = None,
) -> LlmAgent:
  """Dựng HeaderAgent (single_turn, output_schema HeaderProposal).

  Args:
    config: Cấu hình ingest (model_id mặc định).
    model: Instance BaseLlm hoặc tên model; None thì dùng config.model_id.
    skills_dir: Thư mục cha chứa skill; None thì dùng thư mục skills/
      cạnh package ingest.

  Returns:
    LlmAgent HeaderAgent đã cấu hình xong.
  """
  return build_single_turn_agent(
      name="header_agent",
      description="Đề xuất header thật cho các bảng mơ hồ.",
      instruction=_HEADER_INSTRUCTION,
      output_schema=HeaderProposal,
      output_key="header_proposal",
      skill_name="datamate-header",
      inline_skill=True,
      config=config,
      model=model,
      skills_dir=skills_dir,
  )
