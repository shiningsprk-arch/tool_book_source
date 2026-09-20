# 书源引擎（MyBooks Toolbox 外部工具包）

Legado 3.0 兼容书源引擎的 **MyBooks 工具箱外部工具包**形态：书源管理、多源搜索、
正文抓取与 EPUB 生成，全部能力通过工具自己的页面 + 15 条接口提供。

- 本项目原本的形态是「桌面版 + 内置插件」，源码补丁在 `书源引擎插件/`（要改 MyBooks 核心
  仓库、走 PR）。本仓库的 `main` 现在是**改造后的外部工具包**：一个 `manifest.json` +
  `backend/` + `frontend/` 的 zip，走开发者模式本地上传安装，不需要改动核心仓库。
  改造清单与逐项差异见 [NOTICE.md](NOTICE.md)。
- 旧形态的源码**没有丢**，在同仓库的 `legacy-project` 分支（及其起点 tag
  `source-engine-archive-20260920`）里，包括 `书源引擎插件/`、`desktop/`、根目录的独立引擎
  与 `test_rule_engine.py`。
- 引擎代码（`backend/engine/`）与原项目**逐字节一致**（对 git blob 比对，见
  `scripts/port_from_upstream.py`），可随时用脚本重新同步对账。

## 安装

1. MyBooks 管理员在「系统设置」里打开开发者模式（`ENABLE_TOOLBOX_DEV_MODE`）；
2. `/admin/toolbox` → 上传 `dist/book_source-1.2.0.zip`；
3. **重启 MyBooks**（安装/卸载需要重启才生效；之后的禁用/启用即时生效）。

装好后工具出现在工具箱列表里，落地页 `/toolbox/book_source`。

## 包结构

```
book_source/
├── manifest.json          # 工具元数据 + 15 条 api_routes
├── icon.png               # 工具图标（/get/tool/book_source/icon）
├── LICENSE                # 上游 MIT 许可证全文
├── NOTICE.md              # 来源与修改说明
├── backend/
│   ├── __init__.py
│   ├── tool.py            # BookSourceTool + 15 个 Handler（含鉴权）
│   ├── dukpy_vendor.py    # 把随包内置的 dukpy 按当前解释器装上（宿主有就不装）
│   ├── vendor/dukpy/      # 随包依赖：裁剪过的 dukpy 0.6.0（3 个平台，MIT）
│   │   ├── VENDOR.json    #   来源 wheel 文件名 + sha256 + 保留文件清单
│   │   ├── LICENSE        #   dukpy 的 MIT 许可证全文
│   │   └── cp312-linux_x86_64/ · cp312-linux_aarch64/ · cp312-win_amd64/
│   └── engine/            # 规则引擎（上游逐字节拷贝）
│       ├── book_source_model.py
│       ├── rule_engine.py           # CSS/JSONPath/XPath/Legado/JS 规则 + 反爬层
│       ├── js_runtime.py            # dukpy JS 沙箱（依赖由随包副本兜底）
│       ├── content_fallbacks.py     # 站点级内容兜底
│       ├── epub_helper.py           # EPUB 生成
│       └── search_task_service.py   # 多源异步搜索
├── frontend/
│   ├── index.html         # 自包含静态页（宿主 iframe 里加载）
│   ├── app.js
│   ├── lib/theme.css      # 脚手架共享视觉基础
│   ├── lib/i18n.js        # 脚手架 i18n 胶水
│   └── locales/{zh,en,zh-TW,manifest}.json
├── scripts/               # 不进包
│   ├── build.py           # 校验 + 打包 + sha256 + 官方 mytool validate
│   ├── make_icon.py       # 生成 icon.png
│   ├── fetch_vendor_wheels.py  # 生成/校验 backend/vendor/dukpy（按平台裁剪）
│   └── port_from_upstream.py   # 从上游仓库同步引擎 / 文案 / 单测
├── tests/                 # 不进包
└── dev/preview/           # 不进包：本地假宿主，无 MyBooks 也能点界面
```

## 接口（`/api/toolbox/tool/book_source/...`）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `list` | 书源列表 |
| POST | `save` | 新增/更新书源（`{raw}`） |
| POST | `toggle` | 启用/停用（`{name}`） |
| POST | `delete` | 删除（`{name}`，破坏性） |
| POST | `search_async` | 异步多源搜索（`{keyword, source_names?}`） |
| GET | `search_status?task_id=` | 搜索进度与结果 |
| GET | `test?source=` | 单源连通性测试 |
| POST | `download` | 下载书籍并入库（`{source, bookUrl, bookTitle?, maxChapters?}`） |
| POST | `generate_epub` | 生成 EPUB 供下载 |
| POST | `cancel` | 取消后台任务（`{task_id}`，仅限本人最近一次任务） |
| GET | `progress` | 当前用户最近一次任务进度 |
| POST | `import_zip` | 上传 zip 导入书源（multipart `file`） |
| POST | `import_url` | 从 URL 导入书源（`{url}`） |
| GET | `download_epub` | 下载最近生成的 EPUB（`application/epub+zip`） |
| GET | `deps` | 运行环境能力探测（缺哪个依赖、JS 规则/EPUB 是否可用） |

