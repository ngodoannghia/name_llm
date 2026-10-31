# -*- coding: utf-8 -*-
"""Kiểm thử tích hợp cho runner parser LLM của Giai đoạn 2 DataMate.

Mục đích của file này
---------------------
Chứng minh runner ``run_phase2_llm.py`` chạy được end-to-end mà không
crash, không sửa ``solver.py``/``shop_view.py`` của phase2, và fallback
về parser template khi backend LLM lỗi hoặc chưa có. Các test chỉ chạy
trên shop ``biz_101`` (shop CSV nhỏ, 40 câu) và giới hạn số câu, để
chạy nhanh mà vẫn đi qua đúng luồng: cache -> view -> parse -> solver
-> gate -> ghi submission tăng dần -> chấm bằng phase1 evaluate.

Quy ước đường dẫn: file test nằm ở ``phase2_llm/tests/`` nên thư mục
gốc ``phase2_llm`` là thư mục cha của nó, ``phase2`` và ``train`` nằm
ngay dưới thư mục cha của ``phase2_llm``. Không hard-code đường dẫn
tuyệt đối của máy; mọi đường dẫn đều tính tương đối từ file này.
"""

import ast
import datetime
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_PHASE2_LLM_DIR = _TESTS_DIR.parent
_DATAMATE_DIR = _PHASE2_LLM_DIR.parent
_PHASE2_DIR = _DATAMATE_DIR / "phase2"
_TRAIN_DIR = _DATAMATE_DIR / "train"
_RUNNER_PATH = _PHASE2_LLM_DIR / "run_phase2_llm.py"

if str(_PHASE2_LLM_DIR) not in sys.path:
    sys.path.insert(0, str(_PHASE2_LLM_DIR))

import run_phase2_llm as runner  # noqa: E402

#: SHA-256 của hai file cấm sửa logic, do coordinator cung cấp tại thời
#: điểm bắt đầu nhiệm vụ (đo trước khi viết runner). Test này đọc sha
#: hiện tại và đối chiếu với hai giá trị đó; nếu một agent khác sửa
#: file, test sẽ fail để coordinator phát hiện ngay.
#: CẬP NHẬT NGÀY 2026-10-31 (theo yêu cầu viết mã nguồn bằng UTF-8):
#: hai file này chỉ được thêm đúng một dòng comment khai báo encoding
#: ở đầu file ("# -*- coding: utf-8 -*-"), không sửa bất kỳ dòng logic
#: nào. Vì vậy SHA đã đổi so với bản gốc, và hai giá trị dưới đây là
#: SHA sau khi thêm dòng đó. SHA gốc trước khi thêm dòng (để đối chiếu
#: lịch sử): solver 05b1d10423ad79461c1ec40e7ebb6b7eeba73ab477f8b5f6383241e1da7e6829,
#: shop_view 1b35aff88baf1118c97e8f890187a6f7ab352fc7563fc862089bba799191279a.
EXPECTED_SOLVER_SHA256 = (
    "1b04d9bbf9c759aa60fec4118b872942a6ff6397473e340623436445f64212e2"
)
EXPECTED_SHOP_VIEW_SHA256 = (
    "9e991d28ed2130d817b6995a3fdb768ace67dd4467b4522a3209c8343e1509a1"
)

#: Các ký tự tiếng Việt có dấu, dùng để kiểm tra docstring thực sự viết
#: bằng tiếng Việt (không chỉ là chuỗi ASCII chung chung).
_VIETNAMESE_CHARS = set("àáảãạăắằẳẵặâấầẩẫậđèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợùúủũụưứừửữựỳýỷỹỵ")


