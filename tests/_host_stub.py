# -*- coding: utf-8 -*-
"""用最小替身顶掉 `webserver.*`，让 `backend/tool.py` 在没有 MyBooks / Calibre 的机器上：

- 能被 import（外部工具的后端本来就是在宿主进程里 import 的，脱离宿主连导入都做不到）；
- 能被实例化、能跑真实的书源 CRUD / 导入 / 任务进度 / 路由响应契约。

只用于本工具包的离线测试。真实运行环境是宿主进程，那边用的是真的 `webserver`。
替身的形状（方法名、参数、返回字段）是从宿主这两个文件里逐条抄出来的，抄错会让测试变成
"自己骗自己"，所以刻意保持最小、并把来源写清楚：

- `webserver/toolbox/base_tool.py`（BaseTool）
- `webserver/toolbox/core_api.py`（CoreAPI / StorageAPI）
- `webserver/services/background_service.py`（BackgroundService / BackgroundTask）
- `webserver/handlers/base.py`（BaseHandler / js / is_admin / user_id）
"""
import hashlib
import os
import shutil
import sys
import tempfile
import types

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# 后台任务替身
# ---------------------------------------------------------------------------

class BackgroundTask:
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELLED = "cancelled"
    STATUS_FAILED = "failed"
    SERVICE_TYPE_OTHER = "other"


class _Task:
    _next_id = 1

    def __init__(self, service_type, service_item, progress, progress_data):
        self.id = _Task._next_id
        _Task._next_id += 1
        self.service_type = service_type
        self.service_item = service_item
        self.progress = progress
        self.progress_data = progress_data or {}
        self.status = BackgroundTask.STATUS_RUNNING
        self.error_message = None

    def to_dict(self):
        return {
            "id": self.id,
            "service_type": self.service_type,
            "service_item": self.service_item,
            "progress": self.progress,
            "progress_data": self.progress_data,
            "status": self.status,
            "error_message": self.error_message,
        }


