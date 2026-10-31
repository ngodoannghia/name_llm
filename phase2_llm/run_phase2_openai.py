# -*- coding: utf-8 -*-
"""Runner PHÁT TRIỂN: chạy pipeline phase2_llm với backend OpenAI, kèm log console đầy đủ.

Mục đích:
    Đây là cầu nối duy nhất giữa pipeline cũ (``run_phase2_llm.py``) và
    backend OpenAI mới (``src/openai_backend.py``). Nó KHÔNG sửa
    ``llm_parser.py``, ``solver.py`` hay ``shop_view.py`` — nó chỉ tiêm
    một hàm parse dùng backend OpenAI vào tham số ``parse_fn`` mà
    pipeline đã hỗ trợ sẵn, và tiêm hai callback log (``on_question``,
    ``on_shop``) mà ``run_phase2_llm.py`` hỗ trợ từ bản log này.

Vì sao cần log đầy đủ (bài học từ đợt chạy --limit 20 --shops biz_101):
    Đợt chạy đó in ra duy nhất dòng Done cuối cùng với
    ``parse_sources={'template_fallback': 20}``, nên người chạy không
    biết vì sao LLM không parse câu nào: thiếu key, key sai, API từ
    chối schema, mạng lỗi, hay JSON trả về sai — tất cả đều bị fallback
    âm thầm. Từ bản này, runner log đủ 4 tầng, theo thứ tự thời gian:
      1. Cấu hình lúc bắt đầu (model, base_url, tham số — KHÔNG in key).
      2. Từng lần gọi API trong ``generate_json`` (kể cả lần repair):
         bắt đầu, thành công hay lỗi, thời gian, và thông báo lỗi API
         (chỉ status code / thông báo của backend, không chứa key).
      3. Từng lần parse: nguồn parse (llm / repair / template_fallback),
         confidence, và notes của ``ParseResult`` — notes này chính là
         nơi ``llm_parser`` ghi lý do fallback (lỗi backend, lỗi
         validate sau repair...), nên đọc notes là biết vì sao fallback.
      4. Từng câu hỏi đã giải xong: id, shop, câu hỏi (rút gọn), measure,
         nguồn parse, confidence, trạng thái (answered / abstain /
         unanswerable), loại đáp án, đáp án (rút gọn), thời gian.
         Và từng shop khi xong: số câu, cache hit/miss, thời gian, lỗi.

    Ngoài console, mọi dòng log còn được ghi vào file log (mặc định
    ``<reports_dir>/run.log``) để người chạy gửi lại log khi cần chẩn
    đoán mà không phải chụp màn hình từng đoạn.

CẢNH BÁO — CHỈ DÙNG CHO PHÁT TRIỂN:
    Xem cảnh báo ở ``src/openai_backend.py``: backend này gọi API thương
    mại, nên submission nó tạo ra CHỈ dùng để đánh giá / sinh trace kiểm
    chứng trong phát triển, KHÔNG PHẢI submission nộp cuối. Bản Docker
    cuối bắt buộc offline, model cục bộ ≤ 15B. Vì vậy runner này ghi
    submission và báo cáo ra thư mục RIÊNG (mặc định
    ``output/submission_openai.jsonl`` và ``reports_openai/``) để không
    ghi đè báo cáo baseline của đợt chạy local/template
    (``reports/PHASE2_LLM_SUMMARY.json``).

Cách dùng:
    1. ``cp .env.example .env`` rồi điền OPENAI_API_KEY vào ``.env`` ở
       máy của bạn (không gửi key qua chat, không commit file ``.env``).
    2. Chạy thử nhỏ trước, rồi mới chạy lớn (mỗi câu hỏi là một lần gọi
       API, 3.000 câu sẽ tốn quota/thời gian đáng kể):
         cd phase2_llm
         python3 run_phase2_openai.py --limit 20 --shops biz_101
       Log sẽ hiện ngay từng câu, và file log ở
       ``reports_openai/run.log``.
    3. Nếu thiếu key, runner dừng ngay với thông báo rõ ràng (fail-fast)
       thay vì chạy rồi fallback toàn bộ về template một cách âm thầm.
    4. Muốn ít log hơn: thêm ``--quiet`` (chỉ còn log bắt đầu, lỗi API,
       tổng kết và cảnh báo, không còn log từng câu / từng lần gọi API).
       Muốn tắt file log: thêm ``--no-log-file``.

    Các cờ khác giống run_phase2_llm.py: --train-dir, --cache-dir,
    --output, --shops, --limit, cộng thêm --env-file, --reports-dir,
    --log-file, --quiet, --no-log-file.
"""

