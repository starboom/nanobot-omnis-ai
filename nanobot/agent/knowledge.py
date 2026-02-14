"""Knowledge store: manages per-employee markdown knowledge bases."""

import re
from pathlib import Path


class KnowledgeStore:
    """Manages a directory of .md knowledge files with progressive loading.

    Files are indexed by name and description (from YAML frontmatter or first
    non-empty line).  The index is injected into the system prompt so the agent
    knows what's available; full content is read on demand via read_file.
    """

    def __init__(self, knowledge_dir: Path):
        self.dir = knowledge_dir

    def list_files(self) -> list[dict]:
        """List all .md files, returning [{name, path, description}]."""
        if not self.dir.exists():
            return []
        files = []
        for p in sorted(self.dir.glob("*.md")):
            files.append({
                "name": p.name,
                "path": str(p),
                "description": self._extract_description(p),
            })
        return files

    def build_index(self) -> str:
        """Build an XML index of knowledge files for the system prompt."""
        files = self.list_files()
        if not files:
            return ""
        lines = ["<knowledge>"]
        for f in files:
            lines.append(
                f'  <file name="{f["name"]}" path="{f["path"]}">'
                f'{f["description"]}</file>'
            )
        lines.append("</knowledge>")
        return "\n".join(lines)

    # ------------------------------------------------------------------

    _FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---", re.DOTALL)
    _DESC_RE = re.compile(r"^description:\s*(.+)$", re.MULTILINE)

    def _extract_description(self, path: Path) -> str:
        """Extract description from YAML frontmatter or first non-empty line."""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

        # Try frontmatter
        fm = self._FRONTMATTER_RE.match(text)
        if fm:
            m = self._DESC_RE.search(fm.group(1))
            if m:
                return m.group(1).strip()

        # Fallback: first non-empty, non-heading-marker line
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("---"):
                # Remove leading markdown heading markers
                stripped = stripped.lstrip("# ").strip()
                if stripped:
                    return stripped[:120]
        return ""
