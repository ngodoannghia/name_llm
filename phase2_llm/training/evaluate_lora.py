#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Đánh giá LoRA theo cổng quyết định của MASTER_PLAN mục 8.

Mục đích của script này
------------------------
So sánh một submission (được tạo ra khi chạy pipeline có parser LoRA)
với baseline phase2, và chỉ kết luận KEEP khi cổng quyết định thỏa —
không bao giờ kết luận KEEP chỉ vì PA trên train tăng, và không bao
giờ bịa số PA của một model chưa chạy.

Baseline lấy từ ``phase2/reports/PHASE2_SUMMARY.json`` (đã kiểm chứng,
không gõ tay số khác):

- PA tổng baseline: ``-0.212`` (581 đúng / 1.217 sai / 1.202 abstain
  trên 3.000 câu).
- PA holdout baseline: ``-0.3564189189189189`` trên 592 câu của 5 shop
  holdout: biz_124, biz_123, biz_106, biz_105, biz_117.
- PA tính theo công thức ``(đúng - sai) / tổng``; abstain tính 0.

Cổng quyết định KEEP (cả ba điều kiện phải cùng đúng):

1. PA holdout của LoRA CAO HƠN baseline holdout (tăng thật, không
   tính bằng: bằng baseline cũng là REJECT).
2. PA tổng của LoRA KHÔNG GIẢM NGHIÊM TRỌNG so với baseline tổng:
   ``tổng >= baseline_tổng - 0.05`` (ngưỡng dung sai ghi rõ trong
   config; giảm quá 0.05 là REJECT dù holdout có tăng).
3. Submission không suy biến: tỉ lệ trả lời > 0 (một submission toàn
   abstain cho PA 0 — cao hơn baseline âm — nhưng không chứng minh
   được LoRA hiểu câu hỏi tốt hơn, nên vẫn là REJECT kèm lý do rõ).

Ngược lại (chỉ cần một điều kiện sai) kết luận là REJECT.

Cách chấm
---------
Ưu tiên dùng hàm chấm ``answers_match`` của phase1 (import qua
``sys.path``, cùng logic với khung đánh giá phase2). Nếu phase1 không
nạp được thì dùng hàm chấm dự phòng :func:`_fallback_match` trong file
này (chỉ là xấp xỉ: so sánh số tuyệt đối, percent làm tròn 2 chữ số,
chuỗi chuẩn hoá NFC/thường hoá; list/table so sánh JSON sau chuẩn
hoá). Khi dùng dự phòng, báo cáo ghi rõ ``scoring_method`` để người
đọc biết số này không hoàn toàn tương đương khung chấm phase1.

Chế độ dry-run
--------------
``--dry-run`` KHÔNG yêu cầu weights LoRA hay model nào cả, vì script
này chỉ chấm một file submission đã có:

- Nếu caller đưa ``--submission`` thì chấm đúng file đó.
- Nếu không đưa, script thử chấm submission baseline của phase2
  (``phase2/output/submission.jsonl``) để minh hoạ cổng quyết định:
  kết quả kỳ vọng là REJECT (bằng baseline, không tăng), và báo cáo
  ghi rõ đây là minh hoạ bằng submission phase2, KHÔNG phải kết quả
  LoRA.
- Nếu cả hai đều không có thì chấm một submission toàn abstain tổng
  hợp trong bộ nhớ, ghi rõ là demo suy biến, và cổng sẽ REJECT vì
  điều kiện chống suy biến.

Cách chạy::

    # Dry-run minh hoạ cổng (không cần weights LoRA):
    python3 evaluate_lora.py --dry-run
    # Chấm submission thật của pipeline LoRA:
    python3 evaluate_lora.py --submission ./submission_lora.jsonl \
        --output ./lora_eval_report.json
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

# Nạp phase1 bằng đường dẫn tương đối từ file này (training/ ->
# datamate là parents[2]), không hard-code đường dẫn tuyệt đối.
_DATAMATE_DIR = Path(__file__).resolve().parents[2]
_PHASE1_SRC = _DATAMATE_DIR / "phase1" / "src"
if str(_PHASE1_SRC) not in sys.path:
    sys.path.insert(0, str(_PHASE1_SRC))

