# -*- coding: utf-8 -*-
"""Nạp rulebook và lịch, cộng các hàm giải nghĩa khoảng thời gian.

Mục đích của module này là biến hai file của split đang chạy
(``rulebook.md`` và ``calendar.csv``) thành luật tường minh mà code tính
toán có thể dùng, thay vì ghi luật cứng trong code. Vì test set có thể
mang rulebook mới (đổi định nghĩa doanh thu, đổi ngày bắt đầu tuần,
đổi cách tính "tháng này", đổi cửa sổ Tết...), mọi luật ở đây đều được
trích lúc chạy từ văn bản thật bằng regex/từ khóa trên văn bản đã
chuẩn hoá. Luật nào không tìm thấy bằng chứng trong file thì giữ
``None`` và ghi vào ``unparsed_notes`` — thà thiếu luật (để tầng trên
abstain) còn hơn bịa luật cũ áp lên split mới.

Module này chỉ dùng thư viện chuẩn Python, không gọi mạng/API và
không đọc gì ngoài hai file được truyền vào.
"""

from __future__ import annotations

import calendar as _calendar_mod
import csv
import datetime
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Chuẩn hoá văn bản (nội bộ, không phụ thuộc module của agent khác)
# ---------------------------------------------------------------------------

def _strip_diacritics(text: str) -> str:
    """Bỏ dấu tiếng Việt để so khớp không phụ thuộc cách gõ có dấu/không dấu.

    Ví dụ: "không huỷ" -> "khong huy". Chỉ dùng cho việc *tìm kiếm* luật,
    còn trích dẫn lưu trong ``rules`` vẫn giữ nguyên văn có dấu.
    """
    decomposed = unicodedata.normalize("NFD", text)
    without_marks = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    # Hai ký tự đ/Đ không tách được bằng NFD nên thay tay.
    return without_marks.replace("đ", "d").replace("Đ", "D")


def _normalize_key(text: str) -> str:
    """Chuẩn hoá để so sánh khái niệm: thường hoá, bỏ dấu, chỉ giữ chữ/số.

    Ví dụ: "Tháng này!" -> "thang nay". Trả ``""`` nếu đầu vào rỗng/None.
    """
    if not text:
        return ""
    lowered = unicodedata.normalize("NFC", str(text)).lower()
    no_marks = _strip_diacritics(lowered)
    cleaned = re.sub(r"[^a-z0-9]+", " ", no_marks)
    return re.sub(r"\s+", " ", cleaned).strip()


