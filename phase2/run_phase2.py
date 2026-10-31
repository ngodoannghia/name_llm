#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Điểm vào duy nhất của Giai đoạn 2 DataMate (xem API_CONTRACT mục 6).

Mục đích của script này
-----------------------
Nối các module rời rạc của Giai đoạn 2 thành một pipeline end-to-end
chạy được trên toàn bộ tập train, theo đúng thứ tự:

1. Đọc ``questions.jsonl`` theo thứ tự ``business_id`` rồi ``id`` câu
   hỏi (deterministic, không dùng ngẫu nhiên).
2. Với mỗi shop, theo thứ tự: thử nạp cache (``cache.py``), nếu miss
   thì ingest bằng phase1 (``load_shop``) + dựng view (``shop_view``)
   + lưu cache (bắt buộc truyền ``data_dir`` để cache tính được hash
   nguồn, nếu không cache sẽ luôn miss một cách an toàn theo thiết kế
   của ``cache.py``). Giải mọi câu của shop đó rồi GIẢI PHÓNG view
   trước khi sang shop khác (không giữ 30 shop cùng lúc trong RAM).
3. Mỗi câu hỏi đi qua: parser (``parse_question``) -> solver
   (``solve``) -> gate (``compute_confidence`` làm trần confidence
   rồi ``decide`` áp ngưỡng 0,65 + kiểm tra unanswerable + format).
   Solver trả entry ``id=None`` vì ``ParsedQuestion`` không mang id,
   nên tầng này PHẢI điền id thật của câu hỏi khi ghi submission.
4. Ghi ``submission.jsonl`` TĂNG DẦN (mỗi câu ghi ngay, flush ngay)
   để crash giữa chừng vẫn còn đáp án đã giải.
5. Chấm bằng phase1 ``evaluate.score_submission`` và ghi
   ``reports/PHASE2_SUMMARY.json`` cùng ``reports/error_analysis.json``.

Cách chạy (xem thêm README.md)::

    python3 run_phase2.py [--train-dir ../train] [--cache-dir ./cache]
      [--output ./output/submission.jsonl] [--shops biz_101,biz_102]
      [--limit N]

Quy ước thoát: trả 0 khi chạy hết mọi shop đã chọn (kể cả khi một shop
lỗi riêng lẻ nhưng đã được bắt và ghi vào báo cáo, vì đó là lỗi đã
kiểm soát, không phải crash đợt chạy); trả khác 0 khi lỗi ở tầng chạy
không bắt được (ví dụ không đọc được file câu hỏi, không nạp được
rulebook/calendar). Kiểm thử unit (``tests/test_phase2.py``) chạy riêng
bằng unittest, không chạy kèm trong script này.

