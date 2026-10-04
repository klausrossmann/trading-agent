"""Versioned prompts: `prompts/<module>/v<N>.md` with YAML front matter (IMPLEMENTATION.md 7.4)."""

from dataclasses import dataclass
from pathlib import Path
from typing import cast, get_args

import yaml

from trading_agent.domain.analysis import Role


@dataclass(frozen=True)
class Prompt:
    module: str
    version: int
    role: Role
    output: str  # name of the output schema, for readers of the prompt file
    text: str

    @property
    def ref(self) -> str:
        return f"{self.module}/v{self.version}"


def load_prompt(root: Path, module: str, version: int) -> Prompt:
    path = root / module / f"v{version}.md"
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        raise ValueError(f"{path}: missing front matter")
    head, sep, body = raw[4:].partition("\n---\n")
    if not sep:
        raise ValueError(f"{path}: unterminated front matter")
    meta = yaml.safe_load(head) or {}
    if meta.get("version") != version:
        raise ValueError(f"{path}: front matter version {meta.get('version')} != {version}")
    role = meta.get("role")
    if role not in get_args(Role):
        raise ValueError(f"{path}: unknown role {role!r}")
    return Prompt(
        module, version, cast(Role, role), str(meta.get("output", "")), body.strip() + "\n"
    )
