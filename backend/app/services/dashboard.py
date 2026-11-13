"""看板配置业务规则与指标口径联动。

修复要点（沿原有「状态写入 run_action / 列表读取 list_entries」两条链路改造）：
1. 看板状态只能按 草稿 → 看板评审中 → 已发布 → 已下线 单向推进一格；
2. 发布前逐卡校验：引用的口径版本必须「已发布」且卡片计算粒度与口径粒度一致，
   任一卡片不通过就整次拒绝并给出指标版本原因，看板停在发布前状态（复现 DASH-0002）；
3. 发布在一把锁 + 快照事务内完成：任一引用写入失败整体回退，重复发布幂等拦截、
   并发切换同一卡片版本用 revision 乐观锁保证只有一次生效；
4. 指标下线后看板原配置保留，卡片运行态显示失效来源并停止刷新；
5. 卡片有效性在读取链路即时计算，不落库、不累积，刷新页面不会重复出现失效标记。
"""
from __future__ import annotations

from typing import Any

from app.services import governance as gov
from app.store import store

MODULE = gov.DASHBOARD_MODULE
STATUS_ORDER = gov.DASHBOARD_STATUS_ORDER
ACTION_RULES = {
    "提交看板评审": ("草稿", "看板评审中"),
    "发布看板": ("看板评审中", "已发布"),
    "下线看板": ("已发布", "已下线"),
}