import argparse
import datetime
import sys
import time
from pathlib import Path

# Thêm thư mục src/ vào sys.path để import được cả run_phase2_llm (cùng
# thư mục với file này) lẫn llm_parser / openai_backend (trong src/).
_PHASE2_LLM_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_PHASE2_LLM_DIR / "src"))
sys.path.insert(0, str(_PHASE2_LLM_DIR))

from llm_parser import parse_with_llm  # noqa: E402
from openai_backend import OpenAIResponsesBackend  # noqa: E402
from run_phase2_llm import run_phase2_llm  # noqa: E402


def _short(text, max_len=120):
    """Rút gọn một chuỗi để log: cắt ở max_len, thay xuống dòng bằng khoảng trắng.

    Dùng cho câu hỏi, đáp án, notes — log phải đọc được trên một dòng,
    không được để một đáp án dạng bảng dài hàng nghìn ký tự tràn màn hình.
    """
    if text is None:
        return ""
    cleaned = str(text).replace("\n", " ").replace("\r", " ").strip()
    if len(cleaned) > max_len:
        return cleaned[:max_len] + "..."
    return cleaned


class ConsoleLogger:
    """Ghi log ra console (stdout) và, nếu có, ra file log.

    Đầu vào (__init__):
        log_file: đường dẫn file log, hoặc None để chỉ in console.
        quiet: True thì chỉ ghi các dòng quan trọng (bắt đầu, lỗi,
            tổng kết, cảnh báo); các dòng chi tiết từng câu / từng lần
            gọi API sẽ bị bỏ qua. False (mặc định) thì ghi đầy đủ.

    Bảo mật: class này chỉ ghi những gì caller truyền vào. Caller phải
    tự chịu trách nhiệm không truyền key vào đây — trong file này,
    không có chỗ nào truyền key, model config chỉ được log ở dạng
    model/base_url/tham số, và thông báo lỗi của backend đã được thiết
    kế để không chứa key (xem openai_backend.py).
    """

    def __init__(self, log_file=None, quiet=False):
        self.quiet = quiet
        self._handle = None
        if log_file is not None:
            log_file = Path(log_file)
            log_file.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(log_file, "w", encoding="utf-8")
            self.log_file = str(log_file)
        else:
            self.log_file = None

    def log(self, message, important=False):
        """Ghi một dòng log, kèm giờ phút giây, ra console và file (nếu có).

        Dòng quan trọng (important=True) luôn được ghi kể cả khi quiet;
        dòng thường chỉ ghi khi không quiet. Luôn flush để log hiện ngay
        cả khi pipeline còn đang chạy dở, không phải chờ tới cuối.
        """
        if self.quiet and not important:
            return
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        print(line, flush=True)
        if self._handle is not None:
            self._handle.write(line + "\n")
            self._handle.flush()

    def close(self):
        """Đóng file log (nếu có). Gọi trong finally của main để file không bị dở."""
        if self._handle is not None:
            self._handle.close()
            self._handle = None


