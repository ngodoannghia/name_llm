# -*- coding: utf-8 -*-
"""Test các node SQLite: ghi DB tạm, validate_shop, shop_route, publish,
finalize."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from ingest._config import IngestConfig
from ingest._models import Verdict
from ingest.nodes._clean_nodes import make_clean_tables
from ingest.nodes._sqlite_nodes import (
    finalize_shop,
    make_write_sqlite_tmp,
    publish_shop,
    shop_route,
    validate_shop,
)

CONFIG = IngestConfig()


def _write_csv(path: Path, rows: list[list[str]]) -> None:
  """Ghi file CSV giả lập."""
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open("w", encoding="utf-8", newline="") as handle:
    csv.writer(handle).writerows(rows)


async def _staging_for_shop(tmp_path: Path) -> tuple[Path, list[dict]]:
  """Chạy clean_tables trên shop giả lập, trả staging_dir và manifest."""
  shop_dir = tmp_path / "biz_csv"
  _write_csv(
      shop_dir / "don.csv",
      [["Mã đơn hàng", "Tổng tiền"], ["DH1", "100000"], ["DH2", "200000"]],
  )
  node = make_clean_tables(CONFIG)
  event = await node(
      merge_groups={"groups": [{"tables": ["don"], "evidence": "giữ riêng"}], "singles": []},
      headers_final={"don": ["Mã đơn hàng", "Tổng tiền"]},
      shop_dir=str(shop_dir),
      output_dir=str(tmp_path),
      business_id="biz_csv",
      input_kind="default",
  )
  return Path(event.actions.state_delta["staging_dir"]), event.actions.state_delta["manifest"]


async def test_write_sqlite_tmp_creates_db_and_meta(tmp_path: Path) -> None:
  """write_sqlite_tmp ghi DB tạm mở được, có bảng dữ liệu + 3 bảng meta."""
  staging_dir, manifest = await _staging_for_shop(tmp_path)
  node = make_write_sqlite_tmp(CONFIG)
  event = await node(
      manifest=manifest,
      staging_dir=str(staging_dir),
      output_dir=str(tmp_path),
      business_id="biz_csv",
  )
  db_path = Path(event.actions.state_delta["tmp_db_path"])
  assert db_path.is_file()
  connection = sqlite3.connect(str(db_path))
  tables = {
      row[0]
      for row in connection.execute(
          "SELECT name FROM sqlite_master WHERE type = 'table'"
      ).fetchall()
  }
  assert "_shop_meta" in tables
  assert "_schema_map" in tables
  assert "_clean_stats" in tables
  rows = connection.execute('SELECT * FROM "don"').fetchall()
  assert len(rows) == 2
  connection.close()
  draft = event.actions.state_delta["schema_draft"]
  assert draft["tables"][0]["row_count"] == 2
  assert "schema_draft_text" in event.actions.state_delta


async def test_validate_shop_ok_and_fail(tmp_path: Path) -> None:
  """validate_shop đạt khi DB + schema khớp, fail khi schema sai shop."""
  staging_dir, manifest = await _staging_for_shop(tmp_path)
  node = make_write_sqlite_tmp(CONFIG)
  event = await node(
      manifest=manifest,
      staging_dir=str(staging_dir),
      output_dir=str(tmp_path),
      business_id="biz_csv",
  )
  db_path = event.actions.state_delta["tmp_db_path"]
  schema_path = tmp_path / "schemas" / "biz_csv.json"
  schema_path.parent.mkdir(parents=True, exist_ok=True)
  schema_path.write_text(
      json.dumps(
          {"business_id": "biz_csv", "tables": [{"sqlite_table": "don"}]},
          ensure_ascii=False,
      ),
      encoding="utf-8",
  )
  verdict = await validate_shop(
      Verdict(status="ok", reason="route ok"),
      tmp_db_path=db_path,
      schema_path=str(schema_path),
      business_id="biz_csv",
  )
  assert verdict.status == "ok"
  verdict = await validate_shop(
      Verdict(status="ok", reason="route ok"),
      tmp_db_path=db_path,
      schema_path=str(schema_path),
      business_id="biz_khac",
  )
  assert verdict.status == "fail"


def test_shop_route_and_publish_and_finalize(tmp_path: Path) -> None:
  """shop_route rẽ đúng, publish rename nguyên tử, finalize tổng hợp report."""
  assert shop_route(Verdict(status="ok", reason="đạt")).actions.route == "ok"
  assert shop_route(Verdict(status="fail", reason="sai")).actions.route == "fail"
  tmp_db = tmp_path / "biz_csv.tmp.db"
  tmp_db.write_bytes(b"fake-db")
  event = publish_shop(
      tmp_db_path=str(tmp_db), output_dir=str(tmp_path), business_id="biz_csv"
  )
  assert Path(event.actions.state_delta["db_path"]).is_file()
  assert not tmp_db.exists()
  report_event = finalize_shop(
      business_id="biz_csv",
      db_path=event.actions.state_delta["db_path"],
      schema_path=str(tmp_path / "schemas" / "biz_csv.json"),
      manifest=[{"row_count": 2}],
  )
  assert report_event.output["status"] == "ok"
  assert report_event.output["table_count"] == 1
  assert report_event.output["row_count"] == 2
  failed_event = finalize_shop(
      business_id="biz_csv", shop_status="failed", shop_error="schema sai"
  )
  assert failed_event.output["status"] == "failed"
  assert failed_event.output["db_path"] == ""
  assert failed_event.output["error"] == "schema sai"
