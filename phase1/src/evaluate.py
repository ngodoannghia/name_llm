# -*- coding: utf-8 -*-
"""Module chấm điểm cho Giai đoạn 1 DataMate (Zalo AI Challenge 2026).

Mục đích của module này là chấm một bản submission theo đúng luật chấm
của ban tổ chức (xem ``~/workspace/user/files/evaluate.md``) và tính chỉ số
Penalized Accuracy (PA):

    PA = (correct - wrong) / total

trong đó đáp án đúng được +1, đáp án sai được -1, đáp án abstain (hoặc
thiếu đáp án) được 0. Mọi câu hỏi có trọng số bằng nhau, không có điểm
từng phần.

Vì sao module này tồn tại riêng, tách khỏi code trả lời câu hỏi: việc chấm
điểm phải độc lập với cách sinh đáp án, để cùng một khung chấm có thể dùng
cho mọi submission (hoàn hảo, abstain, broken, hay do mô hình sinh ra) mà
không làm thay đổi kết quả chỉ vì cách sinh đáp án thay đổi.

Phạm vi và giới hạn:

- Module này chỉ đọc các file JSONL đáp án/submission, không đọc dữ liệu
  train dạng bảng, không gọi mạng hay API, chỉ dùng thư viện chuẩn Python.
- ``HOLDOUT_BUSINESSES`` chỉ là cấu hình chia validation do nhiệm vụ Giai
  đoạn 1 quy định. Nó KHÔNG phải logic trả lời câu hỏi ăn theo
  ``business_id`` và KHÔNG được dùng trong ingest hay rulebook; ở đây nó
  chỉ dùng để tính riêng ``holdout_pa`` trong báo cáo.
- Module này chấm theo đáp án tham chiếu đã cho, không tự suy luận đáp án
  đúng từ dữ liệu gốc. Nếu đáp án tham chiếu sai thì kết quả chấm cũng
  sai theo — đó là giới hạn cố hữu của mọi khung chấm dựa trên đáp án mẫu.

Định dạng dữ liệu đầu vào (mỗi dòng JSONL là một bản ghi có khóa ``id``):

- Đáp án tham chiếu: ``{"id": ..., "type": ..., "answer": ..., "alias": [...]}``.
- Submission: cùng định dạng; một mục submission thiếu hẳn, hoặc có
  ``"type": "abstain"``, đều được tính là abstain.
- Trường ``alias`` (nếu có) liệt kê các dạng chấp nhận được thêm của đáp
  án: với ``string`` là danh sách các chuỗi; với ``list`` là danh sách
  theo từng vị trí, mỗi vị trí là danh sách các dạng chấp nhận được; với
  ``table`` là danh sách theo từng dòng, mỗi dòng là danh sách các dạng
  chấp nhận được của ô tên trong dòng đó. Khi ``alias`` rỗng hoặc vắng
  mặt thì chỉ dạng chính trong ``answer`` được chấp nhận.
"""

from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

__all__ = [
    "HOLDOUT_BUSINESSES",
    "ScoreReport",
    "load_answers",
    "load_submission",
    "normalize_string_answer",
    "answers_match",
    "score_submission",
    "make_perfect_submission",
    "make_abstain_submission",
    "make_broken_submission",
]


# ---------------------------------------------------------------------------
# Cấu hình chia validation (chỉ dùng trong module chấm điểm này)
# ---------------------------------------------------------------------------

#: Tập các shop thuộc holdout validation của Giai đoạn 1.
#:
#: Đây là cấu hình chia validation do nhiệm vụ quy định, KHÔNG phải logic
#: trả lời câu hỏi ăn theo ``business_id``. Module này chỉ dùng nó để tính
#: riêng ``holdout_pa`` trong :class:`ScoreReport`; ingest và rulebook
#: tuyệt đối không được dùng tập này để thay đổi cách đọc dữ liệu hay
#: cách hiểu luật.
HOLDOUT_BUSINESSES: frozenset = frozenset(
    {"biz_124", "biz_123", "biz_106", "biz_105", "biz_117"}
)


# ---------------------------------------------------------------------------
# Báo cáo chấm điểm
# ---------------------------------------------------------------------------


@dataclass
class ScoreReport:
    """Báo cáo chấm điểm tổng hợp cho một bản submission.

    Mục đích: gom toàn bộ kết quả chấm vào một đối tượng duy nhất để
    coordinator và các agent khác đọc được mà không cần tự tính lại từ
    danh sách từng câu.

    Thuộc tính:

    - ``total``: tổng số câu hỏi trong đáp án tham chiếu. Submission có
      thừa câu không nằm trong đáp án tham chiếu sẽ bị bỏ qua, không tính
      vào ``total`` (vì không có căn cứ để chấm chúng).
    - ``correct``: số câu chấm ``correct``.
    - ``wrong``: số câu chấm ``wrong``.
    - ``abstain``: số câu chấm ``abstain`` (thiếu đáp án hoặc type abstain).
    - ``pa``: Penalized Accuracy toàn bộ, bằng ``(correct - wrong) / total``
      (bằng 0.0 khi ``total`` bằng 0 để tránh chia cho 0; trường hợp này
      chỉ xảy ra khi file đáp án tham chiếu rỗng).
    - ``per_type``: dict từ tên type (``number``, ``percent``, ...) sang
      dict ``{'total', 'correct', 'wrong', 'abstain', 'pa'}`` của riêng
      type đó. Type lấy từ đáp án tham chiếu, kể cả khi submission trả
      type khác (câu đó vẫn thuộc về type tham chiếu, và bị tính wrong).
    - ``per_business``: tương tự ``per_type`` nhưng chia theo
      ``business_id`` lấy từ ``questions`` truyền vào
      :func:`score_submission`. Nếu không có ``questions`` hoặc một câu
      không tìm được ``business_id`` thì câu đó rơi vào nhóm ``'unknown'``.
    - ``holdout_pa``: PA tính riêng trên các câu thuộc
      :data:`HOLDOUT_BUSINESSES`. Bằng ``None`` khi không có câu nào thuộc
      holdout (ví dụ khi chấm mà không truyền ``questions``, vì khi đó
      mọi câu đều là ``'unknown'`` và không xác định được shop nào là
      holdout). ``None`` ở đây mang nghĩa "không tính được", khác với
      0.0 (tính được và bằng 0).
    """

    total: int
    correct: int
    wrong: int
    abstain: int
    pa: float
    per_type: dict[str, dict] = field(default_factory=dict)
    per_business: dict[str, dict] = field(default_factory=dict)
    holdout_pa: float | None = None


