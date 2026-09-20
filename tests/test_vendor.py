# -*- coding: utf-8 -*-
"""随包内置的 dukpy 副本：完整性、平台正确性、以及"真的能被加载"。

这套测试回答三个层次的问题：

1. **没被改坏**：`backend/vendor/dukpy/` 里每个文件都与 `VENDOR.json` 的 sha256 一致，
   多一个少一个都算失败（`scripts/fetch_vendor_wheels.py verify`）。
2. **平台标称是真的**：原生模块的二进制头（ELF / PE）里的目标架构，以及文件名里的 CPython
   ABI 标签，必须与所在目录的 tag 一致 —— 防止"把 aarch64 的 .so 放进 x86_64 目录"这种
   打包事故（本机跑不出来，只能靠读文件头发现）。
3. **加载链路通**：跑子进程真加载一遍 —— `backend/dukpy_vendor.install()` 挑到副本、
   `sys.modules['dukpy']` 指的是副本里的文件、引擎的 `_HAS_DUKPY` 因此为真、
   `run_js()` 能真的执行一条 `@js:` 规则。

第 3 层需要"与当前解释器 ABI 匹配的副本"。随包矩阵只带 MyBooks 实际发布的平台
（cp312 Linux x86_64/aarch64、cp312 Windows amd64），所以在本机（可能是别的 ABI）上：

    python scripts/fetch_vendor_wheels.py --only <你的 tag> --out-dir dev/vendor-extra

生成一份到 `dev/vendor-extra/`（已 gitignore，不进包），测试会自动用它；没有就跳过并
打印提示，而不是假装通过。
"""
import json
import os
import struct
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(PKG_ROOT, "scripts")
VENDOR_DIR = os.path.join(PKG_ROOT, "backend", "vendor", "dukpy")
EXTRA_DIR = os.path.join(PKG_ROOT, "dev", "vendor-extra")

sys.path.insert(0, SCRIPTS)
sys.path.insert(0, PKG_ROOT)

import fetch_vendor_wheels as fvw  # noqa: E402


def load_vendor_manifest():
    path = os.path.join(VENDOR_DIR, "VENDOR.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def elf_machine(path):
    with open(path, "rb") as f:
        header = f.read(20)
    if header[:4] != b"\x7fELF":
        return None
    little = header[5] == 1
    machine = header[18:20]
    return struct.unpack("<H" if little else ">H", machine)[0]


def pe_machine(path):
    with open(path, "rb") as f:
        data = f.read(0x40)
    if data[:2] != b"MZ":
        return None
    with open(path, "rb") as f:
        f.seek(struct.unpack("<I", data[0x3C:0x40])[0])
        head = f.read(6)
    if head[:4] != b"PE\x00\x00":
        return None
    return struct.unpack("<H", head[4:6])[0]


ELF_MACHINES = {0x3E: "x86_64", 0xB7: "aarch64"}
PE_MACHINES = {0x8664: "amd64", 0xAA64: "arm64", 0x14C: "i686"}


def current_interpreter_tag():
    """不 import tool.py（它需要宿主替身），直接问 vendor 加载器。"""
    sys.path.insert(0, PKG_ROOT)
    from backend import dukpy_vendor
    return dukpy_vendor.platform_tag()


def run_python(code, env_extra=None, no_site=False):
    """在子进程里跑一段代码（隔离子进程，避免污染测试进程的 sys.modules）。

    要验"随包副本"那条路时用 `BOOK_SOURCE_PREFER_BUNDLED=1` 跳过宿主那份 —— 不能用
    `python -S`（屏蔽 site-packages），因为那样连 requests/bs4 也没了，`backend.tool`
    根本 import 不起来。
    """
    env = dict(os.environ)
    for key in ("BOOK_SOURCE_VENDOR_DIR", "BOOK_SOURCE_PREFER_BUNDLED"):
        env.pop(key, None)
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    cmd = [sys.executable]
    if no_site:
        cmd.append("-S")
    cmd += ["-c", code]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          cwd=PKG_ROOT, env=env)


