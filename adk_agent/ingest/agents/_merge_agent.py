# -*- coding: utf-8 -*-
"""MergeAgent: đề xuất nhóm gộp các sheet là mảnh của cùng bảng logic.

Agent này chỉ chạy khi còn hơn 1 bảng chưa khóa (nhóm khóa bởi code —
cùng chữ ký cột chuẩn hóa — đã chốt ở merge_gate và không nằm trong
đầu vào của agent). Agent đọc skill datamate-merge trước, rồi dựa trên
chữ ký cột, vai trò và ID/ngày liên tục để quyết định bảng nào gộp,
bảng nào giữ riêng. Kết quả được validator kiểm lại (cùng chữ ký cột,
không gộp biến thể lô) rồi gộp tiếp với nhóm khóa trước khi chốt.
"""

from __future__ import annotations

from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm

from .._config import IngestConfig
from .._models import MergeProposal
from ._common import build_single_turn_agent

# Instruction của agent. Tóm tắt bảng được đọc qua placeholder
# {merge_summary_text?} trong instruction (do merge_gate ghi vào state),
# KHÔNG trông chờ vào tin nhắn đầu vào của node: ở lượt chạy đầu, tin
# nhắn đó là tóm tắt, nhưng ở lượt thử lại, ADK đưa output của node
# route (bản dump Verdict lỗi) làm tin nhắn đầu vào — vụ biz_127 cho
# thấy model copy nguyên văn câu lỗi trong Verdict vào đáp án nếu
# không được dặn rõ. Vì vậy instruction dặn riêng trường hợp thử lại
# ở đoạn CHÚ Ý — LẦN THỬ LẠI dưới đây.
_MERGE_INSTRUCTION = """Bạn là MergeAgent trong workflow ingest dữ liệu shop.

Nội dung skill "datamate-merge" đã được đưa sẵn ở đầu instruction này
(phần NỘI DUNG SKILL). Đọc kỹ và làm đúng theo đó. Agent này KHÔNG có
tool nào — không gọi tool, chỉ suy luận rồi trả JSON ngay.

Tóm tắt các bảng bạn cần quyết nằm ngay dưới đây (mỗi bảng có
table_id, chữ ký cột đã chuẩn hóa và số dòng). Đây là nguồn đúng nhất,
luôn ưu tiên hơn tin nhắn đầu vào:
{merge_summary_text?}

CHÚ Ý — đầu vào này CHỈ chứa các bảng chưa được code quyết. Các nhóm
mà code đã khóa (cùng chữ ký cột chuẩn hóa thì code gộp luôn, xem
skill) không nằm trong đầu vào và đã chốt: bạn KHÔNG quyết lại chúng,
KHÔNG nhắc tới chúng trong đề xuất. Bạn chỉ quyết các bảng trong tóm
tắt trên, và chỉ các bảng đó.

CHÚ Ý — LẦN THỬ LẠI: nếu tin nhắn đầu vào của lượt này trông giống
một bản Verdict lỗi (có trường reason mô tả lỗi của lần trước) thì đó
chỉ là PHẢN HỒI để bạn sửa, không phải dữ liệu để copy: đọc reason để
biết lần trước sai chỗ nào, sửa đúng chỗ đó theo tóm tắt trên, và
TUYỆT ĐỐI không copy nội dung reason (ví dụ câu "không tồn tại trong
dữ liệu quét") vào đề xuất. Đề xuất chỉ chứa table_id có trong tóm
tắt trên, không chứa câu chữ nào từ bản Verdict.

Nhiệm vụ: quyết định bảng nào gộp thành một bảng logic, bảng nào giữ riêng,
dựa trên bằng chứng trong đầu vào:
- Chỉ gộp khi các bảng có cùng chữ ký cột, cùng vai trò, và ID/ngày liên tục
  (ví dụ các sheet DuLieuBanHang chia theo thời gian, hoặc bảng _1/_2).
- Quy tắc lát cắt thời gian (bài học từ biz_102): mọi lát cắt thời gian
  của cùng một bảng logic (các sheet tháng, các file tách theo năm) phải
  thành MỘT nhóm duy nhất. KHÔNG BAO GIỜ gộp cặp theo số tháng/năm giống
  nhau (tháng 1 năm 2025 với tháng 1 năm 2026 là hai lát cắt khác nhau
  của cùng một bảng, phải nằm chung một nhóm với mọi tháng khác — tách
  chúng thành cặp riêng là sai).
- KHÔNG gộp sheet khác vai trò chỉ vì cùng file (ví dụ sheet đơn hàng với
  sheet master sản phẩm trong cùng file).
- KHÔNG gộp bảng với biến thể lô của nó (dạng ChiTiet với ChiTietLo) khi
  chưa so sánh toàn bộ schema — mặc định giữ riêng để tránh đếm trùng.
- KHÔNG gộp khi ID overlap mâu thuẫn (cùng ID nhưng nội dung khác nhau).

Quy tắc bắt buộc:
- Mọi bảng trong đầu vào phải xuất hiện đúng một lần: hoặc trong một nhóm
  gộp, hoặc trong danh sách giữ riêng. Không sót bảng, không trùng bảng.
- Mỗi nhóm gộp phải kèm evidence ngắn gọn (vì sao gộp: chữ ký giống nhau,
  ID/ngày liên tục, ...). KHÔNG bịa evidence đo được (ngày, ID) khi đầu
  vào không có số liệu đó: chỉ nêu bằng chứng thật sự nhìn thấy trong
  đầu vào (chữ ký, tên bảng, số dòng).
- Trả về ĐÚNG MỘT JSON theo output_schema, không thêm văn bản ngoài JSON.
"""


def build_merge_agent(
    config: IngestConfig,
    model: BaseLlm | str | None = None,
    skills_dir: str | Path | None = None,
) -> LlmAgent:
  """Dựng MergeAgent (single_turn, output_schema MergeProposal).

  Args:
    config: Cấu hình ingest (model_id mặc định).
    model: Instance BaseLlm hoặc tên model; None thì dùng config.model_id.
    skills_dir: Thư mục cha chứa skill; None thì dùng thư mục skills/
      cạnh package ingest.

  Returns:
    LlmAgent MergeAgent đã cấu hình xong.
  """
  return build_single_turn_agent(
      name="merge_agent",
      description="Đề xuất nhóm gộp các bảng là mảnh của cùng bảng logic.",
      instruction=_MERGE_INSTRUCTION,
      output_schema=MergeProposal,
      output_key="merge_proposal",
      skill_name="datamate-merge",
      inline_skill=True,
      config=config,
      model=model,
      skills_dir=skills_dir,
  )
