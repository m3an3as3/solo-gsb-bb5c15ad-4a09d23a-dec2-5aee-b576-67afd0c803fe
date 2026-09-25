"""启动期持久库结构核对。

``Base.metadata.create_all`` 只会创建**缺失的表**，不会给已有表补列，因此
不能拿它处理半旧库（例如已有 ``events`` 表但缺少后续版本新增的
``reveal_deadline`` 列）：那样启动看似成功、``/health`` 报告就绪，业务
接口访问缺失列时才以 500 暴露。

启动时区分两种情况：

- **空库**（不存在任何应用表）：按 SQLAlchemy 模型 ``create_all`` 初始化
  全部应用表；
- **已有应用表**：按模型逐表核对表是否存在、每张已有表是否包含模型中的
  全部必需列。缺表或缺列时只收集清单并返回「未就绪」——**不补列、不在
  半旧库上补建其它表**，由调用方（lifespan / ``/health``）阻止 API 就绪；
  全部齐备则原样通过，旧数据保留不动。
"""

from dataclasses import dataclass, field

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

# 导入模型以保证全部应用表已注册到 Base.metadata（副作用导入）
from . import models  # noqa: F401
from .database import Base


@dataclass
class SchemaState:
    """结构核对结果。

    ready=False 时，``missing_tables`` 列出缺失的数据表名，
    ``missing_columns`` 按表名列出该表缺失的必需列名。
    """

    ready: bool
    empty_initialized: bool = False
    missing_tables: list[str] = field(default_factory=list)
    missing_columns: dict[str, list[str]] = field(default_factory=dict)

    def describe(self) -> str:
        """人类可读的缺失项清单（同时用于日志与 503 响应）。"""
        lines: list[str] = []
        if self.missing_tables:
            lines.append(f"缺失数据表：{', '.join(self.missing_tables)}")
        for table_name in sorted(self.missing_columns):
            columns = ", ".join(self.missing_columns[table_name])
            lines.append(f"数据表 {table_name} 缺少必需列：{columns}")
        return "；".join(lines)


def _application_tables():
    """全部应用表，按表名排序保证缺失清单顺序稳定。"""
    return sorted(Base.metadata.sorted_tables, key=lambda table: table.name)


def ensure_schema(engine: Engine) -> SchemaState:
    """按上述规则核对（必要时初始化）持久库结构，返回核对结果。

    空库执行 ``create_all``；半旧库不执行任何 DDL——既不 ALTER 补列，
    也不补建缺失的其它表，避免在结构不明的库上留下半迁移状态。
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    tables = _application_tables()
    app_table_names = [table.name for table in tables]

    # 空库：不存在任何一张应用表 -> 照常初始化全部应用表
    if not any(name in existing_tables for name in app_table_names):
        Base.metadata.create_all(bind=engine)
        return SchemaState(ready=True, empty_initialized=True)

    # 已有应用表：按 SQLAlchemy 模型核对全部应用表及必需列，只报告不补建
    missing_tables: list[str] = []
    missing_columns: dict[str, list[str]] = {}
    for table in tables:
        if table.name not in existing_tables:
            missing_tables.append(table.name)
            continue
        present_columns = {column["name"] for column in inspector.get_columns(table.name)}
        absent = [column.name for column in table.columns if column.name not in present_columns]
        if absent:
            missing_columns[table.name] = absent

    if missing_tables or missing_columns:
        return SchemaState(
            ready=False,
            empty_initialized=False,
            missing_tables=missing_tables,
            missing_columns=missing_columns,
        )

    # 完整旧库：结构与模型一致，原样通过、保留数据
    return SchemaState(ready=True, empty_initialized=False)
