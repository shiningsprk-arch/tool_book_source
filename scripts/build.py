# -*- coding: utf-8 -*-
"""打包 + 形状校验：产出 `dist/<tool_id>-<revision>.zip`。

和官方脚手架 `mytool build` 的关系：
- 包内**结构**完全按官方约定 —— `manifest.json` + `icon.png` + `backend/**` + `frontend/**`；
- 额外多两条顶层条目 `LICENSE` / `NOTICE.md`。`mytool build` 不打包这两个文件，但本工具包
  是 MIT 项目（`shiningsprk-arch/tool_book_source`，原名 `book-source-engine`）的衍生作品，
  MIT 要求"版权声明随副本
  一起分发"，所以让它们跟着包走。宿主安装时对顶层条目没有白名单限制（只做路径穿越防护），
  多这两条不影响安装 —— 校验方式见下：打完包会把产物再交给官方 `mytool validate` 跑一遍。
- 打包用 stdlib `zipfile` 而不是 adm-zip，是为了拿到**跨平台可复现**的产物：固定时间戳
  （取自 manifest 的 publish_date）、稳定条目顺序、固定权限位、固定 zip 头的 "version made by"
  宿主字段（详见 build() 里的注释）。本机（Windows/cp313）与 CI（ubuntu/cp312）各打一遍，
  sha256 相同 —— 所以变更记录/商店登记里记的那个哈希对谁都成立。
  （注：deflate 压缩流本身依赖 zlib 版本，同一台机器上重复打包必然一致，这一点由 CI 的
  "打两遍比字节"守住。）

用法：
    python scripts/build.py             # 校验 + 打包 + 打印 sha256 + 官方 validate
    python scripts/build.py --check     # 只校验项目目录，不打包
    python scripts/build.py --no-mytool # 跳过官方脚手架那一遍校验（离线环境）
"""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(HERE)

# zip 顶层允许出现的条目（其余一律视为"不该出现在包里的"）
ROOT_ENTRIES = ("manifest.json", "icon.png", "LICENSE", "NOTICE.md", "backend", "frontend")
ROOT_FILES = ("manifest.json", "icon.png", "LICENSE", "NOTICE.md")