class TestVendorIntegrity(unittest.TestCase):
    def test_manifest_matches_files(self):
        manifest = load_vendor_manifest()
        self.assertEqual(manifest["package"], "dukpy")
        self.assertNotEqual(manifest["platforms"], {})
        self.assertTrue(fvw.verify(manifest, verbose=False),
                        "随包副本与 VENDOR.json 不一致，跑 scripts/fetch_vendor_wheels.py 重新生成")

    def test_wheel_provenance_recorded(self):
        manifest = load_vendor_manifest()
        for tag, entry in manifest["platforms"].items():
            self.assertTrue(entry["wheel"].startswith("dukpy-%s-" % manifest["version"]), tag)
            self.assertEqual(len(entry["wheel_sha256"]), 64, tag)
            self.assertEqual(len(entry["native_sha256"]), 64, tag)

    def test_locales_of_bundled_package_present(self):
        """MIT 许可要求版权声明随副本分发。"""
        license_path = os.path.join(VENDOR_DIR, "LICENSE")
        self.assertTrue(os.path.exists(license_path))
        text = open(license_path, encoding="utf-8").read()
        self.assertIn("MIT", text.upper())
        self.assertIn("Alessandro Molina", text)

    def test_jsmodules_pruned(self):
        """裁掉 jsmodules 是刻意的：它是 10.8 MB 的 TS/Babel/React，引擎从不调用。
        这条断言防止有人"顺手"换回整只 wheel。"""
        manifest = load_vendor_manifest()
        self.assertIn("dukpy/jsmodules/", manifest["pruned"])
        for tag in manifest["platforms"]:
            self.assertFalse(os.path.exists(os.path.join(VENDOR_DIR, tag, "dukpy", "jsmodules")), tag)

    def test_payload_is_reasonably_small(self):
        manifest = load_vendor_manifest()
        for tag, entry in manifest["platforms"].items():
            mb = entry["installed_size"] / 1024.0 / 1024.0
            self.assertLess(mb, 8.0, "%s 解包后 %.1f MB，比预期大太多（是不是没裁 jsmodules）" % (tag, mb))


class TestPlatformCorrectness(unittest.TestCase):
    def test_tag_names_wellformed(self):
        manifest = load_vendor_manifest()
        for tag in manifest["platforms"]:
            ver, _, rest = tag.partition("-")
            plat, _, machine = rest.partition("_")
            self.assertRegex(ver, r"^cp3\d{1,2}$", tag)
            self.assertIn(plat, ("linux", "win", "macos"), tag)
            self.assertTrue(machine, tag)

    def test_native_module_filename_matches_tag_abi(self):
        """文件名里的 ABI 标签（cp312 / cpython-312）必须与目录 tag 的 cpXXX 一致。"""
        manifest = load_vendor_manifest()
        for tag, entry in manifest["platforms"].items():
            want = tag.split("-")[0]  # cp312
            native = entry["native"]
            abi_in_name = want if want in native else want.replace("cp", "cpython-")
            self.assertIn(abi_in_name, native,
                          "%s 的原生模块名 %s 与 ABI 不符" % (tag, native))

    def test_native_binary_matches_declared_architecture(self):
        """读二进制头确认架构 —— 本机跑不了别的架构，只有这一层能发现放错目录。"""
        manifest = load_vendor_manifest()
        for tag, entry in manifest["platforms"].items():
            plat, _, machine = tag.partition("-")[2].partition("_")
            path = os.path.join(VENDOR_DIR, tag, entry["native"])
            if entry["native"].endswith(".so"):
                actual = ELF_MACHINES.get(elf_machine(path))
            else:
                actual = PE_MACHINES.get(pe_machine(path))
            expected = {"linux": {"x86_64": "x86_64", "aarch64": "aarch64"},
                        "win": {"amd64": "amd64", "arm64": "arm64"},
                        "macos": {"x86_64": "x86_64", "arm64": "arm64"}}[plat][machine]
            self.assertEqual(actual, expected, "%s: 二进制架构是 %s" % (tag, actual))

    def test_bundled_matrix_covers_mybooks_targets(self):
        """随包矩阵要盖住 MyBooks 实际发布的形态：cp312 + Linux 双架构 + Windows amd64。
        依据：宿主 Dockerfile.base 用 ubuntu:24.04 的系统 python3（cp312）、
        `make build-base-multiarch`、`prebuilt/` 里同时有 x86_64 与 aarch64 的 cp312 wheel。"""
        manifest = load_vendor_manifest()
        for tag in ("cp312-linux_x86_64", "cp312-linux_aarch64", "cp312-win_amd64"):
            self.assertIn(tag, manifest["platforms"])