def _clean_quote(text: str) -> str:
    """Rút gọn một trích dẫn để lưu vào ``rules``: bỏ dấu bảng/dấu đậm, gộp khoảng trắng.

    Việc này chỉ làm gọn cách trình bày, không đổi nội dung luật: các từ
    và con số trong câu vẫn giữ nguyên thứ tự và ý nghĩa.
    """
    if not text:
        return ""
    cleaned = text.replace("**", "").replace("|", " ").replace("`", "")
    cleaned = unicodedata.normalize("NFC", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _split_evidence_lines(raw_text: str) -> list[str]:
    """Tách văn bản rulebook thành các dòng bằng chứng để quét luật.

    Mỗi dòng (kể cả một dòng bảng markdown) được coi là một đơn vị bằng
    chứng độc lập: luật về doanh thu nằm cùng dòng với từ "doanh thu"
    thì mới được tính cho doanh thu, tránh nhầm với luật công nợ ở dòng
    khác cũng nhắc "không huỷ". Dòng trống và dòng phân cách bảng
    (``|---|---|``) bị bỏ qua.
    """
    lines: list[str] = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if set(line) <= set("|-: "):
            # Dòng phân cách của bảng markdown, không chứa luật.
            continue
        lines.append(line)
    return lines


# ---------------------------------------------------------------------------
# Mẫu từ khóa (đã chuẩn hoá, không dấu) dùng để trích luật
# ---------------------------------------------------------------------------
# Lưu ý quan trọng: các mẫu dưới đây là mẫu NGÔN NGỮ chung (cách người ta
# thường viết "loại đơn huỷ", "tuần bắt đầu thứ Hai"...), không phải nội
# dung luật của split train. Cùng một mẫu có thể trích ra True ở rulebook
# này và False ở rulebook khác, hoặc không trích được gì nếu file mới
# viết theo cách khác hẳn — khi đó trường tương ứng là None.

# Doanh thu loại đơn huỷ / đơn trả hàng: mẫu "loại trừ" và mẫu "gồm cả".
_CANCEL_EXCLUDE_PATTERNS = (
    "khong huy",
    "loai don huy",
    "tru don huy",
    "bo don huy",
    "don huy khong tinh",
    "khong tinh don huy",
    "don huy khong duoc tinh",
)
_CANCEL_INCLUDE_PATTERNS = (
    "gom ca don huy",
    "tinh ca don huy",
    "ke ca don huy",
    "bao gom don huy",
    "don huy van tinh",
    "ca don huy duoc tinh",
)
_RETURN_EXCLUDE_PATTERNS = (
    "khong tra hang",
    "loai don tra",
    "tru don tra",
    "bo don tra",
    "don tra hang khong tinh",
    "khong tinh don tra",
    "don tra hang khong duoc tinh",
    "bo ca don",  # Chỉ có nghĩa khi cùng dòng đã nhắc "tra hang" (kiểm tra riêng).
)
_RETURN_INCLUDE_PATTERNS = (
    "gom ca don tra",
    "tinh ca don tra",
    "ke ca don tra",
    "bao gom don tra",
    "don tra hang van tinh",
    "ca don tra duoc tinh",
)

# Doanh thu chỉ tính đơn hoàn thành thì ngầm loại cả huỷ lẫn trả hàng.
_COMPLETED_ONLY_PATTERNS = (
    "chi tinh don hoan thanh",
    "chi don hoan thanh",
    "chi tinh cac don hoan thanh",
)


def _match_any(normalized: str, patterns: tuple[str, ...]) -> str | None:
    """Trả về mẫu đầu tiên xuất hiện trong văn bản đã chuẩn hoá, hoặc None."""
    for pattern in patterns:
        if pattern in normalized:
            return pattern
    return None


# Mẫu regex linh hoạt cho cách viết "gồm cả đơn huỷ và đơn trả hàng":
# cụm "gồm cả" và tên loại đơn có thể cách nhau vài từ ("gồm cả đơn huỷ
# và đơn trả hàng"), nên chỉ tìm chuỗi con cố định sẽ bỏ sót.
_CANCEL_INCLUDE_RE = re.compile(r"(gom ca|tinh ca|bao gom)[^|\n]{0,50}?huy")
_RETURN_INCLUDE_RE = re.compile(r"(gom ca|tinh ca|bao gom)[^|\n]{0,50}?tra hang")


def _extract_revenue_exclusion(
    lines: list[str],
) -> tuple[bool | None, bool | None, str | None, str | None]:
    """Trích hai luật doanh thu từ các dòng nhắc đến doanh thu.

    Đầu vào: các dòng bằng chứng của rulebook.
    Đầu ra: ``(loại đơn huỷ?, loại đơn trả?, dòng bằng chứng huỷ, dòng bằng chứng trả)``,
    mỗi giá trị là ``True`` (loại khỏi doanh thu), ``False`` (vẫn tính) hoặc
    ``None`` (không tìm thấy bằng chứng).

    Vì sao chỉ xét dòng có từ "doanh thu" (hoặc "đơn tính vào"): cùng một
    cụm "không huỷ" còn xuất hiện trong luật công nợ ("đơn trả hàng vẫn
    tính vào nợ") với ý nghĩa ngược lại, nên nếu quét toàn văn bản sẽ
    nhầm luật nợ thành luật doanh thu. Dòng bảng "Đơn tính vào doanh thu"
    của rulebook train cũng lọt vào diện này nhờ cụm "đơn tính vào".

    Trường hợp biên:
    - Một dòng vừa có mẫu loại trừ vừa có mẫu gồm cả (viết mâu thuẫn):
      ưu tiên mẫu gồm cả là False? Không — mâu thuẫn thì không kết luận
      được, trả None để tầng trên abstain thay vì đoán. Ngoại lệ duy nhất:
      cụm "bỏ cả đơn" đi kèm "trả hàng" được hiểu là loại đơn trả hàng,
      vì đó là cách viết "đơn trả hàng thì bỏ cả đơn" trong rulebook train.
    - Dòng chỉ nói "chỉ tính đơn hoàn thành" mà không nhắc huỷ/trả: suy ra
      cả hai đều bị loại, vì đơn hoàn thành là trạng thái còn lại sau khi
      bỏ huỷ, trả và đang giao... — thực ra đang giao vẫn tính theo luật
      train, nên suy luận này chỉ đúng khi dòng đó thực sự định nghĩa
      doanh thu. Vì vậy mẫu này chỉ áp dụng khi dòng có từ "doanh thu".
    """
    cancelled: bool | None = None
    returned: bool | None = None
    cancelled_evidence: str | None = None
    returned_evidence: str | None = None

    for line in lines:
        norm = _normalize_key(line)
        is_revenue_line = ("doanh thu" in norm) or ("don tinh vao" in norm)
        if not is_revenue_line:
            continue

        # --- Đơn huỷ ---
        if cancelled is None:
            exclude = _match_any(norm, _CANCEL_EXCLUDE_PATTERNS)
            include = _match_any(norm, _CANCEL_INCLUDE_PATTERNS) or (
                "gom ca don huy" if _CANCEL_INCLUDE_RE.search(norm) else None
            )
            if exclude and include:
                # Mâu thuẫn trong cùng một dòng: không kết luận.
                pass
            elif exclude:
                cancelled = True
                cancelled_evidence = _clean_quote(line)
            elif include:
                cancelled = False
                cancelled_evidence = _clean_quote(line)
            elif _match_any(norm, _COMPLETED_ONLY_PATTERNS):
                cancelled = True
                cancelled_evidence = _clean_quote(line)

        # --- Đơn trả hàng ---
        if returned is None:
            exclude = _match_any(norm, _RETURN_EXCLUDE_PATTERNS)
            include = _match_any(norm, _RETURN_INCLUDE_PATTERNS) or (
                "gom ca don tra" if _RETURN_INCLUDE_RE.search(norm) else None
            )
            if exclude == "bo ca don" and "tra hang" not in norm:
                # "bỏ cả đơn" đứng một mình không đủ nghĩa: bỏ qua mẫu này.
                exclude = None
            if exclude and include:
                pass
            elif exclude:
                returned = True
                returned_evidence = _clean_quote(line)
            elif include:
                returned = False
                returned_evidence = _clean_quote(line)
            elif _match_any(norm, _COMPLETED_ONLY_PATTERNS):
                returned = True
                returned_evidence = _clean_quote(line)

        if cancelled is not None and returned is not None:
            break

    return cancelled, returned, cancelled_evidence, returned_evidence


def _extract_week_start(lines: list[str]) -> tuple[bool | None, str | None]:
    """Trích luật "tuần bắt đầu thứ Hai" từ dòng định nghĩa tuần.

    Trả ``True`` nếu tuần bắt đầu thứ Hai, ``False`` nếu bắt đầu ngày khác
    (Chủ nhật, thứ Bảy...), ``None`` nếu không tìm thấy dòng nói tuần bắt
    đầu vào ngày nào. Chỉ xét dòng vừa nhắc "tuần" vừa nhắc "bắt đầu",
    để câu "tuần trước: thứ Hai đến Chủ nhật" (mô tả khoảng, không phải
    định nghĩa điểm bắt đầu) không bị nhầm thành bằng chứng.
    """
    week_start_re = re.compile(
        r"bat dau(?:\s+\w+){0,3}?\s*(thu hai|chu nhat|thu bay|thu ba|thu tu|thu nam|thu sau)"
    )
    for line in lines:
        norm = _normalize_key(line)
        if "tuan" not in norm or "bat dau" not in norm:
            continue
        match = week_start_re.search(norm)
        if match:
            return match.group(1) == "thu hai", _clean_quote(line)
    return None, None


def _extract_month_to_date_cut(lines: list[str]) -> tuple[bool | None, str | None]:
    """Trích luật "tháng này cắt ở hôm nay (as_of_date)".

    Trả ``True`` nếu "tháng này" được định nghĩa là từ ngày 1 đến hết hôm
    nay, ``False`` nếu "tháng này" là cả tháng, ``None`` nếu không tìm
    thấy định nghĩa "tháng này". Chỉ xét dòng nhắc "tháng này" (không xét
    dòng "Tháng M/Y" vì đó là luật khác: tháng có năm thì luôn cả tháng).

    Vì sao phải xét vị trí, không chỉ "dòng có chứa cụm": một dòng khác
    ("...không phải kỳ tháng này") cũng chứa "tháng này" và chứa cụm
    "đến hôm nay" ở mệnh đề trước đó ("tính đến hôm nay"), nhưng nó không
    định nghĩa "tháng này" chút nào. Vì vậy chỉ xét phần văn bản ĐẰNG SAU
    cụm "tháng này" (trong bảng thì xét riêng ô định nghĩa của dòng có ô
    đầu là "Tháng này"), và trong phần đó cụm nào xuất hiện trước thì
    thắng: "hết hôm nay" -> True, "cả tháng"/"hết tháng" -> False.
    """
    cut_phrases = ("het hom nay", "den het hom nay", "den hom nay", "toi hom nay")
    full_phrases = ("ca thang", "het thang", "cuoi thang")

    def _decide(scope: str) -> bool | None:
        """Quyết định trong một đoạn văn bản: cụm cắt/cả tháng nào đến trước."""
        cut_at = min(
            (scope.find(p) for p in cut_phrases if scope.find(p) >= 0),
            default=-1,
        )
        full_at = min(
            (scope.find(p) for p in full_phrases if scope.find(p) >= 0),
            default=-1,
        )
        if cut_at >= 0 and (full_at < 0 or cut_at < full_at):
            return True
        if full_at >= 0:
            return False
        return None

    for line in lines:
        norm = _normalize_key(line)
        if "thang nay" not in norm:
            continue
        if line.startswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) >= 2 and _normalize_key(_clean_quote(cells[0])) == "thang nay":
                # Dòng bảng "Tháng này": chỉ xét ô định nghĩa, không xét ô đầu.
                decision = _decide(_normalize_key(_clean_quote(cells[1])))
                if decision is not None:
                    return decision, _clean_quote(line)
                continue
        # Dòng văn xuôi: chỉ xét tối đa 120 ký tự sau cụm "tháng này".
        scope = norm[norm.find("thang nay") + len("thang nay"):][:120]
        decision = _decide(scope)
        if decision is not None:
            return decision, _clean_quote(line)
    return None, None


