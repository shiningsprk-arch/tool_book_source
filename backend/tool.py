# -*- coding: utf-8 -*-
"""书源引擎 —— MyBooks Toolbox 外部工具后端。

`manifest.json` 的 `entry_backend` 指向本模块的 `BookSourceTool`，`api_routes` 指向下面
15 个 Handler；宿主 `toolbox_manager.collect_tool_routes()` 会把它们挂到：

    /api/toolbox/tool/book_source/<action>

外部工具与内置工具有两个关键差别，改这个文件时必须知道：

1. 宿主动态挂路由时只额外包一层"工具被禁用就 404"的 `prepare()`，**不会注入任何鉴权
   装饰器**（内置工具的路由写在 `webserver/handlers/toolbox.py` 里、自带 `@js @is_admin`）。
   所以鉴权必须在这里显式写上——下面每个 JSON 路由都带 `@js @is_admin`。
2. `api_routes[].path` 只是路径片段（正则），完整的 `/api/toolbox/tool/<tool_id>/` 前缀由
   宿主拼接，本模块不能自己写死完整路由。

与上游 `书源引擎插件/` 的差异见仓库根 `NOTICE.md`。规则引擎本身（`backend/engine/`）
是上游 `webserver/toolbox/book_source_engine/` 的逐字节拷贝，只在 import 上换成了包内相对
引用。
"""
import importlib.util
import ipaddress
import json
import logging
import os
import re
import socket
import tempfile
import threading
import zipfile
from typing import Callable, Optional
from urllib.parse import quote, urlparse, urlunparse

import requests
import tornado

from webserver.handlers.base import BaseHandler, is_admin, js
from webserver.i18n import _
from webserver.services import AsyncService
from webserver.services.background_service import BackgroundService, BackgroundTask
from webserver.toolbox.base_tool import BaseTool

logger = logging.getLogger(__name__)

TOOL_ID = "book_source"

# 与 manifest.json 的 api_routes 对应；宿主固定挂在 /api/toolbox/tool/<tool_id>/ 下
_API_ROOT = "/api/toolbox/tool/%s" % TOOL_ID

_DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/125.0.0.0 Safari/537.36")


class DependencyMissing(RuntimeError):
    """宿主环境缺少本工具运行所需的第三方依赖。

    Handler 层把它翻译成 `{"err": "deps.missing"}`，而不是让它变成 500 —— 外部工具没法像
    内置工具那样往宿主的 `requirements.txt` 里加一行，作者只能"探测 + 明确报错"。
    """


# ---------------------------------------------------------------------------
# 随包内置的 dukpy（`@js:` 规则要用的 JS 引擎）
# ---------------------------------------------------------------------------
# 宿主 requirements.txt 里没有 dukpy，外部工具也改不了它；包里带了一份按平台裁过的副本
# （backend/vendor/dukpy/，生成脚本 scripts/fetch_vendor_wheels.py）。**必须在下面 import
# engine 之前装上**：engine/js_runtime.py 是在模块顶层 `import dukpy` 并据此定
# `_HAS_DUKPY` 的，晚一步就永远是降级状态。
#
# 和引擎导入一样加了守卫：这段出问题也只是"JS 规则不可用"，不能让工具从 /api/toolbox/list
# 里消失。
_VENDOR_IMPORT_ERROR = ""
_DUKPY_VENDOR_STATE = {}
try:
    from . import dukpy_vendor
except Exception as _vendor_err:  # pragma: no cover - 取决于宿主环境
    dukpy_vendor = None
    _VENDOR_IMPORT_ERROR = "%s: %s" % (type(_vendor_err).__name__, _vendor_err)
    logger.error("[%s] dukpy 随包副本加载器导入失败: %s", TOOL_ID, _VENDOR_IMPORT_ERROR)
else:
    _DUKPY_VENDOR_STATE = dukpy_vendor.install()
    if _DUKPY_VENDOR_STATE.get("source") in ("bundled", "external"):
        logger.info("[%s] 已启用随包 dukpy 副本: %s (%s)", TOOL_ID,
                    _DUKPY_VENDOR_STATE.get("path"), _DUKPY_VENDOR_STATE.get("tag"))
    elif _DUKPY_VENDOR_STATE.get("source") == "missing":
        logger.info("[%s] %s", TOOL_ID, _DUKPY_VENDOR_STATE.get("error"))


# ---------------------------------------------------------------------------
# 引擎导入（带守卫）
# ---------------------------------------------------------------------------
# 为什么不在模块顶层裸 import：外置工具的模块是在宿主 `toolbox_manager.load_all()` 阶段
# 被 import 的，一旦这里抛异常，宿主只会记一行 `加载工具 xxx 失败，跳过`，工具在
# `/api/toolbox/list` 里直接消失，管理员看不到任何可用信息。守卫住之后，工具照常加载、
# 照常出现在列表里，只在真正调用时需要依赖的接口上回 `deps.missing`。
_ENGINE_ERROR = ""

try:  # pragma: no cover - 取决于宿主环境
    from .engine import (  # noqa: F401
        BookSource,
        SearchTaskService,
        dump_sources_to_json,
        fetch_book_info,
        fetch_content,
        fetch_explore,
        fetch_toc,
        load_sources_from_json,
        parse_explore_categories,
        search_books,
    )
    from .engine.epub_helper import generate_epub as _generate_epub
    from .engine.js_runtime import _HAS_DUKPY
    _ENGINE_OK = True
except Exception as _err:  # pragma: no cover - 取决于宿主环境
    _ENGINE_OK = False
    _ENGINE_ERROR = "%s: %s" % (type(_err).__name__, _err)
    logger.error("[%s] 规则引擎导入失败: %s", TOOL_ID, _ENGINE_ERROR)

    def _missing_dependency(*_args, **_kwargs):
        raise DependencyMissing(_ENGINE_ERROR)

    BookSource = None
    SearchTaskService = None
    load_sources_from_json = dump_sources_to_json = _missing_dependency
    search_books = fetch_book_info = fetch_toc = fetch_content = _missing_dependency
    parse_explore_categories = fetch_explore = _missing_dependency
    _generate_epub = _missing_dependency
    _HAS_DUKPY = False


# ---------------------------------------------------------------------------
# 模块级工具函数
# ---------------------------------------------------------------------------

def _iter_all_rule_strings(item: dict):
    """递归遍历书源的规则字段，产出 (field_path, string_value)。"""
    if not isinstance(item, dict):
        return
    for key, val in item.items():
        path = str(key)
        if isinstance(val, str):
            yield path, val
        elif isinstance(val, dict):
            for k2, v2 in val.items():
                sub = f"{path}.{k2}"
                if isinstance(v2, str):
                    yield sub, v2
                elif isinstance(v2, list):
                    for i, el in enumerate(v2):
                        if isinstance(el, str):
                            yield f"{sub}[{i}]", el
                        elif isinstance(el, dict):
                            for k3, v3 in el.items():
                                if isinstance(v3, str):
                                    yield f"{sub}[{i}].{k3}", v3


