"""指标口径业务规则。

修复要点（沿原有「状态写入 run_action / 列表读取 list_entries」两条链路改造）：
1. 指标定义状态只能按 草稿 → 口径评审中 → 已发布 → 已下线 单向推进一格，
   禁止草稿直发、禁止已发布重新提交评审，拒绝时给出明确原因；
2. 口径按版本建模：发布动作只冻结当前版本快照，历史版本不可变，
   看板卡片钉在具体版本上，最新草稿不会覆盖历史引用；
3. 下线指标会在审计与引用视图里体现，看板保留原配置，卡片显示失效来源并停止刷新；
4. 所有写入与拒绝都写审计记录，支持从指标详情追到看板卡片、口径版本与审计。
"""
from __future__ import annotations

from typing import Any

from app.services import governance as gov
from app.store import store

MODULE = gov.METRIC_MODULE
REQUIRED_FIELDS = gov.METRIC_REQUIRED_FIELDS
STATUS_ORDER = gov.METRIC_STATUS_ORDER

# 指标定义四态上允许执行的动作：只能推进到序列中的下一格。
ACTION_RULES = {
    "提交口径评审": ("草稿", "口径评审中"),
    "发布指标": ("口径评审中", "已发布"),
    "下线指标": ("已发布", "已下线"),
}
# 已发布指标要修订口径时走「新建口径版本」，而不是把定义打回评审。
VERSION_ACTIONS = {"新建口径版本", "提交口径评审", "发布指标", "下线指标"}


