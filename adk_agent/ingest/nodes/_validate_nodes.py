# -*- coding: utf-8 -*-
"""Các node validator, route và fallback cho 3 loop của workflow ingest.

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
import sqlite3
from collections.abc import Callable
from pathlib import Path

from google.adk import Event

from .._config import IngestConfig
from .._models import (
    HeaderProposal,
    MergeProposal,
    RawTable,
    ShopSchema,
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


async def validate_schema(
    node_input: ShopSchema,
    tmp_db_path: str = "",
    business_id: str = "",
    output_dir: str = "",
) -> Event:
  """Kiểm chứng schema do SchemaAgent đề xuất rồi ghi file schema khi đạt.

  Kiểm chứng bằng truy vấn thật trên DB tạm (không tin lời agent):
  - business_id trong schema phải khớp shop đang chạy.
  - Tập bảng trong schema phải khớp tập bảng trong DB (trừ bảng meta _*).
  - Tập cột mỗi bảng phải khớp cột thật trong DB (PRAGMA table_info).
  - Mỗi sample_values phải tồn tại trong cột (so sánh dạng TEXT).
  - Nếu distinct_values được liệt kê thì số lượng phải khớp
    COUNT(DISTINCT) đo được và từng giá trị phải tồn tại.

  File schema chỉ được ghi khi mọi kiểm tra đạt: schema sai thì workflow
  trả lời về sau sẽ sinh SQL sai, nên không được xuất schema xấu.

  Args:
    node_input: Schema đề xuất (đã coerce thành ShopSchema).
    tmp_db_path: Đường dẫn DB tạm (lấy từ state).
    business_id: Id shop đang chạy (lấy từ state).
    output_dir: Thư mục ghi kết quả (lấy từ state).

  Returns:
    Một Event duy nhất mang Verdict và state (schema_path khi đạt;
    nhánh fail không ghi last_error — route node tự ghi).
  """
  import asyncio

  def _fail(reason: str) -> Event:
    """Tạo Event Verdict fail (không ghi last_error — route tự ghi, xem
    docstring của _verdict_event)."""
    return _verdict_event(Verdict(status="fail", reason=reason))

  if node_input.business_id != business_id:
    return _fail(
        f"Schema ghi business_id {node_input.business_id} nhưng shop đang "
        f"chạy là {business_id}."
    )
  if not node_input.tables:
    return _fail("Schema không chứa bảng nào.")
  if not tmp_db_path or not Path(tmp_db_path).is_file():
    return _fail("Không tìm thấy DB tạm để kiểm chứng schema.")

  def _check() -> str:
    """Phần đồng bộ kiểm tra DB (chạy trong thread riêng).

    Returns:
      Chuỗi rỗng khi đạt, ngược lại là lý do fail.
    """
    connection = sqlite3.connect(f"file:{tmp_db_path}?mode=ro", uri=True)
    try:
      cursor = connection.cursor()
      cursor.execute(
          "SELECT name FROM sqlite_master "
          "WHERE type = 'table' AND name NOT LIKE '\\_%' ESCAPE '\\' "
          "AND name NOT LIKE 'sqlite_%'"
      )
      db_tables = {row[0] for row in cursor.fetchall()}
      schema_tables = {table.sqlite_table for table in node_input.tables}
      if schema_tables != db_tables:
        return (
            f"Bảng trong schema {sorted(schema_tables)} không khớp bảng "
            f"trong DB {sorted(db_tables)}."
        )
      for table in node_input.tables:
        cursor.execute(f'PRAGMA table_info("{table.sqlite_table}")')
        db_columns = {row[1] for row in cursor.fetchall()}
        schema_columns = {col.sqlite_name for col in table.columns}
        if schema_columns != db_columns:
          return (
              f"Cột của bảng {table.sqlite_table} trong schema "
              f"{sorted(schema_columns)} không khớp cột trong DB "
              f"{sorted(db_columns)}."
          )
        for col in table.columns:
          for sample in col.sample_values:
            cursor.execute(
                f'SELECT 1 FROM "{table.sqlite_table}" '
                f"WHERE CAST(\"{col.sqlite_name}\" AS TEXT) = ? LIMIT 1",
                (sample,),
            )
            if cursor.fetchone() is None:
              return (
                  f"Mẫu {sample!r} của cột {col.sqlite_name} (bảng "
                  f"{table.sqlite_table}) không tồn tại trong DB."
              )
          if col.distinct_values is not None:
            cursor.execute(
                f'SELECT COUNT(DISTINCT "{col.sqlite_name}") '
                f'FROM "{table.sqlite_table}"'
            )
            counted = cursor.fetchone()[0]
            if counted != len(col.distinct_values):
              return (
                  f"Số distinct của cột {col.sqlite_name} (bảng "
                  f"{table.sqlite_table}) đo được là {counted} nhưng schema "
                  f"liệt kê {len(col.distinct_values)} giá trị."
              )
      return ""
    finally:
      connection.close()

  problem = await asyncio.to_thread(_check)
  if problem:
    return _fail(problem)
  schema_dir = Path(output_dir) / "schemas"
  schema_path = schema_dir / f"{business_id}.json"

  def _write() -> None:
    """Ghi file schema (chạy trong thread riêng vì là I/O đĩa)."""
    schema_dir.mkdir(parents=True, exist_ok=True)
    text = node_input.model_dump_json(indent=2, ensure_ascii=False)
    schema_path.write_text(text + "\n", encoding="utf-8")

  await asyncio.to_thread(_write)
  return _verdict_event(
      Verdict(status="ok", reason="Schema khớp DB tạm."),
      state={
          "schema_path": str(schema_path),
          "schema_json": node_input.model_dump(),
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


def make_route_schema(config: IngestConfig) -> Callable:
  """Tạo route node cho loop schema.

  Args:
    config: Cấu hình ingest.

  Returns:
    Hàm route node cho loop schema.
  """
  return _make_route(config, "schema_attempts")


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