响应统一是 `{"err": "ok", ...}` / `{"err": "<错误码>", "msg": "..."}`；错误码见
`tests/test_tool_offline.py` 的用例名。

外部工具的动态路由**不会**自动注入鉴权（宿主只包一层"工具被禁用就 404"），所以除
`download_epub`（回字节流，用显式判权）外每条路由都自带 `@js @is_admin`。

## 运行依赖

宿主自带的依赖直接用；缺依赖时**降级并如实上报**（`GET deps`，前端在页面顶部显示一条警告）。
MyBooks 的 `requirements.txt` 里**没有 dukpy**（`@js:` 规则要用它），外部工具又改不了宿主
依赖 —— 所以这个工具包**内置了一份 dukpy**（`backend/vendor/dukpy/`，按平台裁剪，见
[NOTICE.md](NOTICE.md)）。

| 依赖 | 来源 | 缺失后果 |
|---|---|---|
| `requests` / `bs4` / `lxml` | 宿主（`requirements.txt` 已有） | 引擎加载失败，工具只剩报错（`deps.missing`） |
| `ebooklib` | 宿主（`EbookLib==0.19`） | 「生成 EPUB / 下载」报错，搜索与书源管理仍可用 |
| **`dukpy`** | **随包内置**（宿主有就优先用宿主的） | 依赖 `@js:` 规则的书源不可用（校验会明确标 `js_unsupported`） |
| `chardet` | 宿主 | 按 utf-8 解码，个别站点乱码 |
| `curl_cffi` | 宿主 | 退回 `requests`，没有 Chrome TLS 指纹伪装 |

dukpy 的选法（`backend/dukpy_vendor.py`）：宿主已装 → 用宿主的（最自洽）；否则按
`cp<版本>-<平台>_<架构>` 找随包副本；都没有（macOS / Alpine musl / 别的 Python 版本不在
随包矩阵里）→ 走降级，前端会提示"包里带了哪些、当前需要哪个"。

想覆盖不在矩阵里的场景，或者宿主那份 dukpy 有问题，用环境变量：

| 变量 | 作用 |
|---|---|
| `BOOK_SOURCE_VENDOR_DIR` | 指向自备副本根目录（`os.pathsep` 分隔多个），期望 `<tag>/dukpy/…` 结构；用 `python scripts/fetch_vendor_wheels.py --only cp312-macos_arm64 --out-dir <目录>` 生成即可 |
| `BOOK_SOURCE_PREFER_BUNDLED` | `1`/`true` 时跳过宿主那份，强制用包内副本（排查用） |

## 开发

```bash
# 跑测试（不需要 MyBooks/Calibre；引擎用真依赖，宿主接口用 tests/_host_stub.py 的替身）
python -m pytest tests -q

# 本地预览：起一个假宿主，浏览器里点界面（真 fetch + 真 DOM）
node dev/preview/server.mjs            # http://127.0.0.1:8770/get/tool/book_source/index.html

# UI 自动化冒烟（jsdom；需要 npm i jsdom）
node dev/preview/ui_smoke.mjs

# 从原始形态分支重新同步引擎 / 三语文案 / 上游单测
git clone -b legacy-project https://github.com/shiningsprk-arch/tool_book_source /tmp/bse
python scripts/port_from_upstream.py /tmp/bse/书源引擎插件

# 生成/校验随包 dukpy 副本（3 个平台，约 4.5 MB 压缩后）
python scripts/fetch_vendor_wheels.py            # 生成（联网，从 PyPI 拉官方 wheel 再裁剪）
python scripts/fetch_vendor_wheels.py --check    # 只校验 sha256 与文件清单（离线）

# 校验 + 打包（产出 dist/book_source-1.2.0.zip，打印 sha256，并跑官方 mytool validate）
python scripts/build.py
python scripts/build.py --no-vendor             # 不带内置 dukpy 的精简包（约 130 KB）
```

`scripts/build.py` 打完包会把产物交给官方脚手架再校验一遍
（`node ../tools_builder/bin/mytool.js validate dist/*.zip`），"符合规范"以那一步通过为准。

