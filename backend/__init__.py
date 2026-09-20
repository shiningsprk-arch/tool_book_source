# -*- coding: utf-8 -*-
"""book_source 工具包后端。

宿主 `toolbox_manager._ensure_backend_package()` 会把本目录注册成一个真正的 Python
package（名为 `mybooks_tool_book_source_backend`），因此这里的子包/模块之间可以使用
相对 import —— `tool.py` 用 `from .engine import ...` 引用同目录下的引擎包。

本文件刻意保持最小：如果在这里 import 引擎/依赖，一旦宿主缺依赖（例如缺 dukpy 之外的
硬依赖 bs4/lxml/ebooklib），整个工具会在 `load_all()` 阶段加载失败、连工具列表里都看不到；
把重活留给 `tool.py`，可以让"加载得到、但某些能力报 deps.missing"成为可能。
"""
