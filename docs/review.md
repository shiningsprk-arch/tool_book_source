# 自审：按 MyBooks `toolbox-tool-review` 规范过一遍

对照对象是 MyBooks 核心仓库的 `.claude/skills/toolbox-tool-review/SKILL.md`
（a–f 六节）。审核对象：`backend/tool.py`（`BookSourceTool` + 15 个 Handler）、
`manifest.json`、`frontend/`。

**验证边界先说清楚**（避免把"静态核对"当成"真机跑过"）：

| 手段 | 覆盖 | 能证明什么 |
|---|---|---|
| `pytest tests`（218 项） | 引擎 + 后端 + 随包依赖 + 包形状 + 前端契约 | 引擎逻辑没被改造破坏（上游 119 项原样全绿）；CRUD/导入/任务/15 条路由的响应契约在本机真跑；随包 dukpy 副本与 `VENDOR.json` 逐字节一致、二进制架构与目录 tag 相符；文案键、元素 id、接口路径、包内文件一一对应 |
| `node dev/preview/ui_smoke.mjs`（29 项） | 前端交互 | 假宿主下真 fetch + 真 DOM 点一遍：标签页、筛选、启停、六个对话框、搜索轮询、分页、下载/生成进度、封面兜底、Esc/遮罩关闭，全程无未捕获异常 |
| 子进程真加载随包 dukpy | 内置依赖链路 | 用与当前解释器 ABI 匹配的副本跑通：`install()` 挑到副本 → `sys.modules['dukpy']` 指向副本内文件 → 引擎 `_HAS_DUKPY` 为真 → `run_js()` 真的执行 JS（`'aa'.replace('a','b')`='ba'、`/a/g` → 'bb'、`(1+2)+result`='3!'） |
| **未做** | 真机 | 15 条路由在真实 MyBooks 进程里的挂载、Calibre 入库、**cp312 真机上的随包 dukpy 首次加载**、真站点抓取、耗时 |

---

## a. 代码问题与命名规范 + 前端页面范围

**通过。** 逐条对照：

- `tool_id = book_source`（全小写下划线），与 `manifest.json`、`backend/tool.py`、
  `frontend/index.html` 的入口声明一致；工具类 `BookSourceTool` 用 PascalCase、继承 `BaseTool`。
- `service_item_name = "书源管理"` 已设置，由 `BaseTool.create_task()` 统一 `_()` 包裹。
- `info()` 是 `staticmethod`，字段齐全（`tool_id`/`name`/`description`/`revision`/`author`/
  `publish_date`/`repo_url`），且被 `tests/test_tool_offline.py::test_info_matches_manifest`
  钉死与 `manifest.json` 一致。
- 入口方法装饰器：`download_book` / `generate_epub_task` 用 `@AsyncService.register_service`
  （异步 + 后台任务进度）；其余 CRUD/查询用 `@AsyncService.register_function`（同步返回）。
  这是上游沿用下来的划分，`tests/test_tool_offline.py::test_tool_entry_methods_decorated`
  逐个用例钉住。
  ⚠️ 一处需要注意：`toolbox_design.md` 1.3 节说"入口方法都应当用 `register_service`"，
  但宿主的 `register_service` 在 `async_mode()` 为真时会把调用丢进后台队列并返回 `None`——
  CRUD 这类需要立即拿返回值的接口必须用 `register_function`。这也是 `tool-md-to-epub`
  外部工具踩过的坑（见 `tool_md_to_epub` 的注释）。
- 不裸摸宿主数据：全文件没有 `self.db` / `self.session` / `self.sqlite_session`
  （`test_no_direct_db_access` 会拦）。唯一绕过 CoreAPI 的是
  `BackgroundService().get_task()/cancel_task()`——`CoreAPI.tasks` 只暴露
  `create_task/update_progress/complete_task/make_progress_callback`，没有"按 id 查/取消"
  的读法，见 c 节说明。
- flake8（宿主 `.style.yapf` 的 `column_limit=240` + 同一份 ignore 列表）：`backend/tool.py`
  零告警。`backend/engine/` 有上游自带的 F401/W605/E302 告警——引擎是逐字节拷贝，不改。
