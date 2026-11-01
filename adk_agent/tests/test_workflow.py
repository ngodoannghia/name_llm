# -*- coding: utf-8 -*-
"""Test workflow đầu-cuối bằng InMemoryRunner + FakeLlm (không model thật).

Ba kịch bản:
1. Shop CSV sạch 1 bảng: đi nhánh skip cả hai gate, publish thành công.
2. Shop CSV có dòng tiêu đề (header mơ hồ) + canned header sai rồi đúng:
   loop header retry 1 lần rồi ok, cuối cùng vẫn publish thành công.
3. Shop CSV 2 bảng + canned merge sai lặp lại: route fail sớm, fallback
   giữ riêng, cuối cùng vẫn publish thành công.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from google.adk.runners import InMemoryRunner
from google.genai import types

from ingest import build_ingest_workflow, FakeLlm, IngestConfig

CONFIG = IngestConfig()


def _write_csv(path: Path, rows: list[list[str]]) -> None:
  """Ghi file CSV giả lập."""
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open("w", encoding="utf-8", newline="") as handle:
    csv.writer(handle).writerows(rows)


async def _run_workflow(
    shop_dir: Path, output_dir: Path, fake: FakeLlm
) -> tuple[dict, dict, list]:
  """Chạy workflow cho một shop giả lập, trả (report, state cuối, events).

  Args:
    shop_dir: Thư mục shop giả lập (tên thư mục là business_id).
    output_dir: Thư mục ghi kết quả.
    fake: FakeLlm dùng cho cả 3 agent.

  Returns:
    Tuple (report dict từ finalize_shop, state cuối của session, danh
    sách event thu được).
  """
  business_id = shop_dir.name
  workflow = build_ingest_workflow(config=CONFIG, model=fake)
  runner = InMemoryRunner(node=workflow, app_name="datamate_ingest")
  session = await runner.session_service.create_session(
      app_name="datamate_ingest",
      user_id="test",
      session_id=f"test-{business_id}",
      state={
          "business_id": business_id,
          "shop_dir": str(shop_dir),
          "output_dir": str(output_dir),
      },
  )
  events = []
  async for event in runner.run_async(
      user_id="test",
      session_id=session.id,
      new_message=types.Content(
          role="user", parts=[types.Part(text=f"Bắt đầu ingest shop {business_id}.")]
      ),
  ):
    events.append(event)
  final_session = await runner.session_service.get_session(
      app_name="datamate_ingest", user_id="test", session_id=session.id
  )
  report = {}
  for event in events:
    output = event.output
    if isinstance(output, dict) and "table_count" in output and "status" in output:
      report = output
  return report, dict(final_session.state or {}), events


async def test_workflow_clean_single_table_shop(tmp_path: Path) -> None:
  """Shop 1 bảng sạch chạy đầu-cuối: DB + schema + report ok."""
  shop_dir = tmp_path / "data" / "biz_clean"
  _write_csv(
      shop_dir / "don.csv",
      [["Mã đơn hàng", "Tổng tiền"], ["DH1", "100000"], ["DH2", "200000"]],
  )
  output_dir = tmp_path / "out"
  report, _state, events = await _run_workflow(shop_dir, output_dir, FakeLlm())
  assert report["status"] == "ok"
  assert report["table_count"] == 1
  assert report["row_count"] == 2
  db_path = Path(report["db_path"])
  assert db_path.is_file()
  schema_path = Path(report["schema_path"])
  assert schema_path.is_file()
  parsed = json.loads(schema_path.read_text(encoding="utf-8"))
  assert parsed["business_id"] == "biz_clean"
  connection = sqlite3.connect(str(db_path))
  rows = connection.execute('SELECT * FROM "don"').fetchall()
  assert len(rows) == 2
  connection.close()
  for event in events:
    json.loads(event.model_dump_json())


async def test_workflow_header_loop_retry_then_ok(tmp_path: Path) -> None:
  """Header sai lần 1 (canned) rồi đúng lần 2: loop retry rồi publish ok."""
  shop_dir = tmp_path / "data" / "biz_title"
  _write_csv(
      shop_dir / "don.csv",
      [["BÁO CÁO"], ["Mã đơn hàng", "Tổng tiền"], ["DH1", "100000"]],
  )
  output_dir = tmp_path / "out"
  fake = FakeLlm(
      canned=[
          {
              "tables": [{"table_id": "don", "header_row": 1, "columns": ["Mã đơn hàng", "Tiền sai"]}],
          },
          {
              "tables": [{"table_id": "don", "header_row": 1, "columns": ["Mã đơn hàng", "Tổng tiền"]}],
          },
      ]
  )
  report, state, _events = await _run_workflow(shop_dir, output_dir, fake)
  assert report["status"] == "ok"
  assert state.get("header_attempts") == 1


async def test_workflow_schema_table_truncated_json_then_ok(tmp_path: Path) -> None:
  """JSON schema cụt lần 1 (vụ biz_127) không được crash shop.

  Lần schema_table_agent đầu tiên trả JSON cụt qua canned: guard
  after_model phải đổi nó thành sentinel để validator bác bằng Verdict
  fail, route retry, lần 2 FakeLlm dispatch từ bản nháp trả schema đúng
  và shop vẫn publish ok. Trước khi có guard, lần này raise
  ValidationError tại node agent và giết cả workflow.
  """
  shop_dir = tmp_path / "data" / "biz_trunc"
  _write_csv(
      shop_dir / "don.csv",
      [["Mã đơn hàng", "Tổng tiền"], ["DH1", "100000"]],
  )
  output_dir = tmp_path / "out"
  truncated = '{\n  "table_id": "don",\n  "columns": [{"original_name": "Mã đơn hàng", '
  fake = FakeLlm(canned=[truncated])
  report, _state, events = await _run_workflow(shop_dir, output_dir, fake)
  assert report["status"] == "ok"
  assert report["table_count"] == 1
  # Bằng chứng đã retry: agent chạy 2 lượt (lần 1 sentinel bị bác, lần 2
  # dispatch từ bản nháp trả schema đúng), và có đúng một Verdict fail.
  # (Bộ đếm schema_table_attempts bị reset về 0 khi sang bảng mới nên
  # không dùng nó để khẳng định đã retry.)
  agent_turns = [e for e in events if e.author == "schema_table_agent"]
  assert len(agent_turns) == 2
  # (Route node cũng mang output là bản dump Verdict nên đếm fail theo
  # node validate, không đếm mọi event có status fail.)
  fail_verdicts = [
      e
      for e in events
      if isinstance(e.output, dict)
      and e.output.get("status") == "fail"
      and "validate_schema_table" in (getattr(e.node_info, "path", "") or "")
  ]
  assert len(fail_verdicts) == 1
  assert all(event.error_code is None for event in events)
  schema_path = Path(report["schema_path"])
  assert schema_path.is_file()


async def test_workflow_merge_repeated_error_fallback(tmp_path: Path) -> None:
  """Merge sai lặp lại 2 lần: fail sớm, fallback giữ riêng, vẫn publish ok."""
  shop_dir = tmp_path / "data" / "biz_two"
  _write_csv(
      shop_dir / "don.csv",
      [["Mã đơn hàng", "Tổng tiền"], ["DH1", "100000"]],
  )
  _write_csv(
      shop_dir / "khach.csv",
      [["Tên khách"], ["An"]],
  )
  output_dir = tmp_path / "out"
  bad_merge = {
      "groups": [{"tables": ["don"], "evidence": "sót bảng khach"}],
      "singles": [],
  }
  fake = FakeLlm(canned=[bad_merge, bad_merge])
  report, state, _events = await _run_workflow(shop_dir, output_dir, fake)
  assert report["status"] == "ok"
  assert report["table_count"] == 2
  assert state.get("merge_fallback") is True
