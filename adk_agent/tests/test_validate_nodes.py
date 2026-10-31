# -*- coding: utf-8 -*-
"""Test các node validator/route/fallback: header, merge, schema, loop.

Route node được gọi trực tiếp với ctx giả (SimpleNamespace có .state)
vì FunctionNode bind ctx từ framework khi chạy trong graph; gọi trực
tiếp với ctx giả là cách kiểm chứng luật route độc lập với graph.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from ingest._config import IngestConfig
from ingest._models import (
    HeaderProposal,
    MergeProposal,
    ShopSchema,
    TableHeader,
    MergeGroup,
)
from ingest.nodes._validate_nodes import (
    make_route_header,
    make_route_merge,
    make_route_schema,
    mark_header_fallback,
    mark_merge_fallback,
    mark_shop_failed,
    validate_header,
    validate_merge,
    validate_schema,
)
from ingest.nodes._validate_nodes import Verdict

CONFIG = IngestConfig()
RAW = [
    {
        "table_id": "don",
        "header": ["Mã đơn", "Tổng tiền"],
        "column_signature": "ma_don|tong_tien",
        "ambiguous": False,
    },
    {
        "table_id": "don_2",
        "header": ["Mã đơn", "Tổng tiền"],
        "column_signature": "ma_don|tong_tien",
        "ambiguous": False,
    },
    {
        "table_id": "chitiet",
        "header": ["Mã đơn", "Tên hàng"],
        "column_signature": "ma_don|ten_hang",
        "ambiguous": False,
    },
    {
        "table_id": "chitietlo",
        "header": ["Mã đơn", "Tên hàng", "Mã lô"],
        "column_signature": "ma_don|ten_hang|ma_lo",
        "ambiguous": False,
    },
]


def test_validate_header_ok() -> None:
  """validate_header đạt khi đề xuất khớp header ứng viên."""
  proposal = HeaderProposal(
      tables=[TableHeader(table_id="don", header_row=0, columns=["Mã đơn", "Tổng tiền"])],
  )
  event = validate_header(proposal, RAW)
  assert event.output["status"] == "ok"
  assert event.actions.state_delta["headers_final"]["don"] == ["Mã đơn", "Tổng tiền"]


def test_validate_header_wrong_columns_fail() -> None:
  """validate_header bác khi đề xuất khác header ứng viên."""
  proposal = HeaderProposal(
      tables=[TableHeader(table_id="don", header_row=0, columns=["Mã đơn", "Tiền khác"])],
  )
  event = validate_header(proposal, RAW)
  assert event.output["status"] == "fail"
  assert "không khớp" in event.output["reason"]


def test_validate_header_unknown_table_fail() -> None:
  """validate_header bác khi đề xuất bảng không tồn tại."""
  proposal = HeaderProposal(
      tables=[TableHeader(table_id="khong_co", header_row=0, columns=["A"])]
  )
  event = validate_header(proposal, RAW)
  assert event.output["status"] == "fail"


def test_validate_header_duplicate_columns_fail() -> None:
  """validate_header bác khi tên cột trùng sau chuẩn hóa."""
  proposal = HeaderProposal(
      tables=[TableHeader(table_id="don", header_row=0, columns=["Mã đơn", "Ma don"])],
  )
  event = validate_header(proposal, RAW)
  assert event.output["status"] == "fail"


def test_validate_merge_ok_same_signature() -> None:
  """validate_merge đạt khi gộp hai bảng cùng chữ ký cột."""
  proposal = MergeProposal(
      groups=[MergeGroup(tables=["don", "don_2"], evidence="cùng chữ ký")],
      singles=["chitiet", "chitietlo"],
  )
  event = validate_merge(proposal, RAW)
  assert event.output["status"] == "ok"
  assert event.actions.state_delta["merge_groups"]["groups"][0]["tables"] == ["don", "don_2"]


def test_validate_merge_different_signature_fail() -> None:
  """validate_merge bác khi nhóm có chữ ký cột khác nhau."""
  proposal = MergeProposal(
      groups=[MergeGroup(tables=["don", "chitiet"], evidence="gộp bừa")],
      singles=["don_2", "chitietlo"],
  )
  event = validate_merge(proposal, RAW)
  assert event.output["status"] == "fail"


def test_validate_merge_lot_variant_fail() -> None:
  """validate_merge bác khi nhóm chứa cả bảng và biến thể lô của nó."""
  lot_raw = [
        {"table_id": "chitiet", "column_signature": "ma_don|ten_hang"},
        {"table_id": "chitietlo", "column_signature": "ma_don|ten_hang"},
    ]
  proposal = MergeProposal(
      groups=[MergeGroup(tables=["chitiet", "chitietlo"], evidence="gộp lô")],
      singles=[],
  )
  event = validate_merge(proposal, lot_raw)
  assert event.output["status"] == "fail"
  assert "lô" in event.output["reason"]


def test_validate_merge_missing_table_fail() -> None:
  """validate_merge bác khi đề xuất sót bảng chưa phân nhóm."""
  proposal = MergeProposal(
      groups=[MergeGroup(tables=["don"], evidence="đơn")],
      singles=["don_2"],
  )
  event = validate_merge(proposal, RAW)
  assert event.output["status"] == "fail"
  assert "sót" in event.output["reason"]


def _route(route_fn: object, verdict: Verdict, state: dict) -> object:
  """Gọi route node với ctx giả mang state cho trước."""
  return route_fn(SimpleNamespace(state=dict(state)), verdict)  # type: ignore[operator]


def test_route_ok() -> None:
  """Route ok khi Verdict ok, không tăng bộ đếm."""
  route = make_route_header(CONFIG)
  event = _route(route, Verdict(status="ok", reason="đạt"), {})
  assert event.actions.route == "ok"


def test_route_retry_then_fail_after_3() -> None:
  """Route retry 2 lần rồi fail ở lần fail thứ 3 (tổng 3 lần chạy agent)."""
  route = make_route_header(CONFIG)
  state: dict = {}
  event = _route(route, Verdict(status="fail", reason="lỗi A"), state)
  assert event.actions.route == "retry"
  state.update(event.actions.state_delta)
  event = _route(route, Verdict(status="fail", reason="lỗi B"), state)
  assert event.actions.route == "retry"
  state.update(event.actions.state_delta)
  event = _route(route, Verdict(status="fail", reason="lỗi C"), state)
  assert event.actions.route == "fail"
  assert event.actions.state_delta["header_attempts"] == 3


def test_route_fail_early_on_repeated_error() -> None:
  """Route fail sớm khi lỗi lặp đúng lần trước (không chờ đủ 3 lần)."""
  route = make_route_merge(CONFIG)
  first = _route(route, Verdict(status="fail", reason="lỗi X"), {})
  assert first.actions.route == "retry"
  state = dict(first.actions.state_delta)
  second = _route(route, Verdict(status="fail", reason="lỗi X"), state)
  assert second.actions.route == "fail"


def test_route_schema_uses_own_counter() -> None:
  """Route schema dùng bộ đếm riêng (schema_attempts, không lẫn header)."""
  route = make_route_schema(CONFIG)
  event = _route(route, Verdict(status="fail", reason="lỗi S"), {})
  assert event.actions.route == "retry"
  assert "schema_attempts" in event.actions.state_delta


def test_mark_fallbacks() -> None:
  """Fallback header/merge ghi cờ và giá trị an toàn vào state."""
  event = mark_header_fallback(RAW)
  assert event.actions.state_delta["header_fallback"] is True
  assert event.actions.state_delta["headers_final"]["don"] == ["Mã đơn", "Tổng tiền"]
  event = mark_merge_fallback(RAW)
  assert event.actions.state_delta["merge_fallback"] is True
  assert len(event.actions.state_delta["merge_groups"]["groups"]) == 4
  event = mark_shop_failed("schema sai")
  assert event.actions.state_delta["shop_status"] == "failed"
  assert event.actions.state_delta["shop_error"] == "schema sai"


def _make_tmp_db(path: Path) -> None:
  """Tạo DB tạm giả lập với một bảng don có 2 dòng."""
  connection = sqlite3.connect(str(path))
  connection.execute("CREATE TABLE don (ma TEXT, tien INTEGER)")
  connection.executemany("INSERT INTO don VALUES (?, ?)", [("DH1", 100), ("DH2", 200)])
  connection.commit()
  connection.close()


def _schema_for_db() -> ShopSchema:
  """Schema đúng cho DB giả lập ở trên."""
  return ShopSchema(
      business_id="biz_test",
      tables=[
          {
              "table_id": "don",
              "original_names": ["don"],
              "source_sheets": ["don"],
              "sqlite_table": "don",
              "row_count": 2,
              "description_vi": "Bảng đơn.",
              "columns": [
                  {
                      "original_name": "ma",
                      "sqlite_name": "ma",
                      "type": "TEXT",
                      "description_vi": "Mã đơn.",
                      "role_hint": "order_id",
                      "sample_values": ["DH1"],
                      "distinct_count": 2,
                      "distinct_values": ["DH1", "DH2"],
                      "nullable_rate": 0.0,
                  },
                  {
                      "original_name": "tien",
                      "sqlite_name": "tien",
                      "type": "INTEGER",
                      "description_vi": "Tiền.",
                      "role_hint": "money",
                      "sample_values": ["100"],
                      "distinct_count": 2,
                      "distinct_values": ["100", "200"],
                      "nullable_rate": 0.0,
                  },
              ],
              "date_range": {},
          }
      ],
      join_hints=[],
      notes=[],
  )


async def test_validate_schema_ok_writes_file(tmp_path: Path) -> None:
  """validate_schema đạt thì ghi file schema vào output_dir/schemas."""
  db_path = tmp_path / "biz_test.tmp.db"
  _make_tmp_db(db_path)
  event = await validate_schema(
      _schema_for_db(),
      tmp_db_path=str(db_path),
      business_id="biz_test",
      output_dir=str(tmp_path),
  )
  assert event.output["status"] == "ok"
  schema_path = Path(event.actions.state_delta["schema_path"])
  assert schema_path.is_file()
  parsed = json.loads(schema_path.read_text(encoding="utf-8"))
  assert parsed["business_id"] == "biz_test"


async def test_validate_schema_bad_sample_fail(tmp_path: Path) -> None:
  """validate_schema bác khi mẫu không tồn tại trong DB và không ghi file."""
  db_path = tmp_path / "biz_test.tmp.db"
  _make_tmp_db(db_path)
  schema = _schema_for_db()
  schema.tables[0].columns[0].sample_values = ["KHONG_CO"]
  event = await validate_schema(
      schema,
      tmp_db_path=str(db_path),
      business_id="biz_test",
      output_dir=str(tmp_path),
  )
  assert event.output["status"] == "fail"
  assert not (tmp_path / "schemas" / "biz_test.json").exists()


async def test_validate_schema_wrong_business_fail(tmp_path: Path) -> None:
  """validate_schema bác khi business_id trong schema khác shop đang chạy."""
  db_path = tmp_path / "biz_test.tmp.db"
  _make_tmp_db(db_path)
  event = await validate_schema(
      _schema_for_db(),
      tmp_db_path=str(db_path),
      business_id="biz_khac",
      output_dir=str(tmp_path),
  )
  assert event.output["status"] == "fail"
