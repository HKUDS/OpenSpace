"""OpenSpace cloud platform HTTP client.

All methods are **synchronous** (use ``urllib``).  In async contexts
(MCP server), wrap calls with ``asyncio.to_thread()``.

Provides both low-level HTTP operations and higher-level workflows:
  - ``fetch_record`` / ``download_artifact`` / ``fetch_metadata``
  - ``search_record_embeddings``
  - ``stage_artifact`` / ``create_record``
  - ``upload_skill`` (stage → diff → create — full workflow)
  - ``import_skill`` (fetch → download → extract — full workflow)
"""

from __future__ import annotations

import difflib
import io
import json
import logging
import os
import uuid
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("openspace.cloud")

SKILL_FILENAME = "SKILL.md"
SKILL_ID_FILENAME = ".skill_id"
RECORD_EMBEDDING_SEARCH_MAX_LIMIT = 300

_TEXT_EXTENSIONS = frozenset({
    ".md", ".txt", ".yaml", ".yml", ".json", ".py", ".sh", ".toml",
})


class CloudError(Exception):
    """Raised when a cloud API call fails."""

    def __init__(self, message: str, status_code: int = 0, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class OpenSpaceClient:
    """HTTP client for the OpenSpace cloud API.

    Args:
        auth_headers: Pre-resolved auth headers (from ``get_openspace_auth``).
        api_base: API base URL (e.g. ``https://open-space.cloud/api/v1``).
    """

    _DEFAULT_UA = "OpenSpace-Client/1.0"

    def __init__(self, auth_headers: Dict[str, str], api_base: str):
        if not auth_headers:
            raise CloudError(
                "No OPENSPACE_API_KEY configured. "
                "Register at https://open-space.cloud to obtain a key."
            )
        self._headers = {
            "User-Agent": self._DEFAULT_UA,
            **auth_headers,
        }
        self._base = api_base.rstrip("/")

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[bytes] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        timeout: int = 30,
    ) -> tuple[int, bytes]:
        """Execute HTTP request.  Returns ``(status_code, response_body)``."""
        url = f"{self._base}{path}"
        headers = {**self._headers}
        if extra_headers:
            headers.update(extra_headers)

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            resp_body = e.read().decode("utf-8", errors="replace")
            raise CloudError(
                f"HTTP {e.code}: {resp_body[:500]}",
                status_code=e.code,
                body=resp_body,
            )
        except urllib.error.URLError as e:
            raise CloudError(f"Connection failed: {e.reason}")

    def _get_json(self, path: str, timeout: int = 30) -> Dict[str, Any]:
        _, data = self._request("GET", path, timeout=timeout)
        return json.loads(data.decode("utf-8"))

    def fetch_record(self, record_id: str) -> Dict[str, Any]:
        """GET /records/{record_id} — fetch record metadata."""
        return self._get_json(f"/records/{urllib.parse.quote(record_id)}")

    def download_artifact(self, record_id: str) -> bytes:
        """GET /records/{record_id}/download — download artifact zip bytes."""
        _, data = self._request(
            "GET",
            f"/records/{urllib.parse.quote(record_id)}/download",
            timeout=120,
        )
        return data

    def fetch_metadata(
        self,
        *,
        include_embedding: bool = False,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """GET /records/metadata — fetch all visible records with pagination."""
        all_items: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            params: Dict[str, str] = {"limit": str(limit)}
            if include_embedding:
                params["include_embedding"] = "true"
            if cursor:
                params["cursor"] = cursor

            path = f"/records/metadata?{urllib.parse.urlencode(params)}"
            data = self._get_json(path, timeout=15)

            all_items.extend(data.get("items", []))

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break

        return all_items

    def search_record_embeddings(
        self,
        *,
        query: str,
        limit: int = RECORD_EMBEDDING_SEARCH_MAX_LIMIT,
        level: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """POST /records/embeddings/search — fetch server-ranked embedding rows."""
        search_request_payload: Dict[str, Any] = {
            "query": query,
            "limit": limit,
        }
        if level:
            search_request_payload["level"] = level
        if tags:
            search_request_payload["tags"] = tags

        _, response_body = self._request(
            "POST",
            "/records/embeddings/search",
            body=json.dumps(search_request_payload).encode("utf-8"),
            extra_headers={"Content-Type": "application/json"},
            timeout=30,
        )
        return json.loads(response_body.decode("utf-8"))

    def stage_artifact(self, skill_dir: Path) -> tuple[str, int]:
        """POST /artifacts/stage — upload skill files.

        Returns ``(artifact_id, file_count)``.
        """
        file_paths = self._collect_files(skill_dir)
        if not file_paths:
            raise CloudError("No files found in skill directory")

        boundary = f"----OpenSpaceUpload{os.urandom(8).hex()}"
        body_parts: list[bytes] = []
        for fp in file_paths:
            rel_path = str(fp.relative_to(skill_dir))
            body_parts.append(f"--{boundary}\r\n".encode())
            body_parts.append(
                f'Content-Disposition: form-data; name="files"; '
                f'filename="{rel_path}"\r\n'.encode()
            )
            ctype = "text/plain" if fp.suffix in _TEXT_EXTENSIONS else "application/octet-stream"
            body_parts.append(f"Content-Type: {ctype}\r\n\r\n".encode())
            body_parts.append(fp.read_bytes())
            body_parts.append(b"\r\n")
        body_parts.append(f"--{boundary}--\r\n".encode())

        _, resp_data = self._request(
            "POST",
            "/artifacts/stage",
            body=b"".join(body_parts),
            extra_headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout=60,
        )
        stage = json.loads(resp_data.decode("utf-8"))
        artifact_id = stage.get("artifact_id")
        if not artifact_id:
            raise CloudError("No artifact_id in stage response")
        file_count = stage.get("stats", {}).get("file_count", 0)
        return artifact_id, file_count

    def create_record(self, payload: Dict[str, Any]) -> tuple[Dict[str, Any], int]:
        """POST /records — create skill record with 409 conflict handling.

        Returns ``(response_data, status_code)``.
        """
        body = json.dumps(payload).encode("utf-8")
        try:
            status, resp_data = self._request(
                "POST",
                "/records",
                body=body,
                extra_headers={"Content-Type": "application/json"},
            )
            return json.loads(resp_data.decode("utf-8")), status
        except CloudError as e:
            if e.status_code == 409:
                return self._handle_409(e.body, payload)
            raise

    def _handle_409(
        self, body_text: str, payload: Dict[str, Any],
    ) -> tuple[Dict[str, Any], int]:
        """Handle 409 conflict responses."""
        try:
            err_data = json.loads(body_text)
        except json.JSONDecodeError:
            raise CloudError(f"409 conflict: {body_text}", status_code=409, body=body_text)

        err_type = err_data.get("error", "")

        if err_type == "fingerprint_record_id_conflict":
            existing_id = err_data.get("existing_record_id", "")
            return {
                "record_id": existing_id,
                "status": "duplicate",
                "existing_record_id": existing_id,
            }, 409

        if err_type == "record_id_fingerprint_conflict":
            # Retry with a new UUID
            name = payload.get("name", "skill")
            payload["record_id"] = f"{name}__clo_{uuid.uuid4().hex[:8]}"
            retry_body = json.dumps(payload).encode("utf-8")
            status, resp_data = self._request(
                "POST",
                "/records",
                body=retry_body,
                extra_headers={"Content-Type": "application/json"},
            )
            return json.loads(resp_data.decode("utf-8")), status

        raise CloudError(f"409 conflict: {body_text}", status_code=409, body=body_text)

    def upload_skill(
        self,
        skill_dir: Path,
        *,
        visibility: str = "public",
        origin: str = "imported",
        parent_skill_ids: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        created_by: str = "",
        change_summary: str = "",
    ) -> Dict[str, Any]:
        """Upload a local skill to the cloud (stage → diff → create record).

        Returns a result dict with status, record_id, etc.
        """
        from openspace.skill_engine.skill_utils import parse_frontmatter

        skill_path = Path(skill_dir)
        skill_file = skill_path / SKILL_FILENAME
        if not skill_file.exists():
            raise CloudError(f"SKILL.md not found in {skill_dir}")

        content = skill_file.read_text(encoding="utf-8")
        fm = parse_frontmatter(content)
        name = fm.get("name", skill_path.name)
        description = fm.get("description", "")

        if not name:
            raise CloudError("SKILL.md frontmatter missing 'name' field")

        parents = parent_skill_ids or []
        self._validate_origin_parents(origin, parents)

        api_visibility = "group_only" if visibility == "private" else "public"

        # Step 1: Stage
        logger.info(f"upload_skill: staging files for '{name}'")
        artifact_id, file_count = self.stage_artifact(skill_path)
        logger.info(f"upload_skill: staged {file_count} file(s), artifact_id={artifact_id}")

        # Step 2: Content diff
        content_diff = self._compute_content_diff(skill_path, api_visibility, parents)

        # Step 3: Create record
        record_id = f"{name}__clo_{uuid.uuid4().hex[:8]}"
        payload: Dict[str, Any] = {
            "record_id": record_id,
            "artifact_id": artifact_id,
            # name/description are NOT sent — the server extracts them
            # from SKILL.md YAML frontmatter (Task 4+F4 change).
            "origin": origin,
            "visibility": api_visibility,
            "parent_skill_ids": parents,
            "tags": tags or [],
            "level": "workflow",
        }
        if created_by:
            payload["created_by"] = created_by
        if change_summary:
            payload["change_summary"] = change_summary
        if content_diff is not None:
            payload["content_diff"] = content_diff

        record_data, status_code = self.create_record(payload)
        action = "created" if status_code == 201 else "exists (idempotent)"
        final_record_id = record_data.get("record_id", record_id)

        logger.info(
            f"upload_skill: {name} [{final_record_id}] — {action} "
            f"(visibility={api_visibility}, origin={origin})"
        )

        # Check for duplicate status from 409 handling
        if record_data.get("status") == "duplicate":
            return {
                "status": "duplicate",
                "message": f"Same content already exists as record '{record_data.get('existing_record_id', '')}'",
                "existing_record_id": record_data.get("existing_record_id", ""),
            }

        return {
            "status": "success",
            "action": action,
            "record_id": final_record_id,
            "name": name,
            "description": description,
            "visibility": api_visibility,
            "origin": origin,
            "parent_skill_ids": parents,
            "artifact_id": artifact_id,
            "file_count": file_count,
        }

    def import_skill(
        self,
        skill_id: str,
        target_dir: Path,
    ) -> Dict[str, Any]:
        """Download a cloud skill and extract to a local directory.

        Returns a result dict with status, local_path, files, etc.
        """
        # 1. Fetch metadata
        logger.info(f"import_skill: fetching metadata for {skill_id}")
        record_data = self.fetch_record(skill_id)
        skill_name = record_data.get("name", skill_id)

        if "/" in skill_name or "\\" in skill_name or skill_name.startswith("."):
            skill_name = skill_id
        skill_dir = (target_dir / skill_name).resolve()
        if not skill_dir.is_relative_to(target_dir.resolve()):
            raise CloudError(f"Skill name {skill_name!r} escapes target directory")

        # Check if already exists locally
        if skill_dir.exists() and (skill_dir / SKILL_FILENAME).exists():
            return {
                "status": "already_exists",
                "skill_id": skill_id,
                "name": skill_name,
                "local_path": str(skill_dir),
            }

        # 2. Download artifact
        logger.info(f"import_skill: downloading artifact for {skill_id}")
        zip_data = self.download_artifact(skill_id)

        # 3. Extract
        skill_dir.mkdir(parents=True, exist_ok=True)
        extracted = self._extract_zip(zip_data, skill_dir)

        # 4. Write .skill_id sidecar
        (skill_dir / SKILL_ID_FILENAME).write_text(skill_id + "\n", encoding="utf-8")

        logger.info(
            f"import_skill: {skill_name} [{skill_id}] → {skill_dir} "
            f"({len(extracted)} files)"
        )

        return {
            "status": "success",
            "skill_id": skill_id,
            "name": skill_name,
            "description": record_data.get("description", ""),
            "local_path": str(skill_dir),
            "files": extracted,
        }

    @staticmethod
    def _collect_files(skill_dir: Path) -> List[Path]:
        """Collect all files in skill directory (skip .skill_id sidecar)."""
        return [
            p for p in sorted(skill_dir.rglob("*"))
            if p.is_file() and p.name != SKILL_ID_FILENAME
        ]

    @staticmethod
    def _collect_text_files(skill_dir: Path) -> Dict[str, str]:
        """Collect text files as ``{relative_path: content}``."""
        files: Dict[str, str] = {}
        for p in sorted(skill_dir.rglob("*")):
            if p.is_file() and p.name != SKILL_ID_FILENAME:
                rel = str(p.relative_to(skill_dir))
                try:
                    files[rel] = p.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    pass
        return files

    @staticmethod
    def _extract_zip(zip_data: bytes, target_dir: Path) -> List[str]:
        """Extract zip bytes to target directory with path traversal protection."""
        extracted: List[str] = []
        resolved_target = target_dir.resolve()
        try:
            with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    clean_name = Path(info.filename).as_posix()
                    if clean_name.startswith("..") or clean_name.startswith("/"):
                        continue
                    target_path = (target_dir / clean_name).resolve()
                    if not target_path.is_relative_to(resolved_target):
                        continue
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.write_bytes(zf.read(info))
                    extracted.append(clean_name)
        except zipfile.BadZipFile:
            raise CloudError("Downloaded artifact is not a valid zip file")
        return extracted

    @staticmethod
    def _extract_zip_text_files(zip_data: bytes) -> Dict[str, str]:
        """Extract text files from zip as ``{filename: content}``."""
        files: Dict[str, str] = {}
        try:
            with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
                for info in zf.infolist():
                    if info.is_dir() or info.filename == SKILL_ID_FILENAME:
                        continue
                    try:
                        files[info.filename] = zf.read(info).decode("utf-8")
                    except (UnicodeDecodeError, KeyError):
                        pass
        except zipfile.BadZipFile:
            pass
        return files

    @staticmethod
    def _validate_origin_parents(origin: str, parents: List[str]) -> None:
        if origin in ("imported", "captured") and parents:
            raise CloudError(f"origin='{origin}' must not have parent_skill_ids")
        if origin == "derived" and not parents:
            raise CloudError("origin='derived' requires at least 1 parent_skill_id")
        if origin == "fixed" and len(parents) != 1:
            raise CloudError("origin='fixed' requires exactly 1 parent_skill_id")

    def _compute_content_diff(
        self,
        skill_dir: Path,
        api_visibility: str,
        parents: List[str],
    ) -> Optional[str]:
        """Compute content_diff for the upload.

        - public + single parent → diff vs ancestor
        - public + no parent → add-all diff
        - else → None
        """
        if api_visibility != "public":
            return None

        cur_files = self._collect_text_files(skill_dir)

        if len(parents) == 1:
            try:
                anc_zip = self.download_artifact(parents[0])
                anc_files = self._extract_zip_text_files(anc_zip)
                diff = self._unified_diff(anc_files, cur_files)
                if diff:
                    logger.info(f"Computed diff vs ancestor {parents[0]}")
                    return diff
            except Exception as e:
                logger.warning(f"Diff computation failed: {e}")
            return None

        if not parents:
            return self._unified_diff({}, cur_files)

        return None  # multiple parents

    @staticmethod
    def _unified_diff(old_files: Dict[str, str], new_files: Dict[str, str]) -> Optional[str]:
        """Compute combined unified diff between two file snapshots."""
        all_names = sorted(set(old_files) | set(new_files))
        parts: List[str] = []
        for fname in all_names:
            old = old_files.get(fname, "")
            new = new_files.get(fname, "")
            d = "".join(difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"a/{fname}",
                tofile=f"b/{fname}",
                n=3,
            ))
            if d:
                parts.append(d)
        return "\n".join(parts) if parts else None
