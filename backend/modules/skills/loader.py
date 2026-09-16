"""Sync the bundled n8n skill library from disk into the skills table at boot.

Disk is the source of truth; the table is a queryable cache so the assistant tool and the API read
one place. Skills ship with the application and are never written through the API, so a boot-time
sync is the whole lifecycle: folders that disappear are removed, changed bodies are replaced.

The frontmatter parser reads two known keys rather than pulling in a YAML dependency: this project
declares none (see pyproject.toml), and a general YAML loader is a large parser pointed at files on
disk for no gain when the contract is `name` and `description`.
"""
from pathlib import Path
import re
from typing import Any

# Skills live beside the backend package, not in the writable data directory: they are application
# content, versioned with the code.
SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / 'skills'
# A skill id is its folder name and is used verbatim in URLs and tool arguments.
SKILL_ID = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')
MAX_SKILL_BYTES = 256 * 1024
FRONTMATTER = re.compile(r'\A---\r?\n(.*?)\r?\n---\r?\n?', re.DOTALL)


def parse_skill(text: str) -> tuple[str, str, str]:
    """Return (name, description, body) for one SKILL.md.

    A file without usable frontmatter still loads: the body is what the model needs, and a missing
    description costs discoverability, not correctness.
    """
    match = FRONTMATTER.match(text)
    if not match:
        return '', '', text.strip()
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(':')
        if separator and key.strip() in ('name', 'description') and not key.startswith((' ', '\t')):
            fields[key.strip()] = value.strip().strip('"\'')
    return fields.get('name', ''), fields.get('description', ''), text[match.end():].strip()


def read_skills(directory: Path | None = None) -> list[dict[str, str]]:
    """Read every well-formed skill folder. Unreadable or oversized entries are skipped, not fatal."""
    directory = directory or SKILLS_DIR
    if not directory.is_dir():
        return []
    skills: list[dict[str, str]] = []
    for folder in sorted(directory.iterdir()):
        if not folder.is_dir() or folder.is_symlink() or not SKILL_ID.match(folder.name):
            continue
        document = folder / 'SKILL.md'
        if not document.is_file() or document.is_symlink():
            continue
        try:
            if document.stat().st_size > MAX_SKILL_BYTES:
                continue
            text = document.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        name, description, body = parse_skill(text)
        if not body:
            continue
        skills.append({'id': folder.name, 'name': name or folder.name,
                       'description': description, 'content': body})
    return skills


async def initialize(app: Any) -> None:
    """Replace the table's contents with what is on disk. Runs inside one transaction."""
    skills = read_skills()
    async with app.state.db.transaction() as tx:
        present = [row['id'] for row in await tx.fetchall('SELECT id FROM skills')]
        keep = {skill['id'] for skill in skills}
        for identifier in present:
            if identifier not in keep:
                await tx.execute('DELETE FROM skills WHERE id=?', (identifier,))
        for skill in skills:
            await tx.execute(
                'INSERT INTO skills(id,name,description,content) VALUES(?,?,?,?) '
                'ON CONFLICT(id) DO UPDATE SET name=excluded.name,description=excluded.description,'
                'content=excluded.content,updated_at=CURRENT_TIMESTAMP',
                (skill['id'], skill['name'], skill['description'], skill['content']))
    return None
