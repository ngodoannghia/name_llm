# -*- coding: utf-8 -*-
"""Các node validator, route và fallback cho 4 loop của workflow ingest.

Các loop là header, merge, schema theo từng bảng và schema meta. Loop
schema theo từng bảng khác 3 loop còn lại ở điểm: hết lượt thử thì
không fail cả shop mà fallback bảng đó về bản nháp đo được (xem
docstring của make_route_schema_table), vì dữ liệu đo được của bảng
vẫn đúng, chỉ thiếu mô tả do model viết.


Module này thuộc tầng kiểm chứng. Nguyên tắc xuyên suốt (xem plan §2.4):
- Validator trả Verdict, không raise exception để kết thúc loop (node
  raise sẽ dừng cả workflow).
- Route node phát route chuỗi "ok" / "retry" / "fail" dựa trên Verdict và
  bộ đếm trong state; bộ đếm được ghi lại qua Event(state=...), không
  gán trực tiếp ctx.state (replay không tái tạo gán trực tiếp).
- Nhánh "fail" luôn đi tới một node ghi log/kết luận, không bao giờ raise.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

from google.adk import Event

from .._config import IngestConfig
from .._models import (
    HeaderProposal,
    MergeProposal,
    TableHeader,
    RawTable,
    ShopMeta,
    TableSchema,
    Verdict,
)
from .._phase1 import normalize_key
from .._readers import _clean_header_cell, _dedupe_columns


def _normalized(columns: list[str]) -> list[str]:
  """Chuẩn hóa danh sách tên cột để so sánh không phân biệt dấu/case.

  Args:
    columns: Danh sách tên cột gốc.

  Returns:
    Danh sách tên cột đã chuẩn hóa, giữ nguyên thứ tự.
  """
  return [normalize_key(col) for col in columns]


def _verdict_event(
    verdict: Verdict, state: dict | None = None
) -> Event:
  """Đóng gói Verdict thành một Event output duy nhất kèm state.

  Nguyên tắc phân công bắt buộc (đã sửa sau khi test workflow phát hiện
  route fail ngay lần đầu): VALIDATOR KHÔNG ĐƯỢC GHI last_error khi
  fail. Route node so sánh reason lần này với last_error của lần fail
  TRƯỚC (do chính route ghi ở lần trước); nếu validator cũng ghi
  last_error = reason ngay trong lần fail này thì route đọc state sẽ
  thấy reason trùng với chính nó và tưởng nhầm là lỗi lặp, fail sớm sai.
  Vì vậy last_error và bộ đếm attempts chỉ do route node ghi; validator
  chỉ ghi kết quả chốt (headers_final / merge_groups / schema_path).

  Args:
    verdict: Kết quả kiểm chứng.
    state: State delta ghi kèm (mặc định rỗng, không chứa last_error
      khi fail).

  Returns:
    Event mang output là Verdict đã dump và state delta đã cho.
  """
  return Event(output=verdict.model_dump(), state=state or {})


def _header_row_plausible(
    row: list[str], candidate_rows: list[list[str]], index: int
) -> bool:
  """Kiểm tra một dòng ứng viên có dáng của dòng header không.

  Đây là lưới chắn thô, không phải chứng minh "đây chính là header":
  validator chỉ chặn được dòng trống, dòng toàn số (dòng dữ liệu/dòng
  tổng), và trường hợp dưới dòng đó không còn dữ liệu nào trong cửa sổ
  15 dòng (đề xuất trỏ xuống cuối bảng trống). Việc chọn đúng dòng nào
  trong các dòng có dáng header vẫn là phán đoán của agent — giống như
  trước đây việc tin dòng quét cũng là phán đoán của luật scoring.

  Args:
    row: Dòng ứng viên (dạng chuỗi).
    candidate_rows: 15 dòng thô đầu của bảng.
    index: Vị trí của dòng ứng viên trong candidate_rows.

  Returns:
    True khi dòng có ít nhất một ô chứa chữ cái và (nếu còn dòng dưới
    trong cửa sổ) ít nhất một dòng dưới có ô không trống.
  """
  if not any(ch.isalpha() for cell in row for ch in cell):
    return False
  below = candidate_rows[index + 1 :]
  if below and not any(cell.strip() for below_row in below for cell in below_row):
    return False
  return True


def _matched_candidate_row(item: TableHeader, table: dict) -> int | None:
  """Tìm dòng thô mà đề xuất header của agent khớp vào.

  Đề xuất đạt khi tên cột chuẩn hóa của nó khớp đúng một dòng bất kỳ
  trong 15 dòng thô đầu của bảng (sau khi làm sạch ô trống thành cot_N
  và khử trùng tên như lúc quét), không bắt buộc khớp đúng dòng mà
  quét đã chọn — chính điểm này sửa vòng lặp header no-op cũ (validator
  cũ bắt agent nhắc lại đúng candidate nên scan sai không ai sửa được).
  Dòng agent khai trong header_row được ưu tiên nếu dòng đó khớp, còn
  không thì lấy dòng khớp đầu tiên; node làm sạch dùng dòng khớp này
  (qua header_rows_final), không dùng dòng agent khai, vì cột mới là
  bằng chứng còn số dòng khai có thể nhầm.

  Args:
    item: Đề xuất header cho một bảng.
    table: RawTable đã dump của bảng đó.

  Returns:
    Chỉ số dòng khớp (0-based; -1 với bảng SQLite), hoặc None khi
    không dòng nào khớp hoặc dòng khớp không có dáng header. Bảng quét
    từ trước khi có candidate_rows (không có dòng thô trong state) thì
    quay về luật cũ: chỉ khớp khi bằng đúng header ứng viên.
  """
  normalized_proposed = _normalized(item.columns)
  if table.get("header_row", -1) < 0:
    # Bảng SQLite: dòng duy nhất là tên cột từ schema DB.
    if normalized_proposed == _normalized(table.get("header", [])):
      return -1
    return None
  candidate_rows = table.get("candidate_rows") or []
  if not candidate_rows:
    # State cũ không có dòng thô: giữ luật cũ để không bác oan đề xuất
    # đúng candidate của các bảng quét trước bản này.
    if normalized_proposed == _normalized(table.get("header", [])):
      return table.get("header_row", 0)
    return None
  order: list[int] = []
  if 0 <= item.header_row < len(candidate_rows):
    order.append(item.header_row)
  order.extend(index for index in range(len(candidate_rows)) if index not in order)
  for index in order:
    row = candidate_rows[index]
    cleaned = _dedupe_columns(
        [_clean_header_cell(cell, i) for i, cell in enumerate(row)]
    )
    if _normalized(cleaned) != normalized_proposed:
      continue
    if not _header_row_plausible(row, candidate_rows, index):
      continue
    return index
  return None


def validate_header(
    node_input: HeaderProposal,
    raw_tables: list[dict] | None = None,
) -> Event:
  """Kiểm chứng đề xuất header của HeaderAgent.

  Quy tắc kiểm chứng (sửa vòng lặp header no-op cũ):
  - Đề xuất đạt khi tên cột chuẩn hóa khớp một dòng bất kỳ trong 15
    dòng thô đầu của bảng (xem _matched_candidate_row) và dòng đó có
    dáng header; không bắt buộc khớp đúng dòng quét đã chọn.
  - Bảng mơ hồ mà agent không đề xuất thì bị bác (không còn giữ
    candidate như trước): giữ candidate lặng lẽ chính là lý do sentinel
    header cũ {"tables": []} lọt qua validator với "ok" giả, mất lượt
    retry và ghi log sai. Bảng không mơ hồ mà không nằm trong đề xuất
    thì vẫn giữ header ứng viên (agent chỉ cần quyết bảng mơ hồ).
  - State ghi thêm header_rows_final (dòng header chốt theo bảng) để
    node làm sạch dùng đúng dòng agent đã chốt thay vì detect lại.

  Args:
    node_input: Đề xuất header (đã coerce thành HeaderProposal từ output
      dict của agent).
    raw_tables: Danh sách RawTable đã dump (lấy từ state).

  Returns:
    Một Event duy nhất mang Verdict và state (headers_final và
    header_rows_final khi đạt; nhánh fail không ghi last_error — route
    node tự ghi, xem docstring của _verdict_event).
  """
  tables = {table["table_id"]: table for table in (raw_tables or [])}
  proposed: dict[str, list[str]] = {}
  proposed_rows: dict[str, int] = {}
  for item in node_input.tables:
    if item.table_id not in tables:
      return _verdict_event(
          Verdict(
              status="fail",
              reason=f"Bảng {item.table_id} không tồn tại trong dữ liệu quét.",
          ),
          state={},
      )
    if not item.columns:
      return _verdict_event(
          Verdict(
              status="fail",
              reason=f"Bảng {item.table_id} đề xuất header rỗng.",
          ),
          state={},
      )
    normalized = _normalized(item.columns)
    if len(set(normalized)) != len(normalized):
      return _verdict_event(
          Verdict(
              status="fail",
              reason=f"Bảng {item.table_id} có tên cột trùng nhau sau chuẩn hóa.",
          ),
          state={},
      )
    matched = _matched_candidate_row(item, tables[item.table_id])
    if matched is None:
      return _verdict_event(
          Verdict(
              status="fail",
              reason=(
                  f"Bảng {item.table_id} đề xuất header không khớp dòng nào "
                  "trong 15 dòng đầu của bảng thô."
              ),
          ),
          state={},
      )
    proposed[item.table_id] = item.columns
    proposed_rows[item.table_id] = matched
  # Bảng mơ hồ thiếu đề xuất thì bác: "thiếu đề xuất không phải lỗi"
  # là luật cũ đã làm sentinel header rỗng lọt qua với "ok" giả (mất
  # lượt retry, log ghi ok trong khi agent chưa chốt gì). Bảng không
  # mơ hồ thì không cần đề xuất nên không bị luật này chạm tới.
  for table_id, table in tables.items():
    if table.get("ambiguous") and table_id not in proposed:
      return _verdict_event(
          Verdict(
              status="fail",
              reason=(
                  f"Bảng {table_id} mơ hồ nhưng không có đề xuất header."
              ),
          ),
          state={},
      )
  # Các bảng không nằm trong đề xuất thì giữ header ứng viên và dòng
  # quét đã chọn.
  final = {table_id: table["header"] for table_id, table in tables.items()}
  final.update(proposed)
  final_rows = {
      table_id: table.get("header_row", -1) for table_id, table in tables.items()
  }
  final_rows.update(proposed_rows)
  return _verdict_event(
      Verdict(status="ok", reason="Header đề xuất khớp dữ liệu quét."),
      state={
          "headers_final": final,
          "header_rows_final": final_rows,
          "last_error": "",
      },
  )


def _lot_variant_name(table_id: str) -> str:
  """Bỏ hậu tố lô khỏi id bảng để phát hiện cặp ChiTiet/ChiTietLo.

  Đây chỉ là đường dự phòng khi không có tên file/sheet gốc (ví dụ
  fixture test tối thiểu): với cặp lô thật của train, hậu tố "lo" nằm
  GIỮA id (trước "_sheet1") nên hàm này mù — đường chính phải là
  _lot_base_for_table dùng tên gốc (xem dưới). Hàm này được giữ để
  fixture không có tên gốc vẫn giữ được luật cấm cũ.

  Args:
    table_id: Id bảng đã chuẩn hóa.

  Returns:
    Id sau khi bỏ hậu tố "_lo" hoặc "lo" ở cuối (nếu có).
  """
  if table_id.endswith("_lo"):
    return table_id[: -len("_lo")]
  if table_id.endswith("lo"):
    return table_id[: -len("lo")]
  return table_id


# Hậu tố lô nhận diện, xếp dài trước ngắn sau để "chitiet_lot" bị trừ
# "_lot" (thành "chitiet") chứ không bị trừ "lot" (thành "chitiet_" rồi
# mới cắt gạch dưới — kết quả cuối giống nhau nhưng trừ dài trước rõ
# ràng hơn và tránh trường hợp biên như "lo_hang" bị trừ "hang" lẻ).
# Danh sách này chỉ gồm cách gọi lô chung, không gắn với shop nào.
_LOT_SUFFIXES = ("_lo_hang", "lo_hang", "theo_lo", "_lot", "lot", "_lo", "lo")


def _strip_lot_suffix(normalized: str) -> str:
  """Bỏ một hậu tố lô ở cuối tên đã chuẩn hóa (nếu có).

  Args:
    normalized: Tên đã chuẩn hóa (thường, không dấu, chỉ còn chữ/số
      và gạch dưới).

  Returns:
    Tên sau khi bỏ hậu tố; tên gốc nếu không có hậu tố nào hoặc nếu bỏ
    xong thành rỗng (tránh base rỗng làm mọi bảng đều "trùng" nhau).
  """
  for marker in _LOT_SUFFIXES:
    if normalized.endswith(marker):
      stripped = normalized[: -len(marker)].rstrip("_")
      return stripped if stripped else normalized
  return normalized


def _lot_base(source_file: str, source_sheet: str) -> str:
  """Tính "gốc lô" của một bảng từ tên file/sheet GỐC.

  Vì sao phải dùng tên gốc thay vì table_id: table_id là tên file +
  tên sheet đã sanitize và ghép lại, nên hậu tố lô của file (ví dụ
  "ChiTietLo") nằm giữa id ("danhsachhoadonchitietlo_sheet1") và hàm
  trừ hậu tố cuối id không bao giờ thấy nó — đó là lý do guard lô cũ
  chưa từng kích hoạt trên train. Tính trên tên gốc thì ranh giới
  file/sheet còn nguyên: trừ hậu tố lô ở cuối tên file và cuối tên
  sheet rồi ghép lại, hai bảng chỉ khác nhau ở hậu tố lô sẽ có cùng
  gốc.

  Args:
    source_file: Tên file nguồn (kèm đuôi).
    source_sheet: Tên sheet nguồn (rỗng với CSV/SQLite).

  Returns:
    Chuỗi gốc lô để so sánh (hai bảng cùng gốc thì nghi là cặp
    chi tiết/biến thể lô của nhau).
  """
  stem = re.sub(r"[^a-z0-9]+", "_", normalize_key(Path(source_file).stem)).strip("_")
  stem_base = _strip_lot_suffix(stem)
  if not source_sheet:
    return stem_base
  sheet = re.sub(r"[^a-z0-9]+", "_", normalize_key(source_sheet)).strip("_")
  sheet_base = _strip_lot_suffix(sheet)
  return f"{stem_base}_{sheet_base}" if sheet_base else stem_base


def _lot_base_for_table(table: dict) -> str:
  """Tính gốc lô cho một bảng trong dữ liệu quét.

  Args:
    table: RawTable đã dump (có source_file/source_sheet trong luồng
      thật; fixture tối thiểu có thể không có).

  Returns:
    Gốc lô từ tên gốc khi có tên gốc, ngược lại gốc từ table_id
    (đường dự phòng _lot_variant_name).
  """
  if table.get("source_file") or table.get("source_sheet"):
    return _lot_base(
        table.get("source_file", ""), table.get("source_sheet", "")
    )
  return _lot_variant_name(table.get("table_id", ""))


def validate_merge(
    node_input: MergeProposal,
    raw_tables: list[dict] | None = None,
    merge_locked_groups: list[dict] | None = None,
) -> Event:
  """Kiểm chứng đề xuất gộp của MergeAgent, rồi gộp tiếp với nhóm khóa.

  MergeAgent chỉ nhận các bảng CHƯA khóa (nhóm khóa bởi code đã chốt
  ở merge_gate và không nằm trong đầu vào của agent), nên validator
  chỉ kiểm đề xuất trên tập bảng chưa khóa đó, rồi gộp kết quả đạt với
  nhóm khóa thành merge_groups cuối. Nhóm khóa không đi qua validator:
  nó là quyết định của code theo quy tắc "cùng chữ ký cột chuẩn hóa
  thì gộp", không phải phỏng đoán của model.

  Quy tắc kiểm chứng (chỉ áp dụng cho phần đề xuất của agent):
  - Mọi bảng chưa khóa phải xuất hiện đúng một lần trong đề xuất (nhóm
    hoặc singles), không sót, không trùng. Đề xuất nhắc tới bảng đã
    khóa hoặc bảng không tồn tại đều bị bác (bảng đã khóa không nằm
    trong đầu vào nên agent không được quyết lại nó).
  - Nhóm nhiều bảng chỉ đạt khi mọi bảng trong nhóm có cùng chữ ký cột
    (điều kiện cần; chữ ký khác nhau thì chắc chắn không cùng bảng logic).
  - Nhóm chứa đồng thời một bảng và biến thể lô của nó (ChiTiet +
    ChiTietLo) thì bị bác: gộp hai biến thể này sẽ đếm trùng dòng chi
    tiết, phải so sánh toàn bộ schema trước khi gộp (khung lần này chưa
    có bước so sánh đó nên mặc định không gộp). Phát hiện biến thể lô
    dựa trên tên file/sheet GỐC (xem _lot_base_for_table), không dựa
    trên đuôi table_id — vì hậu tố lô của file nằm giữa id nên trừ
    đuôi id sẽ mù trước cặp lô thật.

  Args:
    node_input: Đề xuất gộp (đã coerce thành MergeProposal).
    raw_tables: Danh sách RawTable đã dump (lấy từ state).
    merge_locked_groups: Nhóm đã khóa bởi code (lấy từ state, do
      merge_gate ghi; None thì coi như không có nhóm khóa).

  Returns:
    Một Event duy nhất mang Verdict và state (merge_groups cuối gồm
    nhóm khóa + đề xuất đạt khi ok; nhánh fail không ghi last_error —
    route node tự ghi).
  """
  tables = {table["table_id"]: table for table in (raw_tables or [])}
  locked = merge_locked_groups or []
  locked_ids = {table_id for group in locked for table_id in group["tables"]}
  unlocked_ids = set(tables) - locked_ids
  seen: list[str] = []
  for group in node_input.groups:
    if not group.tables:
      return _verdict_event(
          Verdict(status="fail", reason="Có nhóm gộp rỗng, không chứa bảng nào."),
          state={},
      )
    for table_id in group.tables:
      if table_id in locked_ids:
        return _verdict_event(
            Verdict(
                status="fail",
                reason=(
                    f"Bảng {table_id} đã nằm trong nhóm khóa bởi code nên "
                    "agent không được quyết lại nó."
                ),
            ),
            state={},
        )
      if table_id not in tables:
        return _verdict_event(
            Verdict(
                status="fail",
                reason=f"Bảng {table_id} không tồn tại trong dữ liệu quét.",
            ),
            state={},
        )
      if table_id in seen:
        return _verdict_event(
            Verdict(
                status="fail",
                reason=f"Bảng {table_id} xuất hiện ở nhiều nhóm gộp.",
            ),
            state={},
        )
      seen.append(table_id)
    if len(group.tables) > 1:
      signatures = {tables[table_id]["column_signature"] for table_id in group.tables}
      if len(signatures) != 1:
        return _verdict_event(
            Verdict(
                status="fail",
                reason=(
                    f"Nhóm {group.tables} có chữ ký cột khác nhau nên không "
                    "thể là cùng một bảng logic."
                ),
            ),
            state={},
        )
      bases = {_lot_base_for_table(tables[table_id]) for table_id in group.tables}
      if len(bases) != len(group.tables):
        return _verdict_event(
            Verdict(
                status="fail",
                reason=(
                    f"Nhóm {group.tables} chứa cả bảng và biến thể lô của nó "
                    "(dạng ChiTiet/ChiTietLo) nên không được gộp khi chưa so "
                    "sánh toàn bộ schema."
                ),
            ),
            state={},
        )
  for table_id in node_input.singles:
    if table_id in locked_ids:
      return _verdict_event(
          Verdict(
              status="fail",
              reason=(
                  f"Bảng {table_id} (giữ riêng) đã nằm trong nhóm khóa bởi "
                  "code nên agent không được quyết lại nó."
              ),
          ),
          state={},
      )
    if table_id not in tables:
      return _verdict_event(
          Verdict(
              status="fail",
              reason=f"Bảng {table_id} (giữ riêng) không tồn tại trong dữ liệu quét.",
          ),
          state={},
      )
    if table_id in seen:
      return _verdict_event(
          Verdict(
              status="fail",
              reason=f"Bảng {table_id} vừa nằm trong nhóm vừa nằm ngoài nhóm.",
          ),
          state={},
      )
    seen.append(table_id)
  if set(seen) != unlocked_ids:
    missing = sorted(unlocked_ids - set(seen))
    return _verdict_event(
        Verdict(
            status="fail",
            reason=f"Đề xuất gộp còn sót bảng chưa khóa chưa phân nhóm: {missing}.",
        ),
        state={},
    )
  combined = {
      "groups": locked + node_input.model_dump()["groups"],
      "singles": node_input.model_dump()["singles"],
  }
  return _verdict_event(
      Verdict(status="ok", reason="Đề xuất gộp khớp dữ liệu quét."),
      state={"merge_groups": combined, "last_error": ""},
  )


def _measured_mismatch(agent_table: dict, draft_table: dict) -> str:
  """So sánh các trường đã đo của bảng do agent viết với bản nháp.

  Chỉ so sánh trường đã đo (tên bảng/cột, kiểu, mẫu, distinct, số
  dòng); mô tả và role_hint là phần agent bổ sung nên không so sánh.
  Riêng date_range chỉ so sánh khi bản nháp đã đo được khoảng ngày
  thật (khác rỗng): write_sqlite_tmp hiện chưa đo khoảng ngày nên
  bản nháp luôn để {}, mà {} ở đây nghĩa là "chưa đo", không phải
  "bảng này không có ngày". So sánh tuyệt đối với {} sẽ bác oan mọi
  bảng mà agent tự điền khoảng ngày — đúng lỗi gặp ở test model thật
  biz_123 (model điền khoảng ngày hợp lý rồi bị bác, điền lại y hệt,
  rồi fallback). Khoảng ngày do agent tự điền không vào được file
  schema vì assemble_schema sẽ ghi đè bằng khoảng ngày của bản nháp,
  nên bỏ qua so sánh ở đây không làm lọt giá trị sai vào schema.

  So sánh với bản nháp tương đương kiểm chứng trực tiếp trên DB, vì
  bản nháp chính là kết quả đo từ DB tạm ở node write_sqlite_tmp
  (mẫu, distinct, số dòng đều truy vấn thật), nên validator này không
  cần mở DB lần nữa.

  Args:
    agent_table: Bảng do agent viết, đã dump thành dict.
    draft_table: Bảng trong bản nháp (lấy từ state).

  Returns:
    Chuỗi rỗng khi mọi trường đã đo khớp, ngược lại là lý do sai lệch.
  """
  for key in (
      "table_id",
      "original_names",
      "source_sheets",
      "sqlite_table",
      "row_count",
  ):
    if agent_table.get(key) != draft_table.get(key):
      return (
          f"Trường đã đo {key} của bảng {draft_table.get('sqlite_table')} "
          f"bị sửa: bản nháp là {draft_table.get(key)!r} nhưng agent "
          f"viết {agent_table.get(key)!r}."
      )
  draft_range = draft_table.get("date_range") or {}
  # Chỉ so sánh khi bản nháp đã đo được khoảng ngày thật: bản nháp {}
  # nghĩa là chưa đo, không phải giá trị phải khớp (xem docstring).
  if draft_range != {}:
    agent_range = agent_table.get("date_range") or {}
    if agent_range != draft_range:
      return (
          f"Khoảng ngày của bảng {draft_table.get('sqlite_table')} bị sửa: "
          f"bản nháp là {draft_range!r} nhưng agent viết "
          f"{agent_range!r}."
      )
  agent_columns = agent_table.get("columns", [])
  draft_columns = draft_table.get("columns", [])
  if len(agent_columns) != len(draft_columns):
    return (
        f"Bảng {draft_table.get('sqlite_table')} có "
        f"{len(draft_columns)} cột trong bản nháp nhưng agent viết "
        f"{len(agent_columns)} cột."
    )
  for agent_col, draft_col in zip(agent_columns, draft_columns):
    for key in (
        "original_name",
        "sqlite_name",
        "type",
        "sample_values",
        "distinct_count",
        "distinct_values",
        "nullable_rate",
    ):
      if agent_col.get(key) != draft_col.get(key):
        return (
            f"Trường đã đo {key} của cột {draft_col.get('sqlite_name')} "
            f"(bảng {draft_table.get('sqlite_table')}) bị sửa: bản nháp "
            f"là {draft_col.get(key)!r} nhưng agent viết "
            f"{agent_col.get(key)!r}."
        )
  return ""


def validate_schema_table(
    node_input: TableSchema,
    schema_table_drafts: list[dict] | None = None,
    schema_table_index: int = 0,
    schema_table_results: list[dict] | None = None,
) -> Event:
  """Kiểm chứng bảng do SchemaTableAgent viết so với bản nháp đã đo.

  Kiểm chứng hoàn toàn bằng so sánh với bản nháp (không mở DB, vì bản
  nháp đã đo từ DB): bảng phải đúng bảng đang xử lý, tập cột và mọi
  giá trị đã đo phải giữ nguyên. Mô tả và role_hint được phép khác
  bản nháp (đó chính là phần agent bổ sung); mô tả rỗng cũng đạt, vì
  chất lượng văn bản không được đánh đổi bằng việc bác cả bảng đúng
  dữ liệu.

  Args:
    node_input: Bảng đề xuất (đã coerce thành TableSchema).
    schema_table_drafts: Danh sách bản nháp các bảng (lấy từ state).
    schema_table_index: Vị trí bảng đang xử lý (lấy từ state).
    schema_table_results: Kết quả các bảng đã xong (lấy từ state).

  Returns:
    Một Event duy nhất mang Verdict và state (thêm bảng vào kết quả
    khi đạt; nhánh fail không ghi last_error — route node tự ghi).
  """
  drafts = schema_table_drafts or []
  if schema_table_index < 0 or schema_table_index >= len(drafts):
    return _verdict_event(
        Verdict(
            status="fail",
            reason=(
                f"Không tìm thấy bản nháp cho bảng thứ {schema_table_index}."
            ),
        ),
        state={},
    )
  draft = drafts[schema_table_index]
  mismatch = _measured_mismatch(node_input.model_dump(), draft)
  if mismatch:
    return _verdict_event(Verdict(status="fail", reason=mismatch), state={})
  results = list(schema_table_results or [])
  results.append(node_input.model_dump())
  return _verdict_event(
      Verdict(status="ok", reason="Bảng khớp bản nháp đã đo."),
      state={"schema_table_results": results, "last_error": ""},
  )


def make_route_schema_table(config: IngestConfig) -> Callable:
  """Tạo route node cho vòng lặp schema theo từng bảng.

  Luật chuyển route khác 3 loop còn lại ở nhánh fail cuối: hết lượt
  thử (lỗi lặp hoặc đủ giới hạn) thì KHÔNG fail cả shop mà fallback
  bảng đang xử lý về bản nháp đo được rồi đi tiếp bảng sau. Lý do:
  dữ liệu đo được của bảng vẫn đúng (validator chỉ bác phần mô tả/
  role do model viết sai hoặc model không trả được JSON), nên giữ bảng
  với mô tả rỗng và role unknown vẫn tốt hơn là hủy cả shop 17 bảng
  vì một bảng thiếu văn bản. Bảng fallback được ghi tên vào
  schema_table_fallbacks để assemble_schema chú thích vào notes.

  Các route phát ra:
  - "retry": validator fail nhưng còn lượt thử -> quay lại agent,
    cùng bảng.
  - "next": bảng xong (đạt hoặc fallback) và còn bảng sau -> quay
    lại agent với bản nháp của bảng tiếp theo.
  - "done": bảng cuối cùng xong -> sang init_schema_meta.

  Bộ đếm schema_table_attempts được reset về 0 mỗi khi sang bảng mới,
  vì giới hạn 3 lượt áp dụng cho từng bảng, không phải cho cả shop.

  Args:
    config: Cấu hình ingest (giới hạn loop).

  Returns:
    Hàm route node cho vòng lặp schema theo từng bảng.
  """

  def route_schema_table(ctx: object, node_input: Verdict) -> Event:
    """Chuyển Verdict của bảng thành route cho vòng lặp từng bảng.

    Args:
      ctx: Context của workflow (framework bind tự động); route node
        đọc danh sách bản nháp, vị trí, kết quả và bộ đếm từ ctx.state.
      node_input: Verdict từ validate_schema_table.

    Returns:
      Một Event duy nhất mang output là Verdict, state cập nhật (vị
      trí, kết quả, fallback, bộ đếm, bản nháp bảng tiếp theo) và
      route "retry" / "next" / "done".
    """
    state = ctx.state  # type: ignore[attr-defined]
    drafts = state.get("schema_table_drafts", [])
    index = state.get("schema_table_index", 0)
    if not isinstance(index, int):
      index = 0
    results = list(state.get("schema_table_results", []))
    fallbacks = list(state.get("schema_table_fallbacks", []))
    last_error = state.get("last_error", "")

    def _advance(
        new_results: list[dict], new_fallbacks: list[str]
    ) -> Event:
      """Sang bảng tiếp theo hoặc kết thúc vòng lặp khi hết bảng."""
      new_index = index + 1
      base_state: dict = {
          "schema_table_index": new_index,
          "schema_table_results": new_results,
          "schema_table_fallbacks": new_fallbacks,
          "schema_table_attempts": 0,
          "last_error": "",
      }
      if new_index >= len(drafts):
        return Event(
            output=node_input.model_dump(),
            state=base_state,
            route="done",
        )
      base_state["schema_table_draft_text"] = json.dumps(
          {"table": drafts[new_index]}, ensure_ascii=False
      )
      return Event(
          output=node_input.model_dump(),
          state=base_state,
          route="next",
      )

    if node_input.status == "ok":
      # Validator đã cộng bảng vừa xong vào schema_table_results qua
      # Event state của nó, và state đó đã áp dụng vào session trước
      # khi route chạy (cùng cơ chế param binding theo state của mọi
      # node), nên route chỉ việc đọc results từ ctx.state, không
      # cộng thêm lần nữa.
      return _advance(results, fallbacks)
    reason = node_input.reason
    raw_attempts = state.get("schema_table_attempts", 0)
    attempts = raw_attempts if isinstance(raw_attempts, int) else 0
    new_attempts = attempts + 1
    if (reason and reason == last_error) or (
        new_attempts >= config.max_loop_attempts
    ):
      # Hết lượt thử: fallback bảng này về bản nháp rồi đi tiếp.
      if 0 <= index < len(drafts):
        draft = drafts[index]
        results.append(draft)
        fallbacks.append(draft.get("table_id", ""))
      return _advance(results, fallbacks)
    return Event(
        output=node_input.model_dump(),
        state={
            "schema_table_attempts": new_attempts,
            "last_error": reason,
        },
        route="retry",
    )

  route_schema_table.__name__ = "route_schema_table"
  return route_schema_table


# Vai trò cột không bao giờ là khóa join: nối theo phương thức thanh
# toán, trạng thái, tiền hay số lượng sẽ fan-out nhiều-nhiều (một đơn
# tiền mặt nối với mọi thanh toán tiền mặt khác) chứ không nối đúng
# thực thể. Join trên các role này bị bác dù tham chiếu có tồn tại.
_NON_KEY_JOIN_ROLES = {"payment", "status", "money", "quantity"}
# Vai trò khóa join: hai đầu join phải cùng một vai trò khóa, hoặc ít
# nhất một đầu chưa biết vai trò (unknown) thì mới cho qua.
_KEY_JOIN_ROLES = {"order_id", "product_id", "customer_id"}


def validate_schema_meta(
    node_input: ShopMeta,
    schema_table_results: list[dict] | None = None,
) -> Event:
  """Kiểm chứng join_hints và notes do SchemaMetaAgent viết.

  Hai lớp kiểm chứng (lớp tham chiếu cũ là chưa đủ — join sai nhưng
  trỏ tới cột có thật vẫn lọt qua và đã lọt vào schema thật, ví dụ
  thanh_toan.httt -> don_hang.tra_bang nối theo phương thức thanh toán):
  1. Tính tham chiếu: mỗi join_hint phải trỏ tới bảng và cột có thật
     trong kết quả từng bảng (bảng/cột đã chốt ở vòng lặp trước).
  2. Tương thích vai trò, theo đúng thứ tự sau:
     - Bác khi một đầu có role thuộc nhóm không phải khóa
       (payment/status/money/quantity): join trên các cột này không nối
       thực thể mà fan-out nhiều-nhiều.
     - Bác khi cả hai đầu đều có role khóa đã biết mà khác nhau (ví dụ
       mã khách nối mã hàng): hai thực thể khác nhau không thể join.
     - Cho qua khi ít nhất một đầu unknown, hoặc hai đầu cùng role
       khóa (ví dụ cùng order_id).
  Notes là văn bản tự do nên không kiểm nội dung. Join sai tham chiếu
  hoặc sai vai trò sẽ sinh SQL sai ở bước trả lời nên phải bác để agent
  viết lại; hết lượt thì fallback về rỗng (xem mark_schema_meta_fallback).

  Giới hạn đã biết của luật này (ghi rõ để không ngộ nhận): join mà cả
  hai đầu đều unknown vẫn cho qua — ví dụ các join idcuahang vô nghĩa
  của biz_123 (cả hai đầu đều unknown) không bị luật này chặn, vì
  validator không có ground truth nào để biết idcuahang có phải khóa
  hay không. Chặn được chúng cần đo ID-overlap thật từ DB (mục nâng
  cấp ở FULL_PLAN §5 mục 10), không phải luật role. Trước mắt: QA
  không được tin join_hints mù, nhất là join unknown-unknown.

  Args:
    node_input: Phần meta đề xuất (đã coerce thành ShopMeta).
    schema_table_results: Kết quả các bảng đã xong (lấy từ state).

  Returns:
    Một Event duy nhất mang Verdict và state (schema_meta khi đạt;
    nhánh fail không ghi last_error — route node tự ghi).
  """
  tables: dict[str, set[str]] = {}
  roles: dict[tuple[str, str], str] = {}
  for table in schema_table_results or []:
    table_name = table.get("sqlite_table", "")
    tables[table_name] = set()
    for col in table.get("columns", []):
      col_name = col.get("sqlite_name", "")
      tables[table_name].add(col_name)
      roles[(table_name, col_name)] = col.get("role_hint", "unknown")
  for hint in node_input.join_hints:
    for table_name, col_name, role in (
        (hint.from_table, hint.from_column, "nguồn"),
        (hint.to_table, hint.to_column, "đích"),
    ):
      if table_name not in tables:
        return _verdict_event(
            Verdict(
                status="fail",
                reason=f"Join_hint trỏ tới bảng {role} {table_name} không có thật.",
            ),
            state={},
        )
      if col_name not in tables[table_name]:
        return _verdict_event(
            Verdict(
                status="fail",
                reason=(
                    f"Join_hint trỏ tới cột {col_name} không có trong bảng "
                    f"{table_name}."
                ),
            ),
            state={},
        )
    from_role = roles.get((hint.from_table, hint.from_column), "unknown")
    to_role = roles.get((hint.to_table, hint.to_column), "unknown")
    for role in (from_role, to_role):
      if role in _NON_KEY_JOIN_ROLES:
        return _verdict_event(
            Verdict(
                status="fail",
                reason=(
                    f"Join_hint {hint.from_table}.{hint.from_column} -> "
                    f"{hint.to_table}.{hint.to_column} nối theo cột vai trò "
                    f"{role}, không phải khóa thực thể nên sẽ fan-out "
                    "nhiều-nhiều."
                ),
            ),
            state={},
        )
    if (
        from_role in _KEY_JOIN_ROLES
        and to_role in _KEY_JOIN_ROLES
        and from_role != to_role
    ):
      return _verdict_event(
          Verdict(
              status="fail",
              reason=(
                  f"Join_hint {hint.from_table}.{hint.from_column} -> "
                  f"{hint.to_table}.{hint.to_column} nối hai vai trò khóa "
                  f"khác nhau ({from_role} với {to_role})."
              ),
          ),
          state={},
      )
  return _verdict_event(
      Verdict(
          status="ok",
          reason="Join_hints tham chiếu đúng bảng/cột và tương thích vai trò.",
      ),
      state={"schema_meta": node_input.model_dump(), "last_error": ""},
  )


def make_route_schema_meta(config: IngestConfig) -> Callable:
  """Tạo route node cho loop schema meta (tái dùng luật route chung).

  Args:
    config: Cấu hình ingest.

  Returns:
    Hàm route node cho loop schema meta.
  """
  return _make_route(config, "schema_meta_attempts")


def mark_schema_meta_fallback() -> Event:
  """Ghi fallback meta: join_hints và notes rỗng, không đoán quan hệ.

  Join rỗng an toàn hơn join sai: bước sinh SQL vẫn truy vấn được từng
  bảng riêng lẻ, chỉ thiếu gợi ý nối bảng. Đây là phương án an toàn
  khi model hết lượt thử mà vẫn viết join sai tham chiếu.

  Returns:
    Một Event duy nhất mang output ghi nhận fallback và state
    (schema_meta rỗng, cờ schema_meta_fallback).
  """
  return Event(
      output={"fallback": "schema_meta_empty"},
      state={
          "schema_meta": {"join_hints": [], "notes": []},
          "schema_meta_fallback": True,
          "last_error": "",
      },
  )

def _make_route(
    config: IngestConfig, attempts_key: str
) -> Callable:
  """Tạo route node cho một loop, đã gắn cấu hình giới hạn vòng.

  Luật chuyển route (xem CODE_DESIGN §6.2):
  - Verdict ok -> route "ok".
  - Verdict fail và lý do trùng đúng lỗi lần trước -> route "fail"
    (lỗi lặp 2 lần liên tiếp nghĩa là model không sửa được lỗi đó).
  - Verdict fail và số lần fail đã tới giới hạn -> route "fail".
  - Còn lại -> route "retry" và tăng bộ đếm thêm 1.

  Bộ đếm đếm số lần validator fail, nên tổng số lần agent chạy tối đa
  bằng đúng giới hạn (lần chạy đầu fail -> đếm 1, ..., lần chạy thứ N
  fail -> đếm N -> fail).

  Vì sao route node nhận tham số ``ctx`` thay vì khai báo bộ đếm theo
  tên: FunctionNode chỉ bind tham số theo tên state khi tên đó được khai
  báo tường minh trong chữ ký, mà key bộ đếm thì khác nhau theo từng
  loop (header_attempts / merge_attempts / schema_attempts) nên không
  thể khai báo chung một tên. Tham số ``ctx`` thì framework luôn bind
  thành Context, và route node đọc mọi key state cần thiết từ
  ``ctx.state`` — đây là cách đọc state chính thức, không phải workaround.

  Args:
    config: Cấu hình ingest (giới hạn loop).
    attempts_key: Key state của bộ đếm loop này.

  Returns:
    Hàm route node dùng được trực tiếp trong edges.
  """

  def route_node(ctx: object, node_input: Verdict) -> Event:
    """Chuyển Verdict thành route cho loop.

    Args:
      ctx: Context của workflow (framework bind tự động); route node đọc
        bộ đếm và lỗi lần trước từ ``ctx.state``.
      node_input: Verdict từ validator (đã coerce thành model).

    Returns:
      Một Event duy nhất mang output là Verdict, state cập nhật bộ đếm
      và last_error, và route "ok" / "retry" / "fail".
    """
    state = ctx.state  # type: ignore[attr-defined]
    last_error = state.get("last_error", "")
    if node_input.status == "ok":
      return Event(
          output=node_input.model_dump(),
          state={"last_error": ""},
          route="ok",
      )
    reason = node_input.reason
    # Lỗi lặp lại đúng lần trước thì fail sớm, không chờ đủ giới hạn.
    if reason and reason == last_error:
      return Event(
          output=node_input.model_dump(),
          state={"last_error": reason},
          route="fail",
      )
    raw_attempts = state.get(attempts_key, 0)
    attempts = raw_attempts if isinstance(raw_attempts, int) else 0
    new_attempts = attempts + 1
    if new_attempts >= config.max_loop_attempts:
      return Event(
          output=node_input.model_dump(),
          state={attempts_key: new_attempts, "last_error": reason},
          route="fail",
      )
    return Event(
        output=node_input.model_dump(),
        state={attempts_key: new_attempts, "last_error": reason},
        route="retry",
    )

  # Tên node phải duy nhất trong graph: FunctionNode lấy tên từ __name__
  # của hàm, mà cả ba route node đều dùng chung hàm route_node nên phải
  # đổi tên theo loop (route_header / route_merge / route_schema).
  route_node.__name__ = "route_" + attempts_key.replace("_attempts", "")
  return route_node


def make_route_header(config: IngestConfig) -> Callable:
  """Tạo route node cho loop header.

  Args:
    config: Cấu hình ingest.

  Returns:
    Hàm route node cho loop header.
  """
  return _make_route(config, "header_attempts")


def make_route_merge(config: IngestConfig) -> Callable:
  """Tạo route node cho loop gộp.

  Args:
    config: Cấu hình ingest.

  Returns:
    Hàm route node cho loop gộp.
  """
  return _make_route(config, "merge_attempts")


def mark_header_fallback(raw_tables: list[dict] | None = None) -> Event:
  """Ghi fallback header: quay về header ứng viên từ luật scoring.

  Fallback này được phép vì nó là luật đã kiểm chứng bằng mẫu (điểm
  scoring của phase1), không phải đoán đáp án: khi LLM hết 3 lần vẫn sai
  thì header ứng viên vẫn tốt hơn là dừng cả shop vì một bảng.

  Args:
    raw_tables: Danh sách RawTable đã dump (lấy từ state).

  Returns:
    Một Event duy nhất mang output ghi nhận fallback và state
    (headers_final = header ứng viên, cờ header_fallback).
  """
  tables = raw_tables or []
  final = {table["table_id"]: table["header"] for table in tables}
  final_rows = {
      table["table_id"]: table.get("header_row", -1) for table in tables
  }
  return Event(
      output={"fallback": "header_rule"},
      state={
          "headers_final": final,
          "header_rows_final": final_rows,
          "header_fallback": True,
          "last_error": "",
      },
  )


def mark_merge_fallback(
    raw_tables: list[dict] | None = None,
    merge_locked_groups: list[dict] | None = None,
) -> Event:
  """Ghi fallback gộp: giữ nhóm khóa bởi code, còn lại giữ riêng.

  Khác với trước đây (fallback là không gộp gì cả), fallback mới VẪN
  GIỮ nhóm khóa bởi code: nhóm khóa là quyết định của code theo quy
  tắc "cùng chữ ký cột chuẩn hóa thì gộp", không phải phỏng đoán của
  model nên không có lý do gì để hủy nó khi model sai. Chỉ các bảng
  chưa khóa mới rơi về giữ riêng — đó mới là phần model quyết sai.
  (Chính fallback cũ "không gộp gì cả" sẽ tái tạo lỗi biz_102 nếu
  model lại đề xuất sai rồi hết lượt thử.)

  Không gộp an toàn hơn gộp sai: gộp sai gây đếm trùng, còn không gộp
  chỉ làm schema có nhiều bảng hơn (bước sinh SQL vẫn truy vấn được từng
  bảng). Đây là phương án an toàn đã kiểm chứng, được ghi rõ trong log.

  Args:
    raw_tables: Danh sách RawTable đã dump (lấy từ state).
    merge_locked_groups: Nhóm đã khóa bởi code (lấy từ state, do
      merge_gate ghi; None thì coi như không có nhóm khóa).

  Returns:
    Một Event duy nhất mang output ghi nhận fallback và state
    (merge_groups = nhóm khóa + mỗi bảng chưa khóa một nhóm, cờ
    merge_fallback).
  """
  tables = raw_tables or []
  locked = merge_locked_groups or []
  locked_ids = {table_id for group in locked for table_id in group["tables"]}
  groups = {
      "groups": locked
      + [
          {"tables": [table["table_id"]], "evidence": "giữ riêng fallback"}
          for table in tables
          if table["table_id"] not in locked_ids
      ],
      "singles": [],
  }
  return Event(
      output={"fallback": "merge_none"},
      state={"merge_groups": groups, "merge_fallback": True, "last_error": ""},
  )


def mark_shop_failed(last_error: str = "") -> Event:
  """Ghi kết luận shop FAILED (không publish DB, không xuất schema xấu).

  Node này là đích của mọi nhánh fail cuối (schema fail, validate_shop
  fail): nó chỉ ghi trạng thái vào state, không raise exception, để
  node finalize vẫn chạy và driver vẫn đi tiếp sang shop khác.

  Args:
    last_error: Lý do lỗi gần nhất (lấy từ state).

  Returns:
    Một Event duy nhất mang output trạng thái failed và state
    (shop_status = failed, shop_error = lý do).
  """
  return Event(
      output={"shop_status": "failed"},
      state={"shop_status": "failed", "shop_error": last_error},
  )
