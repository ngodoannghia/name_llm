# Kế hoạch: Xử lý dữ liệu sang SQLite chuẩn bằng Google ADK

> Trạng thái: **PLAN — CHƯA triển khai, chờ người dùng duyệt.**
> Phạm vi: chỉ phần xử lý dữ liệu (ingest) — đọc file thô của một shop, làm sạch,
> ghi một SQLite DB và **một file schema cho mỗi shop**. Phần sinh SQL trả lời
> câu hỏi là workflow khác, dùng schema file này làm đầu vào.
> Ngôn ngữ code và chú thích: tiếng Việt đầy đủ dấu, UTF-8.
> Tuyệt đối không sửa dữ liệu train gốc, không hard-code theo business_id,
> tên cột hay rulebook của split train.

> **Rà soát theo source thật (2026-10-31):** plan này đã được đối chiếu với
> repo ADK đã clone tại `~/workspace/adk-python` (nhánh main, version
> **2.13.0**, commit `8cd3b12c`). Các điểm sửa quan trọng so với bản plan
> đầu tiên nằm ở §2.4 — đọc §2.4 trước khi triển khai, vì nó sửa cả cách
> làm loop, cách ép agent nạp skill, và cách dùng callback/artifact.
> **Thiết kế code chi tiết** (bố cục package, hợp đồng node/state/model,
> chuẩn code) nằm ở tài liệu đi kèm: `CODE_DESIGN.md` cùng thư mục.

---

## 1. Mục tiêu và đầu ra

### 1.1. Đầu vào

- Thư mục dữ liệu của một shop: CSV / XLSX (nhiều sheet) / SQLite.
- README của shop (nếu có), rulebook và calendar của split (chỉ để tham khảo
  vai trò bảng, không hard-code luật train vào code).

### 1.2. Đầu ra cho mỗi shop — đúng 3 thứ

| # | Đầu ra | Đường dẫn (đề xuất) | Ghi chú |
|---|--------|---------------------|---------|
| 1 | SQLite DB của shop | `sqlite/<business_id>.db` | Một DB/shop, giữ layout riêng của shop, không ép format chung |
| 2 | **File schema của shop — 1 file/shop** | `sqlite/schemas/<business_id>.json` | Hợp đồng duy nhất mà workflow trả lời câu hỏi được phép đọc (xem §6) |
| 3 | Log ingest của shop | `logs/ingest/<business_id>.jsonl` | Event stream của ADK cho shop đó (xem §9) |

### 1.3. Ràng buộc bắt buộc

1. **LLM chỉ quyết định trên mẫu và schema, code mới làm hàng loạt.**
   LLM không bao giờ đọc toàn bộ 4 triệu dòng, không làm sạch từng ô.
   Nó chỉ xem header + tối đa vài chục dòng mẫu + thống kê, còn pandas và
   bộ parse tiền/ngày/số lượng chạy toàn bộ dữ liệu.
2. **Mọi đề xuất của LLM đều bị code kiểm chứng.** Đề xuất không qua kiểm
   chứng thì không được ghi vào DB hay schema (xem §5, §8).
3. **Không đoán.** Ô không parse được → NULL và đếm vào `_clean_stats`,
   không điền giá trị bịa. Bảng/shop không xử lý được → đánh dấu FAILED
   rõ ràng, không xuất DB xấu một cách im lặng.
4. **Gộp sheet theo bằng chứng, không gộp bừa theo file.** Các sheet là
   mảnh của cùng một bảng logic (cùng chữ ký cột chuẩn hóa, cùng vai trò,
   ID/ngày liên tục) thì gộp thành một bảng trước khi mô tả và truy vấn.
   Sheet khác vai trò trong cùng một file thì giữ riêng. Biến thể chi tiết
   lô (ChiTiet vs ChiTietLo) phải so sánh toàn bộ schema trước khi gộp,
   tránh đếm trùng.
5. **Offline ở Docker cuối.** Ingest shop mới trong Docker phải chạy bằng
   model local ≤ 15B, không gọi API thương mại, không tải gì về.
6. **Không fallback template ở tầng trả lời.** Lưu ý phân biệt: ở ingest,
   khi LLM đề xuất sai và code bác bỏ thì quay về luật đã kiểm chứng bằng
   mẫu là được (đó là luật làm sạch, không phải đoán đáp án câu hỏi).

---

## 2. Cơ sở ADK đã đọc kỹ — dùng cái gì, không dùng cái gì

Kế hoạch này bám sát ADK, cụ thể là các khối sau (đọc từ tài liệu ADK
chính thức tại adk.dev và hướng dẫn xây dựng agent bằng Skills):

### 2.1. Các khối ADK được dùng

