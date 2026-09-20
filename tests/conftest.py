# -*- coding: utf-8 -*-
"""pytest 公共配置：把包根加进 sys.path，关掉抓取节流。

`backend.engine.*` 是纯引擎（只依赖 requests/bs4/lxml/chardet/dukpy），可以直接导入；
`backend.tool` 依赖宿主 `webserver.*`，要跑得先装 `tests/_host_stub.py` 的替身 ——
见 `tests/test_tool_offline.py`。
"""
import os
import sys

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

os.environ.setdefault("MYBOOKS_FETCH_DELAY", "0")