# ---------------------------------------------------------------------------
# Nạp dữ liệu
# ---------------------------------------------------------------------------


def _load_jsonl_records(path: str | Path) -> dict[str, dict]:
    """Đọc file đáp án/submission và trả về dict theo ``id``.

    Đầu vào: đường dẫn tới file JSONL, mỗi dòng (không rỗng) là một đối
    tượng JSON có khóa ``id``. Ngoài ra hàm cũng chấp nhận file JSON dạng
    danh sách các bản ghi, hoặc dict ánh xạ ``id`` -> bản ghi, để tiện
    dùng lại khi submission được lưu ở dạng JSON thuần thay vì JSONL.

    Đầu ra: dict ánh xạ ``id`` (chuỗi) sang bản ghi gốc (dict). Nếu file
    có hai dòng trùng ``id`` thì dòng sau ghi đè dòng trước; đây là hành
    vi cố ý để submission sửa sau thắng sửa trước, và vì ``id`` câu hỏi
    trong dữ liệu thật là duy nhất nên trường hợp này thực tế không xảy
    ra.

    Trường hợp biên: dòng trống được bỏ qua; bản ghi không phải dict hoặc
    thiếu khóa ``id`` sẽ gây ``ValueError`` kèm số dòng, thay vì bị lặng
    lẽ bỏ qua — vì bỏ qua lặng lẽ có thể làm ``total`` chấm bị thiếu mà
    người dùng không biết.
    """
    text = Path(path).read_text(encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        # File rỗng: trả về dict rỗng, không crash, để bên gọi tự thấy
        # total = 0 trong báo cáo.
        return {}
    records: list = []
    parsed_whole = False
    try:
        # Thử đọc toàn bộ file như một tài liệu JSON duy nhất trước (dạng
        # danh sách, bản ghi đơn, hoặc dict id -> bản ghi). Cách này an
        # toàn với JSONL vì json.loads toàn bộ file JSONL sẽ lỗi "Extra
        # data" và rơi xuống nhánh JSONL bên dưới, chứ không nhận diện
        # nhầm dòng đầu tiên thành tài liệu JSON đơn.
        data = json.loads(stripped)
        parsed_whole = True
    except json.JSONDecodeError:
        parsed_whole = False
    if parsed_whole:
        if isinstance(data, list):
            records = data
        elif isinstance(data, dict) and "id" in data:
            records = [data]
        elif isinstance(data, dict):
            for key, value in data.items():
                if not isinstance(value, dict):
                    raise ValueError(
                        f"Bản ghi của id {key!r} trong {path} không phải dict"
                    )
                record = dict(value)
                record.setdefault("id", key)
                records.append(record)
        else:
            raise ValueError(f"File JSON không hợp lệ: {path}")
    else:
        # JSONL: mỗi dòng một bản ghi.
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Dòng {line_no} trong {path} không phải JSON hợp lệ: {exc}"
                ) from exc
    result: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict) or "id" not in record:
            raise ValueError(f"Bản ghi thiếu khóa 'id' trong file {path}: {record!r}")
        result[str(record["id"])] = record
    return result


def load_answers(answers_path: str | Path) -> dict[str, dict]:
    """Nạp đáp án tham chiếu từ file JSONL.

    Đầu vào: đường dẫn tới file đáp án (ví dụ
    ``train/answers.jsonl``), mỗi dòng có ``id``, ``type``, ``answer`` và
    ``alias``.

    Đầu ra: dict ánh xạ ``id`` câu hỏi sang bản ghi đáp án nguyên vẹn
    (không sửa ``answer`` hay ``alias``), để các hàm chấm dùng trực tiếp.

    Vì sao giữ nguyên bản ghi thay vì chỉ giữ ``answer``: luật chấm cần
    cả ``type`` và ``alias`` của tham chiếu, nên trả về bản ghi đầy đủ là
    tiện và ít lỗi nhất.
    """
    return _load_jsonl_records(answers_path)


def load_submission(submission_path: str | Path) -> dict[str, dict]:
    """Nạp submission cần chấm từ file JSONL.

    Đầu vào / đầu ra: giống :func:`load_answers`, nhưng file này là bản
    submission do agent hoặc mô hình sinh ra. Hàm tách riêng với
    ``load_answers`` dù cài đặt giống nhau, để về sau hai định dạng có
    thể khác nhau (ví dụ submission thêm trường thời gian suy luận) mà
    không phải sửa code chỗ gọi.

    Lưu ý: submission thiếu một ``id`` nào đó là hợp lệ — câu đó sẽ được
    chấm là ``abstain`` khi gọi :func:`score_submission`, chứ không phải
    lỗi nạp file.
    """
    return _load_jsonl_records(submission_path)


# ---------------------------------------------------------------------------
# Chuẩn hoá và so sánh giá trị
# ---------------------------------------------------------------------------