try:
    from evaluate import answers_match as _phase1_answers_match
    _PHASE1_SCORING = True
except Exception:  # pragma: no cover
    _phase1_answers_match = None
    _PHASE1_SCORING = False

__all__ = [
    "BASELINE_TOTAL_PA",
    "BASELINE_HOLDOUT_PA",
    "HOLDOUT_BUSINESSES",
    "TOTAL_TOLERANCE",
    "load_jsonl_by_id",
    "normalize_string",
    "fallback_match",
    "score_submission_pa",
    "decide_keep_or_reject",
    "evaluate",
    "main",
]

# Baseline phase2, trích từ phase2/reports/PHASE2_SUMMARY.json.
# Không sửa các số này nếu không có báo cáo phase2 mới thay thế.
BASELINE_TOTAL_PA = -0.212
BASELINE_HOLDOUT_PA = -0.3564189189189189
HOLDOUT_BUSINESSES = ["biz_124", "biz_123", "biz_106", "biz_105", "biz_117"]
TOTAL_TOLERANCE = 0.05


def load_jsonl_by_id(path: Path) -> dict[str, dict]:
    """Đọc file JSONL thành dict theo ``id``, bỏ dòng trống.

    Đầu vào:
        path: đường dẫn file JSONL (questions, answers hoặc submission).

    Đầu ra:
        Dict ánh xạ ``id`` sang bản ghi. Bản ghi thiếu ``id`` thì bị
        bỏ qua và không được tính vào mẫu số PA, vì không xác định
        được nó thuộc câu nào; số dòng bị bỏ sẽ thể hiện qua chênh
        lệch giữa số dòng file và số bản ghi trả về nếu caller kiểm
        tra.
    """
    records: dict[str, dict] = {}
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if isinstance(row, dict) and "id" in row:
                records[str(row["id"])] = row
    return records


def normalize_string(value) -> str:
    """Chuẩn hoá một đáp án chuỗi để so sánh (dự phòng khi thiếu phase1).

    Đầu vào:
        value: giá trị bất kỳ, thường là chuỗi.

    Đầu ra:
        Chuỗi đã chuẩn hoá NFC, thường hoá, thay ký tự không phải
        chữ/số/khoảng trắng bằng khoảng trắng rồi gộp trắng — cùng quy
        ước khớp chuỗi của khung chấm phase2. Giá trị không phải chuỗi
        được chuyển thành chuỗi trước khi chuẩn hoá.
    """
    text = value if isinstance(value, str) else str(value)
    text = unicodedata.normalize("NFC", text).lower()
    return " ".join(
        "".join(ch if ch.isalnum() else " " for ch in text).split())


