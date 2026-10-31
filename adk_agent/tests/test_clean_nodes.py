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
