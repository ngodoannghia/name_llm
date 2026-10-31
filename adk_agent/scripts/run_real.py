# -*- coding: utf-8 -*-
"""Chạy ingest bằng model thật cho một hoặc nhiều shop.

Script này là bản chính thức của các script chạy thử luồng thật trước
đây (vốn chỉ nằm ở /tmp): nó dùng LiteLlm để gọi model thật qua API,
kèm bộ đếm request cứng để không bao giờ vượt ngân sách người dùng
duyệt. Key API chỉ lấy từ biến môi trường OPENAI_API_KEY của tiến
trình (LiteLlm/litellm tự đọc biến này); script không in, không ghi
key ra bất cứ đâu.

Cách chạy (từ thư mục adk_agent/):
  OPENAI_API_KEY=<key> .venv/bin/python scripts/run_real.py \\
      --data-dir /đường/dẫn/train/data \\
      --output-dir ./output_real \\
      --shops biz_123 \\
      --model gpt-4o-mini \\
      --max-requests 30

Chạy tất cả shop trong data-dir (không cần liệt kê từng shop, và
không hard-code danh sách business_id nào — script tự phát hiện thư
mục con của --data-dir):
  OPENAI_API_KEY=<key> .venv/bin/python scripts/run_real.py \\
      --data-dir /đường/dẫn/train/data \\
      --output-dir ./output_real \\
      --max-requests 300
  (--shops all hoặc bỏ hẳn --shops đều cho kết quả như nhau; xem trước
  danh sách sẽ chạy mà không tốn request nào bằng --dry-run.)

Mặc định model là gpt-4o-mini (model chỉ dùng cho test luồng, không
phải model cuối cho Docker) và giới hạn mặc định là 30 request cho cả
lần chạy. Đặt --max-requests 0 nghĩa là không giới hạn — chỉ dùng khi
đã chủ động chấp nhận chi phí, vì model có thể retry nhiều lần khi
validator bác (mỗi lần retry là một request mới).

CẢNH BÁO CHO LẦN CHẠY ALL SHOPS: 30 shop train tốn tổng cộng khoảng
200+ request (shop một bảng chỉ 1 request, shop nhiều bảng như
biz_123 tốn 21 request, cộng thêm retry khi validator bác). Cap mặc
định 30 KHÔNG ĐỦ chạy hết all shops: khi chạm cap, shop đang chạy sẽ
failed, các shop sau vẫn tiếp tục nhưng shop nào cần gọi model cũng
failed ngay ở lần gọi đầu tiên (chỉ shop một bảng không cần model mới
còn ok được). Vì vậy chạy all shops phải đặt --max-requests đủ lớn
(ví dụ 300) hoặc 0, và chấp nhận chi phí tương ứng.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

# Script nằm ở adk_agent/scripts/ nên phải thêm thư mục adk_agent/
# vào sys.path thì mới import được package ingest khi chạy từ bất kỳ
# thư mục nào (khi chạy bằng `python -m ingest._driver` thì không cần
# bước này vì Python đã tự thêm thư mục hiện tại).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from google.adk.models.lite_llm import LiteLlm  # noqa: E402
from google.adk.models.llm_request import LlmRequest  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402

from ingest._config import IngestConfig  # noqa: E402
from ingest._driver import run_ingest_all  # noqa: E402

# Bộ đếm đặt NGOÀI model vì LiteLlm là Pydantic model: gán thuộc tính
# mới lên instance (self.request_count = ...) bị Pydantic chặn với lỗi
# "object has no field". Dict module-level tránh hoàn toàn vấn đề này.
REQUEST_COUNT = {"n": 0}


class CountingLiteLlm(LiteLlm):
    """LiteLlm đếm request và dừng cứng ở giới hạn người dùng đặt.

    Mỗi lần generate_content_async là một request tới model (mỗi lượt
    agent chạy = 1 request, vì agent đã bỏ tool nên không còn vòng
    lặp tool-call). Vượt giới hạn thì raise ngay trước khi gọi, không
    gửi request vượt giới hạn đi; driver bắt exception theo shop, ghi
    failed cho shop đó rồi đi tiếp shop sau (một shop lỗi không dừng
    cả đợt).
    """

    def __init__(self, *args, max_requests: int = 30, **kwargs) -> None:
        """Khởi tạo model kèm giới hạn request.

        Args:
            *args: Đối số truyền cho LiteLlm (ví dụ model=...).
            max_requests: Giới hạn request cho cả lần chạy; 0 nghĩa
                là không giới hạn (chỉ dùng khi đã chấp nhận chi phí).
            **kwargs: Đối số khác truyền cho LiteLlm.
        """
        super().__init__(*args, **kwargs)
        # Lưu giới hạn ngoài field Pydantic bằng object.__setattr__
        # (gán thường self.max_requests = ... cũng bị chặn như
        # request_count, xem ghi chú ở REQUEST_COUNT).
        object.__setattr__(self, "max_requests", max_requests)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        """Đếm rồi ủy quyền cho LiteLlm, hoặc raise khi chạm giới hạn.

        Args:
            llm_request: Request ADK gửi cho model.
            stream: Có stream hay không (giữ nguyên như LiteLlm).

        Yields:
            LlmResponse từ LiteLlm.

        Raises:
            RuntimeError: Khi request này vượt giới hạn đã đặt.
        """
        REQUEST_COUNT["n"] += 1
        limit = self.max_requests
        if limit > 0 and REQUEST_COUNT["n"] > limit:
            raise RuntimeError(
                f"Đã đạt giới hạn {limit} request do người dùng đặt; "
                "dừng chạy trước khi gửi request thứ "
                f"{REQUEST_COUNT['n']}."
            )
        print(
            f"[request {REQUEST_COUNT['n']}"
            + (f"/{limit}]" if limit > 0 else "]")
            + " gọi model...",
            flush=True,
        )
        async for response in super().generate_content_async(
            llm_request, stream
        ):
            yield response


def discover_shops(data_dir: Path) -> list[str]:
    """Phát hiện tất cả shop trong data-dir từ chính thư mục con của nó.

    Không hard-code bất kỳ business_id nào: danh sách shop là mọi thư
    mục con trực tiếp của data-dir, sắp xếp theo tên để lần chạy ổn
    định và dễ đối chiếu với ingest_report.json. Thư mục không phải
    shop (nếu có) sẽ failed riêng ở driver mà không dừng cả đợt.

    Args:
        data_dir: Thư mục cha chứa các thư mục shop.

    Returns:
        Danh sách tên thư mục con, đã sắp xếp; rỗng khi data-dir không
        có thư mục con nào.
    """
    return sorted(
        entry.name for entry in data_dir.iterdir() if entry.is_dir()
    )


def resolve_shops(args: argparse.Namespace) -> list[str]:
    """Xác định danh sách shop sẽ chạy từ đối số dòng lệnh.

    Args:
        args: Đối số dòng lệnh đã parse (data_dir, shops).

    Returns:
        Danh sách shop: liệt kê tường minh từ --shops, hoặc toàn bộ
        shop phát hiện được khi --shops là "all" hoặc bị bỏ hẳn.
    """
    if args.shops is None or args.shops.strip().lower() == "all":
        return discover_shops(Path(args.data_dir))
    return [shop.strip() for shop in args.shops.split(",") if shop.strip()]


def cap_warning(shops_count: int, max_requests: int) -> str | None:
    """Cảnh báo khi cap request có thể không đủ cho đợt chạy.

    Ước tính thô ~7 request/shop (thực tế dao động từ 1 request cho
    shop một bảng tới 21+ request cho shop nhiều bảng như biz_123,
    chưa kể retry khi validator bác). Chỉ là cảnh báo, không chặn:
    cap thấp vẫn có ý nghĩa khi người dùng chỉ muốn lấy vài shop đầu.

    Args:
        shops_count: Số shop trong đợt chạy.
        max_requests: Cap request của đợt chạy (0 = không giới hạn).

    Returns:
        Câu cảnh báo, hoặc None khi cap đủ lớn hoặc không giới hạn.
    """
    if max_requests <= 0 or shops_count <= 1:
        return None
    estimate = shops_count * 7
    if max_requests >= estimate:
        return None
    return (
        f"cap hiện tại là {max_requests} nhưng đợt {shops_count} shop "
        f"ước tính tốn khoảng {estimate} request (trung bình ~7 "
        "request/shop, shop nhiều bảng tốn hơn nhiều); đợt này có thể "
        "không chạy hết. Cân nhắc --max-requests cao hơn hoặc 0."
    )


async def run(args: argparse.Namespace) -> int:
    """Chạy ingest thật cho danh sách shop và in báo cáo cuối.

    Args:
        args: Đối số dòng lệnh đã parse (data_dir, output_dir, shops,
            model, max_requests, dry_run).

    Returns:
        0 khi mọi shop ok, 1 khi có ít nhất một shop failed (0 luôn
        khi chỉ --dry-run, vì chưa chạy shop nào).
    """
    shops = resolve_shops(args)
    if not shops:
        print(f"Không tìm thấy shop nào trong {args.data_dir}.")
        return 1
    print(f"Sẽ chạy {len(shops)} shop: {', '.join(shops)}")
    if args.dry_run:
        # Chỉ liệt kê, không tạo model, không gọi API, không ghi gì.
        print("Chế độ --dry-run: dừng ở đây, chưa gửi request nào.")
        return 0
    warning = cap_warning(len(shops), args.max_requests)
    if warning is not None:
        # Cảnh báo chứ không chặn: người dùng có thể cố tình chạy cap
        # thấp để chỉ lấy vài shop đầu (shop một bảng vẫn ok được sau
        # khi chạm cap), nhưng phải biết cap thấp không chạy hết đợt.
        print(f"CẢNH BÁO: {warning}")
    model = CountingLiteLlm(model=args.model, max_requests=args.max_requests)
    reports = await run_ingest_all(
        shops,
        Path(args.data_dir),
        Path(args.output_dir),
        IngestConfig(),
        model=model,
        use_fake=False,
    )
    for report in reports:
        print(
            f"{report.business_id}: {report.status} "
            f"db={report.db_path} schema={report.schema_path} "
            f"error={report.error}"
        )
    print(
        f"Tổng request đã gửi: {REQUEST_COUNT['n']}"
        + (f"/{args.max_requests}" if args.max_requests > 0 else "")
    )
    return 0 if all(report.status == "ok" for report in reports) else 1


def main(argv: list[str] | None = None) -> int:
    """Entry point CLI của script chạy model thật.

    Args:
        argv: Đối số dòng lệnh (None thì lấy sys.argv).

    Returns:
        0 khi mọi shop ok, 1 khi có ít nhất một shop failed.
    """
    parser = argparse.ArgumentParser(
        description="Chạy workflow ingest ADK bằng model thật."
    )
    parser.add_argument("--data-dir", required=True, help="Thư mục cha chứa các thư mục shop.")
    parser.add_argument("--output-dir", required=True, help="Thư mục ghi DB/schema/log.")
    parser.add_argument("--shops", default=None, help="Danh sách shop cách nhau bằng dấu phẩy, ví dụ biz_123. Bỏ hẳn hoặc đặt 'all' để chạy tất cả shop phát hiện được trong --data-dir.")
    parser.add_argument("--model", default="gpt-4o-mini", help="Tên model cho LiteLlm (mặc định gpt-4o-mini, chỉ dùng cho test luồng).")
    parser.add_argument("--max-requests", type=int, default=30, help="Giới hạn request cho cả lần chạy (mặc định 30; 0 = không giới hạn). Chạy all shops cần đặt cao hơn nhiều (30 shop train khoảng 200+ request).")
    parser.add_argument("--dry-run", action="store_true", help="Chỉ liệt kê danh sách shop sẽ chạy rồi dừng, không gọi model, không ghi kết quả.")
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
