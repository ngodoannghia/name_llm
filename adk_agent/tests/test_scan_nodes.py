# -*- coding: utf-8 -*-
"""Test các node quét: init_shop, branch_input, scan, header_gate, merge_gate.

Test dùng thư mục shop giả lập trong tmp_path (không đụng dữ liệu train)
để kiểm chứng logic node độc lập với dữ liệu thật.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from ingest._config import IngestConfig
from ingest.nodes._scan_nodes import (
    branch_input,
    header_gate,
    make_init_shop,
    make_scan_sheets,
    make_scan_sqlite,
    merge_gate,
)

CONFIG = IngestConfig()


def _write_csv(path: Path, rows: list[list[str]]) -> None:
  """Ghi file CSV giả lập (header + dòng dữ liệu, không README)."""
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open("w", encoding="utf-8", newline="") as handle:
    writer = csv.writer(handle)
    writer.writerows(rows)


async def test_init_shop_default_branch(tmp_path: Path) -> None:
  """init_shop đọc README, liệt kê file và chọn nhánh default khi không có SQLite."""
  shop_dir = tmp_path / "biz_test"
  shop_dir.mkdir()
  (shop_dir / "README.md").write_text("Shop thử nghiệm.\n", encoding="utf-8")
  _write_csv(shop_dir / "don.csv", [["Mã đơn", "Tổng tiền"], ["DH1", "100000"]])
  node = make_init_shop(CONFIG)
  event = await node(business_id="biz_test", shop_dir=str(shop_dir), output_dir=str(tmp_path))
  assert event.output == {"business_id": "biz_test", "input_kind": "default"}
  assert event.actions.state_delta["input_files"] == ["don.csv"]
  assert "Shop thử nghiệm" in event.actions.state_delta["readme_text"]


async def test_init_shop_sqlite_branch(tmp_path: Path) -> None:
  """init_shop chọn nhánh sqlite khi shop có file SQLite."""
  shop_dir = tmp_path / "biz_sql"
  shop_dir.mkdir()
  connection = sqlite3.connect(str(shop_dir / "pos.sqlite"))
  connection.execute("CREATE TABLE don (ma TEXT, tien INTEGER)")
  connection.execute("INSERT INTO don VALUES ('DH1', 100000)")
  connection.commit()
  connection.close()
  node = make_init_shop(CONFIG)
  event = await node(business_id="biz_sql", shop_dir=str(shop_dir), output_dir=str(tmp_path))
  assert event.output["input_kind"] == "sqlite"


async def test_init_shop_missing_state_raises() -> None:
  """init_shop thiếu business_id thì raise (lỗi dừng thật, không loop)."""
  node = make_init_shop(CONFIG)
  with pytest.raises(ValueError, match="business_id"):
    await node(business_id="", shop_dir="/tmp/khong-ton-tai", output_dir="/tmp")


def test_branch_input_routes() -> None:
  """branch_input phát route sqlite/default theo input_kind."""
  assert branch_input({"input_kind": "sqlite"}).actions.route == "sqlite"
  assert branch_input({"input_kind": "default"}).actions.route == "default"
  assert branch_input({}).actions.route == "default"


async def test_scan_sheets_simple_csv(tmp_path: Path) -> None:
  """scan_sheets quét CSV sạch: header dòng 0, không mơ hồ."""
  shop_dir = tmp_path / "biz_csv"
  _write_csv(
      shop_dir / "don.csv",
      [["Mã đơn", "Tổng tiền"], ["DH1", "100000"], ["DH2", "200000"]],
  )
  node = make_scan_sheets(CONFIG)
  event = await node(business_id="biz_csv", shop_dir=str(shop_dir))
  tables = event.output
  assert len(tables) == 1
  assert tables[0]["header"] == ["Mã đơn", "Tổng tiền"]
  assert tables[0]["header_row"] == 0
  assert tables[0]["ambiguous"] is False
  assert tables[0]["row_count"] == 2


async def test_scan_sheets_ambiguous_title_row(tmp_path: Path) -> None:
  """scan_sheets đánh dấu mơ hồ khi header không ở dòng 0 (dòng tiêu đề)."""
  shop_dir = tmp_path / "biz_title"
  _write_csv(
      shop_dir / "don.csv",
      [["BÁO CÁO BÁN HÀNG"], ["Mã đơn", "Tổng tiền"], ["DH1", "100000"]],
  )
  node = make_scan_sheets(CONFIG)
  event = await node(business_id="biz_title", shop_dir=str(shop_dir))
  tables = event.output
  assert tables[0]["header_row"] == 1
  assert tables[0]["ambiguous"] is True


async def test_scan_sqlite(tmp_path: Path) -> None:
  """scan_sqlite quét bảng SQLite: header từ schema, không mơ hồ."""
  shop_dir = tmp_path / "biz_db"
  shop_dir.mkdir()
  connection = sqlite3.connect(str(shop_dir / "pos.sqlite"))
  connection.execute("CREATE TABLE don (ma TEXT, tien INTEGER)")
  connection.executemany("INSERT INTO don VALUES (?, ?)", [("DH1", 1), ("DH2", 2)])
  connection.commit()
  connection.close()
  node = make_scan_sqlite(CONFIG)
  event = await node(business_id="biz_db", shop_dir=str(shop_dir))
  tables = event.output
  assert len(tables) == 1
  assert tables[0]["header"] == ["ma", "tien"]
  assert tables[0]["header_row"] == -1
  assert tables[0]["ambiguous"] is False
  assert tables[0]["row_count"] == 2


def test_header_gate_skip_sets_default() -> None:
  """header_gate nhánh skip vẫn ghi headers_final mặc định."""
  tables = [{"table_id": "don", "header": ["Mã đơn"], "ambiguous": False}]
  event = header_gate(tables)
  assert event.actions.route == "skip"
  assert event.actions.state_delta["headers_final"] == {"don": ["Mã đơn"]}
  assert event.output == '{"ambiguous_tables": []}' or "ambiguous_tables" in event.output


def test_header_gate_agent_branch() -> None:
  """header_gate nhánh agent khi có bảng mơ hồ, output liệt kê đúng bảng đó."""
  tables = [
      {"table_id": "don", "header": ["Mã đơn"], "ambiguous": True},
      {"table_id": "khach", "header": ["Tên"], "ambiguous": False},
  ]
  event = header_gate(tables)
  assert event.actions.route == "agent"
  assert "don" in event.output
  assert event.actions.state_delta["headers_final"]["khach"] == ["Tên"]


def test_merge_gate_skip_and_agent() -> None:
  """merge_gate skip khi 0-1 bảng, agent khi nhiều bảng, luôn ghi mặc định."""
  one = [{"table_id": "don", "column_signature": "ma", "row_count": 2}]
  event = merge_gate(one)
  assert event.actions.route == "skip"
  assert event.actions.state_delta["merge_groups"]["groups"] == [{"tables": ["don"], "evidence": "giữ riêng mặc định"}]
  two = one + [{"table_id": "khach", "column_signature": "ten", "row_count": 3}]
  event = merge_gate(two)
  assert event.actions.route == "agent"
  assert len(event.actions.state_delta["merge_groups"]["groups"]) == 2
