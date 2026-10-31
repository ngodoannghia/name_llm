# -*- coding: utf-8 -*-
"""Các model Pydantic dùng chung cho toàn bộ workflow ingest.

Module này thuộc tầng hợp đồng dữ liệu: mọi input/output của node,
``output_schema`` của LLM và giá trị state có cấu trúc đều là model ở
đây, không dùng ``dict[str, Any]`` tự do. Lý do (theo skill
adk-agent-builder): dict mất validation, mất gợi ý IDE và mất khả năng
FunctionNode tự coerce dict thành model từ type hint.

Quy tắc ``extra`` (theo skill adk-style/pydantic):
- Model argument/config (đi trong workflow, không persisted) dùng
  ``extra="forbid"``: sai chính tả field phải raise, không được lặng
  lẽ rơi.
- Model persisted (ghi ra file JSON rồi đọc lại ở workflow khác) giữ
  ``extra`` mặc định: reader cũ phải chịu được field mới do writer mới
  thêm vào.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Các giá trị role_hint được phép trong schema file. Không tự bịa role
# mới: bước sinh SQL về sau chỉ hiểu đúng các role này.
RoleHint = Literal[
    "order_id",
    "product_id",
    "customer_id",
    "date",
    "money",
    "quantity",
    "status",
    "payment",
    "unknown",
]
# Các trạng thái verdict mà validator trả về, cũng là các route mà route
# node phát ra. Ba giá trị này phải khớp nhau tuyệt đối: route node chỉ
# đọc status rồi phát route cùng tên.
VerdictStatus = Literal["ok", "retry", "fail"]


class Verdict(BaseModel):
  """Kết quả kiểm chứng của một validator node.

  Validator không bao giờ raise exception để kết thúc loop (node raise
  sẽ dừng cả workflow); thay vào đó nó trả Verdict và route node quyết
  định đi tiếp, quay lại hay fail dựa trên status này.
  """

  model_config = ConfigDict(extra="forbid")

  status: VerdictStatus
  """Trạng thái kiểm chứng: ok (đạt), retry (sai, viết lại được), fail (sai, hết cách)."""

  reason: str = ""
  """Lý do đạt/không đạt, viết bằng tiếng Việt để đưa lại cho agent viết lại và ghi log."""

  details: dict[str, str] = Field(default_factory=dict)
  """Chi tiết bổ sung dạng chuỗi (ví dụ giá trị thật đo được, tên bảng vi phạm)."""


class TableHeader(BaseModel):
  """Header chốt cho một bảng trong đề xuất của HeaderAgent."""

  model_config = ConfigDict(extra="forbid")

  table_id: str
  """Id bảng trong dữ liệu quét (xem RawTable.table_id)."""

  header_row: int = Field(ge=-1)
  """Chỉ số dòng header (0-based); -1 nghĩa là bảng SQLite (không qua detect header)."""

  columns: list[str]
  """Danh sách tên cột chốt, theo đúng thứ tự cột trong dữ liệu."""


class HeaderProposal(BaseModel):
  """Đề xuất header của HeaderAgent cho mọi bảng mơ hồ trong shop."""

  model_config = ConfigDict(extra="forbid")

  tables: list[TableHeader]
  """Header chốt cho từng bảng mơ hồ; bảng nào không mơ hồ thì không cần nằm đây."""


class MergeGroup(BaseModel):
  """Một nhóm bảng được đề xuất gộp thành một bảng logic."""

  model_config = ConfigDict(extra="forbid")

  tables: list[str]
  """Id các bảng trong nhóm, theo thứ tự ưu tiên (bảng đầu làm chuẩn tên cột)."""

  evidence: str = ""
  """Bằng chứng gộp (chữ ký cột, ID overlap, khoảng ngày) viết bằng tiếng Việt."""


class MergeProposal(BaseModel):
  """Đề xuất gộp của MergeAgent cho toàn bộ bảng trong shop."""

  model_config = ConfigDict(extra="forbid")

  groups: list[MergeGroup]
  """Các nhóm gộp; nhóm 1 bảng nghĩa là bảng đó giữ riêng nhưng vẫn phải khai ở đây."""

  singles: list[str] = Field(default_factory=list)
  """Id các bảng giữ riêng không nằm trong nhóm nào (tương thích ngược, thường để trống)."""


class ColumnSchema(BaseModel):
  """Mô tả một cột trong file schema của shop (model persisted)."""

  original_name: str
  """Tên cột gốc trong dữ liệu nguồn."""

  sqlite_name: str
  """Tên cột sau khi sanitize thành identifier SQL hợp lệ."""

  type: str
  """Kiểu SQLite: INTEGER, REAL hoặc TEXT."""

  description_vi: str = ""
  """Mô tả vai trò của cột bằng tiếng Việt."""

  role_hint: RoleHint = "unknown"
  """Vai trò gợi ý của cột, chỉ lấy trong enum RoleHint."""

  sample_values: list[str] = Field(default_factory=list)
  """Mẫu tối đa 5 giá trị, tất cả phải có thật trong DB (validator kiểm bằng truy vấn)."""

  distinct_count: int = Field(ge=0, default=0)
  """Số giá trị khác nhau đo được trong cột."""

  distinct_values: list[str] | None = None
  """Liệt kê đầy đủ chỉ khi distinct_count <= ngưỡng; nhiều hơn thì để None."""

  nullable_rate: float = Field(ge=0.0, le=1.0, default=0.0)
  """Tỷ lệ ô NULL/rỗng trong cột (0.0 đến 1.0)."""


class DateRange(BaseModel):
  """Khoảng ngày của một bảng, dạng ISO.

  Dùng model tường minh thay vì ``dict[str, str]`` vì OpenAI strict mode
  (qua LiteLlm) yêu cầu mọi key trong ``required`` đều phải có trong
  ``properties``: một field kiểu dict tự do bị bộ chuyển schema làm rơi
  khỏi ``properties`` trong khi vẫn còn trong ``required``, khiến
  response_format bị từ chối ngay trước lần gọi model đầu tiên.
  """

  min: str = ""
  """Ngày nhỏ nhất dạng ISO, rỗng nếu bảng không có cột ngày."""

  max: str = ""
  """Ngày lớn nhất dạng ISO, rỗng nếu bảng không có cột ngày."""


class TableSchema(BaseModel):
  """Mô tả một bảng trong file schema của shop (model persisted)."""

  table_id: str
  """Id bảng logic (bảng đầu tiên của nhóm gộp, hoặc bảng đơn)."""

  original_names: list[str] = Field(default_factory=list)
  """Tên bảng gốc (file/sheet) đã gộp thành bảng này."""

  source_sheets: list[str] = Field(default_factory=list)
  """Danh sách nguồn (tên file/sheet) của bảng này."""

  sqlite_table: str
  """Tên bảng trong SQLite sau khi sanitize."""

  row_count: int = Field(ge=0, default=0)
  """Số dòng trong bảng (đếm từ DB)."""

  description_vi: str = ""
  """Mô tả bảng bằng tiếng Việt: mỗi dòng là gì."""

  columns: list[ColumnSchema] = Field(default_factory=list)
  """Danh sách cột của bảng."""

  date_range: DateRange = Field(default_factory=DateRange)
  """Khoảng ngày của bảng nếu có cột ngày (xem DateRange)."""


class JoinHint(BaseModel):
  """Gợi ý join giữa hai bảng, kèm bằng chứng đo được (model persisted)."""

  from_table: str
  """Bảng nguồn của join."""

  from_column: str
  """Cột nguồn của join (tên SQL)."""

  to_table: str
  """Bảng đích của join."""

  to_column: str
  """Cột đích của join (tên SQL)."""

  evidence: str = ""
  """Bằng chứng đo được (ví dụ tỷ lệ ID khớp), không ghi join theo phỏng đoán tên cột."""


class ShopSchema(BaseModel):
  """File schema của một shop: hợp đồng duy nhất cho workflow sinh SQL.

  Model persisted nên giữ ``extra`` mặc định (xem docstring đầu file).
  """

  business_id: str
  """Id shop (routing key, không phải logic hard-code theo shop)."""

  source_files: list[str] = Field(default_factory=list)
  """Tên các file nguồn của shop."""

  generated_at: str = ""
  """Thời điểm sinh schema, dạng ISO."""

  model: str = ""
  """Tên model đã sinh schema (khai báo, không bao giờ ghi key hay credential)."""

  tables: list[TableSchema] = Field(default_factory=list)
  """Danh sách bảng logic của shop."""

  join_hints: list[JoinHint] = Field(default_factory=list)
  """Danh sách gợi ý join có bằng chứng."""

  notes: list[str] = Field(default_factory=list)
  """Các bẫy của shop (đếm trùng, giá theo ngày, số dư chạy...) bằng tiếng Việt."""


class ShopMeta(BaseModel):
  """Phần cấp shop của schema: join_hints và notes (output của agent meta).

  Model này chỉ chứa phần model phải suy luận ở cấp shop, tách khỏi
  TableSchema để workflow có thể gọi agent meta một lần duy nhất sau
  khi đã xong từng bảng: input của nó chỉ là tóm tắt bảng/cột/role,
  không kèm mẫu hay distinct nên output luôn nhỏ.
  """

  model_config = ConfigDict(extra="forbid")

  join_hints: list[JoinHint] = Field(default_factory=list)
  """Danh sách gợi ý join có bằng chứng (tên cột chung, vai trò khóa)."""

  notes: list[str] = Field(default_factory=list)
  """Các bẫy của shop bằng tiếng Việt, rỗng khi không phát hiện bẫy."""


class RawTable(BaseModel):
  """Kết quả quét một bảng nguồn: chỉ metadata và mẫu, không chứa toàn bộ dữ liệu."""

  model_config = ConfigDict(extra="forbid")

  table_id: str
  """Id bảng duy nhất trong shop (tên file/sheet đã chuẩn hóa)."""

  source_file: str
  """Tên file nguồn chứa bảng."""

  source_sheet: str = ""
  """Tên sheet nguồn (rỗng với CSV và SQLite)."""

  header: list[str] = Field(default_factory=list)
  """Tên cột ứng viên sau khi detect header và làm sạch tên."""

  header_row: int = Field(ge=-1, default=-1)
  """Chỉ số dòng header (0-based); -1 với bảng SQLite."""

  row_count: int = Field(ge=0, default=0)
  """Số dòng dữ liệu (không tính dòng header)."""

  sample_rows: list[list[str]] = Field(default_factory=list)
  """Mẫu tối đa 20 dòng (10 đầu + 10 ngẫu nhiên seed cố định), ô đã cắt ngắn."""

  ambiguous: bool = False
  """True khi header chưa chắc chắn và cần HeaderAgent xem lại."""

  column_signature: str = ""
  """Chữ ký cột: tên cột đã chuẩn hóa nối bằng dấu |, dùng để kiểm tra gộp."""


class CleanStats(BaseModel):
  """Thống kê làm sạch cho một bảng (và cộng dồn cho cả shop)."""

  model_config = ConfigDict(extra="forbid")

  rows_in: int = Field(ge=0, default=0)
  """Số dòng đọc vào trước khi làm sạch."""

  rows_out: int = Field(ge=0, default=0)
  """Số dòng còn lại sau khi dedup."""

  null_cells: int = Field(ge=0, default=0)
  """Số ô NULL/rỗng sau khi làm sạch."""

  unparseable_cells: int = Field(ge=0, default=0)
  """Số ô có giá trị nhưng không parse được theo ngữ nghĩa cột (thành NULL, không bịa)."""


class ColumnManifest(BaseModel):
  """Mô tả một cột trong manifest staging (nội bộ giữa node clean và node ghi)."""

  model_config = ConfigDict(extra="forbid")

  original_name: str
  """Tên cột gốc (tên chuẩn của nhóm gộp)."""

  sqlite_name: str
  """Tên cột sau khi sanitize."""

  type: str
  """Kiểu SQLite: INTEGER, REAL hoặc TEXT."""

  semantic: str = ""
  """Ngữ nghĩa cột theo map_columns của phase1 (rỗng nếu không map được)."""


class TableManifest(BaseModel):
  """Mô tả một bảng staging trong manifest (nội bộ giữa node clean và node ghi)."""

  model_config = ConfigDict(extra="forbid")

  table_id: str
  """Id bảng logic."""

  source_tables: list[str] = Field(default_factory=list)
  """Id các bảng nguồn đã gộp thành bảng này."""

  sqlite_table: str
  """Tên bảng trong SQLite."""

  columns: list[ColumnManifest] = Field(default_factory=list)
  """Danh sách cột của bảng."""

  row_count: int = Field(ge=0, default=0)
  """Số dòng staging."""

  stats: CleanStats = Field(default_factory=CleanStats)
  """Thống kê làm sạch của bảng này."""


class IngestReport(BaseModel):
  """Kết quả ingest của một shop, do node finalize trả về."""

  model_config = ConfigDict(extra="forbid")

  business_id: str
  """Id shop vừa ingest."""

  status: Literal["ok", "failed"]
  """Trạng thái cuối: ok (đã publish DB + schema) hoặc failed (không xuất DB xấu)."""

  db_path: str = ""
  """Đường dẫn DB chính thức (rỗng khi failed)."""

  schema_path: str = ""
  """Đường dẫn file schema (rỗng khi failed)."""

  table_count: int = Field(ge=0, default=0)
  """Số bảng logic đã ghi."""

  row_count: int = Field(ge=0, default=0)
  """Tổng số dòng đã ghi."""

  error: str = ""
  """Lý do lỗi khi failed, bằng tiếng Việt; rỗng khi ok."""
