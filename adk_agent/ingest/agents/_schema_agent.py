# -*- coding: utf-8 -*-
"""SchemaAgent: hoàn thiện file schema mô tả DB của shop.

Agent này chạy một lần cho mỗi shop, sau khi DB tạm đã ghi xong. Agent
đọc skill datamate-schema trước, rồi dựa trên bản nháp schema đo từ DB
thật (mẫu, distinct, số dòng) để thêm mô tả tiếng Việt, role_hint,
join_hint có bằng chứng và ghi chú bẫy của shop. Kết quả được validator
kiểm lại bằng truy vấn thật trên DB tạm trước khi ghi file schema.
"""

from __future__ import annotations

from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm

from .._config import IngestConfig
from .._models import ShopSchema
from ._common import build_single_turn_agent

# Instruction của agent. Placeholder chỉ đọc state:
# - {business_id?}: id shop đang chạy (schema phải ghi đúng id này).
# - {schema_draft_text?}: bản nháp schema đo từ DB tạm, dạng JSON chuỗi.
# Bản nháp đã chứa mẫu/distinct/số dòng thật; agent chỉ bổ sung mô tả,
# role_hint, join_hint và notes, không được sửa mẫu/distinct/số dòng.
_SCHEMA_INSTRUCTION = """Bạn là SchemaAgent trong workflow ingest dữ liệu shop.

Nội dung skill "datamate-schema" đã được đưa sẵn ở đầu instruction này
(phần NỘI DUNG SKILL). Đọc kỹ và làm đúng theo đó. Agent này KHÔNG có
tool nào — không gọi tool, chỉ suy luận rồi trả JSON ngay.

Shop đang chạy có business_id là: {business_id?}
Bản nháp schema đo từ DB tạm của shop (mẫu, distinct, số dòng đều là giá
trị thật trong DB) là:
{schema_draft_text?}

Nhiệm vụ: hoàn thiện bản nháp thành file schema đầy đủ cho shop, đúng một
file cho một shop.

Quy tắc bắt buộc:
- Giữ nguyên business_id, tập bảng, tên bảng SQL, tên cột SQL, kiểu cột,
  sample_values, distinct_count, distinct_values và nullable_rate từ bản
  nháp — không sửa các giá trị đã đo, không bịa thêm bảng/cột/mẫu.
- Bổ sung cho mỗi bảng và mỗi cột: description_vi bằng tiếng Việt có dấu
  (bảng này là gì, cột này là gì, đơn vị nếu có).
- role_hint cho mỗi cột chỉ nằm trong enum: order_id, product_id,
  customer_id, date, money, quantity, status, payment, unknown. Khi không
  chắc thì dùng unknown, không đoán role.
- join_hints chỉ ghi khi có bằng chứng đo được (tên cột chung giữa hai
  bảng và tỷ lệ ID khớp); không có bằng chứng thì để danh sách rỗng.
- notes ghi bẫy của shop nếu có (ví dụ cột ngày dạng text hỗn hợp, bảng
  lô không được cộng dồn vào doanh thu); không có bẫy thì để rỗng.
- Schema này KHÔNG chứa dữ liệu bán hàng (không tổng doanh thu, không số
  tiền từng đơn) — chỉ chứa mô tả, mẫu và thống kê distinct.
- Trả về ĐÚNG MỘT JSON theo output_schema, không thêm văn bản ngoài JSON.
"""


def build_schema_agent(
    config: IngestConfig,
    model: BaseLlm | str | None = None,
    skills_dir: str | Path | None = None,
) -> LlmAgent:
  """Dựng SchemaAgent (single_turn, output_schema ShopSchema).

  Args:
    config: Cấu hình ingest (model_id mặc định).
    model: Instance BaseLlm hoặc tên model; None thì dùng config.model_id.
    skills_dir: Thư mục cha chứa skill; None thì dùng thư mục skills/
      cạnh package ingest.

  Returns:
    LlmAgent SchemaAgent đã cấu hình xong.
  """
  return build_single_turn_agent(
      name="schema_agent",
      description="Hoàn thiện file schema mô tả DB của shop.",
      instruction=_SCHEMA_INSTRUCTION,
      output_schema=ShopSchema,
      output_key="schema_proposal",
      skill_name="datamate-schema",
      inline_skill=True,
      config=config,
      model=model,
      skills_dir=skills_dir,
  )
