# -*- coding: utf-8 -*-
"""Các node quét dữ liệu nguồn: khởi tạo shop, rẽ nhánh, quét sheet/SQLite.

Module này thuộc tầng node quét. Các node ở đây chỉ đọc metadata và mẫu
(không đưa toàn bộ dữ liệu vào state), và chỉ dùng API public của phase1
qua adapter (detect_header_row) để tìm dòng header thật.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from google.adk import Event

from .._config import IngestConfig
from .._models import RawTable
from .._readers import list_data_files, read_readme_text, read_source_tables
from ._validate_nodes import _lot_variant_name


def make_init_shop(config: IngestConfig) -> Callable:
  """Tạo node init_shop đã gắn cấu hình.

  Args:
    config: Cấu hình ingest (dùng giới hạn ký tự README).

  Returns:
    Hàm node init_shop dùng được trực tiếp trong edges của Workflow.
  """

  async def init_shop(
      business_id: str = "",
      shop_dir: str = "",
      output_dir: str = "",
  ) -> Event:
    """Khởi tạo shop: đọc README, liệt kê file, xác định loại nguồn.

    Node này chạy đầu tiên và lấy business_id/shop_dir/output_dir từ
    state ban đầu của session (driver tạo session kèm state này). Nó
    không tự đoán shop từ tên thư mục hay từ dữ liệu khác.

    Args:
      business_id: Id shop (routing key, lấy từ state session).
      shop_dir: Thư mục dữ liệu của shop (lấy từ state session).
      output_dir: Thư mục ghi kết quả (lấy từ state session).

    Returns:
      Một Event duy nhất mang output (business_id + input_kind) và state
      (business_id, shop_dir, output_dir, readme_text, input_files,
      input_kind). State được ghi qua Event để replay tái tạo được.

    Raises:
      ValueError: Khi business_id hoặc shop_dir trống (thiếu state ban
        đầu thì workflow không thể tiếp tục — đây là lỗi dừng thật, không
        phải lỗi để loop viết lại).
    """
    if not business_id or not shop_dir:
      raise ValueError(
          "init_shop thiếu business_id hoặc shop_dir trong state session; "
          "driver phải tạo session kèm state ban đầu."
      )
    readme_text = read_readme_text(shop_dir, config.readme_max_chars)
    files = [path.name for path in list_data_files(shop_dir)]
    # Loại nguồn: "sqlite" khi có file SQLite (nhánh quét SQLite sẽ chạy);
    # ngược lại là "default" (nhánh quét CSV/XLSX). Shop hỗn hợp cả hai
    # loại là giới hạn đã ghi trong _readers.py (khung lần này chưa hỗ trợ
    # đầy đủ shop hỗn hợp).
    input_kind = "sqlite" if any(
        name.lower().endswith((".sqlite", ".db")) for name in files
    ) else "default"
    output = {"business_id": business_id, "input_kind": input_kind}
    state = {
        "business_id": business_id,
        "shop_dir": shop_dir,
        "output_dir": output_dir,
        "readme_text": readme_text,
        "input_files": files,
        "input_kind": input_kind,
    }
    return Event(output=output, state=state)

  return init_shop


def branch_input(node_input: dict) -> Event:
  """Rẽ nhánh theo loại nguồn: SQLite đi quét SQLite, còn lại đi quét sheet.

  Node này là route node nhưng không cần đếm vòng: nó chỉ phát route
  một lần dựa trên output của init_shop. Vì vậy output của nó chính là
  input nhận được (để node quét sau đó vẫn thấy business_id/input_kind
  nếu cần), kèm route.

  Args:
    node_input: Output của init_shop (dict có key "input_kind").

  Returns:
    Một Event duy nhất mang output gốc và route "sqlite" hoặc "default".
  """
  kind = node_input.get("input_kind", "default") if isinstance(node_input, dict) else "default"
  route = "sqlite" if kind == "sqlite" else "default"
  return Event(output=node_input, route=route)


def make_scan_sheets(config: IngestConfig) -> Callable:
  """Tạo node scan_sheets đã gắn cấu hình mẫu.

  Args:
    config: Cấu hình ingest (số dòng mẫu, seed).

  Returns:
    Hàm node scan_sheets dùng được trực tiếp trong edges.
  """

  async def scan_sheets(
      business_id: str = "",
      shop_dir: str = "",
  ) -> Event:
    """Quét file CSV/XLSX của shop thành metadata bảng + mẫu.

    Args:
      business_id: Id shop (lấy từ state, chỉ dùng để ghi log ngầm qua
        tên bảng — không dùng để rẽ logic theo shop).
      shop_dir: Thư mục dữ liệu của shop (lấy từ state).

    Returns:
      Một Event duy nhất mang output là danh sách RawTable đã dump và
      state {"raw_tables": danh sách đó}. Dữ liệu đầy đủ không vào state.
    """
    tables = await read_source_tables(
        shop_dir,
        first=config.sample_first_rows,
        random_count=config.sample_random_rows,
        seed=config.sample_seed,
        only_sheets=True,
    )
    dumped = [table.model_dump() for table, _frame in tables]
    return Event(output=dumped, state={"raw_tables": dumped})

  return scan_sheets


def make_scan_sqlite(config: IngestConfig) -> Callable:
  """Tạo node scan_sqlite đã gắn cấu hình mẫu.

  Args:
    config: Cấu hình ingest (số dòng mẫu, seed).

  Returns:
    Hàm node scan_sqlite dùng được trực tiếp trong edges.
  """

  async def scan_sqlite(
      business_id: str = "",
      shop_dir: str = "",
  ) -> Event:
    """Quét file SQLite của shop thành metadata bảng + mẫu.

    Bảng SQLite không qua detect header: tên cột lấy thẳng từ schema DB
    nên không bao giờ mơ hồ vì header (ambiguous luôn False).

    Args:
      business_id: Id shop (lấy từ state).
      shop_dir: Thư mục dữ liệu của shop (lấy từ state).

    Returns:
      Một Event duy nhất mang output là danh sách RawTable đã dump và
      state {"raw_tables": danh sách đó}.
    """
    tables = await read_source_tables(
        shop_dir,
        first=config.sample_first_rows,
        random_count=config.sample_random_rows,
        seed=config.sample_seed,
        only_sqlite=True,
    )
    dumped = [table.model_dump() for table, _frame in tables]
    return Event(output=dumped, state={"raw_tables": dumped})

  return scan_sqlite


def _candidate_headers(raw_tables: list[dict]) -> dict[str, list[str]]:
  """Lấy header ứng viên cho mọi bảng từ metadata quét.

  Args:
    raw_tables: Danh sách RawTable đã dump từ state.

  Returns:
    Dict table_id -> header ứng viên.
  """
  return {table["table_id"]: table["header"] for table in raw_tables}


def header_gate(raw_tables: list[dict] | None = None) -> Event:
  """Cổng vào loop header: quyết định có cần HeaderAgent hay không.

  Gate này luôn ghi headers_final mặc định (header ứng viên từ quét) vào
  state ở cả hai nhánh, để node làm sạch về sau không bao giờ thiếu key
  này kể cả khi loop header bị bỏ qua. Validator/route về sau sẽ ghi đè
  headers_final bằng đề xuất đã kiểm chứng khi loop chạy.

  Args:
    raw_tables: Danh sách RawTable đã dump (lấy từ state).

  Returns:
    Một Event duy nhất mang output là JSON chuỗi các bảng mơ hồ, state
    (headers_final mặc định và header_summary_text là đúng chuỗi mơ hồ
    đó để HeaderAgent đọc qua placeholder trong instruction — tin nhắn
    đầu vào ở lượt thử lại là bản dump Verdict, không phải danh sách
    mơ hồ, nên agent không được trông chờ vào tin nhắn đầu vào), và
    route "agent" (có bảng mơ hồ) hoặc "skip" (không có bảng mơ hồ).
  """
  tables = raw_tables or []
  ambiguous = [table for table in tables if table.get("ambiguous")]
  output = json.dumps({"ambiguous_tables": ambiguous}, ensure_ascii=False)
  route = "agent" if ambiguous else "skip"
  return Event(
      output=output,
      state={
          "headers_final": _candidate_headers(tables),
          "header_summary_text": output,
      },
      route=route,
  )


def deterministic_merge_groups(
    raw_tables: list[dict] | None = None,
    input_kind: str = "default",
) -> list[dict]:
  """Nhóm gộp khóa bởi code: cùng chữ ký cột chuẩn hóa thì gộp.

  Đây là bài học từ biz_102 (2026-10-31): 18 sheet tháng cùng header
  nhưng MergeAgent gộp cặp theo số tháng (T01_2025 với T01_2026) thay
  vì gộp cả 18 sheet thành một bảng, mà validator chỉ kiểm hình thức
  nên không bắt được. Quy tắc này của người dùng nói rõ: cùng header
  sau khi chuẩn hóa thì gộp — code quyết luôn, không tốn một lần gọi
  model và không còn cửa cho model đoán sai.

  Quy tắc khóa, theo thứ tự an toàn:
  - Chỉ xét bảng có chữ ký cột khác rỗng (header chưa xác định thì
    không bao giờ khóa, để LLM + validator xử lý).
  - So sánh chữ ký theo TẬP cột đã chuẩn hóa (sắp xếp lại), không kể
    thứ tự cột trong file: hai mảnh của cùng một bảng có thể ghi cột
    theo thứ tự khác nhau, bước làm sạch đã căn lại theo tên chuẩn.
  - Shop SQLite (input_kind == "sqlite") không khóa bảng nào: mỗi bảng
    trong DB là một thực thể riêng theo thiết kế từ phase1 (đơn, chi
    tiết, khách...), không gộp chỉ vì trùng header.
  - Nhóm nào chứa cả bảng và biến thể lô của nó (ChiTiet/ChiTietLo)
    thì không khóa: gộp hai biến thể này sẽ đếm trùng dòng chi tiết,
    để LLM + validator giữ riêng như trước.

  Args:
    raw_tables: Danh sách RawTable đã dump (lấy từ state).
    input_kind: Loại nguồn ("sqlite" hoặc "default", lấy từ state).

  Returns:
    Danh sách nhóm đã khóa, mỗi nhóm là dict {"tables": [...],
    "evidence": ...}; chỉ gồm nhóm từ 2 bảng trở lên. Bảng không nằm
    trong nhóm khóa nào được gọi là bảng chưa khóa và do MergeAgent
    quyết tiếp.
  """
  if input_kind == "sqlite":
    return []
  by_key: dict[tuple[str, ...], list[str]] = {}
  key_order: list[tuple[str, ...]] = []
  for table in raw_tables or []:
    signature = table.get("column_signature", "") or ""
    key = tuple(sorted(part for part in signature.split("|") if part))
    if not key:
      continue
    if key not in by_key:
      by_key[key] = []
      key_order.append(key)
    by_key[key].append(table["table_id"])
  locked: list[dict] = []
  for key in key_order:
    table_ids = by_key[key]
    if len(table_ids) < 2:
      continue
    # Cấm biến thể lô: nếu hai id trong nhóm chỉ khác nhau ở hậu tố
    # lô thì nhóm này không khóa (an toàn hơn gộp sai gây đếm trùng).
    bases = [_lot_variant_name(table_id) for table_id in table_ids]
    if len(set(bases)) != len(table_ids):
      continue
    locked.append(
        {
            "tables": table_ids,
            "evidence": "gộp khóa bởi code: cùng chữ ký cột chuẩn hóa",
        }
    )
  return locked


def merge_gate(
    raw_tables: list[dict] | None = None,
    input_kind: str = "default",
) -> Event:
  """Cổng vào loop gộp: khóa nhóm rõ ràng bằng code, còn lại cho agent.

  Gate này luôn ghi merge_groups mặc định vào state ở cả hai nhánh, để
  node làm sạch không bao giờ thiếu key này kể cả khi loop gộp bị bỏ
  qua. Khác với trước đây (mặc định là mỗi bảng giữ riêng), mặc định
  mới gồm nhóm khóa bởi code (xem deterministic_merge_groups) cộng với
  mỗi bảng chưa khóa giữ riêng.

  MergeAgent chỉ nhận tóm tắt các bảng CHƯA khóa (không nhận nhóm đã
  khóa, vì nhóm đó đã chốt và không được quyết lại). Vì vậy route
  "agent" chỉ phát khi còn hơn 1 bảng chưa khóa để quyết; khi mọi bảng
  đã khóa hoặc chỉ còn 0-1 bảng chưa khóa thì route "skip" thẳng tới
  làm sạch với nhóm mặc định.

  Args:
    raw_tables: Danh sách RawTable đã dump (lấy từ state).
    input_kind: Loại nguồn ("sqlite" hoặc "default", lấy từ state).

  Returns:
    Một Event duy nhất mang output là JSON chuỗi tóm tắt các bảng chưa
    khóa (id, chữ ký cột, số dòng), state (merge_groups mặc định,
    merge_locked_groups để validator/fallback gộp tiếp, và
    merge_summary_text là đúng chuỗi tóm tắt đó để MergeAgent đọc qua
    placeholder trong instruction — xem lý do ở instruction của agent:
    tin nhắn đầu vào ở lượt thử lại là bản dump Verdict, không phải
    tóm tắt, nên agent không được trông chờ vào tin nhắn đầu vào),
    và route "agent" (hơn 1 bảng chưa khóa) hoặc "skip".
  """
  tables = raw_tables or []
  locked = deterministic_merge_groups(tables, input_kind)
  locked_ids = {table_id for group in locked for table_id in group["tables"]}
  unlocked = [table for table in tables if table["table_id"] not in locked_ids]
  summaries = [
      {
          "table_id": table["table_id"],
          "column_signature": table["column_signature"],
          "row_count": table["row_count"],
      }
      for table in unlocked
  ]
  default_groups = {
      "groups": locked
      + [
          {"tables": [table["table_id"]], "evidence": "giữ riêng mặc định"}
          for table in unlocked
      ],
      "singles": [],
  }
  output = json.dumps({"tables": summaries}, ensure_ascii=False)
  route = "agent" if len(unlocked) > 1 else "skip"
  return Event(
      output=output,
      state={
          "merge_groups": default_groups,
          "merge_locked_groups": locked,
          "merge_summary_text": output,
      },
      route=route,
  )
