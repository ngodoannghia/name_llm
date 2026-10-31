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
from openai_backend import OpenAIChatBackend  # noqa: E402
from run_phase2_llm import _parse_with_template_fallback, run_phase2_llm  # noqa: E402


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
        backend: backend thật (ở đây là OpenAIChatBackend).
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
            f"[LLM CALL #{call_no}] bắt đầu gọi API "
            f"(prompt {len(prompt)} ký tự, schema {len(schema)} keys)")
        try:
            result = self._backend.generate_json(prompt, schema)
        except Exception as exc:  # noqa: BLE001 - log rồi raise lại nguyên vẹn
            self.failures += 1
            elapsed_ms = (time.perf_counter() - started) * 1000
            self._logger.log(
                f"[LLM ERROR #{call_no}] gọi API thất bại sau {elapsed_ms:.0f}ms: "
                f"{_short(exc, 500)}",
                important=True)
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        self._logger.log(
            f"[LLM OK #{call_no}] API trả về sau {elapsed_ms:.0f}ms, "
            f"JSON có {len(result)} keys: {_short(sorted(result.keys()), 200)}")
        return result


def parse_with_llm_first(question_text, as_of, calendar, rulebook,
                         products, customers, backend, logger=None,
                         llm_parse_fn=None, template_fallback_fn=None):
    """Parse một câu hỏi theo thứ tự BẮT BUỘC: LLM trước, lỗi thì fallback template.

    Thứ tự này là cam kết của runner, không phải hành vi ngẫu nhiên:
      1. LLM TRƯỚC: gọi ``llm_parse_fn`` (mặc định là
         ``llm_parser.parse_with_llm`` với backend OpenAI) trước tiên.
         Parser template KHÔNG được gọi ở bước này, dù template có thể
         parse được câu hỏi — vì nếu gọi template trước rồi mới gọi LLM,
         hoặc gọi cả hai rồi chọn template, thì kết quả sẽ không còn là
         kết quả LLM nữa và log ``parse_sources`` sẽ nói dối.
         Bên trong ``llm_parser`` đã có repair tối đa 2 lần cho JSON sai
         schema: LLM được sửa tối đa 2 lần trước khi bị coi là lỗi.
      2. LLM LỖI -> TEMPLATE: chỉ khi ``llm_parse_fn`` raise bất kỳ lỗi
         nào (lỗi API, lỗi mạng, lỗi dựng prompt, lỗi không ngờ), hoặc
         trả về kết quả không dùng được (None hoặc không có ``parsed``),
         thì mới gọi parser template một lần duy nhất, với lý do lỗi LLM
         ghi rõ vào notes. Đây là lớp fallback NGOÀI, khác với lớp
         fallback BÊN TRONG ``llm_parser`` (khi backend/validate lỗi,
         ``llm_parser`` tự trả ``template_fallback`` mà không raise) —
         lớp ngoài này chỉ chạy khi ``llm_parse_fn`` tự nó raise ra ngoài,
         tức là trường hợp mà trước đây chỉ có ``solve_one_question``
         mới bắt được một cách gián tiếp.
      3. TEMPLATE CŨNG LỖI -> KHÔNG CRASH SHOP: nếu parser template cũng
         raise, hàm này log lỗi rồi raise lại nguyên vẹn, để tầng
         ``solve_one_question`` chuyển câu đó thành abstain (theo hợp
         đồng cũ: một câu lỗi không giết cả shop), chứ không tự bịa đáp án.

    Đặc biệt lưu ý để tránh gọi template thừa:
      - LLM thành công (source llm/repair, có parsed) -> trả ngay, KHÔNG
        gọi template thêm lần nào.
      - LLM đã tự fallback bên trong (trả template_fallback) -> trả ngay,
        KHÔNG gọi template lần thứ hai, vì gọi lại chỉ tốn thời gian và
        làm notes bị trùng, không đổi kết quả.

    Đầu vào:
        question_text/as_of/calendar/rulebook/products/customers: giống
            chữ ký mà ``solve_one_question`` mong đợi ở parse_fn.
        backend: backend OpenAI đã bọc log (LoggingBackend).
        logger: ConsoleLogger hoặc None (None thì không log, chỉ trả kết quả).
        llm_parse_fn: hàm LLM để tiêm trong test (mặc định parse_with_llm).
        template_fallback_fn: hàm template để tiêm trong test (mặc định
            ``run_phase2_llm._parse_with_template_fallback``).

    Đầu ra:
        Kết quả parse (ParseResult) để ``solve_one_question`` dùng tiếp.

    Raise:
        Chỉ raise khi cả LLM lẫn template đều lỗi (trường hợp 3 ở trên).
    """
    llm_parse_fn = llm_parse_fn if llm_parse_fn is not None else parse_with_llm
    template_fallback_fn = (
        template_fallback_fn
        if template_fallback_fn is not None
        else _parse_with_template_fallback
    )
    # Bước 1: LLM trước, template chưa được gọi ở đây.
    try:
        result = llm_parse_fn(
            question_text, as_of, calendar, rulebook,
            products, customers, backend=backend,
        )
    except Exception as exc:  # noqa: BLE001 - LLM lỗi bất kỳ cũng fallback
        if logger is not None:
            logger.log(
                f"[PARSE ERROR] LLM lỗi, chuyển sang parser template: {exc!r}",
                important=True)
        # Bước 2: LLM lỗi -> template một lần duy nhất.
        try:
            return template_fallback_fn(
                question_text, as_of, calendar, rulebook, products, customers,
                reason=f"llm_parser lỗi, fallback về template: {exc!r}",
            )
        except Exception:
            # Bước 3: template cũng lỗi -> log rồi raise lại để tầng trên
            # chuyển thành abstain, không bịa đáp án, không crash shop.
            if logger is not None:
                logger.log(
                    "[TEMPLATE ERROR] Parser template cũng lỗi, câu này sẽ "
                    "abstain ở tầng solver/gate.",
                    important=True)
            raise
    # LLM đã trả về: kiểm tra kết quả có dùng được không trước khi quyết.
    parsed = getattr(result, "parsed", None)
    if result is None or parsed is None:
        if logger is not None:
            logger.log(
                "[PARSE ERROR] LLM trả kết quả không dùng được "
                "(None hoặc không có parsed), chuyển sang parser template.",
                important=True)
        try:
            return template_fallback_fn(
                question_text, as_of, calendar, rulebook, products, customers,
                reason="llm_parser trả kết quả không dùng được, "
                "fallback về template",
            )
        except Exception:
            if logger is not None:
                logger.log(
                    "[TEMPLATE ERROR] Parser template cũng lỗi, câu này sẽ "
                    "abstain ở tầng solver/gate.",
                    important=True)
            raise
    # LLM thành công, hoặc đã tự fallback bên trong: trả ngay, KHÔNG gọi
    # template thêm lần nào nữa.
    return result


