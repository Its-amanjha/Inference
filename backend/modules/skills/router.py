"""Read-only access to the bundled n8n skill library.

Skills are application content shipped with the release, so there is no write path here: the boot
loader owns the table. Any signed-in reader may list and read them.
"""
from typing import Any

from fastapi import APIRouter, Path, Request

from backend.auth_gate import fail, require_current_role, require_role

router = APIRouter()
SKILL_ID = r'^[a-z0-9][a-z0-9-]{0,63}$'


async def read_skill(request: Request, skill_id: str) -> dict[str, Any]:
    """One skill with its markdown body, or 404. Shared by the API and the assistant tool."""
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request, 'viewer', tx)
        row = await tx.fetchone('SELECT id,name,description,content,updated_at FROM skills WHERE id=?', (skill_id,))
    if row is None:
        fail(404, 'skill_not_found', 'No skill with that identifier is installed.')
    return row


@router.get('')
async def skills(request: Request) -> dict[str, Any]:
    """List installed skills without their bodies, which run to tens of kilobytes each."""
    require_role(request, 'viewer')
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request, 'viewer', tx)
        rows = await tx.fetchall('SELECT id,name,description,updated_at FROM skills ORDER BY id')
    return {'data': rows}


@router.get('/{skill_id}')
async def skill(request: Request, skill_id: str = Path(pattern=SKILL_ID)) -> dict[str, Any]:
    require_role(request, 'viewer')
    return await read_skill(request, skill_id)
