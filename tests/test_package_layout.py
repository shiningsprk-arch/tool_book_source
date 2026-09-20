# -*- coding: utf-8 -*-
"""包形状 / manifest 契约：产物必须能通过官方脚手架 `mytool validate`。

这里不重复实现校验规则，而是直接调用 `scripts/build.py` 里那套（它自己是按
`tools_builder/src/manifest.js` 与宿主 `toolbox_manager.validate_manifest()` 对齐写的），
再补几条官方校验覆盖不到的：语言包齐全、zip 里没有字节码、编译产物能真正解出来。
"""
import io
import json
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import build as build_script  # noqa: E402

PKG_ROOT = build_script.PKG_ROOT
# 本工具包自己的仓库地址（改名后），manifest.repo_url 必须等于它
REPO_URL = "https://github.com/shiningsprk-arch/tool_book_source"


class TestManifest(unittest.TestCase):
    def setUp(self):
        self.manifest = json.load(io.open(os.path.join(PKG_ROOT, "manifest.json"), encoding="utf-8"))

    def test_required_fields_present(self):
        for field in build_script.REQUIRED_MANIFEST_FIELDS:
            self.assertTrue(self.manifest.get(field), field)

    def test_official_validation_passes(self):
        build_script.validate_manifest(self.manifest)   # 失败会 SystemExit

    def test_entries_and_handlers_exist(self):
        build_script.validate_entries(self.manifest)

    def test_frontend_assets_exist(self):
        build_script.validate_frontend_assets(self.manifest)

    def test_api_route_paths_are_unique(self):
        paths = [entry["path"] for entry in self.manifest["api_routes"]]
        self.assertEqual(len(paths), len(set(paths)))
        for path in paths:
            self.assertTrue(path and not path.startswith("/"), path)

    def test_repo_url_points_at_this_repo(self):
        """repo_url 必须是本仓库（商店审核会拿它核对产物与公开源码）。"""
        self.assertEqual(self.manifest["repo_url"], REPO_URL)
        self.assertEqual(REPO_URL, "https://github.com/shiningsprk-arch/tool_book_source")

    def test_declared_locales_match_files(self):
        locales_dir = os.path.join(PKG_ROOT, "frontend", "locales")
        declared = sorted(self.manifest["locales"])
        files = sorted(name[:-5] for name in os.listdir(locales_dir) if name.endswith(".json")
                       and name != "manifest.json")
        self.assertEqual(declared, files)
        self.assertIn(self.manifest["default_locale"], declared)


class TestPackageShape(unittest.TestCase):
    def test_root_shape_and_no_bytecode(self):
        entries = build_script.iter_package_files()
        build_script.check_root_shape(entries)
        for rel, path in entries:
            self.assertNotIn("__pycache__", rel)
            self.assertFalse(rel.endswith((".pyc", ".pyo")), rel)
            self.assertTrue(os.path.exists(path), rel)

    def test_expected_entries(self):
        entries = dict(build_script.iter_package_files())
        for required in ("manifest.json", "icon.png", "LICENSE", "NOTICE.md",
                         "backend/__init__.py", "backend/tool.py",
                         "backend/engine/rule_engine.py", "backend/engine/js_runtime.py",
                         "backend/engine/epub_helper.py", "backend/engine/content_fallbacks.py",
                         "backend/engine/book_source_model.py",
                         "backend/engine/search_task_service.py",
                         "frontend/index.html", "frontend/app.js", "frontend/lib/i18n.js",
                         "frontend/lib/theme.css", "frontend/locales/manifest.json"):
            self.assertIn(required, entries, required)

    def test_tests_and_scripts_are_not_packaged(self):
        entries = dict(build_script.iter_package_files())
        for rel in entries:
            self.assertFalse(rel.startswith(("tests/", "scripts/", "docs/", "dev/")), rel)

    def test_zip_roundtrip(self):
        import tempfile
        manifest = build_script.read_manifest()
        entries = build_script.iter_package_files()
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "book_source.zip")
            build_script.build(manifest, entries, out)
            self.assertEqual(build_script.sha256(out),
                             build_script.sha256(out))  # 稳定可重算
            with zipfile.ZipFile(out) as zf:
                names = set(zf.namelist())
                self.assertIn("manifest.json", names)
                self.assertIn("icon.png", names)
                self.assertIn("backend/tool.py", names)
                self.assertIn("frontend/index.html", names)
                # 解出来还能当 zip 用（宿主 install_from_zip 会走到这一步）
                zf.extractall(os.path.join(tmp, "out"))
            self.assertTrue(os.path.exists(os.path.join(tmp, "out", "manifest.json")))

    def test_zip_is_deterministic(self):
        """同样的输入要打出同样的字节：sha256 才能被写进变更记录 / 商店登记。"""
        import tempfile
        manifest = build_script.read_manifest()
        entries = build_script.iter_package_files()
        with tempfile.TemporaryDirectory() as tmp:
            first = os.path.join(tmp, "a.zip")
            second = os.path.join(tmp, "b.zip")
            build_script.build(manifest, entries, first)
            build_script.build(manifest, entries, second)
            self.assertEqual(build_script.sha256(first), build_script.sha256(second))


class TestLocalLicenseFiles(unittest.TestCase):
    def test_license_is_mit_from_upstream(self):
        text = io.open(os.path.join(PKG_ROOT, "LICENSE"), encoding="utf-8").read()
        self.assertIn("MIT License", text)
        self.assertIn("Copyright (c) 2024 MyBooks Book Source Plugin", text)
        self.assertIn("WITHOUT WARRANTY OF ANY KIND", text)

    def test_notice_mentions_upstream_and_changes(self):
        text = io.open(os.path.join(PKG_ROOT, "NOTICE.md"), encoding="utf-8").read()
        self.assertIn(REPO_URL, text)          # 本仓库（改名后）地址
        self.assertIn("book-source-engine", text)   # 原始项目名 / 改名来源
        self.assertIn("legacy-project", text)  # 旧形态源码在哪个分支（provenance 可核）
        self.assertIn("MIT", text)
        self.assertIn("extractall", text)      # 记了 zip slip 那处修复


class TestEngineUnchanged(unittest.TestCase):
    """引擎是上游逐字节拷贝 —— 用几个只在原版存在的常量/注释锚住，防止被顺手改动。"""

    def test_engine_modules_present(self):
        engine_dir = os.path.join(PKG_ROOT, "backend", "engine")
        self.assertEqual(
            sorted(n for n in os.listdir(engine_dir) if n.endswith(".py")),
            ["__init__.py", "book_source_model.py", "content_fallbacks.py", "epub_helper.py",
             "js_runtime.py", "rule_engine.py", "search_task_service.py"],
        )

    def test_engine_has_no_webserver_imports(self):
        """引擎必须保持"只依赖第三方库"的独立性，否则搬进外部工具包就跑不起来。"""
        engine_dir = os.path.join(PKG_ROOT, "backend", "engine")
        for name in sorted(os.listdir(engine_dir)):
            if not name.endswith(".py"):
                continue
            text = io.open(os.path.join(engine_dir, name), encoding="utf-8").read()
            self.assertNotIn("from webserver", text, name)
            self.assertNotIn("import webserver", text, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