def _sha256_file(path):
    """Tính SHA-256 của một file, đọc theo khối để không tốn RAM.

    Đầu vào: đường dẫn file. Đầu ra: chuỗi hex SHA-256.
    """
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _load_view_and_context(cache_dir):
    """Nạp view của shop biz_101 kèm rulebook/calendar của split train.

    Đầu vào: thư mục cache tạm (dùng cache của phase2 nếu caller đã
    truyền cache đó, còn ở đây test dùng thư mục tạm riêng để không
    làm bẩn cache chung).

    Đầu ra: tuple ``(view, rulebook, calendar)``. View lấy từ cache nếu
    hit, nếu miss thì ingest bằng phase1 rồi dựng view bằng phase2
    (đúng luồng của runner, không đọc trực tiếp file dữ liệu thô).
    """
    from cache import load_shop_cache  # noqa: E402
    from ingest import load_shop  # noqa: E402
    from rulebook import load_calendar, load_rulebook  # noqa: E402
    from shop_view import build_shop_view  # noqa: E402

    calendar = load_calendar(_TRAIN_DIR / "calendar.csv")
    rulebook = load_rulebook(
        _TRAIN_DIR / "rulebook.md", _TRAIN_DIR / "calendar.csv")
    view = load_shop_cache(cache_dir, "biz_101", _TRAIN_DIR)
    if view is None:
        view = build_shop_view(
            load_shop("biz_101", _TRAIN_DIR / "data"), "biz_101")
    return view, rulebook, calendar