class MetricService:
    # ------------------------------------------------------------------ 读取
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("指标编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        # 列表与详情走同一份投影逻辑，保证两边数据对得上。
        items = [self._list_view(row) for row in rows[start:start + size]]
        return items, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return self._detail_view(entry)

    def _list_view(self, entry: dict[str, Any]) -> dict[str, Any]:
        """列表行：展示当前生效（已发布）版本与正在编辑的草稿版本，二者不再混成一个文本字段。"""
        published = self._version(entry, entry.get("published_version")) if entry.get("published_version") else None
        current = self._version(entry, entry.get("current_version"))
        return {
            "id": entry["id"],
            "status": entry["status"],
            "指标编号": entry.get("指标编号"),
            "指标名称": entry.get("指标名称"),
            "计算公式": entry.get("计算公式"),
            "统计粒度": entry.get("统计粒度"),
            "时间窗口": entry.get("时间窗口"),
            "筛选条件": entry.get("筛选条件"),
            "精度位数": entry.get("精度位数"),
            "current_version": entry.get("current_version"),
            "current_version_status": current.get("status") if current else None,
            "published_version": entry.get("published_version"),
            "published_at": published.get("published_at") if published else None,
            "引用看板数": len(gov.cards_referencing_metric(int(entry["id"]))),
        }

    def _detail_view(self, entry: dict[str, Any]) -> dict[str, Any]:
        versions = [dict(item) for item in entry.get("versions", [])]
        references = gov.cards_referencing_metric(int(entry["id"]))
        # 给每个引用补上实时有效性，详情页可直接看到失效卡片与原因。
        for ref in references:
            dashboard = store.find(gov.DASHBOARD_MODULE, int(ref["dashboard_id"]))
            if dashboard is not None:
                for card in dashboard.get("cards", []):
                    if card.get("card_id") == ref["card_id"]:
                        ref["card_view"] = gov.resolve_card(card)
        return {
            "id": entry["id"],
            "status": entry["status"],
            "指标编号": entry.get("指标编号"),
            "指标名称": entry.get("指标名称"),
            "计算公式": entry.get("计算公式"),
            "统计粒度": entry.get("统计粒度"),
            "时间窗口": entry.get("时间窗口"),
            "筛选条件": entry.get("筛选条件"),
            "精度位数": entry.get("精度位数"),
            "current_version": entry.get("current_version"),
            "published_version": entry.get("published_version"),
            "versions": versions,
            "references": references,
            "audits": store.audits(module=MODULE, entry_id=int(entry["id"])),
            "allowed_actions": self._allowed_actions(entry),
        }

    # ------------------------------------------------------------------ 写入
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        try:
            cleaned = gov.validate_metric_values(values)
        except gov.ValidationError as exc:
            return None, f"指标定义保存被拒绝：{exc}"
        with store.lock:
            code = cleaned["指标编号"]
            if any(row.get("指标编号") == code for row in store.rows(MODULE)):
                return None, f"指标编号 {code} 已存在，重复编号不允许保存"
            entry: dict[str, Any] = {
                "id": store.next_id(MODULE),
                "status": STATUS_ORDER[0],
                **cleaned,
                "current_version": "v1",
                "published_version": None,
                "versions": [{
                    "version": "v1",
                    "status": "草稿",
                    "snapshot": self._snapshot(cleaned),
                    "published_at": None,
                }],
            }
            store.rows(MODULE).append(entry)
            gov.audit(
                module=MODULE, entry_id=entry["id"], action="登记指标定义", ok=True,
                detail=f"指标 {code} 登记为草稿，初始口径版本 v1",
            )
            return self._detail_view(entry), ""

    def update_draft(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """只允许修改草稿/评审中版本的口径字段，已发布版本快照不可变。"""
        try:
            cleaned = gov.validate_metric_values(values)
        except gov.ValidationError as exc:
            return None, f"指标草稿保存被拒绝：{exc}"
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"指标定义 {entry_id} 不存在或已归档"
            version = self._version(entry, str(entry.get("current_version")))
            if version is None or version.get("status") not in ("草稿", "口径评审中"):
                return None, "当前口径版本不在可编辑状态，已发布/已下线版本不可修改"
            snapshot = self._snapshot(cleaned)
            version["snapshot"] = snapshot
            for field in ("指标名称", "计算公式", "统计粒度", "时间窗口", "筛选条件", "精度位数"):
                entry[field] = cleaned[field]
            gov.audit(
                module=MODULE, entry_id=entry_id, action="编辑口径草稿", ok=True,
                detail=f"版本 {version['version']} 草稿字段已更新，发布快照不受影响",
                extra={"version": version["version"]},
            )
            return self._detail_view(entry), ""

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action not in VERSION_ACTIONS:
            return None, f"动作「{action}」不属于指标口径可执行范围"
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"指标定义 {entry_id} 不存在或已归档"

            if action == "新建口径版本":
                return self._create_new_version(entry)

            if action == "下线指标":
                return self._offline(entry)

            # 定义级四态单向推进；已发布定义上修订的新版本走版本链路，定义保持「已发布」不回退。
            current_version = self._version(entry, str(entry.get("current_version")))
            definition_status = str(entry.get("status"))

            if action == "提交口径评审":
                # 允许：首版（定义为草稿）或已发布定义上的在途草稿版本；已下线定义禁止任何流转。
                if definition_status == "已下线":
                    return None, self._reject(entry_id, action, "已下线指标不允许重新提交评审，状态机不允许回退")
                if current_version is None or current_version.get("status") != "草稿":
                    return None, self._reject(
                        entry_id, action,
                        f"当前版本 {entry.get('current_version')} 不是草稿（实际为"
                        f"「{current_version and current_version.get('status')}」），禁止提交评审或重复提交",
                    )
                return self._submit_review(entry, ACTION_RULES[action][1])

            if action == "发布指标":
                if definition_status == "已下线":
                    return None, self._reject(entry_id, action, "已下线指标不允许发布，状态机不允许回退")
                if current_version is None or current_version.get("status") != "口径评审中":
                    return None, self._reject(
                        entry_id, action,
                        f"当前版本 {entry.get('current_version')} 不在口径评审中（实际为"
                        f"「{current_version and current_version.get('status')}」），不允许跳过评审直接发布",
                    )
                return self._publish(entry, ACTION_RULES[action][1])

            return None, self._reject(entry_id, action, f"动作「{action}」不属于指标口径可执行范围")

    def _reject(self, entry_id: int, action: str, message: str) -> str:
        gov.audit(module=MODULE, entry_id=entry_id, action=action, ok=False, detail=message)
        return message

    # ------------------------------------------------------------------ 内部
    def _create_new_version(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str]:
        if entry.get("status") != "已发布":
            message = (
                "新建口径版本被拒绝：只有「已发布」指标允许基于现行口径修订新版本，"
                f"当前状态「{entry.get('status')}」不在允许范围"
            )
            gov.audit(module=MODULE, entry_id=int(entry["id"]), action="新建口径版本", ok=False, detail=message)
            return None, message  # type: ignore[return-value]
        current = self._version(entry, str(entry.get("current_version")))
        # 同一指标只允许一个在途草稿/评审版本：避免两个未发布版本并发抢发布口径。
        if current is not None and current.get("status") in ("草稿", "口径评审中"):
            message = (
                f"新建口径版本被拒绝：版本 {current.get('version')} 仍处于「{current.get('status')}」，"
                "必须先发布或终止该在途版本后才能新建下一版"
            )
            gov.audit(module=MODULE, entry_id=int(entry["id"]), action="新建口径版本", ok=False, detail=message)
            return None, message  # type: ignore[return-value]
        label = gov.next_version_label(entry)
        base = self._version(entry, str(entry.get("published_version")))
        snapshot = dict(base["snapshot"]) if base else self._snapshot(entry)
        entry["versions"].append({
            "version": label,
            "status": "草稿",
            "snapshot": snapshot,
            "published_at": None,
        })
        entry["current_version"] = label
        # 定义级状态保持「已发布」：线上仍由已发布版本支撑，新版本走自己的草稿→评审→发布链路。
        gov.audit(
            module=MODULE, entry_id=int(entry["id"]), action="新建口径版本", ok=True,
            detail=f"基于已发布版本 {base and base.get('version')} 新建草稿 {label}，历史引用保持不变",
            extra={"version": label},
        )
        return self._detail_view(entry), f"已新建口径草稿 {label}，历史看板引用不受影响"

    def _submit_review(self, entry: dict[str, Any], target_status: str) -> tuple[dict[str, Any], str]:
        version = self._version(entry, str(entry.get("current_version")))
        if version is None or version.get("status") != "草稿":
            message = f"提交评审被拒绝：当前版本 {entry.get('current_version')} 不是草稿，禁止重复提交"
            gov.audit(module=MODULE, entry_id=int(entry["id"]), action="提交口径评审", ok=False, detail=message)
            return None, message  # type: ignore[return-value]
        version["status"] = "口径评审中"
        # 首版评审推进定义状态；已发布定义上修订的新版本走版本链路，定义状态保持「已发布」。
        if entry.get("status") == "草稿":
            entry["status"] = target_status
            state_detail = f"定义状态推进至「{target_status}」"
        else:
            state_detail = "定义保持「已发布」，线上仍由现行发布版本支撑"
        gov.audit(
            module=MODULE, entry_id=int(entry["id"]), action="提交口径评审", ok=True,
            detail=f"版本 {version['version']} 进入口径评审，{state_detail}",
            extra={"version": version["version"]},
        )
        return self._detail_view(entry), "指标定义已提交口径评审"

    def _publish(self, entry: dict[str, Any], target_status: str) -> tuple[dict[str, Any], str]:
        version = self._version(entry, str(entry.get("current_version")))
        if version is None or version.get("status") != "口径评审中":
            message = (
                f"发布被拒绝：当前版本 {entry.get('current_version')} 状态为"
                f"「{version and version.get('status')}」，只有评审中版本允许发布"
            )
            gov.audit(module=MODULE, entry_id=int(entry["id"]), action="发布指标", ok=False, detail=message)
            return None, message  # type: ignore[return-value]

        previous_label = entry.get("published_version")
        previous = self._version(entry, str(previous_label)) if previous_label else None
        # 冻结新版本；旧发布版本下线但快照保留，历史卡片仍能读到当时口径。
        version["status"] = "已发布"
        version["published_at"] = gov.now_iso()
        if previous is not None and previous["version"] != version["version"]:
            previous["status"] = "已下线"
        entry["published_version"] = version["version"]
        # 首版发布把定义从评审中推进到已发布；修订版发布时定义本就处于已发布，状态不回退。
        if entry.get("status") != "已发布":
            entry["status"] = target_status
        gov.audit(
            module=MODULE, entry_id=int(entry["id"]), action="发布指标", ok=True,
            detail=(
                f"版本 {version['version']} 已发布并冻结为不可变快照；"
                + (f"原发布版本 {previous['version']} 转为已下线，历史引用保留" if previous else "为首版发布")
            ),
            extra={"version": version["version"], "previous_version": previous_label},
        )
        return self._detail_view(entry), f"指标口径版本 {version['version']} 已发布"

    def _offline(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str]:
        if entry.get("status") != "已发布":
            message = (
                f"下线被拒绝：只有「已发布」指标允许下线，当前状态「{entry.get('status')}」"
                "不满足前置条件，状态机不允许跳跃"
            )
            gov.audit(module=MODULE, entry_id=int(entry["id"]), action="下线指标", ok=False, detail=message)
            return None, message  # type: ignore[return-value]
        entry["status"] = "已下线"
        # 整指标下线后，任何版本都不再处于工作态：在途草稿/评审一并终止，已发布版本转下线。
        for version in entry.get("versions", []):
            if version.get("status") in ("草稿", "口径评审中", "已发布"):
                version["status"] = "已下线"
        refs = gov.cards_referencing_metric(int(entry["id"]))
        gov.audit(
            module=MODULE, entry_id=int(entry["id"]), action="下线指标", ok=True,
            detail=(
                "指标已下线，看板保留原配置，引用卡片显示失效来源并停止刷新；"
                f"受影响卡片 {len(refs)} 张"
            ),
            extra={"affected_cards": [ref["card_id"] for ref in refs]},
        )
        return self._detail_view(entry), "指标已下线，关联看板卡片已标记失效并停止刷新"

    def _allowed_actions(self, entry: dict[str, Any]) -> list[str]:
        allowed: list[str] = []
        status = entry.get("status")
        current = self._version(entry, str(entry.get("current_version")))
        current_vstatus = current.get("status") if current else None
        if status == "草稿":
            allowed.append("提交口径评审")
        elif status == "口径评审中":
            allowed.append("发布指标")
        elif status == "已发布":
            # 已发布定义上若有在途新版本，按版本状态给出对应动作；否则允许修订或下线。
            if current_vstatus == "草稿":
                allowed.append("提交口径评审")
            elif current_vstatus == "口径评审中":
                allowed.append("发布指标")
            else:
                allowed.append("新建口径版本")
            allowed.append("下线指标")
        return allowed

    @staticmethod
    def _version(entry: dict[str, Any], label: str | None) -> dict[str, Any] | None:
        if not label:
            return None
        return gov.find_version(entry, label)

    @staticmethod
    def _snapshot(values: dict[str, Any]) -> dict[str, Any]:
        return {
            "指标名称": values.get("指标名称"),
            "计算公式": values.get("计算公式"),
            "统计粒度": values.get("统计粒度"),
            "时间窗口": values.get("时间窗口"),
            "筛选条件": values.get("筛选条件"),
            "精度位数": values.get("精度位数"),
        }
