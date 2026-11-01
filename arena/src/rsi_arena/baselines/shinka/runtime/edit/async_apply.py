"""
Async patch application functions for non-blocking file operations.
Provides async versions of patch application and validation.
"""

import asyncio
import logging
from typing import Tuple, Optional
from pathlib import Path
from .apply_diff import apply_diff_patch
from .apply_full import apply_full_patch
from rsi_arena.baselines.shinka.runtime.utils.languages import normalize_language
from rsi_arena.baselines.shinka.runtime.utils.wolfram import (
    build_wolframscript_argv,
    escape_wolfram_string,
)

try:
    import aiofiles
except ImportError:
    aiofiles = None

logger = logging.getLogger(__name__)
TEXT_ENCODING = "utf-8"


async def _run_validation_subprocess(
    *args: str, timeout: int
) -> Tuple[bool, Optional[str]]:
    """Run a validator subprocess and normalize timeout/error handling."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"Validation timeout after {timeout}s"

    if proc.returncode == 0:
        return True, None

    error_msg = stderr.decode() if stderr else "Unknown compilation error"
    return False, error_msg


async def apply_patch_async(
    original_str: str,
    patch_str: str,
    patch_dir: str,
    language: str = "python",
    patch_type: str = "diff",
    verbose: bool = False,
) -> Tuple[
    Optional[str], int, Optional[str], Optional[str], Optional[str], Optional[Path]
]:
    """Async version of patch application.

    Args:
        original_str: Original code content
        patch_str: Patch content from LLM
        patch_dir: Directory to write patch files
        language: Programming language
        patch_type: Type of patch (diff, full, cross)
        verbose: Enable verbose logging

    Returns:
        Tuple of (modified_code, num_applied, output_path, error_msg, patch_txt, patch_path)
    """
    loop = asyncio.get_event_loop()

    try:
        # Create patch directory synchronously to avoid race conditions
        try:
            Path(patch_dir).mkdir(parents=True, exist_ok=True)
        except FileExistsError:
            # Another task already created it, which is fine
            pass

        # Choose the appropriate patch function
        if patch_type in ["full", "cross"]:
            patch_func = apply_full_patch
        elif patch_type == "diff":
            patch_func = apply_diff_patch
        else:
            raise ValueError(f"Unknown patch type: {patch_type}")

        # Run patch application in thread pool to avoid blocking
        result = await loop.run_in_executor(
            None,
            lambda: patch_func(
                patch_str=patch_str,
                original_str=original_str,
                patch_dir=patch_dir,
                language=language,
                verbose=verbose,
            ),
        )

        return result

    except Exception as e:
        logger.error(f"Error in async patch application: {e}")
        return None, 0, None, str(e), None, None


async def validate_code_async(
    code_path: str, language: str = "python", timeout: int = 30
) -> Tuple[bool, Optional[str]]:
    """Async code validation using subprocess.

    Args:
        code_path: Path to code file to validate
        language: Programming language
        timeout: Timeout for validation in seconds

    Returns:
        Tuple of (is_valid, error_message)
    """
    try:
        try:
            language = normalize_language(language)
        except ValueError:
            language = language.strip().lower()
        if language == "python":
            # Use python -m py_compile for syntax checking
            return await _run_validation_subprocess(
                "python",
                "-m",
                "py_compile",
                code_path,
                timeout=timeout,
            )

        elif language == "rust":
            # Use rustc for Rust syntax checking
            return await _run_validation_subprocess(
                "rustc",
                "--crate-type=lib",
                "-Zparse-only",
                code_path,
                timeout=timeout,
            )
        elif language == "swift":
            # Use swiftc for Swift compilation check
            return await _run_validation_subprocess(
                "swiftc",
                code_path,
                timeout=timeout,
            )
        elif language in ["json", "json5"]:
            # Use jsonschema for JSON validation
            return await _run_validation_subprocess(
                "jsonschema",
                code_path,
                timeout=timeout,
            )
        elif language == "cpp":
            # Use g++ for C++ compilation check
            return await _run_validation_subprocess(
                "g++",
                "-fsyntax-only",
                code_path,
                timeout=timeout,
            )
        elif language == "fortran":
            # Use gfortran for Fortran syntax checking
            return await _run_validation_subprocess(
                "gfortran",
                "-fsyntax-only",
                code_path,
                timeout=timeout,
            )
        elif language == "verilog":
            return await _run_validation_subprocess(
                "iverilog",
                "-t",
                "null",
                "-g2012",
                code_path,
                timeout=timeout,
            )
        elif language == "wolfram":
            # Parse-only via Hold prevents evaluation; non-Hold result indicates a parse error.
            check_code = (
                f'If[Head[ToExpression[Import["{escape_wolfram_string(code_path)}", '
                '"Text"], InputForm, Hold]] === Hold, Print["OK"], Exit[1]]'
            )
            argv = build_wolframscript_argv(["-code", check_code])
            return await _run_validation_subprocess(*argv, timeout=timeout)
        else:
            # For other languages, just check if file exists and is readable
            try:
                if aiofiles:
                    async with aiofiles.open(
                        code_path, "r", encoding=TEXT_ENCODING
                    ) as f:
                        content = await f.read()
                else:
                    loop = asyncio.get_event_loop()
                    content = await loop.run_in_executor(
                        None,
                        lambda: Path(code_path).read_text(encoding=TEXT_ENCODING),
                    )

                if len(content.strip()) > 0:
                    return True, None
                return False, "Empty code file"
            except Exception as e:
                return False, f"File read error: {str(e)}"

    except Exception as e:
        logger.error(f"Error in async code validation: {e}")
        return False, f"Validation error: {str(e)}"


async def write_file_async(file_path: str, content: str) -> bool:
    """Async file writing.

    Args:
        file_path: Path to write file
        content: Content to write

    Returns:
        True if successful, False otherwise
    """
    try:
        # Ensure parent directory exists
        parent_dir = Path(file_path).parent
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: parent_dir.mkdir(parents=True, exist_ok=True)
        )

        if aiofiles:
            # Use aiofiles if available
            async with aiofiles.open(file_path, "w", encoding=TEXT_ENCODING) as f:
                await f.write(content)
        else:
            # Fall back to sync I/O in thread pool
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: Path(file_path).write_text(
                    content, encoding=TEXT_ENCODING
                ),
            )

        return True

    except Exception as e:
        logger.error(f"Error writing file {file_path}: {e}")
        return False


async def read_file_async(file_path: str) -> Optional[str]:
    """Async file reading.

    Args:
        file_path: Path to read file

    Returns:
        File content or None if error
    """
    try:
        if aiofiles:
            # Use aiofiles if available
            async with aiofiles.open(file_path, "r", encoding=TEXT_ENCODING) as f:
                content = await f.read()
        else:
            # Fall back to sync I/O in thread pool
            loop = asyncio.get_event_loop()
            content = await loop.run_in_executor(
                None, lambda: Path(file_path).read_text(encoding=TEXT_ENCODING)
            )
        return content

    except Exception as e:
        logger.error(f"Error reading file {file_path}: {e}")
        return None


async def copy_file_async(src_path: str, dst_path: str) -> bool:
    """Async file copying.

    Args:
        src_path: Source file path
        dst_path: Destination file path

    Returns:
        True if successful, False otherwise
    """
    try:
        # Read source file
        content = await read_file_async(src_path)
        if content is None:
            return False

        # Write to destination
        return await write_file_async(dst_path, content)

    except Exception as e:
        logger.error(f"Error copying file {src_path} to {dst_path}: {e}")
        return False


async def get_code_embedding_async(
    exec_fname: str, embedding_client, max_chars: int = 10000
) -> Tuple[Optional[list], float]:
    """Async code embedding generation.

    Args:
        exec_fname: Path to code file
        embedding_client: Embedding client instance
        max_chars: Maximum characters to embed

    Returns:
        Tuple of (embedding_vector, cost)
    """
    try:
        # Read code file asynchronously
        code_content = await read_file_async(exec_fname)
        if not code_content:
            return None, 0.0

        # Truncate if too long
        if len(code_content) > max_chars:
            code_content = code_content[:max_chars]

        # Generate embedding in thread pool
        loop = asyncio.get_event_loop()

        if hasattr(embedding_client, "embed_async"):
            # Use async embedding if available
            embedding, cost = await embedding_client.embed_async(code_content)
        else:
            # Fall back to sync embedding in thread pool
            embedding, cost = await loop.run_in_executor(
                None, embedding_client.get_embedding, code_content
            )

        return embedding, cost

    except Exception as e:
        logger.error(f"Error generating code embedding for {exec_fname}: {e}")
        return None, 0.0