def normalize_string_answer(value: Any) -> str:
    """Chuẩn hoá đáp án dạng chuỗi theo đúng luật chấm của ban tổ chức.

    Đầu vào: giá trị bất kỳ; chỉ chuỗi mới được chuẩn hoá có nghĩa, các
    giá trị khác được chuyển thành chuỗi bằng ``str()`` trước (hàm này
    chủ yếu dùng cho đáp án string và tên cột của table, nên đầu vào
    thực tế luôn là chuỗi; chuyển đổi này chỉ để hàm không crash khi gặp
    giá trị lạ).

    Các bước chuẩn hoá, theo đúng thứ tự trong luật chấm:

    1. Unicode NFC (gộp các ký tự tổ hợp thành dạng dựng sẵn).
    2. Thường hoá (lowercase).
    3. Mọi ký tự không phải chữ, số, khoảng trắng đều thành khoảng trắng.
       Lưu ý: dấu tiếng Việt được GIỮ (chữ có dấu vẫn là chữ), và ký tự
       ``_`` cũng thành khoảng trắng vì ``_`` không phải chữ/số.
    4. Xoá khoảng trắng đầu/cuối và gộp các khoảng trắng liên tiếp thành
       một khoảng trắng duy nhất.

    Đầu ra: chuỗi đã chuẩn hoá. Ví dụ ``"Sting dâu 330ml"`` thành
    ``"sting dâu 330ml"``, còn ``"Cà phê sữa (M)"`` thành ``"cà phê sữa m"``.

    Trường hợp biên: ``None`` hoặc chuỗi rỗng cho ra chuỗi rỗng ``""``.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = unicodedata.normalize("NFC", text).lower()
    # str.isalnum() đúng với chữ có dấu tiếng Việt (ví dụ 'â', 'đ' đều là
    # chữ), nên cách này giữ dấu mà vẫn loại bỏ dấu câu, ký tự đặc biệt.
    text = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in text)
    return re.sub(r"\s+", " ", text).strip()


def _is_number(value: Any) -> bool:
    """Kiểm tra một giá trị có phải số dùng được cho luật number/percent không.

    Trả ``True`` khi giá trị là ``int`` hoặc ``float`` (không phải NaN hay
    vô cực). Trả ``False`` cho mọi thứ khác, ĐẶC BIỆT là:

    - ``bool``: trong Python ``bool`` là lớp con của ``int`` (``True == 1``),
      nên phải loại riêng, nếu không đáp án boolean ``true`` sẽ bị chấm
      nhầm thành number 1.
    - ``str``: kể cả chuỗi trông giống số như ``"89,526,400"`` hay
      ``"48250000"`` cũng là sai format và phải bị tính wrong, không được
      parse lại. Đây chính là điểm mà :func:`make_broken_submission` kiểm
      tra.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    return False


def _to_decimal(value: Any) -> Decimal | None:
    """Chuyển một số hợp lệ thành ``Decimal`` để so sánh chính xác.

    Vì sao dùng ``Decimal`` thay vì so sánh float trực tiếp: số tiền VND
    có thể lớn tới hàng tỉ, vượt quá độ chính xác nguyên của float, và
    các số thập phân như 0.1 không biểu diễn chính xác trong hệ nhị phân.
    Chuyển qua ``str(value)`` rồi sang ``Decimal`` giữ đúng giá trị thập
    phân mà người viết đáp án thấy.

    Trả ``None`` khi giá trị không phải số hợp lệ (xem :func:`_is_number`)
    hoặc không chuyển được sang ``Decimal``.
    """
    if not _is_number(value):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _numbers_equal(reference: Any, submission: Any) -> bool:
    """So sánh hai số theo luật number: khớp tuyệt đối.

    Cả hai đều phải là số hợp lệ (không phải chuỗi, không phải bool) và
    có giá trị thập phân bằng nhau chính xác. Ví dụ 100 và 100.0 được coi
    là bằng nhau (cùng một giá trị), nhưng 100 và 100.5 thì không.

    Trường hợp biên: nếu một trong hai không phải số thì trả ``False``
    (tức là wrong), không raise lỗi.
    """
    ref = _to_decimal(reference)
    sub = _to_decimal(submission)
    if ref is None or sub is None:
        return False
    return ref == sub


def _percents_equal(reference: Any, submission: Any) -> bool:
    """So sánh hai số theo luật percent: làm tròn 2 chữ số thập phân rồi khớp.

    Cả hai đều phải là số hợp lệ (không phải chuỗi, không phải bool).
    Mỗi bên được làm tròn tới 2 chữ số thập phân (làm tròn nửa lên,
    ``ROUND_HALF_UP`` — tức 2.125 thành 2.13, giống cách con người làm
    tròn) rồi so sánh bằng nhau.

    Dấu bắt buộc phải đúng: vì so sánh sau làm tròn vẫn giữ dấu, nên
    -12.45 và 12.45 không bao giờ bằng nhau. Giá trị 0.124 (tức tỉ lệ dạng
    thập phân thay vì phần trăm) làm tròn thành 0.12, khác 12.4, nên cũng
    bị tính wrong — đây là một trong các lỗi format mà submission broken
    cố tình tạo ra.

    Lưu ý: luật chỉ nói "làm tròn 2 chữ số", không nói làm tròn theo kiểu
    ngân hàng hay nửa lên; với dữ liệu thật (đáp án tham chiếu vốn đã
    làm tròn 2 chữ số) hai cách làm tròn cho cùng kết quả trừ trường hợp
    biên ở đúng chữ số thứ ba bằng 5, vốn hiếm và không xuất hiện trong
    đáp án train đã kiểm tra.
    """
    ref = _to_decimal(reference)
    sub = _to_decimal(submission)
    if ref is None or sub is None:
        return False
    quantum = Decimal("0.01")
    return ref.quantize(quantum, rounding=ROUND_HALF_UP) == sub.quantize(
        quantum, rounding=ROUND_HALF_UP
    )