REQUIRED_MANIFEST_FIELDS = (
    "tool_id", "name", "description", "revision", "author",
    "core_api_version", "entry_backend", "repo_url",
)
TOOL_ID_RE = re.compile(r"^[a-z0-9_]+$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

EXCLUDE_RE = re.compile(r"(^|[\\/])(__pycache__[\\/]|[^\\/]*\.py[co]$|\.DS_Store$)")

# 换行符检查用：这些后缀当二进制，绝不改写
BINARY_EXTS = (".so", ".pyd", ".dll", ".png", ".jpg", ".jpeg", ".ico", ".zip", ".7z", ".gz")
SKIP_DIRS = {".git", "dist", "__pycache__", ".pytest_cache", ".tox", ".venv", "vendor-extra"}


class BuildError(SystemExit):
    pass


def fail(message):
    raise BuildError("✗ %s" % message)


def iter_text_files():
    """包内所有**文本**文件（跳过大目录与二进制后缀）。"""
    for root, dirs, files in os.walk(PKG_ROOT):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for fn in sorted(files):
            if fn.endswith(BINARY_EXTS):
                continue
            yield os.path.join(root, fn)


def check_eol(fix=False):
    """文本文件必须全是 LF。

    原因：打包是**确定性**的（固定时间戳 + 稳定排序），sha256 会被记进变更记录、商店登记和
    `backend/vendor/dukpy/VENDOR.json` 的哈希清单。Windows 上 git 默认 `core.autocrlf=true`，
    一旦有 CRLF 混进来：① 仓库里的字节和别人 clone 下来的字节不一致，同一个 commit 打出不同
    哈希；② 随包依赖的哈希清单在别人机器上直接校验失败。
    `.gitattributes` 里已经写了 `* text=auto eol=lf`（含 checkout），这里再拦一道是因为
    **在本地新建/改写文件**（编辑器、脚本）也会引入 CRLF，而那一步 git 管不到。
    """
    crlf = []
    for path in iter_text_files():
        try:
            data = open(path, "rb").read()
        except OSError:
            continue
        if b"\r\n" in data:
            crlf.append(path)
    if not crlf:
        print("✔ 文本文件换行符统一（全 LF）")
        return

    if fix:
        for path in crlf:
            with open(path, "rb") as f:
                data = f.read()
            with open(path, "wb") as f:
                f.write(data.replace(b"\r\n", b"\n"))
        print("✔ 已把 %d 个文本文件的 CRLF 改成 LF（记得重跑 fetch_vendor_wheels.py --rehash "
              "重算随包依赖的哈希清单）" % len(crlf))
        return

    preview = "\n".join("    %s" % os.path.relpath(p, PKG_ROOT) for p in crlf[:8])
    fail("%d 个文本文件含 CRLF：\n%s%s\n  跑 `python scripts/build.py --fix-eol` 修掉"
         % (len(crlf), preview, "\n    …" if len(crlf) > 8 else ""))


def read_manifest():
    path = os.path.join(PKG_ROOT, "manifest.json")
    if not os.path.exists(path):
        fail("找不到 manifest.json")
    with io.open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as err:
            fail("manifest.json 不是合法 JSON：%s" % err)


def module_file_for(module_rel):
    return os.path.join(PKG_ROOT, "backend", *module_rel.split(".")) + ".py"


def validate_manifest(manifest):
    """与官方 `tools_builder/src/manifest.js` 的 validateManifestObject 对齐。"""
    missing = [f for f in REQUIRED_MANIFEST_FIELDS if not manifest.get(f)]
    if missing:
        fail("manifest.json 缺少必填字段：%s" % "、".join(missing))
    if not TOOL_ID_RE.match(manifest["tool_id"]):
        fail("tool_id 格式不合法：只允许小写字母、数字、下划线")
    for field in ("revision", "core_api_version"):
        if not SEMVER_RE.match(manifest[field]):
            fail("%s 不是合法的语义化版本号（x.y.z）：%s" % (field, manifest[field]))
    if "." not in manifest["entry_backend"]:
        fail("entry_backend 格式应为 <module>.<ClassName>")
    if not re.match(r"^https?://", manifest.get("repo_url", "")):
        fail("repo_url 不是合法的 URL：%s" % manifest.get("repo_url"))
    if not manifest.get("entry_frontend"):
        fail("未声明 entry_frontend，宿主 iframe 会加载失败")
    if not manifest.get("page"):
        fail("未声明 page")
    routes = manifest.get("api_routes") or []
    if not isinstance(routes, list):
        fail("api_routes 必须是数组")
    for i, entry in enumerate(routes):
        if not isinstance(entry, dict) or not entry.get("path") or not entry.get("handler"):
            fail("api_routes[%d] 必须同时包含 path 和 handler" % i)
    locales = manifest.get("locales") or []
    if manifest.get("default_locale") and manifest["default_locale"] not in locales:
        fail("default_locale 不在 locales 列表里")


def validate_entries(manifest):
    """entry_backend / entry_frontend / api_routes 指向的文件都必须真实存在。"""
    backend_mod, backend_cls = manifest["entry_backend"].rsplit(".", 1)
    backend_file = module_file_for(backend_mod)
    if not os.path.exists(backend_file):
        fail("找不到 entry_backend 指向的模块文件：backend/%s.py" % backend_mod.replace(".", "/"))

    frontend_entry = os.path.join(PKG_ROOT, "frontend", manifest["entry_frontend"])
    if not os.path.exists(frontend_entry):
        fail("找不到 entry_frontend 指向的文件：frontend/%s" % manifest["entry_frontend"])

    # handler 的模块文件必须存在，且类名能在该模块里找到（AST 静态检查，不 import：
    # 这些模块 import 了 webserver.*，脱离宿主环境 import 不了）
    import ast
    source = io.open(backend_file, encoding="utf-8").read()
    tree = ast.parse(source)
    classes = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    if backend_cls not in classes:
        fail("backend/%s.py 里找不到 entry_backend 指向的类 %s" % (backend_mod, backend_cls))

    for entry in manifest.get("api_routes", []):
        handler_mod, handler_cls = entry["handler"].rsplit(".", 1)
        handler_file = module_file_for(handler_mod)
        if not os.path.exists(handler_file):
            fail("api_routes 的 handler %s 指向的模块不存在：backend/%s.py"
                 % (entry["handler"], handler_mod.replace(".", "/")))
        if handler_mod == backend_mod:
            handler_classes = classes
        else:
            handler_classes = {
                node.name for node in ast.walk(ast.parse(io.open(handler_file, encoding="utf-8").read()))
                if isinstance(node, ast.ClassDef)
            }
        if handler_cls not in handler_classes:
            fail("backend/%s.py 里找不到 api_routes 声明的 handler 类 %s" % (handler_mod, handler_cls))

    print("✔ entry_backend / entry_frontend / %d 条 api_routes 指向的文件与类都存在"
          % len(manifest.get("api_routes", [])))


def validate_frontend_assets(manifest):
    """index.html 里相对引用的静态资源必须都在 frontend/ 下真实存在（否则宿主里会 404）。"""
    frontend_dir = os.path.join(PKG_ROOT, "frontend")
    index_path = os.path.join(frontend_dir, manifest["entry_frontend"])
    html = io.open(index_path, encoding="utf-8").read()

    refs = set(re.findall(r'(?:src|href)="([^"]+)"', html))
    missing = []
    for ref in refs:
        if ref.startswith(("http://", "https://", "/", "#", "data:", "//")):
            continue  # 宿主提供的 /static/... 之类
        target = os.path.normpath(os.path.join(frontend_dir, ref))
        if not os.path.exists(target):
            missing.append(ref)
    if missing:
        fail("frontend/%s 引用了不存在的资源：%s" % (manifest["entry_frontend"], "、".join(sorted(missing))))

    # 语言包目录声明与 locales/*.json 必须一一对应
    locales_dir = os.path.join(frontend_dir, "locales")
    locale_manifest_path = os.path.join(locales_dir, "manifest.json")
    if not os.path.exists(locale_manifest_path):
        fail("缺少 frontend/locales/manifest.json")
    declared = json.load(io.open(locale_manifest_path, encoding="utf-8"))
    for code in declared.get("locales", []):
        if not os.path.exists(os.path.join(locales_dir, "%s.json" % code)):
            fail("frontend/locales/manifest.json 声明了 %s 但没有对应文件" % code)
    for name in os.listdir(locales_dir):
        if name.endswith(".json") and name != "manifest.json":
            code = name[:-5]
            if code not in declared.get("locales", []):
                fail("frontend/locales/%s 没有在 manifest.json 里声明" % name)
    if declared.get("default") not in declared.get("locales", []):
        fail("frontend/locales/manifest.json 的 default 不在 locales 列表里")

    print("✔ 前端 %d 个相对引用 + %d 个语言包都在位"
          % (len([r for r in refs if not r.startswith(("http", "/", "#", "data:", "//"))]), len(declared.get("locales", []))))


def iter_package_files(include_vendor=True):
    """产出 (zip 内路径, 磁盘绝对路径)，顺序稳定，已剔除字节码/DS_Store。"""
    entries = []
    for name in ROOT_FILES:
        if name == "NOTICE.md" and not os.path.exists(os.path.join(PKG_ROOT, name)):
            continue
        path = os.path.join(PKG_ROOT, name)
        if not os.path.exists(path):
            if name in ("LICENSE",):
                continue
            fail("缺少顶层文件 %s" % name)
        entries.append((name, path))

    for dirname in ("backend", "frontend"):
        base = os.path.join(PKG_ROOT, dirname)
        if dirname == "backend" and not os.path.isdir(base):
            fail("找不到 backend/ 目录")
        for root, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for fn in sorted(files):
                path = os.path.join(root, fn)
                rel = os.path.relpath(path, PKG_ROOT).replace(os.sep, "/")
                if EXCLUDE_RE.search(rel):
                    continue
                if not include_vendor and rel.startswith("backend/vendor/"):
                    continue
                entries.append((rel, path))

    entries.sort(key=lambda kv: kv[0])
    return entries


def validate_vendor():
    """随包 dukpy 副本必须与 VENDOR.json 逐字节对得上（生成脚本会做这件事，这里再验一遍）。"""
    vendor_dir = os.path.join(PKG_ROOT, "backend", "vendor", "dukpy")
    if not os.path.isdir(vendor_dir):
        fail("缺少 backend/vendor/dukpy/（跑 python scripts/fetch_vendor_wheels.py 生成；"
             "想打不带内置依赖的精简包用 --no-vendor）")
    sys.path.insert(0, HERE)
    import fetch_vendor_wheels as fvw
    manifest = fvw.load_manifest()
    if not manifest:
        fail("缺少 backend/vendor/dukpy/VENDOR.json")
    if not fvw.verify(manifest, verbose=False):
        fail("随包 dukpy 副本与 VENDOR.json 不一致（跑 python scripts/fetch_vendor_wheels.py 重新生成）")
    platforms = manifest["platforms"]
    total = sum(entry["installed_size"] for entry in platforms.values())
    print("✔ 随包 dukpy 副本完整：%d 个平台（%s），解包 %.1f MB"
          % (len(platforms), ", ".join(sorted(platforms)), total / 1024.0 / 1024.0))
    return manifest


def check_root_shape(entries):
    bad = []
    for rel, _path in entries:
        top = rel.split("/")[0]
        if top not in ROOT_ENTRIES:
            bad.append(rel)
    if bad:
        fail("包里出现不该有的顶层条目：%s" % "、".join(sorted(bad)))
    print("✔ 包形状：%d 个条目，顶层只有 %s" % (len(entries), " / ".join(ROOT_ENTRIES)))


def zip_time(manifest):
    """用 publish_date 生成固定时间戳，保证同样的输入打包出同样的字节。"""
    try:
        year, month, day = (int(p) for p in str(manifest.get("publish_date", "2026-01-01")).split("-")[:3])
    except ValueError:
        year, month, day = 2026, 1, 1
    return (year, month, day, 0, 0, 0)


def build(manifest, entries, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    stamp = zip_time(manifest)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel, path in entries:
            info = zipfile.ZipInfo(rel, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            # ① 权限位固定（否则随 umask 变）
            info.external_attr = (0o644 & 0xFFFF) << 16
            # ② "version made by" 里的宿主系统固定成 UNIX。CPython 的 ZipInfo 默认按当前平台
            #    取 `create_system`（Windows=0/DOS，Linux=3/UNIX），这一个字节就会让**同一份
            #    内容在 Windows 和 Linux 上打出不同的 sha256** —— 本机和 CI 各打一遍才发现：
            #    解包内容逐字节一致、压缩后大小也一致，只有这个字段不同。
            #    固定它之后，"仓库里记的 sha256"在哪个平台构建都成立。
            info.create_system = 3
            with open(path, "rb") as f:
                zf.writestr(info, f.read())
    return out_path


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_mytool_validate(zip_path):
    """把产物交给官方脚手架再校验一遍 —— 这才是"符合规范"的证据。"""
    builder = os.environ.get("MYTOOL_DIR") or os.path.join(os.path.dirname(PKG_ROOT), "tools_builder")
    cli = os.path.join(builder, "bin", "mytool.js")
    if not os.path.exists(cli):
        print("· 跳过官方校验：找不到 %s（可用 MYTOOL_DIR 指定）" % cli)
        return None
    node = os.environ.get("NODE_BIN") or "node"
    try:
        proc = subprocess.run([node, cli, "validate", zip_path],
                              capture_output=True, text=True, encoding="utf-8")
    except FileNotFoundError:
        print("· 跳过官方校验：找不到 node（可用 NODE_BIN 指定）")
        return None
    out = (proc.stdout or "") + (proc.stderr or "")
    print("· mytool validate →")
    for line in out.strip().splitlines():
        print("    %s" % line)
    if proc.returncode != 0:
        fail("官方脚手架校验未通过（见上面的输出）")
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只校验，不打包")
    parser.add_argument("--no-mytool", action="store_true", help="跳过官方 mytool validate")
    parser.add_argument("--no-vendor", action="store_true",
                        help="打不带内置 dukpy 的精简包（JS 规则退回：宿主有就用、没有就降级）")
    parser.add_argument("--fix-eol", action="store_true",
                        help="把文本文件里的 CRLF 就地改成 LF（改完要重跑 fetch_vendor_wheels.py --rehash）")
    args = parser.parse_args()

    check_eol(fix=args.fix_eol)
    if args.fix_eol:
        return 0

    manifest = read_manifest()
    validate_manifest(manifest)
    print("✔ manifest.json 必填字段 / 版本号 / api_routes 声明合法（%s@%s，core_api %s）"
          % (manifest["tool_id"], manifest["revision"], manifest["core_api_version"]))
    validate_entries(manifest)
    validate_frontend_assets(manifest)
    if args.no_vendor:
        print("· --no-vendor：跳过随包依赖校验，包里不带 backend/vendor/")
    else:
        validate_vendor()
    entries = iter_package_files(include_vendor=not args.no_vendor)
    check_root_shape(entries)

    if args.check:
        print("\n（--check：到此为止，未打包）")
        return 0

    name = "%s-%s.zip" % (manifest["tool_id"], manifest["revision"])
    out_path = os.path.join(PKG_ROOT, "dist", name)
    build(manifest, entries, out_path)
    digest = sha256(out_path)

    vendor_bytes = sum(os.path.getsize(p) for rel, p in entries if rel.startswith("backend/vendor/"))
    size = os.path.getsize(out_path)
    with zipfile.ZipFile(out_path) as zf:
        print("\n✔ 已打包 dist/%s（%d 个条目，%.1f MB）"
              % (name, len(zf.namelist()), size / 1024.0 / 1024.0))
    if vendor_bytes:
        print("  其中随包依赖 backend/vendor/ 原始 %.1f MB（压缩后约占包内 %.1f MB）"
              % (vendor_bytes / 1024.0 / 1024.0,
                 sum(i.compress_size for i in zipfile.ZipFile(out_path).infolist()
                     if i.filename.startswith("backend/vendor/")) / 1024.0 / 1024.0))
        print("  不需要它就用 python scripts/build.py --no-vendor 打成精简包")
    print("  sha256: %s" % digest)

    if not args.no_mytool:
        run_mytool_validate(out_path)

    print("\n安装方式：MyBooks 管理端 /admin/toolbox 上传（需管理员在系统设置里开启 "
          "ENABLE_TOOLBOX_DEV_MODE），安装后重启 MyBooks 生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
