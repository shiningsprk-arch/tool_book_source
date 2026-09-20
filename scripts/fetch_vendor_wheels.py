# -*- coding: utf-8 -*-
"""把 dukpy（`@js:` 规则要用的 JS 引擎）按平台裁好后放进 `backend/vendor/dukpy/`。

为什么是"裁好"而不是"整只 wheel"：dukpy 的 wheel 里 **10.8 MB 是 `dukpy/jsmodules/`**
（TypeScript/Babel/React 编译器），只被 dukpy 的 `jsx_compile` / `typescript_compile` /
`less_compile` 在**调用时**读取（`tsc.py` 里 `open(TS_COMPILER)` 是惰性的）；引擎只用
`dukpy.JSInterpreter`，永远碰不到。裁掉之后每个平台只剩：

    dukpy/*.py (~30 KB) + dukpy/jscore/*.js + dukpy/jsruntime/*.js (~50 KB) + 原生模块

原生模块是平台相关的（POSIX 上是 `.so`、Windows 上是 `.pyd`），而且和 CPython 的 ABI
绑定（cp312 编的不能被 cp313 加载）——所以**每个平台一个目录**，运行时按当前解释器挑。

平台矩阵按 MyBooks 实际发布的形态定（证据在 mybooks 仓库里）：

| 目标 | 依据 |
|---|---|
| `cp312-manylinux_x86_64` | `Dockerfile.base` = ubuntu:24.04 + 系统 python3（cp312）；`prebuilt/OpenCC-…-cp312-…-manylinux2014_x86_64.whl` |
| `cp312-manylinux_aarch64` | 同上，aarch64 版；`make build-base-multiarch` + 仓库里同时放了 aarch64 的 opencc wheel |
| `cp312-win_amd64` | 仓库有 Windows 安装器（`installer/mybooks.iss`）；ABI 按 docker 那份的 cp312 推断 |

其它平台（macOS / Alpine musl / 别的 Python 版本）不在随包矩阵里：运行时探不到匹配的副本就
退回"JS 规则不可用"的降级路径；用户可以用 `BOOK_SOURCE_VENDOR_DIR` 指一个自己下载的 wheel
目录补上（见 `backend/dukpy_vendor.py`）。

用法：
    python scripts/fetch_vendor_wheels.py                 # 生成/刷新随包矩阵
    python scripts/fetch_vendor_wheels.py --check         # 只校验现有副本（不联网）
    python scripts/fetch_vendor_wheels.py --extra cp313-win_amd64
        # 额外拉一个平台进 vendor（本地自测用：能真加载当前解释器的原生模块）
"""
import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(HERE)
VENDOR_DIR = os.path.join(PKG_ROOT, "backend", "vendor", "dukpy")
MANIFEST_NAME = "VENDOR.json"

DUKPY_VERSION = "0.6.0"

# tag -> (pip --platform, --abi, python_version) ；tag 同时是随包目录名。
#
# ⚠️ tag 里的平台段必须用 `backend/dukpy_vendor.platform_tag()` 会生成的名字
# （linux / win / macos），**不能**照抄 wheel 文件名里的 `manylinux`——运行时是按目录名去
# 找副本的，名字对不上就等于这份副本白带（tests/test_vendor.py 的 tag 格式用例专门守这条）。
# wheel 文件名里的 manylinux2014 仍然记在 VENDOR.json 里，provenance 不丢。
DEFAULT_TARGETS = {
    "cp312-linux_x86_64": ("manylinux2014_x86_64", "cp312", "312"),
    "cp312-linux_aarch64": ("manylinux2014_aarch64", "cp312", "312"),
    "cp312-win_amd64": ("win_amd64", "cp312", "312"),
}

EXTRA_TARGETS = {
    # 测试/自用：名字必须能反推出 pip 参数，见 parse_tag
    "cp313-win_amd64": ("win_amd64", "cp313", "313"),
    "cp312-macos_x86_64": ("macosx_10_9_x86_64", "cp312", "312"),
    "cp312-macos_arm64": ("macosx_11_0_arm64", "cp312", "312"),
}

