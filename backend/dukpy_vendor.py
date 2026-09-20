# -*- coding: utf-8 -*-
"""把随包内置的 dukpy（`@js:` 规则要用的 JS 引擎）挂到 `sys.modules["dukpy"]` 上。

## 为什么需要这个文件

MyBooks 的 `requirements.txt` 里**没有** dukpy，而外部工具改不了宿主依赖。不装 dukpy 时
引擎里的 `js_runtime` 会走 `_HAS_DUKPY = False` 的降级路径：依赖 `@js:` 规则的书源全部
不可用。`backend/vendor/dukpy/` 里按平台放了裁剪过的 dukpy 副本（生成脚本
`scripts/fetch_vendor_wheels.py`），这里负责在引擎 import **之前**把它按当前解释器挑出来装上。

## 原生模块为什么不能只在 zip 里

dukpy 的关键是原生扩展（Linux `.so` / Windows `.pyd`），**和 CPython 的 ABI 绑定**
（cp312 编的加载不进 cp313），所以：

- 每个平台+ABI 一个目录：`vendor/dukpy/cp312-manylinux_x86_64/dukpy/…`
- 运行时按 `sys.version_info` + `sys.platform` + `platform.machine()` 选；
- 选不到就**如实降级**（不抛异常——外部工具加载失败会让工具从列表里消失），
  由 `GET /deps` 把"包里带了哪些、当前解释器要哪个"报给前端。

原生模块也不需要解压到临时目录：`sys.path` 之外用 `importlib` 直接把包目录当 `__path__`
加载即可（`spec_from_file_location(..., submodule_search_locations=[...])`），
这样运行期**不写任何文件**（对外部工具的"文件读写只在自己目录内"要求更友好）。

## 优先级

1. 宿主已有的 dukpy（最理想：跟宿主 Python 完全匹配）
2. `backend/vendor/dukpy/<tag>/`（随包副本）
3. 环境变量 `BOOK_SOURCE_VENDOR_DIR` 指的目录（`os.pathsep` 分隔多个，各自期望
   `<tag>/dukpy/…` 结构）—— 给"不在随包矩阵里的平台"（macOS / musl / 其它 Python 版本）
   一个自助补位口子，比如自己 `pip download` 一个 wheel 解成同样的形状。
4. 都没有 → `missing`，保持引擎原有的降级行为。
"""
import importlib.util
import os
import platform
import struct
import sys

VENDOR_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "dukpy")
MANIFEST_NAME = "VENDOR.json"

EXTRA_DIRS_ENV = "BOOK_SOURCE_VENDOR_DIR"
# 强制用包内副本（跳过"宿主已装 dukpy 就优先用宿主"这条）。默认不开：
# 宿主那份跟宿主 Python 完全自洽，正常情况下更可信；这个开关是给
# "宿主那份 dukpy 版本不对/装坏了" 时用的排查口子（也会被测试用来走通随包那条路）。
PREFER_BUNDLED_ENV = "BOOK_SOURCE_PREFER_BUNDLED"

_TRUTHY = ("1", "true", "yes", "on")

# 进程内只装一次；describe() 用它回报"到底用了哪一份"
_STATE = None  # type: dict


def platform_tag() -> "str | None":
    """当前解释器对应的副本目录名，如 `cp312-manylinux_x86_64`。

    和 `scripts/fetch_vendor_wheels.py` 的 tag 命名保持一致：`cp<major><minor>-<平台>_<架构>`。
    纯 CPython 之外（PyPy 等）没有对应 wheel，返回 None。
    """
    if sys.implementation.name != "cpython":
        return None
    if sys.platform.startswith("linux"):
        plat = "linux"
    elif sys.platform == "win32":
        plat = "win"
        if struct.calcsize("P") != 8:
            return None  # 32 位解释器：dukpy 没有 win32 的 cp312+ wheel
    elif sys.platform == "darwin":
        plat = "macos"
    else:
        return None
    machine = platform.machine().lower()
    if not machine:
        return None
    return "cp%d%d-%s_%s" % (sys.version_info[0], sys.version_info[1], plat, machine)


def bundled_tags():
    """随包到底带了哪些平台（给前端解释"为什么没匹配上"用）。"""
    if not os.path.isdir(VENDOR_ROOT):
        return []
    return sorted(
        name for name in os.listdir(VENDOR_ROOT)
        if os.path.isdir(os.path.join(VENDOR_ROOT, name, "dukpy"))
    )


