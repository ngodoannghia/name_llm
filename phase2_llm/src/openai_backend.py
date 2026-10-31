# -*- coding: utf-8 -*-
"""Backend OpenAI Responses API — CHỈ DÙNG CHO PHÁT TRIỂN (TEACHER).

Mục đích:
    Cho phép chạy tầng parser LLM của phase2_llm bằng model API mà người
    dùng cấu hình trong ``.env`` (ví dụ ``gpt-4o-mini`` từ curl đã gửi),
    trong giai đoạn phát triển, để tạo các trace ứng viên rồi kiểm chứng
    bằng solver (chỉ giữ trace nào cho đáp án đúng trên train). Đây là
    cách duy nhất để "chạy được" parser LLM khi máy chưa có model cục bộ
    ≤ 15B, weights, hay transformers/torch.

CẢNH BÁO — KHÔNG PHẢI BACKEND DOCKER CUỐI:
    - Class này CỐ TÌNH không kế thừa ``LocalModelBackend`` của
      ``llm_parser.py``, vì backend đó theo hợp đồng phải chạy offline,
      trên máy cục bộ, không gọi API thương mại. Class này làm ngược lại
      (gọi mạng tới OpenAI), nên nó là một loại backend riêng, chỉ dùng
      trong phát triển.
    - Bản Docker nộp cuối BẮT BUỘC offline, model cục bộ tổng ≤ 15B,
      không gọi API thương mại, không tải gì khi chạy. Vì vậy KHÔNG BAO
      GIỜ đưa class này, file ``.env``, hay key API vào Docker cuối, và
      không dùng submission chạy bằng backend này làm submission nộp.

Cách cấu hình (file ``.env``):
    Backend đọc các biến sau, theo thứ tự ưu tiên: biến môi trường có
    sẵn trong shell luôn thắng, sau đó mới tới giá trị trong file
    ``.env``. Việc này tránh ghi đè key mà người dùng đã export an toàn
    ở máy của mình, và tránh việc một file ``.env`` cũ vô tình thay key
    mới. Thứ tự tìm file ``.env``: đường dẫn truyền vào, biến
    ``LLM_ENV_FILE``, rồi ``phase2_llm/.env``, ``../.env`` (thư mục
    datamate), rồi ``.env`` ở thư mục làm việc hiện tại.

    Các biến (giá trị mặc định lấy đúng từ curl đã gửi, trừ điểm ghi ở
    dưới):
      - ``OPENAI_API_KEY``: bắt buộc, không có giá trị mặc định. Nếu
        thiếu, backend raise ``RuntimeError`` ngay khi khởi tạo, để
        runner fail-fast thay vì chạy hàng trăm câu rồi mới fallback.
      - ``OPENAI_BASE_URL``: mặc định ``https://api.openai.com/v1``.
      - ``OPENAI_MODEL`` (hoặc ``LLM_MODEL`` nếu không có): mặc định
        ``gpt-4o-mini``.
      - ``LLM_SERVICE_TIER``: mặc định ``default`` (theo curl).
      - ``LLM_TEMPERATURE``: mặc định ``1`` (theo curl). Lưu ý: với
        parser, temperature 1 cho kết quả không xác định giữa các lần
        chạy; nếu muốn trace ổn định hơn thì đặt ``0`` trong ``.env``.
      - ``LLM_MAX_OUTPUT_TOKENS``: mặc định ``2048`` (theo curl).
      - ``LLM_TOP_P``: mặc định ``1`` (theo curl).
      - ``LLM_STORE``: mặc định ``true`` (theo curl).
      - ``LLM_TIMEOUT_SECONDS``: mặc định ``120`` (không có trong curl,
        thêm để một lần gọi kẹt mạng không treo cả pipeline).

Hai điểm khác biệt có chủ ý so với curl gốc (để parser chạy đúng):
    1. Curl gốc đặt ``text.format.type = "text"`` (trả văn bản tự do).
       Parser cần JSON theo schema, nên backend đổi thành
       ``text.format = {"type": "json_schema", "name": "ParsedQuestion",
       "schema": schema, "strict": true}``. Schema được truyền vào từ
       ``llm_parser`` (``PARSED_QUESTION_JSON_SCHEMA``), backend không
       tự bịa schema riêng.
    2. Curl gốc có ``include: ["web_search_call.action.sources"]`` và
       ``tools: []``. Vì parser không dùng tool web search, backend
       không gửi trường ``include`` đó, cũng không bật tool nào — vừa
       tránh gọi search ngoài không cần thiết, vừa giữ kết quả chỉ phụ
       thuộc vào câu hỏi + dữ liệu shop đã đưa trong prompt.

Bảo mật:
    - Backend KHÔNG BAO GIỜ in, log, hay đưa key vào thông báo lỗi.
      Lỗi HTTP chỉ ghi status code (ví dụ 401 là key sai/thiếu quyền),
      không ghi header Authorization hay body có thể chứa key.
    - Chỉ dùng thư viện chuẩn ``urllib`` của Python, không cần cài thêm
      gói ``openai`` hay ``python-dotenv`` (môi trường hiện tại không có
      cả hai), nên file này chạy được ngay trên máy đã chạy phase2.

Interface:
    Class này có cùng chữ ký phương thức mà ``llm_parser.parse_with_llm``
    cần: ``generate_json(prompt, schema) -> dict``. Vì vậy runner phát
    triển ``run_phase2_openai.py`` chỉ cần truyền một instance của class
    này vào ``backend=``, không cần sửa ``llm_parser.py``, ``solver.py``
    hay ``shop_view.py``. Khi backend raise ``RuntimeError``, tầng
    ``parse_with_llm`` sẽ tự fallback về parser template theo đúng hợp
    đồng cũ (nhưng runner phát triển fail-fast trước khi chạy nếu key
    thiếu, để người dùng biết ngay là chưa cấu hình xong).
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

__all__ = [
    "OpenAIConfig",
    "OpenAIResponsesBackend",
    "load_dotenv",
]


# ---------------------------------------------------------------------------
# Đọc file .env bằng thư viện chuẩn (không cần python-dotenv)
# ---------------------------------------------------------------------------

def load_dotenv(env_file=None):
    """Nạp các biến trong file ``.env`` vào ``os.environ`` nếu chưa có.

    Đầu vào:
        env_file: đường dẫn file ``.env`` cụ thể, hoặc None để tự tìm
            theo thứ tự: biến ``LLM_ENV_FILE`` → ``phase2_llm/.env``
            (tính từ file này) → ``../.env`` → ``.env`` ở thư mục làm
            việc hiện tại.

    Đầu ra:
        Đường dẫn file đã nạp (dạng chuỗi), hoặc None nếu không tìm
        thấy file nào. Việc không tìm thấy file không phải là lỗi ở đây
        — người dùng có thể đã export biến môi trường trực tiếp; lỗi
        thiếu key sẽ được báo rõ ở bước khởi tạo backend.

    Quy tắc parse (đủ cho file .env thông thường, không cần parser đầy đủ):
        - Bỏ dòng trống và dòng bắt đầu bằng ``#``.
        - Dạng ``KEY=VALUE``; khoảng trắng quanh KEY/VALUE được cắt bỏ.
        - VALUE có thể bọc trong dấu nháy đơn hoặc kép, dấu nháy sẽ được
          gỡ ra. Dấu ``#`` trong VALUE bọc nháy được giữ nguyên.
        - Biến đã có sẵn trong ``os.environ`` KHÔNG bị ghi đè, vì biến
          môi trường của shell là nguồn đáng tin hơn file (xem docstring
          module). Giá trị rỗng trong file (``KEY=``) được coi là chưa
          đặt, để tránh việc file mẫu làm mất key đã export.
    """
    candidates = []
    if env_file:
        candidates.append(Path(env_file))
    env_file_from_env = os.environ.get("LLM_ENV_FILE")
    if env_file_from_env:
        candidates.append(Path(env_file_from_env))
    here = Path(__file__).resolve().parent
    candidates.append(here.parent / ".env")
    candidates.append(here.parent.parent / ".env")
    candidates.append(Path.cwd() / ".env")

    chosen = None
    for candidate in candidates:
        if candidate.is_file():
            chosen = candidate
            break
    if chosen is None:
        return None

    for raw_line in chosen.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        # Gỡ dấu nháy bọc ngoài nếu có (chỉ khi cặp nháy khớp nhau).
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not key:
            continue
        if key in os.environ and os.environ[key]:
            continue
        if value:
            os.environ[key] = value
    return str(chosen)


def _parse_float(raw, default):
    """Chuyển chuỗi sang float, sai định dạng thì dùng giá trị mặc định."""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _parse_int(raw, default):
    """Chuyển chuỗi sang int, sai định dạng thì dùng giá trị mặc định."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _parse_bool(raw, default):
    """Chuyển chuỗi sang bool, nhận true/false/1/0/yes/no (không phân biệt hoa thường)."""
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    return default


