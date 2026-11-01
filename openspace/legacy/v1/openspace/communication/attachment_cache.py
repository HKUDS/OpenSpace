from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Optional

from openspace.utils.logging import Logger

from .types import AttachmentKind, ChannelAttachment

logger = Logger.get_logger(__name__)


class AttachmentCache:
    def __init__(
        self,
        base_dir: Path,
        *,
        max_attachment_bytes: int = 25 * 1024 * 1024,
        max_session_attachment_bytes: int = 100 * 1024 * 1024,
    ):
        self.base_dir = base_dir
        self.max_attachment_bytes = max_attachment_bytes
        self.max_session_attachment_bytes = max_session_attachment_bytes
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def session_dir(self, session_key: str) -> Path:
        directory = self.base_dir / session_key / "attachments"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def save_bytes(
        self,
        *,
        session_key: str,
        data: bytes,
        filename: str,
        kind: AttachmentKind,
        mime_type: str = "",
    ) -> Optional[ChannelAttachment]:
        data_size = len(data)
        if not self._within_limits(session_key, data_size):
            return None

        directory = self.session_dir(session_key)
        safe_name = _safe_name(filename)
        target = directory / f"{uuid.uuid4().hex[:12]}_{safe_name}"
        target.write_bytes(data)
        return ChannelAttachment(
            kind=kind,
            path=str(target),
            name=safe_name,
            mime_type=mime_type,
            size_bytes=len(data),
        )

    def copy_local_file(
        self,
        *,
        session_key: str,
        source_path: str,
        kind: AttachmentKind,
        preferred_name: Optional[str] = None,
        mime_type: str = "",
    ) -> Optional[ChannelAttachment]:
        source = Path(source_path).expanduser()
        if not source.exists():
            logger.warning("Attachment source does not exist: %s", source)
            return None
        source_size = source.stat().st_size
        if not self._within_limits(session_key, source_size):
            return None

        directory = self.session_dir(session_key)
        safe_name = _safe_name(preferred_name or source.name)
        target = directory / f"{uuid.uuid4().hex[:12]}_{safe_name}"
        shutil.copy2(source, target)
        return ChannelAttachment(
            kind=kind,
            path=str(target),
            name=safe_name,
            mime_type=mime_type,
            size_bytes=target.stat().st_size,
            metadata={"source_path": str(source)},
        )

    def _within_limits(self, session_key: str, attachment_size: int) -> bool:
        if attachment_size > self.max_attachment_bytes:
            logger.warning(
                "Rejecting attachment for session %s because %d bytes exceeds limit %d",
                session_key,
                attachment_size,
                self.max_attachment_bytes,
            )
            return False

        session_usage = self._session_usage_bytes(session_key)
        if session_usage + attachment_size > self.max_session_attachment_bytes:
            logger.warning(
                "Rejecting attachment for session %s because session quota would exceed %d bytes",
                session_key,
                self.max_session_attachment_bytes,
            )
            return False
        return True

    def _session_usage_bytes(self, session_key: str) -> int:
        directory = self.base_dir / session_key / "attachments"
        if not directory.exists():
            return 0

        total = 0
        for path in directory.iterdir():
            if path.is_file():
                total += path.stat().st_size
        return total


def _safe_name(name: str) -> str:
    value = (name or "attachment").replace("\x00", "").strip()
    value = Path(value).name
    return value or "attachment"