| Khối ADK | Cách dùng trong ingest | Vì sao dùng |
|----------|------------------------|-------------|
| **Workflow Runtime (ADK 2.0) — graph-based workflow** | Toàn bộ ingest là một workflow graph: mỗi Agent/Tool/Function là một node, các cạnh (edge) điều khiển thứ tự, rẽ nhánh và loop. | Đây là cách ADK 2.0 thay thế SequentialAgent/ParallelAgent/LoopAgent (các lớp này vẫn chạy nhưng đã bị đánh dấu deprecated trong 2.0). Graph cho đường đi tường minh, kết quả dự đoán được, loop có điều kiện dừng rõ ràng — đúng thứ ingest cần. |
| **LlmAgent + `output_schema` (Pydantic)** | HeaderAgent, MergeAgent, SchemaAgent đều trả JSON có cấu trúc qua `output_schema`, không trả văn bản tự do. | Output có cấu trúc thì validator code mới kiểm chứng được một cách máy móc. Văn bản tự do là nguồn gốc của lỗi parse và bịa giá trị. |
| **FunctionTool** | Mọi bước xác định đều là hàm Python thuần (docstring của hàm trở thành schema của tool): quét file, làm sạch, ghi SQLite, kiểm chứng. | Tool là code, chạy nhanh, deterministic, không tốn lần gọi model. |
| **SkillToolset + Agent Skills (đặc tả agentskills.io)** | Kiến thức ingest được đóng gói thành 4 Skill (mỗi Skill là một thư mục có `SKILL.md` + `references/`), agent nạp skill theo nhu cầu bằng 3 tool tự sinh: `list_skills` (L1), `load_skill` (L2), `load_skill_resource` (L3). | Progressive disclosure: mỗi skill chỉ tốn ~100 token metadata ở L1 thay vì nhét hàng nghìn token hướng dẫn vào một system prompt duy nhất (giảm ~90% context nền). Agent chỉ nạp đúng skill nó cần ở đúng bước. Xem §4. |
| **Runner + SessionService** | Một Session cho một shop (`app_name=datamate-ingest`, `session_id=<business_id>`). Dev dùng `InMemorySessionService`; chạy batch/Docker dùng `DatabaseSessionService` trên SQLite local để có thể resume shop bị lỗi. | Session là đơn vị làm việc và phục hồi: shop lỗi thì chạy lại đúng session đó, không chạy lại 30 shop. |
| **State qua Event (`output` / `message` / `state`)** | Các node truyền dữ liệu cho nhau qua Event: node ghi `output` cho node sau, `state` được giữ xuyên suốt session. | Đây là cơ chế truyền dữ liệu chính thức của Workflow Runtime. State chỉ giữ dữ liệu nhỏ dạng text (đường dẫn, thống kê, mẫu, quyết định) — không bao giờ giữ toàn bộ bảng. |
| **ArtifactService** | Schema file và log ingest được đăng ký làm artifact của session (tên + phiên bản), đồng thời Writer tool ghi file vật lý ra disk để workflow trả lời câu hỏi đọc được. | Artifact là registry logic (tên, phiên bản, session sở hữu); file vật lý là thứ `predict.sh` và workflow SQL thực sự đọc. Hai thứ này đi cùng nhau, không thay thế nhau. Dev dùng `InMemoryArtifactService`, Docker dùng artifact local gắn với thư mục làm việc. |
| **Callbacks** | `before_tool`, `after_tool`, `on_tool_error`, `before_model`, `after_model`, `before_agent`, `after_agent` (xem §8). | Callbacks là nơi đặt guardrail: chặn ghi ngoài đường dẫn của shop, chặn SQL không phải SELECT ở bước kiểm chứng, cắt mẫu, validate JSON, biến exception thành payload để loop viết lại. Gate luôn là code, không phải LLM tự chấm mình. |
| **Events (kèm `node_info`)** | Mọi bước đều phát Event; Event stream của session được ghi thành `logs/ingest/<business_id>.jsonl`. | Event là audit trail: shop nào, node nào, đề xuất gì, lỗi gì, viết lại mấy lần, kết quả cuối. Khi câu trả lời sai, nhìn log ingest là biết lỗi từ dữ liệu hay từ SQL. |
| **Model-agnostic qua LiteLlm** | Dev dùng LiteLlm nối Gemini/OpenAI (chỉ để dạy và thử); Docker cuối dùng LiteLlm nối endpoint model local (Ollama/vLLM) trong container. | Cùng một workflow, đổi model bằng cấu hình, không đổi code. Model cuối vẫn phải ≤ 15B, phát hành công khai trước 2026-10-01, có licence cho phép sử dụng. |
| **adk eval (evalsets)** | Bộ eval cho các quyết định của LLM: detect header, quyết định gộp, mô tả schema — chạy trên các ca khó của train (xem §11). | `adk eval` đo trajectory (quyết định + lần gọi tool), nhưng không đủ một mình: ingest còn cần kiểm chứng xác định (đếm dòng, SQL mẫu). Hai thứ đi cùng nhau. |

### 2.2. Các khối ADK KHÔNG dùng — và lý do

| Khối | Lý do không dùng |
|------|------------------|
| MemoryService (long-term memory, VertexAiMemoryBank) | Ingest batch không cần nhớ xuyên shop. Nhớ kiểu "shop trước làm thế này" chính là con đường dẫn tới hard-code theo train. Mỗi shop xử lý độc lập từ dữ liệu của chính nó. |
| `adk web` / `adk api_server` / Agent Engine | Đó là công cụ dev và dịch vụ cloud. Bài toán cần batch driver bằng code gọi Runner trực tiếp trong `predict.sh`, không cần server. |
| MCPToolset / RemoteA2aAgent | Ingest không cần gọi server ngoài hay agent từ xa; mọi tool đều là hàm local. Thêm MCP chỉ thêm điểm lỗi và vi phạm offline. |
| Plugin observability cloud (Cloud Trace, BigQuery Agent Analytics) | Docker thi phải offline. Log dùng file event local (§9), không đẩy lên cloud. Plugin chỉ dùng nếu là plugin log local tự viết. |

### 2.3. Quyết định về phiên bản ADK — ĐÃ CHỐT THEO SOURCE THẬT

- **Chốt: ADK 2.13.0, Workflow graph.** Không còn là khuyến nghị mở:
  trong source Python 2.13.0, `LoopAgent`, `SequentialAgent`,
  `ParallelAgent` đều đã gắn decorator `@deprecated` với thông báo "in
  favor of Workflow and will be removed in a future version"
  (`src/google/adk/agents/loop_agent.py:59-61` và tương tự ở hai file kia).
  Các class vẫn chạy được trong bản này (chỉ phát `DeprecationWarning`),
  nhưng dùng mới là chấp nhận nợ kỹ thuật sẽ bị xóa.
- **Người dùng chốt ngày 2026-10-31: dùng theo hướng ADK 2.0.**
  Tức là Workflow graph + bộ đếm vòng lặp tự làm (state + node route),
  **không** dùng `LoopAgent` dù nó có sẵn `max_iterations`. Quyết định này
  đóng luôn tranh luận ở §2.4 mục 1: giới hạn 3 lần viết lại được thực
  hiện bằng bộ đếm tay trong Workflow, không bằng `LoopAgent`.
- **Phương án dự phòng duy nhất:** dùng `LoopAgent` tạm nếu G0 spike phát
  hiện Workflow graph thiếu thứ gì đó mà LoopAgent có (ví dụ
  `max_iterations` tích hợp sẵn — xem §2.4 mục 1). Nếu dùng dự phòng thì
  ghi rõ trong báo cáo G0 là nợ kỹ thuật.
- **Pin version + constraints file ngay ở G0:** `google-adk==2.13.0` với
  `constraints-3.12.txt` của repo (VM đang dùng Python 3.12, ADK yêu cầu
  Python ≥ 3.11). Skills vẫn tự nhận là experimental trong README của
  package (`src/google/adk/skills/README.md`), nên pin version là bắt buộc
  chứ không phải khuyến nghị.
- **Một hạn chế thừa nhận trong chính thông báo deprecated:** "Workflow
  cannot yet be used as an LlmAgent sub-agent". Ingest không cần kiểu đó
  (Workflow làm root, chạy qua Runner), nên hạn chế này không ảnh hưởng.
- **Tài liệu nội bộ của repo nên đọc ở G0:** `.agents/skills/` trong repo
  chứa `adk-architecture`, `adk-setup`, `adk-agent-builder`... — sát với
  bản 2.13.0 hơn tài liệu web.

### 2.4. Những điểm plan ban đầu chưa đúng — đã sửa theo source thật

Đọc kỹ từng điểm này trước khi code, vì mỗi điểm đều đổi thiết kế:

