# -*- coding: utf-8 -*-
"""把上游内置插件形态改造成外部工具包之后，后端逻辑还对不对。

这套测试不连网、不要 MyBooks、不要 Calibre：`tests/_host_stub.py` 用最小替身顶掉
`webserver.*`，于是 `backend/tool.py` 能在本机 import，书源 CRUD / 导入 / 任务进度 /
15 条路由的响应契约都能真跑一遍。拿不到真机验证的部分（真实 Calibre 入库、真实抓取）
另见 README 的「未验证」一节。
"""
import io
import json
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _host_stub  # noqa: E402

tool = _host_stub.install()

PKG_ROOT = _host_stub.PKG_ROOT


def _source(name="示例源", url="https://example.com", **overrides):
    raw = {
        "bookSourceName": name,
        "bookSourceUrl": url,
        "bookSourceGroup": "测试",
        "searchUrl": "/search?q={{key}}",
        "ruleSearch": {"bookList": "class.book-list@li", "name": "tag.h3@text",
                       "author": "class.author@text", "bookUrl": "tag.a@href"},
        "ruleToc": {"chapterList": "class.chapter@li", "chapterName": "tag.a@text",
                    "chapterUrl": "tag.a@href"},
        "ruleContent": {"content": "id.content@html"},
    }
    raw.update(overrides)
    return raw


