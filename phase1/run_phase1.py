#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
"""
Điểm vào chính của Giai đoạn 1 DataMate.

Mục đích của file này là nối tất cả các module rời rạc (ingest, rulebook,
evaluation, tests) thành một luồng chạy end-to-end duy nhất, để coordinator
không bàn giao các module chưa từng chạy chung với nhau.

Luồng chạy, theo đúng thứ tự:
  1. Nạp rulebook + calendar của split đang chạy (không hard-code rulebook train).
  2. Ingest từng shop một, ghi báo cáo theo từng shop, rồi giải phóng DataFrame
     ngay sau khi đếm xong (không giữ cả ~4 triệu dòng của 30 shop trong RAM
     cùng lúc — đây là quyết định có chủ ý về bộ nhớ, khác với hàm
     ``load_all_shops`` vốn giữ mọi thứ trong dict).
  3. Chấm 3 submission mẫu bằng khung đánh giá (hoàn hảo / toàn abstain /
     sai format) để chứng minh khung đánh giá hoạt động.
  4. Chạy toàn bộ test tổng quát hoá bằng unittest và ghi kết quả ra file.
  5. Ghi mọi kết quả ra thư mục ``reports/`` dưới dạng JSON + text, để
     ``PHASE1_REPORT.md`` có số liệu thật chứ không phải số liệu đoán.

Cách chạy (từ thư mục ``phase1/``):
    python3 run_phase1.py
    python3 run_phase1.py --train-dir ../train --skip-tests
    python3 run_phase1.py --train-dir /duong/dan/toi/train

Lưu ý về đường dẫn: thư mục train có cấu trúc ``train/data/biz_*``,
``train/questions.jsonl``, ``train/answers.jsonl``, ``train/rulebook.md``,
``train/calendar.csv``. Nếu người dùng truyền thẳng thư mục ``data/``
thì script cũng chấp nhận (tự phát hiện thư mục con ``data/``).

Không gọi mạng, không gọi API, không sửa dữ liệu train. Mọi file ghi ra
đều nằm trong ``phase1/reports/``.
"""

import argparse
import gc
import json
import subprocess
import sys
import time
from pathlib import Path

# Thêm thư mục src vào sys.path để import được các module nội bộ khi chạy
# script trực tiếp từ thư mục phase1/ mà không cần cài đặt package.
sys.path.insert(0, str(Path(__file__).parent / "src"))

from evaluate import (  # noqa: E402
    HOLDOUT_BUSINESSES,
    load_answers,
    make_abstain_submission,
    make_broken_submission,
    make_perfect_submission,
    score_submission,
)
from ingest import load_shop  # noqa: E402
from rulebook import load_calendar, load_rulebook  # noqa: E402


def resolve_train_paths(train_dir: Path) -> dict:
    """Xác định các đường dẫn thật trong thư mục train.

    Đầu vào: ``train_dir`` do người dùng truyền vào (có thể là ``train/``
    hoặc thẳng ``train/data/``).
    Đầu ra: dict với các khóa ``data_dir``, ``questions``, ``answers``,
    ``rulebook``, ``calendar`` (mỗi giá trị là một Path).
    Trường hợp biên: nếu cả hai dạng đều không khớp cấu trúc kỳ vọng,
    hàm vẫn trả về đường dẫn suy ra từ ``train_dir`` và các bước sau sẽ
    báo lỗi rõ ràng thay vì âm thầm dùng sai thư mục.
    """
    # Nếu người dùng truyền thẳng thư mục data/ thì thư mục cha của nó
    # mới là thư mục train chứa questions/answers/rulebook/calendar.
    if train_dir.name == "data" and (train_dir.parent / "questions.jsonl").exists():
        root = train_dir.parent
        data_dir = train_dir
    else:
        root = train_dir
        # Ưu tiên train/data/ (cấu trúc thật), nếu không có thì dùng chính train_dir.
        data_dir = train_dir / "data" if (train_dir / "data").is_dir() else train_dir
    return {
        "data_dir": data_dir,
        "questions": root / "questions.jsonl",
        "answers": root / "answers.jsonl",
        "rulebook": root / "rulebook.md",
        "calendar": root / "calendar.csv",
    }


