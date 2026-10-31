# -*- coding: utf-8 -*-
"""Test các node validator/route/fallback: header, merge, schema, loop.

Route node được gọi trực tiếp với ctx giả (SimpleNamespace có .state)
vì FunctionNode bind ctx từ framework khi chạy trong graph; gọi trực
tiếp với ctx giả là cách kiểm chứng luật route độc lập với graph.
"""

from __future__ import annotations

from types import SimpleNamespace

from ingest._config import IngestConfig
from ingest._models import (
    HeaderProposal,
    MergeProposal,
    ShopMeta,
    TableHeader,
    TableSchema,
    MergeGroup,
)
from ingest.nodes._validate_nodes import (
    make_route_header,
    make_route_merge,
    make_route_schema_meta,
    make_route_schema_table,
    mark_header_fallback,
    mark_merge_fallback,
    mark_schema_meta_fallback,
    mark_shop_failed,
    validate_header,
    validate_merge,
    validate_schema_meta,
    validate_schema_table,
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


def test_route_schema_table_advances_and_done() -> None:
  """Route bảng ok thì sang bảng sau, hết bảng thì done."""
  route = make_route_schema_table(CONFIG)
  drafts = [{"table_id": "a", "sqlite_table": "a"}, {"table_id": "b", "sqlite_table": "b"}]
  event = _route(
      route,
      Verdict(status="ok", reason="đạt"),
      {"schema_table_drafts": drafts, "schema_table_index": 0,
       "schema_table_results": [], "schema_table_fallbacks": []},
  )
  assert event.actions.route == "next"
  assert event.actions.state_delta["schema_table_index"] == 1
  assert "b" in event.actions.state_delta["schema_table_draft_text"]
  event = _route(
      route,
      Verdict(status="ok", reason="đạt"),
      {"schema_table_drafts": drafts, "schema_table_index": 1,
       "schema_table_results": [{"table_id": "a"}],
       "schema_table_fallbacks": []},
  )
  assert event.actions.route == "done"


def test_route_schema_table_fallback_after_3() -> None:
  """Route bảng fail đủ 3 lần thì fallback về bản nháp rồi đi tiếp."""
  route = make_route_schema_table(CONFIG)
  drafts = [{"table_id": "a", "sqlite_table": "a"}, {"table_id": "b", "sqlite_table": "b"}]
  state: dict = {"schema_table_drafts": drafts, "schema_table_index": 0,
                 "schema_table_results": [], "schema_table_fallbacks": []}
  for reason in ("lỗi A", "lỗi B", "lỗi C"):
    event = _route(route, Verdict(status="fail", reason=reason), state)
    state = dict(state)
    state.update(event.actions.state_delta)
  assert event.actions.route == "next"
  assert event.actions.state_delta["schema_table_results"] == [drafts[0]]
  assert event.actions.state_delta["schema_table_fallbacks"] == ["a"]
  assert event.actions.state_delta["schema_table_attempts"] == 0


def test_route_schema_table_repeated_error_fallback_early() -> None:
  """Route bảng fail lặp đúng lỗi thì fallback sớm, không chờ đủ 3."""
  route = make_route_schema_table(CONFIG)
  drafts = [{"table_id": "a", "sqlite_table": "a"}]
  state: dict = {"schema_table_drafts": drafts, "schema_table_index": 0,
                 "schema_table_results": [], "schema_table_fallbacks": []}
  event = _route(route, Verdict(status="fail", reason="lỗi X"), state)
  assert event.actions.route == "retry"
  state = dict(state)
  state.update(event.actions.state_delta)
  event = _route(route, Verdict(status="fail", reason="lỗi X"), state)
  assert event.actions.route == "done"
  assert event.actions.state_delta["schema_table_fallbacks"] == ["a"]


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


def _draft_table() -> dict:
  """Bản nháp một bảng don (dạng dict như write_sqlite_tmp tạo ra)."""
  return {
      "table_id": "don",
      "original_names": ["don"],
      "source_sheets": ["don"],
      "sqlite_table": "don",
      "row_count": 2,
      "description_vi": "",
      "columns": [
          {
              "original_name": "ma",
              "sqlite_name": "ma",
              "type": "TEXT",
              "description_vi": "",
              "role_hint": "unknown",
              "sample_values": ["DH1"],
              "distinct_count": 2,
              "distinct_values": ["DH1", "DH2"],
              "nullable_rate": 0.0,
          },
          {
              "original_name": "tien",
              "sqlite_name": "tien",
              "type": "INTEGER",
              "description_vi": "",
              "role_hint": "unknown",
              "sample_values": ["100"],
              "distinct_count": 2,
              "distinct_values": ["100", "200"],
              "nullable_rate": 0.0,
          },
      ],
      "date_range": {},
  }


def _table_for_draft() -> TableSchema:
  """Bảng do agent viết: giữ nguyên giá trị đo, thêm mô tả và role."""
  draft = _draft_table()
  draft["description_vi"] = "Bảng đơn."
  draft["columns"][0]["description_vi"] = "Mã đơn."
  draft["columns"][0]["role_hint"] = "order_id"
  draft["columns"][1]["description_vi"] = "Tiền."
  draft["columns"][1]["role_hint"] = "money"
  return TableSchema(**draft)


def test_validate_schema_table_ok() -> None:
  """validate_schema_table đạt khi agent giữ nguyên giá trị đã đo."""
  event = validate_schema_table(
      _table_for_draft(),
      schema_table_drafts=[_draft_table()],
      schema_table_index=0,
      schema_table_results=[],
  )
  assert event.output["status"] == "ok"
  results = event.actions.state_delta["schema_table_results"]
  assert len(results) == 1
  assert results[0]["sqlite_table"] == "don"
  assert results[0]["columns"][0]["role_hint"] == "order_id"


def test_validate_schema_table_changed_sample_fail() -> None:
  """validate_schema_table bác khi agent sửa mẫu đã đo."""
  table = _table_for_draft()
  table.columns[0].sample_values = ["KHONG_CO"]
  event = validate_schema_table(
      table,
      schema_table_drafts=[_draft_table()],
      schema_table_index=0,
      schema_table_results=[],
  )
  assert event.output["status"] == "fail"
  assert "sample_values" in event.output["reason"]


def test_validate_schema_meta_ok_and_bad_reference() -> None:
  """validate_schema_meta đạt khi join đúng tham chiếu, bác khi sai."""
  results = [_table_for_draft().model_dump()]
  good = ShopMeta(
      join_hints=[
          {
              "from_table": "don",
              "from_column": "ma",
              "to_table": "don",
              "to_column": "ma",
              "evidence": "cùng mã đơn",
          }
      ],
      notes=["Bẫy ví dụ."],
  )
  event = validate_schema_meta(good, schema_table_results=results)
  assert event.output["status"] == "ok"
  assert event.actions.state_delta["schema_meta"]["notes"] == ["Bẫy ví dụ."]
  bad = ShopMeta(
      join_hints=[
          {
              "from_table": "don",
              "from_column": "ma",
              "to_table": "khong_co",
              "to_column": "ma",
              "evidence": "",
          }
      ],
      notes=[],
  )
  event = validate_schema_meta(bad, schema_table_results=results)
  assert event.output["status"] == "fail"


def test_mark_schema_meta_fallback() -> None:
  """Fallback meta ghi join và notes rỗng, không đoán quan hệ."""
  event = mark_schema_meta_fallback()
  assert event.actions.state_delta["schema_meta"] == {
      "join_hints": [],
      "notes": [],
  }
  assert event.actions.state_delta["schema_meta_fallback"] is True


def test_validate_schema_table_ignores_unmeasured_date_range() -> None:
  """validate_schema_table đạt khi agent tự điền date_range chưa đo.

  Bản nháp date_range {} nghĩa là chưa đo, không phải giá trị phải
  khớp: agent điền khoảng ngày (dù đúng hay sai) cũng không bị bác,
  vì assemble_schema sẽ ghi đè bằng khoảng của bản nháp trước khi ghi
  file nên khoảng tự điền không lọt vào schema.
  """
  draft = _draft_table()
  table = _table_for_draft()
  table.date_range = {"min": "2025-01-01", "max": "2025-01-05"}  # type: ignore[assignment]
  event = validate_schema_table(
      table,
      schema_table_drafts=[draft],
      schema_table_index=0,
      schema_table_results=[],
  )
  assert event.output["status"] == "ok"


def test_validate_merge_with_locked_combines() -> None:
  """Validator gộp tiếp nhóm khóa với phần agent đạt, chỉ kiểm phần agent."""
  locked = [{"tables": ["don", "don_2"], "evidence": "gộp khóa bởi code"}]
  proposal = MergeProposal(groups=[], singles=["chitiet", "chitietlo"])
  event = validate_merge(proposal, RAW, locked)
  assert event.output["status"] == "ok"
  combined = event.actions.state_delta["merge_groups"]
  assert combined["groups"] == locked
  assert combined["singles"] == ["chitiet", "chitietlo"]


def test_validate_merge_proposal_locked_id_fails() -> None:
  """Đề xuất nhắc tới bảng đã khóa thì bị bác (agent không được quyết lại)."""
  locked = [{"tables": ["don", "don_2"], "evidence": "gộp khóa bởi code"}]
  proposal = MergeProposal(
      groups=[MergeGroup(tables=["don"], evidence="quyết lại")],
      singles=["chitiet", "chitietlo"],
  )
  event = validate_merge(proposal, RAW, locked)
  assert event.output["status"] == "fail"
  assert "khóa" in event.output["reason"]


def test_validate_merge_missing_unlocked_fails() -> None:
  """Đề xuất sót bảng chưa khóa thì bị bác."""
  locked = [{"tables": ["don", "don_2"], "evidence": "gộp khóa bởi code"}]
  proposal = MergeProposal(groups=[], singles=["chitiet"])
  event = validate_merge(proposal, RAW, locked)
  assert event.output["status"] == "fail"
  assert "chitietlo" in event.output["reason"]


def test_mark_merge_fallback_keeps_locked() -> None:
  """Fallback giữ nhóm khóa, chỉ bảng chưa khóa mới rơi về giữ riêng."""
  locked = [{"tables": ["don", "don_2"], "evidence": "gộp khóa bởi code"}]
  event = mark_merge_fallback(RAW, locked)
  assert event.actions.state_delta["merge_fallback"] is True
  groups = event.actions.state_delta["merge_groups"]["groups"]
  assert groups[0] == locked[0]
  assert [g["tables"] for g in groups[1:]] == [["chitiet"], ["chitietlo"]]