- 前端对话框：不是 Vuetify 页面，`.claude/rules/ui.md` 的 `AppDialog`/主题色/按钮位置那套
  不适用；对齐的是外部工具脚手架自己的约定（`frontend/lib/theme.css` 的 `.mb-*` 组件 +
  `bridge.notify` 提示）。六个对话框都有唯一关闭入口（遮罩点击 / Esc / 显式关闭按钮），
  走后果的按钮（删除、批量下载）都要二次确认。
- 前端不越界：不触碰宿主 DOM 与全局状态（只 postMessage 主动上报高度、发 notify）；
  所有文案在自己的 `bookSource.*` 命名空间内；样式全部走 `.bs-*` 前缀与 CSS 变量，
  没有全局选择器（`body`/`h1`/`table` 等的样式都在本页 iframe 内，出不去）。
- 页面卸载清理定时器：`beforeunload` 里 `stopPoll(true)` + `stopSearchPoll(true)`。

## b. 提供给前端的接口定义是否完整

**通过。** 15 条路由全部在 `manifest.json` 的 `api_routes` 里声明，前缀由宿主拼成
`/api/toolbox/tool/book_source/...`；`tests/test_package_layout.py` 检查
handler 类真实存在，`tests/test_frontend_contract.py` 双向检查"前端调用的路径 ⊆ 声明的
路径"和"声明的路径都被前端调用"。

- 鉴权/JSON 装饰器：14 条 JSON 路由都带 `@js @is_admin`；`download_epub` 回字节流，
  按宿主 `AdminEpubBeautifyBgRaw` 的既有做法显式判权（见 c 节）。逐个断言在
  `test_every_json_route_is_json_and_admin_guarded`。
- 请求参数对照（前后端逐字段核过，Python 侧同样有用例）：

| 路由 | 参数 | 必填 | 前端实际传 |
|---|---|---|---|
| `save` | `raw`(obj，须含 `bookSourceName`) | ✅ | `{raw: obj}` |
| `toggle` / `delete` | `name`(str) | ✅ | `{name}` |
| `search_async` | `keyword`(str)、`source_names`(array?) | keyword ✅ | `{keyword}` |
| `search_status` | `task_id`(query) | ✅ | `?task_id=` |
| `test` | `source`(query) | ✅ | `?source=` |
| `download` / `generate_epub` | `source`、`bookUrl`、`bookTitle?`、`maxChapters?` | 前两个 ✅ | 四个都传 |
| `cancel` | `task_id`(int) | ✅ | `{task_id}` |
| `import_zip` | multipart `file` | ✅ | `FormData('file')` |
| `import_url` | `url`(str) | ✅ | `{url}` |

- 响应结构稳定包含 `err`；错误码枚举：`params.missing` / `params.invalid` /
  `book_source.invalid` / `book_source.not_found` / `book_source.import_failed` /
  `task.not_found` / `deps.missing`。前端对前四个都有分支，`task.not_found` 在
  `/progress` 轮询里被当作"任务已消失"处理（`startPoll` 里的专门分支），
  `deps.missing` 由启动时的 `checkDeps()` 转成顶部警告条。
- 异步任务类接口：`download` / `generate_epub` 立即返回，前端把任务推进自己的任务列表并
  2s 轮询 `/progress`；按钮在请求期间禁用防重复提交（`downloadingMap` / `generatingMap`），
  同时只允许一个活动任务（`state.activeTask` 门闩，与后端 `is_running()` 的单任务语义一致）。
- 每条 handler 都有 docstring 说明用途、方法、参数与（破坏性的）语义，可替代单独的接口文档。
- `import_zip` / `import_url` 的结果**如实**上报（`_import_response()`）：成功带
  `新增 X / 更新 Y / 跳过 Z` 计数；识别不到书源数据回 `book_source.import_failed` +
  "压缩包里没找到书源数据（看过：…）"；一条都没进来（例如全依赖 `<js>` 规则）也回失败。
  以前这两种都走"导入成功，新增 0 个书源"——看着成功、实际什么都没发生。

## c. 接口是否存在安全问题

**基本通过，两处按可接受风险保留（下面标 ⚠️）。**

- **鉴权**：见 b 节，14/15 条 `@is_admin`，第 15 条显式判权。另有宿主自动包的一层
  "工具被禁用 → 404"。未管理员访问 `download_epub` 时得到 403 而不是 500（有专门用例）。