def _extract_tet_rule(lines: list[str]) -> str | None:
    """Trích đoạn văn bản định nghĩa cửa sổ Tết, nếu có.

    Tết được nhận diện khi cùng một dòng nhắc "tết" và nhắc mốc 23 tháng
    Chạp hoặc mùng 10 tháng Giêng (hai mốc mà ``tet_range`` dùng để tra
    lịch). Dòng không có mốc ngày cụ thể (chỉ nói chung "dịp Tết") thì
    không đủ để tra lịch nên không lấy làm ``tet_rule_text``. Trả None
    khi rulebook mới không định nghĩa Tết.
    """
    for line in lines:
        norm = _normalize_key(line)
        if "tet" not in norm:
            continue
        has_chap_milestone = "23" in norm and "chap" in norm
        has_gieng_milestone = "10" in norm and "gieng" in norm
        if has_chap_milestone or has_gieng_milestone:
            return _clean_quote(line)
    return None


def _extract_table_rules(lines: list[str]) -> dict[str, str]:
    """Trích các luật dạng bảng ``| Thuật ngữ | Định nghĩa |`` thành dict.

    Khóa là thuật ngữ ở ô đầu (giữ nguyên cách viết có dấu, chỉ bỏ dấu
    định dạng), giá trị là định nghĩa rút gọn. Đây là cách ghi lại các
    luật thời gian khác (N ngày gần nhất, quý, từ đầu năm...) mà không
    cần hard-code từng luật: rulebook mới viết bảng khác thì dict sẽ
    chứa bảng mới đó. Dòng tiêu đề bảng ("Luật | Mặc định") và dòng
    phân cách đã bị loại ở ``_split_evidence_lines``; ở đây bỏ thêm dòng
    mà ô đầu là tiêu đề cột.

    Trường hợp biên: thuật ngữ trùng nhau ở hai mục khác nhau thì giữ
    định nghĩa đầu tiên và thêm hậu tố "(2)", "(3)"... để không mất luật.
    Định nghĩa dài quá 500 ký tự thì cắt ngắn (vẫn đủ đọc) để dict gọn.
    """
    rules: dict[str, str] = {}
    header_terms = {"luat", "cach noi", "tu", "cach ghi", "cach viet"}
    for line in lines:
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        term = _clean_quote(cells[0])
        definition = _clean_quote(cells[1])
        if not term or not definition:
            continue
        if _normalize_key(term) in header_terms:
            continue
        key = term
        suffix = 2
        while key in rules:
            key = f"{term} ({suffix})"
            suffix += 1
        rules[key] = definition[:500]
    return rules


