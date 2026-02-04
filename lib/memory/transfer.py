"""
Context transfer orchestration.

Coordinates the full pipeline: parse -> dedupe -> truncate -> format -> send.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Default storage directory
TRANSFERS_DIR = Path.home() / ".ccb" / "transfers"

from .types import ConversationEntry, TransferContext, SessionNotFoundError, SessionStats
from .session_parser import ClaudeSessionParser
from .deduper import ConversationDeduper
from .formatter import ContextFormatter


class ContextTransfer:
    """Orchestrate context transfer between providers."""

    SUPPORTED_PROVIDERS = ("codex", "gemini", "opencode", "droid")

    def __init__(
        self,
        max_tokens: int = 8000,
        work_dir: Optional[Path] = None,
    ):
        self.max_tokens = max_tokens
        self.work_dir = work_dir or Path.cwd()
        self.parser = ClaudeSessionParser()
        self.deduper = ConversationDeduper()
        self.formatter = ContextFormatter(max_tokens=max_tokens)

    def extract_conversations(
        self,
        session_path: Optional[Path] = None,
        last_n: int = 3,
        include_stats: bool = True,
    ) -> TransferContext:
        """Extract and process conversations from a session."""
        # Resolve session
        resolved = self.parser.resolve_session(self.work_dir, session_path)
        info = self.parser.get_session_info(resolved)

        # Extract session stats
        stats = None
        if include_stats:
            stats = self.parser.extract_session_stats(resolved)

        # Parse entries
        entries = self.parser.parse_session(resolved)

        # Clean and dedupe
        entries = self._clean_entries(entries)
        entries = self.deduper.dedupe_messages(entries)
        entries = self.deduper.collapse_tool_calls(entries)

        # Build conversation pairs
        pairs = self._build_pairs(entries)

        # Take last N pairs
        if last_n > 0 and len(pairs) > last_n:
            pairs = pairs[-last_n:]

        # Truncate to token limit
        pairs = self.formatter.truncate_to_limit(pairs, self.max_tokens)

        # Estimate tokens
        total_text = "".join(u + a for u, a in pairs)
        token_estimate = self.formatter.estimate_tokens(total_text)

        return TransferContext(
            conversations=pairs,
            source_session_id=info.session_id,
            token_estimate=token_estimate,
            metadata={"session_path": str(resolved)},
            stats=stats,
        )

    def _clean_entries(
        self, entries: list[ConversationEntry]
    ) -> list[ConversationEntry]:
        """Clean all entries."""
        result = []
        for entry in entries:
            cleaned = self.deduper.clean_content(entry.content)
            if cleaned or entry.tool_calls:
                result.append(ConversationEntry(
                    role=entry.role,
                    content=cleaned,
                    uuid=entry.uuid,
                    parent_uuid=entry.parent_uuid,
                    timestamp=entry.timestamp,
                    tool_calls=entry.tool_calls,
                ))
        return result

    def _build_pairs(
        self, entries: list[ConversationEntry]
    ) -> list[tuple[str, str]]:
        """Build user/assistant conversation pairs."""
        pairs: list[tuple[str, str]] = []
        current_user: Optional[str] = None

        for entry in entries:
            if entry.role == "user":
                current_user = entry.content
            elif entry.role == "assistant" and current_user:
                pairs.append((current_user, entry.content))
                current_user = None

        return pairs

    def format_output(
        self,
        context: TransferContext,
        fmt: str = "markdown",
        detailed: bool = False,
    ) -> str:
        """Format context for output."""
        return self.formatter.format(context, fmt, detailed=detailed)

    def send_to_provider(
        self,
        context: TransferContext,
        provider: str,
        fmt: str = "markdown",
    ) -> tuple[bool, str]:
        """Send context to a provider via ask command."""
        if provider not in self.SUPPORTED_PROVIDERS:
            return False, f"Unsupported provider: {provider}"

        formatted = self.format_output(context, fmt)

        # Build the ask command
        cmd_map = {
            "codex": "cask",
            "gemini": "gask",
            "opencode": "oask",
            "droid": "dask",
        }
        cmd = cmd_map.get(provider, "ask")

        try:
            result = subprocess.run(
                [cmd, "--sync", formatted],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                return True, result.stdout
            return False, result.stderr or f"Command failed with code {result.returncode}"
        except FileNotFoundError:
            return False, f"Command not found: {cmd}"
        except subprocess.TimeoutExpired:
            return False, "Command timed out"
        except Exception as e:
            return False, str(e)

    def save_transfer(
        self,
        context: TransferContext,
        fmt: str = "markdown",
        target_provider: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> Path:
        """Save transfer to ~/.ccb/transfers/ with timestamp."""
        TRANSFERS_DIR.mkdir(parents=True, exist_ok=True)

        ext = {"markdown": "md", "plain": "txt", "json": "json"}.get(fmt, "md")
        if filename:
            safe = str(filename).strip().replace("/", "-").replace("\\", "-")
            if not Path(safe).suffix:
                safe = f"{safe}.{ext}"
            filepath = TRANSFERS_DIR / safe
        else:
            # Generate filename: YYYYMMDD-HHMMSS-{session_id}-to-{provider}.md
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            session_short = context.source_session_id[:8]
            provider_suffix = f"-to-{target_provider}" if target_provider else ""
            filepath = TRANSFERS_DIR / f"{ts}-{session_short}{provider_suffix}.{ext}"

        formatted = self.format_output(context, fmt)
        filepath.write_text(formatted, encoding="utf-8")

        return filepath