def run_ingest_all(data_dir: Path) -> list:
    """Ingest từng shop một và trả về báo cáo theo từng shop.

    Vì sao không dùng ``load_all_shops`` ở đây: hàm đó giữ toàn bộ
    DataFrame của 30 shop trong RAM cùng lúc (~4 triệu dòng), dễ tốn
    nhiều bộ nhớ. Chạy từng shop rồi ``del`` + ``gc.collect()`` giúp
    bộ nhớ luôn chỉ chứa một shop tại một thời điểm, phù hợp hơn khi
    chạy đánh giá toàn bộ về sau.

    Mỗi phần tử trong danh sách trả về là một dict đã có thể ghi thẳng
    ra JSON, gồm: business_id, files, tables (tên bảng + số dòng),
    rows_in, rows_out, columns_mapped, columns_unmapped, các cờ hồ sơ
    shop (has_product_detail, has_customer_code, ...), số warning,
    số parse_failures, thời gian chạy (giây) và trường ``error`` nếu
    shop đó lỗi (khi đó các trường số liệu sẽ là 0/None, nhưng shop
    vẫn có mặt trong báo cáo — không được lặng lẽ bỏ shop).
    """
    # Lấy danh sách shop theo thứ tự tên để kết quả deterministic:
    # chạy lại hai lần phải cho cùng thứ tự báo cáo.
    shop_dirs = sorted(
        [p for p in data_dir.iterdir() if p.is_dir() and p.name.startswith("biz_")],
        key=lambda p: p.name,
    )
    reports = []
    for shop_dir in shop_dirs:
        business_id = shop_dir.name
        started = time.time()
        try:
            # load_shop nhận data_dir (thư mục cha chứa các thư mục shop),
            # không phải thư mục của chính shop đó — xem docstring của ingest.
            result = load_shop(business_id, str(data_dir))
            profile = result.profile
            # Đếm số dòng thực tế theo từng bảng, vì profile.rows_in/out
            # là tổng hợp của module ingest và cần đối chiếu lại.
            table_rows = {name: int(len(df)) for name, df in result.tables.items()}
            reports.append({
                "business_id": business_id,
                "status": "ok",
                "files": list(profile.files),
                "tables": table_rows,
                "rows_in": int(profile.rows_in),
                "rows_out": int(profile.rows_out),
                "rows_counted_from_tables": int(sum(table_rows.values())),
                "columns_mapped": dict(profile.columns_mapped),
                "columns_unmapped": list(profile.columns_unmapped),
                "has_product_detail": bool(profile.has_product_detail),
                "has_customer_code": bool(profile.has_customer_code),
                "has_customer_name": bool(profile.has_customer_name),
                "has_cost": bool(profile.has_cost),
                "has_lot_expiry": bool(profile.has_lot_expiry),
                "has_return_table": bool(profile.has_return_table),
                "warnings": list(profile.warnings) + list(result.warnings),
                "warnings_count": len(profile.warnings) + len(result.warnings),
                "parse_failures": int(profile.parse_failures),
                "elapsed_seconds": round(time.time() - started, 2),
                "error": None,
            })
            # Giải phóng DataFrame của shop vừa xong trước khi sang shop khác.
            del result
            gc.collect()
        except Exception as exc:  # noqa: BLE001 - cố tình bắt rộng để một shop lỗi không giết cả đợt chạy
            # Một shop lỗi vẫn phải có mặt trong báo cáo với status = error,
            # để PHASE1_REPORT.md nói rõ shop nào crash thay vì thiếu shop.
            reports.append({
                "business_id": business_id,
                "status": "error",
                "files": [],
                "tables": {},
                "rows_in": 0,
                "rows_out": 0,
                "rows_counted_from_tables": 0,
                "columns_mapped": {},
                "columns_unmapped": [],
                "has_product_detail": False,
                "has_customer_code": False,
                "has_customer_name": False,
                "has_cost": False,
                "has_lot_expiry": False,
                "has_return_table": False,
                "warnings": [],
                "warnings_count": 0,
                "parse_failures": 0,
                "elapsed_seconds": round(time.time() - started, 2),
                "error": f"{type(exc).__name__}: {exc}",
            })
    return reports


