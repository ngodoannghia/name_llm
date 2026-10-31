# -*- coding: utf-8 -*-
"""Các node SQLite: ghi DB tạm, kiểm chứng shop, publish, finalize.

Module này thuộc tầng ghi và kiểm chứng cuối. Nguyên tắc publish nguyên
tử (xem CODE_DESIGN §2 nguyên tắc 4): node ghi chỉ ghi DB tạm, và chỉ
node publish cuối cùng (sau khi validate_shop đạt) mới rename nguyên tử
thành DB chính thức. Không node nào ghi đè trực tiếp lên DB chính thức.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from google.adk import Event

from .._config import IngestConfig
from .._models import IngestReport, ShopSchema, TableSchema, Verdict


def make_write_sqlite_tmp(config: IngestConfig) -> Callable:
  """Tạo node write_sqlite_tmp đã gắn cấu hình distinct.

  Args:
    config: Cấu hình ingest (ngưỡng distinct liệt kê đầy đủ).

  Returns:
    Hàm node write_sqlite_tmp dùng được trực tiếp trong edges.
  """

  async def write_sqlite_tmp(
      manifest: list[dict] | None = None,
      staging_dir: str = "",
      output_dir: str = "",
      business_id: str = "",
  ) -> Event:
    """Ghi DB tạm từ staging và tạo bản nháp schema từ DB thật.

    Bản nháp schema được đo từ DB tạm (mẫu, distinct, số dòng đều truy
    vấn thật), không phải do node này đoán: SchemaAgent về sau chỉ thêm
    mô tả tiếng Việt, role_hint và join hint lên trên bản nháp này, còn
    validator sẽ kiểm lại mọi mẫu/distinct trong bản nháp.

    Args:
      manifest: Manifest staging (lấy từ state).
      staging_dir: Thư mục staging chứa CSV + manifest (lấy từ state).
      output_dir: Thư mục ghi kết quả (lấy từ state).
      business_id: Id shop (lấy từ state).

    Returns:
      Một Event duy nhất mang output {"tmp_db_path", "schema_draft"} và
      state tương ứng (thêm schema_draft_text dạng JSON chuỗi để
      SchemaAgent đọc qua instruction placeholder).
    """
    tables_manifest = manifest or []
    tmp_db_path = str(Path(output_dir) / f"{business_id}.tmp.db")

    def _run() -> dict:
      """Phần đồng bộ ghi DB tạm và đo bản nháp (chạy trong thread riêng)."""
      tmp_path = Path(tmp_db_path)
      if tmp_path.exists():
        # Ghi lại từ đầu cho idempotent: lần chạy fresh sau lỗi không
        # cộng dồn dữ liệu cũ vào DB tạm.
        tmp_path.unlink()
      connection = sqlite3.connect(str(tmp_path))
      try:
        cursor = connection.cursor()
        for table in tables_manifest:
          sqlite_table = table["sqlite_table"]
          columns = table["columns"]
          column_defs = ", ".join(
              f'"{col["sqlite_name"]}" {col["type"]}' for col in columns
          )
          cursor.execute(f'CREATE TABLE "{sqlite_table}" ({column_defs})')
          staging_file = Path(staging_dir) / f"{sqlite_table}.csv"
          frame = pd.read_csv(
              staging_file, dtype=str, keep_default_na=False, encoding="utf-8"
          )
          for col in columns:
            name = col["sqlite_name"]
            if name not in frame.columns:
              frame[name] = None
          records = []
          for row in frame.itertuples(index=False, name=None):
            values: list[object] = []
            for idx, col in enumerate(columns):
              raw = row[idx]
              if raw == "" or raw is None:
                values.append(None)
              elif col["type"] == "INTEGER":
                try:
                  values.append(int(float(raw)))
                except (ValueError, TypeError):
                  values.append(None)
              elif col["type"] == "REAL":
                try:
                  values.append(float(raw))
                except (ValueError, TypeError):
                  values.append(None)
              else:
                values.append(raw)
            records.append(tuple(values))
          placeholders = ", ".join(["?"] * len(columns))
          col_names = ", ".join(f'"{col["sqlite_name"]}"' for col in columns)
          cursor.executemany(
              f'INSERT INTO "{sqlite_table}" ({col_names}) VALUES ({placeholders})',
              records,
          )
        # Ba bảng meta của DB (xem plan §5 N5): hồ sơ shop, map tên,
        # thống kê làm sạch. Bảng meta không nằm trong schema file vì
        # chúng là dữ liệu vận hành, không phải dữ liệu nghiệp vụ.
        cursor.execute(
            "CREATE TABLE _shop_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        generated_at = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "INSERT INTO _shop_meta (key, value) VALUES (?, ?)",
            ("business_id", business_id),
        )
        cursor.execute(
            "INSERT INTO _shop_meta (key, value) VALUES (?, ?)",
            ("generated_at", generated_at),
        )
        cursor.execute(
            "CREATE TABLE _schema_map "
            "(table_id TEXT, sqlite_table TEXT, original_name TEXT, "
            "sqlite_name TEXT, type TEXT, semantic TEXT)"
        )
        for table in tables_manifest:
          for col in table["columns"]:
            cursor.execute(
                "INSERT INTO _schema_map VALUES (?, ?, ?, ?, ?, ?)",
                (
                    table["table_id"],
                    table["sqlite_table"],
                    col["original_name"],
                    col["sqlite_name"],
                    col["type"],
                    col.get("semantic", ""),
                ),
            )
        cursor.execute(
            "CREATE TABLE _clean_stats "
            "(table_id TEXT, rows_in INTEGER, rows_out INTEGER, "
            "null_cells INTEGER, unparseable_cells INTEGER)"
        )
        for table in tables_manifest:
          stats = table["stats"]
          cursor.execute(
              "INSERT INTO _clean_stats VALUES (?, ?, ?, ?, ?)",
              (
                  table["table_id"],
                  stats["rows_in"],
                  stats["rows_out"],
                  stats["null_cells"],
                  stats["unparseable_cells"],
              ),
          )
        connection.commit()
        # Đo bản nháp schema từ DB thật (mẫu, distinct, số dòng).
        draft_tables: list[dict] = []
        for table in tables_manifest:
          sqlite_table = table["sqlite_table"]
          cursor.execute(f'SELECT COUNT(*) FROM "{sqlite_table}"')
          row_count = cursor.fetchone()[0]
          draft_columns: list[dict] = []
          for col in table["columns"]:
            name = col["sqlite_name"]
            cursor.execute(
                f'SELECT DISTINCT "{name}" FROM "{sqlite_table}" '
                f'WHERE "{name}" IS NOT NULL LIMIT 5'
            )
            samples = [
                "" if value is None else str(value)
                for value in [row[0] for row in cursor.fetchall()]
            ]
            cursor.execute(
                f'SELECT COUNT(DISTINCT "{name}") FROM "{sqlite_table}"'
            )
            distinct_count = cursor.fetchone()[0]
            distinct_values: list[str] | None = None
            if distinct_count <= config.distinct_full_limit:
              cursor.execute(
                  f'SELECT DISTINCT "{name}" FROM "{sqlite_table}" '
                  f'WHERE "{name}" IS NOT NULL'
              )
              distinct_values = [
                  "" if value is None else str(value)
                  for value in [row[0] for row in cursor.fetchall()]
              ]
            cursor.execute(
                f'SELECT COUNT(*) FROM "{sqlite_table}" '
                f'WHERE "{name}" IS NULL'
            )
            null_count = cursor.fetchone()[0]
            nullable_rate = (null_count / row_count) if row_count else 0.0
            draft_columns.append(
                {
                    "original_name": col["original_name"],
                    "sqlite_name": name,
                    "type": col["type"],
                    "description_vi": "",
                    "role_hint": "unknown",
                    "sample_values": samples,
                    "distinct_count": distinct_count,
                    "distinct_values": distinct_values,
                    "nullable_rate": nullable_rate,
                }
            )
          draft_tables.append(
              {
                  "table_id": table["table_id"],
                  "original_names": table["source_tables"],
                  "source_sheets": table["source_tables"],
                  "sqlite_table": sqlite_table,
                  "row_count": row_count,
                  "description_vi": "",
                  "columns": draft_columns,
                  "date_range": {},
              }
          )
        return {"tables": draft_tables}
      finally:
        connection.close()

    # _run đã trả {"tables": [...]} nên dùng trực tiếp, không bọc thêm
    # một lớp {"tables": ...} nữa (bọc hai lần sẽ làm schema_draft sai
    # dạng và validator schema về sau không đọc được).
    draft = await asyncio.to_thread(_run)
    output = {"tmp_db_path": tmp_db_path, "schema_draft": draft}
    state = {
        "tmp_db_path": tmp_db_path,
        "schema_draft": draft,
        "schema_draft_text": json.dumps(draft, ensure_ascii=False),
    }
    return Event(output=output, state=state)

  return write_sqlite_tmp


async def init_schema_tables(
    schema_draft: dict | None = None,
) -> Event:
  """Khởi tạo vòng lặp schema theo từng bảng từ bản nháp đã đo.

  Node này là gate của vòng lặp từng bảng: nó tách danh sách bảng
  trong bản nháp thành state riêng cho vòng lặp, đặt vị trí về 0 và
  chuẩn bị bản nháp JSON của bảng đầu tiên cho agent. Khi shop không
  có bảng nào thì route "empty" đi thẳng tới assemble_schema (nơi
  này sẽ không ghi file, để validate_shop fail shop như trước), tránh
  việc agent chạy với input rỗng rồi raise vì output không coerce
  được — node raise sẽ dừng cả workflow, khác với fail-as-route.

  Args:
    schema_draft: Bản nháp schema {"tables": [...]} (lấy từ state).

  Returns:
    Một Event duy nhất mang state vòng lặp và route "tables" (có
    bảng) hoặc "empty" (không có bảng nào).
  """
  drafts = (schema_draft or {}).get("tables", [])
  base_state: dict = {
      "schema_table_drafts": drafts,
      "schema_table_index": 0,
      "schema_table_results": [],
      "schema_table_fallbacks": [],
      "schema_table_attempts": 0,
  }
  if not drafts:
    return Event(output={"schema_tables": 0}, state=base_state, route="empty")
  base_state["schema_table_draft_text"] = json.dumps(
      {"table": drafts[0]}, ensure_ascii=False
  )
  return Event(
      output={"schema_tables": len(drafts)},
      state=base_state,
      route="tables",
  )


async def init_schema_meta(
    schema_table_results: list[dict] | None = None,
) -> Event:
  """Chuẩn bị tóm tắt gọn cho agent meta từ kết quả từng bảng.

  Tóm tắt chỉ giữ tên bảng, tên cột và role_hint — cố tình bỏ mẫu và
  distinct để input của agent meta luôn nhỏ (dù shop có 17 bảng thì
  tóm tắt cũng chỉ vài nghìn ký tự), vì agent meta chỉ cần tên và vai
  trò cột để suy luận join và bẫy, không cần giá trị mẫu.

  Args:
    schema_table_results: Kết quả các bảng đã chốt (lấy từ state).

  Returns:
    Một Event duy nhất mang state schema_meta_summary_text (JSON
    {"tables_summary": [...]}).
  """
  summary = {
      "tables_summary": [
          {
              "sqlite_table": table.get("sqlite_table", ""),
              "columns": [
                  {
                      "sqlite_name": col.get("sqlite_name", ""),
                      "role_hint": col.get("role_hint", "unknown"),
                  }
                  for col in table.get("columns", [])
              ],
          }
          for table in (schema_table_results or [])
      ]
  }
  return Event(
      output={"tables_summary": len(summary["tables_summary"])},
      state={
          "schema_meta_summary_text": json.dumps(summary, ensure_ascii=False)
      },
  )


def make_assemble_schema(config: IngestConfig) -> Callable:
  """Tạo node assemble_schema đã gắn cấu hình (tên model ghi vào schema).

  Args:
    config: Cấu hình ingest; model_id của nó được ghi vào trường
      model của file schema để truy vết model nào đã sinh schema
      (trước đây agent tự ghi tên mẫu từ ví dụ trong skill).

  Returns:
    Hàm node assemble_schema dùng được trực tiếp trong edges.
  """

  async def assemble_schema(
      business_id: str = "",
      output_dir: str = "",
      shop_dir: str = "",
      schema_table_results: list[dict] | None = None,
      schema_meta: dict | None = None,
      schema_table_fallbacks: list[str] | None = None,
  ) -> Event:
    """Ráp file schema cuối từ kết quả từng bảng và phần meta.

    Node này là nơi DUY NHẤT ghi file schema: nó lấy các bảng đã
    chốt ở vòng lặp từng bảng (giá trị đo giữ nguyên, mô tả/role do
    model bổ sung, bảng fallback thì giữ nguyên bản nháp), cộng phần
    meta (join_hints, notes) rồi ghi một file JSON duy nhất. Nhờ ráp
    bằng code mà model không bao giờ phải viết lại toàn bộ schema:
    mỗi lần gọi model chỉ xử lý một bảng hoặc một phần meta nhỏ.

    Args:
      business_id: Id shop (lấy từ state).
      output_dir: Thư mục ghi kết quả (lấy từ state).
      shop_dir: Thư mục dữ liệu gốc của shop (lấy từ state, dùng để
        liệt kê tên file nguồn thật).
      schema_table_results: Kết quả các bảng đã chốt (lấy từ state).
      schema_meta: Phần meta đã chốt (lấy từ state, có thể rỗng khi
        fallback).
      schema_table_fallbacks: Id các bảng fallback về bản nháp (lấy
        từ state, dùng để chú thích vào notes).

    Returns:
      Một Event duy nhất mang output Verdict (ok khi ghi được file,
      fail khi không có bảng nào) và state schema_path (rỗng khi
      không ghi file, để validate_shop fail shop như trước).
    """
    results = schema_table_results or []
    if not results:
      return Event(
          output=Verdict(
              status="fail",
              reason="Không có bảng nào để ráp schema.",
          ).model_dump(),
          state={"schema_path": ""},
      )
    meta = schema_meta or {}
    notes = list(meta.get("notes", []))
    for table_id in schema_table_fallbacks or []:
      notes.append(
          f"Bảng {table_id} dùng bản nháp đo được (model hết lượt thử, "
          "chưa có mô tả)."
      )
    source_files: list[str] = []
    if shop_dir:
      shop_path = Path(shop_dir)
      if shop_path.is_dir():
        # Chỉ liệt kê file dữ liệu: README của shop là tài liệu mô tả,
        # không phải bảng dữ liệu, nên không đưa vào source_files.
        source_files = sorted(
            entry.name
            for entry in shop_path.iterdir()
            if entry.is_file() and entry.name.lower() != "readme.md"
        )
    schema = ShopSchema(
        business_id=business_id,
        source_files=source_files,
        generated_at=datetime.now(timezone.utc).isoformat(),
        model=config.model_id,
        tables=[TableSchema(**table) for table in results],
        join_hints=meta.get("join_hints", []),
        notes=notes,
    )
    schema_dir = Path(output_dir) / "schemas"
    schema_path = schema_dir / f"{business_id}.json"

    def _write() -> None:
      """Ghi file schema (chạy trong thread riêng vì là I/O đĩa)."""
      schema_dir.mkdir(parents=True, exist_ok=True)
      text = schema.model_dump_json(indent=2, ensure_ascii=False)
      schema_path.write_text(text + "\n", encoding="utf-8")

    await asyncio.to_thread(_write)
    return Event(
        output=Verdict(status="ok", reason="Schema đã ráp xong.").model_dump(),
        state={
            "schema_path": str(schema_path),
            "schema_json": schema.model_dump(),
            "last_error": "",
        },
    )

  return assemble_schema


async def validate_shop(
    node_input: Verdict,
    tmp_db_path: str = "",
    schema_path: str = "",
    business_id: str = "",
) -> Verdict:
  """Kiểm chứng cuối shop: DB tạm mở được và schema file khớp shop.

  Node này chạy sau khi route schema đã đạt (node_input là Verdict ok
  của route đó); nó kiểm chứng độc lập một lần nữa trước khi publish,
  vì schema file và DB tạm là hai thứ khác nhau và đều phải đúng thì
  mới được publish. Kiểm tra gồm: DB tạm tồn tại và mở được, schema
  file tồn tại và parse được, business_id trong schema khớp shop đang
  chạy, tập bảng trong schema khớp tập bảng trong DB (trừ bảng meta).

  Args:
    node_input: Verdict từ route schema (chỉ để nối graph, không dùng
      để quyết định — quyết định dựa trên kiểm tra thật ở đây).
    tmp_db_path: Đường dẫn DB tạm (lấy từ state).
    schema_path: Đường dẫn file schema (lấy từ state).
    business_id: Id shop đang chạy (lấy từ state).

  Returns:
    Verdict ok khi mọi kiểm tra đạt, ngược lại Verdict fail kèm lý do.
    Fail ở đây đi tới mark_shop_failed qua shop_route, không raise.
  """
  _ = node_input

  def _check() -> str:
    """Phần đồng bộ kiểm tra (chạy trong thread riêng vì là I/O)."""
    if not tmp_db_path or not Path(tmp_db_path).is_file():
      return "Không tìm thấy DB tạm để kiểm chứng shop."
    if not schema_path or not Path(schema_path).is_file():
      return "Không tìm thấy file schema để kiểm chứng shop."
    try:
      schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
      return f"File schema không parse được thành JSON: {error}."
    if schema.get("business_id") != business_id:
      return "business_id trong schema không khớp shop đang chạy."
    connection = sqlite3.connect(f"file:{tmp_db_path}?mode=ro", uri=True)
    try:
      cursor = connection.cursor()
      cursor.execute(
          "SELECT name FROM sqlite_master "
          "WHERE type = 'table' AND name NOT LIKE '\\_%' ESCAPE '\\' "
          "AND name NOT LIKE 'sqlite_%'"
      )
      db_tables = {row[0] for row in cursor.fetchall()}
    finally:
      connection.close()
    schema_tables = {table.get("sqlite_table") for table in schema.get("tables", [])}
    if schema_tables != db_tables:
      return (
          f"Bảng trong schema {sorted(schema_tables)} không khớp bảng "
          f"trong DB {sorted(db_tables)}."
      )
    return ""

  problem = await asyncio.to_thread(_check)
  if problem:
    return Verdict(status="fail", reason=problem)
  return Verdict(status="ok", reason="DB tạm và schema file khớp nhau.")


def shop_route(node_input: Verdict) -> Event:
  """Rẽ nhánh sau validate_shop: đạt thì publish, không đạt thì FAILED.

  Args:
    node_input: Verdict từ validate_shop.

  Returns:
    Một Event duy nhất mang Verdict và route "ok" hoặc "fail".
  """
  route = "ok" if node_input.status == "ok" else "fail"
  return Event(output=node_input.model_dump(), route=route)


def publish_shop(
    tmp_db_path: str = "",
    output_dir: str = "",
    business_id: str = "",
) -> Event:
  """Publish DB: rename nguyên tử DB tạm thành DB chính thức.

  Rename nguyên tử (os.replace) đảm bảo người đọc không bao giờ thấy DB
  dở dang: hoặc là DB cũ (nếu có), hoặc là DB mới đầy đủ, không có
  trạng thái giữa. DB tạm sau rename không còn tồn tại nữa.

  Args:
    tmp_db_path: Đường dẫn DB tạm (lấy từ state).
    output_dir: Thư mục ghi kết quả (lấy từ state).
    business_id: Id shop (lấy từ state).

  Returns:
    Một Event duy nhất mang output db_path và state db_path.

  Raises:
    RuntimeError: Khi DB tạm không tồn tại (validate_shop đã đạt mà
      vẫn mất DB tạm thì đây là lỗi môi trường, phải dừng thật).
  """
  if not tmp_db_path or not Path(tmp_db_path).is_file():
    raise RuntimeError("Không tìm thấy DB tạm để publish (đã qua validate_shop).")
  db_path = str(Path(output_dir) / f"{business_id}.db")
  os.replace(tmp_db_path, db_path)
  return Event(output={"db_path": db_path}, state={"db_path": db_path})


def finalize_shop(
    business_id: str = "",
    db_path: str = "",
    schema_path: str = "",
    shop_status: str = "",
    shop_error: str = "",
    manifest: list[dict] | None = None,
) -> Event:
  """Node terminal duy nhất: tổng hợp IngestReport cho shop.

  Node này chạy ở cả hai đường (ok và failed) vì cả publish_shop và
  mark_shop_failed đều dẫn tới nó. Khi failed thì db_path/schema_path
  trong report để rỗng (không xuất DB xấu), và lý do lỗi nằm trong
  report để driver ghi log tổng hợp.

  Args:
    business_id: Id shop (lấy từ state).
    db_path: Đường dẫn DB chính thức (lấy từ state, rỗng khi failed).
    schema_path: Đường dẫn file schema (lấy từ state, rỗng khi failed).
    shop_status: Trạng thái do mark_shop_failed ghi (lấy từ state, rỗng
      nghĩa là đường ok).
    shop_error: Lý do lỗi (lấy từ state, rỗng khi ok).
    manifest: Manifest staging (lấy từ state, dùng để đếm bảng/dòng).

  Returns:
    Một Event duy nhất mang output là IngestReport đã dump và state
    shop_status cuối cùng. Đây là output của workflow.
  """
  status = "failed" if shop_status == "failed" else "ok"
  tables = manifest or []
  report = IngestReport(
      business_id=business_id,
      status=status,  # type: ignore[arg-type]
      db_path=db_path if status == "ok" else "",
      schema_path=schema_path if status == "ok" else "",
      table_count=len(tables) if status == "ok" else 0,
      row_count=sum(table.get("row_count", 0) for table in tables)
      if status == "ok"
      else 0,
      error=shop_error if status == "failed" else "",
  )
  return Event(
      output=report.model_dump(),
      state={"shop_status": status},
  )
