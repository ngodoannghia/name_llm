# -*- coding: utf-8 -*-
"""SchemaTableAgent: hoàn thiện mô tả cho ĐÚNG MỘT bảng của shop.

Agent này chạy một lần cho mỗi bảng trong vòng lặp schema theo từng
bảng (thay cho SchemaAgent viết toàn bộ schema một lần như trước).
Vì mỗi lần gọi chỉ nhận một bảng nên output luôn nhỏ, không còn nguy
cơ JSON 17 bảng bị cắt dở như biz_123 gặp phải ở test luồng thật.
Agent chỉ bổ sung mô tả và role_hint; mọi giá trị đã đo (tên, kiểu,
mẫu, distinct, số dòng) phải giữ nguyên để validator kiểm lại.
"""

from __future__ import annotations

from pathlib import Path

from google.adk.models.base_llm import BaseLlm

from .._config import IngestConfig
from .._models import TableSchema
from ._common import build_single_turn_agent

# Instruction của agent. Placeholder chỉ đọc state:
# - {schema_table_draft_text?}: JSON {"table": {...}} của đúng một bảng,
#   đã đo từ DB tạm (tên, kiểu, mẫu, distinct, số dòng đều là giá trị
#   thật). Agent chỉ bổ sung mô tả và role, không sửa giá trị đã đo.
_SCHEMA_TABLE_INSTRUCTION = """Bạn là SchemaTableAgent trong workflow ingest dữ liệu shop.

Nội dung skill "datamate-schema" đã được đưa sẵn ở đầu instruction này
(phần NỘI DUNG SKILL). Đọc kỹ và làm đúng theo đó. Agent này KHÔNG có
tool nào — không gọi tool, chỉ suy luận rồi trả JSON ngay.

Đầu vào của bạn là JSON của ĐÚNG MỘT bảng, dạng {"table": {...}}.
Bảng này đã được đo từ DB tạm của shop (tên bảng, tên cột, kiểu cột,
mẫu, distinct, số dòng đều là giá trị thật trong DB).

Bản nháp của bảng đó là:
{schema_table_draft_text?}

Nhiệm vụ: hoàn thiện đúng bảng đó, không thêm bảng, không bớt bảng,
không thêm cột, không bớt cột.

Quy tắc bắt buộc:
- Giữ nguyên mọi giá trị đã đo: table_id, original_names,
  source_sheets, sqlite_table, row_count, tên cột SQL, kiểu cột,
  sample_values, distinct_count, distinct_values, nullable_rate —
  không sửa các giá trị này, không bịa thêm mẫu hay cột.
- sample_values: sao chép nguyên si từng chuỗi trong bản nháp (kể cả
  dạng mảng JSON), KHÔNG tách chuỗi theo dấu phẩy, không thêm/xoá
  phần tử. (Bài học từ biz_127: tách mảng mẫu theo dấu phẩy làm
  validator bác vì mẫu bị xé nát, tốn lượt thử lại.)
- LẦN THỬ LẠI: nếu tin nhắn đầu vào của lượt này trông giống một bản
  Verdict lỗi (có trường reason) thì đó chỉ là PHẢN HỒI để bạn sửa,
  không phải dữ liệu: đọc reason để sửa đúng chỗ, bản nháp trong
  instruction vẫn là nguồn đúng nhất, và tuyệt đối không copy nội
  dung reason vào schema.
- Riêng date_range: bản nháp để {} nghĩa là chưa đo được — giữ nguyên
  {}, KHÔNG tự điền khoảng ngày từ dữ liệu mẫu (khoảng ngày tự điền
  có thể sai và làm lệch bước trả lời về sau, ví dụ bảng đơn hàng
  đoán 5 ngày trong khi dữ liệu thật kéo dài hơn một năm).
- Chỉ bổ sung: description_vi cho bảng bằng tiếng Việt có dấu (bảng
  này mỗi dòng là gì), và cho mỗi cột description_vi (cột này là gì,
  đơn vị nếu có) cùng role_hint.
- role_hint cho mỗi cột chỉ nằm trong enum: order_id, product_id,
  customer_id, date, money, quantity, status, payment, unknown. Khi
  không chắc thì dùng unknown, không đoán role.
- Schema này KHÔNG chứa dữ liệu bán hàng (không tổng doanh thu, không
  số tiền từng đơn) — chỉ chứa mô tả, mẫu và thống kê distinct.
- Trả về ĐÚNG MỘT JSON theo output_schema, không thêm văn bản ngoài JSON.
"""


def build_schema_table_agent(
    config: IngestConfig,
    model: BaseLlm | str | None = None,
    skills_dir: str | Path | None = None,
):
  """Dựng SchemaTableAgent (single_turn, output_schema TableSchema).

  Args:
    config: Cấu hình ingest (model_id mặc định).
    model: Instance BaseLlm hoặc tên model; None thì dùng config.model_id.
    skills_dir: Thư mục cha chứa skill; None thì dùng thư mục skills/
      cạnh package ingest.

  Returns:
    LlmAgent SchemaTableAgent đã cấu hình xong.
  """
  return build_single_turn_agent(
      name="schema_table_agent",
      description="Hoàn thiện mô tả cho một bảng của shop.",
      instruction=_SCHEMA_TABLE_INSTRUCTION,
      output_schema=TableSchema,
      output_key="schema_table_proposal",
      skill_name="datamate-schema",
      inline_skill=True,
      config=config,
      model=model,
      skills_dir=skills_dir,
  )
