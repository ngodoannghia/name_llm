#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime CHỈ DÙNG RAM cho Giai đoạn 2 DataMate (không đọc/ghi disk cache).

Mục đích:
    Trả lời câu hỏi "dữ liệu shop lưu ở đâu" bằng code chạy thật: dữ liệu
    shop sau ingest (ShopView: lines, orders, profile, products, customers,
    date_min/date_max) được giữ TRONG BỘ NHỚ của tiến trình này, trong một
    dict RAM_CACHE, và solver tính trực tiếp trên object trong RAM đó.
    Script KHÔNG gọi hàm cache, KHÔNG đọc, KHÔNG ghi thư mục
    ``phase2/cache/`` (file .pkl trên đĩa) — để chứng minh runtime không
    cần disk cache vẫn chạy được, và để đo thời gian/RAM thật của chế độ
    này. (Lưu ý chính xác: script tái dùng ``solve_one_question`` từ
    ``run_phase2.py`` nên module đó vẫn được import, nhưng không một
    hàm cache nào được gọi và không một file .pkl nào được chạm tới.)

Khác biệt với pipeline thường (``run_phase2.py``):
    - Pipeline thường: lần đầu ingest -> ghi view ra đĩa (``.pkl``), lần
      sau nạp lại từ đĩa nếu hash nguồn không đổi. Lợi: lần chạy sau
      nhanh, không cần ingest lại shop XLSX lớn. Hại: tốn đĩa (~361MB
      cho 30 shop train), tốn thời gian nạp/ghi pickle, và cache có thể
      cũ nếu nguồn đổi mà hash không bắt được (trong thực tế hash đã
      bắt được, nhưng vẫn là một lớp trung gian).
    - Runtime RAM này: ingest trực tiếp vào RAM, giải mọi câu của shop
      đó ngay trên RAM, rồi (mặc định) GIẢI PHÓNG view trước khi sang
      shop khác. Lợi: không tốn đĩa, không tốn thời gian pickle, không
      sợ cache cũ. Hại: RAM cache là VOLATILE — tiến trình kết thúc là
      mất hết, lần chạy sau phải ingest lại từ đầu (shop XLSX lớn mất
      17-47 giây/shop). Vì vậy chế độ này phù hợp cho runtime một lần
      (kiểu Docker predict.sh: đọc /data, ingest, giải, ghi /result,
      thoát), không phù hợp để chạy đi chạy lại nhiều lần trên cùng
      một máy phát triển nếu muốn tiết kiệm thời gian ingest.

Chế độ giữ RAM:
    - Mặc định (--keep-all KHÔNG bật): một shop tại một thời điểm trong
      RAM. Giải xong shop thì ``del`` view + ``gc.collect()`` trước khi
      sang shop khác. Đây là chế độ an toàn cho máy RAM nhỏ (máy hiện
      tại chỉ có 7,7GB RAM, không nên giữ cả 30 shop cùng lúc).
    - --keep-all: giữ MỌI shop đã ingest trong RAM_CACHE suốt đợt chạy.
      Chỉ dùng khi chạy ít shop nhỏ, hoặc máy có RAM lớn (máy chấm Docker
      có 512GB RAM thì giữ cả private test trong RAM cũng được). Khi bật
      cờ này, nếu cùng một shop được hỏi lại trong cùng tiến trình thì
      sẽ gặp RAM cache hit và không ingest lại.

Cách chạy (từ thư mục phase2/):
    python3 run_ram.py --shops biz_101 --limit 20
    python3 run_ram.py --shops biz_101 --limit 20 --keep-all
    python3 run_ram.py --output ./output/submission_ram.jsonl