# ---------------------------------------------------------------------------
# Cấu hình backend
# ---------------------------------------------------------------------------

class OpenAIConfig:
    """Cấu hình đã giải quyết xong của backend OpenAI (chỉ chứa giá trị, không chứa logic gọi API).

    Đầu vào (__init__):
        env_file: đường dẫn file ``.env`` muốn nạp trước (None thì tự tìm).
        Các trường còn lại có thể truyền trực tiếp để ghi đè biến môi
        trường — phục vụ test (truyền key giả, model giả) mà không cần
        sửa ``os.environ`` thật.

    Lưu ý bảo mật: instance này giữ key trong bộ nhớ trong lúc chạy (bắt
    buộc để gọi API), nhưng không có phương thức nào in key ra, và
    ``__repr__`` được viết lại để không lộ key khi debug vô tình in object.
    """

    def __init__(self, env_file=None, api_key=None, base_url=None,
                 model=None, service_tier=None, temperature=None,
                 max_output_tokens=None, top_p=None, store=None,
                 timeout_seconds=None):
        load_dotenv(env_file)
        # Key: tham số truyền vào thắng, rồi tới biến môi trường / .env.
        # Không có giá trị mặc định — thiếu key là lỗi cấu hình, phải báo
        # rõ, không được tự bịa key hay gọi API không có key.
        self.api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        self.base_url = (
            base_url or os.environ.get("OPENAI_BASE_URL", "")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = (
            model or os.environ.get("OPENAI_MODEL", "")
            or os.environ.get("LLM_MODEL", "")
            or "gpt-4o-mini"
        )
        self.service_tier = (
            service_tier if service_tier is not None
            else os.environ.get("LLM_SERVICE_TIER", "default")
        )
        self.temperature = (
            temperature if temperature is not None
            else _parse_float(os.environ.get("LLM_TEMPERATURE"), 1.0)
        )
        self.max_output_tokens = (
            max_output_tokens if max_output_tokens is not None
            else _parse_int(os.environ.get("LLM_MAX_OUTPUT_TOKENS"), 2048)
        )
        self.top_p = (
            top_p if top_p is not None
            else _parse_float(os.environ.get("LLM_TOP_P"), 1.0)
        )
        self.store = (
            store if store is not None
            else _parse_bool(os.environ.get("LLM_STORE"), True)
        )
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None
            else _parse_int(os.environ.get("LLM_TIMEOUT_SECONDS"), 120)
        )
        if not self.api_key:
            raise RuntimeError(
                "Thiếu OPENAI_API_KEY: hãy điền key vào file phase2_llm/.env "
                "(copy từ .env.example) hoặc export OPENAI_API_KEY trước khi "
                "chạy. Không gửi key qua chat và không commit file .env."
            )

    def __repr__(self):
        # Cố tình không in key: chỉ in model và base_url để debug an toàn.
        return (
            f"OpenAIConfig(model={self.model!r}, base_url={self.base_url!r}, "
            f"api_key=***đã cấu hình***)"
        )