# ---------------------------------------------------------------------------
# Dataclass kết quả
# ---------------------------------------------------------------------------

@dataclass
class Rulebook:
    """Luật đã trích được từ một file rulebook cụ thể lúc chạy.

    Các trường boolean là ``None`` khi file không chứa bằng chứng rõ ràng
    cho luật đó — đó là tín hiệu để tầng trên abstain, không phải giá trị
    mặc định. ``rules`` giữ trích dẫn rút gọn để con người kiểm chứng lại
    được từng luật thay vì tin mù vào giá trị boolean.
    """

    source_path: str
    calendar_path: str
    raw_text: str
    rules: dict[str, str]  # khóa luật -> trích dẫn/văn bản luật (nguyên văn, rút gọn)
    revenue_excludes_cancelled: bool | None
    revenue_excludes_returned: bool | None
    week_starts_monday: bool | None
    month_to_date_cut_at_as_of: bool | None
    tet_rule_text: str | None
    unparsed_notes: list[str]  # luật nào không trích chắc chắn được thì ghi rõ, KHÔNG bịa


@dataclass
class Calendar:
    """Toàn bộ dòng của ``calendar.csv``, giữ nguyên tên cột và giá trị gốc.

    Giữ giá trị dạng chuỗi như trong file (không ép kiểu sớm) để hàm tra
    cứu tự quyết định cách parse; nếu file mới đổi tên cột thì
    ``tet_range`` sẽ trả None thay vì crash, và đó là hành vi đúng khi
    lịch không đủ mốc.
    """

    rows: list[dict]  # mỗi dòng calendar.csv, khóa là tên cột gốc


