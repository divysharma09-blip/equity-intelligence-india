"""Locate the Claude Code CLI. AI features run through it on the user's Claude plan, never the API."""

from __future__ import annotations

import glob
import os
import shutil
from pathlib import Path
from typing import Optional


def find_claude() -> Optional[str]:
    """$CLAUDE_BIN, PATH, the standalone install, then the VS Code extension's bundled binary."""
    candidates = [os.getenv("CLAUDE_BIN"), shutil.which("claude"), str(Path.home() / ".local/bin/claude")]
    # The VS Code extension ships its own binary; its folder name changes on every update.
    candidates += sorted(
        glob.glob(str(Path.home() / ".vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude")),
        key=os.path.getmtime, reverse=True)
    return next((p for p in candidates if p and os.access(p, os.X_OK)), None)


def claude_env() -> dict[str, str]:
    """Environment for the CLI with ANTHROPIC_API_KEY removed, so it always uses the Claude plan login."""
    return {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
