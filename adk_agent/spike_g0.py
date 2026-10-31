# -*- coding: utf-8 -*-
"""Spike G0: chứng minh từng điểm kỹ thuật của ADK 2.13 trước khi tin
khung workflow ingest.

Chạy bằng venv của adk_agent (không gọi model thật, chỉ dùng FakeLlm và
model giả nội bộ):
  .venv/bin/python spike_g0.py

Mỗi phần in PASS/FAIL kèm chi tiết; script trả exit code 0 chỉ khi mọi
phần đều PASS. Các phần:
  A. Workflow graph tối thiểu chạy được qua InMemoryRunner.
  B. Loop quay lại khi validator lỗi, bằng route (không RetryConfig,
     không LoopAgent): node đếm fail 2 lần rồi ok.
  C. SkillToolset nạp skill theo nhu cầu (load_skill_from_dir + toolset).
  D. before_tool callback chặn được tool sai (trả non-None -> tool không
     chạy); after_tool cắt ngắn; on_tool_error trả payload có cấu trúc.
  E. FileArtifactService lưu/nạp được artifact local.
  F. Event serialize ra JSON được (model_dump_json từng event).
  G. new_message=None so với Content mồi khi chạy workflow-first.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from google.adk import Event, Workflow
from google.adk.artifacts.file_artifact_service import FileArtifactService
from google.adk.runners import InMemoryRunner
from google.adk.tools.skill_toolset import SkillToolset
from google.genai import types

from ingest._callbacks import (
    after_tool_callback,
    before_tool_callback,
    on_tool_error_callback,
)
from ingest._skills_loader import load_single_skill

RESULTS: list[tuple[str, bool, str]] = []


def _report(name: str, ok: bool, detail: str = "") -> None:
  """Ghi một kết quả spike và in ra ngay.

  Args:
    name: Tên phần spike.
    ok: True khi phần đó đã chứng minh được.
    detail: Chi tiết ngắn (số liệu, tên, lý do fail).
  """
  RESULTS.append((name, ok, detail))
  print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def _check_node(count: int = 0) -> Event:
  """Node đếm cho spike loop: fail 2 lần rồi ok (route có điều kiện).

  Args:
    count: Bộ đếm từ state (mặc định 0).

  Returns:
    Event mang route "retry" (kèm state count+1) khi count < 2, ngược
    lại route "ok".
  """
  if count < 2:
    return Event(output={"count": count}, state={"count": count + 1}, route="retry")
  return Event(output={"count": count}, route="ok")


def _done_node(node_input: dict | None = None) -> Event:
  """Node cuối của spike loop: chỉ ghi nhận đã tới đích.

  Args:
    node_input: Output của node trước (không dùng để quyết định).

  Returns:
    Event mang output done=True.
  """
  return Event(output={"done": True, "from": node_input})


async def _run_minimal(new_message: object) -> list[Event]:
  """Chạy workflow tối thiểu và thu toàn bộ event.

  Args:
    new_message: Content mồi hoặc None (để so sánh ở phần G).

  Returns:
    Danh sách event thu được.
  """
  workflow = Workflow(
      name="spike_minimal",
      edges=[("START", _check_node, {"retry": _check_node, "ok": _done_node})],
  )
  # app_name của runner và của session phải khớp nhau, nếu không
  # run_async sẽ báo "Session not found" (runner tra session theo
  # app_name của chính nó).
  runner = InMemoryRunner(node=workflow, app_name="spike")
  session = await runner.session_service.create_session(
      app_name="spike", user_id="spike_user", state={"count": 0}
  )
  events = []
  async for event in runner.run_async(
      user_id="spike_user",
      session_id=session.id,
      new_message=new_message,  # type: ignore[arg-type]
  ):
    events.append(event)
  return events


async def _part_a_b_g() -> None:
  """Phần A (graph tối thiểu chạy được), B (loop bằng route) và G
  (new_message=None so với Content mồi)."""
  try:
    events = await _run_minimal(
        types.Content(role="user", parts=[types.Part(text="bắt đầu")])
    )
    outputs = [event.output for event in events if event.output]
    done = any(
        isinstance(output, dict) and output.get("done") is True
        for output in outputs
    )
    _report("A: Workflow graph tối thiểu chạy được", done, f"events={len(events)}")
    # Đếm số lần _check_node chạy: mỗi lần chạy phát một event có output
    # chứa key "count" (retry 2 lần + ok 1 lần = 3 lần).
    runs = sum(
        1
        for output in outputs
        if isinstance(output, dict) and "count" in output
    )
    _report(
        "B: Loop quay lại bằng route (fail 2 lần rồi ok)",
        runs == 3 and done,
        f"check_node chạy {runs} lần (kỳ vọng 3)",
    )
  except Exception as error:
    _report("A: Workflow graph tối thiểu chạy được", False, f"lỗi: {error}")
    _report("B: Loop quay lại bằng route (fail 2 lần rồi ok)", False, f"lỗi: {error}")
  try:
    events_none = await _run_minimal(None)
    done_none = any(
        isinstance(event.output, dict) and event.output.get("done") is True
        for event in events_none
        if event.output
    )
    _report(
        "G: new_message=None chạy được với workflow-first run",
        done_none,
        f"events={len(events_none)} (None {'chạy được' if done_none else 'KHÔNG chạy được, driver giữ Content mồi'})",
    )
  except Exception as error:
    _report("G: new_message=None chạy được với workflow-first run", False, f"lỗi: {error}")


async def _part_c() -> None:
  """Phần C: SkillToolset nạp skill theo nhu cầu."""
  try:
    skills_dir = Path(__file__).resolve().parent / "skills"
    skill = load_single_skill(skills_dir, "datamate-header")
    if skill is None:
      _report("C: SkillToolset nạp skill theo nhu cầu", False, "không nạp được skill datamate-header")
      return
    toolset = SkillToolset(skills=[skill])
    tools = await toolset.get_tools()
    names = [getattr(tool, "name", "?") for tool in tools]
    _report(
        "C: SkillToolset nạp skill theo nhu cầu",
        len(tools) > 0,
        f"skill={skill.name}, tools={names}",
    )
  except Exception as error:
    _report("C: SkillToolset nạp skill theo nhu cầu", False, f"lỗi: {error}")


def _part_d() -> None:
  """Phần D: before_tool chặn tool sai, after_tool cắt ngắn, on_tool_error
  có cấu trúc (gọi trực tiếp callback, mô phỏng semantics ADK: trả
  non-None thì tool không chạy)."""
  fake_tool = SimpleNamespace(name="write_file")
  blocked = before_tool_callback(fake_tool, {}, SimpleNamespace())
  allowed_tool = SimpleNamespace(name="load_skill")
  allowed = before_tool_callback(allowed_tool, {}, SimpleNamespace())
  # list_skills là tool discovery của SkillToolset nên phải được phép;
  # run_skill_script phải bị chặn vì skill dự án không có script nào.
  list_allowed = before_tool_callback(
      SimpleNamespace(name="list_skills"), {}, SimpleNamespace()
  )
  script_blocked = before_tool_callback(
      SimpleNamespace(name="run_skill_script"), {}, SimpleNamespace()
  )
  _report(
      "D1: before_tool chặn được tool sai",
      blocked is not None
      and allowed is None
      and list_allowed is None
      and script_blocked is not None,
      f"blocked={blocked is not None}, allowed={allowed is None}, "
      f"list_allowed={list_allowed is None}, script_blocked={script_blocked is not None}",
  )
  long_result = {"text": "x" * 9000}
  truncated = after_tool_callback(fake_tool, {}, SimpleNamespace(), long_result)
  _report(
      "D2: after_tool cắt ngắn kết quả dài",
      truncated is not None and len(truncated["text"]) < 9000,
      f"độ dài sau cắt={len(truncated['text']) if truncated else 'không cắt'}",
  )
  error_payload = on_tool_error_callback(
      fake_tool, {"path": "/tmp/x"}, SimpleNamespace(), RuntimeError("lỗi thử")
  )
  _report(
      "D3: on_tool_error trả payload có cấu trúc",
      error_payload is not None and "error" in error_payload and "tool" in error_payload,
      f"payload keys={sorted(error_payload.keys()) if error_payload else []}",
  )


async def _part_e() -> None:
  """Phần E: FileArtifactService lưu/nạp được artifact local."""
  try:
    with tempfile.TemporaryDirectory() as tmp:
      service = FileArtifactService(tmp)
      artifact = types.Part(text="nội dung artifact thử")
      version = await service.save_artifact(
          app_name="spike", user_id="spike_user", session_id="s1",
          filename="thu.txt", artifact=artifact,
      )
      loaded = await service.load_artifact(
          app_name="spike", user_id="spike_user", session_id="s1",
          filename="thu.txt", version=version,
      )
      ok = loaded is not None and loaded.text == "nội dung artifact thử"
      _report("E: FileArtifactService lưu/nạp được", ok, f"version={version}")
  except Exception as error:
    _report("E: FileArtifactService lưu/nạp được", False, f"lỗi: {error}")


async def _part_f() -> None:
  """Phần F: event serialize ra JSON được."""
  try:
    events = await _run_minimal(
        types.Content(role="user", parts=[types.Part(text="bắt đầu")])
    )
    lines = [event.model_dump_json() for event in events]
    parsed = [json.loads(line) for line in lines]
    _report("F: Event serialize ra JSON được", len(parsed) == len(events), f"events={len(events)}")
  except Exception as error:
    _report("F: Event serialize ra JSON được", False, f"lỗi: {error}")


async def main() -> int:
  """Chạy toàn bộ spike G0 và trả exit code theo kết quả."""
  await _part_a_b_g()
  await _part_c()
  _part_d()
  await _part_e()
  await _part_f()
  failed = [name for name, ok, _detail in RESULTS if not ok]
  print(f"\nTổng: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS.")
  if failed:
    print(f"Chưa PASS: {failed}")
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(asyncio.run(main()))