def _is_number(value) -> bool:
    """Kiểm tra một giá trị có phải số chấm được không (trừ bool)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _round2(value) -> Decimal | None:
    """Làm tròn một số về 2 chữ số thập phân, kiểu nửa lên."""
    try:
        return Decimal(str(value)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return None


def fallback_match(reference: dict, submission: dict | None) -> str:
    """Chấm một câu bằng logic dự phòng khi không nạp được phase1.

    Đầu vào:
        reference: bản ghi đáp án tham chiếu (``type``, ``answer``,
            ``alias``).
        submission: bản ghi submission cho cùng câu, hoặc None khi
            thiếu dòng (tính là abstain).

    Đầu ra:
        Một trong ``"correct"``, ``"wrong"``, ``"abstain"``, theo đúng
        thứ tự ưu tiên của khung chấm: thiếu/abstain trước, khác type
        là sai, cả hai unanswerable là đúng, rồi so sánh giá trị theo
        type. Đây là xấp xỉ, đặc biệt ở list/table/alias phức tạp;
        vì vậy caller phải ghi rõ đã dùng dự phòng, và số PA từ dự
        phòng chỉ dùng để kiểm tra cổng ở mức minh hoạ, không thay thế
        khung chấm phase1 cho quyết định KEEP cuối cùng.
    """
    if submission is None or not isinstance(submission, dict):
        return "abstain"
    if submission.get("type") == "abstain":
        return "abstain"
    if not isinstance(reference, dict):
        return "wrong"
    if submission.get("type") != reference.get("type"):
        return "wrong"
    ref_type = reference.get("type")
    if ref_type == "unanswerable":
        return "correct"
    ref_answer = reference.get("answer")
    sub_answer = submission.get("answer")
    if ref_type == "number":
        if not _is_number(sub_answer) or not _is_number(ref_answer):
            return "wrong"
        try:
            return "correct" if Decimal(str(sub_answer)) == Decimal(
                str(ref_answer)) else "wrong"
        except Exception:
            return "wrong"
    if ref_type == "percent":
        if not _is_number(sub_answer) or not _is_number(ref_answer):
            return "wrong"
        return "correct" if _round2(sub_answer) == _round2(
            ref_answer) else "wrong"
    if ref_type == "string":
        forms = {normalize_string(ref_answer)}
        for alias in reference.get("alias") or []:
            if isinstance(alias, list):
                forms |= {normalize_string(a) for a in alias}
            else:
                forms.add(normalize_string(alias))
        return "correct" if normalize_string(sub_answer) in forms \
            else "wrong"
    if ref_type == "date":
        return "correct" if str(sub_answer) == str(ref_answer) else "wrong"
    if ref_type == "boolean":
        return "correct" if sub_answer is ref_answer else "wrong"
    if ref_type == "list":
        if not isinstance(sub_answer, list) \
                or not isinstance(ref_answer, list):
            return "wrong"
        if len(sub_answer) != len(ref_answer):
            return "wrong"
        for sub_item, ref_item in zip(sub_answer, ref_answer):
            if isinstance(ref_item, str):
                if normalize_string(sub_item) != normalize_string(ref_item):
                    return "wrong"
            elif sub_item != ref_item:
                return "wrong"
        return "correct"
    if ref_type == "table":
        return "correct" if sub_answer == ref_answer else "wrong"
    return "wrong"


def score_submission_pa(submission: dict[str, dict],
                        answers: dict[str, dict],
                        questions: dict[str, dict]) -> dict:
    """Chấm toàn bộ submission và tính PA tổng, PA holdout, PA theo shop.

    Đầu vào:
        submission: dict id -> bản ghi submission (câu thiếu được tính
            abstain, giống khung chấm phase1).
        answers: dict id -> đáp án tham chiếu (chỉ các câu có trong
            answers mới được chấm).
        questions: dict id -> câu hỏi (dùng để lấy business_id; câu
            không tìm được business_id thì vào nhóm ``unknown`` và
            không tính vào holdout).

    Đầu ra:
        Dict gồm: tổng số, số đúng/sai/abstain, PA tổng, PA holdout,
        số câu holdout, tỉ lệ trả lời, PA theo từng shop, và
        ``scoring_method`` (``phase1_evaluate`` hoặc
        ``fallback_approximate``). PA tính bằng ``(đúng - sai)/tổng``;
        nhóm không có câu nào thì PA là None thay vì 0, để không ai
        đọc nhầm "không có dữ liệu" thành "điểm 0".
    """
    matcher = _phase1_answers_match if _PHASE1_SCORING else fallback_match
    total = correct = wrong = abstain = 0
    hold_total = hold_correct = hold_wrong = 0
    answered = 0
    per_business: dict[str, dict] = {}

    def _group(business_id: str) -> dict:
        """Lấy hoặc tạo nhóm thống kê cho một shop."""
        return per_business.setdefault(
            business_id, {"total": 0, "correct": 0, "wrong": 0,
                          "abstain": 0, "pa": None})

    for question_id, reference in answers.items():
        business_id = str((questions.get(question_id) or {}).get(
            "business_id", "unknown"))
        group = _group(business_id)
        verdict = matcher(reference, submission.get(question_id))
        total += 1
        group["total"] += 1
        if verdict == "correct":
            correct += 1
            group["correct"] += 1
            answered += 1
        elif verdict == "wrong":
            wrong += 1
            group["wrong"] += 1
            answered += 1
        else:
            abstain += 1
            group["abstain"] += 1
        if business_id in HOLDOUT_BUSINESSES:
            hold_total += 1
            if verdict == "correct":
                hold_correct += 1
            elif verdict == "wrong":
                hold_wrong += 1
    for group in per_business.values():
        if group["total"] > 0:
            group["pa"] = (group["correct"] - group["wrong"]) \
                / group["total"]
    return {
        "total": total,
        "correct": correct,
        "wrong": wrong,
        "abstain": abstain,
        "answered": answered,
        "answered_rate": (answered / total) if total else 0.0,
        "pa_total": ((correct - wrong) / total) if total else None,
        "holdout_total": hold_total,
        "holdout_correct": hold_correct,
        "holdout_wrong": hold_wrong,
        "pa_holdout": ((hold_correct - hold_wrong) / hold_total)
        if hold_total else None,
        "per_business": per_business,
        "scoring_method": "phase1_evaluate" if _PHASE1_SCORING
        else "fallback_approximate",
    }


def decide_keep_or_reject(pa_holdout, pa_total,
                          answered_rate: float) -> dict:
    """Áp cổng quyết định KEEP/REJECT cho LoRA.

    Đầu vào:
        pa_holdout: PA holdout của LoRA (None nếu không có câu holdout
            nào — khi đó không đủ bằng chứng và phải REJECT).
        pa_total: PA tổng của LoRA (None thì cũng REJECT).
        answered_rate: tỉ lệ câu submission có trả lời (không abstain).

    Đầu ra:
        Dict ``verdict`` (``KEEP`` hoặc ``REJECT``) kèm danh sách
        ``reasons`` giải thích từng điều kiện. KEEP chỉ khi cả ba điều
        kiện ở docstring module cùng đúng; mọi trường hợp còn lại là
        REJECT, kể cả khi PA holdout tăng nhưng PA tổng giảm quá dung
        sai, hoặc submission toàn abstain.
    """
    reasons = []
    keep = True
    if pa_holdout is None:
        keep = False
        reasons.append("Không có câu holdout nào để đánh giá.")
    elif not pa_holdout > BASELINE_HOLDOUT_PA:
        keep = False
        reasons.append(
            f"PA holdout {pa_holdout:.4f} không cao hơn baseline "
            f"holdout {BASELINE_HOLDOUT_PA:.4f}.")
    else:
        reasons.append(
            f"PA holdout {pa_holdout:.4f} cao hơn baseline holdout "
            f"{BASELINE_HOLDOUT_PA:.4f}.")
    if pa_total is None:
        keep = False
        reasons.append("Không tính được PA tổng.")
    elif pa_total < BASELINE_TOTAL_PA - TOTAL_TOLERANCE:
        keep = False
        reasons.append(
            f"PA tổng {pa_total:.4f} giảm quá dung sai {TOTAL_TOLERANCE} "
            f"so với baseline tổng {BASELINE_TOTAL_PA:.4f}.")
    else:
        reasons.append(
            f"PA tổng {pa_total:.4f} không giảm quá dung sai "
            f"{TOTAL_TOLERANCE} so với baseline tổng "
            f"{BASELINE_TOTAL_PA:.4f}.")
    if not answered_rate or answered_rate <= 0:
        keep = False
        reasons.append(
            "Submission toàn abstain (tỉ lệ trả lời bằng 0): không "
            "chứng minh được LoRA cải thiện khả năng hiểu câu hỏi.")
    return {"verdict": "KEEP" if keep else "REJECT", "reasons": reasons}


def evaluate(submission: dict[str, dict], answers: dict[str, dict],
             questions: dict[str, dict], label: str) -> dict:
    """Chấm một submission và áp cổng quyết định, trả báo cáo đầy đủ.

    Đầu vào:
        submission, answers, questions: như :func:`score_submission_pa`.
        label: nhãn mô tả submission đang chấm (ví dụ
            ``"lora_submission"``, ``"phase2_baseline_demo"`` hay
            ``"all_abstain_demo"``) để báo cáo không bao giờ bị đọc
            nhầm thành kết quả LoRA khi nó chỉ là minh hoạ.

    Đầu ra:
        Dict báo cáo gồm: nhãn, baseline dùng để so sánh, kết quả chấm
        PA, và quyết định KEEP/REJECT kèm lý do. Hàm này không tải
        model hay weights nào cả — nó chỉ chấm file submission.
    """
    scores = score_submission_pa(submission, answers, questions)
    decision = decide_keep_or_reject(
        scores["pa_holdout"], scores["pa_total"],
        scores["answered_rate"])
    return {
        "label": label,
        "baseline": {
            "pa_total": BASELINE_TOTAL_PA,
            "pa_holdout": BASELINE_HOLDOUT_PA,
            "holdout_businesses": HOLDOUT_BUSINESSES,
            "total_tolerance": TOTAL_TOLERANCE,
            "source": "phase2/reports/PHASE2_SUMMARY.json",
        },
        "scores": scores,
        "decision": decision,
    }


def main(argv: list[str] | None = None) -> int:
    """Điểm vào dòng lệnh của script đánh giá LoRA.

    Đầu vào:
        argv: tham số dòng lệnh (None thì lấy từ sys.argv).

    Đầu ra:
        Mã thoát 0 khi chấm xong và in được quyết định (kể cả REJECT,
        vì REJECT là kết quả đánh giá hợp lệ, không phải lỗi chạy);
        1 khi không đọc được file answers/questions bắt buộc. Báo cáo
        JSON được in ra stdout và ghi ra ``--output`` nếu có.
    """
    parser = argparse.ArgumentParser(
        description="Đánh giá submission LoRA theo cổng quyết định "
        "(so với baseline phase2).")
    parser.add_argument(
        "--submission", type=Path, default=None,
        help="File submission.jsonl cần chấm. Không đưa thì dry-run "
        "dùng submission phase2 hoặc demo toàn abstain (xem --dry-run).")
    parser.add_argument(
        "--questions", type=Path,
        default=_DATAMATE_DIR / "train" / "questions.jsonl",
        help="File questions.jsonl để lấy business_id.")
    parser.add_argument(
        "--answers", type=Path,
        default=_DATAMATE_DIR / "train" / "answers.jsonl",
        help="File answers.jsonl tham chiếu.")
    parser.add_argument(
        "--output", type=Path, default=None,
        help="File JSON ghi báo cáo đánh giá (tùy chọn).")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Chế độ minh hoạ cổng quyết định, không yêu cầu weights "
        "LoRA: nếu không có --submission thì dùng submission phase2 "
        "nếu có, nếu không thì dùng demo toàn abstain.")
    args = parser.parse_args(argv)

    try:
        answers = load_jsonl_by_id(args.answers)
        questions = load_jsonl_by_id(args.questions)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"LỖI: không đọc được answers/questions: {exc}",
              file=sys.stderr)
        return 1
    if not answers:
        print("LỖI: file answers rỗng, không thể tính PA.",
              file=sys.stderr)
        return 1

    submission: dict[str, dict] = {}
    label = "lora_submission"
    if args.submission is not None:
        submission = load_jsonl_by_id(args.submission)
        label = "lora_submission"
    elif args.dry_run:
        phase2_submission = _DATAMATE_DIR / "phase2" / "output" \
            / "submission.jsonl"
        if phase2_submission.is_file():
            submission = load_jsonl_by_id(phase2_submission)
            label = ("phase2_baseline_demo_NOT_lora: submission này "
                     "là của pipeline phase2, chỉ dùng để minh hoạ "
                     "cổng quyết định, không phải kết quả LoRA")
        else:
            submission = {}
            label = ("all_abstain_demo_NOT_lora: submission tổng hợp "
                     "toàn abstain trong bộ nhớ, chỉ để minh hoạ cổng")
    else:
        print("LỖI: cần --submission, hoặc dùng --dry-run để minh hoạ "
              "cổng quyết định.", file=sys.stderr)
        return 1

    report = evaluate(submission, answers, questions, label)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    verdict = report["decision"]["verdict"]
    print(f"Kết luận cổng quyết định: {verdict}")
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print(f"Đã ghi báo cáo vào {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