class ToolTestCase(unittest.TestCase):
    """每个用例一个干净的工具数据目录 + 干净的任务表。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="book_source_test_")
        self._saved_root = tool.BookSourceTool.TOOL_DATA_ROOT
        tool.BookSourceTool.TOOL_DATA_ROOT = self.tmp
        tool.BackgroundService().reset()
        tool.BookSourceTool._last_task_ids.clear()

    def tearDown(self):
        import shutil
        tool.BookSourceTool.TOOL_DATA_ROOT = self._saved_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sources_path(self):
        return os.path.join(self.tmp, "book_source", "sources.json")


class TestManifestAndInfo(unittest.TestCase):
    def test_info_matches_manifest(self):
        manifest = json.load(io.open(os.path.join(PKG_ROOT, "manifest.json"), encoding="utf-8"))
        info = tool.BookSourceTool.info()
        for field in ("tool_id", "name", "description", "revision", "author", "publish_date",
                      "repo_url"):
            self.assertEqual(info[field], manifest[field], field)

    def test_service_item_name_set(self):
        self.assertTrue(tool.BookSourceTool.service_item_name)


class TestCapabilities(unittest.TestCase):
    def test_reports_dependency_shape(self):
        caps = tool.BookSourceTool.capabilities()
        self.assertTrue(caps["engine"])
        self.assertEqual(caps["engine_error"], "")
        for name in ("requests", "bs4", "lxml", "dukpy", "ebooklib"):
            self.assertIn(name, caps["deps"])
            self.assertIn("required", caps["deps"][name])
        self.assertIsInstance(caps["missing_required"], list)
        self.assertIsInstance(caps["js_rules"], bool)

    def test_reports_dukpy_vendor_state(self):
        """dukpy 是"宿主没有就吃包里内置副本"的那一个，得把用了哪一份说清楚。"""
        caps = tool.BookSourceTool.capabilities()
        self.assertIn(caps["dukpy_source"], ("host", "bundled", "external", "missing", "unavailable"))
        self.assertEqual(caps["deps"]["dukpy"]["source"], caps["dukpy_source"])
        vendor = caps["dukpy_vendor"]
        for key in ("source", "tag", "path", "error", "requested_tag", "bundled_tags"):
            self.assertIn(key, vendor)
        self.assertIsInstance(vendor["bundled_tags"], list)
        self.assertEqual(caps["js_rules"], bool(tool._HAS_DUKPY))
        # js_rules 为真时，dukpy 必然来自某一处；为假时必须给出原因
        if caps["js_rules"]:
            self.assertNotIn(caps["dukpy_source"], ("missing", "unavailable"))
        else:
            self.assertTrue(vendor["error"])

    def test_engine_error_message_helper(self):
        self.assertEqual(tool._deps_error("boom")["err"], "deps.missing")


class TestSafeExtractZip(unittest.TestCase):
    def test_rejects_path_traversal(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "evil.zip")
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("../escaped.txt", "pwned")
            dest = os.path.join(tmp, "out")
            os.makedirs(dest)
            with self.assertRaises(ValueError):
                tool.safe_extract_zip(zip_path, dest)
            self.assertFalse(os.path.exists(os.path.join(tmp, "escaped.txt")))

    def test_accepts_normal_zip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "ok.zip")
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("dir/importBookSource.json", "[]")
            dest = os.path.join(tmp, "out")
            os.makedirs(dest)
            tool.safe_extract_zip(zip_path, dest)
            self.assertTrue(os.path.exists(os.path.join(dest, "dir", "importBookSource.json")))


class TestSourceCrud(ToolTestCase):
    def test_add_list_toggle_delete(self):
        book_source = tool.BookSourceTool()
        self.assertEqual(book_source.list_sources(), [])

        self.assertEqual(book_source.add_source(_source())["status"], "added")
        self.assertTrue(os.path.exists(self.sources_path()))

        listed = book_source.list_sources()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["bookSourceName"], "示例源")
        self.assertTrue(listed[0]["enabled"])

        # 同名再来一次是更新而不是新增
        self.assertEqual(book_source.add_source(_source(url="https://example.org"))["status"],
                         "updated")
        self.assertEqual(len(book_source.list_sources()), 1)

        toggled = book_source.toggle_source("示例源")
        self.assertEqual(toggled["status"], "toggled")
        self.assertFalse(toggled["enabled"])
        self.assertFalse(book_source.list_sources()[0]["enabled"])

        self.assertEqual(book_source.delete_source("示例源")["status"], "deleted")
        self.assertEqual(book_source.list_sources(), [])

    def test_not_found_paths(self):
        book_source = tool.BookSourceTool()
        self.assertEqual(book_source.toggle_source("不存在")["status"], "not_found")
        self.assertEqual(book_source.delete_source("不存在")["status"], "not_found")
        self.assertIsNone(book_source.get_source("不存在"))

    def test_save_is_atomic_and_leaves_no_tmp(self):
        book_source = tool.BookSourceTool()
        book_source.add_source(_source())
        self.assertFalse(os.path.exists(self.sources_path() + ".tmp"))

    def test_sources_live_under_tool_data_root(self):
        self.assertEqual(tool.BookSourceTool()._sources_path(), self.sources_path())


class TestImport(ToolTestCase):
    def _zip_with(self, payload):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("importBookSource.json", json.dumps(payload, ensure_ascii=False))
        self.addCleanup(os.unlink, path)
        return path

    def test_import_skips_js_dependent_sources(self):
        compatible = _source("兼容源")
        blocked = _source("JS 源", searchUrl="@js:java.ajax('x')")
        zip_path = self._zip_with([compatible, blocked])
        result = tool.BookSourceTool().import_sources_from_zip(zip_path)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual([s["bookSourceName"] for s in tool.BookSourceTool().list_sources()],
                         ["兼容源"])

    def test_import_rejects_non_zip(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write("not a zip")
        self.addCleanup(os.unlink, path)
        result = tool.BookSourceTool().import_sources_from_zip(path)
        self.assertEqual(result["status"], "format_error")

    def test_import_rejects_traversal_zip(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("../importBookSource.json", "[]")
        self.addCleanup(os.unlink, path)
        result = tool.BookSourceTool().import_sources_from_zip(path)
        self.assertEqual(result["status"], "format_error")

    def test_import_from_url_blocks_private_addresses(self):
        import unittest.mock
        with unittest.mock.patch.object(tool.requests, "get") as get:
            result = tool.BookSourceTool().import_sources_from_url("http://127.0.0.1/sources.json")
        self.assertEqual(result["status"], "fetch_failed")
        get.assert_not_called()

    def test_is_safe_url(self):
        self.assertFalse(tool.BookSourceTool._is_safe_url(""))
        self.assertFalse(tool.BookSourceTool._is_safe_url("ftp://example.com"))
        self.assertFalse(tool.BookSourceTool._is_safe_url("http://127.0.0.1/x"))
        self.assertFalse(tool.BookSourceTool._is_safe_url("http://10.0.0.5/x"))


class TestSearchWithoutNetwork(ToolTestCase):
    def test_search_async_without_sources(self):
        result = tool.BookSourceTool().search_async("任意")
        self.assertEqual(result["task_id"], "")
        self.assertEqual(result["total"], 0)

    def test_test_source_missing(self):
        with self.assertRaises(ValueError):
            tool.BookSourceTool().test_source("不存在")


class TestHandlers(ToolTestCase):
    """逐条走一遍路由的响应契约：宿主约定成功 `{"err": "ok", ...}`。"""

    def handler(self, cls, **kwargs):
        return cls(**kwargs)

    def test_list(self):
        tool.BookSourceTool().add_source(_source())
        rsp = self.handler(tool.ListHandler).get()
        self.assertEqual(rsp["err"], "ok")
        self.assertEqual(len(rsp["data"]), 1)

    def test_save_missing_raw(self):
        rsp = self.handler(tool.SaveHandler, body=json.dumps({}).encode()).post()
        self.assertEqual(rsp["err"], "params.missing")

    def test_save_bad_json_body(self):
        rsp = self.handler(tool.SaveHandler, body=b"{not json").post()
        self.assertEqual(rsp["err"], "params.missing")

    def test_save_ok(self):
        body = json.dumps({"raw": _source()}).encode()
        rsp = self.handler(tool.SaveHandler, body=body).post()
        self.assertEqual(rsp["err"], "ok")
        self.assertEqual(rsp["data"]["status"], "added")

    def test_toggle_not_found(self):
        body = json.dumps({"name": "不存在"}).encode()
        rsp = self.handler(tool.ToggleHandler, body=body).post()
        self.assertEqual(rsp["err"], "book_source.not_found")

    def test_delete_not_found(self):
        body = json.dumps({"name": "不存在"}).encode()
        rsp = self.handler(tool.DeleteHandler, body=body).post()
        self.assertEqual(rsp["err"], "book_source.not_found")

    def test_search_status_missing_task(self):
        self.assertEqual(self.handler(tool.SearchStatusHandler).get()["err"], "params.missing")
        rsp = self.handler(tool.SearchStatusHandler, arguments={"task_id": "nope"}).get()
        self.assertEqual(rsp["err"], "task.not_found")

    def test_search_async_missing_keyword(self):
        rsp = self.handler(tool.SearchAsyncHandler, body=json.dumps({}).encode()).post()
        self.assertEqual(rsp["err"], "params.missing")

    def test_search_async_rejects_non_list_source_names(self):
        body = json.dumps({"keyword": "x", "source_names": "oops"}).encode()
        rsp = self.handler(tool.SearchAsyncHandler, body=body).post()
        self.assertEqual(rsp["err"], "params.invalid")

    def test_download_missing_params(self):
        rsp = self.handler(tool.DownloadHandler, body=json.dumps({"source": "x"}).encode()).post()
        self.assertEqual(rsp["err"], "params.missing")

    def test_generate_epub_missing_params(self):
        rsp = self.handler(tool.GenerateEpubHandler, body=json.dumps({}).encode()).post()
        self.assertEqual(rsp["err"], "params.missing")

    def test_max_chapters_clamping(self):
        self.assertEqual(tool._max_chapters({"maxChapters": 0}), 1)
        self.assertEqual(tool._max_chapters({"maxChapters": 10 ** 9}), 9999)
        self.assertEqual(tool._max_chapters({"maxChapters": "abc"}), 9999)
        self.assertEqual(tool._max_chapters({}), 9999)

    def test_progress_without_task(self):
        rsp = self.handler(tool.ProgressHandler).get()
        self.assertEqual(rsp["err"], "ok")
        self.assertIsNone(rsp["data"])

    def test_progress_with_task(self):
        task_id = tool.BackgroundService().update_task(
            "tool:book_source", "书源管理", progress=42,
            progress_data={"status": "下载章节"}).id
        tool.BookSourceTool.set_last_task(1, task_id)
        rsp = self.handler(tool.ProgressHandler).get()
        self.assertEqual(rsp["data"]["task_id"], task_id)
        self.assertEqual(rsp["data"]["progress"], 42)

    def test_import_url_missing(self):
        rsp = self.handler(tool.ImportUrlHandler, body=json.dumps({}).encode()).post()
        self.assertEqual(rsp["err"], "params.missing")

    def test_import_zip_missing_file(self):
        self.assertEqual(self.handler(tool.ImportZipHandler).post()["err"], "params.missing")

    def test_deps_handler(self):
        rsp = self.handler(tool.DepsHandler).get()
        self.assertEqual(rsp["err"], "ok")
        self.assertIn("deps", rsp["data"])

    def test_cancel_rejects_unknown_task(self):
        body = json.dumps({"task_id": 12345}).encode()
        self.assertEqual(self.handler(tool.CancelHandler, body=body).post()["err"],
                         "task.not_found")

    def test_cancel_rejects_non_integer(self):
        body = json.dumps({"task_id": "abc"}).encode()
        self.assertEqual(self.handler(tool.CancelHandler, body=body).post()["err"],
                         "params.invalid")

    def test_cancel_only_own_task(self):
        """别的用户的任务不能被当前用户取消（宿主的 cancel_task 只按 id，不校验归属）。"""
        foreign = tool.BackgroundService().update_task("tool:other", "别的工具").id
        body = json.dumps({"task_id": foreign}).encode()
        rsp = self.handler(tool.CancelHandler, body=body).post()
        self.assertEqual(rsp["err"], "task.not_found")
        self.assertEqual(tool.BackgroundService().get_task(foreign)["status"], "running")

    def test_cancel_own_task(self):
        task_id = tool.BackgroundService().update_task("tool:book_source", "书源管理").id
        tool.BookSourceTool.set_last_task(1, task_id)
        body = json.dumps({"task_id": task_id}).encode()
        rsp = self.handler(tool.CancelHandler, body=body).post()
        self.assertEqual(rsp["err"], "ok")
        self.assertEqual(tool.BackgroundService().get_task(task_id)["status"], "cancelled")

    def test_download_epub_requires_login(self):
        handler = self.handler(tool.DownloadEpubHandler)
        handler.get()
        self.assertEqual(handler.status, 401)

    def test_download_epub_requires_admin(self):
        handler = self.handler(tool.DownloadEpubHandler, current_user=object())
        handler.get()
        self.assertEqual(handler.status, 403)

    def test_download_epub_serves_last_generated_file(self):
        epub_path = os.path.join(self.tmp, "book_source", "我的书.epub")
        os.makedirs(os.path.dirname(epub_path), exist_ok=True)
        with open(epub_path, "wb") as f:
            f.write(b"PK\x03\x04fake-epub")
        task_id = tool.BackgroundService().update_task("tool:book_source", "书源管理").id
        tool.BackgroundService().complete_task(task_id)
        tool.BackgroundService().tasks[task_id].progress_data = {"epub_path": epub_path}
        tool.BookSourceTool.set_last_task(1, task_id)

        handler = self.handler(tool.DownloadEpubHandler, current_user=object(), admin_user=object())
        handler.get()
        self.assertEqual(handler.headers["Content-Type"], "application/epub+zip")
        self.assertIn("filename*=UTF-8''", handler.headers["Content-Disposition"])
        self.assertEqual(handler.body_out, b"PK\x03\x04fake-epub")

    def test_download_epub_404_when_nothing_generated(self):
        handler = self.handler(tool.DownloadEpubHandler, current_user=object(), admin_user=object())
        handler.get()
        self.assertEqual(handler.status, 404)


class TestPendingGuardInfo(ToolTestCase):
    """后台任务与入库的接线：进度回调、入库参数、工作目录清理。"""

    def test_download_book_imports_and_cleans_up(self):
        book_source = tool.BookSourceTool()
        book_source.add_source(_source())

        calls = {}

        def fake_build(source, book_url, book_title, max_chapters, task_id):
            work_dir = book_source.get_work_dir(book_url)
            epub_path = os.path.join(work_dir, "示例.epub")
            with open(epub_path, "wb") as f:
                f.write(b"epub")
            calls["work_dir"] = work_dir
            return "示例书", "示例作者", epub_path

        book_source._build_epub = fake_build  # type: ignore[assignment]
        book_source.download_book("示例源", "https://example.com/book/1", user_id=1)

        self.assertEqual(len(book_source.imported_files), 1)
        imported = book_source.imported_files[0]
        self.assertEqual(imported["title"], "示例书")
        self.assertEqual(imported["authors"], ["示例作者"])
        self.assertEqual(imported["user_id"], 1)

        task = tool.BookSourceTool.get_last_task(1)
        self.assertEqual(task["status"], "completed")
        self.assertFalse(os.path.exists(calls["work_dir"]))

    def test_generate_epub_task_keeps_file(self):
        book_source = tool.BookSourceTool()
        book_source.add_source(_source())

        def fake_build(source, book_url, book_title, max_chapters, task_id):
            work_dir = book_source.get_work_dir(book_url)
            epub_path = os.path.join(work_dir, "示例.epub")
            with open(epub_path, "wb") as f:
                f.write(b"epub")
            return "示例书", "示例作者", epub_path

        book_source._build_epub = fake_build  # type: ignore[assignment]
        book_source.generate_epub_task("示例源", "https://example.com/book/1", user_id=1)

        self.assertEqual(book_source.imported_files, [])
        self.assertTrue(book_source.get_last_epub_path(1).endswith("示例.epub"))

    def test_failure_marks_task_failed(self):
        book_source = tool.BookSourceTool()
        book_source.add_source(_source())

        def boom(*args, **kwargs):
            raise ValueError("抓取失败")

        book_source._build_epub = boom  # type: ignore[assignment]
        book_source.download_book("示例源", "https://example.com/book/1", user_id=1)
        task = tool.BookSourceTool.get_last_task(1)
        self.assertEqual(task["status"], "failed")
        self.assertIn("抓取失败", task["error_message"])


class TestRouteWiring(unittest.TestCase):
    """路由表 / 鉴权装饰器 / 与 manifest 的一致性。"""

    def test_route_paths_match_manifest(self):
        manifest = json.load(io.open(os.path.join(PKG_ROOT, "manifest.json"), encoding="utf-8"))
        declared = {entry["path"] for entry in manifest["api_routes"]}
        wired = {path.rsplit("/", 1)[1] for path, _cls in tool.BOOK_SOURCE_ROUTES}
        self.assertEqual(declared, wired)
        for path, _cls in tool.BOOK_SOURCE_ROUTES:
            self.assertTrue(path.startswith("/api/toolbox/tool/book_source/"))

    def test_route_handlers_match_manifest(self):
        manifest = json.load(io.open(os.path.join(PKG_ROOT, "manifest.json"), encoding="utf-8"))
        for entry in manifest["api_routes"]:
            _module_name, cls_name = entry["handler"].rsplit(".", 1)
            self.assertTrue(hasattr(tool, cls_name), entry["handler"])
            self.assertTrue(issubclass(getattr(tool, cls_name), tool.BaseHandler),
                            "%s 必须继承 BaseHandler" % cls_name)

    def test_every_json_route_is_json_and_admin_guarded(self):
        """外部工具的动态路由不会自动注入鉴权（宿主只包一层"禁用即 404"），
        所以每个 JSON 路由都必须自己写上 `@js` + `@is_admin`。"""
        exempt = {tool.DownloadEpubHandler}  # 回字节流，不能用 @js，改显式判权
        for _path, cls in tool.BOOK_SOURCE_ROUTES:
            methods = [m for m in ("get", "post") if hasattr(cls, m)]
            self.assertTrue(methods, "%s 没有 get/post" % cls.__name__)
            for method in methods:
                func = getattr(cls, method)
                if cls in exempt:
                    self.assertFalse(getattr(func, "_js_decorated", False),
                                     "%s.%s 回的是字节流，不能被 @js 包" % (cls.__name__, method))
                    continue
                self.assertTrue(getattr(func, "_js_decorated", False),
                                "%s.%s 缺 @js" % (cls.__name__, method))
                self.assertTrue(getattr(func, "_admin_decorated", False),
                                "%s.%s 缺 @is_admin" % (cls.__name__, method))

    def test_download_epub_does_explicit_auth(self):
        """没走 @is_admin 的那条路由必须自己做登录 + 管理员判断（靠 status 断言兜住）。"""
        self.assertIn("_deny", dir(tool.DownloadEpubHandler))

    def test_tool_entry_methods_decorated(self):
        for name in ("list_sources", "add_source", "delete_source", "toggle_source",
                     "search_async", "get_search_status", "test_source", "import_sources_from_zip",
                     "import_sources_from_url"):
            func = getattr(tool.BookSourceTool, name)
            self.assertEqual(getattr(func, "_async_kind", None), "function", name)
        for name in ("download_book", "generate_epub_task"):
            func = getattr(tool.BookSourceTool, name)
            self.assertEqual(getattr(func, "_async_kind", None), "service", name)

    def test_no_direct_db_access(self):
        """工具代码应通过 CoreAPI（self.api.*）访问宿主数据，不裸摸 self.db / self.session。"""
        source = io.open(os.path.join(PKG_ROOT, "backend", "tool.py"), encoding="utf-8").read()
        for forbidden in ("self.db", "self.session", "self.sqlite_session"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
