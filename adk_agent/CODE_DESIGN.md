# Thiết kế code chuẩn cho workflow ingest ADK

> Trạng thái: **THIẾT KẾ — CHƯA viết code, chờ duyệt cùng plan ingest.**
> Tài liệu này thiết kế cấu trúc code cho workflow ingest trong
> `ADK_INGEST_PLAN.md`, theo đúng các skill ADK đã đọc (xem §1).
> Ngôn ngữ code và chú thích: tiếng Việt đầy đủ dấu, UTF-8 (quy tắc của
> dự án — thắng mọi quy ước nhà của ADK nếu có xung đột về ngôn ngữ).
> Không hard-code theo business_id, tên cột hay rulebook của split train.
> Không sửa dữ liệu train gốc, không sửa code trong `~/workspace/adk-python/`
> (đó là repo tham khảo chỉ đọc).

---

## 1. Skill nào đã dùng — và skill nào cố tình KHÔNG dùng

Nói thẳng trước: **không có skill nào tên đúng "code system design"**
trong catalog của Muse lẫn trong `.agents/skills/` của repo ADK. Các skill
bao phủ đúng việc này là ba skill sau của ADK, và tài liệu này dùng cả ba:

| Skill (đường dẫn trong repo ADK) | Dùng để làm gì ở đây |
|---|---|
| `adk-architecture` (`.agents/skills/adk-architecture/SKILL.md`) | Thiết kế hệ thống: runtime là graph các node, `BaseNode` là đơn vị thực thi, `Workflow` lên lịch theo edge, hai kênh riêng biệt — `Context` đưa kết quả lên trên, `Event` đưa persistence/streaming vào session; checkpoint/resume; ranh giới public API. |
| `adk-agent-builder` (`.agents/skills/adk-agent-builder/SKILL.md`) | Lắp ráp agent/workflow từ các mảnh có sẵn: function node, LlmAgent node, routing, loop, state/event, callbacks, kiểm thử bằng `InMemoryRunner`. Chỉ đọc đúng reference khớp với việc đang làm, không nạp cả cây. |
| `adk-style` (`.agents/skills/adk-style/SKILL.md`) | Chuẩn code: private-by-default, import, type hint, Pydantic v2, format, docstring, logging, async I/O, bố cục file và kiểm thử. |

