# -*- coding: utf-8 -*-
"""Test node làm sạch: gộp theo tên chuẩn, chuyển kiểu, dedup, staging."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pandas as pd

from ingest._config import IngestConfig
from ingest.nodes._clean_nodes import _canonical_frame, _clean_frame, make_clean_tables

CONFIG = IngestConfig()


def _write_csv(path: Path, rows: list[list[str]]) -> None:
  """Ghi file CSV giả lập."""
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open("w", encoding="utf-8", newline="") as handle:
    csv.writer(handle).writerows(rows)


def test_canonical_frame_renames_by_normalized_name() -> None:
  """_canonical_frame đổi tên cột theo tên chuẩn hóa rồi nối dọc."""
  first = pd.DataFrame({"Mã đơn": ["DH1"], "Tổng tiền": ["100000"]})
  second = pd.DataFrame({"Ma don": ["DH2"], "Tong tien": ["200000"]})
  combined = _canonical_frame([first, second], ["Mã đơn", "Tổng tiền"])
  assert list(combined.columns) == ["Mã đơn", "Tổng tiền"]
  assert len(combined) == 2
  assert combined.iloc[1]["Mã đơn"] == "DH2"


def test_clean_frame_converts_and_dedups() -> None:
  """_clean_frame chuyển tiền/ngày/số lượng, dedup dòng giống hệt."""
  frame = pd.DataFrame(
      {
          "Mã đơn hàng": ["DH001", "DH002", "DH001"],
          "Ngày bán": ["01/01/2025", "02/01/2025", "01/01/2025"],
          "Tổng tiền": ["100.000", "200.000", "100.000"],
          "Số lượng": ["2", "3", "2"],
          "Tên khách": ["An", "Bình", "An"],
      }
  )
  cleaned, manifest, stats = _clean_frame(frame)
  assert stats.rows_in == 3
  assert stats.rows_out == 2
  assert stats.unparseable_cells == 0
  money = [col for col in manifest if col["semantic"] == "line_amount"][0]
  assert money["type"] == "INTEGER"
  assert cleaned.iloc[0]["Tổng tiền"] == 100000.0
  assert cleaned.iloc[0]["Ngày bán"] == "2025-01-01"


def test_clean_frame_keeps_identical_rows_without_order_id() -> None:
  """Bảng không có mã đơn giữ nguyên các dòng giống hệt nhau."""
  frame = pd.DataFrame(
      {
          "Tên hàng": ["Coca Cola 330ml", "Coca Cola 330ml"],
          "Ngày bán": ["01/01/2025", "01/01/2025"],
          "Tổng tiền": ["10000", "10000"],
          "Số lượng": ["1", "1"],
      }
  )
  cleaned, _manifest, stats = _clean_frame(frame)
  assert stats.rows_in == 2
  assert stats.rows_out == 2


def test_clean_frame_uses_sheet_year_only_for_yearless_dates() -> None:
  """Ngày không năm lấy năm của dòng, ngày có năm không bị ghi đè."""
  frame = pd.DataFrame(
      {
          "Mã đơn hàng": ["DH1", "DH2"],
          "Ngày lập": ["1/1", "01/02/2026"],
      }
  )
  cleaned, _manifest, stats = _clean_frame(frame, sheet_years=[2025, 2025])
  assert cleaned.iloc[0]["Ngày lập"] == "2025-01-01"
  assert cleaned.iloc[1]["Ngày lập"] == "2026-02-01"
  assert stats.unparseable_cells == 0


def test_clean_frame_yearless_date_without_sheet_year_stays_null() -> None:
  """Không biết năm thì ngày không năm vẫn thành NULL như cũ."""
  frame = pd.DataFrame(
      {
          "Mã đơn hàng": ["DH1"],
          "Ngày lập": ["1/1"],
      }
  )
  cleaned, _manifest, stats = _clean_frame(frame)
  assert cleaned.iloc[0]["Ngày lập"] is None or pd.isna(cleaned.iloc[0]["Ngày lập"])
  assert stats.unparseable_cells == 1


def test_clean_frame_maps_vietnamese_money_variants() -> None:
  """Từ điển bổ sung map 'Tiền' và 'Tổng sau giảm giá' thành tiền."""
  frame = pd.DataFrame(
      {
          "Mã đơn hàng": ["DH1", "DH2"],
          "Tiền": ["300.000đ", "55.000đ"],
          "Tổng sau giảm giá": ["307,000", "90,000"],
      }
  )
  cleaned, manifest, _stats = _clean_frame(frame)
  by_name = {col["original_name"]: col for col in manifest}
  assert by_name["Tiền"]["semantic"] == "line_amount"
  assert by_name["Tổng sau giảm giá"]["semantic"] == "order_total"
  assert cleaned.iloc[0]["Tiền"] == 300000.0
  assert cleaned.iloc[0]["Tổng sau giảm giá"] == 307000.0


def test_clean_frame_flags_suspected_money_text_column() -> None:
  """Cột TEXT toàn tiền bẩn được gắn cờ, STT/số điện thoại thì không."""
  frame = pd.DataFrame(
      {
          "Cột lạ": ["307,000", "90,000", "55.000đ", "1tr440"],
          "STT": ["1", "2", "3", "4"],
          "Số máy": ["0901234567", "0912345678", "0923456789", "0934567890"],
      }
  )
  _cleaned, manifest, _stats = _clean_frame(frame)
  by_name = {col["original_name"]: col for col in manifest}
  assert by_name["Cột lạ"]["suspected_money"] is True
  assert by_name["STT"]["suspected_money"] is False
  assert by_name["Số máy"]["suspected_money"] is False


def test_clean_frame_unparseable_becomes_null_and_counted() -> None:
  """Giá trị tiền không parse được thành None và được đếm."""
  frame = pd.DataFrame(
      {
          "Mã đơn hàng": ["DH1", "DH2"],
          "Tổng tiền": ["100000", "không phải tiền"],
      }
  )
  cleaned, _manifest, stats = _clean_frame(frame)
  assert stats.unparseable_cells == 1
  assert cleaned.iloc[1]["Tổng tiền"] is None or pd.isna(cleaned.iloc[1]["Tổng tiền"])


async def test_clean_tables_uses_year_from_file_name(tmp_path: Path) -> None:
  """clean_tables mang năm từ tên file tới dòng ngày không năm."""
  shop_dir = tmp_path / "biz_year"
  _write_csv(
      shop_dir / "sobanhang_2025.csv",
      [["Ngày lập", "Tên hàng"], ["1/1", "A"], ["2/1", "B"]],
  )
  merge_groups = {
      "groups": [{"tables": ["sobanhang_2025"], "evidence": "giữ riêng"}],
      "singles": [],
  }
  headers_final = {"sobanhang_2025": ["Ngày lập", "Tên hàng"]}
  node = make_clean_tables(CONFIG)
  event = await node(
      merge_groups=merge_groups,
      headers_final=headers_final,
      shop_dir=str(shop_dir),
      output_dir=str(tmp_path),
      business_id="biz_year",
      input_kind="default",
  )
  staging_dir = Path(event.actions.state_delta["staging_dir"])
  manifests = json.loads((staging_dir / "manifest.json").read_text(encoding="utf-8"))
  staging_file = staging_dir / f"{manifests[0]['sqlite_table']}.csv"
  with staging_file.open(encoding="utf-8", newline="") as handle:
    rows = list(csv.reader(handle))
  assert rows[1][0] == "2025-01-01"
  assert rows[2][0] == "2025-01-02"


async def test_clean_tables_uses_final_header_row(tmp_path: Path) -> None:
  """clean_tables dùng dòng header chốt thay vì detect lại."""
  shop_dir = tmp_path / "biz_header"
  _write_csv(
      shop_dir / "bang.csv",
      [
          ["Mã", "Tên", "SL", "Tiền"],
          ["Mã đơn", "Tên hàng", "Số lượng", "Tổng tiền"],
          ["DH1", "Coca", "2", "10000"],
          ["DH2", "Pepsi", "3", "20000"],
      ],
  )
  merge_groups = {
      "groups": [{"tables": ["bang"], "evidence": "giữ riêng"}],
      "singles": [],
  }
  headers_final = {"bang": ["Mã đơn", "Tên hàng", "Số lượng", "Tổng tiền"]}
  header_rows_final = {"bang": 1}
  node = make_clean_tables(CONFIG)
  event = await node(
      merge_groups=merge_groups,
      headers_final=headers_final,
      header_rows_final=header_rows_final,
      shop_dir=str(shop_dir),
      output_dir=str(tmp_path),
      business_id="biz_header",
      input_kind="default",
  )
  staging_dir = Path(event.actions.state_delta["staging_dir"])
  manifests = json.loads((staging_dir / "manifest.json").read_text(encoding="utf-8"))
  staging_file = staging_dir / f"{manifests[0]['sqlite_table']}.csv"
  with staging_file.open(encoding="utf-8", newline="") as handle:
    rows = list(csv.reader(handle))
  assert rows[0] == ["Mã đơn", "Tên hàng", "Số lượng", "Tổng tiền"]
  assert rows[1][0] == "DH1"
  assert rows[1][3] == "10000.0"


async def test_clean_tables_writes_staging_and_manifest(tmp_path: Path) -> None:
  """clean_tables ghi CSV staging + manifest cho node ghi SQLite."""
  shop_dir = tmp_path / "biz_csv"
  _write_csv(
      shop_dir / "don.csv",
      [["Mã đơn hàng", "Tổng tiền"], ["DH1", "100000"], ["DH2", "200000"]],
  )
  _write_csv(
      shop_dir / "khach.csv",
      [["Tên khách"], ["An"], ["Bình"]],
  )
  merge_groups = {
      "groups": [
          {"tables": ["don"], "evidence": "giữ riêng"},
          {"tables": ["khach"], "evidence": "giữ riêng"},
      ],
      "singles": [],
  }
  headers_final = {"don": ["Mã đơn hàng", "Tổng tiền"], "khach": ["Tên khách"]}
  node = make_clean_tables(CONFIG)
  event = await node(
      merge_groups=merge_groups,
      headers_final=headers_final,
      shop_dir=str(shop_dir),
      output_dir=str(tmp_path),
      business_id="biz_csv",
      input_kind="default",
  )
  staging_dir = Path(event.actions.state_delta["staging_dir"])
  assert staging_dir.is_dir()
  assert (staging_dir / "manifest.json").is_file()
  manifests = json.loads((staging_dir / "manifest.json").read_text(encoding="utf-8"))
  assert len(manifests) == 2
  assert event.actions.state_delta["clean_stats"]["rows_out"] == 4
  for manifest in manifests:
    assert (staging_dir / f"{manifest['sqlite_table']}.csv").is_file()