class LoggingBackend:
    """Bọc một backend thật để log từng lần gọi API, không đổi kết quả trả về.

    Đây là cách runner nhìn thấy được lỗi API mà trước đây bị fallback
    nuốt mất: ``parse_with_llm`` gọi ``generate_json`` tối đa 3 lần cho
    một câu (1 lần đầu + tối đa 2 lần repair), mỗi lần gọi đều đi qua
    lớp bọc này và được log bắt đầu / thành công / lỗi kèm thời gian.

    Đầu vào (__init__):
        backend: backend thật (ở đây là OpenAIResponsesBackend).
        logger: ConsoleLogger để ghi log.
    """

    def __init__(self, backend, logger):
        self._backend = backend
        self._logger = logger
        self.calls = 0
        self.failures = 0

    def generate_json(self, prompt, schema):
        """Gọi backend thật, log rồi trả nguyên kết quả / nguyên lỗi về cho caller.

        Lỗi KHÔNG được nuốt ở đây: phải raise lại nguyên vẹn để
        ``parse_with_llm`` quyết định repair hay fallback theo hợp đồng
        cũ. Chỉ ghi thêm log, không đổi hành vi.
        """
        self.calls += 1
        call_no = self.calls
        started = time.perf_counter()
        self._logger.log(
            f"[LLM CALL #{call_no}] bat dau goi API "
            f"(prompt {len(prompt)} ky tu, schema {len(schema)} keys)")
        try:
            result = self._backend.generate_json(prompt, schema)
        except Exception as exc:  # noqa: BLE001 - log roi raise lai nguyen ven
            self.failures += 1
            elapsed_ms = (time.perf_counter() - started) * 1000
            self._logger.log(
                f"[LLM ERROR #{call_no}] goi API that bai sau {elapsed_ms:.0f}ms: "
                f"{_short(exc, 500)}",
                important=True)
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        self._logger.log(
            f"[LLM OK #{call_no}] API tra ve sau {elapsed_ms:.0f}ms, "
            f"JSON co {len(result)} keys: {_short(sorted(result.keys()), 200)}")
        return result


def parse_args(argv=None):
    """Đọc tham số dòng lệnh (tương tự run_phase2_llm.py, thêm --env-file, --reports-dir, --log-file, --quiet, --no-log-file)."""
    parser = argparse.ArgumentParser(
        description="Chay pipeline phase2_llm voi backend OpenAI (CHI PHAT TRIEN), kem log day du.")
    parser.add_argument(
        "--train-dir", default="../train",
        help="Thư mục split train (mặc định ../train, tính từ phase2_llm/).")
    parser.add_argument(
        "--cache-dir", default="../phase2/cache",
        help="Thư mục cache theo shop (mặc định ../phase2/cache).")
    parser.add_argument(
        "--output", default="./output/submission_openai.jsonl",
        help="File submission (mặc định ./output/submission_openai.jsonl, "
             "KHÁC với submission_llm.jsonl của bản local để không ghi đè).")
    parser.add_argument(
        "--reports-dir", default="./reports_openai",
        help="Thư mục báo cáo (mặc định ./reports_openai, KHÁC với reports/ "
             "để không ghi đè summary baseline).")
    parser.add_argument(
        "--shops", default=None,
        help="Chỉ chạy các shop này, cách nhau bằng dấu phẩy (mặc định: mọi shop).")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Chỉ chạy N câu hỏi đầu tiên theo thứ tự đã sắp (mặc định: mọi câu).")
    parser.add_argument(
        "--env-file", default=None,
        help="Đường dẫn file .env (mặc định: tự tìm phase2_llm/.env, xem openai_backend.py).")
    parser.add_argument(
        "--log-file", default=None,
        help="Đường dẫn file log (mặc định: <reports-dir>/run.log; "
             "dùng --no-log-file để tắt file log).")
    parser.add_argument(
        "--no-log-file", action="store_true",
        help="Không ghi file log, chỉ in console.")
    parser.add_argument(
        "--quiet", action="store_true",
        help="Ít log hơn: chỉ log bắt đầu, lỗi API, tổng kết và cảnh báo, "
             "không log từng câu / từng lần gọi API thành công.")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        args.limit = None
    return args


