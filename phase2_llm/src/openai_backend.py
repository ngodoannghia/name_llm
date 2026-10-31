# -*- coding: utf-8 -*-
"""Backend OpenAI Chat Completions — CHỈ DÙNG CHO PHÁT TRIỂN (TEACHER).

Mục đích:
    Cho phép chạy tầng parser LLM của phase2_llm bằng model API mà người
    dùng cấu hình trong ``.env`` (ví dụ ``gpt-4o-mini`` từ curl đã gửi),
    trong giai đoạn phát triển, để tạo các trace ứng viên rồi kiểm chứng
    bằng solver (chỉ giữ trace nào cho đáp án đúng trên train). Đây là
    cách duy nhất để "chạy được" parser LLM khi máy chưa có model cục bộ
    ≤ 15B, weights, hay transformers/torch.

    Bản này dùng đúng dạng gọi trong curl mới nhất người dùng gửi:
    ``POST {base_url}/chat/completions`` với body gồm ``model``,
    ``messages`` (system + user), ``response_format: {"type": "json_object"}``
    và ``temperature: 0``. Bản trước dùng Responses API
    (``POST /responses`` với ``input`` + ``text.format json_schema``);
    bản đó đã bị thay thế hoàn toàn, không còn gửi tới ``/responses`` nữa.

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

    Các biến:
      - ``OPENAI_API_KEY``: bắt buộc, không có giá trị mặc định. Nếu
        thiếu, backend raise ``RuntimeError`` ngay khi khởi tạo, để
        runner fail-fast thay vì chạy hàng trăm câu rồi mới fallback.
      - ``OPENAI_BASE_URL``: mặc định ``https://api.openai.com/v1``.
      - ``OPENAI_MODEL`` (hoặc ``LLM_MODEL`` nếu không có): mặc định
        ``gpt-4o-mini``.
      - ``LLM_TEMPERATURE``: mặc định ``0`` (theo curl mới; temperature
        0 cho kết quả parser xác định nhất, khác bản Responses cũ mặc
        định 1).
      - ``LLM_MAX_OUTPUT_TOKENS``: mặc định ``2048``. Tên biến giữ nguyên
        để tương thích file ``.env`` cũ, nhưng khi gửi Chat Completions
        nó được map thành trường ``max_tokens`` (Chat Completions không
        có trường ``max_output_tokens``).
      - ``LLM_TOP_P``: mặc định ``1``.
      - ``LLM_TIMEOUT_SECONDS``: mặc định ``120`` (không có trong curl,
        thêm để một lần gọi kẹt mạng không treo cả pipeline).

Điểm khác biệt quan trọng so với bản Responses cũ (phải hiểu rõ):
    1. Curl mới dùng ``response_format: {"type": "json_object"}``, KHÔNG
       phải ``json_schema`` với ``strict: true``. Nghĩa là API chỉ đảm
       bảo trả về một object JSON hợp lệ, KHÔNG đảm bảo đúng trường,
       đúng enum, đúng kiểu của ParsedQuestion. Vì vậy tầng
       ``llm_parser.validate_and_build`` + repair tối đa 2 lần trở thành
       chốt chặn bắt buộc, không còn là dự phòng: JSON sai schema sẽ bị
       repair, repair không được thì fallback về template, chứ không
       được coi là parse thành công.
    2. Nội dung system/user trong curl ví dụ (trích xuất sản phẩm, các
       trường ``product_type``/``feature``, câu "bình sữa có van chống
       sặc") CHỈ là ví dụ minh họa dạng gọi, KHÔNG phải nội dung dùng
       cho DataMate. Backend này KHÔNG hard-code nội dung đó. Nội dung
       thật vẫn là prompt do ``llm_parser.build_prompt`` tạo (câu hỏi,
       thực thể shop, mô tả schema ParsedQuestion), gửi ở vị trí user;
       system message chỉ là chỉ thị chung "chỉ trả JSON duy nhất" kèm
       schema JSON nguyên vẹn để model thấy đúng tên trường/kiểu.
    3. Các trường Responses-specific của bản cũ (``service_tier``,
       ``store``, ``input``, ``text.format``, ``include`` web search)
       không còn được gửi, vì curl Chat Completions mới không dùng
       chúng. Biến ``LLM_SERVICE_TIER`` / ``LLM_STORE`` trong file
       ``.env`` cũ (nếu còn) sẽ bị bỏ qua, không còn ý nghĩa.

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
    "OpenAIChatBackend",
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
                 model=None, temperature=None,
                 max_output_tokens=None, top_p=None,
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
        # Theo curl mới: temperature 0, không còn service_tier/store.
        self.temperature = (
            temperature if temperature is not None
            else _parse_float(os.environ.get("LLM_TEMPERATURE"), 0.0)
        )
        self.max_output_tokens = (
            max_output_tokens if max_output_tokens is not None
            else _parse_int(os.environ.get("LLM_MAX_OUTPUT_TOKENS"), 2048)
        )
        self.top_p = (
            top_p if top_p is not None
            else _parse_float(os.environ.get("LLM_TOP_P"), 1.0)
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
# Backend gọi OpenAI Chat Completions API
# ---------------------------------------------------------------------------

class OpenAIChatBackend:
    """Backend phát triển gọi OpenAI Chat Completions để sinh JSON.

    Đây KHÔNG phải ``LocalModelBackend`` (xem cảnh báo ở docstring module).
    Nó chỉ có cùng phương thức ``generate_json(prompt, schema)`` để
    ``llm_parser.parse_with_llm`` dùng được mà không cần sửa code cũ.

    Đầu vào (__init__):
        config: một ``OpenAIConfig`` đã tạo sẵn, hoặc None để tự đọc từ
            biến môi trường / file ``.env``.
        env_file: đường dẫn file ``.env`` (chỉ dùng khi config là None).

    Raise:
        RuntimeError ngay khi khởi tạo nếu thiếu key, và khi gọi API gặp
        lỗi HTTP, lỗi mạng, hoặc phản hồi không chứa nội dung / nội dung
        không phải JSON. Caller (parse_with_llm) sẽ bắt lỗi này và
        fallback về template, đúng hợp đồng cũ — không bao giờ trả một
        JSON giả vờ để pipeline tưởng đã parse xong.
    """

    def __init__(self, config=None, env_file=None):
        self.config = config if config is not None else OpenAIConfig(env_file=env_file)

    def _build_system_message(self, schema):
        """Dựng system message cho Chat Completions từ schema.

        System message gồm chỉ thị chung (chỉ trả JSON duy nhất, không
        giải thích, không tính số đáp án) kèm schema JSON nguyên vẹn.
        Điều này quan trọng vì ``response_format: json_object`` chỉ ép
        JSON hợp lệ, không ép schema — model cần thấy schema thật ở
        system để trả đúng tên trường/kiểu, trước khi tầng validate của
        llm_parser kiểm chứng lại lần nữa.
        """
        instruction = (
            "Bạn là bộ phận parse câu hỏi của hệ thống DataMate. "
            "Chỉ trả DUY NHẤT một object JSON, không giải thích, "
            "không tính số đáp án, không thêm text ngoài JSON."
        )
        if schema is None:
            return instruction
        try:
            schema_text = json.dumps(schema, ensure_ascii=False)
        except (TypeError, ValueError):
            return instruction
        return (
            f"{instruction} JSON phải tuân thủ schema sau:\n{schema_text}"
        )

    def _build_payload(self, prompt, schema):
        """Dựng body JSON gửi tới Chat Completions, đúng dạng curl mới.

        - ``messages``: system (chỉ thị + schema) và user (prompt đầy đủ
          do llm_parser tạo: câu hỏi, thực thể shop, mô tả schema).
        - ``response_format``: ``{"type": "json_object"}`` — chỉ ép JSON
          object, không ép schema (xem docstring module).
        - ``temperature``: mặc định 0 theo curl mới.
        - ``max_tokens``: map từ biến cấu hình LLM_MAX_OUTPUT_TOKENS cũ.
        """
        return {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": self._build_system_message(schema)},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "max_tokens": self.config.max_output_tokens,
        }

    @staticmethod
    def _extract_content(response):
        """Trích nội dung văn bản từ JSON của Chat Completions.

        Dạng chuẩn: ``choices[0].message.content`` là chuỗi JSON.
        Hàm raise ``RuntimeError`` nếu không có choices, message rỗng,
        hoặc content rỗng — thay vì trả chuỗi rỗng để caller parse sai.
        """
        if isinstance(response, dict):
            choices = response.get("choices") or []
            if choices and isinstance(choices[0], dict):
                message = choices[0].get("message") or {}
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content
        raise RuntimeError(
            "Phản hồi OpenAI không chứa nội dung choices[0].message.content; "
            "không thể trích JSON ParsedQuestion."
        )

    @staticmethod
    def _strip_code_fence(text):
        """Gỡ khung markdown ```json ... ``` nếu model lỡ bọc JSON trong đó.

        Với ``response_format: json_object`` model không nên dùng fence,
        nhưng gỡ fence là an toàn (chỉ cắt phần bao ngoài, không sửa
        nội dung JSON bên trong) và giúp tránh một lỗi fallback không
        đáng có. Nếu sau khi gỡ vẫn không phải JSON thì caller vẫn raise.
        """
        cleaned = text.strip()
        if cleaned.startswith("```"):
            # Bỏ dòng mở ```json hoặc ```, và dòng đóng ``` cuối cùng.
            lines = cleaned.splitlines()
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        return cleaned

    def generate_json(self, prompt, schema):
        """Gọi Chat Completions một lần và trả dict JSON đã parse.

        Đầu vào:
            prompt: prompt đầy đủ do ``llm_parser.build_prompt`` tạo.
            schema: JSON Schema của ParsedQuestion — dùng để dựng system
                message (xem _build_system_message), không gửi riêng
                thành trường json_schema vì Chat Completions với
                json_object không nhận trường đó.

        Đầu ra:
            dict là JSON mà model trả về (chưa qua validate của
            ``llm_parser`` — tầng đó sẽ kiểm chứng, repair tối đa 2 lần
            nếu sai schema, rồi fallback về template nếu vẫn sai).

        Raise:
            RuntimeError khi lỗi HTTP (kèm status code, không kèm key),
            lỗi mạng/timeout, hoặc văn bản trả về không phải JSON hợp lệ.
        """
        url = f"{self.config.base_url}/chat/completions"
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
                f"OpenAI Chat Completions API trả lời HTTP {exc.code}; "
                f"kiểm tra lại OPENAI_API_KEY và OPENAI_MODEL "
                f"({self.config.model})."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - lỗi mạng/timeout đều fallback
            raise RuntimeError(
                f"Không gọi được OpenAI Chat Completions API ({type(exc).__name__}); "
                f"kiểm tra mạng và OPENAI_BASE_URL ({self.config.base_url})."
            ) from exc
        try:
            response = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                "Phản hồi OpenAI không phải JSON hợp lệ; không thể trích "
                "JSON ParsedQuestion."
            ) from exc
        text = self._strip_code_fence(self._extract_content(response))
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


# Tên cũ giữ lại để code/test cũ import được, nhưng nó giờ là backend
# Chat Completions, không còn là Responses API nữa.
OpenAIResponsesBackend = OpenAIChatBackend