- **输入校验**
  - `import_url`：`_is_safe_url()` 做 DNS 解析 + 私网/回环/链路本地/保留地址拒绝（SSRF 防护），
    解析失败也拒绝。有用例确认私网地址不会真的发出请求。
  - 分类浏览 `explore`：同样过 `_is_safe_url()`（上游行为，保留）。
  - `cancel` 的 `task_id`：强制 `int()`，**并校验该任务属于当前用户**
    （`get_last_task(user_id)['id'] == task_id`）——上游直接透传给
    `BackgroundService.cancel_task()`，任一管理员可以猜 id 取消别人的任务。
  - ⚠️ `save` / `test` / `download` / `generate_epub` 接受书源里的 URL 并让服务端去请求，
    没有 `_is_safe_url()` 限制。**有意不加**：自建书源服务常部署在内网，加了会直接让这类
    用户用不了；且这几条路由只对管理员开放（攻击者 = 管理员本人，SSRF 打自己的 NAS 没有
    实际收益）。上游同样不拦。如果将来工具开放给非管理员，这里必须补上。
  - Calibre 搜索语法注入：本工具**不**调用 `search_books`，无此面。
  - `maxChapters` 钳到 `[1, 9999]`，避免"抓 20 万章"这种请求把服务器拖死。
- **文件上传**
  - `import_zip`：只接受 multipart `file`，落盘到 `tempfile.NamedTemporaryFile(delete=False)`
    并在 `finally` 里删除；解析时只读 `importBookSource.json` / `importBookSource.txt`。
  - ⚠️ 上传大小上限交给宿主（Tornado `max_body_size`）；工具侧没有额外限制。zip 里唯一会被
    解析的文件名是白名单，且解压有条目数/内容上限之外的保护（见下条）。
  - **zip slip**：`safe_extract_zip()` 先逐条校验成员绝对路径落在目标目录内再解压，
    对齐宿主 `toolbox_manager._safe_extract()`。上游这里是裸的 `extractall()`，已修，
    并有 `test_rejects_path_traversal` / `test_import_rejects_traversal_zip` 两个用例钉住。
- **命令执行/反序列化**：无 `shell=True`、无 `eval`、无 `pickle`。JS 规则经 dukpy 沙箱，
  另挡掉 `java.ajax/java.post/java.getString` 与 `while(true)` 这类无界循环（引擎自带）。
- **内置第三方二进制**（`backend/vendor/dukpy/`）：包里带了三份**预编译**的原生模块
  （`.so`/`.pyd`，dukpy 0.6.0，MIT）。三点交代清楚：
  1. **来源可核**：`VENDOR.json` 记了每份对应的 PyPI wheel 文件名 + 原始 sha256 + 保留文件
     清单，`python scripts/fetch_vendor_wheels.py --check` 能离线复验，可对着 PyPI 上的文件
     核到字节；
  2. **做了裁剪**（删 `dukpy/jsmodules/`，只影响 dukpy 自己的 TS/Babel/LESS 编译器，
     引擎不调用），所以它**不是**官方 wheel 的原样副本——审核时按"修改过的 MIT 组件"看，
     修改说明在 `NOTICE.md`；
  3. **运行期不解压、不写盘**：用 `importlib` 把包目录当 `__path__` 直接加载，副本一直
     是包内的只读文件（见 e 节）。
  商店审核如果要"产物可由公开源码复现"，这一条需要在提交说明里单独讲；
  `python scripts/build.py --no-vendor` 可以打不带它的精简包。
- **密钥/敏感信息**：本工具不处理任何凭据，不写日志打印书源内容之外的敏感项。
- **速率与资源限制**：写入侧有节流（`MYBOOKS_FETCH_DELAY` + 抖动，引擎自带）；
  本工具是"用户手动触发 + 单任务门闩"，没有自动轮询/定时抓取，不会被重复触发耗尽资源。
  需要说明的是**没有并发上限之上的全局配额**，与上游一致。
- **CSRF**：所有 POST 都走宿主标准链路，工具侧没有关闭 xsrf 校验。

## d. 使用了 MyBooks 的哪些接口/能力

（下表是**实际调用到**的方法，不是规范里那张表的抄写。）