class TestLoader(unittest.TestCase):
    CARGO = (
        "import json, sys\n"
        "sys.path.insert(0, %r)\n"
        "from backend import dukpy_vendor\n"
        "state = dukpy_vendor.install()\n"
        "out = {'state': state}\n"
        "out['requested'] = dukpy_vendor.platform_tag()\n"
        "out['bundled'] = dukpy_vendor.bundled_tags()\n"
        "try:\n"
        "    import dukpy\n"
        "    out['dukpy_file'] = dukpy.__file__\n"
        "except Exception as err:\n"
        "    out['dukpy_file'] = 'ERR %%s: %%s' %% (type(err).__name__, err)\n"
        "print(json.dumps(out))\n"
    ) % PKG_ROOT

    def test_install_never_raises_and_reports_state(self):
        proc = run_python(self.CARGO)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertIn(out["state"]["source"], ("host", "bundled", "external", "missing"))
        self.assertEqual(out["state"]["bundled_tags"], out["bundled"])
        if out["requested"] is None:
            self.assertEqual(out["state"]["source"], "missing")
            self.assertTrue(out["state"]["error"])
        else:
            self.assertEqual(out["requested"], out["state"]["requested_tag"])

    def matching_shipped_tag(self):
        tag = current_interpreter_tag()
        if not tag:
            return None
        if os.path.isfile(os.path.join(VENDOR_DIR, tag, "dukpy", "__init__.py")):
            return tag
        return None

    def test_broken_copy_degrades_instead_of_raising(self):
        """坏副本（比如文件被截断）不能让工具崩，只能降级 + 给出原因。"""
        if self.matching_shipped_tag():
            self.skipTest("随包里就有匹配 %s 的副本，坏的那份根本轮不到被选（这条用例在"
                          "别的解释器上才有意义）" % self.matching_shipped_tag())
        import tempfile
        tag = current_interpreter_tag() or "cp312-linux_x86_64"
        with tempfile.TemporaryDirectory() as tmp:
            broken = os.path.join(tmp, tag, "dukpy")
            os.makedirs(broken)
            with open(os.path.join(broken, "__init__.py"), "w", encoding="utf-8") as f:
                f.write("raise ImportError('模拟损坏的副本')\n")
            proc = run_python(self.CARGO, env_extra={
                "BOOK_SOURCE_VENDOR_DIR": tmp, "BOOK_SOURCE_PREFER_BUNDLED": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(out["state"]["source"], "missing")
        self.assertIn("模拟损坏的副本", out["state"]["error"])


class TestRealLoad(unittest.TestCase):
    """真加载一条路：只有存在与当前解释器 ABI 匹配的副本时才跑（见模块 docstring）。"""

    def matching_extra_dir(self):
        tag = current_interpreter_tag()
        if not tag:
            return None
        if os.path.isfile(os.path.join(EXTRA_DIR, tag, "dukpy", "__init__.py")):
            return (EXTRA_DIR, tag)
        if os.path.isfile(os.path.join(VENDOR_DIR, tag, "dukpy", "__init__.py")):
            return (VENDOR_DIR, tag)
        return None

    def test_vendored_dukpy_loads_and_engine_sees_it(self):
        match = self.matching_extra_dir()
        if not match:
            self.skipTest("没有与 %s 匹配的副本；用 `python scripts/fetch_vendor_wheels.py "
                          "--only <tag> --out-dir dev/vendor-extra` 生成一份再跑"
                          % (current_interpreter_tag() or "当前解释器"))
        root, tag = match
        code = (
            "import json, sys\n"
            "sys.path.insert(0, %r)\n"
            "sys.path.insert(0, %r)\n"
            "import _host_stub\n"
            "_host_stub.install()   # 顶掉 webserver.*，不然 backend.tool 起不来\n"
            "from backend import dukpy_vendor\n"
            "from backend.engine import js_runtime\n"
            "from backend import tool\n"
            "out = {\n"
            "  'vendor': dukpy_vendor.describe(),\n"
            "  'has_dukpy': js_runtime._HAS_DUKPY,\n"
            "  'dukpy_file': getattr(__import__('dukpy'), '__file__', ''),\n"
            "  'replace': js_runtime.run_js(\"result.replace('a','b')\", result='aa'),\n"
            "  'replace_re': js_runtime.run_js(\"result.replace(/a/g,'b')\", result='aa'),\n"
            "  'calc': js_runtime.run_js(\"(1+2).toString() + result\", result='!'),\n"
            "  'engine_ok': tool._ENGINE_OK,\n"
            "  'tool_has_dukpy': tool._HAS_DUKPY,\n"
            "}\n"
            "print(json.dumps(out))\n"
        ) % (PKG_ROOT, HERE)
        # 强制走随包副本那条路（本机宿主装了 dukpy，默认会优先用宿主的）
        proc = run_python(code, env_extra={
            "BOOK_SOURCE_VENDOR_DIR": root, "BOOK_SOURCE_PREFER_BUNDLED": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])

        self.assertIn(out["vendor"]["source"], ("bundled", "external"))
        self.assertEqual(out["vendor"]["tag"], tag)
        self.assertTrue(out["has_dukpy"], "引擎的 _HAS_DUKPY 应为 True（随包副本已生效）")
        self.assertTrue(out["engine_ok"])
        # tool.py 里"先装随包 dukpy、再 import engine"的顺序必须成立，
        # 否则引擎顶层已经读到 _HAS_DUKPY=False，晚装也没用
        self.assertTrue(out["tool_has_dukpy"],
                        "tool.py 必须在 import engine 之前装好随包 dukpy")
        self.assertIn("vendor", out["dukpy_file"].replace("\\", "/"))
        # 真的执行了 JS：'aa'.replace('a','b') 只换第一个 → 'ba'；正则 /a/g 才换全部 → 'bb'
        self.assertEqual(out["replace"], "ba")
        self.assertEqual(out["replace_re"], "bb")
        self.assertEqual(out["calc"], "3!")

    def test_host_copy_wins_over_bundled(self):
        """宿主自己装了 dukpy 时优先用宿主的（不管版本/路径，都是运行环境自洽的那一份）。"""
        probe = run_python(
            "import importlib.util as u, json, sys\n"
            "print(json.dumps({'host': u.find_spec('dukpy') is not None}))\n")
        host = json.loads(probe.stdout.strip().splitlines()[-1])["host"]
        if not host:
            self.skipTest("当前解释器没有装宿主版 dukpy")
        code = (
            "import json, sys\n"
            "sys.path.insert(0, %r)\n"
            "from backend import dukpy_vendor\n"
            "print(json.dumps(dukpy_vendor.install()))\n"
        ) % PKG_ROOT
        proc = run_python(code)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(state["source"], "host")


if __name__ == "__main__":
    unittest.main(verbosity=2)
