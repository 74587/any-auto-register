"""测试期的数据库隔离。

``core.db`` 在 import 时就按 ``DATABASE_URL`` 建好 engine，所以这里必须在任何
测试模块 import 之前改环境变量 —— conftest 是 pytest 唯一保证先于测试模块执行
的入口。跑测试不再往仓库根目录写 ``account_manager.db``，也不会读到上一次跑
留下的脏数据。

建表同样放在这里：生产环境由 ``main.py`` 启动时调 ``init_db()``，测试里没人调，
不建表的话所有落库路径都会撞上 ``no such table``。``configs`` 表定义在
``core.config_store`` 里，不 import 它就不会进 ``SQLModel.metadata``。
"""

import os
import tempfile

_TMP_DB_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP_DB_DIR.name, 'test.db')}"

import core.config_store  # noqa: E402,F401  注册 configs 表
from core.db import init_db  # noqa: E402  必须在 DATABASE_URL 设好之后再 import

init_db()


def pytest_sessionfinish(session, exitstatus):
    from core.db import engine

    engine.dispose()
    try:
        _TMP_DB_DIR.cleanup()
    except PermissionError:
        pass
