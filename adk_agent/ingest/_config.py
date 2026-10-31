# -*- coding: utf-8 -*-
"""Cấu hình chạy cho workflow ingest ADK.

Module này thuộc tầng cấu hình: mọi tham số chạy của workflow (thư mục
vào/ra, giới hạn loop, giới hạn mẫu, tên model) đều đi qua
:class:`IngestConfig`, không đọc biến toàn cục rải rác trong các node.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# Số lần sinh tối đa cho mỗi loop viết lại (1 lần đầu + 2 lần viết lại).
# Vì sao là 3: đủ cho model sửa lỗi thường gặp (sai tên cột, sai định
# dạng JSON) mà không đốt quá nhiều lần gọi model cho một shop; hết 3 lần
# thì đi nhánh fail an toàn thay vì lặp vô hạn.
MAX_LOOP_ATTEMPTS = 3
# Số lần một lỗi được phép lặp lại trước khi fail sớm: cùng một lỗi xuất
# hiện 2 lần liên tiếp nghĩa là model không sửa được lỗi đó, viết lại nữa
# cũng khó khác nên dừng sớm để tiết kiệm lần gọi.
MAX_REPEATED_ERROR = 2
# README của shop chỉ đưa tối đa 4000 ký tự vào state: đủ để model hiểu
# bối cảnh shop mà không phình prompt; README dài hơn thì phần đầu thường
# đã chứa mô tả bảng quan trọng nhất.
README_MAX_CHARS = 4000
# Số dòng mẫu đầu tiên lấy cho mỗi bảng khi quét.
SAMPLE_FIRST_ROWS = 10
# Số dòng mẫu ngẫu nhiên (seed cố định) lấy thêm ở giữa/cuối bảng: bắt
# các cột thưa chỉ xuất hiện ở dòng sau, mà mẫu 10 dòng đầu không thấy.
SAMPLE_RANDOM_ROWS = 10
# Seed cố định cho mẫu ngẫu nhiên: chạy lại ingest lần 2 phải cho cùng
# mẫu, cùng DB và cùng schema (tính tái lập được).
SAMPLE_SEED = 42
# Distinct chỉ liệt kê đầy đủ trong schema khi số giá trị khác nhau
# không vượt quá ngưỡng này; vượt ngưỡng thì chỉ giữ số lượng + mẫu.
# Vì sao là 50: cột trạng thái thường dưới 50 giá trị và rất cần liệt kê
# đầy đủ cho bước sinh SQL, còn cột mã (hàng nghìn giá trị) thì liệt kê
# đầy đủ chỉ làm phình schema vô ích.
DISTINCT_FULL_LIMIT = 50
# Cắt danh sách dài (distinct, mẫu) khi đưa vào prompt/model: tránh tràn
# context ở shop nhiều bảng.
TRUNCATE_LIST_LIMIT = 500
# Độ dài tối đa của một ô mẫu khi đưa vào state: ô mô tả dài không giới
# hạn sẽ phình state và làm chậm serialize event.
SAMPLE_CELL_MAX_CHARS = 200
# Số dòng tối đa của một bảng trong state mẫu: mẫu chỉ để model hiểu
# dạng dữ liệu, không phải để model đọc toàn bộ bảng.
SAMPLE_MAX_ROWS = 20


class IngestConfig(BaseModel):
  """Cấu hình cho một lần chạy ingest của một hoặc nhiều shop.

  Mọi trường đều có giá trị mặc định hợp lý để driver có thể chạy ngay
  mà không cần truyền đủ tham số. Trường ``model_id`` là tên model mặc
  định (chuỗi) khi workflow không được truyền instance ``BaseLlm`` riêng;
  khi truyền instance model (fake trong test, LiteLlm thật khi chạy thật)
  thì instance đó thắng model_id.
  """

  model_config = ConfigDict(extra="forbid")

  data_dir: str = ""
  """Thư mục gốc chứa dữ liệu các shop (mỗi shop một thư mục con)."""

  output_dir: str = ""
  """Thư mục gốc ghi kết quả: DB, schema file, log và staging."""

  app_name: str = "datamate-ingest"
  """Tên ứng dụng ADK dùng làm khóa session (app_name, user_id, session_id)."""

  user_id: str = "datamate"
  """User id cố định cho mọi session ingest (batch, không có user thật)."""

  max_loop_attempts: int = Field(default=MAX_LOOP_ATTEMPTS, ge=1)
  """Số lần sinh tối đa cho mỗi loop (xem hằng số MAX_LOOP_ATTEMPTS)."""

  max_repeated_error: int = Field(default=MAX_REPEATED_ERROR, ge=1)
  """Số lần một lỗi được phép lặp lại trước khi fail sớm."""

  readme_max_chars: int = Field(default=README_MAX_CHARS, ge=0)
  """Số ký tự tối đa của README đưa vào state."""

  sample_first_rows: int = Field(default=SAMPLE_FIRST_ROWS, ge=0)
  """Số dòng mẫu đầu tiên lấy cho mỗi bảng khi quét."""

  sample_random_rows: int = Field(default=SAMPLE_RANDOM_ROWS, ge=0)
  """Số dòng mẫu ngẫu nhiên lấy thêm cho mỗi bảng khi quét."""

  sample_seed: int = SAMPLE_SEED
  """Seed cố định cho mẫu ngẫu nhiên (tính tái lập được)."""

  distinct_full_limit: int = Field(default=DISTINCT_FULL_LIMIT, ge=0)
  """Ngưỡng distinct để liệt kê đầy đủ trong schema file."""

  session_kind: str = "memory"
  """Loại session service: "memory" (InMemory) hoặc "sqlite" (file local)."""

  model_id: str = ""
  """Tên model mặc định cho 3 agent khi không truyền instance model riêng
  (chuỗi rỗng nghĩa là chưa có model — dựng agent lúc đó sẽ fail fast)."""
