from typing import Literal
from fastapi import APIRouter, Query, Request
from backend import audit
from backend.auth_gate import fail, require_current_role, require_role

router=APIRouter()


@router.get('')
@router.get('/')
async def errors(request: Request,instance_id: str | None=Query(default=None,max_length=128),workflow_id: str | None=Query(default=None,max_length=128),
                 status: Literal['open','resolved']='open',limit: int=Query(default=50,ge=1,le=100),before: int | None=Query(default=None,ge=1)):
    require_role(request,'viewer')
    conditions=['status=?']
    params=[status]
    if instance_id:
        conditions.append('instance_id=?')
        params.append(instance_id)
    if workflow_id:
        conditions.append('workflow_id=?')
        params.append(workflow_id)
    if before:
        conditions.append('id<?')
        params.append(before)
    params.append(limit+1)
    rows=await request.app.state.db.fetchall(f"SELECT id,instance_id,workflow_id,workflow_name,execution_id,node_name,error_type,error_message,count,occurred_at,status,resolved_at FROM errors WHERE {' AND '.join(conditions)} ORDER BY id DESC LIMIT ?",params)
    more=len(rows)>limit
    rows=rows[:limit]
    for row in rows:
        row['id']=str(row['id'])
    return {'data':rows,'next_cursor':rows[-1]['id'] if more else None}


@router.post('/{identifier}/resolve')
async def resolve(identifier: int,request: Request):
    require_role(request,'operator')
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request,'operator',tx)
        if not await tx.fetchone('SELECT id FROM errors WHERE id=?',(identifier,)):
            fail(404,'error_not_found','Error was not found.')
        await tx.execute("UPDATE errors SET status='resolved',resolved_at=datetime('now') WHERE id=?",(identifier,))
    await audit.record(request,'errors.resolved',str(identifier))
    return {'id':str(identifier),'status':'resolved'}


# Undo for a resolve: the row returns to the open queue unchanged.
@router.post('/{identifier}/reopen')
async def reopen(identifier: int,request: Request):
    require_role(request,'operator')
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request,'operator',tx)
        if not await tx.fetchone('SELECT id FROM errors WHERE id=?',(identifier,)):
            fail(404,'error_not_found','Error was not found.')
        await tx.execute("UPDATE errors SET status='open',resolved_at=NULL WHERE id=?",(identifier,))
    await audit.record(request,'errors.reopened',str(identifier))
    return {'id':str(identifier),'status':'open'}