def _single_string_match(submission_value: Any, reference_value: Any, alias: Any) -> bool:
    """Kiểm tra một đáp án chuỗi có khớp tham chiếu hoặc alias nào không.

    Đầu vào:

    - ``submission_value``: giá trị submission đưa ra; phải là chuỗi, nếu
      là số hay kiểu khác thì luôn là wrong (không tự chuyển số thành
      chuỗi để so sánh, vì luật string yêu cầu đáp án là tên).
    - ``reference_value``: đáp án chính dạng chuỗi.
    - ``alias``: danh sách các dạng chấp nhận được thêm (ví dụ mã sản phẩm
      ``SP0003`` cho tên ``"Cà phê sữa (M)"``); có thể là ``None`` hay
      rỗng, khi đó chỉ đáp án chính được chấp nhận. Các phần tử không
      phải chuỗi trong alias được bỏ qua.

    Đầu ra: ``True`` khi chuỗi đã chuẩn hoá của submission bằng chuỗi đã
    chuẩn hoá của đáp án chính hoặc của bất kỳ alias nào. Việc chuẩn hoá
    dùng :func:`normalize_string_answer` nên giữ dấu tiếng Việt và coi
    dấu câu/khoảng trắng thừa là như nhau.

    Trường hợp biên: submission là chuỗi rỗng chỉ khớp khi tham chiếu
    (sau chuẩn hoá) cũng rỗng — thực tế không xảy ra với dữ liệu thật.
    """
    if not isinstance(submission_value, str):
        return False
    normalized_sub = normalize_string_answer(submission_value)
    candidates = []
    if isinstance(reference_value, str):
        candidates.append(normalize_string_answer(reference_value))
    if isinstance(alias, (list, tuple)):
        for form in alias:
            if isinstance(form, str):
                candidates.append(normalize_string_answer(form))
    return normalized_sub in candidates


