#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Huấn luyện LoRA cho parser DataMate (KHÔNG full finetune).

Mục đích của script này
------------------------
Huấn luyện một adapter LoRA lên trên một base model có sẵn, để model
học cách biến câu hỏi tiếng Việt thành ``ParsedQuestion`` (JSON đúng
schema của API_CONTRACT mục 2), mà không làm mất năng lực chung của
model gốc — đúng yêu cầu LoRA ở MASTER_PLAN mục 8. Việc tính đáp án
vẫn thuộc về solver bằng code; LoRA ở đây chỉ thay thế template
parser, không bao giờ được tự nhẩm số.

Trạng thái base model — đọc kỹ trước khi chạy thật
--------------------------------------------------
``config.json`` để ``base_model`` là null, nghĩa là CHƯA CHỌN model.
Đây là cố tình, không phải thiếu sót: nhiệm vụ yêu cầu model ≤ 15B
tham số, weights public trước 01/10/2026, licence cho phép dùng, và
cấm bịa tên model hay licence chưa kiểm chứng. Vì vậy script này
KHÔNG tự chọn một cái tên model nào cả:

- Chế độ ``--dry-run`` chạy được mà không cần weights, không cần
  torch/transformers/peft, chỉ kiểm tra dataset + config và ước lượng
  số bước train. Đây là chế độ duy nhất được chạy cho đến khi base
  model được chọn và kiểm chứng licence.
- Chế độ train thật sẽ DỪNG NGAY với lỗi rõ ràng nếu ``base_model``
  còn null, hoặc nếu thiếu torch/transformers/peft. Không bao giờ
  tự download weights nặng trong lúc chạy mà không có lựa chọn tường
  minh của người vận hành.

Định dạng dữ liệu huấn luyện
-----------------------------
Mỗi dòng JSONL của ``generate_verified_traces.py`` được chuyển thành
một ví dụ huấn luyện dạng văn bản::

    Prompt: "Phân tích câu hỏi sau thành ParsedQuestion (JSON)..."
            + câu hỏi + ngày as_of_date
    Completion: JSON của parsed_question_json (ensure_ascii=False)

Khi train thật, token của prompt nên được mask khỏi loss (chỉ tính
loss trên completion), để model học sinh JSON chứ không học thuộc
prompt. Ghi chú này được giữ trong code hàm :func:`train` vì bản
train tối giản ở đây chưa cài masking chi tiết cho từng kiến trúc
tokenizer — người vận hành phải rà lại bước này trước khi train thật
và ghi rõ vào log train, không được coi là đã xong.

Cách chạy::

    # Kiểm tra khô, không cần weights (luôn pass nếu dataset/config ổn):
    python3 train_lora.py --dry-run
    # Train thật (chỉ chạy được sau khi đã chọn base_model trong config):
    python3 train_lora.py --output-dir ./lora_output

Script không gọi mạng/API ngoài việc thư viện huấn luyện tự tải weights
của base model đã chọn (ở chế độ train thật); chế độ dry-run không tải
gì cả. Toàn bộ comment/docstring trong file này viết bằng tiếng Việt.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

# Nạp hàm kiểm tra schema từ generator cùng thư mục (không copy logic
# kiểm tra sang hai nơi để khỏi lệch nhau theo thời gian).
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from generate_verified_traces import validate_parsed_json
except Exception:  # pragma: no cover - dự phòng khi chạy file rời.
    validate_parsed_json = None  # type: ignore[assignment]

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "REQUIRED_DATASET_FIELDS",
    "load_config",
    "validate_config",
    "load_dataset",
    "validate_dataset",
    "build_training_text",
    "estimate_steps",
    "check_training_dependencies",
    "dry_run",
    "train",
    "main",
]

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
REQUIRED_DATASET_FIELDS = [
    "question", "normalized_question", "as_of_date", "business_id",
    "parsed_question_json", "verified", "verification_method",
    "source", "generalization_tag",
]


