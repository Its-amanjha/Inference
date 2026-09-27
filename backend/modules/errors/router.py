from typing import Literal
from fastapi import APIRouter, Query, Request
from backend import audit
from backend.auth_gate import fail, require_current_role, require_role

router=APIRouter()

# Records written before errors were grouped count as a group of their own until retention regroups them.
GROUP="COALESCE(group_key,'row:'||id)"


@router.get('')
@router.get('/')
async def errors(request: Request,instance_id: str | None=Query(default=None,max_length=128),workflow_id: str | None=Query(default=None,max_length=128),
                 status: Literal['open','resolved']='open',limit: int=Query(default=50,ge=1,le=100),before: int | None=Query(default=None,ge=1)):
    """One row per problem: the same workflow, step, type and message are one error. ``count`` is how many
    times it happened in the retention period, ``occurred_at`` the last time and ``first_seen`` the first;
    ``id`` and ``execution_id`` are the latest occurrence's. A problem is open while any occurrence is open,
    and ``returned`` says it came back after it was resolved. The cursor is the group's newest record."""
    require_role(request,'viewer')
    conditions,params=['1=1'],[]
    if instance_id:
        conditions.append('instance_id=?')
        params.append(instance_id)
    if workflow_id:
        conditions.append('workflow_id=?')
        params.append(workflow_id)
    outer=['rn=1','open_rows>0' if status=='open' else 'open_rows=0']
    if before:
        outer.append('cursor<?')
        params.append(before)
    params.append(limit+1)
    rows=await request.app.state.db.fetchall(
        f"WITH scoped AS (SELECT *,{GROUP} AS g FROM errors WHERE {' AND '.join(conditions)}), "
        "ranked AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY g ORDER BY occurred_at DESC,id DESC) AS rn,"
        "COUNT(*) OVER (PARTITION BY g) AS occurrences,SUM(status='open') OVER (PARTITION BY g) AS open_rows,"
        "MIN(occurred_at) OVER (PARTITION BY g) AS first_seen,MAX(id) OVER (PARTITION BY g) AS cursor,"
        "MAX(CASE WHEN status='resolved' THEN resolved_at END) OVER (PARTITION BY g) AS last_resolved FROM scoped) "
        "SELECT id,instance_id,workflow_id,workflow_name,execution_id,node_name,error_type,error_message,occurrences AS count,"
        "occurred_at,first_seen,CASE WHEN open_rows>0 THEN 'open' ELSE 'resolved' END AS status,"
        "CASE WHEN open_rows>0 THEN NULL ELSE last_resolved END AS resolved_at,"
        "open_rows>0 AND last_resolved IS NOT NULL AS returned,cursor "
        f"FROM ranked WHERE {' AND '.join(outer)} ORDER BY cursor DESC LIMIT ?",params)
    more=len(rows)>limit
    rows=rows[:limit]
    next_cursor=str(rows[-1]['cursor']) if more else None
    for row in rows:
        row['id']=str(row['id'])
        row['returned']=bool(row['returned'])
        del row['cursor']
    return {'data':rows,'next_cursor':next_cursor}


async def _group(tx,identifier):
    row=await tx.fetchone(f'SELECT {GROUP} AS g FROM errors WHERE id=?',(identifier,))
    if not row:
        fail(404,'error_not_found','Error was not found.')
    return row['g']


@router.post('/{identifier}/resolve')
async def resolve(identifier: int,request: Request):
    """Resolve the problem: every open occurrence of its group. A new occurrence opens it again."""
    require_role(request,'operator')
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request,'operator',tx)
        group=await _group(tx,identifier)
        await tx.execute(f"UPDATE errors SET status='resolved',resolved_at=datetime('now') WHERE status='open' AND {GROUP}=?",(group,))
    await audit.record(request,'errors.resolved',str(identifier))
    return {'id':str(identifier),'status':'resolved'}


# Undo for a resolve: the occurrences the latest resolve closed return to the open queue.
@router.post('/{identifier}/reopen')
async def reopen(identifier: int,request: Request):
    require_role(request,'operator')
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request,'operator',tx)
        group=await _group(tx,identifier)
        await tx.execute(f"UPDATE errors SET status='open',resolved_at=NULL WHERE status='resolved' AND {GROUP}=? "
                         f"AND resolved_at IS (SELECT MAX(resolved_at) FROM errors WHERE status='resolved' AND {GROUP}=?)",(group,group))
    await audit.record(request,'errors.reopened',str(identifier))
    return {'id':str(identifier),'status':'open'}
