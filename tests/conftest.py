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

# 生产路径里 `backend/tool.py` 会在 import engine **之前**把随包 dukpy 装上
# （见 `backend/dukpy_vendor.py`）；测试里也照做一遍。否则宿主没装 dukpy 的环境（比如 CI 的
# cp312 runner）会让 `backend/engine/js_runtime.py` 顶层读到 `_HAS_DUKPY = False`，
# 引擎那 20 来个 `@js:` 用例全部 skip —— 白白放过"随包副本到底能不能用"这件事。
try:
    from backend import dukpy_vendor
    _DUKPY_STATE = dukpy_vendor.install()
except Exception as _err:  # 环境异常不应该让整个测试套件收集失败
    _DUKPY_STATE = {"source": "unavailable", "error": str(_err)}

