# -*- coding: utf-8 -*-
"""把上游 `book-source-engine` 仓库的「内置插件形态」源码搬运/转换成外置工具包内容。

这个脚本只做三件机械的事，方便随时重跑对账：

1. `backend/engine/` —— 从原始形态分支的
   `书源引擎插件/webserver/toolbox/book_source_engine/`
   逐字节拷贝（引擎一行不改，改的只有 import 之外的地方，而 import 本来就是包内相对引用）。
2. `frontend/locales/*.json` —— 从上游 `书源引擎插件/locales/*.json` 的嵌套结构
   （`{"bookSource": {...}}`）拍平成脚手架约定的扁平键（`"bookSource.xxx"`），
   并补上上游漏掉的 3 个键 + 本工具包新增的依赖提示键。
3. `tests/test_engine.py` —— 上游引擎单测，只改导入那几行（其余逐字节一致），
   这样"上游 100+ 个用例在这份工具包里照样全绿"就是对账证据。

用法：
    python scripts/port_from_upstream.py <上游插件目录>
    # 上游插件目录 = 原始形态分支里的 `书源引擎插件/`
    # 例：git clone -b legacy-project https://github.com/shiningsprk-arch/tool_book_source /tmp/bse
    #     python scripts/port_from_upstream.py /tmp/bse/书源引擎插件
    # （本仓库 main 已经是外置工具包，原始形态在 legacy-project 分支 / tag
    #   source-engine-archive-20260920 里）

不做的事：`backend/tool.py` 与 `frontend/index.html` / `frontend/app.js` 是重写过的，
不在这里生成（改动说明见仓库根 NOTICE.md）。
"""
import hashlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(HERE)

ENGINE_SRC_REL = os.path.join("webserver", "toolbox", "book_source_engine")
ENGINE_DST_REL = os.path.join("backend", "engine")

# 上游三个 locale 文件里都缺这三个键，而 Vue 页面确实在用（界面上会直接漏出原始 key）。
# 文案按上游同文件的风格补写；上游修掉之后这里会以"上游已有"为准，不会覆盖。
EXTRA_STRINGS = {
    "searchHint": {
        "zh": "共 {n} 个可用书源",
        "en": "{n} enabled source(s)",
        "zh-TW": "共 {n} 個可用書源",
    },
    "confirmDownloadAllMsg": {
        "zh": "将依次下载当前搜索结果的 {n} 本书，确定继续？",
        "en": "This will download {n} book(s) from the current results. Continue?",
        "zh-TW": "將依序下載目前搜尋結果的 {n} 本書，確定繼續？",
    },
    "testReachable": {
        "zh": "连通正常，返回 {n} 条结果",
        "en": "Reachable, {n} result(s) returned",
        "zh-TW": "連線正常，回傳 {n} 筆結果",
    },
}

# 本工具包新增的键：外部工具不能改宿主的 requirements.txt，缺依赖只能在运行时如实上报。
DEPS_STRINGS = {
    "depsEngineMissing": {
        "zh": "规则引擎加载失败：{error}",
        "en": "Rule engine failed to load: {error}",
        "zh-TW": "規則引擎載入失敗：{error}",
    },
    "depsNoJs": {
        "zh": "JS 规则不可用：宿主未安装 dukpy，包内也没有匹配当前解释器的副本",
        "en": "JS rules unavailable: dukpy is not installed on the host and no bundled copy matches this interpreter",
        "zh-TW": "JS 規則不可用：宿主未安裝 dukpy，包內也沒有符合目前解譯器的副本",
    },
    "depsNoJsDetail": {
        "zh": "内置副本适用于 {bundled}，当前解释器需要 {wanted}；可设 BOOK_SOURCE_VENDOR_DIR 指向自备副本",
        "en": "Bundled copies cover {bundled}, this interpreter needs {wanted}; set BOOK_SOURCE_VENDOR_DIR to a self-provided copy",
        "zh-TW": "內建副本適用於 {bundled}，目前解譯器需要 {wanted}；可設 BOOK_SOURCE_VENDOR_DIR 指向自備副本",
    },
    "depsNoEpub": {
        "zh": "未安装 ebooklib，EPUB 生成不可用",
        "en": "ebooklib is not installed; EPUB generation is unavailable",
        "zh-TW": "未安裝 ebooklib，EPUB 產生功能無法使用",
    },
    "depsMissingRequired": {
        "zh": "缺少必需依赖：{name}",
        "en": "Missing required dependency: {name}",
        "zh-TW": "缺少必需相依套件：{name}",
    },
}

LOCALES = ("zh", "en", "zh-TW")
NAMESPACE = "bookSource"


def md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def copy_engine(upstream_dir):
    src = os.path.join(upstream_dir, ENGINE_SRC_REL)
    dst = os.path.join(PKG_ROOT, ENGINE_DST_REL)
    if not os.path.isdir(src):
        raise SystemExit("上游目录里找不到 %s" % src)
    os.makedirs(dst, exist_ok=True)

    copied = []
    for name in sorted(os.listdir(src)):
        if not name.endswith(".py"):
            continue
        src_file = os.path.join(src, name)
        with open(src_file, "rb") as f:
            data = f.read()
        # ⚠️ Windows 上 git 默认 core.autocrlf=true，**工作区**里是 CRLF，而仓库里的 blob 是 LF。
        # 这里统一成 LF，拷贝结果才等于上游的 blob（"逐字节一致"要以 blob 为准），也才符合本包
        # `.gitattributes` 的 `* text=auto eol=lf` 与打包的确定性要求。
        data = data.replace(b"\r\n", b"\n")
        with open(os.path.join(dst, name), "wb") as f:
            f.write(data)
        copied.append((name, hashlib.md5(data).hexdigest(), len(data)))

    print("engine/%d 个文件已同步（统一 LF 后逐字节拷贝，md5 如下）：" % len(copied))
    for name, digest, size in copied:
        print("  %-24s %s  %6d B" % (name, digest, size))
    return copied


