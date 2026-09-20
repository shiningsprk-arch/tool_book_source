# -*- coding: utf-8 -*-
"""前端契约：静态页和 JSON 后端、文案目录、宿主 bridge 之间必须严丝合缝。

这些检查的价值在于"能抓到肉眼容易漏的错"：文案键漏一个 → 界面上直接显示 `bookSource.xxx`；
id 拼错 → 那一块交互静默失效（点按钮没反应，不报错）；接口路径写错 → 404；
`[hidden]` 被组件的 display 压过 → 关闭的对话框一直挂在页面上。
"""
import io
import json
import os
import re
import unittest

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND = os.path.join(PKG_ROOT, "frontend")


def read(path):
    return io.open(path, encoding="utf-8").read()


class FrontendCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = read(os.path.join(FRONTEND, "index.html"))
        cls.js = read(os.path.join(FRONTEND, "app.js"))
        cls.manifest = json.load(io.open(os.path.join(PKG_ROOT, "manifest.json"), encoding="utf-8"))
        cls.locales = {}
        locales_dir = os.path.join(FRONTEND, "locales")
        for name in os.listdir(locales_dir):
            if name.endswith(".json") and name != "manifest.json":
                cls.locales[name[:-5]] = json.load(io.open(os.path.join(locales_dir, name),
                                                          encoding="utf-8"))


class TestI18n(FrontendCase):
    def used_keys(self):
        keys = set(re.findall(r'data-i18n="([^"]+)"', self.html))
        for attr in re.findall(r'data-i18n-attr="([^"]+)"', self.html):
            for rule in attr.split(";"):
                parts = rule.split(":")
                if len(parts) == 2:
                    keys.add(parts[1].strip())
        keys |= set(re.findall(r"\bt\('([A-Za-z0-9_.]+)'", self.js))
        return {k for k in keys if k.startswith("bookSource.")}

    def test_every_used_key_is_translated(self):
        used = self.used_keys()
        self.assertTrue(used)
        for locale, table in self.locales.items():
            missing = sorted(used - set(table))
            self.assertEqual(missing, [], "%s 缺少文案：%s" % (locale, missing))

    def test_no_unused_keys(self):
        used = self.used_keys()
        for locale, table in self.locales.items():
            unused = sorted(set(table) - used)
            self.assertEqual(unused, [], "%s 有没被用到的文案：%s" % (locale, unused))

    def test_locales_have_identical_key_sets(self):
        reference = set(self.locales[self.manifest["default_locale"]])
        for locale, table in self.locales.items():
            self.assertEqual(set(table) ^ reference, set(), "%s 键集合与默认语言不一致" % locale)

    def test_placeholders_consistent_across_locales(self):
        """带占位符的文案在三种语言里必须用同一组参数名，否则 English 会少一个 {n}。"""
        pattern = re.compile(r"\{([a-zA-Z0-9_]+)\}")
        for key in sorted(self.locales[self.manifest["default_locale"]]):
            expected = set(pattern.findall(self.locales[self.manifest["default_locale"]][key]))
            for locale, table in self.locales.items():
                actual = set(pattern.findall(table[key]))
                self.assertEqual(actual, expected, "%s 的 %s 占位符不一致" % (locale, key))

    def test_locales_manifest_declares_all(self):
        declared = json.load(io.open(os.path.join(FRONTEND, "locales", "manifest.json"),
                                     encoding="utf-8"))
        self.assertEqual(declared["default"], self.manifest["default_locale"])
        self.assertEqual(sorted(declared["locales"]), sorted(self.manifest["locales"]))


class TestDomContract(FrontendCase):
    def html_ids(self):
        return set(re.findall(r'id="([A-Za-z0-9_-]+)"', self.html))

    def js_ids(self):
        return set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", self.js))

    def test_js_only_touches_existing_ids(self):
        missing = sorted(self.js_ids() - self.html_ids())
        self.assertEqual(missing, [], "app.js 引用了 index.html 里不存在的元素：%s" % missing)

    def test_every_html_id_is_used(self):
        unused = sorted(self.html_ids() - self.js_ids())
        self.assertEqual(unused, [], "index.html 里有没接线到的元素：%s" % unused)

    def test_hidden_attribute_is_not_overridden(self):
        """theme.css 给 .mb-btn / .mb-dialog-overlay 设了 display，会盖掉 [hidden] 的
        display:none —— 少了这条 !important，关掉的对话框会一直挂在页面上。"""
        self.assertRegex(self.html, r"\[hidden\]\s*\{\s*display:\s*none\s*!important\s*;?\s*\}")
        self.assertIn("mb-dialog-overlay", self.html)
        self.assertGreater(len(re.findall(r'class="mb-dialog-overlay"\s+hidden', self.html)), 0)

    def test_no_inline_event_handlers(self):
        """宿主可能下发 CSP；内联 on* 处理器会被拦掉，静默失效。"""
        self.assertEqual(re.findall(r"\son(?:click|error|load|change|input|keydown)\s*=", self.html), [])

    def test_host_bridge_and_theme_wired(self):
        self.assertIn('<script src="/static/toolbox-bridge.js"></script>', self.html)
        self.assertIn('href="lib/theme.css"', self.html)
        self.assertIn('src="lib/i18n.js"', self.html)
        self.assertIn("bridge.onThemeChange", self.js)
        self.assertIn("data-theme", self.js)
        self.assertIn("MyBooksToolI18n.create", self.js)

    def test_no_hardcoded_api_prefix(self):
        """接口前缀应由 bridge 拼（/api/toolbox/tool/<tool_id>/），工具里不能写死 ——
        写死之后换 tool_id 或宿主改前缀就会静默 404。注释里提到完整路径没问题。"""
        code = re.sub(r"/\*.*?\*/", "", self.js, flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
        self.assertNotIn("'/api/toolbox/tool/book_source", code)
        self.assertIn("bridge.toolId", code)


class TestApiContract(FrontendCase):
    def called_paths(self):
        paths = set(re.findall(r"\b(?:api|postJson)\('([^']+)'", self.js))
        paths |= set(re.findall(r"apiUrl\('([^']+)'\)", self.js))
        return {p.split("?")[0] for p in paths}

    def declared_paths(self):
        return {entry["path"] for entry in self.manifest["api_routes"]}

    def test_frontend_only_calls_declared_routes(self):
        undeclared = sorted(self.called_paths() - self.declared_paths())
        self.assertEqual(undeclared, [], "前端调了 manifest 里没声明的接口：%s" % undeclared)

    def test_every_route_is_used_by_frontend(self):
        unused = sorted(self.declared_paths() - self.called_paths())
        self.assertEqual(unused, [], "这些接口声明了但前端没调用：%s" % unused)

    def test_query_params_present(self):
        self.assertIn("'search_status?task_id='", self.js)
        self.assertIn("'test?source='", self.js)


class TestPollingAndCleanup(FrontendCase):
    def test_poll_intervals_documented(self):
        self.assertIn("SEARCH_POLL_MS = 1500", self.js)
        self.assertIn("TASK_POLL_MS = 2000", self.js)

    def test_timers_are_cleared_on_unload(self):
        self.assertIn("beforeunload", self.js)
        self.assertIn("clearInterval", self.js)

    def test_escape_and_overlay_close_all_dialogs(self):
        self.assertIn("Escape", self.js)
        for dialog in ("dlg-edit", "dlg-settings", "dlg-delete", "dlg-confirm-all", "dlg-test"):
            self.assertIn("'%s'" % dialog, self.js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
