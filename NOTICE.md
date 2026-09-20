# NOTICE / 来源与修改说明

本工具包（`book_source`，MyBooks Toolbox 外部工具）是 **book-source-engine**（书源管理
插件 / 桌面版）的**衍生作品**，按原项目的 MIT 许可证再分发。

- **原始项目**：`book-source-engine` — 书源管理插件（桌面版 + MyBooks 内置插件两套形态）
  仓库：<https://github.com/shiningsprk-arch/tool_book_source>（本仓库，2026-09-20 从 `book-source-engine` 改名而来）
  原始形态源码：本仓库 `legacy-project` 分支 / tag `source-engine-archive-20260920`，
  例如 `git show source-engine-archive-20260920:书源引擎插件/webserver/toolbox/book_source_engine/rule_engine.py`
  许可证：MIT（Copyright (c) 2024 MyBooks Book Source Plugin，全文见同目录 `LICENSE`）

## 修改说明（相对原始形态的 `书源引擎插件/` 目录）

`书源引擎插件/` 是一份**内置插件形态的源码补丁**（`webserver/handlers/`、
`webserver/toolbox/`、`app/pages/toolbox/*.vue`、`app/locales/*`），需要改动 MyBooks
核心仓库、走 PR 合并。本工具包把同一份实现**改造为 MyBooks Toolbox 的外部工具包形态**
（`manifest.json` + `backend/` + `frontend/`），目标是走开发者模式（`ENABLE_TOOLBOX_DEV_MODE`）
本地上传安装，不需要改动核心仓库。

具体改动：

| 位置 | 上游 | 本工具包 | 说明 |
|---|---|---|---|
| `backend/engine/`（7 个模块） | `webserver/toolbox/book_source_engine/` | 原样搬运，一行未改 | 规则引擎/JS 运行时/兜底抓取/EPUB/搜索任务服务 |
| `backend/tool.py` | `webserver/toolbox/book_source_tool.py` + `webserver/handlers/book_source_api.py` | 两者合并进一个文件 | 工具类与 14 条路由的 Handler 同处一个模块，便于宿主 `collect_tool_routes()` 复用同一份模块对象 |
| 路由路径 | `/api/toolbox/book_source/<action>`（写死在 `handlers/toolbox.py` 的 `routes()`） | `/api/toolbox/tool/book_source/<action>`（`manifest.json` 的 `api_routes`） | 外部工具的路由前缀由宿主拼接 |
| 前端 | `app/pages/toolbox/book_source.vue`（Vuetify2 + Nuxt 页面） | `frontend/index.html` + `frontend/app.js`（自包含原生静态页，走 `toolbox-bridge.js`） | 外部工具前端运行在 iframe 内，不依赖宿主的 Vue/Vuetify |
| 文案 | 合并进 `app/locales/{zh,en,zh-TW}.json` 的 `bookSource` 命名空间 | `frontend/locales/*.json`，键名扁平化为 `bookSource.xxx` | 外部工具自带文案，翻译内容逐条一致 |
| `tools/merge_locales.py` | 存在 | 删除 | 只有内置形态才需要往宿主 locale 文件里合并 |
| 依赖声明 | `requirements.txt` 追加 `dukpy` | 不改宿主，运行时探测 | 外部工具不能改宿主依赖，缺依赖时降级并给出明确提示 |

除上述改造外，另修了两处上游问题（都在本工具包内）：

1. **ZIP 导入的路径穿越**：上游 `import_sources_from_zip()` 直接 `zipfile.extractall()`，
   恶意 zip 里带 `../` 的成员会写到解压目录之外。本工具包改为先逐条校验成员路径再解压
   （对齐宿主 `toolbox_manager._safe_extract()` 的做法）。
2. **三语文案缺 3 个键**：上游 Vue 页面用到 `searchHint` / `confirmDownloadAllMsg` /
   `testReachable`，但三个 locale 文件里都没有，界面会直接显示原始 key。已补齐。

引擎代码本身（`backend/engine/`）与原项目保持逐字节一致，便于与上游对账、后续同步。

## 随包第三方组件：dukpy

`backend/vendor/dukpy/` 里带了 **dukpy 0.6.0**（`@js:` 书源规则要用的 JS 引擎，Duktape 的
Python 绑定）——因为 MyBooks 的 `requirements.txt` 里没有它，而外部工具包改不了宿主依赖。

- 上游：<https://github.com/amol-/dukpy>，MIT，Copyright (c) 2014 Alessandro Molina
  （许可证全文见 `backend/vendor/dukpy/LICENSE`，与 wheel 内 `licenses/LICENSE` 逐字节一致）
- 来源：PyPI `dukpy==0.6.0` 的官方 wheel；每个平台的原始 wheel 文件名与 sha256 记在
  `backend/vendor/dukpy/VENDOR.json`，可据此核对到 PyPI 上的具体文件
- **做了裁剪**：删掉了 `dukpy/jsmodules/`（TypeScript/Babel/React 编译器，各平台约 10.8 MB）。
  它们只被 `dukpy.jsx_compile` / `typescript_compile` / `less_compile` 在**调用时**读取，
  规则引擎从不调用这些函数（只用 `JSInterpreter`）。裁掉后每个平台只剩原生模块 + ~50 KB
  纯 Python，包体积从 11.4 MB 降到 4.5 MB
- 平台矩阵按 MyBooks 实际发布的形态定（依据见 `scripts/fetch_vendor_wheels.py` 的注释）：
  `cp312-linux_x86_64`（Docker x86_64）、`cp312-linux_aarch64`（multiarch/NAS）、
  `cp312-win_amd64`（Windows 安装器）
- 重新生成/校验：`python scripts/fetch_vendor_wheels.py`（生成）、加 `--check`（只校验
  sha256 与文件清单，不联网）
- 宿主**自己装了** dukpy 时优先用宿主的；镜像/ABI 不匹配时如实降级，不崩
  （见 `backend/dukpy_vendor.py`）
- 不想要这个内置依赖：`python scripts/build.py --no-vendor` 打精简包（约 130 KB），
  JS 规则退回"宿主有就用、没有就降级"