def main(argv=None):
    """Điểm vào dòng lệnh: kiểm tra key trước, rồi chạy pipeline với backend OpenAI kèm log."""
    args = parse_args(argv)

    # Đường dẫn được giải tương đối theo vị trí script (giống runner cũ),
    # để chạy từ đâu cũng giống nhau; reports_dir cần có trước để đặt
    # file log mặc định vào đó.
    train_dir = Path(args.train_dir)
    if not train_dir.is_absolute():
        train_dir = (_PHASE2_LLM_DIR / train_dir).resolve()
    cache_dir = Path(args.cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = (_PHASE2_LLM_DIR / cache_dir).resolve()
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = (_PHASE2_LLM_DIR / output_path).resolve()
    reports_dir = Path(args.reports_dir)
    if not reports_dir.is_absolute():
        reports_dir = (_PHASE2_LLM_DIR / reports_dir).resolve()
    reports_dir.mkdir(parents=True, exist_ok=True)
    if args.no_log_file:
        log_file = None
    elif args.log_file:
        log_file = Path(args.log_file)
        if not log_file.is_absolute():
            log_file = (_PHASE2_LLM_DIR / log_file).resolve()
    else:
        log_file = reports_dir / "run.log"
    logger = ConsoleLogger(log_file=log_file, quiet=args.quiet)
    try:
        # Fail-fast: nếu thiếu key thì dừng ngay, không chạy pipeline rồi mới
        # phát hiện mọi câu đều fallback về template. Thông báo lỗi ở đây
        # không chứa key (xem OpenAIConfig).
        try:
            real_backend = OpenAIResponsesBackend(env_file=args.env_file)
        except RuntimeError as exc:
            logger.log(f"Loi cau hinh OpenAI: {exc}", important=True)
            print(f"Loi cau hinh OpenAI: {exc}", file=sys.stderr)
            return 2
        backend = LoggingBackend(real_backend, logger)
        config = real_backend.config

        shops = None
        if args.shops:
            shops = [part.strip() for part in args.shops.split(",") if part.strip()]

        # --- Log bắt đầu: đủ để người khác tái hiện đợt chạy, trừ key ---
        logger.log("=" * 70, important=True)
        logger.log("BAT DAU chay phase2_llm voi backend OpenAI (CHI PHAT TRIEN)", important=True)
        logger.log(
            "CANH BAO: submission nay KHONG dung de nop Docker cuoi "
            "(Docker cuoi bat buoc offline, model cuc bo <= 15B).",
            important=True)
        logger.log(
            f"Cau hinh LLM: model={config.model}, base_url={config.base_url}, "
            f"service_tier={config.service_tier}, temperature={config.temperature}, "
            f"max_output_tokens={config.max_output_tokens}, top_p={config.top_p}, "
            f"store={config.store}, timeout={config.timeout_seconds}s "
            f"(OPENAI_API_KEY: da cau hinh, KHONG in ra day).",
            important=True)
        logger.log(
            f"Duong dan: train_dir={train_dir}, cache_dir={cache_dir}, "
            f"output={output_path}, reports_dir={reports_dir}, "
            f"log_file={logger.log_file or '(tat file log)'}",
            important=True)
        logger.log(f"Shops: {shops or '(tat ca)'}, limit: {args.limit or '(khong gioi han)'}",
                   important=True)
        logger.log("=" * 70, important=True)

        # Đếm câu hỏi đã log để dòng log có dạng [n] dù callback của
        # pipeline không truyền số thứ tự.
        question_counter = {"n": 0}

        # Hàm parse tiêm vào pipeline: giữ nguyên chữ ký mà solve_one_question
        # mong đợi, chỉ thêm backend OpenAI (đã bọc log) và log kết quả
        # parse kèm notes — notes là nơi llm_parser ghi lý do fallback.
        def parse_fn(question_text, as_of, calendar, rulebook, products, customers):
            parse_started = time.perf_counter()
            result = parse_with_llm(
                question_text, as_of, calendar, rulebook,
                products, customers, backend=backend,
            )
            parse_ms = (time.perf_counter() - parse_started) * 1000
            parsed = getattr(result, "parsed", None)
            measure = getattr(parsed, "measure", "?") if parsed is not None else "?"
            notes = "; ".join(str(note) for note in (getattr(result, "notes", []) or []))
            logger.log(
                f"[PARSE] source={result.source}, measure={measure}, "
                f"confidence={result.confidence:.3f}, {parse_ms:.0f}ms, "
                f"cau hoi={_short(question_text, 80)}"
                + (f", notes={_short(notes, 300)}" if notes else ""))
            if result.source == "template_fallback":
                logger.log(
                    "[PARSE WARNING] Cau nay roi ve parser template, LLM khong parse duoc. "
                    f"Ly do trong notes o tren (thuong la loi API o cac dong [LLM ERROR] truoc do).",
                    important=True)
            return result

        def on_question(question, entry, info, elapsed_seconds):
            question_counter["n"] += 1
            answer = entry.get("answer", "")
            logger.log(
                f"[Q {question_counter['n']}] {question.get('business_id')} "
                f"{question.get('id')} | as_of={question.get('as_of_date')} | "
                f"cau hoi={_short(question.get('question', ''), 80)} | "
                f"measure={info.get('measure')} | parse_source={info.get('parse_source')} | "
                f"confidence={info.get('confidence', 0.0):.3f} | "
                f"status={info.get('status')} | type={entry.get('type')} | "
                f"answer={_short(answer, 120)} | {elapsed_seconds * 1000:.0f}ms"
                + (f" | error_group={info.get('error_group')}"
                   if info.get("error_group") not in (None, "") else "")
                + (f" | runner_error={_short(info.get('runner_error'), 200)}"
                   if info.get("runner_error") else ""))

        def on_shop(business_id, n_questions, cache_hit, seconds, errors):
            logger.log(
                f"[SHOP DONE] {business_id}: {n_questions} cau, "
                f"cache={'hit' if cache_hit else 'miss'}, {seconds:.1f}s"
                + (f", LOI: {_short(errors, 300)}" if errors else ""),
                important=True)

        summary = run_phase2_llm(
            train_dir, cache_dir, output_path, shops=shops, limit=args.limit,
            reports_dir=reports_dir, parse_fn=parse_fn,
            on_question=on_question, on_shop=on_shop,
        )
        score = summary.get("score") or {}
        logger.log("=" * 70, important=True)
        logger.log(
            f"Done: {summary['written']}/{summary['total_questions']} cau, "
            f"parse_sources={summary['parse_sources']}, "
            f"PA={score.get('pa')}, correct={score.get('correct')}, "
            f"wrong={score.get('wrong')}, abstain={score.get('abstain')}, "
            f"holdout_pa={score.get('holdout_pa')}, "
            f"thoi_gian={summary['run_seconds']}s",
            important=True)
        logger.log(
            f"LLM API: {backend.calls} lan goi, {backend.failures} lan loi. "
            f"File submission: {output_path}. File bao cao: {reports_dir}. "
            f"File log: {logger.log_file or '(tat file log)'}",
            important=True)
        if summary["shop_errors"]:
            logger.log(
                f"Shop loi (da bat, khong giet dot chay): {summary['shop_errors']}",
                important=True)
        # Cảnh báo quan trọng nhất cho người đọc log: nếu toàn bộ câu đều
        # fallback thì đây KHÔNG phải kết quả LLM, dù dòng Done trông vẫn
        # bình thường. Đây chính là trường hợp đợt chạy biz_101 trước đó.
        parse_sources = summary.get("parse_sources") or {}
        if parse_sources and set(parse_sources.keys()) == {"template_fallback"}:
            logger.log(
                "CANH BAO: TAT CA cau deu roi ve parser template, LLM khong parse "
                "duoc cau nao. Hay doc cac dong [LLM ERROR] va [PARSE WARNING] o tren "
                "de biet loi API cu the, dung lay PA cua dot chay nay so voi baseline "
                "LLM (no chi la ket qua template cu).",
                important=True)
        logger.log("=" * 70, important=True)
        return 0
    finally:
        logger.close()


if __name__ == "__main__":
    raise SystemExit(main())
