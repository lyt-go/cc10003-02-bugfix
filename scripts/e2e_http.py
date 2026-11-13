"""端到端 HTTP 验收：通过真实 FastAPI 端口验证指标口径与看板联动。

运行：python -I e2e_http.py
依赖：仅标准库 urllib，不安装第三方包。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8011"
failures: list[str] = []


def call(method: str, path: str, body: object | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def check(name: str, cond: bool, extra: object = None) -> None:
    print(("PASS" if cond else "FAIL"), name, "" if cond else f"-> {extra}")
    if not cond:
        failures.append(name)


# 1. 异常复现：METR-0002 评审中，DASH-0002 发布必须停在发布前并给出指标版本原因
status, payload = call("GET", "/api/dashboard?keyword=DASH-0002")
dash2 = payload["items"][0]
check("列表读 DASH-0002 为看板评审中", dash2["status"] == "看板评审中", dash2["status"])
check("列表显示 1 张失效卡", dash2["失效卡片数"] == 1, dash2["失效原因"])
check("列表失效原因指向 METR-0002 版本", any("METR-0002" in r for r in dash2["失效原因"]), dash2["失效原因"])

status, detail = call("GET", "/api/dashboard/2")
check("详情 can_publish=False", detail["can_publish"] is False)
check("详情发布拦截原因明确到口径版本",
      any("METR-0002" in b and "口径版本 v1" in b and "已发布" in b for b in detail["publish_blockers"]),
      detail["publish_blockers"])
c22 = next(c for c in detail["cards"] if c["card_id"] == "C-2-2")
check("METR-0003 正常引用不受影响", c22["valid"] is True and c22["refresh"] is True)

status, result = call("POST", "/api/dashboard/2/actions", {"values": {"action": "发布看板"}})
check("发布请求 HTTP 200 且 ok=false（有拒绝原因而非静默）", status == 200 and result["ok"] is False, result)
check("拒绝消息含指标版本原因", "METR-0002" in result["message"] and "已发布" in result["message"], result["message"])
status, after = call("GET", "/api/dashboard/2")
check("看板停在发布前状态", after["status"] == "看板评审中", after["status"])
check("拦截未产生半成品 revision", after["revision"] == 1, after["revision"])

# 2. 指标状态机：草稿直发被拒、已发布回流评审被拒
status, result = call("POST", "/api/metric/1/actions", {"values": {"action": "发布指标"}})
check("草稿直发指标被拒", result["ok"] is False and "跳过评审" in result["message"], result["message"])
status, result = call("POST", "/api/metric/3/actions", {"values": {"action": "提交口径评审"}})
check("已发布重新提交评审被拒", result["ok"] is False and "不是草稿" in result["message"], result["message"])

# 3. 正常推进 METR-0001：评审 -> 发布，发布后历史版本冻结
call("POST", "/api/metric/1/actions", {"values": {"action": "提交口径评审"}})
status, result = call("POST", "/api/metric/1/actions", {"values": {"action": "发布指标"}})
check("METR-0001 发布成功", result["ok"] and result["entry"]["status"] == "已发布", result)
status, m1 = call("GET", "/api/metric/1")
check("发布版本 v1 冻结", any(v["version"] == "v1" and v["status"] == "已发布" for v in m1["versions"]))

# 4. 让 METR-0002 走完评审->发布后，DASH-0002 可发布，且只生效一次
call("POST", "/api/metric/2/actions", {"values": {"action": "发布指标"}})
status, result = call("POST", "/api/dashboard/2/actions", {"values": {"action": "发布看板"}})
check("引用全部发布后看板发布成功", result["ok"] and result["entry"]["status"] == "已发布", result)
status, result2 = call("POST", "/api/dashboard/2/actions", {"values": {"action": "发布看板"}})
check("重复发布被拒绝（只有一次生效）", result2["ok"] is False and "重复发布" in result2["message"], result2["message"])
status, d2 = call("GET", "/api/dashboard/2")
check("发布记录已生成", any(a["action"] == "发布看板" and a["result"] == "成功" for a in d2["audits"]))
check("发布拦截历史也留痕", any(a["action"] == "发布看板" and a["result"] == "拒绝" for a in d2["audits"]))

# 5. 并发切换同一卡片版本：revision 乐观锁，只有一次生效
call("POST", "/api/metric/3/actions", {"values": {"action": "新建口径版本"}})
call("POST", "/api/metric/3/actions", {"values": {"action": "提交口径评审"}})
call("POST", "/api/metric/3/actions", {"values": {"action": "发布指标"}})
_, d3 = call("GET", "/api/dashboard/3")
rev = d3["revision"]
s1 = call("POST", "/api/dashboard/3/cards/C-3-1/switch",
          {"metric_version": "v2", "expected_revision": rev, "card_grain": "天"})
s2 = call("POST", "/api/dashboard/3/cards/C-3-1/switch",
          {"metric_version": "v2", "expected_revision": rev, "card_grain": "天"})
check("首次版本切换生效", s1[1]["ok"] is True, s1[1])
check("并发切换 revision 失配失败", s2[1]["ok"] is False and "并发冲突" in s2[1]["message"], s2[1])
_, d3b = call("GET", "/api/dashboard/3")
check("卡片最终只引用 v2", d3b["cards"][0]["metric_version"] == "v2")

# 6. 指标下线：看板原配置保留，卡片失效停刷新，重复读取不累积
call("POST", "/api/metric/3/actions", {"values": {"action": "下线指标"}})
_, d3c = call("GET", "/api/dashboard/3")
card = d3c["cards"][0]
check("下线后卡片配置保留", card["metric_version"] == "v2")
check("下线卡片失效且停止刷新", card["valid"] is False and card["refresh"] is False and "已下线" in card["reason"], card)
_, d3d = call("GET", "/api/dashboard/3")
check("刷新后失效卡片不重复显示", len(d3d["cards"]) == len(d3c["cards"]) == 1)

# 7. 保存前阈值校验（缺依据/越界拒绝保存）
_, bad1 = call("POST", "/api/metric", {"values": {"指标编号": "X-1", "指标名称": "n", "计算公式": "f", "统计粒度": "天"}})
check("指标编号越界拒绝", bad1["ok"] is False and "METR-" in bad1["message"], bad1["message"])
_, bad2 = call("POST", "/api/dashboard", {"values": {
    "看板编号": "DASH-9001", "看板名称": "n", "布局模板": "l", "刷新间隔": 1,
    "可见范围": "全员可见", "看板负责人": "o"}})
check("刷新间隔越界拒绝", bad2["ok"] is False and "刷新间隔" in bad2["message"], bad2["message"])

# 8. 全链路追溯：指标详情 -> 看板卡片 -> 口径版本 -> 审计
_, m3 = call("GET", "/api/metric/3")
check("指标详情追到看板卡片", any(r["card_id"] == "C-3-1" for r in m3["references"]))
check("指标详情含口径版本", {v["version"] for v in m3["versions"]} >= {"v1", "v2"})
check("指标详情含审计", len(m3["audits"]) > 0)
check("历史 v1 快照未被草稿覆盖",
      next(v for v in m3["versions"] if v["version"] == "v1")["snapshot"]["计算公式"]
      == "SUM(pay_amt)/COUNT(DISTINCT user_id)")

# 9. 审计查询接口
_, audits = call("GET", "/api/audit?module=dashboard&entry_id=2")
check("审计接口可按模块+记录过滤", audits["total"] >= 2 and all(a["module"] == "dashboard" for a in audits["items"]))

print()
if failures:
    print(f"{len(failures)} 项失败：{failures}")
    sys.exit(1)
print("HTTP 端到端验收全部通过")
