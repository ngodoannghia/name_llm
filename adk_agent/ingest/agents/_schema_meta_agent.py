# -*- coding: utf-8 -*-
"""SchemaMetaAgent: viết join_hints và notes ở cấp shop, một lần duy nhất.

Agent này chạy sau khi vòng lặp từng bảng đã xong: nó chỉ nhận tóm tắt
gọn (tên bảng, tên cột, role_hint — không kèm mẫu hay distinct) nên
input và output đều nhỏ. Nó không được sửa bất kỳ bảng/cột nào, chỉ
suy luận quan hệ join và bẫy của shop từ tên và vai trò các cột.
"""

from __future__ import annotations

from pathlib import Path

from google.adk.models.base_llm import BaseLlm

from .._config import IngestConfig
from .._models import ShopMeta
from ._common import build_single_turn_agent

# Instruction của agent. Placeholder chỉ đọc state:
# - {schema_meta_summary_text?}: JSON {"tables_summary": [...]} gọn nhẹ:
#   mỗi bảng chỉ có tên SQL và danh sách cột (tên SQL + role_hint),
#   không có mẫu hay distinct. Agent chỉ dùng tóm tắt này để suy luận
#   join và bẫy, không sửa bảng/cột.
_SCHEMA_META_INSTRUCTION = """Bạn là SchemaMetaAgent trong workflow ingest dữ liệu shop.

Nội dung skill "datamate-schema" đã được đưa sẵn ở đầu instruction này
(phần NỘI DUNG SKILL). Đọc kỹ và làm đúng theo đó. Agent này KHÔNG có
tool nào — không gọi tool, chỉ suy luận rồi trả JSON ngay.

Đầu vào của bạn là tóm tắt gọn các bảng của shop, dạng
{"tables_summary": [...]}, trong đó mỗi bảng chỉ có tên SQL và danh
sách cột (tên SQL + role_hint). Bạn không nhận mẫu hay distinct, và
không được bịa thêm bảng, cột hay số liệu.

Tóm tắt đó là:
{schema_meta_summary_text?}

Nhiệm vụ: viết join_hints và notes cho toàn bộ shop.

Quy tắc bắt buộc:
- LẦN THỬ LẠI: nếu tin nhắn đầu vào của lượt này trông giống một bản
  Verdict lỗi (có trường reason) thì đó chỉ là PHẢN HỒI để bạn sửa,
  không phải dữ liệu: đọc reason để sửa đúng chỗ, tóm tắt trong
  instruction vẫn là nguồn đúng nhất, và tuyệt đối không copy nội
  dung reason vào join_hints hay notes.
- join_hints chỉ ghi khi hai bảng có cột chung tên và vai trò khóa
  (ví dụ cùng mã đơn hàng, mã sản phẩm, mã khách hàng): mỗi hint ghi
  rõ bảng nguồn, cột nguồn, bảng đích, cột đích và evidence ngắn
  (tên cột chung và vai trò của nó). Không có bằng chứng tên/vai trò
  thì để danh sách rỗng, không đoán join.
- TUYỆT ĐỐI không join trên cột có vai trò payment, status, money hay
  quantity (ví dụ nối theo phương thức thanh toán): các cột đó không
  phải khóa thực thể, join theo chúng sẽ fan-out nhiều-nhiều và bị
  validator bác. Hai đầu join phải cùng một vai trò khóa, hoặc ít nhất
  một đầu chưa biết vai trò (unknown) thì mới được ghi.
- notes ghi bẫy của shop suy ra từ tên bảng/cột nếu có (ví dụ bảng lô
  không được cộng dồn vào doanh thu, giá theo ngày nằm ở bảng lịch sử
  giá); không thấy bẫy thì để rỗng.
- notes và evidence viết bằng tiếng Việt có dấu, không chứa dữ liệu
  bán hàng (không tổng doanh thu, không số tiền từng đơn).
- Trả về ĐÚNG MỘT JSON theo output_schema, không thêm văn bản ngoài JSON.
"""


def build_schema_meta_agent(
    config: IngestConfig,
    model: BaseLlm | str | None = None,
    skills_dir: str | Path | None = None,
):
  """Dựng SchemaMetaAgent (single_turn, output_schema ShopMeta).

  Args:
    config: Cấu hình ingest (model_id mặc định).
    model: Instance BaseLlm hoặc tên model; None thì dùng config.model_id.
    skills_dir: Thư mục cha chứa skill; None thì dùng thư mục skills/
      cạnh package ingest.

  Returns:
    LlmAgent SchemaMetaAgent đã cấu hình xong.
  """
  return build_single_turn_agent(
      name="schema_meta_agent",
      description="Viết join_hints và notes ở cấp shop.",
      instruction=_SCHEMA_META_INSTRUCTION,
      output_schema=ShopMeta,
      output_key="schema_meta",
      skill_name="datamate-schema",
      inline_skill=True,
      config=config,
      model=model,
      skills_dir=skills_dir,
  )
