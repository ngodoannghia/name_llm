# -*- coding: utf-8 -*-
"""Các agent của workflow ingest, mỗi agent một file riêng.

Agent được import trực tiếp từ module con (ví dụ
``ingest.agents._header_agent``) trong _workflow.py; module __init__ này
không re-export agent để tránh import vòng và giữ graph ráp ở một nơi.
"""

from __future__ import annotations