| 类别 | 命名空间/方法 | 操作类型 | 位置 / 说明 |
|---|---|---|---|
| 工具数据目录 | `CoreAPI.storage.get_work_dir()`（不带 key = 工具根目录） | 读 + 写（工具专属目录内） | `tool.py: _sources_path()`；书源表存在 `TOOL_DATA_ROOT/book_source/sources.json` |
| 工作目录 | `BaseTool.get_work_dir(book_url)` | 创建目录 | EPUB 产物落在 `TOOL_DATA_ROOT/book_source/<md5(book_url)[:16]>/` |
| 工作目录 | `BaseTool.cleanup_work_dir(work_dir)` | 删除（工具工作目录） | 下载入库成功后清理（`_do_download` 的 `finally`）；生成 EPUB 的产物**保留**供下载 |
| 后台任务 | `BaseTool.create_task` / `update_task_progress` / `complete_task` | 状态变更 | 任务的 service_type 由宿主按 `tool:book_source` 打标，落在任务面板 |
| Calibre 书库 | `BaseTool.import_file(user_id, path, title, [author])` | ⚠️ 写 | 仅 `download` 路径：`import_book` + 新建 `Item` 记录；`delete_after_import` 用默认 `True` |
| 任务查询 | `BackgroundService().get_task(task_id)` | 读 | `/progress`、`get_last_epub_path`、`/cancel` 的归属校验 |
| 任务取消 | `BackgroundService().cancel_task(task_id)` | 状态变更 | `/cancel`；先做归属校验再调用 |
| 站内消息 | 未使用 | — | — |
| 系统配置 | 未使用 | — | — |
| Calibre 读接口 | 未使用 | — | 工具不查书库，只入库 |

### 数据变更操作汇总

| 操作 | 触发条件 | 二次确认 | 归属/权限校验 | 可逆性 |
|---|---|---|---|---|
| 覆盖/新增书源（`save`） | 管理员点保存 | 编辑器内有 JSON 校验错误提示 | 管理员 | 可逆（再编辑回去），写入是 `tmp + os.replace` 原子替换 |
| 删除书源（`delete`） | 管理员点删除 | ✅ 前端 `dlg-delete` 二次确认（设置页里的删除按钮也先落到这个确认框） | 管理员 | **不可逆**（没有回收站，直接改写 `sources.json`） |
| 入库 EPUB（`download`） | 管理员点下载 | —（这是显式动作本身） | 管理员 + `user_id` 归属 | 不可逆，但只**新增**书库记录，不删改既有书籍；导入用的临时文件由 `cleanup_work_dir` 清掉 |
| 删除工作目录文件（`cleanup_work_dir`） | 入库成功后 | — | 仅限本工具自己的 `TOOL_DATA_ROOT/book_source/<hash>/` | 产物是中间文件，已入库副本不受影响 |
| 取消任务（`cancel`） | 管理员点取消 | — | ✅ 只允许本人最近一次本工具任务 | 任务状态变更 |

**没有任何** `delete_book` / `remove_formats` / `delete_item_by_book_id` 调用——
本工具不删书、不改既有书籍元数据。

## e. 文件读写：循环写入 / 工作目录范围 / 清理

**通过。**

- **工作目录范围**：所有落盘都在 `TOOL_DATA_ROOT/book_source/` 下——书源表在工具根目录，
  EPUB 产物在该根目录的 `<md5(book_url)[:16]>/` 子目录，全部通过
  `self.api.storage.get_work_dir()` / `BaseTool.get_work_dir()` 取得，没有拼 `/tmp`、
  没有写到用户提供的任意路径。zip 上传的中转文件用 `tempfile.NamedTemporaryFile`，
  在 `finally` 里删除。
- **随包副本零写入**：内置的 dukpy（`backend/vendor/dukpy/`）**不需要解压到临时目录**——
  用 `importlib.util.spec_from_file_location(..., submodule_search_locations=[...])` 把包目录
  当 `__path__` 直接加载，原生模块从包内只读路径 `dlopen`。并且加载期间显式
  `sys.dont_write_bytecode = True`：否则解释器会往 `backend/vendor/.../dukpy/__pycache__/`
  里写 `.pyc`，既让"运行期零写入"不成立，也会让随包清单（不允许出现清单外文件）在别人机器
  上校验失败——**这正是 CI 的 cp312 runner 上炸出来的问题**，现在有
  `test_loading_copy_leaves_no_bytecode_in_package` 钉住。所以运行期不会多出任何缓存、临时
  文件或残留（也因此天然没有"缓存目录竞态/被别的进程改写"的问题）。
- **路径穿越**：书名会被 `re.sub(r'[\\/:*?"<>|]', '_', title)` 净化后再拼进产物路径；
  zip 成员走 `safe_extract_zip()` 校验；`download_epub` 不接受任何路径参数，只回
  "该用户最近一次生成任务的产物路径"（该路径由服务端自己生成）。
