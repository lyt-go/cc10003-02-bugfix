"""并发与事务回退验证（服务层，真实多线程 + 故障注入）。

运行：python -I concurrency_rollback.py
"""
from __future__ import annotations

import concurrent.futures
import os
import sys

# -I 模式下不读 PYTHONPATH/cwd，这里按脚本相对位置把后端目录加入模块搜索路径。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/backend")

from app.services import governance as gov  # noqa: E402
from app.services.dashboard import DashboardService  # noqa: E402
from app.services.metric import MetricService  # noqa: E402
from app.store import store  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, extra: object = None) -> None:
    print(("PASS" if cond else "FAIL"), name, "" if cond else f"-> {extra}")
    if not cond:
        failures.append(name)


class FaultingCard(dict):
    """在发布流程写 refresh_enabled 时抛错，模拟任一指标卡引用写入失败。

    __deepcopy__ 保留普通 dict 语义，避免快照阶段被故障逻辑拦截（故障只应发生在真实写入时）。
    """

    def __setitem__(self, key, value):  # type: ignore[no-untyped-def]
        if key == "refresh_enabled":
            raise RuntimeError("模拟指标卡引用写入失败")
        return super().__setitem__(key, value)

    def __deepcopy__(self, memo):  # type: ignore[no-untyped-def]
        return dict(self)


ds, ms = DashboardService(), MetricService()

# 准备：让 METR-0003 发布出 v2、v3，供并发切换 v2 -> v3
ms.run_action(3, "新建口径版本")
ms.run_action(3, "提交口径评审")
ms.run_action(3, "发布指标")
check("准备：METR-0003 v2 已发布",
      gov.find_version(store.find("metric", 3), "v2")["status"] == "已发布")
ms.run_action(3, "新建口径版本")
ms.run_action(3, "提交口径评审")
ms.run_action(3, "发布指标")
check("准备：METR-0003 v3 已发布",
      gov.find_version(store.find("metric", 3), "v3")["status"] == "已发布")
ms.run_action(1, "提交口径评审")
ms.run_action(1, "发布指标")

ds.create_entry({"看板编号": "DASH-0020", "看板名称": "并发看板", "布局模板": "l",
                 "刷新间隔": 300, "可见范围": "全员可见", "看板负责人": "o"})
new_id = max(row["id"] for row in store.rows("dashboard"))
# 卡片先钉在当前已发布的 v3，再发布 v4 作为并发切换的目标版本
ds.add_card(new_id, {"metric_id": 3, "metric_version": "v3", "card_grain": "天"})
sub_entry, sub_msg = ds.run_action(new_id, "提交看板评审")
check("准备：并发看板进入看板评审中", sub_entry is not None and sub_entry["status"] == "看板评审中", sub_msg)
ms.run_action(3, "新建口径版本")
ms.run_action(3, "提交口径评审")
ms.run_action(3, "发布指标")
check("准备：METR-0003 v4 已发布",
      gov.find_version(store.find("metric", 3), "v4")["status"] == "已发布")
base_revision = store.find("dashboard", new_id)["revision"]

# 1. 真实并行：8 线程同一 expected_revision 并发切换同一卡片版本，只能一次生效
def switch(_: int) -> tuple[bool, str]:
    entry, message = ds.switch_card_version(
        new_id, f"C-{new_id}-1",
        {"metric_version": "v4", "expected_revision": base_revision, "card_grain": "天"},
    )
    return entry is not None, message

with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    results = list(pool.map(switch, range(8)))
check("8 并发切换只有 1 次生效",
      sum(1 for ok, _ in results if ok) == 1, [m for _, m in results])
check("其余并发均报 revision 冲突",
      sum(1 for ok, msg in results if not ok and "并发冲突" in msg) == 7)
final_card = store.find("dashboard", new_id)["cards"][0]
check("最终引用 v4 且 revision 只 +1",
      final_card["metric_version"] == "v4"
      and store.find("dashboard", new_id)["revision"] == base_revision + 1)

# 2. 真实并行：重复发布同一看板，只能一次生效
def publish(_: int) -> tuple[bool, str]:
    entry, message = ds.run_action(new_id, "发布看板")
    return entry is not None, message

with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    pub_results = list(pool.map(publish, range(8)))
check("8 并发发布只有 1 次生效",
      sum(1 for ok, _ in pub_results if ok) == 1, [m for _, m in pub_results])
check("看板最终为已发布", store.find("dashboard", new_id)["status"] == "已发布")

# 3. 故障注入：发布过程中某张卡片引用写入抛错，整次发布整体回退
# 前置：METR-0001 也发布到 v1，保证两张卡引用都合法，故障才会发生在写入阶段而非前置校验。
ms.run_action(1, "提交口径评审")
ms.run_action(1, "发布指标")
check("准备：METR-0001 v1 已发布",
      gov.find_version(store.find("metric", 1), "v1")["status"] == "已发布")

ds.create_entry({"看板编号": "DASH-0022", "看板名称": "回退看板", "布局模板": "l",
                 "刷新间隔": 300, "可见范围": "全员可见", "看板负责人": "o"})
rb_id = max(row["id"] for row in store.rows("dashboard"))
ds.add_card(rb_id, {"metric_id": 3, "metric_version": "v4", "card_grain": "天"})
ds.add_card(rb_id, {"metric_id": 1, "metric_version": "v1", "card_grain": "天"})
rb = store.find("dashboard", rb_id)
rb["cards"][1] = FaultingCard(rb["cards"][1])
ds.run_action(rb_id, "提交看板评审")

before = store.snapshot()
before_status = rb["status"]
before_rev = rb["revision"]
entry, message = ds.run_action(rb_id, "发布看板")
check("故障发布返回失败并提示回退", entry is None and "回退" in message, message)
check("回退后看板状态保持发布前", store.find("dashboard", rb_id)["status"] == before_status)
check("回退后 revision 未推进", store.find("dashboard", rb_id)["revision"] == before_rev)


def business_tables(snap):  # type: ignore[no-untyped-def]
    # 审计表设计上跨事务保留（回退事件本身要留痕），因此只比对业务表是否整体回到发布前。
    return {name: rows for name, rows in snap.items() if name != "audit"}


after = store.snapshot()
check("业务表与发布前快照一致（全量回退，审计留痕除外）", business_tables(after) == business_tables(before))
check("回退事件写入审计",
      any("回退" in a["detail"] and a["result"] == "拒绝"
          for a in store.audits(module="dashboard", entry_id=rb_id)))

# 4. 故障排除后正常发布仍成功（回退没有污染后续事务）
rb["cards"][1] = dict(rb["cards"][1])
entry, message = ds.run_action(rb_id, "发布看板")
check("故障恢复后正常发布成功", entry is not None and entry["status"] == "已发布", message)

print()
if failures:
    print(f"{len(failures)} 项失败：{failures}")
    sys.exit(1)
print("并发与回退验证全部通过")