Không gọi mạng/API, chạy offline, deterministic như pipeline thường
(parser template, solver, gate giống hệt run_phase2.py). Đáp án train
(nếu có answers.jsonl) chỉ dùng để CHẤM sau khi đã ghi submission,
không dùng để quyết định đáp án.
"""

import argparse
import gc
import json
import resource
import sys
import time
from collections import defaultdict
from pathlib import Path

_PHASE2_DIR = Path(__file__).resolve().parent
_PHASE1_SRC = _PHASE2_DIR.parent / "phase1" / "src"
_PHASE2_SRC = _PHASE2_DIR / "src"
for _candidate in (str(_PHASE2_SRC), str(_PHASE1_SRC)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

# CỐ TÌNH không import cache.py ở đây: runtime này không dùng disk cache.
from ingest import load_shop  # noqa: E402
from rulebook import load_calendar, load_rulebook  # noqa: E402
from run_phase2 import load_questions, solve_one_question  # noqa: E402
from shop_view import build_shop_view  # noqa: E402

try:
    from evaluate import load_answers, score_submission  # noqa: E402
except ImportError:  # pragma: no cover - dự phòng khi evaluate chưa có
    load_answers = None
    score_submission = None

# Cache RAM của tiến trình: business_id -> ShopView (object trong RAM).
# Chỉ sống trong tiến trình này, thoát là mất — khác disk cache .pkl.
RAM_CACHE = {}


def _ram_mb_peak():
    """Trả về peak RAM của tiến trình tính bằng MB (Linux, ru_maxrss tính bằng KB)."""
    try:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return -1.0


def get_shop_view_ram(business_id, train_dir, ram_cache):
    """Lấy ShopView của một shop từ RAM_CACHE, miss thì ingest vào RAM.

    Đầu vào:
        business_id: mã shop cần lấy view.
        train_dir: thư mục split (chứa data/).
        ram_cache: dict RAM_CACHE của tiến trình.

    Đầu ra:
        Tuple (view, cache_hit): view là ShopView trong RAM, cache_hit
        True nếu view đã có sẵn trong RAM từ trước (không ingest lại),
        False nếu vừa ingest xong và mới đưa vào RAM.
    """
    if business_id in ram_cache:
        return ram_cache[business_id], True
    ingest_result = load_shop(business_id, train_dir / "data")
    view = build_shop_view(ingest_result, business_id)
    ram_cache[business_id] = view
    return view, False


def _view_size(view):
    """Đếm số dòng/orders/sản phẩm của view để in log (không tính RAM chính xác từng object)."""
    lines = getattr(view, "lines", None)
    orders = getattr(view, "orders", None)
    products = getattr(view, "products", None)
    try:
        n_lines = len(lines) if lines is not None else 0
    except TypeError:
        n_lines = -1
    try:
        n_orders = len(orders) if orders is not None else 0
    except TypeError:
        n_orders = -1
    try:
        n_products = len(products) if products is not None else 0
    except TypeError:
        n_products = -1
    return n_lines, n_orders, n_products


def parse_args(argv=None):
    """Đọc tham số dòng lệnh cho runtime RAM."""
    parser = argparse.ArgumentParser(
        description="Chạy Giai đoạn 2 chỉ dùng RAM, không dùng disk cache.")
    parser.add_argument(
        "--train-dir", default="../train",
        help="Thư mục split train (mặc định ../train, tính từ phase2/).")
    parser.add_argument(
        "--output", default="./output/submission_ram.jsonl",
        help="File submission ghi tăng dần (mặc định ./output/submission_ram.jsonl).")
    parser.add_argument(
        "--shops", default=None,
        help="Chỉ chạy các shop này, cách nhau bằng dấu phẩy (mặc định: mọi shop).")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Chỉ chạy N câu hỏi đầu tiên theo thứ tự đã sắp (mặc định: mọi câu).")
    parser.add_argument(
        "--keep-all", action="store_true",
        help="Giữ mọi shop trong RAM suốt đợt chạy (chỉ dùng khi RAM lớn hoặc ít shop). "
             "Mặc định: giải xong một shop thì giải phóng view trước khi sang shop khác.")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        args.limit = None
    return args


def main(argv=None):
    """Chạy runtime RAM: ingest từng shop vào RAM, giải, ghi submission, chấm (nếu có đáp án)."""
    args = parse_args(argv)
    train_dir = Path(args.train_dir)
    if not train_dir.is_absolute():
        train_dir = (_PHASE2_DIR / train_dir).resolve()
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = (_PHASE2_DIR / output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    questions = load_questions(train_dir / "questions.jsonl")
    if args.shops:
        wanted = {part.strip() for part in args.shops.split(",") if part.strip()}
        questions = [q for q in questions if str(q["business_id"]) in wanted]
    if args.limit is not None:
        questions = questions[:args.limit]
    by_shop = defaultdict(list)
    for question in questions:
        by_shop[str(question["business_id"])].append(question)

    calendar = load_calendar(train_dir / "calendar.csv")
    rulebook = load_rulebook(train_dir / "rulebook.md", train_dir / "calendar.csv")

    ram_cache = RAM_CACHE
    ram_hits = 0
    ram_misses = 0
    shop_errors = {}
    written = 0
    run_started = time.perf_counter()

    print(f"[RAM] Bắt đầu runtime RAM-only: {len(questions)} câu, "
          f"{len(by_shop)} shop, keep_all={args.keep_all}, "
          f"KHÔNG dùng disk cache (phase2/cache/).", flush=True)
    print(f"[RAM] RAM peak lúc bắt đầu: {_ram_mb_peak():.1f} MB.", flush=True)

    with open(output_path, "w", encoding="utf-8") as out_handle:
        for business_id in sorted(by_shop):
            shop_started = time.perf_counter()
            shop_questions = by_shop[business_id]
            try:
                view, cache_hit = get_shop_view_ram(business_id, train_dir, ram_cache)
                if cache_hit:
                    ram_hits += 1
                else:
                    ram_misses += 1
                n_lines, n_orders, n_products = _view_size(view)
                print(f"[RAM] Shop {business_id}: cache_hit={cache_hit}, "
                      f"lines={n_lines}, orders={n_orders}, products={n_products}, "
                      f"RAM peak={_ram_mb_peak():.1f} MB, "
                      f"ingest+chuẩn bị={time.perf_counter() - shop_started:.2f}s "
                      f"(view đang ở trong RAM).", flush=True)
                for question in shop_questions:
                    entry, _info = solve_one_question(question, view, rulebook, calendar)
                    out_handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    out_handle.flush()
                    written += 1
                print(f"[RAM] Shop {business_id} xong: {len(shop_questions)} câu, "
                      f"tổng thời gian shop={time.perf_counter() - shop_started:.2f}s, "
                      f"RAM peak={_ram_mb_peak():.1f} MB.", flush=True)
            except Exception as exc:  # noqa: BLE001 - shop lỗi thì abstain, không giết đợt chạy
                shop_errors.setdefault(business_id, []).append(repr(exc))
                for question in shop_questions:
                    out_handle.write(json.dumps(
                        {"id": str(question["id"]), "type": "abstain"},
                        ensure_ascii=False) + "\n")
                    out_handle.flush()
                    written += 1
                print(f"[RAM] Shop {business_id} LỖI (đã bắt, ghi abstain): {exc!r}", flush=True)
            finally:
                # Mặc định: giải phóng view của shop vừa xong khỏi RAM.
                # Nếu --keep-all thì giữ lại để lần hỏi lại cùng shop trong
                # cùng tiến trình sẽ gặp RAM cache hit.
                if not args.keep_all and business_id in ram_cache:
                    del ram_cache[business_id]
                    gc.collect()

    run_seconds = time.perf_counter() - run_started
    print(f"[RAM] Done: {written}/{len(questions)} câu, "
          f"RAM hits={ram_hits}, misses={ram_misses}, "
          f"thời gian={run_seconds:.2f}s, RAM peak={_ram_mb_peak():.1f} MB, "
          f"output={output_path}", flush=True)
    if shop_errors:
        print(f"[RAM] Shop lỗi (đã bắt, không giết đợt chạy): {shop_errors}", flush=True)
    print("[RAM] Lưu ý: RAM_CACHE chỉ sống trong tiến trình này. "
          "Tiến trình thoát là RAM được giải phóng, lần chạy sau phải "
          "ingest lại từ đầu — khác disk cache .pkl vốn còn lại trên đĩa.", flush=True)

    # Chấm (nếu có đáp án train) — chỉ để báo cáo, không dùng để quyết định đáp án.
    if score_submission is not None and load_answers is not None:
        answers_path = train_dir / "answers.jsonl"
        if answers_path.is_file():
            try:
                from evaluate import load_submission
                score = score_submission(
                    load_submission(output_path), load_answers(answers_path))
                # ScoreReport là object có thuộc tính, không phải dict.
                print(f"[RAM] Chấm trên toàn bộ train (câu chưa chạy tính là abstain): "
                      f"PA={score.pa}, correct={score.correct}, "
                      f"wrong={score.wrong}, abstain={score.abstain}.", flush=True)
            except Exception as exc:  # noqa: BLE001 - lỗi chấm không giết đợt chạy
                print(f"[RAM] Không chấm được (lỗi chấm, không ảnh hưởng submission): {exc!r}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