def _extra_roots():
    raw = os.environ.get(EXTRA_DIRS_ENV, "")
    return [os.path.abspath(p) for p in raw.split(os.pathsep) if p.strip()]


def _looks_like_package(root: str, tag: str) -> bool:
    return os.path.isfile(os.path.join(root, tag, "dukpy", "__init__.py"))


def _load_package(root: str, tag: str):
    """把 `<root>/<tag>/dukpy/` 作为包 `dukpy` 装进 sys.modules。返回 (module, error)。"""
    package_dir = os.path.join(root, tag, "dukpy")
    init_file = os.path.join(package_dir, "__init__.py")
    spec = importlib.util.spec_from_file_location(
        "dukpy", init_file, submodule_search_locations=[package_dir])
    if spec is None or spec.loader is None:
        return None, "无法为 %s 建 spec" % init_file
    module = importlib.util.module_from_spec(spec)
    # 先登记再执行：dukpy/__init__.py 里有 `from . import _dukpy`，相对导入要求包里已有自己
    sys.modules["dukpy"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as err:  # ABI 不匹配、glibc 太旧、文件损坏都会落到这里
        sys.modules.pop("dukpy", None)
        return None, "%s: %s" % (type(err).__name__, err)
    return module, ""


def install(extra_dirs=None) -> dict:
    """挑一份可用的 dukpy 装上（幂等）。**不会抛异常**，失败时回 `source="missing"`。

    :return: `{"source": "host"|"bundled"|"external"|"missing", "tag", "path", "error"}`
    """
    global _STATE
    if _STATE is not None:
        return _STATE

    state = {"source": "missing", "tag": "", "path": "", "error": "",
             "requested_tag": platform_tag() or "", "bundled_tags": bundled_tags(),
             "prefer_bundled": os.environ.get(PREFER_BUNDLED_ENV, "").strip().lower() in _TRUTHY}

    # 1) 宿主已经有一份能用的（可用 BOOK_SOURCE_PREFER_BUNDLED 跳过本步）
    prefer_bundled = os.environ.get(PREFER_BUNDLED_ENV, "").strip().lower() in _TRUTHY
    if not prefer_bundled and ("dukpy" in sys.modules or _host_has_dukpy()):
        state["source"] = "host"
        state["path"] = "<host site-packages>"
        _STATE = state
        return state

    tag = state["requested_tag"]
    if not tag:
        state["error"] = "当前解释器（%s / %s）不在随包矩阵内" % (
            sys.implementation.name, sys.version.split()[0])
        _STATE = state
        return state

    roots = [VENDOR_ROOT] + (list(extra_dirs) if extra_dirs else _extra_roots())
    errors = []
    for root in roots:
        if not root or not _looks_like_package(root, tag):
            continue
        module, error = _load_package(root, tag)
        if module is not None:
            state.update(source="bundled" if root == VENDOR_ROOT else "external",
                         tag=tag, path=os.path.join(root, tag))
            _STATE = state
            return state
        errors.append("%s → %s" % (os.path.join(root, tag), error))

    state["error"] = ("包里没有 %s 的 dukpy 副本（随包矩阵：%s）"
                      % (tag, ", ".join(state["bundled_tags"]) or "空"))
    if errors:
        detail = " / ".join(errors)
        if sys.platform.startswith("linux"):
            # 最常见的失败原因是 libc 不匹配：随包副本是 manylinux(glibc) 构建的
            detail += "（随包副本是 manylinux/glibc 构建：musl 系统（Alpine）或 glibc 过旧时加载不了）"
        state["error"] += "；尝试过但加载失败：" + detail
    _STATE = state
    return state


def _host_has_dukpy() -> bool:
    try:
        return importlib.util.find_spec("dukpy") is not None
    except (ImportError, ValueError, AttributeError):
        return False


def describe() -> dict:
    """给 `GET /deps` 用的当前状态（未 install 过也会给出"能不能装"的预判）。"""
    if _STATE is not None:
        return dict(_STATE)
    tag = platform_tag() or ""
    return {
        "source": "unknown",
        "tag": "",
        "path": "",
        "error": "",
        "requested_tag": tag,
        "bundled_tags": bundled_tags(),
    }


def reset_state():
    """仅供测试：忘掉进程内的安装结果（不动 sys.modules）。"""
    global _STATE
    _STATE = None
