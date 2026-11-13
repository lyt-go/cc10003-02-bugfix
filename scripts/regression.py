"""指标口径-看板联动全量回归（服务层，单进程顺序校验全部业务规则）。

运行：python -I regression.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/backend")

from app.services.dashboard import DashboardService  # noqa: E402
from app.services.metric import MetricService  # noqa: E402
from app.store import store  # noqa: E402

failures: list[str] = []


def check(name, cond, extra=None):  # type: ignore[no-untyped-def]
    print(("PASS" if cond else "FAIL"), name, "" if cond else f"-> {extra}")
    if not cond:
        failures.append(name)


ms, ds = MetricService(), DashboardService()

# ---- 状态机单向推进：禁止草稿直发、已发布回流评审、跳级下线 ----
e, msg = ms.run_action(1, "发布指标"); check("草稿直发指标被拒", e is None and "跳过评审" in msg, msg)
e, msg = ms.run_action(1, "下线指标"); check("草稿跳级下线被拒", e is None)
e, msg = ms.run_action(3, "提交口径评审"); check("已发布无在途草稿时提交评审被拒", e is None, msg)

# ---- 首版正常链路：草稿->评审->发布，重复提交被拒 ----
e, _ = ms.run_action(1, "提交口径评审"); check("提交评审", e and e["status"] == "口径评审中")
e, msg = ms.run_action(1, "提交口径评审"); check("重复提交评审被拒", e is None, msg)
e, _ = ms.run_action(1, "发布指标"); check("首版发布", e and e["status"] == "已发布")
m1 = store.find("metric", 1)
check("v1 冻结为已发布", m1["versions"][0]["status"] == "已发布" and m1["published_version"] == "v1")

# ---- 复现 DASH-0002：METR-0002 评审中，发布拦截且给出版本原因，METR-0003 引用不受影响 ----
# 该验证必须在修订 METR-0003 之前进行：此时 METR-0003 v1 仍已发布，C-2-2 应保持有效。
e, msg = ds.run_action(2, "发布看板")
check("DASH-0002 停在发布前", e is None and store.find("dashboard", 2)["status"] == "看板评审中", msg)
d2 = ds.get_entry(2)
check("拦截原因到指标版本", any("METR-0002" in b and "已发布" in b for b in d2["publish_blockers"]))
check("METR-0003 引用仍有效", next(c for c in d2["cards"] if c["card_id"] == "C-2-2")["valid"] is True)
e, msg = ds.run_action(2, "发布看板"); check("拦截无半成品 revision", e is None and store.find("dashboard", 2)["revision"] == 1)

# ---- 已发布定义修订新版本：定义保持已发布，新版本走草稿->评审->发布，旧版下线快照保留 ----
e, msg = ms.run_action(3, "新建口径版本"); check("已发布可新建版本", e is not None, msg)
check("新建版本后定义仍已发布", store.find("metric", 3)["status"] == "已发布")
e, msg = ms.run_action(3, "新建口径版本"); check("在途草稿禁止再建版本", e is None and "在途" in msg, msg)
e, _ = ms.run_action(3, "提交口径评审"); check("在途草稿可提交评审(定义不回退)", e is not None and e["status"] == "已发布")
e, _ = ms.run_action(3, "发布指标"); check("在途版本发布", e is not None and e["published_version"] == "v2")
m3 = store.find("metric", 3)
check("旧 v1 下线但快照保留", m3["versions"][0]["status"] == "已下线"
      and m3["versions"][0]["snapshot"]["计算公式"] == "SUM(pay_amt)/COUNT(DISTINCT user_id)")

# ---- 看板状态机：草稿不能直发 ----
e, msg = ds.run_action(1, "发布看板"); check("草稿看板直发被拒", e is None and "单向推进" in msg, msg)

# ---- 粒度不一致 + 非法引用 ----
e, _ = ds.create_entry({"看板编号": "DASH-0030", "看板名称": "粒度", "布局模板": "l",
                        "刷新间隔": 300, "可见范围": "全员可见", "看板负责人": "o"})
gid = max(r["id"] for r in store.rows("dashboard"))
e, _ = ds.add_card(gid, {"metric_id": 3, "metric_version": "v2", "card_grain": "月"})
check("粒度不符可挂载(发布才拦)", e is not None)
e, msg = ds.run_action(gid, "提交看板评审"); check("粒度不一致拦截评审", e is None and "粒度" in msg, msg)
e, msg = ds.add_card(gid, {"metric_id": 99, "metric_version": "v1", "card_grain": "天"})
check("不存在指标禁止挂载", e is None)
e, msg = ds.add_card(gid, {"metric_id": 3, "metric_version": "v9", "card_grain": "天"})
check("不存在版本禁止挂载", e is None and "v9" in msg, msg)

# ---- 发布成功链路（METR-0002 发布；C-2-2 原钉的 METR-0003 v1 已被 v2 取代，先切到现行 v2）+ 重复发布幂等 ----
ms.run_action(2, "发布指标")
rev = store.find("dashboard", 2)["revision"]
e, msg = ds.switch_card_version(2, "C-2-2", {"metric_version": "v2", "expected_revision": rev, "card_grain": "天"})
check("C-2-2 切换到现行发布版本", e is not None, msg)
e, msg = ds.run_action(2, "发布看板"); check("引用就绪后发布成功", e is not None and e["status"] == "已发布", msg)
e, msg = ds.run_action(2, "发布看板"); check("重复发布被拒", e is None and "重复发布" in msg, msg)
check("发布成功与拦截均入审计",
      len([a for a in store.audits(module="dashboard", entry_id=2) if a["action"] == "发布看板"]) >= 2)

# ---- 指标下线：配置保留、卡片失效停刷新、重复读取不累积 ----
ms.run_action(3, "下线指标")
d3 = ds.get_entry(3)
c = d3["cards"][0]
check("下线卡片配置保留", c["metric_version"] in ("v1", "v2"))
check("下线卡片失效停刷新", c["valid"] is False and c["refresh"] is False and "已下线" in c["reason"], c["reason"])
check("重复读取不累积失效标记", len(ds.get_entry(3)["cards"]) == len(d3["cards"]))
e, msg = ms.run_action(3, "提交口径评审"); check("已下线指标禁止回流", e is None, msg)

# ---- 保存前阈值校验 ----
e, msg = ms.create_entry({"指标编号": "BAD", "指标名称": "n", "计算公式": "f", "统计粒度": "天"})
check("指标编号格式越界拒绝", e is None and "METR-" in msg, msg)
e, msg = ms.create_entry({"指标编号": "METR-9001", "指标名称": "n", "计算公式": "f", "统计粒度": "秒"})
check("粒度越界拒绝", e is None and "统计粒度" in msg, msg)
e, msg = ms.create_entry({"指标编号": "METR-9002", "指标名称": "n", "计算公式": "f",
                          "统计粒度": "天", "精度位数": 99})
check("精度越界拒绝", e is None and "精度位数" in msg, msg)
e, msg = ds.create_entry({"看板编号": "DASH-9001", "看板名称": "n", "布局模板": "l",
                          "刷新间隔": 5, "可见范围": "全员可见", "看板负责人": "o"})
check("刷新间隔越界拒绝", e is None and "刷新间隔" in msg, msg)
e, msg = ds.create_entry({"看板编号": "DASH-0002", "看板名称": "n", "布局模板": "l",
                          "刷新间隔": 300, "可见范围": "全员可见", "看板负责人": "o"})
check("重复看板编号拒绝", e is None and "已存在" in msg, msg)

# ---- 全链路追溯 ----
detail = ms.get_entry(3)
check("指标详情->看板卡片", any(r["card_id"] for r in detail["references"]))
check("指标详情->口径版本", len(detail["versions"]) >= 2)
check("指标详情->审计", len(detail["audits"]) > 0)
dd = ds.get_entry(3)
check("看板详情->发布审计", len(dd["audits"]) > 0)
check("卡片->版本可追溯", dd["cards"][0]["metric_version"] in [v["version"] for v in detail["versions"]])

# ---- 列表与详情同口径 ----
lst, _ = ds.list_entries()
r2 = next(r for r in lst if r["看板编号"] == "DASH-0002")
check("列表含失效卡片统计与原因", "失效卡片数" in r2 and isinstance(r2["失效原因"], list))
ml, _ = ms.list_entries(status="已下线")
check("列表状态过滤", {r["指标编号"] for r in ml} == {"METR-0003"})

print()
if failures:
    print(f"{len(failures)} 项失败：{failures}")
    sys.exit(1)
print("全量回归通过")