def load_config(config_path: Path) -> dict:
    """Đọc file cấu hình JSON của đợt train LoRA.

    Đầu vào:
        config_path: đường dẫn tới ``config.json``.

    Đầu ra:
        Dict cấu hình. File không tồn tại hoặc JSON sai thì raise lỗi
        rõ ràng, thay vì dùng cấu hình mặc định lặng lẽ rồi train sai
        siêu tham số mà không ai biết.
    """
    with open(config_path, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("config.json phải là một JSON object.")
    return config


def validate_config(config: dict) -> list[str]:
    """Kiểm tra cấu hình train, trả về danh sách cảnh báo/lỗi nhẹ.

    Đầu vào:
        config: dict cấu hình đã đọc từ file.

    Đầu ra:
        List chuỗi mô tả vấn đề. Danh sách rỗng nghĩa là cấu hình đủ
        để chạy dry-run. Riêng ``base_model`` null KHÔNG được coi là
        lỗi ở đây, vì dry-run cố tình chạy được khi chưa chọn model;
        việc chặn train thật khi chưa chọn model nằm ở :func:`train`.
        Các siêu tham số LoRA phải là số dương, nếu không thì đó là
        lỗi cấu hình thật và cần sửa trước khi train.
    """
    problems = []
    for key in ("lora_rank", "lora_alpha", "learning_rate", "epochs",
                "per_device_train_batch_size",
                "gradient_accumulation_steps", "max_seq_length"):
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{key} phải là số, hiện là: {value!r}")
        elif float(value) <= 0:
            problems.append(f"{key} phải > 0, hiện là: {value!r}")
    if config.get("base_model") is None:
        problems.append(
            "base_model chưa chọn (null): chỉ chạy được --dry-run; "
            "train thật yêu cầu model ≤15B, weights public trước "
            "01/10/2026, licence cho phép dùng (xem config.json).")
    training_data = config.get("training_data")
    if not training_data:
        problems.append("training_data chưa chỉ định file dataset JSONL.")
    return problems


def load_dataset(dataset_path: Path) -> list[dict]:
    """Đọc dataset JSONL thành list dict, bỏ dòng trống.

    Đầu vào:
        dataset_path: đường dẫn file JSONL.

    Đầu ra:
        List các dict theo thứ tự file. Dòng JSON sai sẽ raise lỗi,
        không lặng lẽ bỏ qua, vì bỏ qua lặng lẽ sẽ làm sai số bước
        ước lượng và sai thống kê verified.
    """
    rows = []
    with open(dataset_path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def validate_dataset(rows: list[dict]) -> dict:
    """Kiểm tra toàn bộ dataset theo schema của pipeline.

    Đầu vào:
        rows: list các dòng JSONL đã đọc.

    Đầu ra:
        Dict thống kê: tổng dòng, số dòng verified True/False, số
        dòng theo ``generalization_tag`` và ``source``, số dòng lỗi
        schema kèm tối đa 5 ví dụ lỗi đầu tiên (đủ để sửa, không in
        cả dataset ra log). Một dòng được tính là hợp lệ khi đủ trường
        bắt buộc và ``parsed_question_json`` qua
        :func:`validate_parsed_json` của generator. Thống kê này là
        đầu vào bắt buộc của dry-run: dataset lỗi schema thì dry-run
        vẫn in thống kê nhưng train thật phải dừng.
    """
    stats: dict = {
        "total": len(rows),
        "verified_true": 0,
        "verified_false": 0,
        "schema_errors": 0,
        "schema_error_examples": [],
        "by_generalization_tag": {},
        "by_source": {},
    }
    for row in rows:
        if row.get("verified") is True:
            stats["verified_true"] += 1
        else:
            stats["verified_false"] += 1
        tag = str(row.get("generalization_tag", ""))
        stats["by_generalization_tag"][tag] = \
            stats["by_generalization_tag"].get(tag, 0) + 1
        source = str(row.get("source", ""))
        stats["by_source"][source] = \
            stats["by_source"].get(source, 0) + 1
        error = None
        for field in REQUIRED_DATASET_FIELDS:
            if field not in row:
                error = f"thiếu trường: {field}"
                break
        if error is None and validate_parsed_json is not None:
            ok, reason = validate_parsed_json(row["parsed_question_json"])
            if not ok:
                error = f"parsed_question_json sai schema: {reason}"
        if error is not None:
            stats["schema_errors"] += 1
            if len(stats["schema_error_examples"]) < 5:
                stats["schema_error_examples"].append(error)
    return stats


def build_training_text(row: dict) -> tuple[str, str]:
    """Chuyển một dòng dataset thành cặp (prompt, completion) huấn luyện.

    Đầu vào:
        row: một dòng JSONL đúng schema của pipeline.

    Đầu ra:
        Tuple ``(prompt, completion)``. Prompt nêu rõ nhiệm vụ (phân
        tích câu hỏi thành ParsedQuestion JSON), kèm câu hỏi gốc và
        ngày ``as_of_date`` vì kỳ tương đối phụ thuộc vào ngày này.
        Completion là JSON của ``parsed_question_json`` với
        ``ensure_ascii=False`` để giữ tiếng Việt có dấu trong tên thực
        thể. Cặp này KHÔNG chứa đáp án train: đáp án chỉ dùng để kiểm
        chứng trace ở bước generator, không bao giờ xuất hiện ở đây.
    """
    prompt = (
        "Phân tích câu hỏi sau thành ParsedQuestion (JSON) theo đúng "
        "schema: measure, entity_kind, entity_key, entity_display, "
        "period_start, period_end, period_kind, period_months, "
        "ranking_k, ranking_target, ranking_metric, unit, "
        "threshold_money, threshold_op, answer_type, confidence_parse, "
        "parse_notes, normalized_text.\n"
        f"Câu hỏi: {row['question']}\n"
        f"Ngày tham chiếu (as_of_date): {row['as_of_date']}\n"
        "ParsedQuestion (JSON):"
    )
    completion = json.dumps(
        row["parsed_question_json"], ensure_ascii=False)
    return prompt, completion


def estimate_steps(num_examples: int, batch_size: int,
                   grad_accum: int, epochs: int) -> dict:
    """Ước lượng số bước train từ kích thước dataset và cấu hình.

    Đầu vào:
        num_examples: số ví dụ huấn luyện.
        batch_size: batch size trên mỗi thiết bị.
        grad_accum: số bước tích lũy gradient.
        epochs: số epoch.

    Đầu ra:
        Dict ``steps_per_epoch`` và ``total_steps``, tính bằng công
        thức ceil(n / (batch * grad_accum)) * epochs. Đây chỉ là ước
        lượng lập lịch, không phải số bước thực tế khi dataloader bỏ
        ví dụ quá dài (max_seq_length) — số thực tế sẽ thấp hơn hoặc
        bằng ước lượng này.
    """
    if num_examples <= 0 or batch_size <= 0 or grad_accum <= 0:
        return {"steps_per_epoch": 0, "total_steps": 0}
    effective_batch = batch_size * grad_accum
    per_epoch = math.ceil(num_examples / effective_batch)
    return {
        "steps_per_epoch": per_epoch,
        "total_steps": per_epoch * max(1, int(epochs)),
    }


def check_training_dependencies() -> dict:
    """Kiểm tra các thư viện nặng cần cho train thật, không import thừa.

    Đầu vào:
        Không có (chỉ kiểm tra môi trường hiện tại).

    Đầu ra:
        Dict ánh xạ tên thư viện (``torch``, ``transformers``,
        ``peft``) sang True/False theo việc import được hay không.
        Hàm này KHÔNG tải weights, KHÔNG tải model, và KHÔNG làm
        dry-run thất bại khi thiếu thư viện — dry-run cố tình pass
        khi thiếu thư viện để người vận hành vẫn kiểm tra được dataset
        và config trước khi cài môi trường nặng.
    """
    status = {}
    for module in ("torch", "transformers", "peft"):
        try:
            __import__(module)
            status[module] = True
        except Exception:
            status[module] = False
    return status


def dry_run(config: dict, dataset_path: Path) -> dict:
    """Chạy kiểm tra khô: dataset, schema, config, ước lượng bước.

    Đầu vào:
        config: dict cấu hình đã đọc.
        dataset_path: đường dẫn file dataset JSONL.

    Đầu ra:
        Dict báo cáo dry-run gồm: đường dẫn dataset, thống kê dataset
        (từ :func:`validate_dataset`), vấn đề cấu hình, ước lượng
        bước train, trạng thái thư viện nặng, và cờ ``ready_for_real``
        (luôn False khi base_model chưa chọn, dù mọi kiểm tra khác
        đều pass — vì chưa chọn model thì chưa thể train thật). Hàm
        này KHÔNG tải weights, KHÔNG khởi tạo model, KHÔNG ghi adapter
        nào cả, nên chạy được trên máy không có GPU và không có torch.
    """
    rows = load_dataset(dataset_path)
    dataset_stats = validate_dataset(rows)
    config_problems = validate_config(config)
    steps = estimate_steps(
        num_examples=dataset_stats["total"],
        batch_size=int(config.get("per_device_train_batch_size", 1)),
        grad_accum=int(config.get("gradient_accumulation_steps", 1)),
        epochs=int(config.get("epochs", 1)),
    )
    deps = check_training_dependencies()
    report = {
        "mode": "dry-run",
        "dataset_path": str(dataset_path),
        "dataset_stats": dataset_stats,
        "config_problems": config_problems,
        "estimated_steps": steps,
        "dependencies": deps,
        "trained_real_weights": False,
        "ready_for_real": bool(
            config.get("base_model")
            and dataset_stats["schema_errors"] == 0
            and dataset_stats["verified_true"] > 0
            and all(deps.values())),
    }
    return report


def train(config: dict, dataset_path: Path, output_dir: Path) -> dict:
    """Huấn luyện LoRA thật (chỉ chạy khi đủ điều kiện, nếu không thì dừng).

    Đầu vào:
        config: dict cấu hình đã đọc.
        dataset_path: đường dẫn file dataset JSONL.
        output_dir: thư mục sẽ ghi adapter LoRA.

    Đầu ra:
        Dict kết quả train (đường dẫn adapter, số ví dụ, số bước ước
        lượng). Hàm dừng ngay bằng ``RuntimeError`` khi:

        - ``base_model`` còn null (chưa chọn model, xem docstring
          module — không được tự bịa tên model để chạy tiếp);
        - thiếu torch/transformers/peft;
        - dataset có lỗi schema hoặc không có dòng verified True nào
          (train trên trace chưa kiểm chứng là vi phạm cổng quyết
          định MASTER_PLAN mục 8).

        Khi đủ điều kiện, hàm dùng ``transformers`` + ``peft`` để gắn
        adapter LoRA lên base model và train theo cấu hình. Bước mask
        token prompt khỏi loss và bước chọn ``target_modules`` theo
        kiến trúc base model đã chọn phải được rà lại trước khi chạy
        đợt train thật đầu tiên và ghi rõ vào log train; bản tối giản
        này không giấu việc đó thành "đã xong".
    """
    if not config.get("base_model"):
        raise RuntimeError(
            "Chưa chọn base_model trong config.json (hiện là null). "
            "Train thật yêu cầu model ≤15B, weights public trước "
            "01/10/2026, licence cho phép dùng, đã kiểm chứng. "
            "Hãy chọn và ghi rõ model + licence vào config rồi chạy "
            "lại; không được tự bịa tên model.")
    deps = check_training_dependencies()
    missing = [name for name, ok in deps.items() if not ok]
    if missing:
        raise RuntimeError(
            f"Thiếu thư viện huấn luyện: {', '.join(missing)}. "
            "Hãy cài torch/transformers/peft đúng phiên bản tương "
            "thích với GPU/CUDA của máy rồi chạy lại.")
    rows = load_dataset(dataset_path)
    dataset_stats = validate_dataset(rows)
    if dataset_stats["schema_errors"] > 0:
        raise RuntimeError(
            f"Dataset có {dataset_stats['schema_errors']} dòng lỗi "
            "schema; sửa dataset trước khi train.")
    if dataset_stats["verified_true"] <= 0:
        raise RuntimeError(
            "Dataset không có dòng verified=True nào; train trên "
            "trace chưa kiểm chứng vi phạm cổng quyết định.")
    # Import nặng chỉ thực hiện ở đây, sau mọi kiểm tra cổng, để
    # dry-run và các lần chạy kiểm tra không bị phụ thuộc vào torch.
    import torch  # noqa: F401
    from peft import LoraConfig, get_peft_model  # noqa: F401
    from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: F401

    target_modules = config.get("target_modules")
    if not target_modules:
        raise RuntimeError(
            "target_modules chưa chỉ định trong config.json. Module "
            "đích của LoRA phụ thuộc kiến trúc base model đã chọn, "
            "phải điền đúng theo kiến trúc đó trước khi train thật.")
    output_dir.mkdir(parents=True, exist_ok=True)
    # Ghi chú triển khai (không phải code train đầy đủ chạy ngay):
    # bước tiếp theo ở đây là nạp tokenizer + model của base_model đã
    # chọn, gắn LoraConfig(r, alpha, dropout, target_modules),
    # tokenize cặp (prompt, completion) từ build_training_text với
    # mask prompt, rồi chạy Trainer. Đoạn này được giữ ở dạng chặn
    # tường minh thay vì code train nửa vời, vì mỗi kiến trúc base
    # model có cách nạp và padding khác nhau; người vận hành phải
    # hoàn thiện và kiểm tra bước này trên một batch nhỏ trước khi
    # chạy toàn bộ, rồi mới được coi là "đã train thật".
    raise RuntimeError(
        "Chặn train thật ở bước cuối: base model đã chọn nhưng bước "
        "nạp model/tokenizer theo kiến trúc cụ thể chưa được hoàn "
        "thiện trong bản pipeline này. Hãy hoàn thiện bước nạp model "
        "cho đúng kiến trúc đã chọn, kiểm tra trên một batch nhỏ, "
        "rồi mới chạy train toàn bộ. Không được báo cáo là đã train "
        "khi chưa qua bước này.")


def main(argv: list[str] | None = None) -> int:
    """Điểm vào dòng lệnh của script train LoRA.

    Đầu vào:
        argv: tham số dòng lệnh (None thì lấy từ sys.argv).

    Đầu ra:
        Mã thoát 0 khi dry-run hoàn tất (kể cả khi base_model chưa
        chọn hoặc thiếu thư viện nặng, vì dry-run cố tình pass để
        kiểm tra dataset/config); mã thoát khác 0 khi train thật bị
        chặn bởi cổng quyết định (chưa chọn model, thiếu thư viện,
        dataset lỗi schema, hoặc chưa hoàn thiện bước nạp model).
    """
    parser = argparse.ArgumentParser(
        description="Train LoRA cho parser DataMate (dry-run mặc định "
        "không cần weights).")
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH,
        help="Đường dẫn config.json.")
    parser.add_argument(
        "--dataset", type=Path, default=None,
        help="Đường dẫn dataset JSONL (mặc định lấy từ config).")
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path(__file__).resolve().parent / "lora_output",
        help="Thư mục ghi adapter LoRA (chỉ dùng khi train thật).")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Chỉ kiểm tra dataset/config và ước lượng bước, không "
        "train, không tải weights.")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    dataset_path = args.dataset or Path(str(config.get(
        "training_data", "")))
    # Đường dẫn tương đối trong config được hiểu là tương đối từ thư
    # mục training/ (nơi chứa config), không phải từ thư mục làm việc
    # hiện tại, để lệnh chạy được từ bất kỳ đâu mà vẫn tìm đúng file.
    if dataset_path and not dataset_path.is_absolute():
        candidate = Path(__file__).resolve().parent / dataset_path
        if candidate.is_file():
            dataset_path = candidate
    if not str(dataset_path):
        print("LỖI: config không chỉ định training_data và không có "
              "--dataset.", file=sys.stderr)
        return 2
    if args.dry_run:
        report = dry_run(config, dataset_path)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("DRY-RUN: chưa train thật weights nào. "
              "ready_for_real =", report["ready_for_real"])
        return 0
    try:
        result = train(config, dataset_path, args.output_dir)
    except RuntimeError as exc:
        print(f"Train bị chặn: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
