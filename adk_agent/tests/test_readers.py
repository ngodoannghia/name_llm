# -*- coding: utf-8 -*-
"""Test tầng đọc nguồn: trích năm từ tên sheet/file, cờ ambiguous sát điểm."""

from __future__ import annotations

from ingest._readers import _extract_sheet_year, _table_from_rows


def test_extract_sheet_year_prefers_sheet_name() -> None:
  """Năm trong tên sheet thắng năm trong tên file."""
  assert _extract_sheet_year("Thang 01-2025", "SoBanHang.xlsx") == 2025
  assert _extract_sheet_year("T01_2026", "DonHang_2025.xlsx") == 2026


def test_extract_sheet_year_falls_back_to_file_name() -> None:
  """Sheet không có năm thì lấy năm trong tên file."""
  assert _extract_sheet_year("", "SoBanHang_2025.csv") == 2025
  assert _extract_sheet_year("Sheet1", "BaoCao_2026.xlsx") == 2026


def test_extract_sheet_year_first_year_wins() -> None:
  """Tên có nhiều năm thì lấy năm đầu tiên (quy tắc đã ghi rõ)."""
  assert _extract_sheet_year("BaoCao_2024_2025", "File.xlsx") == 2024


def test_extract_sheet_year_none_when_no_year() -> None:
  """Không có năm ở đâu thì trả None, không đoán năm."""
  assert _extract_sheet_year("Sheet1", "DonHang_1.xlsx") is None
  assert _extract_sheet_year("", "khach.csv") is None


def test_table_from_rows_close_call_marks_ambiguous() -> None:
  """Hai dòng đầu hòa điểm thì bảng mơ hồ để agent xem lại."""
  rows = [
      ["Mã", "Tên", "SL", "Tiền"],
      ["Mã đơn", "Tên hàng", "Số lượng", "Tổng tiền"],
      ["DH1", "Coca", "2", "10000"],
  ]
  metadata, _frame = _table_from_rows(
      source_file="bang.csv",
      source_sheet="",
      header_row=0,
      rows=rows,
      taken=set(),
      first=10,
      random_count=10,
      seed=42,
  )
  assert metadata.ambiguous is True
  assert metadata.candidate_rows == rows


def test_table_from_rows_clear_winner_not_ambiguous() -> None:
  """Dòng đầu thắng rõ thì bảng không mơ hồ vì sát điểm."""
  rows = [
      ["Mã đơn", "Tên hàng", "Số lượng", "Tổng tiền"],
      ["DH1", "Coca", "2", "10000"],
  ]
  metadata, _frame = _table_from_rows(
      source_file="bang.csv",
      source_sheet="",
      header_row=0,
      rows=rows,
      taken=set(),
      first=10,
      random_count=10,
      seed=42,
  )
  assert metadata.ambiguous is False