Tổng quát hoá: script không hard-code business_id, id câu hỏi, tên cột
train hay đáp án train. Đáp án train (nếu có file ``answers.jsonl``)
chỉ dùng để CHẤM và phân tích lỗi sau khi đã ghi submission, không
dùng để quyết định đáp án. Không gọi mạng/API, chạy offline và
deterministic (thứ tự shop/câu cố định, cache key theo hash nguồn).
"""

from __future__ import annotations

import argparse
import datetime
import gc
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Nạp phase1 và phase2 bằng đường dẫn tương đối từ file này, không
# hard-code đường dẫn tuyệt đối của máy. File này nằm ở
# <datamate>/phase2/run_phase2.py nên phase1/src và phase2/src đều nằm
# ngay dưới thư mục cha của nó.
# ---------------------------------------------------------------------------
_PHASE2_DIR = Path(__file__).resolve().parent
_PHASE1_SRC = _PHASE2_DIR.parent / "phase1" / "src"
_PHASE2_SRC = _PHASE2_DIR / "src"
for _candidate in (str(_PHASE2_SRC), str(_PHASE1_SRC)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from cache import load_shop_cache, save_shop_cache  # noqa: E402
from gate import GATE_THRESHOLD, compute_confidence, decide  # noqa: E402
from ingest import load_shop  # noqa: E402
from parser import parse_question  # noqa: E402
from rulebook import load_calendar, load_rulebook  # noqa: E402
from shop_view import build_shop_view  # noqa: E402
from solver import solve  # noqa: E402

try:
    from evaluate import answers_match, load_answers, load_submission, score_submission  # noqa: E402
except ImportError:  # pragma: no cover - dự phòng khi evaluate chưa có
    answers_match = None
    load_answers = None
    load_submission = None
    score_submission = None

__all__ = [
    "parse_args",
    "load_questions",
    "parse_as_of",
    "sanitize_entry",
    "solve_one_question",
    "run_phase2",
    "main",
]


# ---------------------------------------------------------------------------
# Tiện ích đọc dữ liệu và chuẩn hoá entry
# ---------------------------------------------------------------------------


def parse_args(argv=None):
    """Phân tích tham số dòng lệnh theo API_CONTRACT mục 6.

    Đầu vào:
        argv: danh sách tham số (``None`` thì lấy từ ``sys.argv``).

    Đầu ra:
        Đối tượng argparse.Namespace với các trường ``train_dir``,
        ``cache_dir``, ``output``, ``shops`` (chuỗi gốc hoặc ``None``)
        và ``limit`` (int hoặc ``None``).

    Trường hợp biên: ``--shops`` là danh sách cách nhau bằng dấu phẩy,
    khoảng trắng thừa được bỏ; ``--limit 0`` được coi là không giới
    hạn (giống không truyền), vì giới hạn 0 câu thì đợt chạy vô nghĩa.
    """
    parser = argparse.ArgumentParser(
        description="Chạy end-to-end Giai đoạn 2 DataMate.")
    parser.add_argument(
        "--train-dir", default="../train",
        help="Thư mục split train (mặc định ../train, tính từ phase2/).")
    parser.add_argument(
        "--cache-dir", default="./cache",
        help="Thư mục cache theo shop (mặc định ./cache).")
    parser.add_argument(
        "--output", default="./output/submission.jsonl",
        help="File submission ghi tăng dần (mặc định ./output/submission.jsonl).")
    parser.add_argument(
        "--shops", default=None,
        help="Chỉ chạy các shop này, cách nhau bằng dấu phẩy (mặc định: mọi shop).")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Chỉ chạy N câu hỏi đầu tiên theo thứ tự đã sắp (mặc định: mọi câu).")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        args.limit = None
    return args


def load_questions(questions_path):
    """Đọc questions.jsonl và sắp xếp theo business_id rồi id câu hỏi.

    Đầu vào:
        questions_path: đường dẫn tới file JSONL câu hỏi.

    Đầu ra:
        Danh sách dict câu hỏi đã sắp xếp. Dòng trống được bỏ qua;
        bản ghi thiếu ``id`` hoặc ``business_id`` sẽ gây ``ValueError``
        (không bỏ lặng lẽ, vì submission thiếu id thì không chấm được).

    Vì sao sắp xếp ở đây thay vì tin thứ tự file: thứ tự file có thể
    đổi giữa các lần xuất dữ liệu, trong khi pipeline cam kết
    deterministic theo (business_id, id) ở mọi tầng (cache, ghi tăng
    dần, báo cáo).
    """
    questions = []
    with open(questions_path, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict) or not record.get("id") \
                    or not record.get("business_id"):
                raise ValueError(
                    f"Dòng {line_number} trong {questions_path} thiếu "
                    "id hoặc business_id, không thể chạy pipeline.")
            questions.append(record)
    questions.sort(key=lambda q: (str(q["business_id"]), str(q["id"])))
    return questions


def parse_as_of(question, view):
    """Giải ngày ``as_of`` của một câu hỏi, có dự phòng xác định.

    Đầu vào:
        question: dict câu hỏi, kỳ vọng có ``as_of_date`` dạng
            ``YYYY-MM-DD``.
        view: ShopView của shop (dùng ``date_max`` làm dự phòng).

    Đầu ra:
        Tuple ``(as_of, da_dung_du_phong)``. Ngày hợp lệ thì dùng đúng
        ngày đó; ngày thiếu/sai định dạng thì dùng ``date_max`` của
        shop, nếu shop cũng không có ngày nào thì dùng 2026-01-01, và
        cờ dự phòng là ``True`` để caller ghi chú vào log phân tích
        (câu dùng ngày dự phòng có thể sai kỳ tương đối, nhưng vẫn
        tốt hơn crash cả shop vì một câu thiếu ngày).
    """
    raw = question.get("as_of_date")
    if isinstance(raw, str) and raw.strip():
        try:
            return datetime.date.fromisoformat(raw.strip()), False
        except ValueError:
            pass
    date_max = getattr(view, "date_max", None)
    if isinstance(date_max, datetime.datetime):
        return date_max.date(), True
    if isinstance(date_max, datetime.date):
        return date_max, True
    return datetime.date(2026, 1, 1), True


def sanitize_entry(entry):
    """Chuyển entry đáp án về kiểu JSON thuần, không đổi giá trị.

    Đầu vào:
        entry: dict đáp án của solver/gate (có thể chứa số numpy do
            pandas tính ra, hoặc tuple trong table rows).

    Đầu ra:
        Dict mới với cùng khóa/giá trị, trong đó số numpy thành
        int/float Python, tuple thành list, bool giữ nguyên bool (phải
        kiểm tra bool TRƯỚC int vì bool là lớp con của int trong
        Python). Giá trị không chuyển được thì giữ nguyên để tầng ghi
        JSON báo lỗi rõ ràng thay vì lặng lẽ đổi đáp án.
    """
    def _clean(value):
        """Chuyển đệ quy một giá trị về kiểu JSON thuần."""
        if isinstance(value, bool) or value is None \
                or isinstance(value, (str, int, float)):
            return value
        if isinstance(value, dict):
            return {key: _clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_clean(item) for item in value]
        try:
            import numbers

            if isinstance(value, numbers.Integral):
                return int(value)
            if isinstance(value, numbers.Real):
                return float(value)
        except (TypeError, ValueError, OverflowError):
            pass
        return value

    if not isinstance(entry, dict):
        return {"id": None, "type": "abstain"}
    return {key: _clean(item) for key, item in entry.items()}


# ---------------------------------------------------------------------------
# Giải một câu hỏi: parser -> solver -> gate, điền id thật
# ---------------------------------------------------------------------------


def solve_one_question(question, view, rulebook, calendar):
    """Giải một câu hỏi thành (entry, thông tin phân tích lỗi).

    Đầu vào:
        question: dict câu hỏi (có ``id``, ``question``, ``as_of_date``).
        view: ShopView của shop chứa câu hỏi.
        rulebook: rulebook của split đang chạy.
        calendar: calendar của split đang chạy.

    Đầu ra:
        Tuple ``(entry, info)`` trong đó entry là dict submission đã
        điền id thật (``{'id', 'type', ...}``), còn info là dict gồm
        ``measure``, ``status`` (answered/abstain/unanswerable),
        ``error_group`` (None nếu answered), ``confidence`` và
        ``as_of_fallback`` (True nếu ngày as_of phải dùng dự phòng).

    Thứ tự và lý do từng bước (không được đảo):

    1. Parser: hiểu câu hỏi bằng chính products/customers của shop.
    2. Solver: tính đáp án bằng code, entry lúc này có ``id=None``.
    3. Gate: ``compute_confidence`` tính confidence theo bằng chứng
       (parse, thực thể, cột, kỳ...) và dùng làm TRẦN cho confidence
       của solver (lấy min), vì ``decide`` của gate chỉ dùng
       confidence của solver khi solver đã gán — nếu không lấy min ở
       đây, một câu parse mơ hồ nhưng solver tự tin 1.0 sẽ lọt qua
       ngưỡng 0,65 một cách oan. Sau đó ``decide`` kiểm tra
       unanswerable trước, áp ngưỡng, kiểm tra format sau.
    4. Điền id thật và sanitize: bước cuối cùng, sau mọi quyết định
       của gate, để id không bị gate làm mất (gate giữ id gốc nếu có,
       nhưng id gốc lúc này là None nên tầng này phải điền sau).

    Trường hợp biên: mọi ngoại lệ trong một câu hỏi đều được bắt thành
    abstain với ``error_group='filter'`` và ghi rõ lỗi, để một câu lỗi
    không giết cả shop (solver cũng đã bắt lỗi nội bộ, nhưng parser và
    gate vẫn có thể lỗi với dữ liệu lạ, nên tầng này bắt thêm một lớp).
    """
    question_id = str(question["id"])
    try:
        as_of, as_of_fallback = parse_as_of(question, view)
        parsed = parse_question(
            question.get("question", ""), as_of, calendar, rulebook,
            getattr(view, "products", []) or [],
            getattr(view, "customers", []) or [],
        )
        result = solve(parsed, view, rulebook, calendar)
        # Gate làm trần confidence (xem docstring bước 3).
        if getattr(result, "status", None) == "answered":
            try:
                gate_confidence = compute_confidence(
                    parsed, view, result.evidence)
                solver_confidence = getattr(result, "confidence", None)
                if isinstance(solver_confidence, (int, float)) \
                        and not isinstance(solver_confidence, bool):
                    result.confidence = min(
                        float(solver_confidence), float(gate_confidence))
                else:
                    result.confidence = float(gate_confidence)
            except Exception:
                # Tính trần confidence lỗi thì giữ confidence của
                # solver, decide vẫn áp ngưỡng và format như thường.
                pass
        result = decide(parsed, view, result, rulebook)
        entry = dict(getattr(result, "entry", None) or {})
        entry["id"] = question_id
        if getattr(result, "status", None) == "abstain":
            entry = {"id": question_id, "type": "abstain"}
        elif getattr(result, "status", None) == "unanswerable":
            entry = {"id": question_id, "type": "unanswerable"}
            raw_entry = getattr(result, "entry", None) or {}
            if isinstance(raw_entry, dict) and raw_entry.get("reason"):
                entry["reason"] = raw_entry["reason"]
        entry = sanitize_entry(entry)
        error_group = getattr(result, "error_group", None)
        status = getattr(result, "status", None) or "abstain"
        if status == "abstain" and error_group is None:
            # Solver đã trả lời nhưng gate chuyển thành abstain (dưới
            # ngưỡng hoặc lỗi khác) mà không gán nhóm: ghi là 'filter'
            # cho báo cáo (nhóm chung cho lỗi lọc/quyết định), kèm ghi
            # chú trong evidence của runner (không sửa result gốc).
            error_group = "filter"
        if status == "unanswerable":
            error_group = "unanswerable"
        info = {
            "measure": getattr(parsed, "measure", "unknown"),
            "status": status,
            "error_group": error_group,
            "confidence": getattr(result, "confidence", None),
            "as_of_fallback": as_of_fallback,
        }
        return entry, info
    except Exception as exc:  # noqa: BLE001 - bắt tổng theo từng câu
        return (
            {"id": question_id, "type": "abstain"},
            {
                "measure": "unknown",
                "status": "abstain",
                "error_group": "filter",
                "confidence": 0.0,
                "as_of_fallback": False,
                "runner_error": repr(exc),
            },
        )


# ---------------------------------------------------------------------------
# Chạy toàn bộ pipeline và ghi báo cáo
# ---------------------------------------------------------------------------


def run_phase2(train_dir, cache_dir, output_path, shops=None, limit=None):
    """Chạy pipeline Giai đoạn 2 và trả về dict tóm tắt (summary).

    Đầu vào:
        train_dir: thư mục split (chứa questions.jsonl, answers.jsonl,
            rulebook.md, calendar.csv, data/).
        cache_dir: thư mục cache theo shop.
        output_path: file submission ghi tăng dần.
        shops: list business_id cần chạy, hoặc ``None`` để chạy mọi shop
            có trong questions.
        limit: số câu hỏi tối đa (theo thứ tự đã sắp), hoặc ``None``.

    Đầu ra:
        Dict summary đã ghi ra ``reports/PHASE2_SUMMARY.json`` (cùng
        với ``reports/error_analysis.json``), gồm PA tổng, per-type,
        per-business, holdout, số correct/wrong/abstain, tỉ lệ bao phủ,
        thời gian từng shop, cache hit/miss và shop lỗi (nếu có).

    Hành vi khi shop lỗi: load/build view lỗi thì shop đó được ghi vào
    ``shop_errors``, mọi câu của shop đó được ghi thành abstain (để
    submission vẫn đủ câu), và đợt chạy TIẾP TỤC sang shop khác — theo
    contract, shop lỗi riêng lẻ không giết cả đợt chạy. Chỉ lỗi ở tầng
    chạy chung (không đọc được questions, rulebook, calendar) mới lan
    ra ngoài và làm main trả exit khác 0.
    """
    train_dir = Path(train_dir)
    cache_dir = Path(cache_dir)
    output_path = Path(output_path)
    reports_dir = _PHASE2_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    questions = load_questions(train_dir / "questions.jsonl")
    if shops:
        wanted = {str(shop).strip() for shop in shops if str(shop).strip()}
        questions = [q for q in questions if str(q["business_id"]) in wanted]
    if limit is not None:
        questions = questions[:limit]
    questions_by_id = {str(q["id"]): q for q in questions}

    # Nạp rulebook/calendar của split đang chạy (không hard-code luật).
    calendar = load_calendar(train_dir / "calendar.csv")
    rulebook = load_rulebook(train_dir / "rulebook.md", train_dir / "calendar.csv")

    by_shop = defaultdict(list)
    for question in questions:
        by_shop[str(question["business_id"])].append(question)

    answers = None
    answers_path = train_dir / "answers.jsonl"
    if answers_path.is_file() and load_answers is not None:
        answers = load_answers(answers_path)

    shop_timings = {}
    shop_errors = {}
    cache_stats = {"hit": 0, "miss": 0}
    per_question = []  # (qid, business_id, entry, info)
    written = 0
    run_started = time.perf_counter()

    # Ghi tăng dần: mở file ở chế độ ghi mới (đợt chạy mới) rồi append
    # từng câu ngay sau khi giải xong, flush sau mỗi câu.
    with open(output_path, "w", encoding="utf-8") as out_handle:
        for business_id in sorted(by_shop):
            shop_started = time.perf_counter()
            shop_questions = by_shop[business_id]
            view = None
            cache_hit = False
            try:
                view = load_shop_cache(cache_dir, business_id, train_dir)
                if view is not None:
                    cache_hit = True
                    cache_stats["hit"] += 1
                else:
                    cache_stats["miss"] += 1
                    ingest_result = load_shop(
                        business_id, train_dir / "data")
                    view = build_shop_view(ingest_result, business_id)
                    try:
                        # BẮT BUỘC truyền data_dir=train_dir để cache
                        # tính được hash nguồn; không truyền thì cache
                        # lưu với source_hash=None và lần sau luôn miss.
                        save_shop_cache(
                            cache_dir, business_id, view,
                            data_dir=train_dir)
                    except Exception as exc:  # noqa: BLE001
                        # Lỗi ghi cache không giết đợt chạy: lần sau
                        # ingest lại, đáp án vẫn đúng.
                        shop_errors.setdefault(business_id, []).append(
                            f"cache_save_failed: {exc!r}")
                for question in shop_questions:
                    entry, info = solve_one_question(
                        question, view, rulebook, calendar)
                    out_handle.write(
                        json.dumps(entry, ensure_ascii=False) + "\n")
                    out_handle.flush()
                    per_question.append(
                        (str(question["id"]), business_id, entry, info))
                    written += 1
            except Exception as exc:  # noqa: BLE001 - bắt tổng theo shop
                shop_errors.setdefault(business_id, []).append(repr(exc))
                # Shop lỗi vẫn ghi abstain cho mọi câu của shop đó để
                # submission đủ câu, rồi tiếp tục shop khác.
                for question in shop_questions:
                    entry = {"id": str(question["id"]), "type": "abstain"}
                    out_handle.write(
                        json.dumps(entry, ensure_ascii=False) + "\n")
                    out_handle.flush()
                    per_question.append((
                        str(question["id"]), business_id, entry,
                        {
                            "measure": "unknown",
                            "status": "abstain",
                            "error_group": "filter",
                            "confidence": 0.0,
                            "as_of_fallback": False,
                            "runner_error": repr(exc),
                        }))
                    written += 1
            finally:
                # Giải phóng view trước khi sang shop khác (contract).
                view = None
                gc.collect()
                shop_timings[business_id] = {
                    "seconds": round(time.perf_counter() - shop_started, 3),
                    "questions": len(shop_questions),
                    "cache_hit": cache_hit,
                }

    # Chấm bằng phase1 evaluate (chỉ khi có đáp án tham chiếu).
    summary = {
        "total_questions": len(questions),
        "written": written,
        "cache": cache_stats,
        "shop_timings": shop_timings,
        "shop_errors": shop_errors,
        "run_seconds": round(time.perf_counter() - run_started, 3),
        "status_counts": dict(Counter(info["status"] for _qid, _biz, _e, info in per_question)),
        "coverage": {},
        "score": None,
        "holdout": None,
        "non_holdout": None,
    }
    status_counts = Counter(info["status"] for _qid, _biz, _e, info in per_question)
    total = len(per_question) if per_question else 1
    summary["coverage"] = {
        "answered": status_counts.get("answered", 0),
        "abstain": status_counts.get("abstain", 0),
        "unanswerable_predicted": status_counts.get("unanswerable", 0),
        "answered_rate": status_counts.get("answered", 0) / total,
        "abstain_rate": status_counts.get("abstain", 0) / total,
        "unanswerable_rate": status_counts.get("unanswerable", 0) / total,
    }

    verdict_by_id = {}
    if answers is not None and score_submission is not None \
            and load_submission is not None and answers_match is not None:
        submission = load_submission(output_path)
        report = score_submission(submission, answers, questions_by_id)
        summary["score"] = {
            "total": report.total,
            "correct": report.correct,
            "wrong": report.wrong,
            "abstain": report.abstain,
            "pa": report.pa,
            "per_type": report.per_type,
            "per_business": report.per_business,
            "holdout_pa": report.holdout_pa,
        }
        # PA riêng holdout và các shop còn lại (tính từ per_business,
        # dùng đúng tập HOLDOUT của phase1 evaluate, không hard-code
        # lại danh sách shop ở đây ngoài việc đọc per_business).
        try:
            from evaluate import HOLDOUT_BUSINESSES  # noqa: E402
        except ImportError:
            HOLDOUT_BUSINESSES = frozenset()
        holdout_tot = holdout_cor = holdout_wro = 0
        other_tot = other_cor = other_wro = 0
        for biz, group in report.per_business.items():
            if biz in HOLDOUT_BUSINESSES:
                holdout_tot += group["total"]
                holdout_cor += group["correct"]
                holdout_wro += group["wrong"]
            else:
                other_tot += group["total"]
                other_cor += group["correct"]
                other_wro += group["wrong"]
        summary["holdout"] = {
            "total": holdout_tot, "correct": holdout_cor,
            "wrong": holdout_wro,
            "pa": (holdout_cor - holdout_wro) / holdout_tot if holdout_tot else None,
        }
        summary["non_holdout"] = {
            "total": other_tot, "correct": other_cor,
            "wrong": other_wro,
            "pa": (other_cor - other_wro) / other_tot if other_tot else None,
        }
        for qid, reference in answers.items():
            if str(qid) in questions_by_id:
                verdict_by_id[str(qid)] = answers_match(
                    reference, submission.get(str(qid)))

    # Phân tích lỗi: nhóm abstain/unanswerable theo error_group, và
    # nhóm câu sai theo measure đã parse (để biết loại câu nào yếu).
    error_groups = {}
    for qid, biz, _entry, info in per_question:
        if info["status"] == "answered":
            continue
        group = info["error_group"] or "filter"
        bucket = error_groups.setdefault(group, {"count": 0, "examples": []})
        bucket["count"] += 1
        if len(bucket["examples"]) < 3:
            bucket["examples"].append(
                {"id": qid, "business_id": biz,
                 "measure": info["measure"]})
    wrong_by_measure = {}
    for qid, biz, _entry, info in per_question:
        if verdict_by_id.get(qid) != "wrong":
            continue
        bucket = wrong_by_measure.setdefault(
            info["measure"], {"count": 0, "examples": []})
        bucket["count"] += 1
        if len(bucket["examples"]) < 3:
            bucket["examples"].append({"id": qid, "business_id": biz})
    wrong_by_business = {}
    for qid, biz, _entry, _info in per_question:
        if verdict_by_id.get(qid) != "wrong":
            continue
        bucket = wrong_by_business.setdefault(
            biz, {"count": 0, "examples": []})
        bucket["count"] += 1
        if len(bucket["examples"]) < 3:
            bucket["examples"].append({"id": qid})
    error_analysis = {
        "error_groups": error_groups,
        "wrong_by_measure": wrong_by_measure,
        "wrong_by_business": wrong_by_business,
        "verdict_counts": dict(Counter(verdict_by_id.values())),
        "shop_errors": shop_errors,
        "shop_timings": shop_timings,
    }

    with open(reports_dir / "PHASE2_SUMMARY.json", "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    with open(reports_dir / "error_analysis.json", "w", encoding="utf-8") as handle:
        json.dump(error_analysis, handle, ensure_ascii=False, indent=2)
    return summary


def main(argv=None):
    """Điểm vào dòng lệnh: parse args, chạy pipeline, in tóm tắt.

    Trả exit code 0 khi chạy hết (xem docstring module về quy ước
    thoát); khác 0 khi lỗi tầng chạy chung không bắt được.
    """
    args = parse_args(argv)
    # Đường dẫn mặc định trong contract là tương đối từ thư mục phase2
    # (nơi đặt script), nên giải tương đối theo vị trí script, không
    # theo thư mục làm việc hiện tại, để chạy từ đâu cũng giống nhau.
    train_dir = Path(args.train_dir)
    if not train_dir.is_absolute():
        train_dir = (_PHASE2_DIR / train_dir).resolve()
    cache_dir = Path(args.cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = (_PHASE2_DIR / cache_dir).resolve()
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = (_PHASE2_DIR / output_path).resolve()
    shops = None
    if args.shops:
        shops = [part.strip() for part in args.shops.split(",") if part.strip()]
    summary = run_phase2(
        train_dir, cache_dir, output_path, shops=shops, limit=args.limit)
    score = summary.get("score") or {}
    print(
        f"Done: {summary['written']}/{summary['total_questions']} câu, "
        f"PA={score.get('pa')}, correct={score.get('correct')}, "
        f"wrong={score.get('wrong')}, abstain={score.get('abstain')}, "
        f"holdout_pa={score.get('holdout_pa')}, "
        f"cache={summary['cache']}, "
        f"thời_gian={summary['run_seconds']}s")
    if summary["shop_errors"]:
        print(f"Shop lỗi (đã bắt, không giết đợt chạy): {summary['shop_errors']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