# ---------------------------------------------------------------------------
# Backend gọi OpenAI Responses API
# ---------------------------------------------------------------------------

class OpenAIResponsesBackend:
    """Backend phát triển gọi OpenAI Responses API để sinh JSON theo schema.

    Đây KHÔNG phải ``LocalModelBackend`` (xem cảnh báo ở docstring module).
    Nó chỉ có cùng phương thức ``generate_json(prompt, schema)`` để
    ``llm_parser.parse_with_llm`` dùng được mà không cần sửa code cũ.

    Đầu vào (__init__):
        config: một ``OpenAIConfig`` đã tạo sẵn, hoặc None để tự đọc từ
            biến môi trường / file ``.env``.
        env_file: đường dẫn file ``.env`` (chỉ dùng khi config là None).

    Raise:
        RuntimeError ngay khi khởi tạo nếu thiếu key, và khi gọi API gặp
        lỗi HTTP, lỗi mạng, hoặc phản hồi không chứa văn bản / văn bản
        không phải JSON. Caller (parse_with_llm) sẽ bắt lỗi này và
        fallback về template, đúng hợp đồng cũ — không bao giờ trả một
        JSON giả vờ để pipeline tưởng đã parse xong.
    """

    def __init__(self, config=None, env_file=None):
        self.config = config if config is not None else OpenAIConfig(env_file=env_file)

    def _build_payload(self, prompt, schema):
        """Dựng body JSON gửi tới Responses API từ prompt và schema.

        Hai điểm khác curl gốc đã giải thích ở docstring module: dùng
        ``json_schema`` thay cho ``text``, và không gửi ``include``
        web search vì parser không dùng tool nào.
        """
        payload = {
            "model": self.config.model,
            "input": prompt,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "ParsedQuestion",
                    "schema": schema,
                    "strict": True,
                }
            },
            "temperature": self.config.temperature,
            "max_output_tokens": self.config.max_output_tokens,
            "top_p": self.config.top_p,
            "store": self.config.store,
        }
        if self.config.service_tier:
            payload["service_tier"] = self.config.service_tier
        return payload

    @staticmethod
    def _extract_output_text(response):
        """Trích văn bản trả lời từ JSON của Responses API.

        API có thể trả trường tổng hợp ``output_text`` (thuận tiện nhất),
        hoặc mảng ``output`` chứa các item dạng message với content là
        ``output_text``. Hàm thử cả hai, và raise ``RuntimeError`` nếu
        không trích được gì — thay vì trả chuỗi rỗng để caller parse sai.
        """
        if isinstance(response, dict):
            aggregated = response.get("output_text")
            if isinstance(aggregated, str) and aggregated.strip():
                return aggregated
            for item in response.get("output", []) or []:
                if not isinstance(item, dict):
                    continue
                for part in item.get("content", []) or []:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        text = part.get("text", "")
                        if isinstance(text, str) and text.strip():
                            return text
        raise RuntimeError(
            "Phản hồi OpenAI không chứa văn bản output_text; "
            "không thể trích JSON ParsedQuestion."
        )

    def generate_json(self, prompt, schema):
        """Gọi Responses API một lần và trả dict JSON đã parse.

        Đầu vào:
            prompt: prompt đầy đủ do ``llm_parser.build_prompt`` tạo.
            schema: JSON Schema của ParsedQuestion (truyền nguyên vẹn
                vào ``text.format`` của API, không tự sửa schema).

        Đầu ra:
            dict là JSON mà model trả về (chưa qua validate của
            ``llm_parser`` — tầng đó sẽ kiểm chứng và repair nếu cần).

        Raise:
            RuntimeError khi lỗi HTTP (kèm status code, không kèm key),
            lỗi mạng/timeout, hoặc văn bản trả về không phải JSON hợp lệ.
        """
        url = f"{self.config.base_url}/responses"
        data = json.dumps(self._build_payload(prompt, schema)).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as handle:
                raw = handle.read()
        except urllib.error.HTTPError as exc:
            # Chỉ ghi status code, không ghi header/body (có thể lộ key
            # trong thông báo lỗi của một số proxy) và không retry ở đây:
            # retry vô hạn sẽ đốt quota và treo pipeline; caller fallback
            # về template là hành vi đúng theo hợp đồng.
            raise RuntimeError(
                f"OpenAI Responses API trả lời HTTP {exc.code}; "
                f"kiểm tra lại OPENAI_API_KEY và OPENAI_MODEL "
                f"({self.config.model})."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - lỗi mạng/timeout đều fallback
            raise RuntimeError(
                f"Không gọi được OpenAI Responses API ({type(exc).__name__}); "
                f"kiểm tra mạng và OPENAI_BASE_URL ({self.config.base_url})."
            ) from exc
        try:
            response = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                "Phản hồi OpenAI không phải JSON hợp lệ; không thể trích "
                "JSON ParsedQuestion."
            ) from exc
        text = self._extract_output_text(response)
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise RuntimeError(
                "Văn bản OpenAI trả về không phải JSON hợp lệ "
                f"(200 ký tự đầu: {text[:200]!r}); "
                "llm_parser sẽ repair hoặc fallback về template."
            ) from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(
                "JSON OpenAI trả về không phải object (dict); "
                "llm_parser sẽ repair hoặc fallback về template."
            )
        return parsed