def safe_extract_zip(zip_path: str, dest_dir: str) -> None:
    """把 zip 解压到 dest_dir，逐条校验成员路径，挡住 zip slip（`../` 路径穿越）。

    上游实现是直接 `zf.extractall(tmpdir)`：一个成员名为 `../../../etc/x` 的恶意 zip 会把
    文件写到解压目录之外。做法对齐宿主 `toolbox_manager._safe_extract()`。
    """
    dest_abs = os.path.abspath(dest_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            member_path = os.path.abspath(os.path.join(dest_dir, member.filename))
            if member_path != dest_abs and not member_path.startswith(dest_abs + os.sep):
                raise ValueError(_("zip 包内包含非法路径：%s") % member.filename)
        zf.extractall(dest_dir)


_LEGACY_SOURCE_FILENAMES = ("importBookSource.json", "importBookSource.txt")
_SOURCE_SCAN_EXTS = (".json", ".txt")


def _candidate_source_files(root: str):
    """列出 zip 解压目录里"可能是书源数据"的文件（相对路径），并排好处理顺序。

    ① 先其余 `.json` / `.txt`，按路径排序（预设包）；
    ② 最后才是 `importBookSource.json` / `importBookSource.txt`（Legado 自己的导出命名）。

    **顺序为什么是这样**：导入是依次并入的，同名书源后处理到的会覆盖先前的，所以把
    "用户自己导出的那份"放最后 → **同名时以 `importBookSource.*` 为准**，符合直觉；
    同时预设包之间的优先级也由文件名排序决定，同一份 zip 每次导入结果一致。

    为什么不能只认 ②：实际流传的书源包名字五花八门（`booksources.seed.json`、
    `tickmao-legado-full.json`、`shidahuilang-good.json`…），只认固定文件名会"导入成功但 0 条"。
    """
    legacy, others = [], []
    for dirpath, _dirs, files in os.walk(root):
        for fn in sorted(files):
            if not fn.lower().endswith(_SOURCE_SCAN_EXTS):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/")
            (legacy if fn in _LEGACY_SOURCE_FILENAMES else others).append(rel)
    return sorted(others) + legacy


def describe_environment() -> dict:
    """探测本工具需要/可选的第三方依赖，供前端解释能力降级。

    外部工具不能改宿主的 `requirements.txt`，缺什么只能在运行时探明并如实上报。
    dukpy 是特殊的那个：宿主没有，但包里带了按平台裁过的副本（见 `dukpy_vendor`），
    所以这里额外回报"到底用了哪一份、要不要不匹配"。
    """
    required = {
        "requests": "抓取书源内容",
        "bs4": "CSS 规则解析",
        "lxml": "HTML/XML 解析后端",
    }
    optional = {
        "dukpy": "JS 规则（@js: / jsLib）",
        "ebooklib": "EPUB 生成",
        "chardet": "响应编码嗅探（缺失时按 utf-8 解码）",
        "curl_cffi": "Chrome TLS 指纹伪装（缺失时回退 requests）",
    }
    if dukpy_vendor is not None:
        vendor = dukpy_vendor.describe()
    else:
        vendor = {"source": "unavailable", "tag": "", "path": "",
                  "error": _VENDOR_IMPORT_ERROR, "requested_tag": "",
                  "bundled_tags": []}

    missing_required = []
    deps = {}
    for name, purpose in required.items():
        ok = importlib.util.find_spec(name) is not None
        deps[name] = {"ok": ok, "required": True, "purpose": purpose}
        if not ok:
            missing_required.append(name)
    for name, purpose in optional.items():
        ok = importlib.util.find_spec(name) is not None
        entry = {"ok": ok, "required": False, "purpose": purpose}
        if name == "dukpy":
            entry["source"] = vendor["source"]
        deps[name] = entry

    return {
        "engine": _ENGINE_OK,
        "engine_error": _ENGINE_ERROR,
        "js_rules": bool(_HAS_DUKPY),
        "epub": bool(deps["ebooklib"]["ok"]),
        "dukpy_source": vendor["source"],
        "dukpy_vendor": vendor,
        "deps": deps,
        "missing_required": missing_required,
    }


# ---------------------------------------------------------------------------
# 工具类
# ---------------------------------------------------------------------------

class BookSourceTool(BaseTool):
    """书源管理工具：书源 CRUD / 校验 / 多源搜索 / 正文抓取 / EPUB 生成。"""

    service_item_name = "书源管理"

    # 每个用户最近一次下载/生成任务（多用户互不干扰）
    _last_task_ids: dict = {}
    _tasks_lock = threading.Lock()

    # 书源文件读写锁（防止并发保存互相覆盖）
    _sources_lock = threading.RLock()

    @staticmethod
    def info() -> dict:
        # 与仓库根 manifest.json 的对应字段保持一致（tests/test_package_layout.py 会校验）
        return {
            "tool_id": TOOL_ID,
            "name": "书源引擎",
            "description": "Legado 3.0 兼容书源引擎：书源管理、多源搜索、正文抓取与 EPUB 生成",
            "revision": "1.2.1",
            "author": "黏菌",
            "publish_date": "2026-09-20",
            "repo_url": "https://github.com/shiningsprk-arch/tool_book_source",
        }

    # ── 运行环境 ────────────────────────────────────────────────

    @staticmethod
    def capabilities() -> dict:
        """本工具当前实际具备的能力（依赖探测结果）。"""
        return describe_environment()

    # ── 并发控制 ────────────────────────────────────────────────

    @classmethod
    def is_running(cls, user_id: int = 1) -> bool:
        task = cls.get_last_task(user_id)
        return bool(task and task.get("status") == BackgroundTask.STATUS_RUNNING)

    @classmethod
    def get_last_task(cls, user_id: int = 1) -> Optional[dict]:
        with cls._tasks_lock:
            task_id = cls._last_task_ids.get(user_id)
        if task_id is None:
            return None
        return BackgroundService().get_task(task_id)

    @classmethod
    def set_last_task(cls, user_id: int, task_id: int):
        with cls._tasks_lock:
            cls._last_task_ids[user_id] = task_id

    @staticmethod
    def _is_safe_url(url: str) -> bool:
        """SSRF 防护：仅允许公网 http/https，拒绝私有/回环/链路本地等地址。"""
        if not url:
            return False
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return False
        try:
            infos = socket.getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM)
        except OSError:
            return False
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                return False
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                return False
        return True

    # ── 书源 CRUD ──────────────────────────────────────────────

    @AsyncService.register_function
    def list_sources(self) -> list:
        """列出所有已保存的书源。"""
        sources = self._load_sources()
        return [s.to_dict() if hasattr(s, "to_dict") else s for s in sources]

    @AsyncService.register_function
    def get_source(self, name: str) -> Optional[dict]:
        """按书源名查找。"""
        for s in self._load_sources():
            if s.bookSourceName == name:
                return s.to_dict() if hasattr(s, "to_dict") else s
        return None

    @AsyncService.register_function
    def add_source(self, source_data: dict) -> dict:
        """添加或更新一个书源。"""
        with self._sources_lock:
            sources = self._load_sources()
            new_source = BookSource.from_dict(source_data)
            for i, s in enumerate(sources):
                if s.bookSourceName == new_source.bookSourceName:
                    sources[i] = new_source
                    self._save_sources(sources)
                    return {"status": "updated", "name": new_source.bookSourceName}
            sources.append(new_source)
            self._save_sources(sources)
            return {"status": "added", "name": new_source.bookSourceName}

    @AsyncService.register_function
    def delete_source(self, name: str) -> dict:
        """删除一个书源。"""
        with self._sources_lock:
            sources = self._load_sources()
            before = len(sources)
            sources = [s for s in sources if s.bookSourceName != name]
            if len(sources) == before:
                return {"status": "not_found", "name": name}
            self._save_sources(sources)
            return {"status": "deleted", "name": name}

    @AsyncService.register_function
    def toggle_source(self, name: str, enabled: bool = None) -> dict:
        """启用/禁用一个书源。"""
        with self._sources_lock:
            sources = self._load_sources()
            for s in sources:
                if s.bookSourceName == name:
                    if enabled is not None:
                        s.enabled = enabled
                    else:
                        s.enabled = not s.enabled
                    self._save_sources(sources)
                    return {"status": "toggled", "name": name, "enabled": s.enabled}
            return {"status": "not_found", "name": name}

    # ── 书源校验 ──────────────────────────────────────────────

    @staticmethod
    def _iter_rule_values(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for v in value.values():
                yield from BookSourceTool._iter_rule_values(v)
        elif isinstance(value, list):
            for v in value:
                yield from BookSourceTool._iter_rule_values(v)

    @staticmethod
    def _has_unsupported_js(value):
        if not isinstance(value, str):
            return False
        low = value.lower()
        return "<js>" in low or "{{js." in low or "{{java" in low or "java.ajax" in low or "java.post" in low

    @staticmethod
    def _requires_js(raw):
        rule_search = raw.get("ruleSearch") or {}
        rule_content = raw.get("ruleContent") or {}
        search_url = raw.get("searchUrl", "")
        search_js_blocked = BookSourceTool._has_unsupported_js(search_url) or (
            "@js:" in search_url and not search_url.strip().startswith("@js:")
        )
        return (
            search_js_blocked
            or BookSourceTool._has_unsupported_js(rule_search.get("bookList", ""))
            or BookSourceTool._has_unsupported_js(rule_content.get("content", ""))
        )

    @staticmethod
    def _source_tags(raw):
        tags = []
        if not isinstance(raw, dict):
            return tags
        if raw.get("bookSourceType") is not None:
            tags.append("text" if str(raw.get("bookSourceType") or "0") == "0" else "non-text")
        url = (raw.get("bookSourceUrl") or "").strip()
        if url.lower().startswith("https://"):
            tags.append("https")
        elif url:
            tags.append("http")
        rule_search = raw.get("ruleSearch") or {}
        rule_book = raw.get("ruleBookInfo") or {}
        rule_toc = raw.get("ruleToc") or {}
        rule_content = raw.get("ruleContent") or {}
        if raw.get("searchUrl") and rule_search.get("bookList"):
            tags.append("search")
        if rule_book:
            tags.append("info")
        if rule_toc.get("chapterList"):
            tags.append("toc")
        if rule_content.get("content"):
            tags.append("content")
        if raw.get("exploreUrl"):
            tags.append("explore")
        if raw.get("loginUrl"):
            tags.append("login-needed")
        if rule_content.get("webJs"):
            tags.append("webjs-unsupported")
        if rule_content.get("imageStyle"):
            tags.append("image-style")
        values = list(BookSourceTool._iter_rule_values(raw))
        if any(v.strip().startswith(("$", "@json:")) for v in values):
            tags.append("json")
        if any("@css:" in v or "class." in v or "tag." in v or "id." in v for v in values):
            tags.append("html")
        if any("@js:" in v or "<js>" in v for v in values):
            tags.append("js-runtime")
        return tags

    @staticmethod
    def _missing_required_features(raw):
        rule_search = raw.get("ruleSearch") or {}
        rule_toc = raw.get("ruleToc") or {}
        rule_content = raw.get("ruleContent") or {}
        checks = [
            ("bookSourceName", raw.get("bookSourceName")),
            ("bookSourceUrl", raw.get("bookSourceUrl")),
            ("searchUrl", raw.get("searchUrl")),
            ("ruleSearch.bookList", rule_search.get("bookList")),
            ("ruleSearch.name", rule_search.get("name")),
            ("ruleSearch.bookUrl", rule_search.get("bookUrl")),
            ("ruleToc.chapterList", rule_toc.get("chapterList")),
            ("ruleToc.chapterName", rule_toc.get("chapterName")),
            ("ruleToc.chapterUrl", rule_toc.get("chapterUrl")),
            ("ruleContent.content", rule_content.get("content")),
        ]
        return [name for name, value in checks if not value]

    @AsyncService.register_function
    def validate_source(self, raw: dict, timeout: int = 8) -> dict:
        """校验书源连通性和功能完整性。"""
        if not isinstance(raw, dict):
            return {"ok": False, "status": "invalid", "message": "书源格式无效", "tags": []}
        if not raw.get("bookSourceName") or not raw.get("bookSourceUrl"):
            return {"ok": False, "status": "invalid", "message": "缺少 bookSourceName 或 bookSourceUrl"}

        tags = self._source_tags(raw)

        # DNS + HTTP 连通性
        source_url = (raw.get("bookSourceUrl") or "").strip()
        if "://" not in source_url:
            source_url = "http://" + source_url
        parsed = urlparse(source_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return {"ok": False, "status": "invalid", "message": "书源 URL 无效", "tags": tags}

        probe_url = urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        network_tags = []
        try:
            socket.getaddrinfo(host, port)
            network_tags.append("dns-ok")
        except OSError as err:
            network_tags.append("dns-failed")
            return {
                "ok": False, "status": "dns_failed",
                "message": f"DNS 解析失败：{err}", "tags": sorted(set(tags + network_tags)),
            }

        try:
            resp = requests.get(probe_url, timeout=timeout, allow_redirects=True, stream=True,
                                headers={"User-Agent": _DEFAULT_UA})
            resp.close()
            network_tags.append("connect-ok")
            network_tags.append("http-%s" % resp.status_code)
            if resp.status_code >= 500:
                return {
                    "ok": False, "status": "connect_failed",
                    "message": f"HTTP 状态 {resp.status_code}", "tags": sorted(set(tags + network_tags)),
                }
        except requests.exceptions.SSLError as err:
            network_tags.append("ssl-failed")
            return {
                "ok": False, "status": "ssl_failed",
                "message": f"SSL 校验失败：{err}", "tags": sorted(set(tags + network_tags)),
            }
        except Exception as err:
            network_tags.append("connect-failed")
            return {
                "ok": False, "status": "connect_failed",
                "message": f"连通性测试失败：{err}", "tags": sorted(set(tags + network_tags)),
            }

        tags = sorted(set(tags + network_tags))

        # JS 依赖检测
        if self._requires_js(raw):
            return {
                "ok": False, "status": "js_unsupported",
                "message": "关键规则依赖 JS，暂不支持", "tags": tags,
            }

        # 功能完整性检查（非阻塞，仅提示）
        missing = self._missing_required_features(raw)
        if missing:
            return {
                "ok": False, "status": "incomplete",
                "message": f"缺少关键规则：{', '.join(missing[:4])}", "tags": tags,
            }

        return {"ok": True, "status": "ok", "message": "检测通过", "tags": tags}

    @staticmethod
    def _check_engine_compatible(item: dict):
        """检查书源是否与当前引擎兼容（不依赖网络）。

        只检查关键规则字段 — searchUrl、bookList、chapterList、content、header。
        非关键字段（coverUrl、exploreUrl、init、jsLib 等）不阻塞导入。
        """
        critical_fields = {
            "searchUrl", "ruleSearch.bookList", "ruleSearch.name",
            "ruleSearch.author", "ruleSearch.bookUrl",
            "ruleToc.chapterList", "ruleToc.chapterName", "ruleToc.chapterUrl",
            "ruleContent.content",
        }
        for field, value in _iter_all_rule_strings(item):
            if field not in critical_fields:
                continue
            if BookSourceTool._has_unsupported_js(value):
                return False, f"{field} 含不支持的 JS 调用"

        header = item.get("header", "")
        if isinstance(header, str) and (header.startswith("@js:") or header.startswith("<js>")):
            return False, "header 为 JS 动态，不支持"

        return True, ""

    @AsyncService.register_function
    def import_sources_from_url(self, url: str) -> dict:
        """从远程 URL 批量导入书源（Legado 书源订阅）。跳过引擎不兼容的书源。"""
        if not self._is_safe_url(url):
            return {"status": "fetch_failed", "added": 0,
                    "message": "URL 无效或指向内网地址（已阻止）"}
        try:
            resp = requests.get(url, timeout=30, headers={"User-Agent": _DEFAULT_UA})
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("import book source from url failed: %s", exc)
            return {"status": "fetch_failed", "added": 0, "message": f"拉取书源 URL 失败：{exc}"}

        if not self._looks_like_sources(data):
            return {"status": "no_sources", "added": 0, "updated": 0, "skipped": 0,
                    "message": "该 URL 返回的不是书源数据（缺少 bookSourceName/bookSourceUrl）"}
        return self._import_items(data, source_label="url")

    @AsyncService.register_function
    def import_sources_from_zip(self, zip_path: str) -> dict:
        """从 ZIP 文件导入书源。`status` = ok / no_sources / format_error。

        与上游的三处不同，都是"能不能真的用起来"的问题：

        1. **解压走 `safe_extract_zip()`**：上游是裸的 `zipfile.extractall()`，成员名带 `../`
           的恶意 zip 能写到解压目录之外。
        2. **按内容识别来源文件，而不是只认文件名**（见 `_candidate_source_files()`）：
           上游只看 `importBookSource.json/.txt`，而真实书源包的命名五花八门，结果是
           "导入成功，新增 0 个书源"。现在扫 zip 里所有 `.json`/`.txt`，用
           `_looks_like_sources()` 按结构判断；无关 JSON（配置、元数据表）跳过并记明原因。
           同名书源以 `importBookSource.*` 为准（它排在最后处理），其次按文件名排序决定。
        3. `_import_items()` 改成整包只落盘一次：上游逐条 `add_source()` 是 O(n²)，
           实测 800 条 23 秒、3000 条 5 分钟以上。
        """
        errors = []
        added = updated = skipped = 0
        used = []
        seen_files = []

        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                safe_extract_zip(zip_path, tmpdir)
            except (zipfile.BadZipFile, ValueError) as exc:
                return {"status": "format_error", "added": 0, "updated": 0, "skipped": 0,
                        "message": f"不是合法的 zip 文件：{exc}", "errors": [str(exc)]}

            for rel in _candidate_source_files(tmpdir):
                seen_files.append(rel)
                fpath = os.path.join(tmpdir, rel)
                try:
                    with open(fpath, "r", encoding="utf-8-sig") as f:
                        data = json.load(f)
                except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                    errors.append(f"{rel}: 解析失败 — {exc}")
                    continue
                if not self._looks_like_sources(data):
                    errors.append(f"{rel}: 跳过 — 不是书源数据（无 bookSourceName/bookSourceUrl）")
                    continue
                result = self._import_items(data, source_label=rel, errors=errors)
                added += result["added"]
                updated += result["updated"]
                skipped += result["skipped"]
                errors = result["errors"]
                if result["status"] == "ok":
                    used.append(rel)

        if not used:
            return {"status": "no_sources", "added": added, "updated": updated, "skipped": skipped,
                    "scanned": seen_files[:20], "errors": errors[:10],
                    "message": "压缩包里没找到书源数据（看过：%s）"
                               % ("、".join(seen_files[:5]) or "没有 .json/.txt")}

        return {"status": "ok", "added": added, "updated": updated,
                "skipped": skipped, "files": used, "errors": errors[:10]}

    @staticmethod
    def _looks_like_sources(data) -> bool:
        """按**结构**判断是不是书源数据（而不是靠文件名）。

        只看前 10 条：真实书源包里偶尔前面有非书源的占位项，但"整份都不是书源"的情况更常见
        （配置、元数据表、订阅索引…），所以按"有没有一条像"来决定放不放行。
        """
        items = data if isinstance(data, list) else [data]
        for item in items[:10]:
            if isinstance(item, dict) and item.get("bookSourceName") and item.get("bookSourceUrl"):
                return True
        return False

    def _import_items(self, data, source_label: str = "", errors: Optional[list] = None) -> dict:
        """把一份书源 JSON（数组或单对象）里兼容的书源**一次性**并入书源表。

        ⚠️ 刻意不逐条调 `add_source()`：那个方法每次都"读全量 → 改 → 写全量"，整包导入会退化成
        O(n²)（实测 50/100/200/400/800 条 = 0.20/0.62/2.03/6.55/23.37 秒，2973 条 >5 分钟）。
        这里在内存里合并，最后只 `_save_sources()` 一次（仍在 `_sources_lock` 里，语义不变）。
        """
        errors = [] if errors is None else errors
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return {"status": "format_error", "added": 0, "updated": 0, "skipped": 0,
                    "message": "书源格式应为 JSON 数组或对象", "errors": errors}

        accepted = []
        skipped = 0
        for item in data:
            if not isinstance(item, dict):
                errors.append("invalid item")
                continue
            name = item.get("bookSourceName", "unknown")
            if not name or not item.get("bookSourceUrl"):
                errors.append(f"missing fields: {name}")
                continue

            ok, reason = self._check_engine_compatible(item)
            if not ok:
                skipped += 1
                errors.append(f"{name}: 跳过 — {reason}")
                continue

            accepted.append(item)

        added = 0
        updated = 0
        if accepted:
            with self._sources_lock:
                sources = self._load_sources()
                index = {s.bookSourceName: i for i, s in enumerate(sources)}
                for item in accepted:
                    try:
                        new_source = BookSource.from_dict(item)
                    except Exception as exc:  # 单条坏数据不该毁掉整包导入
                        errors.append("%s: 解析失败 — %s"
                                      % (item.get("bookSourceName") or "unknown", exc))
                        continue
                    name = new_source.bookSourceName
                    if name in index:
                        sources[index[name]] = new_source
                        updated += 1
                    else:
                        index[name] = len(sources)
                        sources.append(new_source)
                        added += 1
                if added or updated:
                    self._save_sources(sources)  # ← 整包只写一次盘

        return {"status": "ok", "added": added, "updated": updated,
                "skipped": skipped, "errors": errors}

    # ── 搜索与测试 ──────────────────────────────────────────────

    @AsyncService.register_function
    def search(self, source_name: str, keyword: str) -> list:
        """在指定书源中搜索（同步）。"""
        source = self._find_source(source_name)
        if not source:
            raise ValueError(_("书源不存在: %s") % source_name)
        results = search_books(source, keyword)
        return [
            {
                "name": r.get("name", ""),
                "author": r.get("author", ""),
                "kind": r.get("kind", ""),
                "wordCount": r.get("wordCount", ""),
                "lastChapter": r.get("lastChapter", ""),
                "intro": r.get("intro", ""),
                "coverUrl": r.get("coverUrl", ""),
                "bookUrl": r.get("bookUrl", ""),
            }
            for r in results
        ]

    @AsyncService.register_function
    def search_async(self, keyword: str, source_names: Optional[list] = None) -> dict:
        """异步多书源并发搜索，立即返回 task_id。

        source_names 为 None 时搜索所有已启用的书源。
        """
        all_sources = self._load_sources()
        targets = all_sources if source_names is None else [
            s for s in all_sources if s.bookSourceName in source_names
        ]
        targets = [s for s in targets if s.enabled]
        if not targets:
            return {"task_id": "", "total": 0, "error": _("没有可用的书源")}
        sources = [{"name": s.bookSourceName, "source": s} for s in targets]
        svc = SearchTaskService()
        return svc.create_task(keyword, sources)

    @AsyncService.register_function
    def get_search_status(self, task_id: str) -> Optional[dict]:
        """查询异步搜索任务进度。"""
        return SearchTaskService().get_status(task_id)

    @AsyncService.register_function
    def search_all(self, keyword: str, timeout: int = 60) -> list:
        """同步搜索所有已启用的书源（等待全部完成）。"""
        import time as _time
        all_sources = self._load_sources()
        enabled = [s for s in all_sources if s.enabled]
        if not enabled:
            return []
        sources = [{"name": s.bookSourceName, "source": s} for s in enabled]
        svc = SearchTaskService()
        result = svc.create_task(keyword, sources)
        task_id = result["task_id"]
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            status = svc.get_status(task_id)
            if status and status["finished"]:
                break
            _time.sleep(0.5)
        status = svc.get_status(task_id) or {}
        if not status.get("finished"):
            logger.warning("search_all 超时（%ds），返回部分结果 %d 条", timeout,
                           len(status.get("results", [])))
        books = []
        for r in status.get("results", []):
            for b in r.get("books", []):
                b["_source"] = r["source_name"]
                books.append(b)
        return books

    @AsyncService.register_function
    def test_source(self, source_name: str) -> dict:
        """测试书源连通性，返回搜索结果示例。"""
        source = self._find_source(source_name)
        if not source:
            raise ValueError(_("书源不存在: %s") % source_name)
        results = self.search(source_name, _("测试"))
        return {
            "source": source_name,
            "reachable": True,
            "sample_count": len(results),
            "samples": results[:3],
        }

    # ── Explore / 分类浏览 ─────────────────────────────────────

    @AsyncService.register_function
    def explore_categories(self, source_name: str) -> list:
        """解析书源的 exploreUrl 返回分类列表。"""
        source = self._find_source(source_name)
        if not source:
            raise ValueError(_("书源不存在: %s") % source_name)
        return parse_explore_categories(source.exploreUrl)

    @AsyncService.register_function
    def explore(self, source_name: str, url: str) -> list:
        """从分类 URL 获取书籍列表。"""
        source = self._find_source(source_name)
        if not source:
            raise ValueError(_("书源不存在: %s") % source_name)
        if not self._is_safe_url(url):
            raise ValueError(_("分类 URL 无效或指向内网地址（已阻止）"))
        return fetch_explore(source, url)

    # ── 下载 ────────────────────────────────────────────────────

    @AsyncService.register_service
    def download_book(
        self,
        source_name: str,
        book_url: str,
        book_title: str = "",
        max_chapters: int = 9999,
        user_id: int = 1,
        callback: Optional[Callable[[int], None]] = None,
    ):
        """异步下载书籍，生成 EPUB 并导入 Calibre。"""
        source = self._find_source(source_name)
        if not source:
            raise ValueError(_("书源不存在: %s") % source_name)

        task_id = self.create_task({"progress": 0, "status": _("初始化")})
        self.set_last_task(user_id, task_id)
        try:
            self._do_download(source, book_url, book_title, max_chapters, user_id, task_id)
        except Exception as exc:
            logger.error("下载失败: %s", exc, exc_info=True)
            self.complete_task(task_id, error_message=str(exc))

    @AsyncService.register_function
    def generate_epub(self, source_name: str, book_url: str,
                      book_title: str = "", max_chapters: int = 9999) -> str:
        """同步生成 EPUB 文件，不导入 Calibre。

        Returns:
            EPUB 文件路径
        """
        source = self._find_source(source_name)
        if not source:
            raise ValueError(_("书源不存在: %s") % source_name)

        detail = fetch_book_info(source, book_url)
        title = book_title or detail.get("name", _("未知书籍"))
        author = detail.get("author", _("未知作者"))
        cover_url = detail.get("coverUrl", "")

        toc = fetch_toc(source, book_url)
        # 卷头行（isVolume 为真）不是实际章节，跳过
        to_download = [e for e in toc if not e.get("isVolume")][:max_chapters]

        chapters = []
        for i, entry in enumerate(to_download, 1):
            ch_title = entry.get("chapterName", _("第 %d 章") % i)
            ch_url = entry.get("chapterUrl", "")
            content = fetch_content(source, ch_url)
            if content:
                chapters.append({"title": ch_title, "content": content, "url": ch_url})

        if not chapters:
            raise ValueError(_("未获取到有效章节内容"))

        safe_name = re.sub(r'[\\/:*?"<>|]', '_', title)
        output_path = os.path.join(self.get_work_dir(book_url), f"{safe_name}.epub")
        return _generate_epub(
            title=title, author=author, chapters=chapters,
            cover_url=cover_url, output_path=output_path,
            referer=source.bookSourceUrl,
        )

    @AsyncService.register_service
    def generate_epub_task(
        self,
        source_name: str,
        book_url: str,
        book_title: str = "",
        max_chapters: int = 9999,
        user_id: int = 1,
    ):
        """异步生成 EPUB（不入库、不清理文件），完成后路径写入任务进度。"""
        source = self._find_source(source_name)
        if not source:
            raise ValueError(_("书源不存在: %s") % source_name)

        task_id = self.create_task({"progress": 0, "status": _("初始化")})
        self.set_last_task(user_id, task_id)
        try:
            self._do_generate_epub(source, book_url, book_title, max_chapters, task_id)
        except Exception as exc:
            logger.error("生成 EPUB 失败: %s", exc, exc_info=True)
            self.complete_task(task_id, error_message=str(exc))

    def get_last_epub_path(self, user_id: int = 1) -> str:
        """返回用户最近一次生成任务的 EPUB 文件路径（未完成/失败返回空）。"""
        task = self.get_last_task(user_id)
        if not task or task.get("status") != BackgroundTask.STATUS_COMPLETED:
            return ""
        progress_data = task.get("progress_data") or {}
        path = progress_data.get("epub_path", "")
        if path and os.path.exists(path):
            return path
        return ""

    def _build_epub(self, source, book_url, book_title, max_chapters, task_id):
        """抓取书籍信息 → 目录 → 逐章正文 → 写出 EPUB。返回 (title, author, epub_path)。

        下载入库与"只生成供下载"两条后台任务只有结尾不同（入库 + 清目录 vs 留着文件），
        前半段完全一致，抽成一个方法避免两边改漏。
        """
        self.update_task_progress(task_id, 5, {"status": _("获取书籍信息")})
        detail = fetch_book_info(source, book_url)
        title = book_title or detail.get("name", _("未知书籍"))
        author = detail.get("author", _("未知作者"))
        cover_url = detail.get("coverUrl", "")

        self.update_task_progress(task_id, 15, {"status": _("获取目录")})
        toc = fetch_toc(source, book_url)
        # 卷头行（isVolume 为真）不是实际章节，跳过
        to_download = [e for e in toc if not e.get("isVolume")][:max_chapters]

        chapters = []
        total = len(to_download)
        for i, entry in enumerate(to_download, 1):
            ch_title = entry.get("chapterName", _("第 %d 章") % i)
            ch_url = entry.get("chapterUrl", "")
            pct = 15 + int(70 * i / max(total, 1))
            self.update_task_progress(
                task_id, pct,
                {"status": _("下载章节 [%d/%d]: %s") % (i, total, ch_title)},
            )
            content = fetch_content(source, ch_url)
            if content:
                chapters.append({"title": ch_title, "content": content, "url": ch_url})

        if not chapters:
            raise ValueError(_("未获取到有效章节内容"))

        self.update_task_progress(task_id, 90, {"status": _("生成 EPUB")})
        work_dir = self.get_work_dir(book_url)
        safe_name = re.sub(r'[\\/:*?"<>|]', '_', title)
        epub_path = os.path.join(work_dir, f"{safe_name}.epub")
        _generate_epub(
            title=title, author=author, chapters=chapters,
            cover_url=cover_url, output_path=epub_path,
            referer=source.bookSourceUrl,
        )
        return title, author, epub_path

    def _do_generate_epub(self, source, book_url: str, book_title: str,
                          max_chapters: int, task_id: int):
        """后台生成 EPUB（不导入 Calibre，产物保留供下载）。"""
        _title, _author, epub_path = self._build_epub(
            source, book_url, book_title, max_chapters, task_id)
        self.update_task_progress(task_id, 100, {"status": _("生成完成"), "epub_path": epub_path})
        self.complete_task(task_id)

    # ── 内部方法 ────────────────────────────────────────────────

    def _load_sources(self):
        """从工具数据目录加载书源列表。"""
        path = self._sources_path()
        if not os.path.exists(path):
            return []
        try:
            return load_sources_from_json(path)
        except Exception as exc:
            logger.error("加载书源失败: %s", exc)
            return []

    def _save_sources(self, sources):
        """原子保存书源列表到工具数据目录（tmp + os.replace）。"""
        path = self._sources_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + ".tmp"
        dump_sources_to_json(sources, tmp_path)
        os.replace(tmp_path, path)

    def _sources_path(self) -> str:
        # TOOL_DATA_ROOT/<tool_id>/sources.json —— 走 CoreAPI.storage
        # （get_work_dir() 不带 key 时就是该工具的根数据目录，并保证目录已创建）
        return os.path.join(self.api.storage.get_work_dir(), "sources.json")

    def _find_source(self, name: str):
        for s in self._load_sources():
            if s.bookSourceName == name:
                return s
        return None

    def _do_download(self, source, book_url: str, book_title: str,
                     max_chapters: int, user_id: int, task_id: int):
        """后台下载任务：抓取 → 生成 EPUB → 入库 → 清理工作目录。"""
        title, author, epub_path = self._build_epub(
            source, book_url, book_title, max_chapters, task_id)

        self.update_task_progress(task_id, 95, {"status": _("导入 Calibre")})
        try:
            self.import_file(user_id, epub_path, title, [author])
            self.complete_task(task_id)
        except Exception as exc:
            logger.error("Calibre 导入失败: %s", exc)
            self.complete_task(task_id, error_message=str(exc))
        finally:
            # import_file 默认 delete_after_import=True，副本已入库，源文件不必留着
            self.cleanup_work_dir(os.path.dirname(epub_path))

    # ── 资源清理 ────────────────────────────────────────────────

    def cleanup(self):
        pass


# ---------------------------------------------------------------------------
# 路由 Handler
# ---------------------------------------------------------------------------
# 宿主 `collect_tool_routes()` 从 manifest.json 的 api_routes 里读 path + handler，把
# handler 类挂到 `/api/toolbox/tool/book_source/<path>`，并且**不会**补鉴权装饰器 ——
# 所以下面每个 JSON 路由都显式写 `@js @is_admin`（顺序与内置工具一致：js 在外层）。
#
# 唯一的例外是 download_epub：它回的是文件字节流不是 JSON 信封，不能用 `@js`；而
# `@is_admin` 在非管理员时返回的是 dict，没有 `@js` 兜底会被 Tornado 当成"返回值不为
# None"报 500。所以按宿主 handlers/toolbox.py 里 `AdminEpubBeautifyBgRaw` 的既有做法：
# 显式判权 + 显式写出响应。


def _json_body(handler) -> dict:
    """解析 JSON 请求体；空 body 或非法 JSON 时返回空 dict（由调用方判必填参数）。"""
    try:
        data = tornado.escape.json_decode(handler.request.body)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _max_chapters(data: dict, default: int = 9999) -> int:
    try:
        return max(1, min(int(data.get("maxChapters", default)), 9999))
    except (TypeError, ValueError):
        return default


# 导入接口的 status -> 响应。三种情况必须分开说，否则"导入成功，新增 0 个书源"会让人以为
# 导入成功了（实际上往往是"压缩包里的文件名不是 importBookSource.json"这种问题）。
_IMPORT_FAILED_STATUS = ("format_error", "no_sources", "fetch_failed")


def _import_response(result: dict) -> dict:
    """把 import_sources_from_zip / import_sources_from_url 的结果翻译成接口响应。"""
    status = result.get("status")
    if status in _IMPORT_FAILED_STATUS:
        return {"err": "book_source.import_failed",
                "msg": result.get("message", _("导入失败")), "data": result}
    added = result.get("added", 0)
    updated = result.get("updated", 0)
    skipped = result.get("skipped", 0)
    if not added and not updated:
        # 认出了书源文件，但一条都没进来（比如全依赖 <js> 规则）—— 也算失败，别报"成功"
        return {"err": "book_source.import_failed",
                "msg": _("没有导入任何书源：跳过 %s 条（多为依赖 JS 规则或格式不完整）") % skipped,
                "data": result}
    return {"err": "ok",
            "msg": _("导入完成：新增 %s，更新 %s，跳过 %s") % (added, updated, skipped),
            "data": result}


def _deps_error(err) -> dict:
    return {"err": "deps.missing", "msg": _("宿主环境缺少依赖：%s") % err}


class _ToolHandler(BaseHandler):
    """把引擎/环境异常统一翻译成 `{"err": ...}` 而不是 500 的公共基类。"""

    def _call(self, fn, *args, **kwargs):
        try:
            return {"err": "ok", "data": fn(*args, **kwargs)}
        except DependencyMissing as err:
            return _deps_error(err)
        except ValueError as err:
            return {"err": "book_source.invalid", "msg": str(err)}


class ListHandler(_ToolHandler):
    """GET list —— 书源列表。"""

    @js
    @is_admin
    def get(self):
        return self._call(BookSourceTool().list_sources)


class SaveHandler(_ToolHandler):
    """POST save —— 新增/更新书源（body: `{raw}`）。"""

    @js
    @is_admin
    def post(self):
        data = _json_body(self)
        raw = data.get("raw")
        if not isinstance(raw, dict) or not (raw.get("bookSourceName") or "").strip():
            return {"err": "params.missing", "msg": _("请提供书源 JSON（含 bookSourceName）")}
        return self._call(BookSourceTool().add_source, raw)


class ToggleHandler(_ToolHandler):
    """POST toggle —— 启用/停用书源（body: `{name}`）。"""

    @js
    @is_admin
    def post(self):
        name = (_json_body(self).get("name") or "").strip()
        if not name:
            return {"err": "params.missing", "msg": _("请提供书源名称")}
        try:
            result = BookSourceTool().toggle_source(name)
        except DependencyMissing as err:
            return _deps_error(err)
        if result.get("status") == "not_found":
            return {"err": "book_source.not_found", "msg": _("书源不存在: %s") % name}
        return {"err": "ok", "data": result}


class DeleteHandler(_ToolHandler):
    """POST delete —— 删除书源（body: `{name}`）。

    ⚠️ 破坏性操作：直接改写 `TOOL_DATA_ROOT/book_source/sources.json`，不可逆（没有回收站）。
    前端在调用前必须弹二次确认（`book_source.vue` 原本的 `showDeleteDialog`）。
    """

    @js
    @is_admin
    def post(self):
        name = (_json_body(self).get("name") or "").strip()
        if not name:
            return {"err": "params.missing", "msg": _("请提供书源名称")}
        try:
            result = BookSourceTool().delete_source(name)
        except DependencyMissing as err:
            return _deps_error(err)
        if result.get("status") == "not_found":
            return {"err": "book_source.not_found", "msg": _("书源不存在: %s") % name}
        return {"err": "ok", "data": result}


class SearchAsyncHandler(_ToolHandler):
    """POST search_async —— 启动多源异步搜索（body: `{keyword, source_names?}`）。"""

    @js
    @is_admin
    def post(self):
        data = _json_body(self)
        keyword = (data.get("keyword") or "").strip()
        if not keyword:
            return {"err": "params.missing", "msg": _("请提供搜索关键词")}
        source_names = data.get("source_names")
        if source_names is not None and not isinstance(source_names, list):
            return {"err": "params.invalid", "msg": _("source_names 应为数组")}
        return self._call(BookSourceTool().search_async, keyword, source_names)


class SearchStatusHandler(_ToolHandler):
    """GET search_status?task_id= —— 搜索进度与结果。"""

    @js
    @is_admin
    def get(self):
        task_id = self.get_argument("task_id", "")
        if not task_id:
            return {"err": "params.missing", "msg": _("缺少 task_id")}
        try:
            status = BookSourceTool().get_search_status(task_id)
        except DependencyMissing as err:
            return _deps_error(err)
        if status is None:
            return {"err": "task.not_found", "msg": _("搜索任务不存在或已过期")}
        return {"err": "ok", "data": status}


class TestHandler(_ToolHandler):
    """GET test?source= —— 单源连通性测试。"""

    @js
    @is_admin
    def get(self):
        name = self.get_argument("source", "")
        if not name:
            return {"err": "params.missing", "msg": _("请提供书源名称")}
        return self._call(BookSourceTool().test_source, name)


class DownloadHandler(_ToolHandler):
    """POST download —— 下载书籍并入库（异步任务，立即返回）。"""

    @js
    @is_admin
    def post(self):
        data = _json_body(self)
        source = (data.get("source") or "").strip()
        book_url = (data.get("bookUrl") or "").strip()
        if not source or not book_url:
            return {"err": "params.missing", "msg": _("请提供书源与书籍地址")}
        try:
            BookSourceTool().download_book(
                source_name=source,
                book_url=book_url,
                book_title=data.get("bookTitle", ""),
                max_chapters=_max_chapters(data),
                user_id=self.user_id(),
            )
        except DependencyMissing as err:
            return _deps_error(err)
        return {"err": "ok", "msg": _("下载任务已启动，右上角可以查看进度")}


class GenerateEpubHandler(_ToolHandler):
    """POST generate_epub —— 生成 EPUB 供下载（异步任务，立即返回）。"""

    @js
    @is_admin
    def post(self):
        data = _json_body(self)
        source = (data.get("source") or "").strip()
        book_url = (data.get("bookUrl") or "").strip()
        if not source or not book_url:
            return {"err": "params.missing", "msg": _("请提供书源与书籍地址")}
        try:
            BookSourceTool().generate_epub_task(
                source_name=source,
                book_url=book_url,
                book_title=data.get("bookTitle", ""),
                max_chapters=_max_chapters(data),
                user_id=self.user_id(),
            )
        except DependencyMissing as err:
            return _deps_error(err)
        return {"err": "ok", "msg": _("EPUB 生成任务已启动，右上角可以查看进度")}


class CancelHandler(_ToolHandler):
    """POST cancel —— 取消后台任务（body: `{task_id}`）。

    只允许取消"当前用户最近一次本工具任务"：宿主的 `BackgroundService.cancel_task()`
    只按 task_id 取消、不校验归属，直接透传会让任一管理员按顺序猜 id 取消别人的任务。
    """

    @js
    @is_admin
    def post(self):
        data = _json_body(self)
        task_id = data.get("task_id")
        if not task_id:
            return {"err": "params.missing", "msg": _("缺少 task_id")}
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            return {"err": "params.invalid", "msg": _("task_id 应为整数")}

        mine = BookSourceTool.get_last_task(self.user_id()) or {}
        if mine.get("id") != task_id:
            return {"err": "task.not_found", "msg": _("任务不存在或不属于当前用户")}

        if not BackgroundService().cancel_task(task_id):
            return {"err": "task.not_found", "msg": _("任务不存在或已结束")}
        return {"err": "ok", "msg": _("任务已取消")}


class ProgressHandler(_ToolHandler):
    """GET progress —— 当前用户最近一次任务进度。"""

    @js
    @is_admin
    def get(self):
        task = BookSourceTool.get_last_task(self.user_id())
        if not task:
            return {"err": "ok", "data": None}
        return {
            "err": "ok",
            "data": {
                "task_id": task.get("id"),
                "progress": task.get("progress", 0),
                "status": task.get("status"),
                "progress_data": task.get("progress_data") or {},
            },
        }


class ImportZipHandler(_ToolHandler):
    """POST import_zip —— 上传 zip 导入书源（multipart `file`）。"""

    @js
    @is_admin
    def post(self):
        if not self.request.files or 'file' not in self.request.files:
            return {"err": "params.missing", "msg": _("未上传文件")}

        file_meta = self.request.files['file'][0]
        suffix = os.path.splitext(file_meta['filename'])[1] or ".zip"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(file_meta['body'])
            tmp_path = f.name
        try:
            result = BookSourceTool().import_sources_from_zip(tmp_path)
        except DependencyMissing as err:
            return _deps_error(err)
        except (ValueError, RuntimeError) as err:
            return {"err": "book_source.import_failed", "msg": str(err)}
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        return _import_response(result)


class ImportUrlHandler(_ToolHandler):
    """POST import_url —— 从 URL 导入书源（body: `{url}`）。"""

    @js
    @is_admin
    def post(self):
        url = (_json_body(self).get("url") or "").strip()
        if not url:
            return {"err": "params.missing", "msg": _("网址不能为空")}
        try:
            result = BookSourceTool().import_sources_from_url(url)
        except DependencyMissing as err:
            return _deps_error(err)
        except (ValueError, RuntimeError) as err:
            return {"err": "book_source.import_failed", "msg": str(err)}
        return _import_response(result)


class DownloadEpubHandler(BaseHandler):
    """GET download_epub —— 下载最近一次生成的 EPUB（`application/epub+zip`）。

    回的是字节流，不能用 `@js`（见文件头说明），所以显式判权 + 显式写响应。
    """

    def get(self):
        if not self.current_user:
            self._deny(401, _("请先登录"))
            return
        if not self.admin_user:
            self._deny(403, _("当前用户非管理员, 无权限操作"))
            return

        try:
            epub_path = BookSourceTool().get_last_epub_path(self.user_id())
        except DependencyMissing as err:
            self._deny(500, str(err))
            return

        if not epub_path or not os.path.exists(epub_path):
            self._deny(404, "EPUB not found")
            return

        filename = os.path.basename(epub_path)
        ascii_name = re.sub(r'[^\x00-\x7f]', '_', filename) or "book.epub"
        self.set_header('Content-Type', 'application/epub+zip')
        self.set_header(
            'Content-Disposition',
            'attachment; filename="%s"; filename*=UTF-8\'\'%s' % (ascii_name, quote(filename)))
        with open(epub_path, 'rb') as f:
            self.write(f.read())

    def _deny(self, status: int, message: str) -> None:
        self.set_status(status)
        self.set_header("Content-Type", "text/plain; charset=utf-8")
        self.write(message)


class DepsHandler(_ToolHandler):
    """GET deps —— 运行环境能力探测（缺哪个依赖、JS 规则/EPUB 是否可用）。"""

    @js
    @is_admin
    def get(self):
        return {"err": "ok", "data": BookSourceTool.capabilities()}


# 供本地测试/文档引用；宿主实际用的是 manifest.json 里的 api_routes 声明
BOOK_SOURCE_ROUTES = [
    (r"/api/toolbox/tool/book_source/list", ListHandler),
    (r"/api/toolbox/tool/book_source/save", SaveHandler),
    (r"/api/toolbox/tool/book_source/toggle", ToggleHandler),
    (r"/api/toolbox/tool/book_source/delete", DeleteHandler),
    (r"/api/toolbox/tool/book_source/search_async", SearchAsyncHandler),
    (r"/api/toolbox/tool/book_source/search_status", SearchStatusHandler),
    (r"/api/toolbox/tool/book_source/test", TestHandler),
    (r"/api/toolbox/tool/book_source/download", DownloadHandler),
    (r"/api/toolbox/tool/book_source/generate_epub", GenerateEpubHandler),
    (r"/api/toolbox/tool/book_source/cancel", CancelHandler),
    (r"/api/toolbox/tool/book_source/progress", ProgressHandler),
    (r"/api/toolbox/tool/book_source/import_zip", ImportZipHandler),
    (r"/api/toolbox/tool/book_source/import_url", ImportUrlHandler),
    (r"/api/toolbox/tool/book_source/download_epub", DownloadEpubHandler),
    (r"/api/toolbox/tool/book_source/deps", DepsHandler),
]