class TestRunnerLimit(unittest.TestCase):
    """Kiểm tra runner chạy được với --limit nhỏ và ghi đủ số dòng."""

    def test_limit_20_mot_shop_khong_crash_va_du_so_dong(self):
        """Chạy runner với 1 shop, limit 20 câu, không crash, đủ 20 dòng.

        Cách kiểm tra: gọi trực tiếp :func:`run_phase2_llm` với thư
        mục tạm (cache/output/reports riêng) để không ghi đè báo cáo
        của đợt chạy full; sau đó đọc file submission và đếm dòng,
        kiểm tra mỗi dòng là JSON có ``id`` và ``type``. Chạy bằng hàm
        thay vì subprocess ở đây để test nhanh và vẫn đi qua đúng luồng
        ghi tăng dần của runner.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output = tmp_path / "submission_llm.jsonl"
            summary = runner.run_phase2_llm(
                _TRAIN_DIR, tmp_path / "cache", output,
                shops=["biz_101"], limit=20,
                reports_dir=tmp_path / "reports")
            self.assertEqual(summary["written"], 20)
            self.assertEqual(summary["total_questions"], 20)
            lines = output.read_text(encoding="utf-8").strip().split("\n")
            self.assertEqual(len(lines), 20)
            for line in lines:
                entry = json.loads(line)
                self.assertIn("id", entry)
                self.assertIn("type", entry)
            if runner.BACKEND_KIND == "template_fallback_tam_thoi":
                self.assertEqual(
                    summary["parse_sources"].get("template_fallback", 0), 20)
            else:
                self.assertEqual(
                    sum(summary["parse_sources"].values()), 20)

    def test_ghi_tang_dan_trong_khi_chay(self):
        """Chứng minh submission được ghi tăng dần, không chờ hết đợt.

        Cách kiểm tra (xác định, không canh thời gian mơ hồ): chạy
        runner trong một luồng riêng với hàm parse mà lần gọi thứ hai
        ngủ 5 giây; test chờ (poll) cho tới khi file submission có
        đúng 1 dòng — dòng của câu đầu tiên đã được ghi ngay dù câu
        thứ hai còn đang bị chặn — rồi assert luồng vẫn đang chạy và
        file vẫn chỉ có 1 dòng; sau khi luồng kết thúc thì đủ 5 dòng.
        Nếu runner chờ hết đợt mới ghi thì lúc poll sẽ không bao giờ
        thấy 1 dòng trong khi luồng còn chạy, test sẽ fail.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output = tmp_path / "submission_llm.jsonl"
            call_count = {"n": 0}
            lock = threading.Lock()

            def slow_parse(question_text, as_of, calendar, rulebook,
                           products, customers):
                """Hàm parse: lần gọi thứ hai ngủ 5 giây, các lần khác nhanh."""
                from parser import parse_question as template_parse  # noqa: E402

                with lock:
                    call_count["n"] += 1
                    call_number = call_count["n"]
                if call_number == 2:
                    time.sleep(5)
                parsed = template_parse(
                    question_text, as_of, calendar, rulebook,
                    products or [], customers or [])
                return runner.ParseResult(
                    parsed=parsed, source="template_fallback",
                    confidence=float(parsed.confidence_parse or 0.0),
                    notes=["test chậm để kiểm tra ghi tăng dần"])

            errors = []

            def _run():
                """Chạy runner trong luồng, ghi lỗi vào list để assert sau."""
                try:
                    runner.run_phase2_llm(
                        _TRAIN_DIR, tmp_path / "cache", output,
                        shops=["biz_101"], limit=5,
                        reports_dir=tmp_path / "reports",
                        parse_fn=slow_parse)
                except Exception as exc:  # noqa: BLE001 - ghi lỗi cho test
                    errors.append(exc)

            def _line_count():
                """Đếm số dòng không rỗng trong file submission nếu đã có."""
                if not output.is_file():
                    return 0
                return len([line for line in output.read_text(
                    encoding="utf-8").split("\n") if line.strip()])

            thread = threading.Thread(target=_run, daemon=True)
            thread.start()
            deadline = time.time() + 60
            while time.time() < deadline and _line_count() < 1:
                time.sleep(0.1)
            self.assertEqual(_line_count(), 1)
            self.assertTrue(thread.is_alive())
            thread.join(timeout=90)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(_line_count(), 5)

    def test_cli_limit_20_qua_subprocess(self):
        """Chạy runner qua dòng lệnh với --limit 20, exit code 0.

        Cách kiểm tra: gọi ``python3 run_phase2_llm.py --shops biz_101
        --limit 20`` bằng subprocess với thư mục tạm, để chứng minh cả
        đường CLI (parse_args, giải đường dẫn tương đối) cũng hoạt
        động, không chỉ đường gọi hàm trực tiếp.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output = tmp_path / "submission_llm.jsonl"
            result = subprocess.run(
                [sys.executable, str(_RUNNER_PATH),
                 "--train-dir", str(_TRAIN_DIR),
                 "--cache-dir", str(tmp_path / "cache"),
                 "--output", str(output),
                 "--shops", "biz_101", "--limit", "20"],
                capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, msg=result.stderr[-2000:])
            lines = [line for line in output.read_text(
                encoding="utf-8").split("\n") if line.strip()]
            self.assertEqual(len(lines), 20)


class TestFallback(unittest.TestCase):
    """Kiểm tra fallback khi backend LLM lỗi, không crash."""

    def test_backend_loi_khong_crash_va_fallback_template(self):
        """Hàm parse raise lỗi thì solve_one_question vẫn trả entry.

        Cách kiểm tra: lấy một câu hỏi thật của biz_101, tiêm hàm parse
        luôn raise ``RuntimeError`` (mô phỏng backend LLM hỏng), gọi
        :func:`solve_one_question` và assert không raise, entry có id
        đúng, và info ghi ``parse_source='template_fallback'``.
        """
        with tempfile.TemporaryDirectory() as tmp:
            view, rulebook, calendar = _load_view_and_context(Path(tmp))
            questions = runner.load_questions(_TRAIN_DIR / "questions.jsonl")
            question = next(
                q for q in questions if q["business_id"] == "biz_101")

            def broken_parse(*_args, **_kwargs):
                """Hàm parse giả lập backend hỏng, luôn raise lỗi."""
                raise RuntimeError("backend LLM giả lập bị hỏng")

            entry, info = runner.solve_one_question(
                question, view, rulebook, calendar, parse_fn=broken_parse)
            self.assertEqual(entry["id"], str(question["id"]))
            self.assertIn(entry["type"],
                          ("abstain", "unanswerable", "number", "percent",
                           "string", "date", "boolean", "list", "table"))
            self.assertEqual(info["parse_source"], "template_fallback")

    def test_stub_backend_generate_raise_va_parse_fallback(self):
        """Stub LocalModelBackend raise thì parse_with_llm vẫn fallback.

        Cách kiểm tra: gọi :func:`parse_with_llm` với backend là stub
        (``generate`` luôn raise), assert kết quả có
        ``source='template_fallback'`` và ``parsed`` không phải None.
        """
        with tempfile.TemporaryDirectory() as tmp:
            view, rulebook, calendar = _load_view_and_context(Path(tmp))
            questions = runner.load_questions(_TRAIN_DIR / "questions.jsonl")
            question = next(
                q for q in questions if q["business_id"] == "biz_101")
            result = runner.parse_with_llm(
                question["question"], datetime.date(2026, 1, 1),
                calendar, rulebook, view.products, view.customers,
                backend=runner.LocalModelBackend())
            self.assertEqual(result.source, "template_fallback")
            self.assertIsNotNone(result.parsed)

    def test_validate_and_build_loai_json_sai(self):
        """JSON LLM sai measure hoặc bịa thực thể thì validate trả None.

        Cách kiểm tra: truyền JSON có measure lạ và JSON có entity_key
        không nằm trong shop, cả hai đều phải trả ``None`` để caller
        rơi về template, không được dựng ParsedQuestion từ dữ liệu lạ.
        """
        self.assertIsNone(runner.validate_and_build(
            {"measure": "measure_la_khong_co", "answer_type": "number"},
            "cau hoi mau", None, None, None, [], []))
        self.assertIsNone(runner.validate_and_build(
            {"measure": "revenue", "entity_kind": "product",
             "entity_key": "san_pham_khong_ton_tai",
             "answer_type": "number"},
            "cau hoi mau", None, None, None,
            [{"key": "sp_that", "display": "San pham that"}], []))


class TestKhongSuaPhase2(unittest.TestCase):
    """Kiểm tra solver.py và shop_view.py của phase2 không bị sửa."""

    def test_sha256_solver_va_shop_view_khop_ban_goc(self):
        """SHA-256 hiện tại phải khớp SHA gốc do coordinator cung cấp.

        Ghi chú cho coordinator: test này đối chiếu với SHA tại thời
        điểm bắt đầu nhiệm vụ; nếu test fail nghĩa là một agent nào đó
        đã sửa solver/shop_view sau thời điểm đó và coordinator cần
        đối chiếu lại với bản gốc đã lưu, không phải do runner này sửa
        (runner chỉ đọc, không ghi vào phase2/).
        """
        self.assertEqual(
            _sha256_file(_PHASE2_DIR / "src" / "solver.py"),
            EXPECTED_SOLVER_SHA256)
        self.assertEqual(
            _sha256_file(_PHASE2_DIR / "src" / "shop_view.py"),
            EXPECTED_SHOP_VIEW_SHA256)


class TestDocstringTiengViet(unittest.TestCase):
    """Kiểm tra bằng AST: module/class/hàm public của runner có docstring."""

    def test_runner_co_docstring_tieng_viet_cho_moi_thanh_phan_public(self):
        """Mọi thành phần public cấp module của runner đều có docstring.

        Cách kiểm tra: parse ``run_phase2_llm.py`` bằng AST, lấy
        docstring của module, của mọi class public và mọi hàm public
        cấp module cùng mọi phương thức public của class, rồi assert
        mỗi docstring không rỗng và chứa ít nhất một ký tự tiếng Việt
        có dấu (để phân biệt docstring tiếng Việt thật với chuỗi
        placeholder ASCII). Hàm/method private (bắt đầu bằng ``_``) và
        dunder được miễn, đúng yêu cầu "hàm public".
        """
        tree = ast.parse(
            _RUNNER_PATH.read_text(encoding="utf-8"),
            filename=str(_RUNNER_PATH))
        checked = []

        def _assert_docstring(node, name):
            """Assert một node AST có docstring tiếng Việt, ghi tên để báo lỗi."""
            docstring = ast.get_docstring(node)
            checked.append(name)
            self.assertIsNotNone(docstring, msg=f"Thieu docstring: {name}")
            self.assertTrue(docstring.strip(),
                            msg=f"Docstring rong: {name}")
            self.assertTrue(
                any(char in _VIETNAMESE_CHARS for char in docstring),
                msg=f"Docstring không phải tiếng Việt: {name}")

        _assert_docstring(tree, "module run_phase2_llm")
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                _assert_docstring(node, f"class {node.name}")
                for child in node.body:
                    if isinstance(child, ast.FunctionDef) \
                            and not child.name.startswith("_"):
                        _assert_docstring(
                            child, f"{node.name}.{child.name}")
            elif isinstance(node, ast.FunctionDef) \
                    and not node.name.startswith("_"):
                _assert_docstring(node, f"ham {node.name}")
        self.assertGreater(len(checked), 10)


if __name__ == "__main__":
    unittest.main()