def parse_args(argv=None):
    """Đọc tham số dòng lệnh (tương tự run_phase2_llm.py, thêm --env-file, --reports-dir, --log-file, --quiet, --no-log-file)."""
    parser = argparse.ArgumentParser(
        description="Chạy pipeline phase2_llm với backend OpenAI (CHỈ PHÁT TRIỂN), kèm log đầy đủ.")
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
            real_backend = OpenAIChatBackend(env_file=args.env_file)
        except RuntimeError as exc:
            logger.log(f"Lỗi cấu hình OpenAI: {exc}", important=True)
            print(f"Lỗi cấu hình OpenAI: {exc}", file=sys.stderr)
            return 2
        backend = LoggingBackend(real_backend, logger)
        config = real_backend.config

        shops = None
        if args.shops:
            shops = [part.strip() for part in args.shops.split(",") if part.strip()]

        # --- Log bắt đầu: đủ để người khác tái hiện đợt chạy, trừ key ---
        logger.log("=" * 70, important=True)
        logger.log("BẮT ĐẦU chạy phase2_llm với backend OpenAI (CHỈ PHÁT TRIỂN)", important=True)
        logger.log(
            "CẢNH BÁO: submission này KHÔNG dùng để nộp Docker cuối "
            "(Docker cuối bắt buộc offline, model cục bộ <= 15B).",
            important=True)
        logger.log(
            f"Cấu hình LLM: model={config.model}, base_url={config.base_url}, "
            f"endpoint=/chat/completions, response_format=json_object, "
            f"temperature={config.temperature}, "
            f"max_tokens={config.max_output_tokens}, top_p={config.top_p}, "
            f"timeout={config.timeout_seconds}s "
            f"(OPENAI_API_KEY: đã cấu hình, KHÔNG in ra đây).",
            important=True)
        logger.log(
            f"Đường dẫn: train_dir={train_dir}, cache_dir={cache_dir}, "
            f"output={output_path}, reports_dir={reports_dir}, "
            f"log_file={logger.log_file or '(tắt file log)'}",
            important=True)
        logger.log(f"Shops: {shops or '(tất cả)'}, limit: {args.limit or '(không giới hạn)'}",
                   important=True)
        logger.log("=" * 70, important=True)

        # Đếm câu hỏi đã log để dòng log có dạng [n] dù callback của
        # pipeline không truyền số thứ tự.
        question_counter = {"n": 0}

        # Hàm parse tiêm vào pipeline: giữ nguyên chữ ký mà solve_one_question
        # mong đợi, nhưng thứ tự bên trong là LLM trước, lỗi thì template
        # (xem parse_with_llm_first), và log kết quả parse kèm notes —
        # notes là nơi llm_parser ghi lý do fallback.
        def parse_fn(question_text, as_of, calendar, rulebook, products, customers):
            parse_started = time.perf_counter()
            result = parse_with_llm_first(
                question_text, as_of, calendar, rulebook,
                products, customers, backend=backend, logger=logger,
            )
            parse_ms = (time.perf_counter() - parse_started) * 1000
            parsed = getattr(result, "parsed", None)
            measure = getattr(parsed, "measure", "?") if parsed is not None else "?"
            notes = "; ".join(str(note) for note in (getattr(result, "notes", []) or []))
            logger.log(
                f"[PARSE] source={result.source}, measure={measure}, "
                f"confidence={result.confidence:.3f}, {parse_ms:.0f}ms, "
                f"câu hỏi={_short(question_text, 80)}"
                + (f", notes={_short(notes, 300)}" if notes else ""))
            if result.source == "template_fallback":
                logger.log(
                    "[PARSE WARNING] Câu này rơi về parser template, LLM không parse được. "
                    f"Lý do trong notes ở trên (thường là lỗi API ở các dòng [LLM ERROR] trước đó).",
                    important=True)
            return result

        def on_question(question, entry, info, elapsed_seconds):
            question_counter["n"] += 1
            answer = entry.get("answer", "")
            logger.log(
                f"[Q {question_counter['n']}] {question.get('business_id')} "
                f"{question.get('id')} | as_of={question.get('as_of_date')} | "
                f"câu hỏi={_short(question.get('question', ''), 80)} | "
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
                f"[SHOP DONE] {business_id}: {n_questions} câu, "
                f"cache={'hit' if cache_hit else 'miss'}, {seconds:.1f}s"
                + (f", LỖI: {_short(errors, 300)}" if errors else ""),
                important=True)

        summary = run_phase2_llm(
            train_dir, cache_dir, output_path, shops=shops, limit=args.limit,
            reports_dir=reports_dir, parse_fn=parse_fn,
            on_question=on_question, on_shop=on_shop,
        )
        score = summary.get("score") or {}
        logger.log("=" * 70, important=True)
        logger.log(
            f"Done: {summary['written']}/{summary['total_questions']} câu, "
            f"parse_sources={summary['parse_sources']}, "
            f"PA={score.get('pa')}, correct={score.get('correct')}, "
            f"wrong={score.get('wrong')}, abstain={score.get('abstain')}, "
            f"holdout_pa={score.get('holdout_pa')}, "
            f"thời_gian={summary['run_seconds']}s",
            important=True)
        logger.log(
            f"LLM API: {backend.calls} lần gọi, {backend.failures} lần lỗi. "
            f"File submission: {output_path}. File báo cáo: {reports_dir}. "
            f"File log: {logger.log_file or '(tắt file log)'}",
            important=True)
        if summary["shop_errors"]:
            logger.log(
                f"Shop lỗi (đã bắt, không giết đợt chạy): {summary['shop_errors']}",
                important=True)
        # Cảnh báo quan trọng nhất cho người đọc log: nếu toàn bộ câu đều
        # fallback thì đây KHÔNG phải kết quả LLM, dù dòng Done trông vẫn
        # bình thường. Đây chính là trường hợp đợt chạy biz_101 trước đó.
        parse_sources = summary.get("parse_sources") or {}
        if parse_sources and set(parse_sources.keys()) == {"template_fallback"}:
            logger.log(
                "CẢNH BÁO: TẤT CẢ câu đều rơi về parser template, LLM không parse "
                "được câu nào. Hãy đọc các dòng [LLM ERROR] và [PARSE WARNING] ở trên "
                "để biết lỗi API cụ thể, đừng lấy PA của đợt chạy này so với baseline "
                "LLM (nó chỉ là kết quả template cũ).",
                important=True)
        logger.log("=" * 70, important=True)
        return 0
    finally:
        logger.close()


if __name__ == "__main__":
    raise SystemExit(main())