class BackgroundService:
    """与宿主同名同形的内存替身（宿主里是数据库表）。"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(BackgroundService, cls).__new__(cls)
            cls._instance.tasks = {}
        return cls._instance

    def reset(self):
        self.tasks = {}

    def update_task(self, service_type, service_item, progress=0, progress_data=None):
        task = _Task(service_type, service_item, progress, progress_data)
        self.tasks[task.id] = task
        return task

    def update_progress(self, task_id, progress, progress_data=None):
        task = self.tasks.get(task_id)
        if task is None:
            return False
        task.progress = progress
        if progress_data is not None:
            task.progress_data = progress_data
        return True

    def complete_task(self, task_id, error_message=None):
        task = self.tasks.get(task_id)
        if task is None:
            return False
        task.status = BackgroundTask.STATUS_COMPLETED if error_message is None \
            else BackgroundTask.STATUS_FAILED
        task.error_message = error_message
        if error_message is None:
            task.progress = 100
        return True

    def cancel_task(self, task_id):
        task = self.tasks.get(task_id)
        if task is None or task.status != BackgroundTask.STATUS_RUNNING:
            return False
        task.status = BackgroundTask.STATUS_CANCELLED
        return True

    def get_task(self, task_id):
        task = self.tasks.get(task_id)
        return task.to_dict() if task else None


# ---------------------------------------------------------------------------
# BaseTool / CoreAPI 替身
# ---------------------------------------------------------------------------

class StorageAPI:
    def __init__(self, owner):
        self._owner = owner

    def get_work_dir(self, unique_key=None):
        return self._owner.get_work_dir(unique_key or "")

    def cleanup_work_dir(self, work_dir):
        self._owner.cleanup_work_dir(work_dir)


class TasksAPI:
    def __init__(self, owner):
        self._owner = owner

    def create_task(self, progress_data=None):
        return self._owner.create_task(progress_data)


class CoreAPI:
    def __init__(self, owner):
        self._owner = owner
        self.storage = StorageAPI(owner)
        self.tasks = TasksAPI(owner)


class BaseTool:
    service_item_name = ""
    TOOL_DATA_ROOT = os.path.join(tempfile.gettempdir(), "mybooks_toolbox_stub")
    TOOL_SERVICE_TYPE = None
    SUPPORTED_FORMATS = {"epub", "pdf", "azw3", "mobi", "txt"}

    def __init__(self):
        self.api = CoreAPI(self)
        # 测试用：import_file 的调用记录（宿主里会真的落库）
        self.imported_files = []

    @staticmethod
    def info():
        raise NotImplementedError

    @classmethod
    def tool_id(cls):
        return cls.info()["tool_id"]

    def get_work_dir(self, unique_key=""):
        if unique_key:
            key_hash = hashlib.md5(unique_key.encode()).hexdigest()[:16]
            work_dir = os.path.join(self.TOOL_DATA_ROOT, self.tool_id(), key_hash)
        else:
            work_dir = os.path.join(self.TOOL_DATA_ROOT, self.tool_id())
        os.makedirs(work_dir, exist_ok=True)
        return work_dir

    def cleanup_work_dir(self, work_dir):
        shutil.rmtree(work_dir, ignore_errors=True)

    def create_task(self, progress_data=None):
        task = BackgroundService().update_task(
            service_type=self.TOOL_SERVICE_TYPE or BackgroundTask.SERVICE_TYPE_OTHER,
            service_item=self.service_item_name,
            progress=0,
            progress_data=progress_data or {},
        )
        return task.id

    def update_task_progress(self, task_id, progress, progress_data=None):
        BackgroundService().update_progress(task_id, progress, progress_data or {})

    def complete_task(self, task_id, error_message=None):
        BackgroundService().complete_task(task_id, error_message=error_message)

    def import_file(self, user_id, file_path, title, authors, *, delete_after_import=True):
        self.imported_files.append({
            "user_id": user_id, "file_path": file_path, "title": title,
            "authors": list(authors or []), "delete_after_import": delete_after_import,
        })
        return len(self.imported_files)


# ---------------------------------------------------------------------------
# Handler 替身
# ---------------------------------------------------------------------------

class BaseHandler:
    """可脱离 Tornado 直接实例化的 BaseHandler 替身。"""

    def __init__(self, body=b"", arguments=None, files=None,
                 current_user=None, admin_user=None, user_id=1):
        self.request = types.SimpleNamespace(body=body, files=files or {})
        self._arguments = dict(arguments or {})
        self._user_id = user_id
        self.current_user = current_user
        self.admin_user = admin_user
        self.status = None
        self.headers = {}
        self.body_out = b""

    def get_argument(self, name, default=""):
        return self._arguments.get(name, default)

    def get_arguments(self, name):
        value = self._arguments.get(name)
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    def user_id(self):
        return self._user_id

    def set_status(self, status):
        self.status = status

    def set_header(self, name, value):
        self.headers[name] = value

    def write(self, chunk):
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        self.body_out += chunk

    def finish(self):
        pass


def js(func):
    """宿主版本负责写 JSON + 兜异常；这里只标记，测试直接调用被装饰的函数拿返回值。"""
    func._js_decorated = True
    return func


def is_admin(func):
    """宿主版本会拦截非管理员；这里只标记，由测试断言"每个 JSON 路由都标了"。"""
    func._admin_decorated = True
    return func


class AsyncService:
    """宿主版本会注入 db / 把 register_service 丢进后台队列；这里原样透传，
    这样测试能同步拿到返回值（否则 download/generate 的返回值恒为 None）。"""

    @staticmethod
    def register_function(func):
        func._async_kind = "function"
        return func

    @staticmethod
    def register_service(func):
        func._async_kind = "service"
        return func

    def setup(self, calibre_db=None, scoped_session=None, need_check_db=False):
        self.db = calibre_db
        self.scoped_session = scoped_session

    def async_mode(self):
        return True


# ---------------------------------------------------------------------------
# 安装
# ---------------------------------------------------------------------------

def _module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


def install():
    """把替身挂进 sys.modules，并返回加载好的 `backend.tool` 模块（幂等）。"""
    if "webserver" in sys.modules and getattr(sys.modules["webserver"], "_mybooks_stub", False):
        return sys.modules["backend.tool"] if "backend.tool" in sys.modules else _load_tool()

    root = _module("webserver")
    root._mybooks_stub = True
    root.__path__ = []

    i18n = _module("webserver.i18n", _=lambda text: text)
    root.i18n = i18n

    handlers = _module("webserver.handlers")
    handlers.__path__ = []
    root.handlers = handlers
    base = _module("webserver.handlers.base", BaseHandler=BaseHandler, js=js, is_admin=is_admin)
    handlers.base = base

    services = _module("webserver.services", AsyncService=AsyncService)
    services.__path__ = []
    root.services = services
    bg = _module("webserver.services.background_service",
                 BackgroundService=BackgroundService, BackgroundTask=BackgroundTask)
    services.background_service = bg

    toolbox = _module("webserver.toolbox")
    toolbox.__path__ = []
    root.toolbox = toolbox
    base_tool = _module("webserver.toolbox.base_tool", BaseTool=BaseTool)
    toolbox.base_tool = base_tool

    _module("webserver.loader", get_settings=lambda: types.SimpleNamespace(get=lambda *a, **k: None))

    return _load_tool()


def _load_tool():
    import importlib.util
    if "backend.tool" in sys.modules:
        return sys.modules["backend.tool"]
    if PKG_ROOT not in sys.path:
        sys.path.insert(0, PKG_ROOT)
    spec = importlib.util.spec_from_file_location(
        "backend.tool", os.path.join(PKG_ROOT, "backend", "tool.py"))
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "backend"
    sys.modules["backend.tool"] = module
    spec.loader.exec_module(module)
    return module