**1. Workflow KHÔNG có bộ đếm vòng lặp tích hợp sẵn.**
Không có `max_loop_iterations` hay điều kiện `while` trên `Workflow`
(đã grep trong source). "Tối đa 3 lần viết lại" phải tự làm: một
function node tăng `ctx.state["<tên>_attempts"]` mỗi lần validator fail,
rồi node route kiểm tra `attempts >= 3` để phát route `"fail"` thay vì
`"retry"`. Route chỉ nhận `bool | int | str` — node route phải trích ra
một chuỗi (`"ok"` / `"retry"` / `"fail"`) rồi mới `Event(route=...)`.
Cycle quay lại node trước **bắt buộc phải có route** — validator từ chối
mọi cycle toàn cạnh không-route ("Cycles must include at least one
conditional (routed) edge"). Import đúng: `from google.adk import
Workflow` và `from google.adk.workflow import START` (START không export
từ `google.adk`). Tên node phải là Python identifier hợp lệ, và muốn
dùng lại cùng một node ở nhiều chỗ (quay lại `header_agent` trong loop)
thì phải truyền **đúng cùng một object instance**. Chỉ có một node
terminal được ra output của workflow — nếu fan-out nhiều nhánh cùng ra
output thì workflow lỗi lúc kết thúc, phải gộp qua `JoinNode`.

**2. "Validator fail thì loop lại" KHÔNG dùng RetryConfig.**
`RetryConfig` chỉ kích hoạt khi node **raise exception** (mặc định
`max_attempts=5` tính cả lần đầu, chỉ dùng cho lỗi transient như timeout
gọi model hay lỗi đọc file). Một validator trả output "fail" mà không
raise thì **không** trigger retry — vòng "viết lại khi sai" phải làm bằng
**cạnh route có điều kiện** như §5. Hai cơ chế này không được lẫn nhau.

**3. Node lỗi là dừng cả workflow — nên "fail" phải là route, không phải exception.**
Không có cờ "continue on error" trên node. Validator muốn kết luận shop
FAILED thì phải trả output `{"status": "failed"}` và route tới node ghi
log/kết luận (không raise), thay vì raise exception. Chỉ raise khi muốn
dừng thật. Điều này cũng khớp với semantics resume: node đã hoàn thành
được replay từ session events, không chạy lại — nên node ghi SQLite phải
idempotent, và plan giữ nguyên tắc ghi DB tạm rồi rename nguyên tử.

**4. Skill là dữ liệu thụ động — framework KHÔNG ép agent nạp đúng skill ở đúng bước.**
`Skill` chỉ là cấu trúc dữ liệu mà tool `load_skill` đọc ra; không có cơ
chế nào trong `SkillToolset` bắt agent ở một bước nhất định phải load một
skill nhất định. Cách chắc chắn để "đúng skill ở đúng bước" là: **mỗi
bước dùng một Agent riêng, `SkillToolset` của agent đó chỉ chứa đúng 1
skill đó, và instruction của agent ghi rõ tên skill phải load đầu tiên**
(ví dụ HeaderAgent: "trước mọi việc, gọi `load_skill` với
`skill_name='datamate-header'`"). Với catalog nhỏ ổn định này có thể dùng
`discovery_mode=EAGER` để bỏ lượt `list_skills` và chèn catalog XML vào
system instruction. Ngoài ra: `load_skill_from_dir` nạp toàn bộ skill vào
RAM ngay lúc gọi (kể cả references/assets/scripts) — progressive
disclosure chỉ tiết kiệm **token prompt**, không tiết kiệm RAM load.

**5. Frontmatter và giới hạn của Skill (theo code thật).**
Cấu trúc thư mục: `SKILL.md` (bắt buộc) + `references/` + `assets/` +
`scripts/` (tùy chọn). Frontmatter chấp nhận: `name` (bắt buộc, kebab-case
≤ 64 ký tự, **phải trùng đúng tên thư mục**), `description` (bắt buộc,
không rỗng, ≤ 1024 ký tự), `license`, `compatibility`,
`allowed_tools`/`allowed-tools`, `metadata`. Hai bẫy: `allowed_tools`
**chỉ được parse và giữ lại, không dùng để hạn chế gì** — không được dựa
vào nó để cấm tool; và `metadata.adk_additional_tools` phải là list chuỗi
khai đúng tên tool đã truyền vào `SkillToolset(additional_tools=[...])`
— sai chính tả thì tool lặng lẽ không xuất hiện, không cảnh báo, và tool
đã thêm sẽ ở lại đến hết session (trừ khi bật `SKILL_LIFECYCLE`, mặc định
tắt). Muốn body skill chứa giá trị quét được thì cần
`metadata.adk_inject_state: true` và dùng `{var?}` trong body. Script
trong `scripts/` chỉ chạy được khi có code executor/environment (không
có thì `run_skill_script` trả lỗi `NO_CODE_EXECUTOR`, timeout mặc định
300 giây cho script shell) — nên code làm sạch hàng loạt vẫn ở
FunctionTool, không đặt vào `scripts/`. Registry skill đi kèm duy nhất
(`GCPSkillRegistry`) cần Google Cloud — Docker offline chỉ dùng skill
local qua `load_skill_from_dir`.

**6. Callback: ngưỡng dừng khác nhau, và chỉ `before_tool` mới chặn được.**
`CallbackContext` và `ToolContext` trong bản 2.13.0 đều là alias của một
class `Context` duy nhất. Danh sách callback thật: `before_agent` /
`after_agent` (mọi agent), `before_model` / `after_model` /
`on_model_error` / `before_tool` / `after_tool` / `on_tool_error`
(LlmAgent). Ngưỡng dừng: `before_model` dừng theo **truthy**,
`before_tool` dừng theo **non-None (kể cả `{}`)** — không được lẫn hai
ngưỡng này. `after_tool` **không chặn được** ghi sai hay SQL xấu vì tool
đã chạy xong trước khi nó được gọi — mọi guardrail chặn ghi ngoài thư mục
shop hay chặn SQL không phải SELECT phải đặt ở `before_tool_callback`
trả dict non-None. Plugin chạy trước callback của agent: nếu plugin đã
trả kết quả thì callback của agent bị suppress hoàn toàn. Không có
callback chặn path/SQL có sẵn trong ADK — phải tự viết. Không có field
`on_agent_error_callback` trên agent (chỉ có ở tầng plugin).

**7. LlmAgent: nhiệt độ và output_schema không đặt như cách nghĩ thông thường.**
Không có tham số `temperature` trực tiếp trên `LlmAgent` — phải qua
`generate_content_config=types.GenerateContentConfig(temperature=0)`,
truyền nhầm sẽ bị validator từ chối; tương tự, `response_schema` phải đặt
qua `LlmAgent(output_schema=...)`, không đặt trong
`generate_content_config`. `output_schema` **không đảm bảo** model luôn
trả đúng schema: khi có function tool mà model không có capability
`output_schema_and_tools`, ADK dùng workaround tool `set_model_response`
thay vì `response_schema` native; và validation cuối chỉ kiểm tra — JSON
sai sẽ raise lỗi chứ không tự sửa, nên loop viết lại phải bắt lỗi
validation này và đẩy lại cho agent. Model local qua LiteLlm có thể rơi
vào trường hợp capability False, nên G0 phải đo tỷ lệ JSON hợp schema
của chính model local sẽ dùng. FunctionTool: không cần decorator, truyền
thẳng hàm vào `tools=[func]` là agent tự bọc; toàn bộ docstring sau
cleandoc thành description; `*args`/`**kwargs` bị loại khỏi schema; một
annotation không resolve được sẽ làm **mất toàn bộ schema tham số**;
tham số context khai báo bằng annotation `ToolContext`/`Context` hoặc
tên `tool_context`; validation kiểu argument chỉ bật khi feature flag
`FUNCTION_TOOL_ARG_VALIDATION` được bật.

**8. Runner/Session/Artifact: tên implementation và việc phải tự làm.**
Batch driver phải tạo session trước bằng
`session_service.create_session(...)` hoặc bật `auto_create_session=True`
trên `Runner` thường (mặc định `False`; `InMemoryRunner` chỉ dùng cho
test và không nhận cờ này). `DatabaseSessionService` cần URL với async
driver (`sqlite+aiosqlite:///`) và extra `google-adk[db]`
(`sqlalchemy[asyncio]`); có `SqliteSessionService(db_path)` gọn nhẹ hơn
làm phương án thay thế. Resume đi qua `get_session` (tạo lại session với
id đã tồn tại sẽ raise `AlreadyExistsError`); key state tiền tố `temp:`
không được persist; mọi giá trị state phải JSON-safe. Artifact local có
sẵn implementation: **`FileArtifactService(root_dir=...)`** (layout
`root/apps/.../sessions/.../artifacts/.../versions/`, stage vào file
`.pending` rồi rename publish) — không cần tự viết. Artifact version đầu
tiên là 0. ADK **không có hàm export event ra file** — batch driver phải
tự serialize từng event từ `run_async` ra `.jsonl` (bytes trong event sẽ
thành base64 khi dump JSON). Model chỉ thấy tên artifact trước, nội dung
chỉ gắn vào request khi nó gọi `load_artifacts` — và file SQLite binary
không được đưa cho model dưới dạng artifact, phải đọc bằng tool SQL của
code. Model mặc định khi không cấu hình là `gemini-3.5-flash` — mọi agent
đều phải set model tường minh.

**9. adk eval: trajectory không đủ để kết luận ingest đúng.**
`adk eval` chạy agent thật (tốn lần gọi model) với root có thể là
`Workflow`, evalset file `.evalset.json`. Nhưng `TrajectoryEvaluator`
chỉ so sánh **tên tool + argument của tool call** (EXACT / IN_ORDER /
ANY_ORDER, có tùy chọn `ignore_args`), không so sánh kết quả tool trả về,
không so sánh state cuối, không so sánh JSON agent sinh ra. Vì vậy
trajectory chỉ đo được "agent có gọi đúng validator, đúng thứ tự, có
loop lại sau lỗi không" — còn "detect header đúng / gộp đúng / schema
đúng" phải chấm thêm bằng `final_session_state` của EvalCase và bằng
validator code N2/N3/N6. Đừng dùng trajectory score làm chỉ số duy nhất
cho ingest.

**10. LiteLlm cho model local: chưa có ví dụ chính thức, phải spike chạy thật.**
`LiteLlm(model=...)` nhận đúng một tham số bắt buộc là `model`; `api_base`
đi qua `**kwargs` vào `completion_args` của `litellm.acompletion`. Trong
source ADK **không có ví dụ Ollama/vLLM** nào — cách khai báo chính xác
cho từng loại endpoint phải được chứng minh bằng một lần chạy thật với
endpoint local ở G0, không chốt chỉ từ đọc code.

**11. Chưa xác minh, để lại cho G0 spike.**
Chạy Workflow lần đầu với `new_message=None` hay cần một `Content` mồi;
cơ chế chi tiết của `ResumabilityConfig`; ngưỡng kích thước state thực tế
(chỉ biết phải JSON-safe, không có số byte cụ thể trong source).

---

## 3. Kiến trúc tổng thể

```
predict_ingest.py (batch driver, code thuần)
  └─ for mỗi business_id (một shop tại một thời điểm, tiết kiệm RAM):
       └─ Runner.run_async(IngestWorkflow, session_id=business_id)
            └─ IngestWorkflow (ADK Workflow graph, §5)
                 ├─ Node code (FunctionNode): quét, làm sạch, ghi DB, kiểm chứng
                 ├─ Node LLM (AgentNode): HeaderAgent, MergeAgent, SchemaAgent
                 ├─ Node điều kiện: rẽ nhánh SQLite/CSV/XLSX, rẽ nhánh loop/thoát
                 └─ State + Artifacts + Events của session đó
  └─ Ghi ingest_report.json (tổng hợp 30 shop: OK / FAILED + lý do)
```

Nguyên tắc bộ nhớ: một shop tại một thời điểm. Nạp shop, xử lý xong, ghi
DB + schema + log, giải phóng, mới sang shop tiếp theo. Đây cũng là kiến
trúc Docker cuối (không nạp 30 shop cùng lúc).

---

## 4. Bốn Skill ingest (Agent Skills, định dạng SKILL.md)

Mỗi Skill là một thư mục: `SKILL.md` (mô tả ngắn + hướng dẫn + khi nào
dùng) và `references/` (tài liệu dài chỉ nạp khi cần: ví dụ, bảng trạng
thái, ca khó). Agent chỉ thấy L1 (tên + mô tả ~100 token) lúc khởi động;
khi bước vào đúng node, nó gọi `load_skill` để lấy L2 (hướng dẫn đầy đủ),
và `load_skill_resource` để lấy L3 khi cần ví dụ chi tiết.

| Skill | Dùng ở node | Nội dung chính (L2) |
|-------|-------------|---------------------|
| `datamate-header` | HeaderAgent | Cách nhận diện dòng header thật trong sheet bẩn: bỏ dòng tiêu đề/logo/trống, header có thể không ở dòng 1; chấm điểm header (tỷ lệ ô text, trùng lặp, kiểu dữ liệu cột bên dưới); khi nào thì header không chắc và phải hỏi validator; ví dụ sheet 2 tầng tiêu đề, sheet có dòng tổng ở cuối. |
| `datamate-merge` | MergeAgent | Tiêu chí gộp: cùng chữ ký cột chuẩn hóa (tên cột sau khi bỏ dấu, viết thường, bỏ ký tự thừa), cùng vai trò, ID/ngày liên tục, không trùng ID vô lý. Cấm: gộp sheet khác vai trò chỉ vì cùng file; gộp ChiTiet với ChiTietLo khi chưa so sánh toàn bộ schema (nguy cơ đếm trùng); gộp khi ID overlap mâu thuẫn. |
| `datamate-cleaning` | CleanTables (tài liệu tham khảo cho tool, không phải prompt tự do) | Quy tắc parse tiền (bỏ dấu phân cách, xử lý "1.200.000", "1,200,000", "1.2tr"), ngày (nhiều định dạng text, kể cả "1 thg 1 2025"), số lượng và quy đổi đơn vị (chỉ quy đổi khi có hệ số, không có hệ số thì để trống và đếm, không đoán = 1); forward-fill; dedup; kiểu suy luận (tiền → INTEGER VND, ngày → TEXT ISO, số lượng → số). |
| `datamate-schema` | SchemaAgent | Cấu trúc file schema (§6); quy tắc mô tả: mô tả bằng tiếng Việt, nêu vai trò của cột (mã đơn, mã hàng, ngày, tiền, số lượng, trạng thái...); mẫu và distinct phải lấy từ DB thật, cấm bịa; distinct chỉ liệt kê đầy đủ khi ≤ 50 giá trị (đặc biệt quan trọng với cột trạng thái); ghi chú join phải có bằng chứng (tên cột chung + ID overlap đo được). |

Lưu ý: Skill chứa **hướng dẫn và ví dụ**, không chứa code thực thi.
Code thực thi nằm ở FunctionTool và validator (§7, §8). Cách tách này
giúp đổi hướng dẫn không cần đổi code, và đổi code không cần đổi skill.

---

## 5. Workflow graph chi tiết — từng node, cạnh và loop

Ký hiệu: `[code]` = FunctionNode chạy code xác định, không gọi LLM.
`[LLM]` = AgentNode chạy LlmAgent. `→` = cạnh thường, `↺` = cạnh loop
quay lại khi validator báo lỗi, `◇` = node điều kiện rẽ nhánh.

```
START
  │
  ▼
N0  init_shop [code]
    • Đọc README của shop (tối đa 4000 ký tự đưa vào state, không đưa cả file).
    • Liệt kê file của shop, phân loại CSV / XLSX / SQLite.
    • Khởi tạo state: business_id, danh sách file, trạng thái từng bảng = pending.
    • Ghi event bắt đầu.
  │
  ▼
◇   branch_input: SQLite → N1b, ngược lại → N1a
  │
  ├─► N1a scan_sheets [code]
  │     • Với mỗi sheet/CSV: detect header bằng scoring (quét tối đa 15 dòng
  │       đầu, giống Phase 1 đã kiểm chứng), đọc mẫu (10 dòng đầu + 10 dòng
  │       ngẫu nhiên có seed cố định ở giữa/cuối để bắt cột thưa), thống kê
  │       (số dòng, distinct ID, khoảng ngày).
  │     • CSV: sniff delimiter/encoding trước khi đọc.
  │     • Output vào state.raw_tables: mỗi bảng có header ứng viên + điểm
  │       tin cậy + mẫu + thống kê. Bảng nào điểm tin cậy thấp → cờ ambiguous.
  │
  └─► N1b scan_sqlite [code]
        • Đọc sqlite_master lấy schema, không qua bước detect header.
        • Với mỗi bảng: mẫu + thống kê như N1a, cờ ambiguous chỉ khi tên
          bảng/cột không rõ vai trò.
  │
  ▼
N2  header_loop ↺ [LLM + code]
    • Chỉ chạy cho bảng có cờ ambiguous. Bảng đã chắc thì đi thẳng qua.
    • HeaderAgent [LLM]: nạp skill datamate-header, đề xuất header qua
      output_schema HeaderProposal {dòng header, tên cột chuẩn hóa, lý do}.
    • HeaderValidator [code]: chấm lại header đề xuất bằng scoring + kiểm
      tra mẫu (cột phải có giá trị thật, không phải toàn trống).
    • Sai → exception + điểm scoring được đẩy lại cho HeaderAgent viết lại.
    • Đúng → ghi header chốt vào state. Hết 3 lần vẫn sai → quay về luật
      scoring (đã kiểm chứng bằng mẫu), ghi rõ vào log là "rule fallback",
      không dừng cả shop vì một bảng.
  │
  ▼
N3  merge_loop ↺ [LLM + code]
    • MergeAgent [LLM]: nạp skill datamate-merge, đề xuất nhóm gộp qua
      output_schema MergeProposal {nhóm bảng, bằng chứng gộp, bảng giữ riêng}.
    • MergeValidator [code]: kiểm từng nhóm — cùng chữ ký cột chuẩn hóa,
      cùng vai trò, ID overlap không mâu thuẫn, ngày liên tục; kiểm riêng
      trường hợp lot-detail bằng so sánh toàn bộ schema.
    • Sai → lỗi validator được đẩy lại cho MergeAgent viết lại.
    • Đúng → ghi merge_groups vào state. Hết 3 lần vẫn sai → giữ mỗi sheet
      là một bảng riêng (an toàn hơn gộp sai gây đếm trùng), ghi rõ vào log.
  │
  ▼
N4  clean_tables [code, không LLM]
    • Với mỗi bảng đã gộp: pandas + normalizers (parse tiền/ngày/số lượng
      theo skill datamate-cleaning), forward-fill, dedup sau gộp, quy đổi
      đơn vị (thiếu hệ số thì để trống + đếm, không đoán).
    • Suy luận kiểu cho từng cột, đếm ô không parse được vào _clean_stats.
    • Output: các DataFrame đã sạch trong bộ nhớ tạm của node (không đưa
      vào state — state chỉ giữ thống kê và đường dẫn tạm).
  │
  ▼
N5  write_sqlite [code]
    • Sanitize tên bảng/cột thành identifier SQL hợp lệ, giữ bảng map
      tên gốc → tên SQL.
    • Ghi vào DB TẠM `<business_id>.tmp.db`: các bảng đã sạch + 3 bảng meta
      `_shop_meta` (shop, nguồn file, thời gian), `_schema_map` (tên gốc →
      tên SQL), `_clean_stats` (số dòng vào/ra, số ô NULL do không parse
      được, số dòng trùng đã bỏ).
    • Chưa publish: DB tạm chỉ thành DB chính thức khi N7 kiểm chứng đạt.
  │
  ▼
N6  schema_loop ↺ [LLM + code]
    • SchemaAgent [LLM]: nạp skill datamate-schema, sinh file schema của
      shop qua output_schema ShopSchema (cấu trúc §6), từ: bảng đã gộp,
      mẫu, distinct, README, bảng map tên.
    • SchemaValidator [code]: kiểm JSON đúng schema; mọi bảng/cột trong DB
      tạm đều có mặt trong schema và ngược lại; mọi sample_values và
      distinct_values đều có thật trong DB (kiểm bằng truy vấn); distinct
      liệt kê đầy đủ thì số lượng phải khớp với COUNT(DISTINCT) đo được.
    • Sai → lỗi validator (kèm giá trị thật đo được) được đẩy lại cho
      SchemaAgent viết lại. Hết 3 lần vẫn sai → shop FAILED (schema sai
      thì workflow trả lời sẽ sinh SQL sai, không được xuất schema xấu).
  │
  ▼
N7  validate_shop [code]
    • Mở lại DB tạm (không dùng DataFrame trong bộ nhớ): đếm dòng từng
      bảng đối chiếu với profile Phase 1 (nếu có); chạy một số SQL mẫu
      (tổng tiền theo trạng thái, đếm đơn theo tháng) để chắc DB truy vấn
      được; kiểm schema file parse được và khớp DB.
    • Đạt → publish: đổi tên DB tạm thành `<business_id>.db` (atomic
      rename), đăng ký schema file và DB làm artifact của session.
    • Không đạt → cạnh quay về node gây lỗi tương ứng (N2/N3/N4/N6),
      tối đa 2 vòng toàn shop; quá → shop FAILED, DB tạm bị xóa, chỉ
      giữ log và lý do lỗi.
  │
  ▼
N8  finalize [code]
    • Ghi ingest_report cho shop (OK/FAILED, số bảng, số dòng, số ô NULL,
      số lần loop, node nào từng fallback) và đóng session.
END
```

### Loop và điều kiện dừng — áp dụng thống nhất cho N2, N3, N6

Cơ chế loop theo code thật của Workflow (xem §2.4 mục 1–3): mỗi loop =
cặp `validator_node` (trả Pydantic `Verdict`) + `route_node`
(`Event(route=...)`) + cạnh route quay lại agent tương ứng + cạnh route
thoát. **Không dùng `RetryConfig` cho vòng viết lại** — RetryConfig chỉ
cho lỗi transient khi node raise exception.

- Mỗi loop tối đa **3 lần sinh** (1 lần đầu + 2 lần viết lại), đếm bằng
  `ctx.state["<tên>_attempts"]` do một function node tự tăng; route node
  kiểm tra `attempts >= 3` để phát route `"fail"` thay vì `"retry"`.
- Mỗi lần viết lại nhận đủ: đề xuất cũ, exception/lỗi validator đầy đủ,
  giá trị thật đo được (nếu có), và danh sách các lần thử trước để không
  lặp lại đề xuất cũ. LlmAgent trong graph chạy ở mode `single_turn`
  nên agent nhớ các lần thử trước qua state (instruction dạng
  `{feedback?}`), không dựa vào chat history.
- Thoát loop khi: validator đạt (route `"ok"`), hoặc hết 3 lần, hoặc
  cùng một lỗi lặp lại 2 lần liên tiếp (viết lại nữa cũng khó khác).
- Hết loop mà chưa đạt: N2/N3 dùng phương án an toàn đã kiểm chứng
  (luật scoring / không gộp) và ghi log; N6 thì shop FAILED vì schema
  là hợp đồng cho bước trả lời, không được sai. **Nhánh `"fail"` phải là
  một node ghi log/kết luận, không được raise exception** — vì node raise
  sẽ dừng cả workflow, và batch driver không thể đi tiếp sang shop khác
  một cách sạch sẽ.

---

## 6. File schema của shop — 1 file/shop, cấu trúc JSON

File: `sqlite/schemas/<business_id>.json`. Đây là **hợp đồng duy nhất**
giữa ingest và workflow trả lời câu hỏi: SqlGenerator về sau chỉ được
dùng bảng/cột có trong file này, và `run_sql` cũng kiểm chứng theo
file này.

```json
{
  "business_id": "biz_123",
  "source_files": ["pos.sqlite"],
  "generated_at": "2026-11-01T00:00:00+07:00",
  "model": "tên model local đã dùng (khai báo, không ghi key)",
  "tables": [
    {
      "table_id": "don_hang",
      "original_names": ["don_hang"],
      "source_sheets": ["don_hang"],
      "sqlite_table": "don_hang",
      "row_count": 33630,
      "description_vi": "Bảng đơn hàng: mỗi dòng là một đơn.",
      "columns": [
        {
          "original_name": "MaHoaDon",
          "sqlite_name": "ma_hoa_don",
          "type": "TEXT",
          "description_vi": "Mã đơn hàng, khóa nối sang chi tiết đơn.",
          "role_hint": "order_id",
          "sample_values": ["HD001", "HD002"],
          "distinct_count": 33630,
          "distinct_values": null,
          "nullable_rate": 0.0
        },
        {
          "original_name": "TinhTrang",
          "sqlite_name": "tinh_trang",
          "type": "TEXT",
          "description_vi": "Trạng thái đơn.",
          "role_hint": "status",
          "sample_values": ["Hoàn thành", "Đã hủy"],
          "distinct_count": 4,
          "distinct_values": ["Hoàn thành", "Đã hủy", "Trả hàng", "Đang giao"],
          "nullable_rate": 0.0
        }
      ],
      "date_range": {"min": "2025-01-01", "max": "2026-06-28"}
    }
  ],
  "join_hints": [
    {
      "from_table": "chi_tiet_don",
      "from_column": "ma_hoa_don",
      "to_table": "don_hang",
      "to_column": "ma_hoa_don",
      "evidence": "tên cột chung + 33630/33630 mã đơn khớp nhau"
    }
  ],
  "notes": [
    "Không cộng dồn chi_tiet_don_lo để tính doanh thu (bảng tách lô, sẽ đếm trùng).",
    "Giá tại một ngày lấy từ lich_su_gia, không lấy giá hiện tại trong san_pham."
  ]
}
```

Quy tắc bắt buộc của schema file:

- `distinct_values` chỉ liệt kê đầy đủ khi `distinct_count ≤ 50`; nếu
  nhiều hơn thì để `null` và chỉ giữ `distinct_count` + mẫu.
- `sample_values` tối đa 5 giá trị, phải có thật trong DB (validator N6
  kiểm bằng truy vấn).
- `role_hint` chỉ dùng các giá trị trong enum cố định: `order_id`,
  `product_id`, `customer_id`, `date`, `money`, `quantity`, `status`,
  `payment`, `unknown`. Không tự bịa role mới.
- `join_hints` phải có `evidence` đo được (tỷ lệ ID khớp), không ghi
  join theo phỏng đoán từ tên cột đơn thuần.
- `notes` ghi các bẫy đã phát hiện trong shop (đếm trùng, giá theo ngày,
  số dư tồn kho chạy) — đây là thứ SqlGenerator về sau cần nhất.
- File schema **không chứa dữ liệu bán hàng** (không có doanh thu, số
  lượng bán theo dòng). Nó chỉ mô tả cấu trúc, đúng như log_openai trước
  đây chỉ chứa entity list chứ không chứa số liệu.

---

## 7. Tools — tất cả là FunctionTool (hàm Python + docstring)

| Tool | Input chính | Output | LLM hay code | Validator đi kèm |
|------|-------------|--------|--------------|------------------|
| `list_shop_files` | business_id, thư mục data | Danh sách file + loại | code | — |
| `read_readme` | đường dẫn README | Text README đã cắt 4000 ký tự | code | — |
| `scan_sheets` | danh sách file | raw_tables: header ứng viên, điểm tin cậy, mẫu, thống kê | code | Điểm header, mẫu không toàn trống |
| `scan_sqlite` | đường dẫn .sqlite | schema từ sqlite_master + mẫu + thống kê | code | — |
| `validate_header` | HeaderProposal + mẫu | đạt/lỗi + điểm scoring | code | Chạy trong loop N2 |
| `validate_merge` | MergeProposal + chữ ký cột + ID overlap | đạt/lỗi + nhóm vi phạm | code | Chạy trong loop N3 |
| `clean_tables` | merge_groups + raw data | DataFrame đã sạch + _clean_stats | code | Đếm NULL, đếm dòng |
| `write_sqlite_tmp` | DataFrame + schema_map | Đường dẫn DB tạm | code | Ghi đúng thư mục của shop |
| `validate_schema` | ShopSchema + DB tạm | đạt/lỗi + giá trị thật đo được | code | Chạy trong loop N6 |
| `validate_shop` | DB tạm + schema file | đạt/lỗi + node gây lỗi | code | Chạy ở N7 |
| `publish_shop` | DB tạm + schema file | DB chính thức + artifact | code | Atomic rename, chỉ khi N7 đạt |

Không có tool nào gọi model, gọi mạng, hay ghi ra ngoài thư mục làm việc
của shop. Mọi tool đều nhận đúng dữ liệu nó cần, không nhận toàn bộ
state.

---

## 8. Agents và Callbacks

### 8.1. Agents

| Agent | Model | Skill nạp | Tools | output_schema | Nhiệt độ |
|-------|-------|-----------|-------|---------------|----------|
| HeaderAgent | LiteLlm (dev: Gemini/OpenAI; Docker: model local) | datamate-header | validate_header (đọc kết quả) | HeaderProposal | 0 |
| MergeAgent | LiteLlm | datamate-merge | validate_merge | MergeProposal | 0 |
| SchemaAgent | LiteLlm | datamate-schema | validate_schema | ShopSchema | 0 |
| IngestWorkflow | (không phải LLM — là Workflow graph) | — | — | — | — |

Tất cả agent đều có instruction bằng tiếng Việt, yêu cầu: chỉ dùng
bảng/cột đã quét được, không bịa tên bảng/cột, không bịa giá trị mẫu,
không tính toán số liệu tổng hợp (việc đó là của code).

### 8.2. Callbacks — guardrail bằng code

| Callback | Việc làm |
|----------|----------|
| `before_tool_callback` (ghi DB) | Chặn mọi đường ghi không nằm trong thư mục của shop đang xử lý. |
| `before_tool_callback` (kiểm chứng SQL) | Chỉ cho phép SELECT; chặn INSERT/UPDATE/DELETE/DROP và chặn truy vấn tới bảng ngoài schema file. |
| `after_tool_callback` | Cắt mẫu trả về (tối đa 10 dòng/bảng, danh sách distinct tối đa 500 mục), cắt kết quả SQL (tối đa 1000 dòng) để không tràn context. |
| `before_model_callback` | Tiêm đúng phần state node đó cần (không tiêm toàn bộ state), tiêm skill đã nạp; cắt danh sách entity dài. |
| `after_model_callback` | Validate JSON theo output_schema; JSON sai schema thì trả lỗi về loop viết lại, không đưa JSON hỏng xuống node sau. |
| `on_tool_error_callback` | Biến exception thành payload có cấu trúc (loại lỗi, câu lệnh lỗi, giá trị thật đo được) để loop viết lại có cái mà sửa. |
| `after_agent_callback` | Ghi event của node vào log của shop (đề xuất, lỗi, số lần thử). |

---

## 9. Session, State, Artifacts và Log

### 9.1. Session

- Một session cho một shop: `app_name = "datamate-ingest"`,
  `user_id = "datamate"`, `session_id = <business_id>`.
- SessionService: InMemory khi dev, DatabaseSessionService (SQLite local)
  khi chạy batch/Docker để resume shop lỗi.

### 9.2. State keys (chỉ dữ liệu nhỏ dạng text)

| Key | Nội dung |
|-----|----------|
| `business_id`, `input_files`, `input_kind` | Shop đang xử lý, file nguồn, loại CSV/XLSX/SQLite |
| `readme_text` | README đã cắt 4000 ký tự |
| `raw_tables` | Header ứng viên, điểm tin cậy, mẫu, thống kê từng bảng |
| `headers_final` | Header chốt sau N2 |
| `merge_groups` | Nhóm gộp chốt sau N3 |
| `clean_stats` | Số dòng vào/ra, số ô NULL, số dòng trùng đã bỏ |
| `tmp_db_path`, `schema_path` | Đường dẫn DB tạm và schema file |
| `loop_attempts` | Số lần thử của từng loop (N2/N3/N6) |
| `shop_status` | `pending` / `ok` / `failed` + lý do |

Dữ liệu lớn (DataFrame, toàn bộ dòng) **không bao giờ** vào state —
chúng ở trong bộ nhớ của node và trong DB tạm.

### 9.3. Artifacts của session

| Artifact | Nội dung |
|----------|----------|
| `schema.json` | File schema của shop (§6), phiên bản tăng khi chạy lại |
| `ingest_log.jsonl` | Event stream của session (node, đề xuất, lỗi, lần thử) |

File vật lý tương ứng nằm ở `sqlite/schemas/<business_id>.json` và
`logs/ingest/<business_id>.jsonl` để `predict.sh` và workflow trả lời
đọc trực tiếp mà không cần qua ArtifactService.

---

## 10. Model và chạy offline

- Dev: LiteLlm nối Gemini hoặc OpenAI (chỉ để dạy, thử và đo; không bao
  giờ vào Docker cuối).
- Docker cuối: LiteLlm nối endpoint model local trong container
  (Ollama hoặc vLLM), model ≤ 15B, phát hành công khai trước 2026-10-01,
  licence cho phép sử dụng, khai báo trong README của solution.
- Nhiệt độ 0 ở mọi agent để kết quả tái lập được.
- Structured output (`output_schema`) bắt buộc ở HeaderAgent, MergeAgent,
  SchemaAgent — model local nhỏ viết JSON tự do rất dễ hỏng, schema là
  cách khóa output lại.

---

## 11. Đánh giá ingest — adk eval + kiểm chứng xác định

`adk eval` một mình không đủ cho ingest, vì ingest đúng hay sai đo bằng
dữ liệu, không đo bằng cảm nhận. Đánh giá gồm 2 lớp:

**Lớp 1 — adk eval (evalsets cho quyết định LLM):**
- Evalset detect header: các sheet khó của train (header không ở dòng 1,
  sheet 2 tầng tiêu đề, sheet có dòng tổng).
- Evalset quyết định gộp: biz_109 (8 sheet DuLieuBanHang), biz_106
  (_1/_2), biz_119/120 (ChiTiet vs ChiTietLo — đáp án đúng là KHÔNG gộp
  bừa), biz_123 (SQLite, không gộp bảng khác vai trò).
- Evalset schema: schema file sinh ra có khớp DB không (validator N6
  chính là bộ chấm tự động).
- Đo thêm: tỷ lệ loop phải viết lại, số lần thử trung bình, tỷ lệ shop
  FAILED.

**Lớp 2 — kiểm chứng xác định (không cần LLM):**
- Đếm dòng từng bảng trong DB mới so với profile Phase 1 (3.997.042 dòng
  vào, 3.982.749 dòng ra trên toàn train là mốc tham chiếu).
- Mở lại DB bằng sqlite3, chạy SQL mẫu (đếm đơn, tổng tiền theo trạng
  thái) và đối chiếu với kết quả tính từ ShopView Phase 1.
- Schema file parse được bằng json.load, mọi bảng/cột khớp DB, mọi mẫu
  và distinct đều có thật.
- Chạy lại ingest lần 2 trên cùng shop phải cho cùng DB và schema
  (deterministic ở nhiệt độ 0 + seed cố định cho mẫu ngẫu nhiên).

---

## 12. Giai đoạn triển khai (chỉ bắt đầu khi người dùng duyệt plan này)

| Giai đoạn | Việc làm | Đầu ra kiểm chứng được |
|-----------|----------|------------------------|
| **G0 — Spike ADK** | Cài `google-adk==2.13.0` với `constraints-3.12.txt` và extra `db`; đọc `.agents/skills/adk-architecture` và `adk-setup` trong repo; chứng minh: Workflow graph chạy được, loop quay lại khi validator lỗi (bằng route, không bằng RetryConfig), SkillToolset nạp skill theo nhu cầu (mỗi agent một toolset một skill), callback chặn được tool sai (before_tool trả dict non-None), artifact + event log ghi được (FileArtifactService, driver tự serialize event ra .jsonl), chạy Workflow lần đầu với `new_message=None` hay cần Content mồi, và khai báo model local qua LiteLlm với endpoint thật (Ollama/vLLM) kèm đo tỷ lệ JSON hợp schema. | Báo cáo spike + xác nhận chốt 2.13.0 + Workflow graph. Go/no-go cho toàn bộ plan. |
| **G1 — Node xác định** | Viết tools và node code: quét, làm sạch, ghi DB tạm, kiểm chứng shop (N0, N1a, N1b, N4, N5, N7, N8). Chưa có LLM. | Chạy được trên 3 shop pilot chỉ bằng luật, DB mở được bằng DB Browser. |
| **G2 — Agent + Skill** | Viết 4 Skill (§4), HeaderAgent, MergeAgent, SchemaAgent với output_schema, và 3 loop N2/N3/N6. | Evalset header/gộp/schema chạy được, tỷ lệ loop viết lại đo được. |
| **G3 — Pilot 3 shop** | Chạy ingest đầy đủ trên biz_101 (XLSX thường), biz_123 (SQLite sạch nhất), biz_109 (nhiều sheet cần gộp). | 3 DB + 3 schema file + 3 log, đạt kiểm chứng §11, báo cáo pilot. |
| **G4 — Full 30 shop** | Chạy toàn train, sửa lỗi theo báo cáo, viết báo cáo ingest tổng. | 30 DB + 30 schema file + ingest_report.json. |
| **G5 — Nối workflow trả lời** | SqlGenerator đọc schema file của shop (không đọc DB thô), run_sql kiểm chứng theo schema file. | Workflow trả lời chạy được trên 3 shop pilot chỉ từ DB + schema file. |

---

## 13. Rủi ro và giới hạn (nói trước, không giấu)

1. **Model local nhỏ viết JSON/schema kém.** Giảm nhẹ: output_schema bắt
   buộc, validator code kiểm chứng, loop viết lại, và schema là node duy
   nhất được phép làm shop FAILED (không xuất schema xấu).
2. **Prompt phình ở shop nhiều bảng (biz_123 có 17 bảng).** Giảm nhẹ:
   SchemaAgent làm việc theo từng bảng đã gộp, không nhét toàn bộ DB vào
   một prompt; Skill chỉ nạp khi cần (progressive disclosure); danh sách
   distinct cắt ở 500 mục, liệt kê đầy đủ chỉ khi ≤ 50.
3. **ADK đổi API giữa các bản.** Giảm nhẹ: pin version ở G0, giữ tool và
   validator là hàm Python thuần (không phụ thuộc ADK) để đổi framework
   vẫn giữ được phần lớn code.
4. **Ngày/tiền bẩn ở shop lạ.** Giảm nhẹ: ô không parse được → NULL +
   đếm, không đoán; shop có tỷ lệ NULL bất thường thì cảnh báo trong
   ingest_report để người dùng soi trước khi tin đáp án.
5. **Thời gian.** Ingest là bước chạy một lần cho mỗi shop (lúc build
   Docker và lúc predict gặp shop mới), không phải chạy mỗi câu hỏi, nên
   vài phút/shop là chấp nhận được; ngân sách 6 giờ cho private test
   vẫn do workflow trả lời quyết định chính.

---

## 14. Quyết định cần người dùng trước khi bắt đầu code

1. **Phiên bản ADK:** đã chốt theo source thật — **2.13.0 + Workflow
   graph** (LoopAgent đã deprecated trong bản này, xem §2.3). Không cần
   quyết định này nữa, trừ khi G0 spike phát hiện Workflow graph thiếu
   thứ gì đó bắt buộc.
2. **Model dev cho G0–G2:** Gemini hay OpenAI (chỉ để thử, không vào
   Docker)?
3. **Pilot 3 shop:** giữ biz_101 / biz_123 / biz_109 hay đổi shop khác?
4. **Vị trí file:** giữ `sqlite/` + `sqlite/schemas/` như §1.2 hay muốn
   đặt schema file ở chỗ khác?