def build_locales(upstream_dir):
    src_dir = os.path.join(upstream_dir, "locales")
    dst_dir = os.path.join(PKG_ROOT, "frontend", "locales")
    os.makedirs(dst_dir, exist_ok=True)

    for locale in LOCALES:
        with io.open(os.path.join(src_dir, "%s.json" % locale), encoding="utf-8") as f:
            nested = json.load(f)
        flat = {}
        for key, value in nested[NAMESPACE].items():
            flat["%s.%s" % (NAMESPACE, key)] = value

        added = []
        for extra in (EXTRA_STRINGS, DEPS_STRINGS):
            for key, by_locale in sorted(extra.items()):
                full = "%s.%s" % (NAMESPACE, key)
                if full in flat:
                    continue
                flat[full] = by_locale[locale]
                added.append(key)

        ordered = dict(sorted(flat.items(), key=lambda kv: kv[0]))
        out = os.path.join(dst_dir, "%s.json" % locale)
        with io.open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(ordered, ensure_ascii=False, indent=2))
            f.write("\n")
        print("%s.json: %d 个键（上游 %d + 新增 %d：%s）"
              % (locale, len(ordered), len(nested[NAMESPACE]), len(added), ", ".join(added) or "无"))


def write_locales_manifest():
    path = os.path.join(PKG_ROOT, "frontend", "locales", "manifest.json")
    payload = {"default": "zh", "locales": list(LOCALES)}
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(payload, ensure_ascii=False, indent=2))
        f.write("\n")
    print("locales/manifest.json: %s" % payload)


def copy_upstream_tests(upstream_dir):
    """把上游 1183 行的引擎单测搬过来，只改导入那几行，其余逐字节一致。

    改动内容：`sys.path` 指向包根（这样 `backend.engine.*` 可导入）+
    `from webserver.toolbox.book_source_engine.X` → `from backend.engine.X`。
    引擎没改，所以上游这 100 多个用例原样跑得过就是最直接的对账证据。
    """
    src = os.path.join(upstream_dir, "tests", "test_book_source_engine.py")
    dst = os.path.join(PKG_ROOT, "tests", "test_engine.py")
    if not os.path.exists(src):
        raise SystemExit("上游目录里找不到 %s" % src)

    with io.open(src, encoding="utf-8") as f:
        original = f.read()

    text = original.replace(
        'sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))\n',
        '# ── 以下到本文件末尾由 scripts/port_from_upstream.py 生成 ─────────────────\n'
        '# 除紧邻的这几行导入改动外，与上游 `书源引擎插件/tests/test_book_source_engine.py`\n'
        '# 逐字节一致；要改先确认该改的是不是上游那份。\n'
        'sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))\n'
        '# 本工具包里引擎位于 backend/engine/，所以导入路径是 backend.engine.*\n',
    ).replace(
        # 该文件里全部 27 处对上游客源包的引用都写成这个点分模块路径（`from ... import ...`、
        # mock.patch 的字符串 target、__import__ 的参数），没有一处写成文件路径形态，
        # 所以整体替换是安全的
        "webserver.toolbox.book_source_engine",
        "backend.engine",
    )

    if text == original:
        raise SystemExit("导入行替换没有命中，上游文件结构可能变了，请人工核对")

    with io.open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)

    # 用真正的 diff 统计改动行数：只有上面那几行被替换/新增过
    import difflib
    diff = [
        line for line in difflib.unified_diff(original.splitlines(), text.splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    print("tests/test_engine.py: 已生成（上游 %d 行，改动 %d 行）"
          % (len(original.splitlines()), len(diff)))

    # 测试夹具（得奇小说书源、示例书源）
    src_data = os.path.join(upstream_dir, "tests", "data")
    dst_data = os.path.join(PKG_ROOT, "tests", "data")
    if not os.path.isdir(src_data):
        raise SystemExit("上游目录里找不到 %s" % src_data)
    os.makedirs(dst_data, exist_ok=True)
    names = sorted(n for n in os.listdir(src_data) if n.endswith(".json"))
    for name in names:
        with open(os.path.join(src_data, name), "rb") as f:
            data = f.read()
        with open(os.path.join(dst_data, name), "wb") as f:
            f.write(data)
    print("tests/data/: %s" % ", ".join(names))


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__.strip().split("用法：")[-1].strip())
    upstream_dir = os.path.abspath(sys.argv[1])
    print("上游插件目录: %s\n" % upstream_dir)
    copy_engine(upstream_dir)
    print()
    build_locales(upstream_dir)
    print()
    write_locales_manifest()
    print()
    copy_upstream_tests(upstream_dir)


if __name__ == "__main__":
    main()