**Skill cố tình KHÔNG dùng cho thiết kế này: `adk-unit-design`.**
Theo đúng mô tả của chính nó, skill đó viết tài liệu kiến trúc cho một
code unit **như đã triển khai** ("Nothing proposed or aspirational belongs
in one") — tức chỉ dùng để ghi lại code đã có, không dùng để thiết kế
code chưa viết. Dùng nó ở đây sẽ là dùng sai phạm vi của skill. Khi code
ingest đã viết xong, có thể quay lại dùng `adk-unit-design` để ghi tài
liệu as-built cho từng unit.

Lưu ý về phiên bản skill: `adk-agent-builder` ghi rõ API của nó đã kiểm
chứng với `google-adk` 2.6.2, còn repo đang dùng là 2.13.0. Mọi tên symbol
trong tài liệu này đều lấy từ source 2.13.0 đã đọc ở lần rà soát trước;
nếu một symbol thiếu lúc chạy, đọc source dưới `src/google/adk/` chứ
không đoán tên lân cận.

---

## 2. Nguyên tắc thiết kế hệ thống (từ `adk-architecture`)

Bốn nguyên tắc này quyết định toàn bộ bố cục code bên dưới. Vi phạm một
trong bốn thì thiết kế coi như hỏng, dù code vẫn chạy được trong demo:

1. **Node là đơn vị thực thi duy nhất.** Mọi bước ingest — kể cả bước code
   thuần — đều là một node trong graph. Không có hàm "điều phối" đứng ngoài
   graph tự gọi các bước theo thứ tự riêng, vì như vậy graph mất tính
   tường minh, mất khả năng resume từng node và mất event log.
2. **Hai kênh không được lẫn.** `Context` (`ctx`) đưa kết quả đi lên cho
   node cha; `Event` đưa persistence và streaming vào session. Một node
   không được "trả kết quả" bằng cách ghi lén vào một biến toàn cục, một
   file tạm không đăng ký, hay một dict dùng chung ngoài state.
3. **State chỉ chứa dữ liệu nhỏ dạng text, JSON-serializable.**
   Đường dẫn, thống kê, mẫu, quyết định, bộ đếm vòng lặp thì được.
   DataFrame, toàn bộ bảng, object `Content` thì không — chúng ở trong bộ
   nhớ của node và trong DB tạm, không bao giờ đi qua state hay
   `event.output`. (Lý do kỹ thuật: `JoinNode` và `DatabaseSessionService`
   đều serialize state; một giá trị không serialize được sẽ làm hỏng
   session ở bước không liên quan.)
4. **Side-effect phải idempotent hoặc publish nguyên tử.** Workflow có
   `rerun_on_resume=True`: node đã hoàn thành được replay từ event, không
   chạy lại; nhưng một lần chạy fresh sau lỗi thì node ghi sẽ chạy lại.
   Vì vậy node ghi SQLite chỉ ghi DB tạm, và chỉ node publish cuối cùng
   mới rename nguyên tử thành DB chính thức. Không có node nào ghi đè
   trực tiếp lên DB chính thức.

---

## 3. Bố cục package

```
workspace/datasets/datamate/adk_agent/
  ADK_INGEST_PLAN.md        # plan nghiệp vụ (đã có)
  CODE_DESIGN.md            # tài liệu này
  ingest/                   # package code ingest (tên package: datamate_ingest
                            # khi đóng gói Docker; trong repo làm việc giữ tên
                            # thư mục ingest/ để tránh nhầm với google.adk)
    __init__.py             # CHỈ export public API + __all__ (xem §4.1)
    _config.py              # IngestConfig (Pydantic, extra='forbid')
    _models.py              # Mọi Pydantic model dùng chung (xem §5)
    _logging.py             # Logger theo module, mức log chuẩn
    nodes/
      __init__.py           # không export node lẻ; workflow lấy node từ
                            # _workflow.py, không import node trực tiếp
      _scan_nodes.py        # init_shop, scan_sheets, scan_sqlite
      _validate_nodes.py    # validate_header, validate_merge,
                            # validate_schema_table, validate_schema_meta
                            # (route node nằm cùng file với validator)
      _clean_nodes.py       # clean_tables (gọi normalizers của phase1)
      _sqlite_nodes.py      # write_sqlite_tmp, init_schema_tables,
                            # init_schema_meta, assemble_schema,
                            # validate_shop, publish_shop, finalize_shop
    agents/
      __init__.py           # không export agent lẻ
      _header_agent.py      # HeaderAgent (một agent một file)
      _merge_agent.py       # MergeAgent
      _schema_table_agent.py # SchemaTableAgent (một bảng mỗi lần)
      _schema_meta_agent.py # SchemaMetaAgent (join_hints + notes cấp shop)
    _callbacks.py           # before_tool / after_tool / on_tool_error
    _skills_loader.py       # Nạp 4 skill local, kiểm tra tên + frontmatter
    _workflow.py            # build_ingest_workflow() -> Workflow (duy nhất
                            # nơi khai báo edges; không nơi nào khác được
                            # tự ráp graph)
    _driver.py              # run_ingest_shop(), run_ingest_all(),
                            # serialize event ra .jsonl, ingest_report.json
    _services.py            # Tạo Runner + SessionService + ArtifactService
                            # (FileArtifactService / DatabaseSessionService)
  skills/                   # 4 skill ADK, mỗi skill một thư mục
    datamate-header/
      SKILL.md
      references/
    datamate-merge/
      SKILL.md
      references/
    datamate-cleaning/
      SKILL.md
      references/
    datamate-schema/
      SKILL.md
      references/
  tests/
    test_config.py
    test_models.py
    test_scan_nodes.py
    test_validate_nodes.py
    test_route_nodes.py
    test_clean_nodes.py
    test_sqlite_nodes.py
    test_agents.py          # agent với model giả (FakeLlm), không gọi model thật
    test_workflow.py        # graph chạy đầu-cuối bằng InMemoryRunner
    test_driver.py          # driver với shop giả lập, DB tạm + publish
    test_docstrings.py      # quét AST: mọi module/class/hàm public phải có
                            # docstring tiếng Việt, mọi field Pydantic phải có
                            # attribute docstring (xem §4.4)
  evals/
    header.evalset.json
    merge.evalset.json
    schema.evalset.json
```

Quy tắc bố cục (lấy từ `adk-style`, đã điều chỉnh cho dự án này):

- **Một agent một file** trong `agents/`. Function node là hàm, không phải
  class, nên nhóm theo họ việc trong `nodes/` là đủ — không cần một file
  cho một hàm, nhưng một file không được ôm hai họ việc khác nhau (ví dụ
  không để validator nằm chung file với node ghi SQLite).
- **Chỉ `_workflow.py` được ráp graph.** Không một module nào khác được
  tạo `Workflow(...)`. Như vậy edge list là single source of truth: muốn
  biết luồng đi, đọc đúng một file.
- **Chỉ `_driver.py` được tạo `Runner` và chạy batch.** Không một node hay
  agent nào được tự tạo Runner con.
- **Skill là thư mục dữ liệu, không phải code Python.** `skills/` không
  chứa `.py` làm sạch hay ghi DB. Code làm sạch nằm ở `nodes/`, skill chỉ
  chứa hướng dẫn + ví dụ + quy tắc mà agent đọc.
- **Test mirror source path.** Test của `nodes/_scan_nodes.py` nằm ở
  `tests/test_scan_nodes.py`; test của một module mới thì tạo file
  `test_<tên module>.py` mới, không tạo file theo tên một lần sửa chữa.

---

## 4. Chuẩn code áp dụng

### 4.1. Visibility — private-by-default (từ `adk-style/visibility.md`)

ADK quy định mọi file `.py` mới dưới `src/google/adk/` phải có tiền tố
`_` và public API phải export tường minh qua `__init__.py` + `__all__`.
Áp dụng nguyên tắc đó cho package này (không áp dụng máy móc đường dẫn
`src/google/adk/` vì đây không phải repo ADK):

- Mọi module triển khai đều `_`-prefixed (`_scan_nodes.py`,
  `_header_agent.py`...). Module không `_` duy nhất là `__init__.py` của
  từng package.
- Public API của package ingest chỉ gồm những thứ driver và workflow trả
  lời câu hỏi cần gọi, export tường minh trong `ingest/__init__.py`:
  `build_ingest_workflow`, `run_ingest_shop`, `run_ingest_all`,
  `IngestConfig`, `IngestReport`. Mọi thứ khác là nội bộ — code ngoài
  package không được import trực tiếp từ module `_`.
- Trong nội bộ package, import trực tiếp từ module cụ thể
  (`from ._models import Verdict`), không import qua `__init__.py` của
  package, để tránh import vòng và để ranh giới public/private rõ ràng.
- Wrapper node nội bộ của framework (như `_ToolNode`, `_LlmAgentWrapper`)
  không bao giờ được import hay subclass trong code này — node được đưa
  thẳng vào `edges` dưới dạng `NodeLike` và framework tự bọc; cấu hình
  riêng cho node (tên, `retry_config`, `timeout`) đi qua helper `node()`.

### 4.2. Type hint — annotate everything (từ `adk-style/typing.md`)

- Mọi hàm đều có type hint cho tham số và kiểu trả về, kể cả hàm private
  và `-> None`.
- Dùng builtin generics (`list[str]`, `dict[str, int]`), không dùng
  `typing.List`/`typing.Dict` cho code mới.
- Dùng `X | None`, không dùng `Optional[X]` cho code mới.
- Tránh `Any`: dùng kiểu cụ thể, `Literal` cho tập chuỗi cố định (ví dụ
  route `"ok" | "retry" | "fail"`), `TypedDict` cho dict có key đã biết
  chỉ khi không dùng được Pydantic model — còn không thì dùng Pydantic
  model (xem §5).
- Tham số hàm: dùng `collections.abc` (`Mapping`, `Sequence`) cho tham số
  đầu vào, kiểu cụ thể cho giá trị trả về.
- Constructor/hàm có từ 2 tham số cùng kiểu trở lên mà đổi chỗ vẫn
  typecheck được thì dùng keyword-only (`*`) để tránh bug đổi thứ tự lặng
  lẽ. Đặc biệt áp dụng cho `IngestConfig` và các hàm ghi DB (đường dẫn
  nguồn, đường dẫn đích, business_id đều là `str`).
- Không dùng mutable default (`list`, `dict`, `set` làm default) — dùng
  `None` làm sentinel rồi tạo mới trong thân hàm.
- Không dùng `assert` trong code production — `assert` bị strip khi chạy
  với `-O` nên không phải bảo đảm runtime. Vi phạm bất biến thì raise
  `ValueError`/`TypeError`/`RuntimeError` với thông điệp nêu rõ điều kiện.
- Không dùng `# type: ignore` trần — nếu bắt buộc thì ghi rõ nguyên nhân
  bên ngoài (`# type: ignore[arg-type]  # <nguyên nhân>`).
- `isinstance` là cách chuẩn để phân biệt kiểu runtime: viết chuỗi
  `if/elif` đầy đủ và luôn kết thúc bằng `else` raise `TypeError`, để một
  subclass mới không lặng lẽ rơi qua.
- Đầu mỗi module (trừ `__init__.py`): `from __future__ import annotations`
  ngay sau docstring/comment đầu file, rồi mới tới import. Thứ tự import:
  thư viện chuẩn, third-party, rồi import nội bộ package.

### 4.3. Format

Áp dụng cấu hình của ADK skill cho package mới này, và ghi rõ đây là một
quyết định có chủ ý (nếu dự án muốn giữ style 4-space của code phase1/2
cũ thì đổi đúng một chỗ này, không đổi rải rác):

- Indent 2 space, không dùng tab; dòng tối đa 80 ký tự (trừ dòng import).
- Formatter tương thích pyink (fork Black của Google); import sắp xếp
  theo profile Google của isort.
- Quote theo style đang chiếm đa số trong file đang sửa, không churn quote
  chỉ vì đổi file.

### 4.4. Docstring và comment đầy đủ (từ `adk-style/documentation.md`, thắng bởi quy tắc dự án)

**Người dùng yêu cầu ngày 2026-10-31: code phải comment đầy đủ.**
"Đầy đủ" ở đây được hiểu theo checklist dưới đây — tức là mọi chỗ người
đọc cần giải thích đều có giải thích, chứ không phải đếm số dòng comment.
Nếu người dùng muốn mức cao hơn (comment cả những khối logic hiển nhiên)
thì checklist này sẽ được nâng lên, không tự ý hạ xuống.

Checklist bắt buộc cho mọi file code mới trong package ingest:

1. **Module docstring ở đầu mỗi file:** module này làm gì, thuộc tầng nào
   (node / agent / validator / driver), phụ thuộc vào module nào, và
   output chính của nó là gì.
2. **Mọi class và mọi hàm public đều có docstring đầy đủ:** API làm gì,
   mọi tham số (kiểu và ý nghĩa), giá trị trả về, và từng exception có
   thể raise. Với Pydantic model, mỗi field có attribute docstring ngay
   dưới field (xem §5).
3. **Mọi hàm private phức tạp đều có docstring ngắn** nêu mục đích và
   điều kiện tiên quyết (ví dụ: hàm này chỉ được gọi khi state đã có key
   nào, vì sao).
4. **Comment "tại sao" bắt buộc tại mọi điểm sau** — đây là những chỗ
   người đọc mới chắc chắn sẽ thắc mắc nếu không có comment:
   - Điều kiện dừng loop (vì sao là 3 lần, vì sao lỗi lặp 2 lần thì fail
     sớm) và luật chuyển route `"ok"` / `"retry"` / `"fail"`.
   - Mọi fallback an toàn (vì sao luật scoring thay được LLM ở header,
     vì sao không gộp an toàn hơn gộp sai ở merge).
   - Mọi quy tắc validator (vì sao chữ ký cột phải khớp, vì sao
     ChiTiet/ChiTietLo phải so toàn bộ schema trước khi gộp).
   - Mọi chỗ code làm ngược trực giác: fail-as-route thay vì raise
     (vì node raise sẽ dừng cả workflow), ghi state qua `Event(state=...)`
     thay vì gán `ctx.state` (vì replay không tái tạo gán trực tiếp),
     chặn ở `before_tool` thay vì `after_tool` (vì after_tool chạy sau
     khi tác dụng phụ đã xảy ra), ngưỡng dừng non-None của `before_tool`
     khác ngưỡng truthy của `before_model`.
   - Mọi hằng số mang tính quyết định: 3 lần loop, README cắt 4000 ký
     tự, mẫu 10 + 10 dòng, distinct liệt kê đầy đủ khi ≤ 50, cắt danh
     sách ở 500 mục — mỗi hằng số đều có comment nêu lý do chọn con số
     đó, không để số trần không giải thích.
   - Mọi chỗ code "đoán" bị cấm đoán: vì sao ô không parse được thì để
     NULL và đếm, vì sao thiếu hệ số quy đổi thì để trống thay vì đoán
     bằng 1.
5. **Comment nội bộ giải thích tại sao, không nhắc lại cái gì.** Code đã
   nói nó làm gì rồi; một comment chỉ xứng đáng tồn tại khi nó nêu ràng
   buộc, bug, hay yêu cầu thứ tự mà đọc code không thấy được. Ngược lại,
   comment nhắc lại code (kiểu `i += 1  # tăng i lên 1`) là nhiễu, không
   tính là "đầy đủ".
6. **Không đưa link tới issue, PR hay design doc vào comment/log/error.**
   Link hỏng nhanh hơn code; lý do phải nằm trong chính comment, link
   (nếu cần) nằm ở PR. Áp dụng cả cho thông điệp lỗi.
7. **Quy tắc dự án thắng ở đây:** toàn bộ docstring, comment và thông điệp
   log/lỗi đều viết bằng **tiếng Việt đầy đủ dấu, UTF-8**. ADK skill không
   quy định ngôn ngữ comment, nên không có xung đột thật sự — nhưng nếu
   sau này có xung đột thì quy tắc dự án thắng.

**Kiểm chứng bằng máy, không kiểm bằng mắt.** Kế thừa cách làm đã dùng ở
phase1/phase2: một test quét bằng AST kiểm tra mọi module/class/hàm public
trong package ingest đều có docstring tiếng Việt (có dấu), và mọi field
của Pydantic model đều có attribute docstring. Test này nằm trong
`tests/test_docstrings.py` và phải pass thì code mới coi là đạt chuẩn
comment — thiếu docstring là lỗi kiểm thử, không phải góp ý review.

### 4.5. Logging

- Logger theo tên module: `logger = logging.getLogger(__name__)`, không
  dùng logger root, không dùng `print` trong code production.
- Mức log: `ERROR` cho shop FAILED và lỗi khiến node dừng; `WARNING` cho
  fallback đã kiểm chứng (luật scoring thay LLM, không gộp thay vì gộp
  sai) và cho ô NULL do không parse được vượt ngưỡng; `INFO` cho mốc
  shop (bắt đầu, xong từng node lớn, publish); `DEBUG` cho chi tiết
  validator (điểm scoring, nhóm gộp bị bác và lý do).
- Thông điệp log nêu rõ shop, node và lý do — không log "lỗi rồi" chung
  chung. Không bao giờ log nội dung file dữ liệu thô hay key/API.

### 4.6. Async và I/O (từ `adk-style/async.md`)

- Mọi I/O (đọc file, đọc DB, ghi DB, gọi model) đều nằm trong
  `async def`. ADK chạy trên một event loop; một câu lệnh đồng bộ chặn
  trong node sẽ làm đứng mọi agent đang chạy cùng lúc.
- pandas, openpyxl và sqlite3 đồng bộ không có API async: bọc bằng
  `asyncio.to_thread` khi gọi từ node async. Không dùng `time.sleep`,
  không dùng HTTP client đồng bộ trong code async.
- Function node có thể là hàm thường hoặc `async def` — framework đều
  nhận. Quy tắc ở đây: node nào chạm I/O thì viết `async def`, node thuần
  tính toán trên dữ liệu đã có trong bộ nhớ thì hàm thường là đủ.

---

## 5. Pydantic models — hợp đồng dữ liệu của toàn bộ workflow

Theo `adk-agent-builder/best-practices.md`: **mọi input/output của node,
`output_schema` của LLM và giá trị state có cấu trúc đều phải là
`BaseModel`, không phải `dict[str, Any]`**. Dict mất validation, mất
gợi ý IDE và mất khả năng FunctionNode tự coerce dict thành model từ
type hint.

Tất cả model đặt trong `_models.py`, dùng Pydantic v2, `Field()` kèm
ràng buộc và attribute docstring dưới mỗi field (để vừa làm tài liệu
vừa làm description trong JSON schema khi bật
`ConfigDict(use_attribute_docstrings=True)`).

| Model | Vai trò | `extra` | Ghi chú |
|---|---|---|---|
| `Verdict` | Output của mọi validator node: `status: Literal["ok", "retry", "fail"]`, `reason: str`, `details: dict` | `'forbid'` | Route node chỉ đọc `status` rồi phát route cùng tên. Sai chính tả field phải raise, không được lặng lẽ rơi. |
| `HeaderProposal` | `output_schema` của HeaderAgent: dòng header, tên cột chuẩn hóa, lý do | `'forbid'` | Đây là model argument — misspelled field raise thay vì bị drop (theo `adk-style/pydantic.md`). |
| `MergeProposal` | `output_schema` của MergeAgent: nhóm gộp + bằng chứng + bảng giữ riêng | `'forbid'` | Như trên. |
| `ColumnSchema` | Một cột trong schema file: tên gốc, tên SQL, kiểu, mô tả, `role_hint` (enum cố định), mẫu, distinct | mặc định | Xem ghi chú về model persisted bên dưới. |
| `TableSchema` | Một bảng trong schema file: tên, nguồn sheet, số dòng, danh sách cột, khoảng ngày | mặc định | Như trên. |
| `ShopSchema` | Toàn bộ file schema của shop (§6 plan): bảng + join_hints + notes | mặc định | **Model persisted** (ghi ra file JSON, đọc lại ở workflow trả lời) nên giữ `extra` mặc định: một reader cũ phải chịu được field mới do writer mới thêm vào, thay vì raise. |
| `RawTable` | Kết quả quét một bảng: header ứng viên, điểm tin cậy, mẫu, thống kê | `'forbid'` | Chỉ đi trong workflow, không persisted. |
| `CleanStats` | Thống kê làm sạch: dòng vào/ra, ô NULL, dòng trùng đã bỏ | `'forbid'` | Ghi vào `_clean_stats` và ingest report. |
| `IngestReport` | Kết quả một shop: `status: Literal["ok", "failed"]`, số bảng/dòng, số lần loop, node fallback, lý do lỗi | `'forbid'` | Driver ghi ra `ingest_report.json` tổng hợp. |
| `IngestConfig` | Cấu hình chạy: thư mục data, thư mục output, model, giới hạn loop, giới hạn mẫu | `'forbid'` | Mọi tham số chạy đều qua config này, không đọc biến toàn cục rải rác. `model_post_init()` dùng cho setup, không override `__init__` (override `__init__` sẽ phá thứ tự validation của Pydantic). |

Quy tắc validator trong model:

- Ràng buộc đơn giản (số ≥ 0, độ dài, enum) viết bằng `Field(ge=...)`,
  không viết validator riêng.
- Logic một field dùng `@field_validator` kèm `@classmethod` viết tường
  minh; logic liên field dùng `@model_validator(mode='after')` và phải trả
  về chính instance.
- Field giữ client hay trạng thái nội bộ không serialize thì dùng
  `PrivateAttr()` hoặc `exclude=True`, không đưa cờ điều khiển transient
  vào model persisted (ví dụ không đưa `loop_attempts` vào `ShopSchema`).

---

## 6. Thiết kế graph — edges, routes và state (single source of truth)

Toàn bộ graph chỉ được khai báo trong `_workflow.py` qua
`build_ingest_workflow()`. Bảng dưới là hợp đồng: đổi edge hay state key
ở đây thì phải đổi đồng thời instruction template, route node, driver và
evalset — bốn nơi đó không được tự ý lệch nhau.

### 6.1. Edges

```text
START -> init_shop -> branch_input
branch_input --(sqlite)--> scan_sqlite --+
branch_input --(default)--> scan_sheets -+-> header_gate
header_gate -> header_loop_route
header_loop_route --(retry)--> header_agent -> validate_header -> header_loop_route
header_loop_route --(ok)-----> merge_agent -> validate_merge -> merge_loop_route
header_loop_route --(fail)---> mark_header_fallback -> merge_agent
merge_loop_route --(retry)--> merge_agent
merge_loop_route --(ok)-----> clean_tables -> write_sqlite_tmp -> init_schema_tables
merge_loop_route --(fail)---> mark_merge_fallback -> clean_tables
init_schema_tables --(tables)--> schema_table_agent -> validate_schema_table -> route_schema_table
init_schema_tables --(empty)---> assemble_schema
route_schema_table --(retry)--> schema_table_agent (cùng bảng)
route_schema_table --(next)---> schema_table_agent (bảng sau)
route_schema_table --(done)---> init_schema_meta -> schema_meta_agent -> validate_schema_meta -> route_schema_meta
route_schema_meta --(retry)--> schema_meta_agent
route_schema_meta --(ok)-----> assemble_schema
route_schema_meta --(fail)---> mark_schema_meta_fallback -> assemble_schema
assemble_schema -> validate_shop -> publish_shop -> finalize_shop
validate_shop --(fail)----> mark_shop_failed -> finalize_shop
```

Đọc kỹ các quy tắc graph áp dụng ở đây (từ `adk-agent-builder` và lần rà
source 2.13.0):

- `START` là sentinel, không bao giờ chạy; cạnh từ `START` không được mang
  route; `START` không được có cạnh vào; mọi node phải tới được từ `START`.
- **Mọi cạnh đi ra của một route node đều phải mang route.** Cạnh không
  route sẽ fire trên mọi output của node nguồn, kể cả khi node đó vừa
  chọn một nhánh khác — nên route node không được có cạnh "mặc định"
  không route đi kèm.
- Cycle quay lại agent chỉ hợp lệ vì cạnh đó mang route; cycle toàn cạnh
  không route bị validator từ chối ngay lúc khởi tạo.
- Chỉ có một node terminal ra output của workflow: `finalize_shop`. Các
  node trung gian trả output cho node sau, không phải output của workflow.
  Không dùng `JoinNode` ở đây vì graph ingest là tuyến tính có rẽ nhánh,
  không có fan-out cần gộp.
- Tên node phải là Python identifier hợp lệ; cùng một object node
  (`header_agent`) được dùng lại ở cả cạnh vào lần đầu và cạnh loop quay
  lại — không tạo hai instance cùng tên (trùng tên node là lỗi validation).
- `branch_input` là một function node rẽ nhánh bằng route (`"sqlite"` /
  `"default"`), không phải `if` trong driver. Lý do: nhánh phải hiện trên
  graph để event log và resume thấy được nó.

### 6.2. Loop — bộ đếm bằng state, route bằng chuỗi

Mỗi loop (header, merge, schema meta) có đúng ba thành phần, không thêm không bớt:

1. **Agent node** (LlmAgent, `single_turn`, `output_schema` + `output_key`).
2. **Validator node** (function node thuần code): nhận output của agent
   (đã coerce thành Pydantic model từ type hint), trả `Verdict`.
3. **Route node** (function node): nhận `Verdict`, đọc bộ đếm trong state,
   trả `Event(output=<verdict>, route=<chuỗi>)` với chuỗi là một trong
   `"ok"`, `"retry"`, `"fail"`.

Loop schema theo từng bảng dùng cùng ba thành phần nhưng route phát
`"retry"` (cùng bảng), `"next"` (bảng sau) và `"done"` (hết bảng),
vì một vòng lặp phải đi qua nhiều bảng chứ không chỉ lặp lại một
agent cho tới khi đạt.

Bộ đếm: route node tăng `attempts` và ghi lại **qua
`Event(state={...})`**, không gán trực tiếp `ctx.state[...] = ...`.
Lý do (từ `adk-agent-builder/best-practices.md`): state ghi qua `Event`
nằm trong event history và sống sót qua replay; gán trực tiếp `ctx.state`
là side-effect mà replay không tái tạo được. Đây là điểm thiết kế này
sửa so với bản plan đầu tiên (plan nói "tăng biến đếm trong state" mà
không nói rõ bằng kênh nào).

Luật chuyển route của route node:

```text
validator ok                  -> "ok"
validator fail và attempts < 3 -> "retry"   (và attempts += 1)
validator fail và attempts >= 3 -> "fail"
cùng một lỗi lặp lại 2 lần liên tiếp -> "fail" (không chờ đủ 3 lần)
```

Nhánh `"fail"`: header/merge đi tới node fallback an toàn đã kiểm chứng
(luật scoring / không gộp) và ghi rõ vào log; schema meta đi tới
`mark_schema_meta_fallback` (join_hints và notes rỗng); chỉ khi
assemble_schema không ráp được bảng nào (shop không có bảng) thì
validate_shop mới fail và đi tới `mark_shop_failed` (không publish DB,
không xuất schema xấu). Riêng loop từng bảng KHÔNG có nhánh fail cả
shop: hết lượt thử thì fallback bảng đó về bản nháp đo được rồi đi
tiếp bảng sau, vì dữ liệu đo được vẫn đúng, chỉ thiếu mô tả. **Không
nhánh nào raise exception để kết thúc loop**, vì node raise sẽ dừng cả
workflow (xem §2 nguyên tắc 4 và plan §2.4 mục 3).

### 6.3. State keys — hợp đồng giữa các node

| Key | Ghi bởi node | Đọc bởi node | Ghi chú |
|---|---|---|---|
| `business_id`, `input_files`, `input_kind` | `init_shop` | mọi node cần | `input_kind`: `"xlsx"`, `"csv"`, `"sqlite"` |
| `readme_text` | `init_shop` | SchemaAgent (qua instruction `{readme_text?}`) | Đã cắt 4000 ký tự ở node quét, không đưa cả file vào state |
| `raw_tables` | `scan_sheets` / `scan_sqlite` | HeaderAgent, MergeAgent | List `RawTable` đã dump thành dict (JSON-safe) |
| `headers_final` | `validate_header` (qua route `"ok"`) | MergeAgent, clean_tables | Dict bảng -> header chốt |
| `header_rows_final` | `header_gate` (mặc định), `validate_header` (qua route `"ok"`), `mark_header_fallback` | clean_tables | Dict bảng -> dòng header chốt (0-based; -1 với SQLite); clean_tables đọc lại nguồn với đúng dòng này thay vì detect lại, vì detect lại sẽ vô hiệu hóa quyết định sửa dòng header của agent |
| `candidate_rows` (trong từng RawTable của `raw_tables`) | `scan_sheets` / `scan_sqlite` | `validate_header` | 15 dòng thô đầu của bảng, để validator kiểm chứng đề xuất header khớp một dòng bất kỳ trong đó (không bắt buộc khớp dòng quét đã chọn) |
| `merge_groups` | `merge_gate` (mặc định), `validate_merge` (qua route `"ok"`), `mark_merge_fallback` | clean_tables | Nhóm gộp chốt = nhóm khóa bởi code + phần agent đạt (hoặc nhóm khóa + giữ riêng khi fallback) |
| `merge_locked_groups` | `merge_gate` | `validate_merge`, `mark_merge_fallback` | Nhóm khóa bởi code: các bảng không phải SQLite có cùng chữ ký cột chuẩn hóa (cùng bộ cột, không kể thứ tự); ngoại lệ không khóa: bảng SQLite, nhóm chứa cả bảng lẫn biến thể lô. MergeAgent không nhận và không được quyết lại các bảng này |
| `clean_stats` | `clean_tables` | `validate_shop`, `finalize_shop` | `CleanStats` đã dump |
| `tmp_db_path`, `schema_path` | `write_sqlite_tmp`, `assemble_schema` | `validate_shop`, `publish_shop` | Đường dẫn tạm, chưa phải DB chính thức |
| `schema_table_drafts`, `schema_table_index`, `schema_table_results`, `schema_table_fallbacks`, `schema_table_draft_text` | `init_schema_tables`, `validate_schema_table`, `route_schema_table` | agent bảng, route bảng, `assemble_schema` | Trạng thái vòng lặp từng bảng |
| `schema_meta_summary_text`, `schema_meta` | `init_schema_meta`, `validate_schema_meta` | agent meta, `assemble_schema` | Tóm tắt gọn và kết quả meta cấp shop |
| `header_attempts`, `merge_attempts`, `schema_table_attempts`, `schema_meta_attempts` | route node tương ứng | chính route node đó | Bộ đếm vòng, ghi qua `Event(state=...)`; bộ đếm bảng reset về 0 khi sang bảng mới |
| `last_error` | validator node | route node, `finalize_shop` | Lỗi validator gần nhất + giá trị thật đo được, để route node phát hiện lỗi lặp và để log ghi lại |
| `shop_status` | `finalize_shop` | driver (đọc lại session sau chạy) | `"ok"` hoặc `"failed"` + lý do |

Mọi key khác ngoài bảng này đều là key lạ — thêm key mới thì phải thêm
dòng vào bảng này trước khi code, vì instruction template và test đều
đọc bảng này làm chuẩn.

### 6.4. Function node — quy tắc viết hàm

Lấy từ `adk-agent-builder/function-nodes.md` và `best-practices.md`:

- Chữ ký hàm là hợp đồng binding: tham số tên `ctx` nhận `Context`,
  tham số tên `node_input` nhận output của node tiền nhiệm, mọi tham số
  còn lại được tra theo tên trong `ctx.state` (thiếu mà không có default
  thì node raise `WorkflowDataError` **trước khi thân hàm chạy**). Vì vậy
  tham số optional trong node phải có default, và node trước phải ghi
  đúng key state mà node sau cần — bảng §6.3 là nơi kiểm tra điều này.
- Annotation vừa là tài liệu vừa là luật coerce: FunctionNode chạy mỗi
  argument qua Pydantic `TypeAdapter` từ annotation. `dict` thành model
  con, `str` nhận từ `Content` sẽ nối text và bỏ phần non-text (kèm
  warning). Tránh annotation union (`list | dict`) vì adapter sẽ chấp
  nhận bất kỳ thành viên nào — nếu bắt buộc dùng union thì kiểm tra bằng
  `isinstance` trong thân hàm.
- Một hàm **hoặc yield hoặc return, không bao giờ cả hai**: hàm có `yield`
  sẽ thành generator và giá trị `return` bị bỏ lặng lẽ, không báo lỗi.
  Node cần nhiều event thì yield từng `Event`; node một kết quả thì
  `return Event(output=..., state=...)` hoặc return giá trị thường (sẽ
  được bọc thành `Event(output=...)`).
- **Tối đa một output event cho mỗi lần chạy node.** Yield thêm event
  `message` (hiển thị) hay `state` (chỉ đổi state) thì được, nhưng event
  thứ hai mang `output` sẽ bị gộp thành list và lặng lẽ đổi kiểu
  `node_input` của node sau.
- Trả `None` nghĩa là không phát event — node sau không fire. Dùng có chủ
  ý cho nhánh "không có gì để làm tiếp", không dùng `None` thay cho
  `Verdict(fail)` (fail phải đi qua route để log và fallback chạy).
- Node I/O viết `async def` và bọc call chặn bằng `asyncio.to_thread`
  (xem §4.6). Node thuần tính toán viết hàm thường.

### 6.5. LlmAgent node — quy tắc viết agent

Lấy từ `adk-agent-builder/llm-agent-nodes.md` và lần rà source 2.13.0:

- Agent đưa thẳng vào `edges`, framework tự bọc thành node — không import
  hay subclass wrapper nội bộ. Mode mặc định khi làm node là
  `single_turn`; không dùng `mode='chat'` ở giữa graph (graph validation
  từ chối edge vào agent chat từ node khác, chat chỉ hợp lệ ngay sau
  `START`).
- `include_contents='none'` cho các agent trích xuất/phân loại không trạng
  thái (HeaderAgent, MergeAgent, SchemaAgent đều thuộc loại này): agent
  chỉ xét input hiện tại, không bị history của vòng loop trước làm nhiễu.
  Trí nhớ vòng lặp đi qua state (`{feedback?}`), không qua history.
- Mọi agent đều có `output_schema` (Pydantic model ở §5) và `output_key`
  (ghi output đã validate, dạng dict, vào state để node sau đọc). Agent
  không có `output_schema` mà đi vào `JoinNode` hay session DB sẽ làm hỏng
  serialize — ở đây không dùng JoinNode, nhưng quy tắc vẫn giữ để sau
  này thêm fan-out không phải sửa agent.
- Instruction template chỉ đọc state: `{ten_key}` raise nếu key thiếu,
  `{ten_key?}` thay bằng chuỗi rỗng nếu thiếu. **Không bao giờ viết
  `{node_input}` trong instruction** — `node_input` được đưa cho model
  dưới dạng user message, còn placeholder chỉ resolve từ state, nên
  `{node_input}` sẽ raise `KeyError` lúc chạy.
- Nhiệt độ qua `generate_content_config` (temperature 0 cho mọi agent),
  không đặt trực tiếp trên agent; `response_schema` qua `output_schema`,
  không đặt trong `generate_content_config` (validator của ADK sẽ từ
  chối nếu đặt nhầm).
- Agent không được tự ý đổi model: model lấy từ `IngestConfig` qua
  `_services.py`, mọi agent trong một lần chạy dùng cùng một model. Model
  mặc định của ADK khi không cấu hình là `gemini-3.5-flash` — vì vậy
  không một agent nào được để trống trường model.
- Mỗi agent có `SkillToolset` chỉ chứa đúng 1 skill của bước đó, và
  instruction ghi rõ tên skill phải load đầu tiên (xem plan §2.4 mục 4:
  framework không tự ép agent nạp đúng skill, cấu trúc này mới ép được).

---

## 7. Callbacks — guardrail nằm ở đâu (từ lần rà source 2.13.0)

Toàn bộ callback đặt trong `_callbacks.py`, không rải rác trong node hay
agent. Bảng này là hợp đồng: đổi ngưỡng hay đổi vị trí đều phải sửa bảng
này trước.

| Callback | Việc làm | Ngưỡng dừng |
|---|---|---|
| `before_tool_callback` | **Chặn duy nhất có tác dụng.** Kiểm tra tool ghi DB: đường dẫn có nằm trong thư mục của shop đang xử lý (`tool_context.state["business_id"]`) không. Kiểm tra tool chạy SQL kiểm chứng: có phải SELECT không, có chạm bảng ngoài schema file không. Vi phạm thì trả dict non-None (kể cả `{}` kèm thông điệp lỗi) — tool thật sẽ **không** chạy. | Trả non-None thì dừng và dùng dict đó làm kết quả; trả `None` thì tool chạy bình thường. |
| `after_tool_callback` | Chỉ cắt ngắn/thay thế kết quả trả về cho model (cắt mẫu, cắt danh sách distinct, cắt kết quả SQL). **Không dùng để chặn** — vì lúc này tool đã chạy xong, tác dụng phụ đã xảy ra. | Trả non-None thì thay thế kết quả; trả `None` thì giữ kết quả gốc. |
| `on_tool_error_callback` | Biến exception thành payload có cấu trúc (loại lỗi, câu lệnh lỗi, giá trị thật đo được) để loop viết lại có cái mà sửa. | Trả non-None thì dùng làm recovery response; trả `None` thì chuyển sang callback tiếp theo. |
| `before_model_callback` | Tiêm đúng phần state node đó cần, cắt danh sách dài trước khi gọi model. | Trả response truthy thì bỏ qua gọi model; trả `None` thì gọi bình thường. **Ngưỡng này khác `before_tool`** — không được lẫn. |
| `after_model_callback` | Validate JSON theo `output_schema` đã nằm ở tầng framework; callback này chỉ dùng để ghi log và cắt ngắn, không tự sửa JSON. | Trả response truthy thì thay thế response của model. |
| `on_model_error_callback` | Biến lỗi gọi model thành payload cho loop (để route node quyết định retry/fail), không tự gọi lại model trong callback. | Trả non-None thì dùng làm recovery response. |

Hai lưu ý về thứ tự (đã kiểm chứng trong source):

- Plugin chạy trước callback của agent: nếu plugin đã trả kết quả thì
  callback của agent bị suppress hoàn toàn. Thiết kế này không dùng
  plugin can thiệp vào tool/model, nên callback trong bảng trên luôn là
  lớp guardrail duy nhất — không được thêm plugin "tiện tay" mà không
  sửa bảng này.
- Khi `before_tool` đã trả lời thì `after_tool` bị bỏ qua (vì không có
  "tool run" nào để mô tả). Test guardrail phải assert trên kết quả cuối
  và trên việc file/DB **không** thay đổi, không assert trên việc
  `after_tool` có chạy không.

Không có callback chặn path/SQL có sẵn trong ADK — `_callbacks.py` là code
tự viết, và đó là nơi duy nhất được phép chứa logic chặn.

---

## 8. Driver, session, artifact và event log

`_driver.py` và `_services.py` là hai module duy nhất chạm tới Runner và
các service. Node và agent không được tự tạo session hay artifact service.

- **Tạo session trước khi chạy.** `Runner` mặc định
  `auto_create_session=False`; driver hoặc gọi
  `session_service.create_session(app_name="datamate-ingest",
  user_id="datamate", session_id=<business_id>)` trước, hoặc bật
  `auto_create_session=True` trên `Runner` thường. `InMemoryRunner` chỉ
  dùng cho test và không nhận cờ này. Resume shop lỗi đi qua `get_session`
  (tạo lại session với id đã tồn tại sẽ raise `AlreadyExistsError`).
- **SessionService:** dev dùng `InMemorySessionService`; batch/Docker dùng
  `DatabaseSessionService` với URL async driver
  (`sqlite+aiosqlite:///`) và extra `google-adk[db]`, hoặc
  `SqliteSessionService(db_path)` gọn nhẹ hơn. Key state tiền tố `temp:`
  không được persist — không dùng `temp:` cho dữ liệu cần sống qua resume.
- **ArtifactService:** dùng `FileArtifactService(root_dir=...)` có sẵn
  (layout `root/apps/.../sessions/.../artifacts/.../versions/`, stage file
  `.pending` rồi rename publish). Version artifact đầu tiên là 0.
  Artifact của session gồm `schema.json` (file schema của shop) và log
  ingest; file vật lý tương ứng nằm ở `sqlite/schemas/<business_id>.json`
  và `logs/ingest/<business_id>.jsonl` để workflow trả lời đọc trực tiếp.
  **Không đưa file SQLite binary cho model dưới dạng artifact** — model
  chỉ nhận tên artifact trước, nội dung chỉ gắn vào request khi nó gọi
  `load_artifacts`, và binary DB không phải thứ model đọc được; DB chỉ
  được đọc bằng tool SQL của code.
- **Event log:** ADK không có hàm export event ra file. Driver tự lặp
  `run_async`, serialize từng event (`model_dump_json`, bytes trong event
  sẽ thành base64) và ghi `.jsonl`. Ngoài ra, nếu dùng
  `DatabaseSessionService` thì toàn bộ event đã nằm trong DB session dưới
  dạng JSON — file `.jsonl` vẫn giữ vì nó đọc được ngay không cần mở DB.
- **Assert trong test và driver:** không assert trên `event.output` của
  chính event của LlmAgent (runner clear nó trên bản copy trước khi đưa
  cho caller). Assert trên output của node sau, trên
  `session.state[output_key]`, hoặc trên `event.content.parts[*].text`.
  Lọc event theo node thì dùng `event.node_info.path` (dạng
  `workflow@1/step@1`), không dùng `event.author` (đó là tên workflow bao
  ngoài, không phải tên node).
- **Một shop một lần chạy, một session một lần chạy.** Driver chạy các
  shop tuần tự (một shop tại một thời điểm, giải phóng rồi mới sang shop
  sau). Giới hạn song song (nếu sau này thêm) đặt ở driver, không đặt ở
  workflow — vì `max_concurrency` của workflow chỉ tính node do graph
  schedule, không tính node spawn bằng `ctx.run_node()`.

---

## 9. Kiểm thử — theo `adk-style/testing.md` và `adk-agent-builder/testing.md`

- **Chạy bằng pytest, `asyncio_mode = "auto"`** để `async def test_...`
  chạy không cần decorator. Test integration chạm model thật thì đánh dấu
  riêng (`pytest -m integration` mới chạy) và skip khi không có key/API;
  test mặc định (`pytest -m "not integration"`) không được gọi model thật.
- **Tên test mô tả hành vi, không mô tả cơ chế.** Ví dụ:
  `test_validator_bac_nhom_gop_khac_vai_tro` (đúng),
  `test_validate_merge_goi_helper` (sai — đó là cơ chế nội bộ).
- **Một test một hành vi.** Test nào không mô tả được bằng một câu thì
  tách ra. Cấu trúc mỗi test: arrange — act — assert, tách bạch bằng
  dòng trống; helper chỉ dùng cho một test thì định nghĩa inline trong
  test đó, chỉ đưa lên module khi từ 3 test trở lên dùng chung.
- **Test qua public interface, không chọc vào state nội bộ.** Assert trên
  output, side-effect (file/DB có/không thay đổi) và lỗi — không assert
  trên thuộc tính private (`_workers`, `_state`...). Dùng component thật
  càng nhiều càng tốt; chỉ mock ở biên: API model, session store, cloud.
- **Model giả cho test agent:** subclass `BaseLlm` (chỉ có một abstract
  method) trả response canned theo thứ tự, hoặc dùng
  `testing_utils.MockModel` của ADK; chạy workflow bằng `InMemoryRunner`
  trên in-memory service. Một `InMemoryRunner` và một session cho một
  test; `app_name` duy nhất cho mỗi test để pytest chạy song song không
  đụng nhau.
- **Test guardrail bằng side-effect, không bằng lời hứa.** Test chặn ghi
  sai đường dẫn phải assert file đích **không** thay đổi sau khi chạy;
  test chặn SQL không phải SELECT phải assert DB **không** thay đổi.
- **Parametrize thay vì copy-paste** khi cùng một hợp đồng phải đúng trên
  nhiều biến thể (ví dụ: XLSX/CSV/SQLite, validator ok/retry/fail).
- **Ma trận test tối thiểu cho mỗi loop** (header/merge/schema): validator
  ok đi tiếp; validator fail lần 1 thì quay lại agent với lỗi đầy đủ;
  fail đủ 3 lần thì đi nhánh fail (fallback hoặc shop FAILED); cùng lỗi
  lặp 2 lần thì fail sớm; agent trả JSON sai schema thì validation raise
  và loop bắt được lỗi đó.

---

## 10. Điểm mở rộng và điểm cấm đổi

### 10.1. Thêm mới mà không phá hợp đồng

- **Thêm loại file nguồn mới** (ví dụ thêm một định dạng bảng tính khác):
  thêm một nhánh ở `branch_input` + một node quét mới trong
  `_scan_nodes.py`. Không được sửa validator, route node hay agent — vì
  đầu ra của node quét mới vẫn phải là `RawTable` như hợp đồng §5.
- **Thêm một skill mới** (ví dụ skill cho một họ shop đặc biệt): thêm thư
  mục skill + thêm agent riêng dùng toolset chỉ chứa skill đó. Không
  được nhét thêm skill vào toolset của agent cũ rồi hy vọng model tự chọn
  đúng (xem plan §2.4 mục 4).
- **Thêm một node kiểm chứng mới**: thêm validator node + route, và thêm
  dòng vào bảng state §6.3 trước khi code. Không được để node mới đọc
  key state chưa có trong bảng.

### 10.2. Cấm đổi nếu không sửa đồng thời cả cụm

- State keys (§6.3), tên node, edge list (§6.1), Pydantic models (§5) và
  instruction template của agent là **một cụm**: đổi một thứ mà không đổi
  cả cụm còn lại sẽ làm node raise `WorkflowDataError` trước khi thân hàm
  chạy, hoặc làm instruction đọc key state không tồn tại và raise
  `KeyError`.
- File schema JSON (§6 plan) là hợp đồng với workflow trả lời câu hỏi:
  đổi field trong `ShopSchema` thì workflow SQL cũng phải đổi cùng lúc,
  và evalset schema phải cập nhật theo.
- `_callbacks.py` là nơi duy nhất chứa logic chặn: không được thêm logic
  chặn thứ hai ở node hay driver (hai lớp chặn sẽ lệch nhau và test
  guardrail sẽ không biết lớp nào đã chặn).

---

## 11. Rủi ro còn lại mà thiết kế này chưa khử được

1. **Model local viết JSON kém.** Thiết kế đã khóa bằng `output_schema` +
   validator + loop, nhưng nếu model local không hỗ trợ
   `output_schema_and_tools` thì ADK rơi về workaround tool
   `set_model_response` (kém tin cậy hơn schema native). G0 spike phải đo
   tỷ lệ JSON hợp schema của chính model sẽ dùng, không giả định.
2. **ADK đổi API.** Skills vẫn tự nhận experimental; `workflow/` toàn
   module `_` (nội bộ, có thể đổi không cần major version). Giảm nhẹ: pin
   2.13.0, code của mình chỉ chạm public API (`Workflow`, `Event`,
   `LlmAgent`, `Runner`, `FileArtifactService`, `SkillToolset`), không
   import module `_` nội bộ của ADK, không subclass node nội bộ.
3. **Replay và side-effect.** Thiết kế đã dùng DB tạm + rename nguyên tử
   và ghi state qua `Event`, nhưng node nào sau này thêm side-effect mới
   (ví dụ ghi file báo cáo giữa chừng) mà quên nguyên tắc này sẽ phá
   resume. Review code mới phải kiểm tra điểm này đầu tiên.
4. **Định dạng của ADK skill builder cũ hơn repo.** `adk-agent-builder`
   ghi API đã kiểm chứng với 2.6.2 trong khi repo là 2.13.0 — mọi symbol
   trong tài liệu này đều đã đối chiếu source 2.13.0, nhưng G0 spike vẫn
   phải chạy thật một graph tối thiểu để khóa lại lần cuối.