# 裁掉的东西（相对 wheel 根的路径前缀）
PRUNE_PREFIXES = ("dukpy/jsmodules/",)
KEEP_DIST_INFO = ("dukpy-{v}.dist-info/METADATA", "dukpy-{v}.dist-info/licenses/LICENSE")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    with open(path, "rb") as f:
        return sha256_bytes(f.read())


def download_wheel(target, platform, abi, py_version, dest):
    """用 pip 的跨平台下载能力取 wheel（-d 指向真实目录，注意别用 Git-Bash 的 /c/... 形式）。"""
    cmd = [
        sys.executable, "-m", "pip", "download", "dukpy==%s" % DUKPY_VERSION,
        "--no-deps", "--only-binary=:all:",
        "--python-version", py_version, "--implementation", "cp",
        "--abi", abi, "--platform", platform,
        "-d", dest,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout or "").splitlines()[-4:] + (proc.stderr or "").splitlines()[-4:])
        raise SystemExit("✗ 下载 %s 失败：\n%s" % (target, tail))
    wheels = [n for n in os.listdir(dest) if n.endswith(".whl")]
    if len(wheels) != 1:
        raise SystemExit("✗ 预期下到 1 个 wheel，实际 %d 个：%s" % (len(wheels), wheels))
    return os.path.join(dest, wheels[0])