- **循环写入/无界增长**
  - 章节循环有明确上限：`max_chapters ≤ 9999`，且只遍历目录里 `isVolume` 为假的条目；
    `total == 0` 时用 `max(total, 1)` 兜住（上游在这里会 `ZeroDivisionError`，虽然该分支
    实际不可达，顺手修了）。
  - 抓不到任何章节时直接抛错（`未获取到有效章节内容`），不会写出空 EPUB 或无限重试。
  - **书源表写入是"读全量 → 改 → 原子替换"**：`_save_sources()` 写 `sources.json.tmp`
    再 `os.replace()`，配合类级 `RLock`（`_sources_lock`）避免并发保存互相覆盖；
    异常路径不会留下半截文件（`os.replace` 之前失败则原文件不动）。
  - ⚠️ 生成 EPUB 的产物**不自动清理**——它就是要给用户下载的。上游也没有 TTL 回收，
    本工具包沿用该行为（把"要不要清"留给用户，或后续加一个手动清理入口）。
- **导入的写入次数**：`_import_items()` 在内存里合并、整包只 `_save_sources()` 一次（仍在
  `_sources_lock` 下），不是逐条"读全量→改→写全量"。原先逐条 `add_source()` 在真实书源包上
  会退化成 O(n²)：实测 800 条 23 秒、2973 条超过 5 分钟；改完 3287 条 0.79 秒。
- **导入的来源文件按内容识别**：扫 zip 里所有 `.json`/`.txt`，用 `_looks_like_sources()`
  按结构判断（有 `bookSourceName` + `bookSourceUrl`），无关 JSON 只记录"跳过 — 不是书源数据"，
  不会误当成书源；`importBookSource.*` 排最后处理，同名书源以它为准。
- **清理逻辑**：下载入库后 `finally: self.cleanup_work_dir(os.path.dirname(epub_path))`
  ——成功与失败都清（`import_file` 默认 `delete_after_import=True`，副本已在书库）。
  清理本身用 `BaseTool.cleanup_work_dir()`（失败只记警告），工具没有自己另写一套。
  `import_zip` 的临时文件在 `finally` 里 `os.unlink`。

## f. 数据安全

**通过。** 工具没有任何数据采集/上报行为。全部出站 HTTP 都发生在两个明确场景：

| 出站请求 | 目标 | 数据 | 触发 |
|---|---|---|---|
| 抓取书源内容（`requests` / 可选 `curl_cffi`） | **用户自己配置的书源站点** | 只发书源的 URL、搜索关键词、书源里声明的 header；不带任何 MyBooks 侧信息（无 token、无 user_id、无库内容） | 管理员点搜索/测试/下载 |
| `import_url` 拉取书源订阅 | 管理员填写的 URL | 只发一个 GET，UA 固定为常见浏览器串 | 管理员点导入 |

没有埋点、没有版本上报、没有回传统计。`jslib`/`@js:` 规则在 dukpy 沙箱里执行，
不能发网络请求（`java.ajax` 等一律拒绝）。

---

## 总体结论

**可以提交审核。** 阻断性安全问题：无。

需要审核方特别注意的几点：

1. `save` / `test` / `download` 不限制书源 URL 的内网访问（c 节 ⚠️）——有意保留，理由是
   "自建书源在内网"是真实用法，且本工具只对管理员开放。如果将来放宽到非管理员，必须补
   `_is_safe_url()`。
2. `import_zip` 的包大小上限依赖宿主 `max_body_size`（c 节 ⚠️）。
3. **包里带了三份预编译的 dukpy 原生模块**（c 节最后一条）：来源、sha256、裁剪内容都在
   `NOTICE.md` 与 `VENDOR.json` 里可核；如果商店要求"产物必须完全由公开源码构建"，
   用 `python scripts/build.py --no-vendor` 出精简包（代价：`@js:` 规则在官方 Docker 镜像
   上会退回降级，因为宿主 `requirements.txt` 里没有 dukpy）。
4. 生成 EPUB 的产物不自动回收，会在 `TOOL_DATA_ROOT/book_source/<hash>/` 下累积
   （e 节 ⚠️）——与上游一致，但外部工具无法像内置工具那样依赖宿主后续统一清理，建议商店
   审核时确认这是可接受的行为。
