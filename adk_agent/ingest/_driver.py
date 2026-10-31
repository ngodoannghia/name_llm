# -*- coding: utf-8 -*-
"""Driver chạy workflow ingest: CLI chạy từng shop hoặc toàn bộ shop.

Driver tạo session trước với state ban đầu (business_id, shop_dir,
output_dir) rồi chạy Runner.run_async với Content mồi, thu thập event,
serialize từng event ra file log JSONL, đọc lại session để lấy report
cuối và ghi báo cáo tổng hợp. Driver chạy các shop tuần tự (một shop
một lúc) để RAM không phình khi shop lớn.

Lưu ý về Content mồi: run_async cho phép new_message=None, nhưng spike
G0 phải kiểm chứng None có chạy được với workflow-first run hay không.
Driver lần này dùng Content mồi rõ ràng ("Bắt đầu ingest shop ...") để
chắc chắn workflow có input khởi động; nếu spike chứng minh None chạy
được thì lần sau có thể bỏ Content mồi.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from google.adk.runners import Runner
from google.genai import types

from ._config import IngestConfig
from ._logging import get_logger
from ._models import IngestReport
from ._services import get_artifact_service, get_session_service
from ._testing import FakeLlm
from ._workflow import build_ingest_workflow

logger = get_logger(__name__)
APP_NAME = "datamate_ingest"
USER_ID = "ingest_driver"


def _event_to_json(event: object) -> str:
  """Serialize một event ADK thành một dòng JSON.

  Event của ADK là Pydantic model nên model_dump_json dùng được; nếu
  event không phải model (trường hợp lạ) thì fallback về repr để log
  không làm crash cả lần chạy.

  Args:
    event: Event từ run_async.

  Returns:
    Một dòng JSON (không có ký tự xuống dòng ở cuối).
  """
  dump = getattr(event, "model_dump_json", None)
  if callable(dump):
    try:
      return dump()
    except Exception:
      pass
  return json.dumps({"repr": repr(event)}, ensure_ascii=False)


async def run_one_shop(
    business_id: str,
    data_dir: Path,
    output_dir: Path,
    config: IngestConfig,
    model: object = None,
    use_fake: bool = False,
) -> IngestReport:
  """Chạy workflow ingest cho một shop và ghi log event JSONL.

  Args:
    business_id: Id shop (tên thư mục con trong data_dir).
    data_dir: Thư mục cha chứa các thư mục shop.
    output_dir: Thư mục ghi DB/schema/log/staging.
    config: Cấu hình ingest.
    model: Instance BaseLlm hoặc tên model cho 3 agent; None thì dùng
      config.model_id. Bị bỏ qua khi use_fake=True.
    use_fake: True thì dùng FakeLlm (chế độ thử luồng, không gọi model
      thật); False thì dùng model đã cho.

  Returns:
    IngestReport của shop (status ok/failed, db_path, schema_path).
  """
  shop_dir = data_dir / business_id
  log_dir = output_dir / "logs" / "ingest"
  log_dir.mkdir(parents=True, exist_ok=True)
  log_path = log_dir / f"{business_id}.jsonl"
  resolved_model = FakeLlm() if use_fake else model
  effective_config = config
  if not use_fake and resolved_model is not None and not config.model_id:
    # Ghi tên model thật vào config để assemble_schema ghi đúng tên
    # model vào trường model của file schema (truy vết), thay vì để
    # agent tự ghi tên mẫu từ ví dụ trong skill như trước. Chỉ áp
    # dụng khi model là instance có thuộc tính model (ví dụ LiteLlm);
    # model dạng chuỗi đã nằm trong config.model_id từ CLI.
    model_name = getattr(resolved_model, "model", "")
    if isinstance(model_name, str) and model_name:
      effective_config = config.model_copy(update={"model_id": model_name})
  workflow = build_ingest_workflow(config=effective_config, model=resolved_model)  # type: ignore[arg-type]
  session_service = get_session_service("memory")
  artifact_service = get_artifact_service(output_dir / "artifacts")
  runner = Runner(
      app_name=APP_NAME,
      node=workflow,
      session_service=session_service,  # type: ignore[arg-type]
      artifact_service=artifact_service,
  )
  session_id = f"ingest-{business_id}"
  await session_service.create_session(
      app_name=APP_NAME,
      user_id=USER_ID,
      session_id=session_id,
      state={
          "business_id": business_id,
          "shop_dir": str(shop_dir),
          "output_dir": str(output_dir),
      },
  )
  # Stream event ra file JSONL ngay khi event tới (không giữ toàn bộ
  # event trong RAM): file chỉ thành JSONL hợp lệ từng dòng, kể cả khi
  # lần chạy bị kill giữa chừng thì các dòng đã ghi vẫn đọc được.
  report = IngestReport(
      business_id=business_id,
      status="failed",
      error="Workflow kết thúc mà không có report cuối.",
  )
  with log_path.open("w", encoding="utf-8") as log_file:
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=types.Content(
            role="user",
            parts=[types.Part(text=f"Bắt đầu ingest shop {business_id}.")],
        ),
    ):
      log_file.write(_event_to_json(event) + "\n")
      log_file.flush()
      output = getattr(event, "output", None)
      if isinstance(output, dict) and output.get("business_id") == business_id:
        # Output của finalize_shop là IngestReport đã dump: nhận diện
        # bằng cặp key business_id + status để không nhầm với output
        # của node khác (init_shop cũng có business_id nhưng không có
        # key status).
        if "status" in output and "table_count" in output:
          report = IngestReport(**output)
  session = await session_service.get_session(
      app_name=APP_NAME, user_id=USER_ID, session_id=session_id
  )
  if session is not None and session.state:
    # State cuối là nguồn thứ hai cho report khi event cuối bị lỡ:
    # shop_status trong state cho biết ok/failed chắc chắn.
    status = session.state.get("shop_status")
    if status == "failed" and report.status == "ok":
      report = IngestReport(
          business_id=business_id,
          status="failed",
          error=session.state.get("shop_error", "Shop FAILED."),
      )
  logger.info("Shop %s xong: status=%s", business_id, report.status)
  return report


async def run_ingest_all(
    shops: list[str],
    data_dir: Path,
    output_dir: Path,
    config: IngestConfig,
    model: object = None,
    use_fake: bool = False,
) -> list[IngestReport]:
  """Chạy ingest tuần tự cho danh sách shop và ghi báo cáo tổng hợp.

  Args:
    shops: Danh sách business_id.
    data_dir: Thư mục cha chứa các thư mục shop.
    output_dir: Thư mục ghi kết quả.
    config: Cấu hình ingest.
    model: Model cho 3 agent (bỏ qua khi use_fake=True).
    use_fake: True thì dùng FakeLlm cho mọi shop.

  Returns:
    Danh sách IngestReport theo thứ tự shops đã cho.
  """
  output_dir.mkdir(parents=True, exist_ok=True)
  reports: list[IngestReport] = []
  for business_id in shops:
    try:
      reports.append(
          await run_one_shop(
              business_id, data_dir, output_dir, config, model, use_fake
          )
      )
    except Exception as error:
      # Một shop lỗi không dừng cả đợt: ghi failed và đi tiếp shop sau,
      # vì lỗi shop này (file hỏng, header lạ) không liên quan shop khác.
      logger.exception("Shop %s lỗi ngoài workflow: %s", business_id, error)
      reports.append(
          IngestReport(
              business_id=business_id, status="failed", error=str(error)
          )
      )
  report_path = output_dir / "ingest_report.json"
  report_path.write_text(
      json.dumps(
          [report.model_dump() for report in reports],
          ensure_ascii=False,
          indent=2,
      )
      + "\n",
      encoding="utf-8",
  )
  return reports


def main(argv: list[str] | None = None) -> int:
  """Entry point CLI của driver.

  Args:
    argv: Đối số dòng lệnh (None thì lấy sys.argv).

  Returns:
    0 khi mọi shop ok, 1 khi có ít nhất một shop failed.
  """
  parser = argparse.ArgumentParser(
      description="Chạy workflow ingest ADK cho một hoặc nhiều shop."
  )
  parser.add_argument("--data-dir", required=True, help="Thư mục cha chứa các thư mục shop.")
  parser.add_argument("--output-dir", required=True, help="Thư mục ghi DB/schema/log.")
  parser.add_argument("--shops", required=True, help="Danh sách shop cách nhau bằng dấu phẩy, ví dụ biz_101,biz_123.")
  parser.add_argument("--fake", action="store_true", help="Dùng FakeLlm (thử luồng, không gọi model thật).")
  parser.add_argument("--model", default="", help="Tên model cho agent (bỏ qua khi dùng --fake).")
  args = parser.parse_args(argv)
  config = IngestConfig(model_id=args.model)
  shops = [shop.strip() for shop in args.shops.split(",") if shop.strip()]
  model: object = None
  if not args.fake and args.model:
    # Tên model dạng chuỗi được LlmAgent tự resolve qua registry của
    # ADK (Gemini qua Google, model khác qua LiteLlm khi cấu hình).
    # Driver lần này không tự bọc LiteLlm: main agent sẽ truyền instance
    # model thật khi chạy test model thật (xem báo cáo cuối).
    model = args.model
  reports = asyncio.run(
      run_ingest_all(
          shops,
          Path(args.data_dir),
          Path(args.output_dir),
          config,
          model=model,
          use_fake=args.fake,
      )
  )
  for report in reports:
    print(f"{report.business_id}: {report.status} db={report.db_path} schema={report.schema_path} error={report.error}")
  return 0 if all(report.status == "ok" for report in reports) else 1


if __name__ == "__main__":
  sys.exit(main())
