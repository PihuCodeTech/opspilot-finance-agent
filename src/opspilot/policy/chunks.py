"""Split the generated policy document into citable sections.

Each `##`/`###` heading starts a chunk whose ID is its section number ("3.1",
"6", "A.V-0017"). Headings with no text of their own (a `##` directly followed
by `###`) produce no chunk.
"""

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{2,3}) (?:Appendix )?(\S+?)\.? (.+)$")


@dataclass(frozen=True)
class PolicyChunkText:
    section_id: str
    title: str
    text: str


def split_sections(document: str) -> list[PolicyChunkText]:
    chunks: list[PolicyChunkText] = []
    current: tuple[str, str] | None = None
    body: list[str] = []

    def flush() -> None:
        if current is not None:
            text = "\n".join(body).strip()
            if text:
                chunks.append(PolicyChunkText(current[0], current[1], text))

    for line in document.splitlines():
        match = _HEADING.match(line)
        if match:
            flush()
            current = (match.group(2), match.group(3).strip())
            body = []
        elif current is not None:
            body.append(line)
    flush()
    return chunks
