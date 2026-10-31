# -*- coding: utf-8 -*-
"""Workflow ingest: NƠI DUY NHẤT ráp graph (không node nào tự ráp graph).

Graph tuân thủ hợp đồng CODE_DESIGN §6:
- START -> init_shop -> branch_input -> {sqlite: scan_sqlite,
  default: scan_sheets} -> header_gate -> {agent: header loop,
  skip: merge_gate} -> {agent: merge loop, skip: clean_tables} ->
  write_sqlite_tmp -> init_schema_tables -> {tables: schema table loop,
  empty: assemble_schema} -> {done: init_schema_meta} ->
  schema meta loop -> assemble_schema -> validate_shop -> shop_route ->
  {ok: publish_shop, fail: mark_shop_failed} -> finalize_shop.
- Vòng lặp schema theo từng bảng: schema_table_agent chỉ nhận một
  bảng mỗi lần (không còn SchemaAgent viết toàn bộ schema một lần như
  trước — chính lần viết đó bị cắt ở biz_123 vì 17 bảng quá dài);
  route_schema_table phát "retry" (cùng bảng), "next" (bảng sau) và
  "done" (hết bảng), hết lượt thì fallback bảng đó về bản nháp đo
  được rồi đi tiếp, không fail cả shop.
- finalize_shop là node terminal duy nhất (không có cạnh đi ra).
- Mọi cạnh đi ra của route node đều mang route ("ok"/"retry"/"fail",
  "agent"/"skip", "sqlite"/"default"): cycle phải có cạnh có route.
- Cùng một object agent được dùng cho cạnh vào lần đầu và cạnh loop quay
  lại (retry trỏ về đúng object agent đó), không tạo agent mới mỗi vòng.
- Cùng nguyên tắc cho mọi node: mỗi node chỉ được tạo đúng MỘT object
  rồi tái sử dụng ở mọi cạnh (graph của ADK phân biệt node theo object
  id, nên tạo hai object cùng tên sẽ thành hai node khác nhau và graph
  không nối đúng).
- Không dùng LoopAgent/SequentialAgent/ParallelAgent (đã deprecated),
  không dùng RetryConfig cho vòng viết lại (RetryConfig chỉ cho lỗi
  transient khi node raise; vòng viết lại ở đây là route có điều kiện).
"""

from __future__ import annotations

from pathlib import Path

from google.adk import Workflow
from google.adk.models.base_llm import BaseLlm

from ._config import IngestConfig
from .agents._header_agent import build_header_agent
from .agents._merge_agent import build_merge_agent
from .agents._schema_meta_agent import build_schema_meta_agent
from .agents._schema_table_agent import build_schema_table_agent
from .nodes._clean_nodes import make_clean_tables
from .nodes._scan_nodes import (
    branch_input,
    header_gate,
    make_init_shop,
    make_scan_sheets,
    make_scan_sqlite,
    merge_gate,
)
from .nodes._sqlite_nodes import (
    finalize_shop,
    init_schema_meta,
    init_schema_tables,
    make_assemble_schema,
    make_write_sqlite_tmp,
    publish_shop,
    shop_route,
    validate_shop,
)
from .nodes._validate_nodes import (
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


def build_ingest_workflow(
    config: IngestConfig | None = None,
    model: BaseLlm | str | None = None,
    skills_dir: str | Path | None = None,
) -> Workflow:
  """Ráp Workflow ingest cho một shop.

  Args:
    config: Cấu hình ingest; None thì dùng IngestConfig mặc định.
    model: Instance BaseLlm (fake trong test, LiteLlm thật khi chạy
      thật) hoặc tên model, truyền cho cả 4 agent; None thì mỗi agent
      dùng config.model_id.
    skills_dir: Thư mục cha chứa skill; None thì dùng thư mục skills/
      cạnh package ingest.

  Returns:
    Workflow đã ráp xong, sẵn sàng chạy qua Runner với session có state
    ban đầu (business_id, shop_dir, output_dir).
  """
  cfg = config or IngestConfig()
  # Tạo mỗi node đúng một lần, rồi tái sử dụng object đó ở mọi cạnh.
  init_shop = make_init_shop(cfg)
  scan_sqlite = make_scan_sqlite(cfg)
  scan_sheets = make_scan_sheets(cfg)
  header_agent = build_header_agent(cfg, model=model, skills_dir=skills_dir)
  route_header = make_route_header(cfg)
  merge_agent = build_merge_agent(cfg, model=model, skills_dir=skills_dir)
  route_merge = make_route_merge(cfg)
  clean_tables = make_clean_tables(cfg)
  write_sqlite_tmp = make_write_sqlite_tmp(cfg)
  schema_table_agent = build_schema_table_agent(cfg, model=model, skills_dir=skills_dir)
  route_schema_table = make_route_schema_table(cfg)
  schema_meta_agent = build_schema_meta_agent(cfg, model=model, skills_dir=skills_dir)
  route_schema_meta = make_route_schema_meta(cfg)
  assemble_schema = make_assemble_schema(cfg)
  edges: list = [
      ("START", init_shop, branch_input,
       {"sqlite": scan_sqlite, "default": scan_sheets}),
      # Hai nhánh quét đều dẫn tới header_gate (nhánh loại trừ: chỉ một
      # nhánh chạy nên header_gate trigger khi nhánh chạy xong, không
      # cần JoinNode — node thường của ADK trigger khi bất kỳ tiền thân
      # nào hoàn thành).
      (scan_sqlite, header_gate),
      (scan_sheets, header_gate),
      (header_gate, {"agent": header_agent, "skip": merge_gate}),
      (header_agent, validate_header, route_header,
       {"retry": header_agent, "ok": merge_gate, "fail": mark_header_fallback}),
      (mark_header_fallback, merge_gate),
      (merge_gate, {"agent": merge_agent, "skip": clean_tables}),
      (merge_agent, validate_merge, route_merge,
       {"retry": merge_agent, "ok": clean_tables, "fail": mark_merge_fallback}),
      (mark_merge_fallback, clean_tables),
      (clean_tables, write_sqlite_tmp, init_schema_tables,
       {"tables": schema_table_agent, "empty": assemble_schema}),
      (schema_table_agent, validate_schema_table, route_schema_table,
       {"retry": schema_table_agent, "next": schema_table_agent,
        "done": init_schema_meta}),
      (init_schema_meta, schema_meta_agent, validate_schema_meta,
       route_schema_meta,
       {"retry": schema_meta_agent, "ok": assemble_schema,
        "fail": mark_schema_meta_fallback}),
      (mark_schema_meta_fallback, assemble_schema),
      (assemble_schema, validate_shop, shop_route,
       {"ok": publish_shop, "fail": mark_shop_failed}),
      (publish_shop, finalize_shop),
      (mark_shop_failed, finalize_shop),
  ]
  return Workflow(name="datamate_ingest", edges=edges)
