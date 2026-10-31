# -*- coding: utf-8 -*-
"""Node làm sạch dữ liệu: gộp bảng, map cột theo ngữ nghĩa, chuyển kiểu.

Module này thuộc tầng làm sạch. Node ở đây là code thuần, không gọi LLM:
LLM chỉ đề xuất header/gộp/schema, còn mọi dòng dữ liệu đều do code xử
lý (pandas + normalizers của phase1). DataFrame không đi qua state:
node này ghi bảng đã sạch ra staging (CSV + manifest JSON) và chỉ đưa
đường dẫn staging + manifest + thống kê vào state cho node ghi SQLite.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pandas as pd
from google.adk import Event

from .._config import IngestConfig
from .._models import (
    CleanStats,
    ColumnManifest,
    TableManifest,
)
from .._phase1 import (
    map_columns,
    normalize_key,
    parse_date,
    parse_money,
    parse_quantity,
)
from .._readers import read_source_tables, sanitize_sqlite_name

# Ngữ nghĩa cột tiền theo map_columns của phase1: các cột này được
# chuyển bằng parse_money (trả None khi không parse được, không gán 0).
MONEY_SEMANTICS = {
    "line_amount",
    "order_total",
    "amount_paid",
    "amount_due",
    "unit_price",
    "cost_price",
    "discount",
}
# Ngữ nghĩa cột ngày: chuyển bằng parse_date thành TEXT ISO.
DATE_SEMANTICS = {
    "order_date",
    "order_datetime",
    "expiry_date",
    "import_date",
    "delivery_date",
    "payment_date",
}
# Ngữ nghĩa cột số lượng: chuyển bằng parse_quantity, chỉ lấy phần số
# (đơn vị gốc được bỏ ở khung lần này; quy đổi về đơn vị cơ bản cần
# bảng quy đổi và là bước nâng cấp sau, không đoán hệ số).
QUANTITY_SEMANTICS = {
    "quantity",
    "quantity_converted",
    "return_quantity",
    "stock_quantity",
}


def _canonical_frame(
    frames: list[pd.DataFrame], canonical: list[str]
) -> pd.DataFrame:
  """Gộp các DataFrame của một nhóm về cùng tên cột chuẩn rồi nối dọc.

  Các bảng trong nhóm đã có cùng chữ ký cột (validator đã kiểm), nhưng
  tên cột gốc có thể khác nhau ở dấu/case. Hàm này đổi tên cột của từng
  bảng theo tên chuẩn hóa về tên chuẩn, sắp xếp đúng thứ tự chuẩn, rồi
  nối dọc. Cột nào không map được tên chuẩn thì giữ nguyên tên (trường
  hợp này không nên xảy ra sau validator, nhưng nối dọc vẫn an toàn hơn
  là crash).

  Args:
    frames: Danh sách DataFrame của các bảng trong nhóm.
    canonical: Tên cột chuẩn (header của bảng đầu nhóm).

  Returns:
    DataFrame đã gộp, cột theo thứ tự chuẩn.
  """
  canonical_norm = [normalize_key(col) for col in canonical]
  aligned: list[pd.DataFrame] = []
  for frame in frames:
    rename: dict[str, str] = {}
    for col in frame.columns:
      norm = normalize_key(str(col))
      if norm in canonical_norm:
        rename[str(col)] = canonical[canonical_norm.index(norm)]
    renamed = frame.rename(columns=rename)
    for col in canonical:
      if col not in renamed.columns:
        renamed[col] = None
    aligned.append(renamed[canonical])
  if not aligned:
    return pd.DataFrame(columns=canonical)
  combined = pd.concat(aligned, ignore_index=True)
  return combined


def _clean_frame(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict, CleanStats]:
  """Làm sạch một DataFrame đã gộp: map ngữ nghĩa, chuyển kiểu, dedup.

  Quy tắc (xem skill datamate-cleaning):
  - Cột map được ngữ nghĩa tiền/ngày/số lượng thì chuyển kiểu bằng
    parse_* của phase1; giá trị không parse được thành None và được đếm
    vào unparseable_cells (không bịa, không gán 0).
  - Cột còn lại: strip khoảng trắng, chuỗi rỗng thành None.
  - Dedup dòng giống hệt nhau sau khi làm sạch (dòng trùng do gộp nhiều
    sheet gây ra).
  - Không forward-fill ở khung lần này: forward-fill chỉ đúng cho cột
    mã/nhóm theo khối đơn hàng, áp bừa cho mọi cột sẽ làm sai dữ liệu;
    bước này để lần nâng cấp có phân tích khối.

  Args:
    frame: DataFrame đã gộp (giá trị chuỗi).

  Returns:
    Tuple (DataFrame đã sạch, manifest cột dạng dict, thống kê làm sạch).
  """
  rows_in = len(frame)
  mapping = {}
  try:
    # map_columns đã kiểm chứng bằng mẫu giá trị nên cột map được là
    # cột có ý nghĩa thật, không phải đoán theo tên; lỗi ở đây (ví dụ
    # DataFrame rỗng) thì coi như không map được cột nào, không crash.
    mapping = map_columns(frame) if not frame.empty else {}
  except Exception:
    mapping = {}
  cleaned = frame.copy()
  unparseable = 0
  columns_manifest: list[dict] = []
  for col in cleaned.columns:
    semantic = ""
    for key, value in mapping.items():
      if value == col:
        semantic = key
        break
    values = cleaned[col].tolist()
    converted: list[object] = []
    col_type = "TEXT"
    if semantic in MONEY_SEMANTICS:
      col_type = "INTEGER"
      for value in values:
        if value is None or (isinstance(value, str) and not value.strip()):
          converted.append(None)
          continue
        parsed = parse_money(value)
        if parsed is None:
          unparseable += 1
          converted.append(None)
        else:
          # Tiền VND thường là số nguyên; nếu có giá trị lẻ thì cột
          # phải là REAL, không được làm tròn lặng lẽ mất tiền.
          if float(parsed) != int(parsed):
            col_type = "REAL"
          converted.append(parsed)
    elif semantic in DATE_SEMANTICS:
      col_type = "TEXT"
      for value in values:
        if value is None or (isinstance(value, str) and not value.strip()):
          converted.append(None)
          continue
        parsed = parse_date(value)
        if parsed is None:
          unparseable += 1
          converted.append(None)
        else:
          converted.append(parsed.isoformat())
    elif semantic in QUANTITY_SEMANTICS:
      col_type = "REAL"
      for value in values:
        if value is None or (isinstance(value, str) and not value.strip()):
          converted.append(None)
          continue
        number, _unit = parse_quantity(value)
        if number is None:
          unparseable += 1
          converted.append(None)
        else:
          converted.append(number)
    else:
      for value in values:
        if value is None:
          converted.append(None)
        elif isinstance(value, str):
          stripped = value.strip()
          converted.append(stripped if stripped else None)
        else:
          converted.append(value)
    cleaned[col] = converted
    columns_manifest.append(
        {
            "original_name": str(col),
            "sqlite_name": sanitize_sqlite_name(str(col), "cot"),
            "type": col_type,
            "semantic": semantic,
        }
    )
  before_dedup = len(cleaned)
  cleaned = cleaned.drop_duplicates(ignore_index=True)
  rows_out = len(cleaned)
  null_cells = int(cleaned.isna().sum().sum()) if not cleaned.empty else 0
  stats = CleanStats(
      rows_in=rows_in,
      rows_out=rows_out,
      null_cells=null_cells,
      unparseable_cells=unparseable,
  )
  _ = before_dedup
  return cleaned, columns_manifest, stats


def make_clean_tables(config: IngestConfig) -> Callable:
  """Tạo node clean_tables đã gắn cấu hình.

  Args:
    config: Cấu hình ingest (số dòng mẫu, seed cho lần đọc lại nguồn).

  Returns:
    Hàm node clean_tables dùng được trực tiếp trong edges.
  """

  async def clean_tables(
      merge_groups: dict | None = None,
      headers_final: dict | None = None,
      shop_dir: str = "",
      output_dir: str = "",
      business_id: str = "",
      input_kind: str = "default",
  ) -> Event:
    """Làm sạch toàn bộ bảng đã gộp và ghi staging cho node ghi SQLite.

    Node này đọc lại file nguồn (vì DataFrame không truyền qua state),
    gộp theo merge_groups, làm sạch từng bảng, ghi mỗi bảng thành một
    file CSV staging và một manifest JSON mô tả bảng/cột/kiểu/thống kê.

    Args:
      merge_groups: Đề xuất gộp đã chốt (lấy từ state).
      headers_final: Header chốt cho từng bảng (lấy từ state).
      shop_dir: Thư mục dữ liệu của shop (lấy từ state).
      output_dir: Thư mục ghi kết quả (lấy từ state).
      business_id: Id shop (lấy từ state).
      input_kind: Loại nguồn ("sqlite" hoặc "default", lấy từ state) để
        đọc lại đúng nhánh nguồn như node quét.

    Returns:
      Một Event duy nhất mang output là manifest (danh sách bảng) và
      state (staging_dir, clean_stats tổng, manifest).
    """
    groups_data = merge_groups or {"groups": [], "singles": []}
    headers = headers_final or {}
    only_sqlite = input_kind == "sqlite"
    only_sheets = not only_sqlite
    tables = await read_source_tables(
        shop_dir,
        first=config.sample_first_rows,
        random_count=config.sample_random_rows,
        seed=config.sample_seed,
        only_sqlite=only_sqlite,
        only_sheets=only_sheets,
    )
    by_id = {table.table_id: frame for table, frame in tables}
    staging_dir = Path(output_dir) / "staging" / business_id

    def _run() -> tuple[list[dict], dict]:
      """Phần đồng bộ ghi staging (chạy trong thread riêng vì là I/O)."""
      staging_dir.mkdir(parents=True, exist_ok=True)
      manifests: list[dict] = []
      total_stats = CleanStats()
      used_sqlite_names: set[str] = set()
      groups = list(groups_data.get("groups", []))
      grouped_ids: set[str] = set()
      for group in groups:
        for table_id in group.get("tables", []):
          grouped_ids.add(table_id)
      for single in groups_data.get("singles", []):
        if single not in grouped_ids:
          groups.append({"tables": [single], "evidence": "giữ riêng"})
          grouped_ids.add(single)
      for table_id in sorted(by_id):
        if table_id not in grouped_ids:
          # Bảng quét được nhưng đề xuất gộp không nhắc tới (không nên
          # xảy ra sau validator, vì validator bắt sót bảng): giữ riêng
          # thay vì làm mất dữ liệu lặng lẽ, và ghi chú vào manifest.
          groups.append({"tables": [table_id], "evidence": "giữ riêng (sót đề xuất)"})
      for group in groups:
        member_ids = [tid for tid in group.get("tables", []) if tid in by_id]
        if not member_ids:
          continue
        first_id = member_ids[0]
        canonical = headers.get(first_id) or list(by_id[first_id].columns)
        frames = [by_id[tid] for tid in member_ids]
        combined = _canonical_frame(frames, [str(col) for col in canonical])
        cleaned, columns_manifest, stats = _clean_frame(combined)
        sqlite_table = sanitize_sqlite_name(first_id, "bang")
        base_name = sqlite_table
        suffix = 2
        while sqlite_table in used_sqlite_names:
          sqlite_table = f"{base_name}_{suffix}"
          suffix += 1
        used_sqlite_names.add(sqlite_table)
        for col in columns_manifest:
          col["sqlite_name"] = sanitize_sqlite_name(col["original_name"], "cot")
        # Khử trùng tên cột SQL trong bảng (hai tên gốc khác nhau có thể
        # sanitize thành cùng một tên): thêm hậu tố số cho lần sau.
        seen_sqlite: dict[str, int] = {}
        for col in columns_manifest:
          name = col["sqlite_name"]
          if name not in seen_sqlite:
            seen_sqlite[name] = 1
          else:
            seen_sqlite[name] += 1
            col["sqlite_name"] = f"{name}_{seen_sqlite[name]}"
        staging_file = staging_dir / f"{sqlite_table}.csv"
        cleaned.to_csv(staging_file, index=False, encoding="utf-8")
        manifest = TableManifest(
            table_id=first_id,
            source_tables=member_ids,
            sqlite_table=sqlite_table,
            columns=[ColumnManifest(**col) for col in columns_manifest],
            row_count=len(cleaned),
            stats=stats,
        )
        manifests.append(manifest.model_dump())
        total_stats.rows_in += stats.rows_in
        total_stats.rows_out += stats.rows_out
        total_stats.null_cells += stats.null_cells
        total_stats.unparseable_cells += stats.unparseable_cells
      manifest_path = staging_dir / "manifest.json"
      manifest_path.write_text(
          json.dumps(manifests, ensure_ascii=False, indent=2) + "\n",
          encoding="utf-8",
      )
      return manifests, total_stats.model_dump()

    manifests, total_stats = await asyncio.to_thread(_run)
    return Event(
        output=manifests,
        state={
            "staging_dir": str(staging_dir),
            "clean_stats": total_stats,
            "manifest": manifests,
        },
    )

  return clean_tables