## 发布产物与可复现构建

- 可直接安装的包在 [Release v1.2.0](https://github.com/shiningsprk-arch/tool_book_source/releases/tag/v1.2.0)：
  `book_source-1.2.0.zip`（4.7 MB / 96 条目，sha256 `5276c18e…`）。CI 每次跑完也会把它作为
  artifact 上传。
- **构建可复现**：同一个 commit 在 Windows/cp313 与 ubuntu/cp312 上执行
  `python scripts/build.py`，打出来的字节完全相同。做法是固定掉所有会变的字段：时间戳（取
  `manifest.publish_date`）、条目顺序、权限位，以及 zip 头里那个跟平台相关的 "version made by"
  宿主字段（`ZipInfo.create_system`，不固定就是 Windows=0/DOS、Linux=3/UNIX 的差别 —— 这一条
  是拿 CI 产物和本机产物逐字节比对才发现的）。所以仓库/Release 里记的 sha256 在哪儿构建都成立。
- CI（`.github/workflows/ci.yml`）跑在 **cp312 的 Linux runner** 上（对齐 MyBooks 官方镜像的
  系统 python3），并且**故意不装 dukpy**，好让 `backend/vendor/dukpy/cp312-linux_x86_64` 被真正
  选中、真正加载 —— 本机开发环境（Windows/其它 ABI）补不上这一环。

## 相对上游的行为差异

完整说明见 [NOTICE.md](NOTICE.md)，要点：

1. **ZIP 导入加了路径穿越防护**：上游 `import_sources_from_zip()` 直接
   `zipfile.extractall()`，成员名带 `../` 的恶意 zip 能写到解压目录之外；本工具包改为先逐条
   校验成员路径再解压（对齐宿主 `toolbox_manager._safe_extract()`）。
2. **补齐 3 个漏掉的文案键**：上游 Vue 页面用到 `searchHint` / `confirmDownloadAllMsg` /
   `testReachable`，但三个 locale 文件里都没有，界面上会直接显示原始 key。
3. **取消任务校验归属**：宿主 `BackgroundService.cancel_task()` 只按 task_id 取消、不校验
   归属，透传会让任一管理员猜 id 取消别人的任务；现在只允许取消本人最近一次本工具任务。
4. 前端从 Vuetify2 的 Nuxt 页面重写为自包含静态页（外部工具跑在 iframe 里，拿不到宿主的
   Vue/Vuetify）；文案改为外部工具自带的 `frontend/locales/*.json`（扁平键），翻译内容逐条
   与上游一致。
5. 新增 `GET deps` 与页面顶部的能力提示（内部工具缺依赖会直接崩，外部工具需要能解释降级）。
6. **内置 dukpy**（上游走的是"往宿主 `requirements.txt` 加 `dukpy`"，外部工具做不到），
   所以 `@js:` 规则在 MyBooks 官方 Docker 镜像（Ubuntu 24.04 / CPython 3.12 / x86_64 与
   aarch64）上开箱可用。

## 已知限制

- **引擎保持上游实现不变**，包括它已知的行为：`@js:` 规则只支持 `result.replace()` 之类的
  字段后处理，`java.ajax` / `<js>` 块一律拒绝执行（`JsRuleUnsupported`）。
- **同一用户同一时刻只跑一个下载/生成任务**（上游设计：`is_running()` 单任务门闩），
  「批量下载」是串行排队。
- **书源校验/下载会按书源里的 URL 发服务端请求**：`import_url` 与分类浏览有 SSRF 防护
  （拒绝内网/回环地址），但对「已保存的书源」不做此限制——自建书源服务常在内网，加限制会
  直接破坏正常用法；这条路由只对管理员开放，故按可接受风险保留（见 docs/review.md）。
- **内置 dukpy 是有平台范围的**：只覆盖 MyBooks 实际发布的 cp312 + Linux(x86_64/aarch64)
  + Windows(amd64)。macOS / Alpine musl / 其它 Python 版本上会退回降级（前端会说明原因，
  可用 `BOOK_SOURCE_VENDOR_DIR` 自己补一份）。包体因此从 130 KB 涨到约 4.7 MB。
- **真机上未验证的部分**：15 条路由在真实 MyBooks 进程里的挂载、Calibre 入库、
  随包 dukpy 在 cp312 真机上的首次加载、整本抓取耗时。本机是 cp313/Windows，只能做到
  "离线把逻辑跑通 + 用本机 ABI 真加载一遍随包副本 + 静态核对宿主符号"。
  详见 docs/review.md 的「验证边界」。
