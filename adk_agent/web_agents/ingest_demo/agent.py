# -*- coding: utf-8 -*-
"""Agent demo cho `adk web`: chạy workflow ingest DataMate qua giao diện web.

Cách dùng (từ thư mục adk_agent/):
  .venv/bin/adk web web_agents

  rồi mở http://localhost:8000, chọn agent `ingest_demo`.

Vì workflow ingest lấy business_id/shop_dir/output_dir từ state ban
đầu của session (driver CLI tự tạo state này, còn adk web thì session
bắt đầu trống), trước khi gửi tin nhắn đầu tiên bạn phải thêm 3 key
sau trong tab State của giao diện web:
  business_id = biz_101
  shop_dir    = /đường/dẫn/train/data/biz_101
  output_dir  = /đường/dẫn/output_web
Sau đó gửi tin nhắn bất kỳ (ví dụ "Bắt đầu ingest"), workflow sẽ chạy
từ init_shop như khi chạy bằng driver. Shop một bảng (biz_101) không
cần gọi model; shop nhiều bảng cần model thật cho header/merge/schema.

Model lấy từ biến môi trường DATAMATE_WEB_MODEL (mặc định
gpt-4o-mini). Nếu biến môi trường OPENAI_API_KEY có sẵn thì dùng
LiteLlm thật; nếu không có key thì dùng FakeLlm để demo giao diện
(kết quả schema là giả, chỉ để xem luồng chạy, không dùng làm kết
quả thật). Muốn chạy model thật qua web thì khởi động lại server kèm
key, ví dụ:
  OPENAI_API_KEY=<key> .venv/bin/adk web web_agents

File này chỉ là adapter demo cho adk web, không thuộc graph ingest:
graph vẫn chỉ ráp ở ingest/_workflow.py, không sửa gì ở đó.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# agent.py nằm ở web_agents/ingest_demo/ nên phải thêm thư mục adk_agent/
# (parents[2]) vào sys.path thì mới import được package ingest; loader
# của adk web chỉ đảm bảo thư mục agents (web_agents/) nằm trên path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ingest._config import IngestConfig
from ingest._workflow import build_ingest_workflow

_MODEL_NAME = os.environ.get("DATAMATE_WEB_MODEL", "gpt-4o-mini")

if os.environ.get("OPENAI_API_KEY"):
    # Có key thì dùng model thật qua LiteLlm (key do litellm tự đọc từ
    # biến môi trường, file này không in hay ghi key ra đâu).
    from google.adk.models.lite_llm import LiteLlm

    _model = LiteLlm(model=_MODEL_NAME)
    _model_id = _MODEL_NAME
    print(f"[ingest_demo] Dùng model thật: {_MODEL_NAME}", flush=True)
else:
    # Không có key thì dùng FakeLlm để demo giao diện với chi phí 0.
    # Kết quả schema lúc này là giả — chỉ xem luồng chạy, đừng lấy làm
    # kết quả ingest thật.
    from ingest._testing import FakeLlm

    _model = FakeLlm()
    _model_id = "fake"
    print(
        "[ingest_demo] Không có OPENAI_API_KEY nên dùng FakeLlm "
        "(demo giao diện, schema giả). Muốn model thật thì khởi động "
        "lại server kèm OPENAI_API_KEY.",
        flush=True,
    )

# Workflow là BaseNode nên loader của adk web chấp nhận làm root_agent.
# config.model_id được đặt tường minh để schema ghi đúng tên model
# (bình thường driver tự set từ instance model, còn adk web thì không
# có driver nên phải set ở đây).
root_agent = build_ingest_workflow(
    config=IngestConfig(model_id=_model_id),
    model=_model,
)