class DashboardService:
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
            rows = [row for row in rows if keyword in str(row.get("看板编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        items = [self._list_view(row) for row in rows[start:start + size]]
        return items, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return self._detail_view(entry)

    def _card_views(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        # 每次读取即时解析，保证列表与详情同口径，且失效标记不会被持久化重复累积。
        return [gov.resolve_card(card) for card in entry.get("cards", [])]

    def _list_view(self, entry: dict[str, Any]) -> dict[str, Any]:
        cards = self._card_views(entry)
        invalid = [card for card in cards if not card.get("valid")]
        return {
            "id": entry["id"],
            "status": entry["status"],
            "看板编号": entry.get("看板编号"),
            "看板名称": entry.get("看板名称"),
            "包含指标": "、".join(str(card.get("metric_code")) for card in cards) or "—",
            "布局模板": entry.get("布局模板"),
            "刷新间隔": entry.get("刷新间隔"),
            "可见范围": entry.get("可见范围"),
            "所属业务线": entry.get("所属业务线"),
            "看板负责人": entry.get("看板负责人"),
            "revision": entry.get("revision", 1),
            "published_at": entry.get("published_at"),
            "卡片总数": len(cards),
            "失效卡片数": len(invalid),
            "失效原因": [f"{card.get('card_id')}：{card.get('reason')}" for card in invalid],
        }

    def _detail_view(self, entry: dict[str, Any]) -> dict[str, Any]:
        cards = self._card_views(entry)
        invalid = [card for card in cards if not card.get("valid")]
        publish_check = self._pre_publish_check(entry)
        return {
            "id": entry["id"],
            "status": entry["status"],
            "看板编号": entry.get("看板编号"),
            "看板名称": entry.get("看板名称"),
            "布局模板": entry.get("布局模板"),
            "刷新间隔": entry.get("刷新间隔"),
            "可见范围": entry.get("可见范围"),
            "所属业务线": entry.get("所属业务线"),
            "看板负责人": entry.get("看板负责人"),
            "revision": entry.get("revision", 1),
            "published_at": entry.get("published_at"),
            "cards": cards,
            "卡片总数": len(cards),
            "失效卡片数": len(invalid),
            "publish_blockers": publish_check,
            "can_publish": entry.get("status") == "看板评审中" and not publish_check,
            "audits": store.audits(module=MODULE, entry_id=int(entry["id"])),
            "allowed_actions": self._allowed_actions(entry),
        }

    # ------------------------------------------------------------------ 写入
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        try:
            cleaned = gov.validate_dashboard_values(values)
        except gov.ValidationError as exc:
            return None, f"看板保存被拒绝：{exc}"
        with store.lock:
            code = cleaned["看板编号"]
            if any(row.get("看板编号") == code for row in store.rows(MODULE)):
                return None, f"看板编号 {code} 已存在，重复编号不允许保存"
            entry: dict[str, Any] = {
                "id": store.next_id(MODULE),
                "status": STATUS_ORDER[0],
                **cleaned,
                "revision": 1,
                "published_at": None,
                "cards": [],
            }
            store.rows(MODULE).append(entry)
            gov.audit(
                module=MODULE, entry_id=entry["id"], action="登记看板页面", ok=True,
                detail=f"看板 {code} 登记为草稿，尚未挂载指标卡",
            )
            return self._detail_view(entry), ""

    def add_card(self, entry_id: int, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """挂载指标卡：保存前就校验引用存在、版本存在、粒度在允许范围内。"""
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"看板页面 {entry_id} 不存在或已归档"
            cards = entry.setdefault("cards", [])
            if len(cards) >= gov.MAX_CARDS_PER_DASHBOARD:
                return None, f"单看板指标卡上限 {gov.MAX_CARDS_PER_DASHBOARD} 张，越界不允许保存"
            try:
                metric_id = int(payload.get("metric_id"))
            except (TypeError, ValueError):
                return None, "metric_id 必须是整数，缺少合法依据不允许保存"
            version = str(payload.get("metric_version") or "").strip()
            grain = str(payload.get("card_grain") or "").strip()
            if grain and grain not in gov.ALLOWED_GRAINS:
                return None, f"卡片计算粒度仅允许 {'/'.join(gov.ALLOWED_GRAINS)}，收到「{grain}」越界"
            metric = gov.find_metric(metric_id)
            if metric is None:
                return None, f"指标定义 {metric_id} 不存在，引用缺少依据不允许保存"
            if gov.find_version(metric, version) is None:
                return None, (
                    f"指标 {metric.get('指标编号')} 不存在口径版本 {version}，"
                    "引用缺少依据不允许保存"
                )
            card_id = str(payload.get("card_id") or f"C-{entry_id}-{len(cards) + 1}").strip()
            if any(card.get("card_id") == card_id for card in cards):
                return None, f"卡片标识 {card_id} 已存在，重复挂载不允许保存"
            cards.append({
                "card_id": card_id,
                "metric_id": metric_id,
                "metric_code": metric.get("指标编号"),
                "metric_version": version,
                "card_grain": grain,
                "refresh_enabled": True,
            })
            entry["revision"] = int(entry.get("revision", 1)) + 1
            gov.audit(
                module=MODULE, entry_id=entry_id, action="挂载指标卡", ok=True,
                detail=f"卡片 {card_id} 引用 {metric.get('指标编号')} 口径版本 {version}，粒度 {grain}",
                extra={"card_id": card_id, "metric_id": metric_id, "metric_version": version},
            )
            return self._detail_view(entry), "指标卡已挂载，引用版本已固定"

    def switch_card_version(
        self,
        entry_id: int,
        card_id: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        """切换卡片引用的口径版本。

        并发控制：调用方带 expected_revision，锁内二次比对；两处同时切换同一卡片时，
        先提交者推进 revision，后提交者预期版本号失配而失败——只有一次生效。
        """
        new_version = str(payload.get("metric_version") or "").strip()
        new_grain = str(payload.get("card_grain") or "").strip() or None
        try:
            expected_revision = int(payload["expected_revision"])
        except (KeyError, TypeError, ValueError):
            return None, "切换版本必须携带整数 expected_revision，缺少并发依据不允许保存"

        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"看板页面 {entry_id} 不存在或已归档"
            current_revision = int(entry.get("revision", 1))
            if expected_revision != current_revision:
                message = (
                    f"并发冲突：卡片 {card_id} 已被其他操作更新（revision {current_revision}），"
                    f"本次依据的 revision {expected_revision} 已过期，切换未生效"
                )
                gov.audit(module=MODULE, entry_id=entry_id, action="切换指标版本", ok=False, detail=message,
                          extra={"card_id": card_id})
                return None, message

            card = next((item for item in entry.get("cards", []) if item.get("card_id") == card_id), None)
            if card is None:
                return None, f"卡片 {card_id} 不存在，切换缺少依据"
            metric = gov.find_metric(int(card["metric_id"]))
            if metric is None:
                return None, f"指标定义 {card['metric_id']} 不存在，切换缺少依据"
            target = gov.find_version(metric, new_version)
            if target is None:
                return None, f"口径版本 {new_version} 不存在，切换缺少依据不允许保存"
            grain = new_grain or str(card.get("card_grain"))
            if grain not in gov.ALLOWED_GRAINS:
                return None, f"卡片计算粒度仅允许 {'/'.join(gov.ALLOWED_GRAINS)}，收到「{grain}」越界"
            if card.get("metric_version") == new_version and str(card.get("card_grain")) == grain:
                return None, f"卡片 {card_id} 已引用版本 {new_version} 且粒度相同，重复切换不生效"

            snapshot = store.snapshot()
            old_version = card.get("metric_version")
            try:
                card["metric_version"] = new_version
                card["card_grain"] = grain
                entry["revision"] = current_revision + 1
            except Exception:  # pragma: no cover - 防御性回退
                store.restore(snapshot)
                raise
            gov.audit(
                module=MODULE, entry_id=entry_id, action="切换指标版本", ok=True,
                detail=f"卡片 {card_id} 引用由 {old_version} 切换为 {new_version}，粒度 {grain}",
                extra={"card_id": card_id, "from_version": old_version, "to_version": new_version},
            )
            return self._detail_view(entry), f"卡片 {card_id} 已切换到口径版本 {new_version}"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于看板配置可执行范围"
        required_status, target_status = ACTION_RULES[action]
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"看板页面 {entry_id} 不存在或已归档"
            current_status = str(entry.get("status"))

            # 重复发布显式拦截：已发布看板再次发布只允许一次生效，给出明确原因而非笼统状态报错。
            if action == "发布看板" and current_status == "已发布":
                message = "发布被拒绝：看板已处于已发布状态，重复发布不会二次生效"
                gov.audit(module=MODULE, entry_id=entry_id, action=action, ok=False, detail=message)
                return None, message

            if current_status != required_status:
                message = (
                    f"状态流转被拒绝：动作「{action}」要求当前状态为「{required_status}」，"
                    f"实际为「{current_status}」，状态只能按 {' → '.join(STATUS_ORDER)} 单向推进"
                )
                gov.audit(module=MODULE, entry_id=entry_id, action=action, ok=False, detail=message)
                return None, message

            if action == "提交看板评审":
                blockers = self._pre_publish_check(entry)
                if blockers:
                    message = "提交看板评审被拒绝：" + "；".join(blockers)
                    gov.audit(module=MODULE, entry_id=entry_id, action=action, ok=False, detail=message)
                    return None, message
                entry["status"] = target_status
                gov.audit(
                    module=MODULE, entry_id=entry_id, action=action, ok=True,
                    detail="引用校验通过，看板进入看板评审中",
                )
                return self._detail_view(entry), "看板已提交评审"

            if action == "发布看板":
                return self._publish(entry)

            return self._offline(entry)

    # ------------------------------------------------------------------ 发布
    def _pre_publish_check(self, entry: dict[str, Any]) -> list[str]:
        """发布前逐卡硬校验：口径版本已发布 + 计算粒度一致。返回全部拒绝原因（明确到卡片与版本）。"""
        blockers: list[str] = []
        cards = entry.get("cards", [])
        if not cards:
            blockers.append("看板未挂载任何指标卡，缺少发布依据")
        for card in cards:
            label = str(card.get("card_id"))
            metric = gov.find_metric(int(card["metric_id"]))
            if metric is None:
                blockers.append(f"{label}：指标定义 {card.get('metric_id')} 不存在，来源失效")
                continue
            version = gov.find_version(metric, str(card.get("metric_version")))
            if version is None:
                blockers.append(
                    f"{label}：{metric.get('指标编号')} 的口径版本 {card.get('metric_version')} 不存在"
                )
                continue
            if metric.get("status") == gov.OFFLINE_ENTRY_STATUS:
                blockers.append(
                    f"{label}：{metric.get('指标编号')} 已下线，口径版本 {version.get('version')} 来源失效"
                )
                continue
            if version.get("status") != gov.PUBLISHED_VERSION_STATUS:
                blockers.append(
                    f"{label}：{metric.get('指标编号')} 口径版本 {version.get('version')} "
                    f"当前为「{version.get('status')}」，必须为「已发布」才允许看板发布"
                )
            snapshot = version.get("snapshot", {})
            if snapshot.get("统计粒度") != card.get("card_grain"):
                blockers.append(
                    f"{label}：{metric.get('指标编号')} 口径版本 {version.get('version')} 的计算粒度为"
                    f"「{snapshot.get('统计粒度')}」，与卡片粒度「{card.get('card_grain')}」不一致"
                )
        return blockers

    def _publish(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str]:
        # 重复发布幂等拦截：已发布看板再次请求直接拒绝，保证只有一次生效。
        if entry.get("status") == "已发布":
            message = "发布被拒绝：看板已处于已发布状态，重复发布不会二次生效"
            gov.audit(module=MODULE, entry_id=int(entry["id"]), action="发布看板", ok=False, detail=message)
            return None, message  # type: ignore[return-value]

        blockers = self._pre_publish_check(entry)
        if blockers:
            message = "看板发布被拒绝，看板停在发布前状态：" + "；".join(blockers)
            gov.audit(
                module=MODULE, entry_id=int(entry["id"]), action="发布看板", ok=False,
                detail=message, extra={"blockers": blockers},
            )
            # 不修改任何状态：DASH-0002 应继续停在「看板评审中」。
            return None, message  # type: ignore[return-value]

        snapshot = store.snapshot()
        try:
            # 整个发布是一个事务：卡片引用写入与看板状态写入同生共死，
            # 任一引用写入失败都用快照整体回退，绝不留半成品。
            for card in entry.get("cards", []):
                card["refresh_enabled"] = True
            entry["status"] = "已发布"
            entry["published_at"] = gov.now_iso()
            entry["revision"] = int(entry.get("revision", 1)) + 1
            gov.audit(
                module=MODULE, entry_id=int(entry["id"]), action="发布看板", ok=True,
                detail=(
                    f"全部 {len(entry.get('cards', []))} 张指标卡引用口径均已发布且粒度一致，"
                    "看板发布成功并生成发布记录"
                ),
                extra={
                    "published_at": entry["published_at"],
                    "cards": [
                        {"card_id": c.get("card_id"), "metric_version": c.get("metric_version")}
                        for c in entry.get("cards", [])
                    ],
                },
            )
        except Exception:
            store.restore(snapshot)
            gov.audit(
                module=MODULE, entry_id=int(entry["id"]), action="发布看板", ok=False,
                detail="引用写入过程中失败，已回退整次看板发布",
            )
            return None, "看板发布失败，已回退全部变更"  # type: ignore[return-value]

        return self._detail_view(entry), "看板发布成功，发布记录已生成"

    def _offline(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str]:
        entry["status"] = "已下线"
        entry["revision"] = int(entry.get("revision", 1)) + 1
        # 原配置（cards、布局、刷新间隔）原样保留，仅状态变化。
        gov.audit(
            module=MODULE, entry_id=int(entry["id"]), action="下线看板", ok=True,
            detail="看板已下线，卡片配置原样保留；下线后不再触发刷新",
        )
        return self._detail_view(entry), "看板已下线，原配置保留"

    def _allowed_actions(self, entry: dict[str, Any]) -> list[str]:
        status = entry.get("status")
        return {
            "草稿": ["提交看板评审"],
            "看板评审中": ["发布看板"],
            "已发布": ["下线看板"],
            "已下线": [],
        }.get(status, [])