# ---------------------------------------------------------------------------
# Nạp dữ liệu
# ---------------------------------------------------------------------------

def load_calendar(calendar_path: str | Path) -> Calendar:
    """Đọc file ``calendar.csv`` thật thành đối tượng ``Calendar``.

    Đầu vào: đường dẫn tới ``calendar.csv`` (các cột kỳ vọng là
    ``date, weekday, lunar_day, lunar_month, lunar_year, lunar_leap, holiday``,
    nhưng hàm không bắt buộc tên cột — chỉ đọc đúng những gì file có).
    Đầu ra: ``Calendar`` với ``rows`` là list các dict, khóa là tên cột gốc.
    Trường hợp biên: file rỗng (chỉ có header) thì ``rows`` là list rỗng;
    file không tồn tại thì lỗi ``FileNotFoundError`` lan ra để tầng gọi
    biết rõ là thiếu lịch, không âm thầm dùng lịch rỗng.
    """
    path = Path(calendar_path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = [dict(row) for row in reader]
    return Calendar(rows=rows)


def load_rulebook(rulebook_path: str | Path, calendar_path: str | Path) -> Rulebook:
    """Nạp rulebook lúc chạy từ file, trích luật bằng từ khóa/regex.

    Đầu vào: đường dẫn tới ``rulebook.md`` của split đang chạy và đường
    dẫn tới ``calendar.csv`` đi kèm (chỉ lưu lại để truy vết, không dùng
    để suy luật — luật phải đến từ văn bản rulebook).
    Đầu ra: ``Rulebook`` với các trường boolean đã trích được; luật nào
    không tìm thấy bằng chứng thì là ``None`` và có ghi chú trong
    ``unparsed_notes``.

    Vì sao không có giá trị mặc định True/False nào trong hàm này: nếu
    gán mặc định theo luật train thì rulebook mới thiếu luật đó sẽ vẫn
    nhận luật cũ — đúng lỗi mà yêu cầu tổng quát hoá cấm. Test
    "rulebook mới" kiểm tra chính điểm này: đổi câu chữ thì trường phải
    đổi theo, xoá luật thì trường phải về None.
    """
    rulebook_file = Path(rulebook_path)
    raw_text = rulebook_file.read_text(encoding="utf-8-sig")
    lines = _split_evidence_lines(raw_text)

    cancelled, returned, cancelled_ev, returned_ev = _extract_revenue_exclusion(lines)
    week_monday, week_ev = _extract_week_start(lines)
    month_cut, month_ev = _extract_month_to_date_cut(lines)
    tet_text = _extract_tet_rule(lines)

    rules = _extract_table_rules(lines)
    # Ghi thêm bằng chứng của 5 luật tối thiểu dưới khóa ngữ nghĩa ổn định,
    # để code dùng luật không phải mò lại trong dict bảng.
    if cancelled_ev is not None:
        rules.setdefault("doanh_thu_loai_don_huy", cancelled_ev)
    if returned_ev is not None:
        rules.setdefault("doanh_thu_loai_don_tra_hang", returned_ev)
    if week_ev is not None:
        rules.setdefault("tuan_bat_dau", week_ev)
    if month_ev is not None:
        rules.setdefault("thang_nay_cat_o_as_of", month_ev)
    if tet_text is not None:
        rules.setdefault("tet", tet_text)
    if not rules:
        # Rulebook viết dạng văn xuôi, không có bảng: giữ lại từng dòng
        # không rỗng làm bằng chứng để không mất luật, thay vì trả dict rỗng.
        for index, line in enumerate(lines, start=1):
            rules[f"dong_{index}"] = _clean_quote(line)[:500]

    unparsed_notes: list[str] = []
    if cancelled is None:
        unparsed_notes.append(
            "Không trích được luật doanh thu có loại đơn huỷ không: "
            "không tìm thấy dòng định nghĩa doanh thu nhắc đến đơn huỷ."
        )
    if returned is None:
        unparsed_notes.append(
            "Không trích được luật doanh thu có loại đơn trả hàng không: "
            "không tìm thấy dòng định nghĩa doanh thu nhắc đến đơn trả hàng."
        )
    if week_monday is None:
        unparsed_notes.append(
            "Không trích được luật tuần bắt đầu thứ Hai không: "
            "không tìm thấy dòng định nghĩa tuần bắt đầu vào ngày nào."
        )
    if month_cut is None:
        unparsed_notes.append(
            "Không trích được luật 'tháng này' có cắt ở as_of_date không: "
            "không tìm thấy dòng định nghĩa 'tháng này'."
        )
    if tet_text is None:
        unparsed_notes.append(
            "Không trích được đoạn văn bản về Tết: "
            "không tìm thấy dòng nhắc Tết kèm mốc 23 tháng Chạp hoặc mùng 10 tháng Giêng."
        )

    return Rulebook(
        source_path=str(rulebook_file),
        calendar_path=str(Path(calendar_path)),
        raw_text=raw_text,
        rules=rules,
        revenue_excludes_cancelled=cancelled,
        revenue_excludes_returned=returned,
        week_starts_monday=week_monday,
        month_to_date_cut_at_as_of=month_cut,
        tet_rule_text=tet_text,
        unparsed_notes=unparsed_notes,
    )


# ---------------------------------------------------------------------------
# Tra lịch âm – dương và các khoảng thời gian
# ---------------------------------------------------------------------------

def _parse_calendar_date(value: str | None) -> datetime.date | None:
    """Parse một ô ngày dương trong calendar.csv (dạng YYYY-MM-DD).

    Trả None nếu ô trống, sai định dạng hoặc không phải ngày thật — để
    ``tet_range`` coi mốc đó là thiếu thay vì crash cả quá trình tra cứu.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def _parse_calendar_int(value: str | None) -> int | None:
    """Parse một ô số nguyên âm lịch (lunar_day/lunar_month/lunar_year).

    Trả None nếu ô trống hoặc không phải số nguyên, vì so sánh chuỗi
    "09" với 9 sẽ sai nếu không ép kiểu thống nhất.
    """
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _find_lunar_date(
    calendar: Calendar, lunar_day: int, lunar_month: int, lunar_year: int
) -> datetime.date | None:
    """Tìm ngày dương của một ngày âm lịch trong calendar.

    Trường hợp biên — tháng nhuận làm trùng khóa âm lịch: cùng một bộ
    (ngày, tháng, năm âm) có thể xuất hiện hai lần (tháng thường và tháng
    nhuận, ví dụ tháng 6 nhuận năm 2025). Khi đó ưu tiên dòng không phải
    tháng nhuận (``lunar_leap`` là false/0/rỗng), vì mốc Tết (tháng Chạp,
    tháng Giêng) không rơi vào tháng nhuận trong thực tế và cách ưu tiên
    này tránh lấy nhầm ngày của tháng nhuận. Nếu chỉ có dòng nhuận thì
    vẫn lấy dòng đó thay vì trả None.
    """
    matches: list[tuple[bool, datetime.date]] = []
    for row in calendar.rows:
        if (
            _parse_calendar_int(row.get("lunar_day")) == lunar_day
            and _parse_calendar_int(row.get("lunar_month")) == lunar_month
            and _parse_calendar_int(row.get("lunar_year")) == lunar_year
        ):
            day = _parse_calendar_date(row.get("date"))
            if day is None:
                continue
            leap_raw = str(row.get("lunar_leap") or "").strip().lower()
            is_leap = leap_raw in ("true", "1", "yes")
            matches.append((is_leap, day))
    if not matches:
        return None
    non_leap = [day for is_leap, day in matches if not is_leap]
    return non_leap[0] if non_leap else matches[0][1]


def tet_range(lunar_year: int, calendar: Calendar) -> tuple[datetime.date, datetime.date] | None:
    """Trả khoảng Tết năm âm ``lunar_year`` theo calendar.csv.

    Đầu vào: năm âm cần tra và ``Calendar`` đã nạp.
    Đầu ra: ``(ngày dương của 23 tháng Chạp năm âm lunar_year-1,
    ngày dương của mùng 10 tháng Giêng năm âm lunar_year)`` — khoảng này
    gồm cả hai đầu mút theo định nghĩa "đến hết mùng 10". Trả None nếu
    thiếu bất kỳ mốc nào trong hai mốc, hoặc hai mốc tìm được không tạo
    thành khoảng hợp lệ (ngày cuối trước ngày đầu, tức lịch bị lỗi).

    Vì sao trừ 1 ở mốc đầu: Tết năm âm Y bắt đầu từ cuối năm âm Y-1
    (23 tháng Chạp là ngày ông Công ông Táo của năm trước), nên tra
    23/12 của năm ``lunar_year - 1``, không phải của chính ``lunar_year``.
    """
    start = _find_lunar_date(calendar, 23, 12, lunar_year - 1)
    end = _find_lunar_date(calendar, 10, 1, lunar_year)
    if start is None or end is None:
        return None
    if end < start:
        return None
    return (start, end)


def month_range(year: int, month: int) -> tuple[datetime.date, datetime.date]:
    """Trả cả tháng: từ ngày 1 đến hết ngày cuối tháng.

    Đầu vào: năm và tháng dương lịch (tháng 1–12).
    Đầu ra: ``(ngày 1, ngày cuối tháng)``; tháng 2 năm nhuận tự đúng nhờ
    ``calendar.monthrange``. Tháng ngoài 1–12 thì ``ValueError`` lan ra
    (do ``datetime.date`` báo lỗi) thay vì âm thầm trả khoảng sai.
    """
    last_day = _calendar_mod.monthrange(year, month)[1]
    return (datetime.date(year, month, 1), datetime.date(year, month, last_day))


def week_range(any_date: datetime.date) -> tuple[datetime.date, datetime.date]:
    """Trả tuần chứa ``any_date``: từ thứ Hai đến Chủ nhật của tuần đó.

    Dùng ``weekday()`` của Python (thứ Hai = 0) nên không phụ thuộc locale
    của máy. Ngày đầu tuần = lùi về thứ Hai gần nhất (chính ngày đó nếu
    đã là thứ Hai); ngày cuối = thứ Hai đó + 6 ngày (Chủ nhật).
    """
    monday = any_date - datetime.timedelta(days=any_date.weekday())
    sunday = monday + datetime.timedelta(days=6)
    return (monday, sunday)


def month_to_date_range(as_of: datetime.date) -> tuple[datetime.date, datetime.date]:
    """Trả khoảng "tháng này tính đến hôm nay": ngày 1 của tháng đến hết ``as_of``.

    Đây là khoảng month-to-date, khác ``month_range`` (cả tháng): nếu
    ``as_of`` là giữa tháng thì ngày cuối là ``as_of``, không phải cuối
    tháng. Trường hợp biên ``as_of`` là ngày 1 thì khoảng chỉ có 1 ngày.
    """
    return (datetime.date(as_of.year, as_of.month, 1), as_of)


def previous_month_same_days_range(as_of: datetime.date) -> tuple[datetime.date, datetime.date]:
    """Trả khoảng tháng trước với cùng số ngày như "tháng này".

    Đầu vào: ngày ``as_of`` (ngày "hôm nay" của câu hỏi).
    Đầu ra: ``(ngày 1 tháng trước, ngày trùng số với as_of trong tháng trước)``.
    Trường hợp biên quan trọng: tháng trước không có ngày trùng số
    (ví dụ ``as_of`` là 31/3 mà tháng trước là tháng 2 chỉ có 28 ngày) thì
    ngày cuối là hết tháng trước (28/2), không được nhảy sang tháng khác
    hay báo lỗi. Tháng 1 thì tháng trước là tháng 12 năm trước.
    """
    if as_of.month == 1:
        prev_year, prev_month = as_of.year - 1, 12
    else:
        prev_year, prev_month = as_of.year, as_of.month - 1
    prev_last_day = _calendar_mod.monthrange(prev_year, prev_month)[1]
    end_day = min(as_of.day, prev_last_day)
    return (
        datetime.date(prev_year, prev_month, 1),
        datetime.date(prev_year, prev_month, end_day),
    )


def last_n_days_range(n: int, as_of: datetime.date) -> tuple[datetime.date, datetime.date]:
    """Trả N ngày liền nhau kể cả hôm nay, ngày cuối là ``as_of``.

    Đầu vào: ``n`` là số ngày, ``as_of`` là ngày cuối ("hôm nay").
    Đầu ra: ``(as_of - (n-1) ngày, as_of)`` — ví dụ N=1 thì chỉ có hôm nay,
    N=7 thì gồm hôm nay và 6 ngày trước đó. ``n <= 0`` thì ``ValueError``,
    vì khoảng 0 hoặc âm ngày là vô nghĩa và im lặng trả khoảng rỗng sẽ
    làm code tính toán hiểu nhầm thành "không có phát sinh".
    """
    if n <= 0:
        raise ValueError(f"n phải >= 1, nhận được n={n}")
    start = as_of - datetime.timedelta(days=n - 1)
    return (start, as_of)


def snapshot_up_to(day: datetime.date) -> datetime.date:
    """Trả chính ``day``: mốc "ảnh chụp đến hết ngày" cho số còn lại.

    Hàm này tồn tại để code dùng rõ nghĩa: hỏi "còn nợ đến hết ngày X"
    thì mốc là X, không cắt kỳ thành tháng hay tuần. Nó không làm gì thêm
    vì bản thân ``day`` đã là ngày cuối của ảnh chụp; việc tính "từ đầu
    dữ liệu đến hết ngày đó" là việc của code tính toán, không phải của
    hàm này.
    """
    return day