def _flatten_alias_forms(value: Any) -> list[str]:
    """Trải phẳng một cấu trúc alias lồng nhau thành danh sách chuỗi.

    Alias của table có thể lồng nhau (ví dụ mỗi dòng là một danh sách các
    mã). Hàm này duyệt đệ quy và thu thập mọi chuỗi lá, để khi chấm một
    ô tên trong dòng đó, mọi mã của dòng đều được coi là dạng chấp nhận
    được của ô tên đó. Đây là cách xử lý thực dụng cho cấu trúc alias
    table trong dữ liệu thật, nơi mỗi dòng chỉ có một ô tên nên việc
    "mã thuộc về dòng nào" là đủ, không cần biết mã thuộc về cột nào.
    """
    forms: list[str] = []
    if isinstance(value, str):
        forms.append(value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            forms.extend(_flatten_alias_forms(item))
    return forms


def _cell_matches(sub_value: Any, ref_value: Any, alias_forms: list[str]) -> bool:
    """Kiểm tra một ô trong list/table có khớp với ô tham chiếu không.

    Quy tắc chọn theo kiểu của ô tham chiếu (vì submission có thể đưa sai
    kiểu, và kiểu tham chiếu mới là chuẩn):

    - Ô tham chiếu là chuỗi: dùng luật string (chuẩn hoá + alias của dòng).
    - Ô tham chiếu là bool: khớp tuyệt đối, submission phải là bool bằng
      đúng giá trị đó.
    - Ô tham chiếu là số: dùng luật number (khớp tuyệt đối, submission
      phải là số, không phải chuỗi).
    - Ô tham chiếu là ``None`` hoặc kiểu khác: chỉ khớp khi hai giá trị
      bằng nhau chính xác.

    ``alias_forms`` chỉ có ý nghĩa với ô chuỗi; với các kiểu khác nó được
    bỏ qua.
    """
    if isinstance(ref_value, bool):
        return isinstance(sub_value, bool) and sub_value == ref_value
    if isinstance(ref_value, str):
        return _single_string_match(sub_value, ref_value, alias_forms)
    if isinstance(ref_value, (int, float)):
        return _numbers_equal(ref_value, sub_value)
    return sub_value == ref_value


def _rows_match_as_set(
    ref_rows: list, sub_rows: list, ref_alias_rows: list | None
) -> bool:
    """Kiểm tra hai danh sách dòng của table có khớp nhau dưới dạng tập hợp không.

    Đầu vào: ``ref_rows`` và ``sub_rows`` là danh sách các dòng (mỗi dòng
    là danh sách các ô, đã được sắp xếp cột về cùng thứ tự tham chiếu
    trước khi gọi hàm này). ``ref_alias_rows`` là alias theo từng dòng
    tham chiếu (có thể ``None``/rỗng).

    Đầu ra: ``True`` khi mỗi dòng submission khớp với đúng một dòng tham
    chiếu khác nhau (ghép đôi một-một) và ngược lại, tức là hai tập dòng
    bằng nhau. Thứ tự dòng không quan trọng — đây chính là "dòng theo
    tập hợp" trong luật chấm.

    Cách làm: ghép đôi bằng thuật toán tìm kiếm theo chiều sâu (DFS) trên
    đồ thị hai phía dòng submission - dòng tham chiếu, vì một dòng
    submission có thể khớp nhiều dòng tham chiếu (ví dụ khi alias làm hai
    tên trở nên tương đương) và ghép tham lam có thể chọn sai. Table
    trong dữ liệu thật rất nhỏ (vài dòng) nên DFS là đủ nhanh và chính
    xác hơn ghép tham lam.

    Trường hợp biên: số dòng khác nhau thì trả ``False`` ngay (kể cả khi
    mọi dòng submission đều xuất hiện trong tham chiếu, vì thiếu dòng
    cũng là sai). Dòng rỗng hay dòng có độ dài khác dòng tham chiếu cũng
    không khớp.
    """
    if len(ref_rows) != len(sub_rows):
        return False
    # Với mỗi dòng submission, liệt kê các dòng tham chiếu mà nó khớp.
    candidates: list[list[int]] = []
    for sub_row in sub_rows:
        matched_refs: list[int] = []
        for ref_idx, ref_row in enumerate(ref_rows):
            if not isinstance(sub_row, (list, tuple)) or not isinstance(
                ref_row, (list, tuple)
            ):
                continue
            if len(sub_row) != len(ref_row):
                continue
            alias_forms: list[str] = []
            if ref_alias_rows is not None and ref_idx < len(ref_alias_rows):
                alias_forms = _flatten_alias_forms(ref_alias_rows[ref_idx])
            if all(
                _cell_matches(sub_cell, ref_cell, alias_forms)
                for sub_cell, ref_cell in zip(sub_row, ref_row)
            ):
                matched_refs.append(ref_idx)
        candidates.append(matched_refs)
    # Ghép đôi một-một bằng DFS (thuật toán Kuhn).
    match_ref_to_sub: dict[int, int] = {}

    def _try_match(sub_idx: int, visited: set[int]) -> bool:
        """Thử ghép dòng submission thứ ``sub_idx`` với một dòng tham chiếu còn trống.

        Dùng thuật toán Kuhn: duyệt các dòng tham chiếu ứng viên, nếu dòng
        đó đã có chủ thì thử chuyển chủ cũ sang dòng khác; thành công thì
        ghi nhận ghép đôi và trả ``True``.
        """
        for ref_idx in candidates[sub_idx]:
            if ref_idx in visited:
                continue
            visited.add(ref_idx)
            if ref_idx not in match_ref_to_sub or _try_match(
                match_ref_to_sub[ref_idx], visited
            ):
                match_ref_to_sub[ref_idx] = sub_idx
                return True
        return False

    for sub_idx in range(len(sub_rows)):
        if not _try_match(sub_idx, set()):
            return False
    return len(match_ref_to_sub) == len(ref_rows)


def _table_matches(reference: dict, submission: dict) -> bool:
    """Kiểm tra đáp án table có khớp với table tham chiếu không.

    Đầu vào: hai bản ghi đáp án (đã biết cùng type ``table``); giá trị
    ``answer`` của mỗi bên phải là dict có ``columns`` (danh sách tên
    cột) và ``rows`` (danh sách các dòng).

    Luật khớp cột, theo đúng hợp đồng:

    1. Trước hết so tên cột theo tên chuẩn hoá (dùng
       :func:`normalize_string_answer` cho cả hai bên): nếu tập tên cột
       chuẩn hoá của hai bên bằng nhau và không trùng lặp thì sắp xếp
       lại cột của submission theo đúng thứ tự cột tham chiếu rồi mới so
       dòng. Nhờ vậy submission đưa cột theo thứ tự khác vẫn đúng.
    2. Nếu tên cột không khớp nhau (tập tên khác nhau) thì chuyển sang
       khớp theo vị trí: cột thứ i của submission ứng với cột thứ i của
       tham chiếu. Khi đó số cột của hai bên phải bằng nhau, nếu không
       thì sai ngay.

    Sau khi cột đã về cùng thứ tự, các dòng được so dưới dạng tập hợp
    (xem :func:`_rows_match_as_set`): ô tên theo luật string (kèm alias
    của dòng), ô số theo luật number.

    Trường hợp biên: ``answer`` không phải dict, thiếu ``columns``/``rows``,
    hoặc ``columns``/``rows`` không phải danh sách thì đều là wrong.
    """
    ref_answer = reference.get("answer")
    sub_answer = submission.get("answer")
    if not isinstance(ref_answer, dict) or not isinstance(sub_answer, dict):
        return False
    ref_columns = ref_answer.get("columns")
    sub_columns = sub_answer.get("columns")
    ref_rows = ref_answer.get("rows")
    sub_rows = sub_answer.get("rows")
    if (
        not isinstance(ref_columns, list)
        or not isinstance(sub_columns, list)
        or not isinstance(ref_rows, list)
        or not isinstance(sub_rows, list)
    ):
        return False
    ref_norm = [normalize_string_answer(c) for c in ref_columns]
    sub_norm = [normalize_string_answer(c) for c in sub_columns]
    if len(ref_norm) == len(sub_norm) and sorted(ref_norm) == sorted(sub_norm):
        # Khớp theo tên: tìm vị trí của mỗi cột tham chiếu trong submission.
        # Vì hai tập đã bằng nhau nên mỗi tên đều tìm được đúng một lần
        # (trừ trường hợp tên cột trùng nhau sau chuẩn hoá — khi đó rơi
        # xuống nhánh vị trí ở dưới sẽ an toàn hơn, nên kiểm tra trùng).
        if len(set(ref_norm)) == len(ref_norm) and len(set(sub_norm)) == len(sub_norm):
            order = [sub_norm.index(name) for name in ref_norm]
            reordered_sub_rows = []
            for row in sub_rows:
                if not isinstance(row, (list, tuple)) or len(row) != len(sub_columns):
                    return False
                reordered_sub_rows.append([row[i] for i in order])
            ref_alias = reference.get("alias")
            return _rows_match_as_set(
                ref_rows,
                reordered_sub_rows,
                ref_alias if isinstance(ref_alias, list) else None,
            )
    # Khớp theo vị trí (dự phòng khi tên cột không khớp).
    if len(sub_columns) != len(ref_columns):
        return False
    for row in sub_rows:
        if not isinstance(row, (list, tuple)) or len(row) != len(sub_columns):
            return False
    ref_alias = reference.get("alias")
    return _rows_match_as_set(
        ref_rows,
        list(sub_rows),
        ref_alias if isinstance(ref_alias, list) else None,
    )


# ---------------------------------------------------------------------------
# Chấm từng câu
# ---------------------------------------------------------------------------


def answers_match(reference: dict, submission: dict) -> str:
    """Chấm một câu hỏi, trả về ``'correct'``, ``'wrong'`` hoặc ``'abstain'``.

    Đầu vào:

    - ``reference``: bản ghi đáp án tham chiếu (dict có ``type``,
      ``answer``, ``alias``). Nếu ``reference`` không phải dict hoặc
      thiếu ``type`` thì không có căn cứ để chấm đúng — hàm trả ``wrong``
      trừ khi submission là abstain (xem dưới).
    - ``submission``: bản ghi submission cho cùng câu hỏi. Submission
      thiếu hẳn (``None`` hoặc không phải dict) hoặc có
      ``"type": "abstain"`` thì luôn là ``abstain``, bất kể tham chiếu là
      gì — kể cả khi tham chiếu cũng là ``unanswerable`` (chọn không trả
      lời vẫn là abstain, không phải correct).

    Thứ tự các quy tắc (thứ tự này quan trọng, vì một câu có thể vi phạm
    nhiều quy tắc cùng lúc):

    1. Submission thiếu hoặc type ``abstain`` -> ``abstain``.
    2. Type của submission khác type tham chiếu -> ``wrong``. Quy tắc này
       bao hàm cả trường hợp ``unanswerable``: đáp án cho một câu hỏi
       unanswerable mà tham chiếu không phải unanswerable là wrong, và
       ngược lại, submission kiểu ``unanswerable`` cho câu hỏi có đáp án
       thật cũng là wrong.
    3. Cả hai đều là ``unanswerable`` -> ``correct`` (đúng khi và chỉ khi
       tham chiếu cũng unanswerable; giá trị ``answer`` lúc này thường là
       ``None`` và không được so sánh).
    4. Tùy type tham chiếu mà so sánh giá trị:

       - ``number``: khớp tuyệt đối, submission phải là số (chuỗi số, kể
         cả chuỗi có dấu phẩy, đều là wrong).
       - ``percent``: làm tròn mỗi bên tới 2 chữ số thập phân rồi khớp;
         sai dấu là sai.
       - ``string``: chuẩn hoá bằng :func:`normalize_string_answer` rồi
         so bằng nhau, chấp nhận bất kỳ alias nào của tham chiếu.
       - ``date``: khớp tuyệt đối dạng chuỗi (ví dụ ``"2025-09"`` cho câu
         hỏi tháng, ``"2026-08-15"`` cho câu hỏi ngày). Hàm không tự
         chuyển đổi định dạng ngày, vì luật ghi "exact match".
       - ``boolean``: khớp tuyệt đối, submission phải là bool đúng giá trị.
       - ``list``: cùng độ dài và khớp từng vị trí theo thứ tự; phần tử
         chuỗi theo luật string (kèm alias của đúng vị trí đó), phần tử
         số theo luật number, phần tử bool theo luật boolean.
       - ``table``: xem :func:`_table_matches` (cột theo tên rồi vị trí,
         dòng theo tập hợp).
       - Type tham chiếu lạ, không nằm trong danh sách trên: chỉ khớp khi
         giá trị hai bên bằng nhau chính xác. Đây là đường dự phòng để
         hàm không crash khi test set tương lai thêm type mới; nó thiên
         về chặt (dễ wrong) hơn là đoán.

    Đầu ra: một trong ba chuỗi ``'correct'``, ``'wrong'``, ``'abstain'``.
    """
    # Quy tắc 1: thiếu submission hoặc chủ động abstain.
    if not isinstance(submission, dict):
        return "abstain"
    sub_type = submission.get("type")
    if sub_type is None or sub_type == "abstain":
        return "abstain"
    # Không có tham chiếu hợp lệ thì không thể chấm đúng được.
    if not isinstance(reference, dict):
        return "wrong"
    ref_type = reference.get("type")
    if ref_type is None:
        return "wrong"
    # Quy tắc 2: khác type là wrong, không cần xem giá trị.
    if sub_type != ref_type:
        return "wrong"
    # Quy tắc 3: unanswerable đúng khi và chỉ khi tham chiếu cũng vậy;
    # tới đây hai type đã bằng nhau nên chỉ cần cả hai là unanswerable.
    if ref_type == "unanswerable":
        return "correct"
    ref_answer = reference.get("answer")
    sub_answer = submission.get("answer")
    ref_alias = reference.get("alias")
    if ref_type == "number":
        return "correct" if _numbers_equal(ref_answer, sub_answer) else "wrong"
    if ref_type == "percent":
        return "correct" if _percents_equal(ref_answer, sub_answer) else "wrong"
    if ref_type == "string":
        return (
            "correct"
            if _single_string_match(sub_answer, ref_answer, ref_alias)
            else "wrong"
        )
    if ref_type == "date":
        # Khớp tuyệt đối dạng chuỗi, chỉ bỏ khoảng trắng đầu/cuối (do
        # khoảng trắng thừa là lỗi trình bày, không phải lỗi nội dung).
        # Không chuẩn hoá định dạng (ví dụ không đổi 2025-9 thành 2025-09).
        if (
            isinstance(sub_answer, str)
            and isinstance(ref_answer, str)
            and sub_answer.strip() == ref_answer.strip()
        ):
            return "correct"
        return "wrong"
    if ref_type == "boolean":
        if isinstance(sub_answer, bool) and sub_answer == ref_answer:
            return "correct"
        return "wrong"
    if ref_type == "list":
        if not isinstance(sub_answer, list) or not isinstance(ref_answer, list):
            return "wrong"
        if len(sub_answer) != len(ref_answer):
            return "wrong"
        # Alias của list: danh sách theo từng vị trí; vị trí i có thể là
        # danh sách các dạng chấp nhận được của phần tử i.
        alias_rows = ref_alias if isinstance(ref_alias, list) else []
        for idx, (sub_item, ref_item) in enumerate(zip(sub_answer, ref_answer)):
            item_alias: list[str] = []
            if idx < len(alias_rows):
                item_alias = _flatten_alias_forms(alias_rows[idx])
            if not _cell_matches(sub_item, ref_item, item_alias):
                return "wrong"
        return "correct"
    if ref_type == "table":
        return "correct" if _table_matches(reference, submission) else "wrong"
    # Type lạ: dự phòng chặt — chỉ bằng nhau chính xác mới correct.
    return "correct" if sub_answer == ref_answer else "wrong"


# ---------------------------------------------------------------------------
# Chấm toàn bộ submission
# ---------------------------------------------------------------------------


def _empty_group() -> dict:
    """Tạo một nhóm thống kê rỗng cho per_type / per_business.

    Mỗi nhóm có cùng 5 khóa với :class:`ScoreReport`: tổng số, số đúng,
    số sai, số abstain và PA riêng của nhóm. PA ban đầu là 0.0 và sẽ được
    tính lại sau khi đã đếm xong (khi total bằng 0 thì giữ 0.0).
    """
    return {"total": 0, "correct": 0, "wrong": 0, "abstain": 0, "pa": 0.0}


def _finalize_group(group: dict) -> None:
    """Tính PA cho một nhóm thống kê sau khi đã đếm xong.

    PA = (correct - wrong) / total; khi total bằng 0 thì giữ 0.0 để tránh
    chia cho 0. Hàm sửa trực tiếp dict truyền vào.
    """
    total = group["total"]
    if total > 0:
        group["pa"] = (group["correct"] - group["wrong"]) / total
    else:
        group["pa"] = 0.0


def score_submission(
    submission: dict[str, dict],
    answers: dict[str, dict],
    questions: dict[str, dict] | None = None,
) -> ScoreReport:
    """Chấm toàn bộ submission và trả về báo cáo tổng hợp.

    Đầu vào:

    - ``submission``: dict ánh xạ ``id`` câu hỏi sang bản ghi submission
      (kết quả của :func:`load_submission`). Câu hỏi nào không có trong
      dict này được tính là abstain.
    - ``answers``: dict ánh xạ ``id`` sang bản ghi đáp án tham chiếu
      (kết quả của :func:`load_answers`). Chỉ các câu trong ``answers``
      mới được chấm; submission thừa câu ngoài ``answers`` bị bỏ qua.
    - ``questions``: dict ánh xạ ``id`` sang bản ghi câu hỏi (có
      ``business_id``), dùng để chia ``per_business``. Tham số này tùy
      chọn: nếu ``None`` hoặc một câu không tìm được ``business_id`` thì
      câu đó vào nhóm ``'unknown'``. Vì lý do tiện dụng, hàm cũng chấp
      nhận ``questions`` ở dạng danh sách các bản ghi câu hỏi (sẽ tự
      chuyển thành dict theo ``id``).

    Đầu ra: :class:`ScoreReport` với tổng số, số correct/wrong/abstain,
    PA toàn bộ, thống kê theo từng type (lấy type từ tham chiếu) và theo
    từng shop, cùng ``holdout_pa`` tính riêng trên
    :data:`HOLDOUT_BUSINESSES` (``None`` nếu không có câu holdout nào
    xác định được).

    Trường hợp biên: ``answers`` rỗng thì báo cáo có total bằng 0, pa
    bằng 0.0 và holdout_pa bằng ``None`` — không raise lỗi, để bên gọi
    tự nhận ra file đáp án rỗng từ total.
    """
    if submission is None:
        submission = {}
    if answers is None:
        answers = {}
    # Chấp nhận questions ở dạng danh sách để tiện gọi từ code test.
    questions_by_id: dict[str, dict] = {}
    if isinstance(questions, list):
        for record in questions:
            if isinstance(record, dict) and "id" in record:
                questions_by_id[str(record["id"])] = record
    elif isinstance(questions, dict):
        questions_by_id = questions

    per_type: dict[str, dict] = {}
    per_business: dict[str, dict] = {}
    correct = 0
    wrong = 0
    abstain = 0
    for qid, reference in answers.items():
        result = answers_match(reference, submission.get(qid))
        if result == "correct":
            correct += 1
        elif result == "wrong":
            wrong += 1
        else:
            abstain += 1
        ref_type = (
            reference.get("type")
            if isinstance(reference, dict) and reference.get("type") is not None
            else "unknown"
        )
        group = per_type.setdefault(ref_type, _empty_group())
        group["total"] += 1
        group[result] += 1
        question = questions_by_id.get(qid)
        business_id = "unknown"
        if isinstance(question, dict):
            raw_biz = question.get("business_id")
            if isinstance(raw_biz, str) and raw_biz:
                business_id = raw_biz
        biz_group = per_business.setdefault(business_id, _empty_group())
        biz_group["total"] += 1
        biz_group[result] += 1
    total = len(answers)
    pa = (correct - wrong) / total if total > 0 else 0.0
    for group in per_type.values():
        _finalize_group(group)
    for group in per_business.values():
        _finalize_group(group)
    holdout_correct = 0
    holdout_wrong = 0
    holdout_total = 0
    for business_id, group in per_business.items():
        if business_id in HOLDOUT_BUSINESSES:
            holdout_total += group["total"]
            holdout_correct += group["correct"]
            holdout_wrong += group["wrong"]
    holdout_pa: float | None = None
    if holdout_total > 0:
        holdout_pa = (holdout_correct - holdout_wrong) / holdout_total
    return ScoreReport(
        total=total,
        correct=correct,
        wrong=wrong,
        abstain=abstain,
        pa=pa,
        per_type=per_type,
        per_business=per_business,
        holdout_pa=holdout_pa,
    )


# ---------------------------------------------------------------------------
# Các submission mẫu dùng để kiểm thử khung chấm
# ---------------------------------------------------------------------------


def make_perfect_submission(answers: dict[str, dict]) -> dict[str, dict]:
    """Tạo submission hoàn hảo bằng cách sao chép toàn bộ đáp án tham chiếu.

    Đầu vào: dict đáp án tham chiếu (kết quả của :func:`load_answers`).

    Đầu ra: dict submission mới, mỗi ``id`` ánh xạ tới một bản sao sâu
    (deepcopy) của bản ghi tham chiếu, gồm đủ ``type``, ``answer`` và
    ``alias``. Dùng deepcopy để việc sửa submission sau đó (ví dụ trong
    :func:`make_broken_submission`) không làm hỏng đáp án gốc.

    Mục đích: kiểm thử khung chấm — submission này khi chấm phải cho
    PA bằng 1.0, vì mọi câu đều đúng. Nếu không bằng 1.0 thì khung chấm
    có lỗi, chứ không phải đáp án sai.
    """
    return {qid: deepcopy(record) for qid, record in answers.items()}


def make_abstain_submission(answers: dict[str, dict]) -> dict[str, dict]:
    """Tạo submission toàn abstain (không trả lời câu nào).

    Đầu vào: dict đáp án tham chiếu; chỉ dùng các ``id`` của nó để biết
    cần abstain những câu nào.

    Đầu ra: dict mà mỗi ``id`` ánh xạ tới
    ``{"type": "abstain", "answer": None}``. Khi chấm, mọi câu đều là
    abstain nên PA phải bằng 0.0 — đây là mốc baseline: một hệ thống
    không dám trả lời vẫn tốt hơn một hệ thống trả lời sai nhiều (PA âm).

    Lưu ý: submission rỗng ``{}`` cũng cho cùng kết quả (thiếu đáp án là
    abstain), nhưng tạo tường minh từng mục abstain giúp phân biệt được
    "chủ động abstain" với "quên nộp câu đó" khi gỡ lỗi.
    """
    return {qid: {"type": "abstain", "answer": None} for qid in answers}


def make_broken_submission(answers: dict[str, dict]) -> dict[str, dict]:
    """Tạo submission cố tình sai format ở ít nhất một câu để kiểm thử.

    Đầu vào: dict đáp án tham chiếu.

    Đầu ra: bắt đầu từ submission hoàn hảo, sau đó phá hỏng ít nhất một
    đáp án theo hai kiểu lỗi format điển hình mà mô hình hay mắc:

    1. Câu ``number`` đầu tiên gặp được: đổi số thành chuỗi có dấu phẩy
       phân tách hàng nghìn (ví dụ 89526400 thành ``"89,526,400"``).
       Chuỗi này phải bị tính wrong vì luật number yêu cầu giá trị số,
       không chấp nhận chuỗi — khung chấm không được tự parse lại chuỗi.
    2. Câu ``percent`` đầu tiên gặp được: đổi giá trị phần trăm thành
       dạng tỉ lệ thập phân (ví dụ 12.4 thành 0.124). Sau làm tròn 2 chữ
       số, 0.12 khác 12.4 nên phải bị tính wrong.

    Cả hai kiểu phá đều được áp dụng nếu tìm được đủ hai loại câu; nếu
    dữ liệu chỉ có một trong hai loại thì chỉ phá loại đó, nhưng luôn
    đảm bảo ít nhất một câu bị phá. Trường hợp biên: nếu không tìm được
    câu number hay percent nào (ví dụ tập đáp án chỉ toàn string), hàm
    phá câu đầu tiên bằng cách đổi type của nó thành một type khác (ví
    dụ string thành number) để câu đó chắc chắn bị tính wrong do khác
    type. Nhờ vậy submission broken khi chấm luôn có ít nhất 1 wrong và
    PA nhỏ hơn 1.0, bất kể thành phần type của tập đáp án.
    """
    broken = make_perfect_submission(answers)
    broke_number = False
    broke_percent = False
    for qid, record in answers.items():
        if not isinstance(record, dict):
            continue
        if record.get("type") == "number" and not broke_number:
            value = record.get("answer")
            if _is_number(value):
                # Định dạng có dấu phẩy: 89526400 -> "89,526,400".
                broken[qid] = {
                    "type": "number",
                    "answer": f"{value:,}",
                    "alias": deepcopy(record.get("alias", [])),
                }
                broke_number = True
        elif record.get("type") == "percent" and not broke_percent:
            value = record.get("answer")
            if _is_number(value):
                # Dạng tỉ lệ thập phân thay vì phần trăm: 12.4 -> 0.124.
                broken[qid] = {
                    "type": "percent",
                    "answer": value / 100,
                    "alias": deepcopy(record.get("alias", [])),
                }
                broke_percent = True
        if broke_number and broke_percent:
            break
    if not broke_number and not broke_percent and broken:
        # Dự phòng: không có câu number/percent nào để phá.
        first_qid = next(iter(broken))
        record = broken[first_qid]
        wrong_type = "number" if record.get("type") != "number" else "string"
        broken[first_qid] = {
            "type": wrong_type,
            "answer": record.get("answer"),
            "alias": deepcopy(record.get("alias", [])),
        }
    return broken
