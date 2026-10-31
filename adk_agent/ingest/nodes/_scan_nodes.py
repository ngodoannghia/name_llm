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
    Một Event duy nhất mang output là JSON chuỗi các bảng mơ hồ và route
    "agent" (có bảng mơ hồ) hoặc "skip" (không có bảng mơ hồ).
  """
  tables = raw_tables or []
  ambiguous = [table for table in tables if table.get("ambiguous")]
  output = json.dumps({"ambiguous_tables": ambiguous}, ensure_ascii=False)
  route = "agent" if ambiguous else "skip"
  return Event(
      output=output,
      state={"headers_final": _candidate_headers(tables)},
      route=route,
  )


def merge_gate(raw_tables: list[dict] | None = None) -> Event:
  """Cổng vào loop gộp: quyết định có cần MergeAgent hay không.

  Tương tự header_gate, gate này luôn ghi merge_groups mặc định (mỗi bảng
  giữ riêng) vào state ở cả hai nhánh, để node làm sạch không bao giờ
  thiếu key này kể cả khi loop gộp bị bỏ qua (shop chỉ có 1 bảng).

  Args:
    raw_tables: Danh sách RawTable đã dump (lấy từ state).

  Returns:
    Một Event duy nhất mang output là JSON chuỗi tóm tắt bảng (id, chữ ký
    cột, số dòng) và route "agent" (nhiều hơn 1 bảng) hoặc "skip" (0-1 bảng).
  """
  tables = raw_tables or []
  summaries = [
      {
          "table_id": table["table_id"],
          "column_signature": table["column_signature"],
          "row_count": table["row_count"],
      }
      for table in tables
  ]
  default_groups = {
      "groups": [
          {"tables": [table["table_id"]], "evidence": "giữ riêng mặc định"}
          for table in tables
      ],
      "singles": [],
  }
  output = json.dumps({"tables": summaries}, ensure_ascii=False)
  route = "agent" if len(tables) > 1 else "skip"
  return Event(output=output, state={"merge_groups": default_groups}, route=route)
