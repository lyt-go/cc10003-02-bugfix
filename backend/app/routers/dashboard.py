"""看板配置接口：维护看板页面、指标卡引用与发布联动。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, CardPayload, CardSwitchPayload, EntryPayload, PageResult
from app.services.dashboard import DashboardService

router = APIRouter(prefix="/api/dashboard", tags=["看板配置"])

service = DashboardService()

LIST_FIELDS = ["看板编号", "看板名称", "包含指标", "布局模板", "刷新间隔", "可见范围", "所属业务线", "看板负责人"]
STATUSES = ["草稿", "看板评审中", "已发布", "已下线"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按看板编号检索"),
    status: str | None = Query(default=None, description="草稿、看板评审中、已发布、已下线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按看板编号与状态过滤看板配置列表；列表与详情共用卡片解析逻辑，口径一致。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """看板详情：含每张指标卡的实时有效性、发布拦截原因与审计（发布记录）。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"看板页面 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/audit", response_model=dict)
def get_entry_audit(entry_id: int) -> dict[str, Any]:
    """单条看板的审计记录，发布成功/拦截都在此留痕。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"看板页面 {entry_id} 不存在或已归档")
    from app.store import store

    items = store.audits(module="dashboard", entry_id=entry_id)
    return {"items": items, "total": len(items)}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条看板页面，保存前按明确阈值校验，缺字段或越界时说明原因。"""
    entry, message = service.create_entry(payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message="看板页面已登记", entry=entry)


@router.post("/{entry_id}/cards", response_model=ActionResult)
def add_card(entry_id: int, payload: CardPayload) -> ActionResult:
    """挂载指标卡：引用必须指向存在的指标与口径版本，粒度取允许范围内的值。"""
    entry, message = service.add_card(entry_id, payload.model_dump())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/cards/{card_id}/switch", response_model=ActionResult)
def switch_card_version(entry_id: int, card_id: str, payload: CardSwitchPayload) -> ActionResult:
    """切换卡片口径版本；expected_revision 失配说明发生并发修改，本次不生效。"""
    entry, message = service.switch_card_version(entry_id, card_id, payload.model_dump())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """状态单向推进：提交看板评审、发布看板、下线看板；发布前逐卡校验引用版本与粒度。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出看板配置清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dashboard", "total": total, "items": items}
