"""指标口径接口：维护指标定义与口径版本，覆盖草稿编辑、提交评审、发布、下线与新版本修订。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.metric import MetricService

router = APIRouter(prefix="/api/metric", tags=["指标口径"])

service = MetricService()

LIST_FIELDS = ["指标编号", "指标名称", "计算公式", "统计粒度", "时间窗口", "筛选条件", "精度位数", "口径版本"]
STATUSES = ["草稿", "口径评审中", "已发布", "已下线"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按指标编号检索"),
    status: str | None = Query(default=None, description="草稿、口径评审中、已发布、已下线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按指标编号与状态过滤指标口径列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """指标定义详情：含全部口径版本、被哪些看板卡片引用、审计记录，供联动追溯。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"指标定义 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/audit", response_model=dict)
def get_entry_audit(entry_id: int) -> dict[str, Any]:
    """单条指标定义的审计记录。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"指标定义 {entry_id} 不存在或已归档")
    from app.store import store

    items = store.audits(module="metric", entry_id=entry_id)
    return {"items": items, "total": len(items)}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条指标定义，保存前按明确阈值校验，缺字段或越界时说明原因。"""
    entry, message = service.create_entry(payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message="指标定义已登记", entry=entry)


@router.put("/{entry_id}/draft", response_model=ActionResult)
def update_draft(entry_id: int, payload: EntryPayload) -> ActionResult:
    """编辑草稿/评审中口径字段；已发布版本快照不可变。"""
    entry, message = service.update_draft(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message="口径草稿已保存", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """状态单向推进：提交口径评审、发布指标、下线指标、新建口径版本；违规动作被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出指标口径清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "metric", "total": total, "items": items}