def prune_wheel(wheel_path, out_dir):
    """把 wheel 解成 `out_dir/`，只留运行 `dukpy.JSInterpreter` 需要的东西。

    - 丢掉 `dukpy/jsmodules/**`（10.8 MB 的 TypeScript/Babel/React，惰性使用）
    - 保留 `dukpy/*.py`、`dukpy/jscore/**`、`dukpy/jsruntime/**`、原生模块
    - dist-info 里只留 METADATA + LICENSE，落到 `_meta/`（避免 `importlib` 把 dist-info
      当成包；也避免被当成可疑目录）
    - 丢掉 dist-info/RECORD：裁过之后它已经对不上了，完整性改由 VENDOR.json 的 sha256 保证
    """
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)

    kept = []
    native = None
    with zipfile.ZipFile(wheel_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if name.endswith("/"):
                continue
            if any(name.startswith(p) for p in PRUNE_PREFIXES):
                continue
            if ".dist-info/" in name:
                continue  # 单独处理，见下
            if name.startswith("dukpy/") and not name.endswith((".py", ".js", ".so", ".pyd")):
                continue  # py.typed 之类
            if "/" not in name:
                continue  # 顶层散落文件
            if name.endswith((".so", ".pyd")):
                native = name
            data = zf.read(info)
            target = os.path.join(out_dir, name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(data)
            kept.append({"path": name, "size": len(data), "sha256": sha256_bytes(data)})

        # dist-info 的两份文本落到 _meta/（名字进清单，verify 会一起校验）
        for tmpl in KEEP_DIST_INFO:
            cand = tmpl.format(v=DUKPY_VERSION)
            try:
                data = zf.read(cand)
            except KeyError:
                continue
            rel = "_meta/%s" % os.path.basename(cand)
            target = os.path.join(out_dir, "_meta", os.path.basename(cand))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(data)
            kept.append({"path": rel, "size": len(data), "sha256": sha256_bytes(data)})

    if native is None:
        raise SystemExit("✗ %s 里没有找到原生模块（.so/.pyd）" % wheel_path)
    kept.sort(key=lambda item: item["path"])
    total = sum(item["size"] for item in kept)
    return native, kept, total


def build_manifest_entry(target, wheel_path, out_dir):
    native, kept, total = prune_wheel(wheel_path, out_dir)
    return {
        "wheel": os.path.basename(wheel_path),
        "wheel_sha256": sha256_file(wheel_path),
        "wheel_size": os.path.getsize(wheel_path),
        "native": native,
        "native_sha256": next(i["sha256"] for i in kept if i["path"] == native),
        "files": kept,
        "files_count": len(kept),
        "installed_size": total,
    }


def parse_tag(tag):
    """`cp312-manylinux_x86_64` → ('manylinux2014_x86_64', 'cp312', '312')。

    tag 里的 `manylinux` / `win` / `macos` 只是目录名用的短写，下载时要换成 pip 认的
    platform 串；这里做映射，映射不到的 tag 只能通过 --extra 的注册表来。
    """
    for known, spec in list(DEFAULT_TARGETS.items()) + list(EXTRA_TARGETS.items()):
        if known == tag:
            return spec
    raise SystemExit("✗ 未知平台 tag：%s（可用：%s）"
                     % (tag, ", ".join(sorted(list(DEFAULT_TARGETS) + list(EXTRA_TARGETS)))))


def load_manifest():
    path = os.path.join(VENDOR_DIR, MANIFEST_NAME)
    if not os.path.exists(path):
        return None
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


def write_manifest(manifest):
    os.makedirs(VENDOR_DIR, exist_ok=True)
    path = os.path.join(VENDOR_DIR, MANIFEST_NAME)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(manifest, ensure_ascii=False, indent=2))
        f.write("\n")


def verify(manifest, verbose=True):
    """校验随包副本与 VENDOR.json 逐字节一致（打包前/测试里都用它）。"""
    problems = []
    for tag, entry in sorted(manifest.get("platforms", {}).items()):
        out_dir = os.path.join(VENDOR_DIR, tag)
        if not os.path.isdir(out_dir):
            problems.append("%s: 目录不存在" % tag)
            continue
        seen = set()
        for item in entry["files"]:
            path = os.path.join(out_dir, item["path"])
            seen.add(item["path"])
            if not os.path.exists(path):
                problems.append("%s: 缺少 %s" % (tag, item["path"]))
                continue
            actual = sha256_file(path)
            if actual != item["sha256"]:
                problems.append("%s: %s 内容被改过（sha256 不符）" % (tag, item["path"]))
        # 反向：目录里不能有清单里没写的东西
        for root, _dirs, files in os.walk(out_dir):
            for fn in files:
                rel = os.path.relpath(os.path.join(root, fn), out_dir).replace(os.sep, "/")
                if rel == MANIFEST_NAME:
                    continue
                if rel not in seen:
                    problems.append("%s: 多出一个清单里没有的文件 %s" % (tag, rel))
    if problems:
        for p in problems:
            print("  ✗ %s" % p)
        return False
    if verbose:
        total = sum(e["installed_size"] for e in manifest["platforms"].values())
        print("✔ vendor/dukpy 校验通过：%d 个平台，解包后共 %.1f MB"
              % (len(manifest["platforms"]), total / 1024.0 / 1024.0))
    return True


def rehash(manifest):
    """按磁盘现状重算每个文件的 size/sha256，不联网、不动文件。

    用途：把仓库里的文本文件统一成 LF 之后（`.gitattributes` 的 `* text=auto eol=lf`），
    从 wheel 里解出来的 CRLF 会被规范化，清单里的哈希就必须跟着重算 —— 否则**别人 clone
    下来的副本会校验失败**（这是"哈希清单"这类机制最典型的坑）。
    """
    for tag, entry in sorted(manifest["platforms"].items()):
        out_dir = os.path.join(VENDOR_DIR, tag)
        kept = []
        for item in entry["files"]:
            path = os.path.join(out_dir, item["path"])
            data = open(path, "rb").read()
            kept.append({"path": item["path"], "size": len(data), "sha256": sha256_bytes(data)})
        entry["files"] = kept
        entry["installed_size"] = sum(i["size"] for i in kept)
        native = next(i for i in kept if i["path"] == entry["native"])
        entry["native_sha256"] = native["sha256"]
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只校验现有副本，不联网")
    parser.add_argument("--rehash", action="store_true",
                        help="按磁盘现状重算清单里的 sha256（换行符规范化之后用），不联网")
    parser.add_argument("--extra", action="append", default=[],
                        help="额外拉取的平台 tag（见 EXTRA_TARGETS）")
    parser.add_argument("--only", action="append", default=[],
                        help="只处理这些 tag（默认全部随包矩阵）")
    parser.add_argument("--out-dir", default=None,
                        help="输出根目录（默认 backend/vendor/dukpy）。指向仓库外/临时目录可以"
                             "单独拉一个平台给自己用，不影响随包矩阵")
    args = parser.parse_args()

    # 单个全局出口：下面所有函数都读 VENDOR_DIR，--out-dir 就是给它换个根
    global VENDOR_DIR
    if args.out_dir:
        VENDOR_DIR = os.path.abspath(args.out_dir)

    existing = load_manifest()
    if args.check:
        if not existing:
            raise SystemExit("✗ 还没有 %s，先跑一次不带 --check 的" % MANIFEST_NAME)
        sys.exit(0 if verify(existing) else 1)

    if args.rehash:
        if not existing:
            raise SystemExit("✗ 还没有 %s，先跑一次不带 --rehash 的" % MANIFEST_NAME)
        write_manifest(rehash(existing))
        print("→ 已按磁盘现状重算清单哈希")
        if not verify(load_manifest()):
            raise SystemExit("✗ 重算后自检失败")
        return

    targets = list(args.only) or list(DEFAULT_TARGETS)
    for tag in args.extra:
        if tag not in targets:
            targets.append(tag)

    platforms = dict((existing or {}).get("platforms", {}))
    with tempfile.TemporaryDirectory(prefix="dukpy_wheels_") as cache:
        for tag in targets:
            platform, abi, py_version = parse_tag(tag)
            print("→ %s（%s / %s）" % (tag, platform, abi))
            wheel = download_wheel(tag, platform, abi, py_version, cache)
            out_dir = os.path.join(VENDOR_DIR, tag)
            entry = build_manifest_entry(tag, wheel, out_dir)
            platforms[tag] = entry
            print("   %s → %s，保留 %d 个文件 / %.1f MB（原始 wheel %.1f MB，裁掉 jsmodules）"
                  % (os.path.basename(wheel), os.path.basename(entry["native"]),
                     entry["files_count"], entry["installed_size"] / 1024.0 / 1024.0,
                     entry["wheel_size"] / 1024.0 / 1024.0))
            os.unlink(wheel)

    manifest = {
        "package": "dukpy",
        "version": DUKPY_VERSION,
        "license": "MIT",
        "homepage": "https://github.com/amol-/dukpy",
        "what": "Legado 书源 @js: 规则用的 JS 引擎（Duktape）；引擎里的 js_runtime 只用 JSInterpreter",
        "pruned": list(PRUNE_PREFIXES),
        "pruned_reason": ("jsmodules/ 里的 TypeScript/Babel/React 编译器只被 dukpy 的 "
                          "jsx_compile / typescript_compile / less_compile 惰性读取（各平台约 "
                          "10.8 MB），引擎从不调用，裁掉后每个平台只剩原生模块 + ~50 KB 纯 Python"),
        "select_by": "cp<major><minor>-<platform>_<machine>（见 backend/dukpy_vendor.py）",
        "platforms": platforms,
    }
    write_manifest(manifest)

    # LICENSE 单独放一份在 vendor/dukpy/ 下便于审核
    for tag in sorted(platforms):
        lic = os.path.join(VENDOR_DIR, tag, "_meta", "LICENSE")
        if os.path.exists(lic):
            shutil.copyfile(lic, os.path.join(VENDOR_DIR, "LICENSE"))
            break

    print()
    if not verify(load_manifest()):
        raise SystemExit("✗ 生成后自检失败")


if __name__ == "__main__":
    main()
