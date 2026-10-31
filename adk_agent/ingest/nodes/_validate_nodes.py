# -*- coding: utf-8 -*-
"""Các node validator, route và fallback cho 4 loop của workflow ingest.

Các loop là header, merge, schema theo từng bảng và schema meta. Loop
schema theo từng bảng khác 3 loop còn lại ở điểm: hết lượt thử thì
không fail cả shop mà fallback bảng đó về bản nháp đo được (xem
docstring của make_route_schema_table), vì dữ liệu đo được của bảng
vẫn đúng, chỉ thiếu mô tả do model viết.


Module này thuộc tầng kiểm chứng. Nguyên tắc xuyên suốt (xem plan §2.4):
- Validator trả Verdict, không raise exception để kết thúc loop (node
  raise sẽ dừng cả workflow).
- Route node phát route chuỗi "ok" / "retry" / "fail" dựa trên Verdict và
  bộ đếm trong state; bộ đếm được ghi lại qua Event(state=...), không
  gán trực tiếp ctx.state (replay không tái tạo gán trực tiếp).
- Nhánh "fail" luôn đi tới một node ghi log/kết luận, không bao giờ raise.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from google.adk import Event

from .._config import IngestConfig
from .._models import (
    HeaderProposal,
    MergeProposal,
    RawTable,
    ShopMeta,
    TableSchema,
    Verdict,
)
from .._phase1 import normalize_key


def _normalized(columns: list[str]) -> list[str]:
  """Chuẩn hóa danh sách tên cột để so sánh không phân biệt dấu/case.

  Args:
    columns: Danh sách tên cột gốc.

  Returns:
    Danh sách tên cột đã chuẩn hóa, giữ nguyên thứ tự.
  """
  return [normalize_key(col) for col in columns]


def _verdict_event(
    verdict: Verdict, state: dict | None = None
) -> Event:
  """Đóng gói Verdict thành một Event output duy nhất kèm state.

  Nguyên tắc phân công bắt buộc (đã sửa sau khi test workflow phát hiện
  route fail ngay lần đầu): VALIDATOR KHÔNG ĐƯỢC GHI last_error khi
  fail. Route node so sánh reason lần này với last_error của lần fail
  TRƯỚC (do chính route ghi ở lần trước); nếu validator cũng ghi
  last_error = reason ngay trong lần fail này thì route đọc state sẽ
  thấy reason trùng với chính nó và tưởng nhầm là lỗi lặp, fail sớm sai.
  Vì vậy last_error và bộ đếm attempts chỉ do route node ghi; validator
  chỉ ghi kết quả chốt (headers_final / merge_groups / schema_path).

  Args:
    verdict: Kết quả kiểm chứng.
    state: State delta ghi kèm (mặc định rỗng, không chứa last_error
      khi fail).

  Returns:
    Event mang output là Verdict đã dump và state delta đã cho.
  """
  return Event(output=verdict.model_dump(), state=state or {})


def validate_header(
    node_input: HeaderProposal,
    raw_tables: list[dict] | None = None,
) -> Event:
  """Kiểm chứng đề xuất header của HeaderAgent.

  Quy tắc kiểm chứng của khung lần này (ghi rõ để lần sau nâng cấp):
  đề xuất chỉ đạt khi tên cột chuẩn hóa của nó khớp đúng header ứng viên
  từ quét (do validator chưa có dòng thô để kiểm chứng header ở dòng
  khác). Đây là quy tắc an toàn nhưng chặt: agent đề xuất header khác
  dòng quét sẽ bị bác và fallback về luật scoring. Lần sau nâng cấp
  validator để kiểm chứng header ở bất kỳ dòng nào trong 15 dòng đầu.

  Args:
    node_input: Đề xuất header (đã coerce thành HeaderProposal từ output
      dict của agent).
    raw_tables: Danh sách RawTable đã dump (lấy từ state).

  Returns:
    Một Event duy nhất mang Verdict và state (headers_final khi đạt;
    nhánh fail không ghi last_error — route node tự ghi, xem docstring
    của _verdict_event).
  """
  tables = {table["table_id"]: table for table in (raw_tables or [])}
  proposed: dict[str, list[str]] = {}
  for item in node_input.tables:
    if item.table_id not in tables:
      return _verdict_event(
          Verdict(
              status="fail",
              reason=f"Bảng {item.table_id} không tồn tại trong dữ liệu quét.",
          ),
          state={},
      )
    if not item.columns:
      return _verdict_event(
          Verdict(
              status="fail",
              reason=f"Bảng {item.table_id} đề xuất header rỗng.",
          ),
          state={},
      )
    normalized = _normalized(item.columns)
    if len(set(normalized)) != len(normalized):
      return _verdict_event(
          Verdict(
              status="fail",
              reason=f"Bảng {item.table_id} có tên cột trùng nhau sau chuẩn hóa.",
          ),
          state={},
      )
    candidate = _normalized(tables[item.table_id]["header"])
    if normalized != candidate:
      return _verdict_event(
          Verdict(
              status="fail",
              reason=(
                  f"Bảng {item.table_id} đề xuất header không khớp header "
                  f"ứng viên từ quét (ứng viên: {tables[item.table_id]['header']})."
              ),
          ),
          state={},
      )
    proposed[item.table_id] = item.columns
  # Các bảng mơ hồ không nằm trong đề xuất thì giữ header ứng viên:
  # thiếu đề xuất không phải lỗi, vì agent chỉ cần đề xuất cho bảng nó
  # muốn sửa; bảng còn lại đã có header ứng viên tin cậy từ quét.
  final = {table_id: table["header"] for table_id, table in tables.items()}
  final.update(proposed)
  return _verdict_event(
      Verdict(status="ok", reason="Header đề xuất khớp dữ liệu quét."),
      state={"headers_final": final, "last_error": ""},
  )


def _lot_variant_name(table_id: str) -> str:
  """Bỏ hậu tố lô khỏi id bảng để phát hiện cặp ChiTiet/ChiTietLo.

  Args:
    table_id: Id bảng đã chuẩn hóa.

  Returns:
    Id sau khi bỏ hậu tố "_lo" hoặc "lo" ở cuối (nếu có).
  """
  if table_id.endswith("_lo"):
    return table_id[: -len("_lo")]
  if table_id.endswith("lo"):
    return table_id[: -len("lo")]
  return table_id


def validate_merge(
    node_input: MergeProposal,
    raw_tables: list[dict] | None = None,
) -> Event:
  """Kiểm chứng đề xuất gộp của MergeAgent.

  Quy tắc kiểm chứng:
  - Mọi bảng quét phải xuất hiện đúng một lần trong đề xuất (nhóm hoặc
    singles), không sót, không trùng.
  - Nhóm nhiều bảng chỉ đạt khi mọi bảng trong nhóm có cùng chữ ký cột
    (điều kiện cần; chữ ký khác nhau thì chắc chắn không cùng bảng logic).
  - Nhóm chứa đồng thời một bảng và biến thể lô của nó (ChiTiet +
    ChiTietLo) thì bị bác: gộp hai biến thể này sẽ đếm trùng dòng chi
    tiết, phải so sánh toàn bộ schema trước khi gộp (khung lần này chưa
    có bước so sánh đó nên mặc định không gộp).

  Args:
    node_input: Đề xuất gộp (đã coerce thành MergeProposal).
    raw_tables: Danh sách RawTable đã dump (lấy từ state).

  Returns:
    Một Event duy nhất mang Verdict và state (merge_groups khi đạt;
    nhánh fail không ghi last_error — route node tự ghi).
  """
  tables = {table["table_id"]: table for table in (raw_tables or [])}
  seen: list[str] = []
  for group in node_input.groups:
    if not group.tables:
      return _verdict_event(
          Verdict(status="fail", reason="Có nhóm gộp rỗng, không chứa bảng nào."),
          state={},
      )
    for table_id in group.tables:
      if table_id not in tables:
        return _verdict_event(
            Verdict(
                status="fail",
                reason=f"Bảng {table_id} không tồn tại trong dữ liệu quét.",
            ),
            state={},
        )
      if table_id in seen:
        return _verdict_event(
            Verdict(
                status="fail",
                reason=f"Bảng {table_id} xuất hiện ở nhiều nhóm gộp.",
            ),
            state={},
        )
      seen.append(table_id)
    if len(group.tables) > 1:
      signatures = {tables[table_id]["column_signature"] for table_id in group.tables}
      if len(signatures) != 1:
        return _verdict_event(
            Verdict(
                status="fail",
                reason=(
                    f"Nhóm {group.tables} có chữ ký cột khác nhau nên không "
                    "thể là cùng một bảng logic."
                ),
            ),
            state={},
        )
      bases = {_lot_variant_name(table_id) for table_id in group.tables}
      if len(bases) != len(group.tables):
        return _verdict_event(
            Verdict(
                status="fail",
                reason=(
                    f"Nhóm {group.tables} chứa cả bảng và biến thể lô của nó "
                    "(dạng ChiTiet/ChiTietLo) nên không được gộp khi chưa so "
                    "sánh toàn bộ schema."
                ),
            ),
            state={},
        )
  for table_id in node_input.singles:
    if table_id not in tables:
      return _verdict_event(
          Verdict(
              status="fail",
              reason=f"Bảng {table_id} (giữ riêng) không tồn tại trong dữ liệu quét.",
          ),
          state={},
      )
    if table_id in seen:
      return _verdict_event(
          Verdict(
              status="fail",
              reason=f"Bảng {table_id} vừa nằm trong nhóm vừa nằm ngoài nhóm.",
          ),
          state={},
      )
    seen.append(table_id)
  if set(seen) != set(tables):
    missing = sorted(set(tables) - set(seen))
    return _verdict_event(
        Verdict(
            status="fail",
            reason=f"Đề xuất gộp còn sót bảng chưa phân nhóm: {missing}.",
        ),
        state={},
    )
  return _verdict_event(
      Verdict(status="ok", reason="Đề xuất gộp khớp dữ liệu quét."),
      state={"merge_groups": node_input.model_dump(), "last_error": ""},
  )


def _measured_mismatch(agent_table: dict, draft_table: dict) -> str:
  """So sánh các trường đã đo của bảng do agent viết với bản nháp.

  Chỉ so sánh trường đã đo (tên bảng/cột, kiểu, mẫu, distinct, số
  dòng, khoảng ngày); mô tả và role_hint là phần agent bổ sung nên
  không so sánh. Bản nháp lưu date_range dạng {} khi bảng không có
  cột ngày, còn agent (qua TableSchema) luôn trả {"min": ..., "max":
  ...} nên phải chuẩn hóa {} thành {"min": "", "max": ""} trước khi
  so sánh, nếu không mọi bảng không có cột ngày đều bị bác oan.

  So sánh với bản nháp tương đương kiểm chứng trực tiếp trên DB, vì
  bản nháp chính là kết quả đo từ DB tạm ở node write_sqlite_tmp
  (mẫu, distinct, số dòng đều truy vấn thật), nên validator này không
  cần mở DB lần nữa.

  Args:
    agent_table: Bảng do agent viết, đã dump thành dict.
    draft_table: Bảng trong bản nháp (lấy từ state).

  Returns:
    Chuỗi rỗng khi mọi trường đã đo khớp, ngược lại là lý do sai lệch.
  """
  for key in (
      "table_id",
      "original_names",
      "source_sheets",
      "sqlite_table",
      "row_count",
  ):
    if agent_table.get(key) != draft_table.get(key):
      return (
          f"Trường đã đo {key} của bảng {draft_table.get('sqlite_table')} "
          f"bị sửa: bản nháp là {draft_table.get(key)!r} nhưng agent "
          f"viết {agent_table.get(key)!r}."
      )
  draft_range = draft_table.get("date_range") or {}
  if draft_range == {}:
    draft_range = {"min": "", "max": ""}
  if agent_table.get("date_range") != draft_range:
    return (
        f"Khoảng ngày của bảng {draft_table.get('sqlite_table')} bị sửa: "
        f"bản nháp là {draft_range!r} nhưng agent viết "
        f"{agent_table.get('date_range')!r}."
    )
  agent_columns = agent_table.get("columns", [])
  draft_columns = draft_table.get("columns", [])
  if len(agent_columns) != len(draft_columns):
    return (
        f"Bảng {draft_table.get('sqlite_table')} có "
        f"{len(draft_columns)} cột trong bản nháp nhưng agent viết "
        f"{len(agent_columns)} cột."
    )
  for agent_col, draft_col in zip(agent_columns, draft_columns):
    for key in (
        "original_name",
        "sqlite_name",
        "type",
        "sample_values",
        "distinct_count",
        "distinct_values",
        "nullable_rate",
    ):
      if agent_col.get(key) != draft_col.get(key):
        return (
            f"Trường đã đo {key} của cột {draft_col.get('sqlite_name')} "
            f"(bảng {draft_table.get('sqlite_table')}) bị sửa: bản nháp "
            f"là {draft_col.get(key)!r} nhưng agent viết "
            f"{agent_col.get(key)!r}."
        )
  return ""


def validate_schema_table(
    node_input: TableSchema,
    schema_table_drafts: list[dict] | None = None,
    schema_table_index: int = 0,
    schema_table_results: list[dict] | None = None,
) -> Event:
  """Kiểm chứng bảng do SchemaTableAgent viết so với bản nháp đã đo.

  Kiểm chứng hoàn toàn bằng so sánh với bản nháp (không mở DB, vì bản
  nháp đã đo từ DB): bảng phải đúng bảng đang xử lý, tập cột và mọi
  giá trị đã đo phải giữ nguyên. Mô tả và role_hint được phép khác
  bản nháp (đó chính là phần agent bổ sung); mô tả rỗng cũng đạt, vì
  chất lượng văn bản không được đánh đổi bằng việc bác cả bảng đúng
  dữ liệu.

  Args:
    node_input: Bảng đề xuất (đã coerce thành TableSchema).
    schema_table_drafts: Danh sách bản nháp các bảng (lấy từ state).
    schema_table_index: Vị trí bảng đang xử lý (lấy từ state).
    schema_table_results: Kết quả các bảng đã xong (lấy từ state).

  Returns:
    Một Event duy nhất mang Verdict và state (thêm bảng vào kết quả
    khi đạt; nhánh fail không ghi last_error — route node tự ghi).
  """
  drafts = schema_table_drafts or []
  if schema_table_index < 0 or schema_table_index >= len(drafts):
    return _verdict_event(
        Verdict(
            status="fail",
            reason=(
                f"Không tìm thấy bản nháp cho bảng thứ {schema_table_index}."
            ),
        ),
        state={},
    )
  draft = drafts[schema_table_index]
  mismatch = _measured_mismatch(node_input.model_dump(), draft)
  if mismatch:
    return _verdict_event(Verdict(status="fail", reason=mismatch), state={})
  results = list(schema_table_results or [])
  results.append(node_input.model_dump())
  return _verdict_event(
      Verdict(status="ok", reason="Bảng khớp bản nháp đã đo."),
      state={"schema_table_results": results, "last_error": ""},
  )


def make_route_schema_table(config: IngestConfig) -> Callable:
  """Tạo route node cho vòng lặp schema theo từng bảng.

  Luật chuyển route khác 3 loop còn lại ở nhánh fail cuối: hết lượt
  thử (lỗi lặp hoặc đủ giới hạn) thì KHÔNG fail cả shop mà fallback
  bảng đang xử lý về bản nháp đo được rồi đi tiếp bảng sau. Lý do:
  dữ liệu đo được của bảng vẫn đúng (validator chỉ bác phần mô tả/
  role do model viết sai hoặc model không trả được JSON), nên giữ bảng
  với mô tả rỗng và role unknown vẫn tốt hơn là hủy cả shop 17 bảng
  vì một bảng thiếu văn bản. Bảng fallback được ghi tên vào
  schema_table_fallbacks để assemble_schema chú thích vào notes.

  Các route phát ra:
  - "retry": validator fail nhưng còn lượt thử -> quay lại agent,
    cùng bảng.
  - "next": bảng xong (đạt hoặc fallback) và còn bảng sau -> quay
    lại agent với bản nháp của bảng tiếp theo.
  - "done": bảng cuối cùng xong -> sang init_schema_meta.

  Bộ đếm schema_table_attempts được reset về 0 mỗi khi sang bảng mới,
  vì giới hạn 3 lượt áp dụng cho từng bảng, không phải cho cả shop.

  Args:
    config: Cấu hình ingest (giới hạn loop).

  Returns:
    Hàm route node cho vòng lặp schema theo từng bảng.
  """

  def route_schema_table(ctx: object, node_input: Verdict) -> Event:
    """Chuyển Verdict của bảng thành route cho vòng lặp từng bảng.

    Args:
      ctx: Context của workflow (framework bind tự động); route node
        đọc danh sách bản nháp, vị trí, kết quả và bộ đếm từ ctx.state.
      node_input: Verdict từ validate_schema_table.

    Returns:
      Một Event duy nhất mang output là Verdict, state cập nhật (vị
      trí, kết quả, fallback, bộ đếm, bản nháp bảng tiếp theo) và
      route "retry" / "next" / "done".
    """
    state = ctx.state  # type: ignore[attr-defined]
    drafts = state.get("schema_table_drafts", [])
    index = state.get("schema_table_index", 0)
    if not isinstance(index, int):
      index = 0
    results = list(state.get("schema_table_results", []))
    fallbacks = list(state.get("schema_table_fallbacks", []))
    last_error = state.get("last_error", "")

    def _advance(
        new_results: list[dict], new_fallbacks: list[str]
    ) -> Event:
      """Sang bảng tiếp theo hoặc kết thúc vòng lặp khi hết bảng."""
      new_index = index + 1
      base_state: dict = {
          "schema_table_index": new_index,
          "schema_table_results": new_results,
          "schema_table_fallbacks": new_fallbacks,
          "schema_table_attempts": 0,
          "last_error": "",
      }
      if new_index >= len(drafts):
        return Event(
            output=node_input.model_dump(),
            state=base_state,
            route="done",
        )
      base_state["schema_table_draft_text"] = json.dumps(
          {"table": drafts[new_index]}, ensure_ascii=False
      )
      return Event(
          output=node_input.model_dump(),
          state=base_state,
          route="next",
      )

    if node_input.status == "ok":
      # Validator đã cộng bảng vừa xong vào schema_table_results qua
      # Event state của nó, và state đó đã áp dụng vào session trước
      # khi route chạy (cùng cơ chế param binding theo state của mọi
      # node), nên route chỉ việc đọc results từ ctx.state, không
      # cộng thêm lần nữa.
      return _advance(results, fallbacks)
    reason = node_input.reason
    raw_attempts = state.get("schema_table_attempts", 0)
    attempts = raw_attempts if isinstance(raw_attempts, int) else 0
    new_attempts = attempts + 1
    if (reason and reason == last_error) or (
        new_attempts >= config.max_loop_attempts
    ):
      # Hết lượt thử: fallback bảng này về bản nháp rồi đi tiếp.
      if 0 <= index < len(drafts):
        draft = drafts[index]
        results.append(draft)
        fallbacks.append(draft.get("table_id", ""))
      return _advance(results, fallbacks)
    return Event(
        output=node_input.model_dump(),
        state={
            "schema_table_attempts": new_attempts,
            "last_error": reason,
        },
        route="retry",
    )

  route_schema_table.__name__ = "route_schema_table"
  return route_schema_table


def validate_schema_meta(
    node_input: ShopMeta,
    schema_table_results: list[dict] | None = None,
) -> Event:
  """Kiểm chứng join_hints và notes do SchemaMetaAgent viết.

  Chỉ kiểm tra tính tham chiếu: mỗi join_hint phải trỏ tới bảng và
  cột có thật trong kết quả từng bảng (bảng/cột đã chốt ở vòng lặp
  trước). Notes là văn bản tự do nên không kiểm nội dung. Join sai
  tham chiếu sẽ sinh SQL lỗi ở bước trả lời nên phải bác để agent
  viết lại; hết lượt thì fallback về rỗng (xem mark_schema_meta_fallback).

  Args:
    node_input: Phần meta đề xuất (đã coerce thành ShopMeta).
    schema_table_results: Kết quả các bảng đã xong (lấy từ state).

  Returns:
    Một Event duy nhất mang Verdict và state (schema_meta khi đạt;
    nhánh fail không ghi last_error — route node tự ghi).
  """
  tables: dict[str, set[str]] = {}
  for table in schema_table_results or []:
    tables[table.get("sqlite_table", "")] = {
        col.get("sqlite_name", "") for col in table.get("columns", [])
    }
  for hint in node_input.join_hints:
    for table_name, col_name, role in (
        (hint.from_table, hint.from_column, "nguồn"),
        (hint.to_table, hint.to_column, "đích"),
    ):
      if table_name not in tables:
        return _verdict_event(
            Verdict(
                status="fail",
                reason=f"Join_hint trỏ tới bảng {role} {table_name} không có thật.",
            ),
            state={},
        )
      if col_name not in tables[table_name]:
        return _verdict_event(
            Verdict(
                status="fail",
                reason=(
                    f"Join_hint trỏ tới cột {col_name} không có trong bảng "
                    f"{table_name}."
                ),
            ),
            state={},
        )
  return _verdict_event(
      Verdict(status="ok", reason="Join_hints tham chiếu đúng bảng/cột."),
      state={"schema_meta": node_input.model_dump(), "last_error": ""},
  )


def make_route_schema_meta(config: IngestConfig) -> Callable:
  """Tạo route node cho loop schema meta (tái dùng luật route chung).

  Args:
    config: Cấu hình ingest.

  Returns:
    Hàm route node cho loop schema meta.
  """
  return _make_route(config, "schema_meta_attempts")


def mark_schema_meta_fallback() -> Event:
  """Ghi fallback meta: join_hints và notes rỗng, không đoán quan hệ.

  Join rỗng an toàn hơn join sai: bước sinh SQL vẫn truy vấn được từng
  bảng riêng lẻ, chỉ thiếu gợi ý nối bảng. Đây là phương án an toàn
  khi model hết lượt thử mà vẫn viết join sai tham chiếu.

  Returns:
    Một Event duy nhất mang output ghi nhận fallback và state
    (schema_meta rỗng, cờ schema_meta_fallback).
  """
  return Event(
      output={"fallback": "schema_meta_empty"},
      state={
          "schema_meta": {"join_hints": [], "notes": []},
          "schema_meta_fallback": True,
          "last_error": "",
      },
  )

def _make_route(
    config: IngestConfig, attempts_key: str
) -> Callable:
  """Tạo route node cho một loop, đã gắn cấu hình giới hạn vòng.

  Luật chuyển route (xem CODE_DESIGN §6.2):
  - Verdict ok -> route "ok".
  - Verdict fail và lý do trùng đúng lỗi lần trước -> route "fail"
    (lỗi lặp 2 lần liên tiếp nghĩa là model không sửa được lỗi đó).
  - Verdict fail và số lần fail đã tới giới hạn -> route "fail".
  - Còn lại -> route "retry" và tăng bộ đếm thêm 1.

  Bộ đếm đếm số lần validator fail, nên tổng số lần agent chạy tối đa
  bằng đúng giới hạn (lần chạy đầu fail -> đếm 1, ..., lần chạy thứ N
  fail -> đếm N -> fail).

  Vì sao route node nhận tham số ``ctx`` thay vì khai báo bộ đếm theo
  tên: FunctionNode chỉ bind tham số theo tên state khi tên đó được khai
  báo tường minh trong chữ ký, mà key bộ đếm thì khác nhau theo từng
  loop (header_attempts / merge_attempts / schema_attempts) nên không
  thể khai báo chung một tên. Tham số ``ctx`` thì framework luôn bind
  thành Context, và route node đọc mọi key state cần thiết từ
  ``ctx.state`` — đây là cách đọc state chính thức, không phải workaround.

  Args:
    config: Cấu hình ingest (giới hạn loop).
    attempts_key: Key state của bộ đếm loop này.

  Returns:
    Hàm route node dùng được trực tiếp trong edges.
  """

  def route_node(ctx: object, node_input: Verdict) -> Event:
    """Chuyển Verdict thành route cho loop.

    Args:
      ctx: Context của workflow (framework bind tự động); route node đọc
        bộ đếm và lỗi lần trước từ ``ctx.state``.
      node_input: Verdict từ validator (đã coerce thành model).

    Returns:
      Một Event duy nhất mang output là Verdict, state cập nhật bộ đếm
      và last_error, và route "ok" / "retry" / "fail".
    """
    state = ctx.state  # type: ignore[attr-defined]
    last_error = state.get("last_error", "")
    if node_input.status == "ok":
      return Event(
          output=node_input.model_dump(),
          state={"last_error": ""},
          route="ok",
      )
    reason = node_input.reason
    # Lỗi lặp lại đúng lần trước thì fail sớm, không chờ đủ giới hạn.
    if reason and reason == last_error:
      return Event(
          output=node_input.model_dump(),
          state={"last_error": reason},
          route="fail",
      )
    raw_attempts = state.get(attempts_key, 0)
    attempts = raw_attempts if isinstance(raw_attempts, int) else 0
    new_attempts = attempts + 1
    if new_attempts >= config.max_loop_attempts:
      return Event(
          output=node_input.model_dump(),
          state={attempts_key: new_attempts, "last_error": reason},
          route="fail",
      )
    return Event(
        output=node_input.model_dump(),
        state={attempts_key: new_attempts, "last_error": reason},
        route="retry",
    )

  # Tên node phải duy nhất trong graph: FunctionNode lấy tên từ __name__
  # của hàm, mà cả ba route node đều dùng chung hàm route_node nên phải
  # đổi tên theo loop (route_header / route_merge / route_schema).
  route_node.__name__ = "route_" + attempts_key.replace("_attempts", "")
  return route_node


def make_route_header(config: IngestConfig) -> Callable:
  """Tạo route node cho loop header.

  Args:
    config: Cấu hình ingest.

  Returns:
    Hàm route node cho loop header.
  """
  return _make_route(config, "header_attempts")


def make_route_merge(config: IngestConfig) -> Callable:
  """Tạo route node cho loop gộp.

  Args:
    config: Cấu hình ingest.

  Returns:
    Hàm route node cho loop gộp.
  """
  return _make_route(config, "merge_attempts")


def mark_header_fallback(raw_tables: list[dict] | None = None) -> Event:
  """Ghi fallback header: quay về header ứng viên từ luật scoring.

  Fallback này được phép vì nó là luật đã kiểm chứng bằng mẫu (điểm
  scoring của phase1), không phải đoán đáp án: khi LLM hết 3 lần vẫn sai
  thì header ứng viên vẫn tốt hơn là dừng cả shop vì một bảng.

  Args:
    raw_tables: Danh sách RawTable đã dump (lấy từ state).

  Returns:
    Một Event duy nhất mang output ghi nhận fallback và state
    (headers_final = header ứng viên, cờ header_fallback).
  """
  tables = raw_tables or []
  final = {table["table_id"]: table["header"] for table in tables}
  return Event(
      output={"fallback": "header_rule"},
      state={"headers_final": final, "header_fallback": True, "last_error": ""},
  )


def mark_merge_fallback(raw_tables: list[dict] | None = None) -> Event:
  """Ghi fallback gộp: giữ mỗi bảng riêng, không gộp gì cả.

  Không gộp an toàn hơn gộp sai: gộp sai gây đếm trùng, còn không gộp
  chỉ làm schema có nhiều bảng hơn (bước sinh SQL vẫn truy vấn được từng
  bảng). Đây là phương án an toàn đã kiểm chứng, được ghi rõ trong log.

  Args:
    raw_tables: Danh sách RawTable đã dump (lấy từ state).

  Returns:
    Một Event duy nhất mang output ghi nhận fallback và state
    (merge_groups = mỗi bảng một nhóm, cờ merge_fallback).
  """
  tables = raw_tables or []
  groups = {
      "groups": [
          {"tables": [table["table_id"]], "evidence": "giữ riêng fallback"}
          for table in tables
      ],
      "singles": [],
  }
  return Event(
      output={"fallback": "merge_none"},
      state={"merge_groups": groups, "merge_fallback": True, "last_error": ""},
  )


def mark_shop_failed(last_error: str = "") -> Event:
  """Ghi kết luận shop FAILED (không publish DB, không xuất schema xấu).

  Node này là đích của mọi nhánh fail cuối (schema fail, validate_shop
  fail): nó chỉ ghi trạng thái vào state, không raise exception, để
  node finalize vẫn chạy và driver vẫn đi tiếp sang shop khác.

  Args:
    last_error: Lý do lỗi gần nhất (lấy từ state).

  Returns:
    Một Event duy nhất mang output trạng thái failed và state
    (shop_status = failed, shop_error = lý do).
  """
  return Event(
      output={"shop_status": "failed"},
      state={"shop_status": "failed", "shop_error": last_error},
  )