def run_evaluation_samples(paths: dict) -> dict:
    """Chạy 3 submission mẫu qua khung đánh giá và trả về kết quả chấm.

    Ba trường hợp bắt buộc, mỗi trường hợp phải cho kết quả kỳ vọng:
      - perfect (copy đáp án): PA phải = 1.0.
      - abstain (toàn abstain): PA phải = 0.0.
      - broken (sai format có chủ đích): phải có ít nhất 1 wrong, PA < 1.0.
    Hàm này không tự kết luận pass/fail bằng cách đoán: nó chỉ trả về
    số liệu thật, việc đối chiếu với kỳ vọng nằm ở báo cáo và ở người đọc.
    Nếu file answers/questions không tồn tại, hàm sẽ để lỗi lan ra ngoài
    (không bịa số liệu đánh giá) — vì đánh giá sai còn nguy hiểm hơn không đánh giá.
    """
    answers = load_answers(str(paths["answers"]))
    # Đọc questions để lấy business_id cho báo cáo per_business; nếu thiếu
    # file questions thì score_submission vẫn chạy được với business_id = 'unknown'.
    questions = {}
    if paths["questions"].exists():
        with open(paths["questions"], encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    record = json.loads(line)
                    questions[record["id"]] = record
    results = {}
    for name, maker in (
        ("perfect", make_perfect_submission),
        ("abstain", make_abstain_submission),
        ("broken", make_broken_submission),
    ):
        submission = maker(answers)
        report = score_submission(submission, answers, questions if questions else None)
        results[name] = {
            "total": int(report.total),
            "correct": int(report.correct),
            "wrong": int(report.wrong),
            "abstain": int(report.abstain),
            "pa": float(report.pa),
            "holdout_pa": float(report.holdout_pa) if report.holdout_pa is not None else None,
            "per_type": {
                type_name: {
                    "total": int(v["total"]),
                    "correct": int(v["correct"]),
                    "wrong": int(v["wrong"]),
                    "abstain": int(v["abstain"]),
                    "pa": float(v["pa"]),
                }
                for type_name, v in sorted(report.per_type.items())
            },
        }
    return results


def run_tests(phase1_dir: Path) -> dict:
    """Chạy toàn bộ test tổng quát hoá bằng unittest và trả về kết quả thô.

    Dùng subprocess để chạy ``python3 -m unittest`` trong một tiến trình
    riêng, tránh việc import test làm bẩn trạng thái của tiến trình chính.
    Kết quả trả về gồm exit code, toàn bộ stdout/stderr, và hai con số
    trích từ dòng tổng kết (``Ran N tests``, ``OK``/``FAILED``) — nếu
    không trích được thì để None chứ không đoán.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=str(phase1_dir),
        capture_output=True,
        text=True,
        timeout=600,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    ran = None
    for line in output.splitlines():
        if line.startswith("Ran "):
            # Dạng: "Ran 12 tests in 0.037s"
            try:
                ran = int(line.split()[1])
            except (IndexError, ValueError):
                ran = None
    return {
        "exit_code": int(proc.returncode),
        "tests_ran": ran,
        "ok": "OK" in output and "FAILED" not in output,
        "output_tail": "\n".join(output.splitlines()[-40:]),
    }


def main() -> int:
    """Hàm main: parse tham số, chạy từng bước, ghi báo cáo, in tóm tắt.

    Trả về 0 nếu ingest không có shop nào error và test exit code = 0;
    trả về 1 nếu có shop error hoặc test fail — để người chạy script
    biết ngay bằng exit code mà không cần đọc hết báo cáo.
    """
    parser = argparse.ArgumentParser(
        description="Chạy end-to-end Giai đoạn 1 DataMate: rulebook + ingest 30 shop + đánh giá mẫu + tests."
    )
    parser.add_argument(
        "--train-dir",
        default="../train",
        help="Thư mục train (mặc định ../train khi chạy từ phase1/).",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Bỏ qua bước chạy unittest (dùng khi chỉ muốn đo ingest).",
    )
    args = parser.parse_args()

    phase1_dir = Path(__file__).parent
    reports_dir = phase1_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    paths = resolve_train_paths(Path(args.train_dir))

    # Bước 1: nạp rulebook + calendar của split đang chạy.
    print("[1/4] Nạp rulebook...", flush=True)
    rulebook = load_rulebook(str(paths["rulebook"]), str(paths["calendar"]))
    calendar = load_calendar(str(paths["calendar"]))
    rulebook_info = {
        "source_path": str(paths["rulebook"]),
        "calendar_path": str(paths["calendar"]),
        "calendar_rows": len(calendar.rows),
        "rules_count": len(rulebook.rules),
        "revenue_excludes_cancelled": rulebook.revenue_excludes_cancelled,
        "revenue_excludes_returned": rulebook.revenue_excludes_returned,
        "week_starts_monday": rulebook.week_starts_monday,
        "month_to_date_cut_at_as_of": rulebook.month_to_date_cut_at_as_of,
        "tet_rule_text": rulebook.tet_rule_text,
        "unparsed_notes": list(rulebook.unparsed_notes),
    }
    print(
        f"  Rulebook: {rulebook_info['rules_count']} luật, "
        f"loại đơn huỷ={rulebook_info['revenue_excludes_cancelled']}, "
        f"loại đơn trả={rulebook_info['revenue_excludes_returned']}, "
        f"tuần bắt đầu thứ Hai={rulebook_info['week_starts_monday']}, "
        f"calendar {rulebook_info['calendar_rows']} dòng.",
        flush=True,
    )

    # Bước 2: ingest từng shop.
    print("[2/4] Ingest từng shop...", flush=True)
    ingest_reports = run_ingest_all(paths["data_dir"])
    ok_shops = [r for r in ingest_reports if r["status"] == "ok"]
    error_shops = [r for r in ingest_reports if r["status"] != "ok"]
    total_in = sum(r["rows_in"] for r in ingest_reports)
    total_out = sum(r["rows_out"] for r in ingest_reports)
    print(
        f"  Ingest: {len(ok_shops)}/{len(ingest_reports)} shop ok, "
        f"{len(error_shops)} shop lỗi, tổng dòng vào={total_in}, ra={total_out}.",
        flush=True,
    )
    for report in ingest_reports:
        print(
            f"    {report['business_id']}: {report['status']}, "
            f"{report['rows_in']} -> {report['rows_out']} dòng, "
            f"{report['elapsed_seconds']}s"
            + (f", LỖI: {report['error']}" if report["error"] else ""),
            flush=True,
        )

    # Bước 3: chấm 3 submission mẫu.
    print("[3/4] Chấm 3 submission mẫu...", flush=True)
    evaluation = run_evaluation_samples(paths)
    for name, result in evaluation.items():
        print(
            f"  {name}: total={result['total']}, correct={result['correct']}, "
            f"wrong={result['wrong']}, abstain={result['abstain']}, PA={result['pa']}, "
            f"holdout_PA={result['holdout_pa']}",
            flush=True,
        )

    # Bước 4: chạy tests (trừ khi người dùng yêu cầu bỏ qua).
    if args.skip_tests:
        tests_result = {"exit_code": None, "tests_ran": None, "ok": None, "output_tail": "skipped by --skip-tests"}
        print("[4/4] Bỏ qua tests theo --skip-tests.", flush=True)
    else:
        print("[4/4] Chạy unittest...", flush=True)
        tests_result = run_tests(phase1_dir)
        print(
            f"  Tests: exit_code={tests_result['exit_code']}, "
            f"ran={tests_result['tests_ran']}, ok={tests_result['ok']}",
            flush=True,
        )

    # Ghi báo cáo JSON để PHASE1_REPORT.md và các bước sau dùng số liệu thật.
    summary = {
        "rulebook": rulebook_info,
        "holdout_businesses": sorted(HOLDOUT_BUSINESSES),
        "ingest": {
            "shops_total": len(ingest_reports),
            "shops_ok": len(ok_shops),
            "shops_error": [r["business_id"] for r in error_shops],
            "rows_in_total": total_in,
            "rows_out_total": total_out,
            "total_elapsed_seconds": round(sum(r["elapsed_seconds"] for r in ingest_reports), 2),
            "per_shop": ingest_reports,
        },
        "evaluation_samples": evaluation,
        "tests": tests_result,
    }
    with open(reports_dir / "phase1_summary.json", "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    with open(reports_dir / "tests_output.txt", "w", encoding="utf-8") as handle:
        handle.write(tests_result["output_tail"] or "")

    print(f"Báo cáo JSON: {reports_dir / 'phase1_summary.json'}", flush=True)
    # Exit code phản ánh đúng trạng thái: có shop lỗi hoặc test fail thì khác 0.
    if error_shops or (not args.skip_tests and tests_result["exit_code"] != 0):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
