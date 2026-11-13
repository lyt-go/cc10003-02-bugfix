"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

联动修复后这里额外承担三件事：
1. ``_lock`` 是跨指标/看板写操作的互斥锁，并发的发布、版本切换在同一把锁内串行化并二次校验；
2. ``audit`` 表保存审计记录，任何状态写入、引用变更、发布拦截都留痕；
3. ``snapshot`` / ``restore`` 提供整次看板发布的回退点，任一引用写入失败都能整体撤销。
"""
from __future__ import annotations

import copy
import threading
from typing import Any

from app.seed import build_seed_tables


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = build_seed_tables()
        self._tables.setdefault("audit", [])
        # 指标口径与看板发布共用一把锁：两端的状态写入与引用切换必须串行。
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def module_names(self) -> list[str]:
        return [name for name in sorted(self._tables) if name != "audit"]

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        """深拷贝当前全部表，作为事务回退点。"""
        return copy.deepcopy(self._tables)

    def restore(self, snapshot: dict[str, list[dict[str, Any]]]) -> None:
        """用回退点整体覆盖，撤销半成品写入。"""
        self._tables = copy.deepcopy(snapshot)

    def add_audit(self, record: dict[str, Any]) -> dict[str, Any]:
        audits = self.rows("audit")
        record = dict(record)
        record["id"] = max((int(row.get("id", 0)) for row in audits), default=0) + 1
        audits.append(record)
        return record

    def audits(self, *, module: str | None = None, entry_id: int | None = None) -> list[dict[str, Any]]:
        rows = list(self.rows("audit"))
        if module:
            rows = [row for row in rows if row.get("module") == module]
        if entry_id is not None:
            rows = [row for row in rows if int(row.get("entry_id", -1)) == entry_id]
        return rows

    def overview(self) -> dict[str, object]:
        pending_marks = ("草稿", "评审中", "验证中", "校验中", "评估中", "生成中")
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            pending = 0
            abnormal = 0
            for row in rows:
                # 老模块仍带 pending/abnormal 显式标记；metric/dashboard 改造后按状态与卡片健康度推导。
                if "pending" in row:
                    pending += 1 if row.get("pending") else 0
                elif any(mark in str(row.get("status", "")) for mark in pending_marks):
                    pending += 1
                if "abnormal" in row:
                    abnormal += 1 if row.get("abnormal") else 0
                elif row.get("status") == "已下线":
                    abnormal += 1
                elif name == "dashboard" and any(
                    not card.get("valid", True) for card in _dashboard_card_views(row)
                ):
                    abnormal += 1
            modules.append({"name": name, "created": len(rows), "pending": pending, "abnormal": abnormal})
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


def _dashboard_card_views(row: dict[str, Any]) -> list[dict[str, Any]]:
    # 延迟导入避免 store → governance → store 的循环依赖。
    from app.services import governance

    return [governance.resolve_card(card) for card in row.get("cards", [])]


store = Store()
