#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import mimetypes
import os
import pathlib
import platform
import re
import select
import subprocess
import textwrap
import unicodedata
import urllib.parse
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any


DEFAULT_TOOL_OUTPUT_CHARS = 4000
TEXT_EXPORTABLE_SUFFIXES = {
    ".md",
    ".txt",
    ".json",
    ".pbtxt",
    ".yaml",
    ".yml",
    ".toml",
    ".log",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".m4v"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
PATH_REF_RE = re.compile(
    r"file://(?P<file_uri>/[^\s)\]`\"']+)|(?P<abs>/Users/[^\s)\]`\"']+)"
)
LIVE_STEP_SKIP_TYPES = {
    "CORTEX_STEP_TYPE_EPHEMERAL_MESSAGE",
}
LIVE_TRANSCRIPT_STEP_TYPES = {
    "CORTEX_STEP_TYPE_USER_INPUT",
    "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
    "CORTEX_STEP_TYPE_NOTIFY_USER",
}
NODE_RPC_HELPER = r"""
const http2 = require("http2");
const fs = require("fs");

const port = process.env.AG_PORT;
const csrf = process.env.AG_CSRF;
const method = process.env.AG_METHOD;
const certPath = process.env.AG_CERT_PATH;
const payloadJson = process.env.AG_PAYLOAD || "{}";

const payload = JSON.parse(payloadJson);
const ca = fs.readFileSync(certPath);
const client = http2.connect(`https://127.0.0.1:${port}`, {
  ca,
  servername: "localhost",
});

client.on("error", (error) => {
  console.error(String(error && error.stack || error));
  process.exit(98);
});

const req = client.request({
  ":method": "POST",
  ":path": `/exa.language_server_pb.LanguageServerService/${method}`,
  "content-type": "application/json",
  "connect-protocol-version": "1",
  "x-codeium-csrf-token": csrf,
});

let status = 0;
let body = "";
req.setEncoding("utf8");
req.on("response", (headers) => {
  status = Number(headers[":status"] || 0);
});
req.on("data", (chunk) => {
  body += chunk;
});
req.on("error", (error) => {
  console.error(String(error && error.stack || error));
  client.close();
  process.exit(99);
});
req.on("end", () => {
  client.close();
  if (status !== 200) {
    console.error(body || `HTTP ${status}`);
    process.exit(97);
  }
  process.stdout.write(body);
});
req.end(JSON.stringify(payload));
"""


@dataclass
class WorkspaceInfo:
    identifier: str
    name: str
    slug: str
    root: str | None
    aliases: set[str]


@dataclass
class SessionCandidate:
    session_id: str
    session_file: pathlib.Path
    workspace_dir: pathlib.Path
    workspace: WorkspaceInfo
    conversation: dict[str, Any]
    logs_path: pathlib.Path | None
    log_entries: list[dict[str, Any]]


@dataclass
class SessionRecord:
    session_id: str
    session_file: pathlib.Path
    workspace_dir: pathlib.Path
    workspace: WorkspaceInfo
    conversation: dict[str, Any]
    log_entries: list[dict[str, Any]]
    log_paths: list[pathlib.Path]
    source_paths: list[pathlib.Path]


@dataclass
class TranscriptEvent:
    timestamp: str | None
    sort_timestamp: dt.datetime | None
    order_key: tuple[int, int]
    kind: str
    label: str
    body: str
    metadata: dict[str, Any]


@dataclass
class ArtifactBundle:
    bundle_id: str
    workspace: WorkspaceInfo
    brain_dir: pathlib.Path | None
    annotation_path: pathlib.Path | None
    browser_recording_dir: pathlib.Path | None
    source_paths: list[pathlib.Path]
    text_files: list[pathlib.Path]
    media_files: list[pathlib.Path]
    extracted_paths: list[str]
    evidence_counts: Counter[str]


@dataclass
class CodeTrackerSnapshot:
    snapshot_id: str
    workspace: WorkspaceInfo
    snapshot_dir: pathlib.Path
    files: list[pathlib.Path]


@dataclass
class LiveConversationRecord:
    conversation_id: str
    trajectory_id: str
    workspace: WorkspaceInfo
    summary: dict[str, Any]
    trajectory: dict[str, Any]
    source_kind: str
    source_pid: int
    source_port: int


@dataclass
class GeneratedPage:
    item_id: str
    file_path: pathlib.Path
    content: str | bytes


@dataclass
class ExportStateRecord:
    item_id: str
    file_path: str
    signature: str


@dataclass
class ExportRunResult:
    output_dir: pathlib.Path
    page_count: int
    processed_pages: int
    workspace_count: int
    live_conversation_count: int
    raw_conversation_count: int
    reconstructed_conversation_count: int
    conversation_count: int
    artifact_count: int
    code_tracker_count: int


class WorkspaceCatalog:
    def __init__(self) -> None:
        self._by_identifier: dict[str, WorkspaceInfo] = {}
        self._alias_to_identifier: dict[str, str] = {}
        self._root_to_identifier: dict[str, str] = {}

    def register(
        self,
        identifier: str,
        *,
        root: str | None = None,
        aliases: list[str] | None = None,
        name: str | None = None,
    ) -> WorkspaceInfo:
        identifier = str(identifier).strip()
        if not identifier:
            raise ValueError("workspace identifier is required")
        aliases_set = {identifier}
        if aliases:
            aliases_set.update(str(alias).strip() for alias in aliases if str(alias).strip())
        root_text = str(root).strip() if root else None
        if root_text:
            aliases_set.add(sha256_text(root_text))
        canonical = self._alias_to_identifier.get(identifier)
        if canonical is None and root_text:
            canonical = self._root_to_identifier.get(root_text)
        if canonical is None:
            canonical = identifier

        existing = self._by_identifier.get(canonical)
        if existing is None:
            if root_text and not name:
                display_name = normalize_title(pathlib.Path(root_text).name or identifier, identifier)
            else:
                display_name = normalize_title(name or identifier, identifier)
            slug = slugify(identifier if identifier and not re.fullmatch(r"[0-9a-f]{32,64}", identifier) else display_name)
            existing = WorkspaceInfo(
                identifier=canonical,
                name=display_name,
                slug=slug,
                root=root_text,
                aliases=set(),
            )
            self._by_identifier[canonical] = existing
        else:
            if root_text and not existing.root:
                existing.root = root_text
            if root_text and (existing.name == existing.identifier or re.fullmatch(r"[0-9a-f]{32,64}", existing.name)):
                existing.name = normalize_title(pathlib.Path(root_text).name or existing.identifier, existing.identifier)
                if re.fullmatch(r"[0-9a-f]{32,64}", existing.identifier):
                    existing.slug = slugify(existing.name + "-" + short_identifier(existing.identifier))

        aliases_set.add(existing.slug)

        for alias in aliases_set:
            self._alias_to_identifier[alias] = canonical
            existing.aliases.add(alias)
        if root_text:
            self._root_to_identifier[root_text] = canonical
        return existing

    def get_by_alias(self, alias: str) -> WorkspaceInfo | None:
        alias = str(alias).strip()
        canonical = self._alias_to_identifier.get(alias)
        if canonical is None:
            return None
        return self._by_identifier[canonical]

    def match_path(self, path_text: str) -> WorkspaceInfo | None:
        path_text = path_text.strip()
        best: WorkspaceInfo | None = None
        best_len = -1
        for root, identifier in self._root_to_identifier.items():
            if path_text.startswith(root) and len(root) > best_len:
                best = self._by_identifier[identifier]
                best_len = len(root)
        if best is not None:
            return best

        path = pathlib.Path(path_text)
        for part in path.parts:
            matched = self.get_by_alias(part)
            if matched is not None:
                return matched

        try:
            works_index = path.parts.index("works")
        except ValueError:
            return None
        if len(path.parts) > works_index + 1:
            workspace_id = path.parts[works_index + 1]
            root = str(pathlib.Path(*path.parts[: works_index + 2]))
            return self.register(workspace_id, root=root)
        return None

    def all(self) -> list[WorkspaceInfo]:
        return sorted(self._by_identifier.values(), key=lambda item: item.name.lower())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export Antigravity workspace conversations and workspace-affiliated artifacts into Markdown."
        )
    )
    parser.add_argument(
        "--gemini-home",
        default=str(pathlib.Path.home() / ".gemini"),
        help="Path to GEMINI_CLI_HOME. Defaults to ~/.gemini.",
    )
    parser.add_argument(
        "--output-dir",
        default="antigravity-export",
        help="Directory where Markdown files will be written.",
    )
    parser.add_argument(
        "--session-id",
        action="append",
        dest="session_ids",
        default=[],
        help="Export only the specified conversation ID. Repeat to export multiple conversations.",
    )
    parser.add_argument(
        "--workspace-id",
        action="append",
        dest="workspace_ids",
        default=[],
        help=(
            "Export only the specified workspace identifier "
            "(slug, code-tracker prefix, or legacy hash). Repeat to export multiple workspaces."
        ),
    )
    parser.add_argument(
        "--project-id",
        action="append",
        dest="workspace_ids",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of conversations to export, ordered by lastUpdated descending.",
    )
    parser.add_argument(
        "--include-system-only",
        action="store_true",
        help="Include conversations that only contain info / warning / error messages.",
    )
    parser.add_argument(
        "--include-subagents",
        action="store_true",
        help="Include subagent conversations when present in the chat store.",
    )
    parser.add_argument(
        "--max-tool-output-chars",
        type=int,
        default=DEFAULT_TOOL_OUTPUT_CHARS,
        help="Maximum number of characters to keep for each tool output block.",
    )
    parser.add_argument(
        "--no-standalone-ls",
        action="store_true",
        help="Do not spawn the bundled Antigravity standalone language server when no live app server is available.",
    )
    parser.add_argument(
        "--no-incremental",
        action="store_true",
        help="Force rewriting every generated page instead of reusing cached signatures.",
    )
    return parser.parse_args()


def slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_only).strip("-").lower()
    return slug or "workspace"


def normalize_title(text: str, fallback: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned or not any(ch.isalnum() for ch in cleaned):
        return fallback
    if len(cleaned) > 120:
        cleaned = cleaned[:117].rstrip() + "..."
    return cleaned


def short_identifier(identifier: str) -> str:
    if re.fullmatch(r"[0-9a-f]{32,64}", identifier):
        return identifier[:12]
    return identifier


def relative_link_path(target: pathlib.Path, start_dir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(os.path.relpath(target, start_dir))


def source_map_key(path: pathlib.Path | str) -> str:
    candidate = pathlib.Path(path).expanduser()
    return str(candidate.resolve(strict=False))


def is_image_file(path: pathlib.Path) -> bool:
    return path.suffix.lower() in IMAGE_SUFFIXES


def is_video_file(path: pathlib.Path) -> bool:
    return path.suffix.lower() in VIDEO_SUFFIXES


def is_audio_file(path: pathlib.Path) -> bool:
    return path.suffix.lower() in AUDIO_SUFFIXES


def media_output_name(path: pathlib.Path, index: int) -> str:
    suffix = path.suffix.lower()
    stem = slugify(path.stem)[:80] or "media"
    return f"{index:03d}-{stem}-{sha256_file_name(path)}{suffix}"


def generated_file_signature(content: str | bytes) -> str:
    if isinstance(content, bytes):
        return hashlib.sha256(content).hexdigest()
    return sha256_text(content)


def write_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def remove_file_if_exists(path: pathlib.Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def prune_empty_dirs(base: pathlib.Path) -> None:
    if not base.exists():
        return
    for path in sorted(base.rglob("*"), reverse=True):
        if path.is_dir():
            try:
                next(path.iterdir())
            except StopIteration:
                path.rmdir()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file_name(path: pathlib.Path) -> str:
    return sha256_text(path.as_posix())[:10]


def format_iso_timestamp(value: Any) -> str | None:
    if value in (None, ""):
        return None
    candidate = str(value).strip()
    if not candidate:
        return None
    try:
        parsed = dt.datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return candidate
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone().isoformat(timespec="seconds")


def parse_iso_datetime(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{int(num_bytes)} B"


def fence(text: str, language: str = "") -> str:
    matches = re.findall(r"`+", text)
    longest = max((len(match) for match in matches), default=0)
    marker = "`" * max(3, longest + 1)
    suffix = language if language else ""
    return f"{marker}{suffix}\n{text}\n{marker}"


def choose_language(text: str) -> str:
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return "json"
    return "text"


def summarize_text(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars > 0 and len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "\n…", True
    return text, False


def ensure_part_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    return [value]


def part_is_simple_text(part: Any) -> bool:
    return isinstance(part, str) or (isinstance(part, dict) and "text" in part and len(part) == 1)


def human_size_from_base64(data: str) -> str:
    bytes_len = int(len(data) * 3 / 4) if data else 0
    return human_size(bytes_len)


def part_to_text(part: Any, *, verbose: bool = True) -> str:
    if part is None:
        return ""
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return json.dumps(part, ensure_ascii=False, indent=2)

    if part.get("videoMetadata") is not None and verbose:
        return "[Video Metadata]"
    if part.get("thought") is not None and verbose:
        return f"[Thought: {part.get('thought')}]"
    if part.get("codeExecutionResult") is not None and verbose:
        return "[Code Execution Result]"
    if part.get("executableCode") is not None and verbose:
        return "[Executable Code]"
    if "text" in part and part.get("text") is not None:
        return str(part.get("text"))
    if "fileData" in part and verbose:
        payload = part.get("fileData") or {}
        mime = payload.get("mimeType") or "unknown"
        uri = payload.get("fileUri") or payload.get("uri")
        suffix = f", {uri}" if uri else ""
        return f"[File Data: {mime}{suffix}]"
    if "functionCall" in part and verbose:
        payload = part.get("functionCall") or {}
        return f"[Function Call: {payload.get('name') or 'tool'}]"
    if "functionResponse" in part and verbose:
        payload = part.get("functionResponse") or {}
        return f"[Function Response: {payload.get('name') or 'tool'}]"
    if "inlineData" in part and verbose:
        payload = part.get("inlineData") or {}
        mime = payload.get("mimeType") or "unknown"
        category = "Media"
        if str(mime).startswith("audio/"):
            category = "Audio"
        elif str(mime).startswith("video/"):
            category = "Video"
        elif str(mime).startswith("image/"):
            category = "Image"
        return f"[{category}: {mime}, {human_size_from_base64(str(payload.get('data') or ''))}]"
    return json.dumps(part, ensure_ascii=False, indent=2)


def part_list_union_to_text(value: Any, *, verbose: bool = True) -> str:
    parts = ensure_part_list(value)
    if not parts:
        return ""
    if all(part_is_simple_text(part) for part in parts):
        return "".join(part_to_text(part, verbose=verbose) for part in parts).strip()
    rendered = [part_to_text(part, verbose=verbose).strip() for part in parts]
    return "\n\n".join(item for item in rendered if item).strip()


def load_project_registry(gemini_home: pathlib.Path, catalog: WorkspaceCatalog) -> None:
    registry_path = gemini_home / "projects.json"
    if not registry_path.exists():
        return
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    projects = payload.get("projects")
    if not isinstance(projects, dict):
        return
    for root, slug in projects.items():
        root_text = str(root).strip()
        slug_text = str(slug).strip()
        if not root_text or not slug_text:
            continue
        catalog.register(slug_text, root=root_text, aliases=[sha256_text(root_text)])


def discover_base_workspaces(gemini_home: pathlib.Path, catalog: WorkspaceCatalog) -> None:
    load_project_registry(gemini_home, catalog)

    tmp_dir = gemini_home / "tmp"
    if tmp_dir.exists():
        for path in sorted(tmp_dir.iterdir()):
            if not path.is_dir() or path.name == "bin":
                continue
            project_root_file = path / ".project_root"
            root = None
            if project_root_file.exists():
                try:
                    raw = project_root_file.read_text(encoding="utf-8").strip()
                except OSError:
                    raw = ""
                if raw:
                    root = raw
            catalog.register(path.name, root=root)

    code_tracker_active = gemini_home / "antigravity" / "code_tracker" / "active"
    if code_tracker_active.exists():
        for snapshot_dir in sorted(code_tracker_active.iterdir()):
            if not snapshot_dir.is_dir():
                continue
            prefix, _, suffix = snapshot_dir.name.partition("_")
            aliases = [snapshot_dir.name]
            if suffix:
                aliases.append(suffix)
            catalog.register(prefix, aliases=aliases)


def antigravity_app_cert_path() -> pathlib.Path | None:
    override = os.environ.get("ANTIGRAVITY_LANGUAGE_SERVER_CERT_PATH")
    if override:
        candidate = pathlib.Path(override).expanduser()
        if candidate.exists():
            return candidate
    app_root = antigravity_app_root_path()
    if app_root is None:
        return None
    candidate = app_root / "extensions" / "antigravity" / "dist" / "languageServer" / "cert.pem"
    if candidate.exists():
        return candidate
    return None


def antigravity_app_root_candidates() -> list[pathlib.Path]:
    seen: set[str] = set()
    candidates: list[pathlib.Path] = []

    def add(path: str | pathlib.Path | None) -> None:
        if not path:
            return
        candidate = pathlib.Path(path).expanduser()
        key = str(candidate)
        if key in seen:
            return
        seen.add(key)
        candidates.append(candidate)

    add(os.environ.get("ANTIGRAVITY_EDITOR_APP_ROOT"))
    add(os.environ.get("ANTIGRAVITY_APP_ROOT"))

    system = platform.system()
    home = pathlib.Path.home()
    if system == "Darwin":
        add("/Applications/Antigravity.app/Contents/Resources/app")
        add(home / "Applications" / "Antigravity.app" / "Contents" / "Resources" / "app")
    elif system == "Linux":
        add("/opt/Antigravity/resources/app")
        add("/opt/antigravity/resources/app")
        add("/usr/lib/Antigravity/resources/app")
        add("/usr/lib/antigravity/resources/app")
        add("/usr/share/antigravity/resources/app")
        add(home / ".local" / "share" / "Antigravity" / "resources" / "app")
        add(home / ".local" / "share" / "antigravity" / "resources" / "app")
    elif system == "Windows":
        local_appdata = os.environ.get("LOCALAPPDATA")
        program_files = os.environ.get("ProgramFiles")
        program_files_x86 = os.environ.get("ProgramFiles(x86)")
        add(pathlib.Path(local_appdata) / "Programs" / "Antigravity" / "resources" / "app" if local_appdata else None)
        add(pathlib.Path(local_appdata) / "Antigravity" / "resources" / "app" if local_appdata else None)
        add(pathlib.Path(program_files) / "Antigravity" / "resources" / "app" if program_files else None)
        add(pathlib.Path(program_files_x86) / "Antigravity" / "resources" / "app" if program_files_x86 else None)
    return candidates


def antigravity_app_root_path() -> pathlib.Path | None:
    for candidate in antigravity_app_root_candidates():
        if candidate.exists():
            return candidate
    return None


def antigravity_language_server_binary_path() -> pathlib.Path | None:
    override = os.environ.get("ANTIGRAVITY_LANGUAGE_SERVER_PATH")
    if override:
        candidate = pathlib.Path(override).expanduser()
        if candidate.exists():
            return candidate

    app_root = antigravity_app_root_path()
    if app_root is None:
        return None
    bin_dir = app_root / "extensions" / "antigravity" / "bin"
    if not bin_dir.exists():
        return None

    machine = platform.machine().lower()
    system = platform.system()
    preferred_names: list[str] = []
    if system == "Darwin":
        if machine in {"arm64", "aarch64"}:
            preferred_names.extend(["language_server_macos_arm", "language_server_darwin_arm64"])
        preferred_names.extend(["language_server_macos", "language_server_darwin", "language_server"])
    elif system == "Linux":
        if machine in {"arm64", "aarch64"}:
            preferred_names.extend(["language_server_linux_arm64", "language_server_linux_arm"])
        elif machine in {"x86_64", "amd64"}:
            preferred_names.extend(["language_server_linux_x64", "language_server_linux_amd64"])
        preferred_names.extend(["language_server_linux", "language_server"])
    elif system == "Windows":
        if machine in {"arm64", "aarch64"}:
            preferred_names.extend(["language_server_windows_arm64.exe"])
        elif machine in {"x86_64", "amd64"}:
            preferred_names.extend(["language_server_windows_x64.exe", "language_server_windows_amd64.exe"])
        preferred_names.extend(["language_server_windows.exe", "language_server.exe"])

    for name in preferred_names:
        candidate = bin_dir / name
        if candidate.exists():
            return candidate

    for candidate in sorted(bin_dir.iterdir()):
        if not candidate.is_file():
            continue
        if candidate.name.startswith("language_server"):
            return candidate
    return None


def parse_file_uri(uri: str) -> str | None:
    if not uri:
        return None
    if uri.startswith("file://"):
        parsed = urllib.parse.urlparse(uri)
        return urllib.parse.unquote(parsed.path) or None
    return uri


@dataclass
class LiveLanguageServerEndpoint:
    source_kind: str
    pid: int
    port: int
    csrf_token: str


def run_json_command(command: list[str]) -> str:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "command failed").strip())
    return completed.stdout


def node_connect_rpc(
    cert_path: pathlib.Path,
    *,
    port: int,
    csrf_token: str,
    method: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    env = os.environ.copy()
    env.update(
        {
            "AG_PORT": str(port),
            "AG_CSRF": csrf_token,
            "AG_METHOD": method,
            "AG_CERT_PATH": str(cert_path),
            "AG_PAYLOAD": json.dumps(payload, ensure_ascii=False),
        }
    )
    try:
        completed = subprocess.run(
            ["node", "-e", NODE_RPC_HELPER],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=20,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Timed out calling live RPC {method} on port {port}") from exc
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or f"node rpc failed: {completed.returncode}").strip())
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from live RPC {method}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected RPC payload type for {method}: {type(payload).__name__}")
    return payload


def parse_language_server_processes() -> list[tuple[int, str]]:
    output = run_json_command(["ps", "axww", "-o", "pid=,command="])
    processes: list[tuple[int, str]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or "--enable_lsp" not in line:
            continue
        if re.search(r"(^|[/\\])language_server[^/\s\\\\]*(?:\.exe)?(\s|$)", line) is None:
            continue
        match = re.match(r"^(?P<pid>\d+)\s+(?P<command>.+)$", line)
        if not match:
            continue
        processes.append((int(match.group("pid")), match.group("command")))
    return processes


def parse_listening_ports(pid: int) -> list[int]:
    completed = subprocess.run(
        ["lsof", "-Pan", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return []
    ports: list[int] = []
    for line in completed.stdout.splitlines():
        match = re.search(r":(\d+)\s+\(LISTEN\)$", line.strip())
        if match:
            ports.append(int(match.group(1)))
    return sorted(set(ports))


def extract_flag(command: str, flag: str) -> str | None:
    match = re.search(rf"{re.escape(flag)}\s+([^\s]+)", command)
    if not match:
        return None
    return match.group(1)


def discover_live_language_servers(cert_path: pathlib.Path) -> list[LiveLanguageServerEndpoint]:
    endpoints: list[LiveLanguageServerEndpoint] = []
    for pid, command in parse_language_server_processes():
        csrf_token = extract_flag(command, "--csrf_token")
        if not csrf_token:
            continue
        for port in parse_listening_ports(pid):
            try:
                response = node_connect_rpc(
                    cert_path,
                    port=port,
                    csrf_token=csrf_token,
                    method="Heartbeat",
                    payload={"metadata": {}},
                )
            except RuntimeError:
                continue
            if "lastExtensionHeartbeat" in response:
                endpoints.append(
                    LiveLanguageServerEndpoint(
                        source_kind="app",
                        pid=pid,
                        port=port,
                        csrf_token=csrf_token,
                    )
                )
                break
    return endpoints


def spawn_standalone_language_server(
    cert_path: pathlib.Path,
    gemini_home: pathlib.Path,
) -> tuple[subprocess.Popen[str], LiveLanguageServerEndpoint] | None:
    binary_path = antigravity_language_server_binary_path()
    if binary_path is None:
        return None
    csrf_token = str(uuid.uuid4())
    command = [
        str(binary_path),
        "--enable_lsp",
        "--standalone",
        "--random_port",
        "--csrf_token",
        csrf_token,
        "--app_data_dir",
        "antigravity",
        "--gemini_dir",
        str(gemini_home),
        "--cloud_code_endpoint",
        "https://daily-cloudcode-pa.googleapis.com",
    ]
    env = os.environ.copy()
    app_root = antigravity_app_root_path()
    if app_root is not None:
        env.setdefault("ANTIGRAVITY_EDITOR_APP_ROOT", str(app_root))
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    port: int | None = None
    if process.stderr is not None:
        deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=15)
        while dt.datetime.now(dt.timezone.utc) < deadline:
            if process.poll() is not None:
                break
            ready, _, _ = select.select([process.stderr], [], [], 0.5)
            if not ready:
                continue
            line = process.stderr.readline()
            if not line:
                continue
            match = re.search(r"port at (\d+) for HTTPS", line)
            if match:
                port = int(match.group(1))
                break

    if port is None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        return None

    heartbeat_deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=10)
    while dt.datetime.now(dt.timezone.utc) < heartbeat_deadline:
        try:
            response = node_connect_rpc(
                cert_path,
                port=port,
                csrf_token=csrf_token,
                method="Heartbeat",
                payload={"metadata": {}},
            )
        except RuntimeError:
            if process.poll() is not None:
                break
            continue
        if "lastExtensionHeartbeat" in response:
            return (
                process,
                LiveLanguageServerEndpoint(
                    source_kind="standalone",
                    pid=process.pid,
                    port=port,
                    csrf_token=csrf_token,
                ),
            )

    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    return None


def register_live_workspace(catalog: WorkspaceCatalog, summary: dict[str, Any]) -> WorkspaceInfo | None:
    workspaces = summary.get("workspaces")
    if not isinstance(workspaces, list):
        return None
    for workspace_payload in workspaces:
        if not isinstance(workspace_payload, dict):
            continue
        folder_uri = str(workspace_payload.get("workspaceFolderAbsoluteUri") or "").strip()
        root = parse_file_uri(folder_uri)
        if not root:
            root = parse_file_uri(str(workspace_payload.get("gitRootAbsoluteUri") or "").strip())
        if not root:
            continue
        existing = catalog.match_path(root)
        aliases = [sha256_text(root)]
        if existing is not None:
            existing.aliases.update(aliases)
            return existing
        path = pathlib.Path(root)
        return catalog.register(path.name or root, root=root, aliases=aliases)
    return None


def should_export_live_conversation(trajectory: dict[str, Any], args: argparse.Namespace) -> bool:
    if args.include_system_only:
        return True
    for step in trajectory.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if str(step.get("type") or "") == "CORTEX_STEP_TYPE_USER_INPUT":
            return True
    return False


def fetch_live_conversations_from_endpoint(
    args: argparse.Namespace,
    catalog: WorkspaceCatalog,
    cert_path: pathlib.Path,
    endpoint: LiveLanguageServerEndpoint,
) -> dict[str, LiveConversationRecord]:
    requested_session_ids = set(args.session_ids)
    requested_workspace_ids = set(args.workspace_ids)
    conversations_by_id: dict[str, LiveConversationRecord] = {}
    try:
        summaries_response = node_connect_rpc(
            cert_path,
            port=endpoint.port,
            csrf_token=endpoint.csrf_token,
            method="GetAllCascadeTrajectories",
            payload={},
        )
    except RuntimeError:
        return {}
    summaries = summaries_response.get("trajectorySummaries") or {}
    if not isinstance(summaries, dict):
        return {}

    for cascade_id, summary in summaries.items():
        if not isinstance(summary, dict):
            continue
        cascade_text = str(cascade_id).strip()
        if not cascade_text:
            continue
        if requested_session_ids and cascade_text not in requested_session_ids:
            continue
        workspace = register_live_workspace(catalog, summary)
        if workspace is None:
            continue
        if requested_workspace_ids and not requested_workspace_ids.intersection(workspace.aliases):
            continue
        try:
            trajectory_response = node_connect_rpc(
                cert_path,
                port=endpoint.port,
                csrf_token=endpoint.csrf_token,
                method="GetCascadeTrajectory",
                payload={"cascadeId": cascade_text},
            )
        except RuntimeError:
            continue
        trajectory = trajectory_response.get("trajectory") or {}
        if not isinstance(trajectory, dict):
            continue
        if not should_export_live_conversation(trajectory, args):
            continue
        record = LiveConversationRecord(
            conversation_id=cascade_text,
            trajectory_id=str(trajectory.get("trajectoryId") or summary.get("trajectoryId") or cascade_text),
            workspace=workspace,
            summary=summary,
            trajectory=trajectory,
            source_kind=endpoint.source_kind,
            source_pid=endpoint.pid,
            source_port=endpoint.port,
        )
        existing = conversations_by_id.get(cascade_text)
        if existing is None:
            conversations_by_id[cascade_text] = record
            continue
        existing_updated = parse_iso_datetime(existing.summary.get("lastModifiedTime"))
        candidate_updated = parse_iso_datetime(summary.get("lastModifiedTime"))
        if (candidate_updated or dt.datetime.min.replace(tzinfo=dt.timezone.utc)) > (
            existing_updated or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        ):
            conversations_by_id[cascade_text] = record
    return conversations_by_id


def discover_live_conversations(
    args: argparse.Namespace,
    gemini_home: pathlib.Path,
    catalog: WorkspaceCatalog,
) -> list[LiveConversationRecord]:
    cert_path = antigravity_app_cert_path()
    if cert_path is None:
        return []
    try:
        endpoints = discover_live_language_servers(cert_path)
    except (FileNotFoundError, RuntimeError):
        endpoints = []

    conversations_by_id: dict[str, LiveConversationRecord] = {}
    for endpoint in endpoints:
        for cascade_id, record in fetch_live_conversations_from_endpoint(
            args,
            catalog,
            cert_path,
            endpoint,
        ).items():
            existing = conversations_by_id.get(cascade_id)
            if existing is None:
                conversations_by_id[cascade_id] = record
                continue
            existing_updated = parse_iso_datetime(existing.summary.get("lastModifiedTime"))
            candidate_updated = parse_iso_datetime(record.summary.get("lastModifiedTime"))
            if (candidate_updated or dt.datetime.min.replace(tzinfo=dt.timezone.utc)) > (
                existing_updated or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
            ):
                conversations_by_id[cascade_id] = record

    if not conversations_by_id and not args.no_standalone_ls:
        spawned = spawn_standalone_language_server(cert_path, gemini_home)
        if spawned is not None:
            spawned_process, endpoint = spawned
            try:
                for cascade_id, record in fetch_live_conversations_from_endpoint(
                    args,
                    catalog,
                    cert_path,
                    endpoint,
                ).items():
                    conversations_by_id[cascade_id] = record
            finally:
                if spawned_process.poll() is None:
                    spawned_process.terminate()
                    try:
                        spawned_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        spawned_process.kill()
                        spawned_process.wait(timeout=5)

    conversations = sorted(
        conversations_by_id.values(),
        key=lambda item: (
            parse_iso_datetime(item.summary.get("lastModifiedTime"))
            or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            item.conversation_id,
        ),
        reverse=True,
    )
    if args.limit is not None:
        conversations = conversations[: args.limit]
    return conversations


def load_logs_by_session(logs_path: pathlib.Path) -> dict[str, list[dict[str, Any]]]:
    if not logs_path.exists():
        return {}
    try:
        payload = json.loads(logs_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, list):
        return {}

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in payload:
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("sessionId") or "").strip()
        if not session_id:
            continue
        grouped[session_id].append(item)

    for items in grouped.values():
        items.sort(
            key=lambda row: (
                parse_iso_datetime(row.get("timestamp")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                int(row.get("messageId") or 0),
            )
        )
    return grouped


def is_valid_conversation(payload: dict[str, Any]) -> bool:
    return (
        bool(str(payload.get("sessionId") or "").strip())
        and isinstance(payload.get("messages"), list)
        and bool(str(payload.get("startTime") or "").strip())
        and bool(str(payload.get("lastUpdated") or "").strip())
    )


def candidate_preference_key(candidate: SessionCandidate) -> tuple[int, int, int, str]:
    workspace = candidate.workspace
    identifier = candidate.workspace_dir.name
    has_root = 1 if workspace.root else 0
    prefers_friendly_identifier = 1 if identifier == workspace.identifier else 0
    last_updated = parse_iso_datetime(candidate.conversation.get("lastUpdated"))
    updated_ts = int(last_updated.timestamp()) if last_updated else 0
    return (updated_ts, has_root, prefers_friendly_identifier, identifier)


def merge_log_entries(candidates: list[SessionCandidate]) -> tuple[list[dict[str, Any]], list[pathlib.Path]]:
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    log_paths: set[pathlib.Path] = set()
    for candidate in candidates:
        if candidate.logs_path:
            log_paths.add(candidate.logs_path)
        for item in candidate.log_entries:
            digest = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if digest in seen:
                continue
            seen.add(digest)
            merged.append(item)
    merged.sort(
        key=lambda row: (
            parse_iso_datetime(row.get("timestamp")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            int(row.get("messageId") or 0),
        )
    )
    return merged, sorted(log_paths)


def merge_workspace_candidates(candidates: list[SessionCandidate], primary: SessionCandidate) -> WorkspaceInfo:
    workspace = primary.workspace
    for candidate in candidates:
        workspace.aliases.update(candidate.workspace.aliases)
        if candidate.workspace.root and not workspace.root:
            workspace.root = candidate.workspace.root
    return workspace


def discover_sessions(
    args: argparse.Namespace, gemini_home: pathlib.Path, catalog: WorkspaceCatalog
) -> list[SessionRecord]:
    tmp_dir = gemini_home / "tmp"
    if not tmp_dir.exists():
        raise SystemExit(f"No tmp directory found under {gemini_home}")

    candidates_by_session: dict[str, list[SessionCandidate]] = defaultdict(list)
    requested_session_ids = set(args.session_ids)
    requested_workspace_ids = set(args.workspace_ids)

    for workspace_dir in sorted(tmp_dir.iterdir()):
        if not workspace_dir.is_dir() or workspace_dir.name == "bin":
            continue
        chats_dir = workspace_dir / "chats"
        if not chats_dir.is_dir():
            continue

        root = None
        project_root_file = workspace_dir / ".project_root"
        if project_root_file.exists():
            try:
                root = project_root_file.read_text(encoding="utf-8").strip() or None
            except OSError:
                root = None
        aliases = []
        if root:
            aliases.append(sha256_text(root))
        workspace = catalog.register(workspace_dir.name, root=root, aliases=aliases)
        if requested_workspace_ids and not requested_workspace_ids.intersection(workspace.aliases):
            continue

        logs_path = workspace_dir / "logs.json"
        logs_by_session = load_logs_by_session(logs_path)
        for session_file in sorted(chats_dir.glob("session-*.json")):
            try:
                conversation = json.loads(session_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(conversation, dict) or not is_valid_conversation(conversation):
                continue
            session_id = str(conversation.get("sessionId") or "").strip()
            if requested_session_ids and session_id not in requested_session_ids:
                continue
            candidates_by_session[session_id].append(
                SessionCandidate(
                    session_id=session_id,
                    session_file=session_file,
                    workspace_dir=workspace_dir,
                    workspace=workspace,
                    conversation=conversation,
                    logs_path=logs_path if logs_path.exists() else None,
                    log_entries=logs_by_session.get(session_id, []),
                )
            )

    sessions: list[SessionRecord] = []
    for session_id, candidates in candidates_by_session.items():
        primary = max(candidates, key=candidate_preference_key)
        workspace = merge_workspace_candidates(candidates, primary)
        log_entries, log_paths = merge_log_entries(candidates)
        sessions.append(
            SessionRecord(
                session_id=session_id,
                session_file=primary.session_file,
                workspace_dir=primary.workspace_dir,
                workspace=workspace,
                conversation=primary.conversation,
                log_entries=log_entries,
                log_paths=log_paths,
                source_paths=sorted({item.session_file for item in candidates}),
            )
        )

    sessions.sort(
        key=lambda item: (
            parse_iso_datetime(item.conversation.get("lastUpdated"))
            or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            item.session_id,
        ),
        reverse=True,
    )
    if args.limit is not None:
        sessions = sessions[: args.limit]
    return sessions


def has_meaningful_session_content(session: SessionRecord) -> bool:
    for message in session.conversation.get("messages") or []:
        if isinstance(message, dict) and message.get("type") in {"user", "gemini"}:
            return True
    for entry in session.log_entries:
        if str(entry.get("type") or "") == "user" and str(entry.get("message") or "").strip():
            return True
    return False


def should_export_session(session: SessionRecord, args: argparse.Namespace) -> bool:
    if not args.include_subagents and session.conversation.get("kind") == "subagent":
        return False
    if not args.include_system_only and not has_meaningful_session_content(session):
        return False
    return True


def extract_first_user_message(session: SessionRecord) -> str:
    for message in session.conversation.get("messages") or []:
        if not isinstance(message, dict) or message.get("type") != "user":
            continue
        content = part_list_union_to_text(message.get("content"), verbose=True).strip()
        if content and not content.startswith("/") and not content.startswith("?"):
            return content
    for message in session.conversation.get("messages") or []:
        if not isinstance(message, dict) or message.get("type") != "user":
            continue
        content = part_list_union_to_text(message.get("content"), verbose=True).strip()
        if content:
            return content
    for entry in session.log_entries:
        if str(entry.get("type") or "") == "user":
            content = str(entry.get("message") or "").strip()
            if content:
                return content
    return "Empty conversation"


def choose_session_title(session: SessionRecord) -> str:
    summary = str(session.conversation.get("summary") or "").strip()
    fallback = session.session_id
    if summary:
        return normalize_title(summary, fallback)
    return normalize_title(extract_first_user_message(session), fallback)


def normalize_compare_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def is_duplicate_log_user(
    text: str,
    timestamp: dt.datetime | None,
    recorded_users: list[tuple[str, dt.datetime | None]],
) -> bool:
    normalized = normalize_compare_text(text)
    if not normalized:
        return True
    for recorded_text, recorded_timestamp in recorded_users:
        if normalized != recorded_text:
            continue
        if timestamp is None or recorded_timestamp is None:
            return True
        if abs((timestamp - recorded_timestamp).total_seconds()) <= 5:
            return True
    return False


def render_tool_args(args_payload: Any) -> str:
    return json.dumps(args_payload, ensure_ascii=False, indent=2)


def render_function_response(part: dict[str, Any]) -> str:
    response = part.get("functionResponse") or {}
    name = str(response.get("name") or "tool")
    call_id = str(response.get("id") or "").strip()
    body = response.get("response")
    payload = body if body is not None else response
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    header = f"Function Response `{name}`"
    if call_id:
        header += f" ({call_id})"
    return f"{header}\n\n{text}"


def render_tool_result(result: Any, max_chars: int) -> tuple[str, bool]:
    if result in (None, ""):
        return "(no result recorded)", False
    if isinstance(result, str):
        return summarize_text(result.rstrip(), max_chars)
    if isinstance(result, list):
        parts: list[str] = []
        for part in result:
            if isinstance(part, dict) and "functionResponse" in part:
                parts.append(render_function_response(part))
            else:
                parts.append(part_to_text(part, verbose=True))
        joined = "\n\n".join(item.strip() for item in parts if str(item).strip()).strip()
        if not joined:
            joined = json.dumps(result, ensure_ascii=False, indent=2)
        return summarize_text(joined, max_chars)
    if isinstance(result, dict) and "functionResponse" in result:
        return summarize_text(render_function_response(result), max_chars)
    return summarize_text(json.dumps(result, ensure_ascii=False, indent=2), max_chars)


def build_transcript_events(session: SessionRecord) -> list[TranscriptEvent]:
    events: list[TranscriptEvent] = []
    recorded_users: list[tuple[str, dt.datetime | None]] = []

    for index, message in enumerate(session.conversation.get("messages") or []):
        if not isinstance(message, dict):
            continue
        message_type = str(message.get("type") or "")
        timestamp = format_iso_timestamp(message.get("timestamp"))
        sort_timestamp = parse_iso_datetime(message.get("timestamp"))
        body = part_list_union_to_text(
            message.get("displayContent") if message.get("displayContent") is not None else message.get("content"),
            verbose=True,
        )
        if message_type == "user":
            recorded_users.append((normalize_compare_text(body), sort_timestamp))
            label = "User"
        elif message_type == "gemini":
            label = "Assistant"
        elif message_type == "info":
            label = "Info"
        elif message_type == "warning":
            label = "Warning"
        elif message_type == "error":
            label = "Error"
        else:
            label = message_type or "Message"
        events.append(
            TranscriptEvent(
                timestamp=timestamp,
                sort_timestamp=sort_timestamp,
                order_key=(0, index),
                kind=message_type,
                label=label,
                body=body,
                metadata=message,
            )
        )

    log_index = 0
    for entry in session.log_entries:
        if str(entry.get("type") or "") != "user":
            continue
        text = str(entry.get("message") or "").strip()
        if not text:
            continue
        sort_timestamp = parse_iso_datetime(entry.get("timestamp"))
        if is_duplicate_log_user(text, sort_timestamp, recorded_users):
            continue
        label = "User Command (log)" if text.startswith(("/", "?", ":")) else "User (log)"
        events.append(
            TranscriptEvent(
                timestamp=format_iso_timestamp(entry.get("timestamp")),
                sort_timestamp=sort_timestamp,
                order_key=(1, log_index),
                kind="log_user",
                label=label,
                body=text,
                metadata=entry,
            )
        )
        log_index += 1

    events.sort(
        key=lambda event: (
            event.sort_timestamp or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            event.order_key,
        )
    )
    return events


def render_tool_calls(tool_calls: list[dict[str, Any]], max_chars: int) -> str:
    lines = ["### Tool Calls", ""]
    for tool in tool_calls:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("displayName") or tool.get("name") or "tool")
        raw_name = str(tool.get("name") or name)
        call_id = str(tool.get("id") or "").strip()
        status = str(tool.get("status") or "unknown")
        timestamp = format_iso_timestamp(tool.get("timestamp")) or "-"
        lines.extend(
            [
                f"#### {name}",
                "",
                f"- Name: `{raw_name}`",
                f"- Status: `{status}`",
            ]
        )
        if call_id:
            lines.append(f"- Call ID: `{call_id}`")
        lines.append(f"- Timestamp: `{timestamp}`")
        description = str(tool.get("description") or "").strip()
        if description:
            lines.append(f"- Description: {description}")
        lines.extend(["", "Arguments:", fence(render_tool_args(tool.get("args") or {}), "json")])
        if tool.get("result") not in (None, ""):
            result_text, truncated = render_tool_result(tool.get("result"), max_chars)
            summary = "Result"
            if truncated:
                summary += f" (truncated to {max_chars} chars)"
            lines.extend(
                [
                    "",
                    f"<details><summary>{summary}</summary>\n\n{fence(result_text, choose_language(result_text))}\n\n</details>",
                ]
            )
        if tool.get("resultDisplay") not in (None, "", []):
            lines.extend(
                [
                    "",
                    "<details><summary>Result Display</summary>\n\n"
                    + fence(json.dumps(tool.get("resultDisplay"), ensure_ascii=False, indent=2), "json")
                    + "\n\n</details>",
                ]
            )
        lines.append("")
    return "\n".join(lines).rstrip()


def render_tokens(tokens: dict[str, Any]) -> str:
    return "\n".join(
        [
            "### Tokens",
            "",
            f"- Input: `{tokens.get('input', 0)}`",
            f"- Output: `{tokens.get('output', 0)}`",
            f"- Cached: `{tokens.get('cached', 0)}`",
            f"- Thoughts: `{tokens.get('thoughts', 0)}`",
            f"- Tool: `{tokens.get('tool', 0)}`",
            f"- Total: `{tokens.get('total', 0)}`",
        ]
    )


def render_thoughts(thoughts: list[dict[str, Any]]) -> str:
    lines = ["### Thinking", ""]
    for thought in thoughts:
        if not isinstance(thought, dict):
            continue
        subject = str(thought.get("subject") or "").strip()
        description = str(thought.get("description") or "").strip()
        timestamp = format_iso_timestamp(thought.get("timestamp"))
        prefix = f"- `{timestamp}` " if timestamp else "- "
        if subject and description:
            lines.append(f"{prefix}**{subject}** {description}")
        elif subject:
            lines.append(f"{prefix}**{subject}**")
        elif description:
            lines.append(f"{prefix}{description}")
    return "\n".join(lines)


def render_event_markdown(event: TranscriptEvent, max_tool_output_chars: int) -> str:
    heading = event.label
    if event.timestamp:
        heading = f"{heading} [{event.timestamp}]"

    if event.kind != "gemini":
        body = event.body if event.body else "(empty)"
        if event.kind in {"info", "warning", "error"} and body.lstrip().startswith("{"):
            body = fence(body, "json")
        return f"## {heading}\n\n{body}\n"

    sections: list[str] = [f"## {heading}", ""]
    model = str(event.metadata.get("model") or "").strip()
    if model:
        sections.extend([f"- Model: `{model}`", ""])
    thoughts = event.metadata.get("thoughts")
    if isinstance(thoughts, list) and thoughts:
        sections.extend([render_thoughts(thoughts), ""])
    body = event.body.strip()
    if body:
        sections.extend([body, ""])
    tool_calls = event.metadata.get("toolCalls")
    if isinstance(tool_calls, list) and tool_calls:
        sections.extend([render_tool_calls(tool_calls, max_tool_output_chars), ""])
    tokens = event.metadata.get("tokens")
    if isinstance(tokens, dict) and tokens:
        sections.extend([render_tokens(tokens), ""])
    return "\n".join(item.rstrip() for item in sections).strip() + "\n"


def render_session_markdown(session: SessionRecord, title: str, args: argparse.Namespace) -> str:
    transcript_events = build_transcript_events(session)
    transcript = "\n".join(
        render_event_markdown(event, args.max_tool_output_chars).rstrip()
        for event in transcript_events
    ).strip()
    if not transcript:
        transcript = "_No readable messages found._"

    metadata_lines = [
        f"- Workspace: `{session.workspace.name}`",
        f"- Workspace Identifier: `{session.workspace.identifier}`",
        f"- Conversation ID: `{session.session_id}`",
        f"- Started: `{format_iso_timestamp(session.conversation.get('startTime')) or '-'}`",
        f"- Updated: `{format_iso_timestamp(session.conversation.get('lastUpdated')) or '-'}`",
        f"- Kind: `{session.conversation.get('kind') or 'main'}`",
        f"- Source Conversation File: `{session.session_file}`",
    ]
    if session.workspace.root:
        metadata_lines.append(f"- Workspace Root: `{session.workspace.root}`")
    if session.workspace.aliases:
        metadata_lines.append(
            "- Workspace Aliases: " + ", ".join(f"`{alias}`" for alias in sorted(session.workspace.aliases))
        )
    if session.log_paths:
        metadata_lines.append("- Log Sources: " + ", ".join(f"`{path}`" for path in session.log_paths))
    if len(session.source_paths) > 1:
        metadata_lines.append(
            "- Duplicate Conversation Files Collapsed: "
            + ", ".join(f"`{path}`" for path in session.source_paths)
        )
    message_counts = Counter(
        str(message.get("type") or "unknown")
        for message in session.conversation.get("messages") or []
        if isinstance(message, dict)
    )
    if message_counts:
        counts_text = ", ".join(f"{key}={value}" for key, value in sorted(message_counts.items()))
        metadata_lines.append(f"- Recorded Message Types: `{counts_text}`")
    if session.log_entries:
        metadata_lines.append(f"- Matching Log Entries: `{len(session.log_entries)}`")

    parts = [
        f"# {title}",
        "",
        "## Metadata",
        "",
        *metadata_lines,
        "",
    ]
    summary_text = str(session.conversation.get("summary") or "").strip()
    if summary_text:
        parts.extend(["## Summary", "", summary_text, ""])
    directories = session.conversation.get("directories") or []
    if isinstance(directories, list) and directories:
        parts.extend(["## Directories", "", *[f"- `{item}`" for item in directories], ""])
    parts.extend(["## Transcript", "", transcript, ""])
    return "\n".join(parts)


def choose_live_conversation_title(conversation: LiveConversationRecord) -> str:
    summary = str(conversation.summary.get("summary") or "").strip()
    if summary:
        return normalize_title(summary, conversation.conversation_id)
    for step in conversation.trajectory.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if str(step.get("type") or "") != "CORTEX_STEP_TYPE_USER_INPUT":
            continue
        user_input = step.get("userInput") or {}
        if not isinstance(user_input, dict):
            continue
        content = str(user_input.get("userResponse") or "").strip()
        if content:
            return normalize_title(content, conversation.conversation_id)
    return normalize_title(conversation.conversation_id, conversation.conversation_id)


def live_step_type_name(step_type: str) -> str:
    cleaned = step_type.replace("CORTEX_STEP_TYPE_", "").strip().lower()
    return cleaned.replace("_", " ").title() or "Step"


def live_step_timestamp(step: dict[str, Any]) -> str | None:
    metadata = step.get("metadata") or {}
    if not isinstance(metadata, dict):
        return None
    for key in ("createdAt", "viewableAt", "completedAt", "lastCompletedChunkAt", "finishedGeneratingAt"):
        value = format_iso_timestamp(metadata.get(key))
        if value:
            return value
    return None


def live_step_payload_key(step: dict[str, Any]) -> str | None:
    for key in step:
        if key not in {"type", "status", "metadata"}:
            return key
    return None


def render_live_user_input(step: dict[str, Any]) -> str:
    payload = step.get("userInput") or {}
    if not isinstance(payload, dict):
        return "(empty)"
    text = str(payload.get("userResponse") or "").strip()
    if text:
        return text
    return part_list_union_to_text(payload.get("items"), verbose=True) or "(empty)"


def render_live_planner_response(step: dict[str, Any], max_tool_output_chars: int) -> str:
    payload = step.get("plannerResponse") or {}
    if not isinstance(payload, dict):
        return "(empty)"
    response_text = (
        str(payload.get("modifiedResponse") or "").strip()
        or str(payload.get("response") or "").strip()
        or "(empty)"
    )
    parts = [response_text]
    tool_calls = payload.get("toolCalls")
    if isinstance(tool_calls, list) and tool_calls:
        normalized_calls: list[dict[str, Any]] = []
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            try:
                args_payload = json.loads(str(tool_call.get("argumentsJson") or "{}"))
            except json.JSONDecodeError:
                args_payload = str(tool_call.get("argumentsJson") or "{}")
            normalized_calls.append(
                {
                    "id": tool_call.get("id"),
                    "name": tool_call.get("name"),
                    "displayName": tool_call.get("name"),
                    "status": "planned",
                    "timestamp": step.get("metadata", {}).get("createdAt"),
                    "args": args_payload,
                }
            )
        if normalized_calls:
            parts.extend(["", render_tool_calls(normalized_calls, max_tool_output_chars)])
    thinking = str(payload.get("thinking") or "").strip()
    if thinking:
        parts.extend(["", "<details><summary>Thinking</summary>", "", thinking, "", "</details>"])
    return "\n".join(parts).strip()


def render_live_notify_user(step: dict[str, Any]) -> str:
    payload = step.get("notifyUser") or {}
    if not isinstance(payload, dict):
        return "(empty)"
    text = str(payload.get("notificationContent") or "").strip() or "(empty)"
    lines = [text]
    review_uris = payload.get("reviewAbsoluteUris")
    if isinstance(review_uris, list) and review_uris:
        lines.extend(["", "Review Files:"])
        for uri in review_uris:
            if uri:
                lines.append(f"- `{parse_file_uri(str(uri)) or uri}`")
    return "\n".join(lines).strip()


def render_live_task_boundary(step: dict[str, Any]) -> str:
    payload = step.get("taskBoundary") or {}
    if not isinstance(payload, dict):
        return "(empty)"
    lines = []
    task_name = str(payload.get("taskName") or "").strip()
    task_status = str(payload.get("taskStatus") or "").strip()
    if task_name:
        lines.append(f"- Task: `{task_name}`")
    if task_status:
        lines.append(f"- Status: `{task_status}`")
    summary = str(payload.get("taskSummaryWithCitations") or payload.get("taskSummary") or "").strip()
    if summary:
        lines.extend(["", summary])
    return "\n".join(lines).strip() or "(empty)"


def render_live_checkpoint(step: dict[str, Any]) -> str:
    payload = step.get("checkpoint") or {}
    if not isinstance(payload, dict):
        return "(empty)"
    lines: list[str] = []
    intent = str(payload.get("userIntent") or "").strip()
    if intent:
        lines.extend(["Intent:", "", intent, ""])
    requests = payload.get("userRequests")
    if isinstance(requests, list) and requests:
        lines.append("Requests:")
        for request in requests:
            lines.append(f"- {str(request).strip()}")
    return "\n".join(lines).strip() or "(empty)"


def render_live_generic_payload(step: dict[str, Any], max_chars: int) -> str:
    payload_key = live_step_payload_key(step)
    if not payload_key:
        return ""
    payload = step.get(payload_key)
    if payload_key == "conversationHistory":
        payload = {"content": str((payload or {}).get("content") or "").strip()}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    summarized, truncated = summarize_text(text, max_chars)
    language = choose_language(summarized)
    heading = "Payload"
    if truncated:
        heading += f" (truncated to {max_chars} chars)"
    return "\n".join([f"<details><summary>{heading}</summary>", "", fence(summarized, language), "", "</details>"])


def render_live_transcript_step(step: dict[str, Any], max_tool_output_chars: int) -> str | None:
    step_type = str(step.get("type") or "")
    timestamp = live_step_timestamp(step)
    if step_type == "CORTEX_STEP_TYPE_USER_INPUT":
        heading = "## User"
        if timestamp:
            heading += f" [{timestamp}]"
        return f"{heading}\n\n{render_live_user_input(step)}\n"
    if step_type == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
        heading = "## Assistant"
        if timestamp:
            heading += f" [{timestamp}]"
        return f"{heading}\n\n{render_live_planner_response(step, max_tool_output_chars)}\n"
    if step_type == "CORTEX_STEP_TYPE_NOTIFY_USER":
        heading = "## Assistant"
        if timestamp:
            heading += f" [{timestamp}]"
        return f"{heading}\n\n{render_live_notify_user(step)}\n"
    return None


def render_live_trace_step(step: dict[str, Any], max_tool_output_chars: int) -> str | None:
    step_type = str(step.get("type") or "")
    if step_type in LIVE_STEP_SKIP_TYPES:
        return None
    timestamp = live_step_timestamp(step)
    heading = live_step_type_name(step_type)
    if timestamp:
        heading += f" [{timestamp}]"

    payload_key = live_step_payload_key(step)
    if step_type in LIVE_TRANSCRIPT_STEP_TYPES:
        if step_type == "CORTEX_STEP_TYPE_USER_INPUT":
            body = render_live_user_input(step)
        elif step_type == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
            body = render_live_planner_response(step, max_tool_output_chars)
        else:
            body = render_live_notify_user(step)
    elif step_type == "CORTEX_STEP_TYPE_TASK_BOUNDARY":
        body = render_live_task_boundary(step)
    elif step_type == "CORTEX_STEP_TYPE_CHECKPOINT":
        body = render_live_checkpoint(step)
    else:
        body = render_live_generic_payload(step, max_tool_output_chars)
    if not body:
        return None
    return f"<details><summary>{heading}</summary>\n\n{body}\n\n</details>"


def extract_paths_from_text(text: str) -> list[str]:
    found: list[str] = []
    for match in PATH_REF_RE.finditer(text):
        raw = match.group("file_uri") or match.group("abs") or ""
        if not raw:
            continue
        found.append(raw.replace("%20", " "))
    return found


def is_text_artifact(path: pathlib.Path) -> bool:
    if path.suffix.lower() in TEXT_EXPORTABLE_SUFFIXES:
        return True
    if ".resolved" in path.name:
        return True
    return False


def infer_workspace_for_bundle(
    brain_dir: pathlib.Path | None,
    catalog: WorkspaceCatalog,
) -> tuple[WorkspaceInfo | None, list[str], Counter[str]]:
    if brain_dir is None or not brain_dir.exists():
        return None, [], Counter()
    extracted_paths: list[str] = []
    counts: Counter[str] = Counter()
    for path in sorted(brain_dir.rglob("*")):
        if not path.is_file() or not is_text_artifact(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for path_text in extract_paths_from_text(text):
            extracted_paths.append(path_text)
            workspace = catalog.match_path(path_text)
            if workspace is not None:
                counts[workspace.identifier] += 1
    if counts:
        best_identifier, _ = counts.most_common(1)[0]
        return catalog.get_by_alias(best_identifier), extracted_paths, counts
    return None, extracted_paths, counts


def collect_artifact_files(paths: list[pathlib.Path]) -> tuple[list[pathlib.Path], list[pathlib.Path]]:
    text_files: list[pathlib.Path] = []
    media_files: list[pathlib.Path] = []
    for path in sorted(paths):
        if not path.is_file():
            continue
        if is_text_artifact(path):
            text_files.append(path)
        else:
            media_files.append(path)
    return text_files, media_files


def discover_artifact_bundles(
    gemini_home: pathlib.Path,
    catalog: WorkspaceCatalog,
    requested_workspace_ids: set[str],
) -> list[ArtifactBundle]:
    antigravity_dir = gemini_home / "antigravity"
    brain_root = antigravity_dir / "brain"
    annotations_root = antigravity_dir / "annotations"
    browser_root = antigravity_dir / "browser_recordings"
    bundles: list[ArtifactBundle] = []
    if not brain_root.exists():
        return bundles

    for brain_dir in sorted(brain_root.iterdir()):
        if not brain_dir.is_dir():
            continue
        bundle_id = brain_dir.name
        workspace, extracted_paths, evidence_counts = infer_workspace_for_bundle(brain_dir, catalog)
        if workspace is None:
            continue
        if requested_workspace_ids and not requested_workspace_ids.intersection(workspace.aliases):
            continue
        annotation_path = annotations_root / f"{bundle_id}.pbtxt"
        if not annotation_path.exists():
            annotation_path = None
        browser_recording_dir = browser_root / bundle_id
        if not browser_recording_dir.exists():
            browser_recording_dir = None

        source_paths: list[pathlib.Path] = []
        if brain_dir.exists():
            source_paths.append(brain_dir)
        if annotation_path:
            source_paths.append(annotation_path)
        if browser_recording_dir:
            source_paths.append(browser_recording_dir)

        candidate_files: list[pathlib.Path] = []
        candidate_files.extend(path for path in brain_dir.rglob("*") if path.is_file())
        if annotation_path:
            candidate_files.append(annotation_path)
        if browser_recording_dir:
            candidate_files.extend(path for path in browser_recording_dir.rglob("*") if path.is_file())
        text_files, media_files = collect_artifact_files(candidate_files)

        bundles.append(
            ArtifactBundle(
                bundle_id=bundle_id,
                workspace=workspace,
                brain_dir=brain_dir,
                annotation_path=annotation_path,
                browser_recording_dir=browser_recording_dir,
                source_paths=source_paths,
                text_files=text_files,
                media_files=media_files,
                extracted_paths=sorted(set(extracted_paths)),
                evidence_counts=evidence_counts,
            )
        )
    return bundles


def discover_code_tracker_snapshots(
    gemini_home: pathlib.Path,
    catalog: WorkspaceCatalog,
    requested_workspace_ids: set[str],
) -> list[CodeTrackerSnapshot]:
    active_root = gemini_home / "antigravity" / "code_tracker" / "active"
    snapshots: list[CodeTrackerSnapshot] = []
    if not active_root.exists():
        return snapshots

    for snapshot_dir in sorted(active_root.iterdir()):
        if not snapshot_dir.is_dir():
            continue
        prefix, _, suffix = snapshot_dir.name.partition("_")
        aliases = [snapshot_dir.name]
        if suffix:
            aliases.append(suffix)
        workspace = catalog.register(prefix, aliases=aliases)
        if requested_workspace_ids and not requested_workspace_ids.intersection(workspace.aliases):
            continue
        files = sorted(path for path in snapshot_dir.iterdir() if path.is_file())
        snapshots.append(
            CodeTrackerSnapshot(
                snapshot_id=snapshot_dir.name,
                workspace=workspace,
                snapshot_dir=snapshot_dir,
                files=files,
            )
        )
    return snapshots


def sanitize_output_basename(path: pathlib.Path, index: int) -> str:
    stem = slugify(path.name)
    return f"{index:03d}-{stem}-{sha256_file_name(path)}.md"


def rewrite_export_links(
    text: str,
    *,
    page_path: pathlib.Path,
    source_output_map: dict[str, pathlib.Path],
) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group("file_uri") or match.group("abs") or ""
        if not raw:
            return match.group(0)
        source_key = source_map_key(raw.replace("%20", " "))
        target = source_output_map.get(source_key)
        if target is None:
            return match.group(0)
        rel = relative_link_path(target, page_path.parent).as_posix()
        label = pathlib.Path(raw).name or raw
        return f"[{label}]({rel})"

    return PATH_REF_RE.sub(replace, text)


def extract_export_links(
    text: str,
    *,
    page_path: pathlib.Path,
    source_output_map: dict[str, pathlib.Path],
) -> list[tuple[str, pathlib.Path]]:
    links: list[tuple[str, pathlib.Path]] = []
    seen: set[str] = set()
    for raw in extract_paths_from_text(text):
        key = source_map_key(raw)
        target = source_output_map.get(key)
        if target is None:
            continue
        digest = f"{raw}->{target}"
        if digest in seen:
            continue
        seen.add(digest)
        links.append((pathlib.Path(raw).name or raw, relative_link_path(target, page_path.parent)))
    return links


def render_text_file_page(
    title: str,
    source_path: pathlib.Path,
    body: str,
    *,
    page_path: pathlib.Path,
    source_output_map: dict[str, pathlib.Path],
) -> str:
    rewritten_body = rewrite_export_links(body, page_path=page_path, source_output_map=source_output_map)
    export_links = extract_export_links(body, page_path=page_path, source_output_map=source_output_map)
    language = choose_language(body)
    if source_path.suffix.lower() == ".md" and ".metadata." not in source_path.name and ".resolved" not in source_path.name:
        rendered = rewritten_body.rstrip() if rewritten_body.strip() else "_Empty file._"
    else:
        rendered = fence(rewritten_body if rewritten_body else "(empty)", language)
    parts = [
        f"# {title}",
        "",
        "## Metadata",
        "",
        f"- Source: `{source_path}`",
        f"- Size: `{human_size(source_path.stat().st_size)}`",
        "",
    ]
    if export_links:
        parts.extend(["## Related Export Links", ""])
        for label, rel_path in export_links:
            parts.append(f"- [{label}]({rel_path.as_posix()})")
        parts.extend(["", "## Content", ""])
    else:
        parts.extend(["## Content", ""])
    parts.extend([rendered, ""])
    return "\n".join(parts)


def render_media_inventory(media_assets: list[tuple[pathlib.Path, pathlib.Path]]) -> list[str]:
    if not media_assets:
        return ["_No media files._"]
    lines: list[str] = []
    for source_path, output_path in media_assets:
        rel = output_path.name
        lines.append(
            f"- [{rel}]({rel}) | `{human_size(source_path.stat().st_size)}` | source `{source_path}`"
        )
        if is_image_file(source_path):
            lines.extend(["", f"![{source_path.name}]({rel})", ""])
        elif is_video_file(source_path):
            mime, _ = mimetypes.guess_type(source_path.name)
            mime = mime or "video/mp4"
            lines.extend(
                [
                    "",
                    f"<video controls src=\"{rel}\" preload=\"metadata\"></video>",
                    "",
                ]
            )
        elif is_audio_file(source_path):
            lines.extend(
                [
                    "",
                    f"<audio controls src=\"{rel}\"></audio>",
                    "",
                ]
            )
    return lines


def render_artifact_bundle_index(bundle: ArtifactBundle, text_page_links: list[tuple[str, pathlib.Path]], media_page_path: pathlib.Path | None) -> str:
    metadata_lines = [
        f"- Workspace: `{bundle.workspace.name}`",
        f"- Workspace Identifier: `{bundle.workspace.identifier}`",
        f"- Bundle ID: `{bundle.bundle_id}`",
    ]
    if bundle.workspace.root:
        metadata_lines.append(f"- Workspace Root: `{bundle.workspace.root}`")
    if bundle.brain_dir:
        metadata_lines.append(f"- Brain Directory: `{bundle.brain_dir}`")
    if bundle.annotation_path:
        metadata_lines.append(f"- Annotation: `{bundle.annotation_path}`")
    if bundle.browser_recording_dir:
        metadata_lines.append(f"- Browser Recording Directory: `{bundle.browser_recording_dir}`")
    if bundle.evidence_counts:
        evidence_text = ", ".join(f"{key}={value}" for key, value in bundle.evidence_counts.most_common())
        metadata_lines.append(f"- Workspace Evidence: `{evidence_text}`")
    metadata_lines.append(f"- Text Files Exported: `{len(text_page_links)}`")
    metadata_lines.append(f"- Media Files Inventoried: `{len(bundle.media_files)}`")

    summary_sections = ["## Files", ""]
    for title, rel_path in text_page_links:
        summary_sections.append(f"- [{title}]({rel_path.as_posix()})")
    if media_page_path is not None:
        summary_sections.append(f"- [Media Inventory]({media_page_path.as_posix()})")
    if bundle.extracted_paths:
        summary_sections.extend(["", "## Path Evidence", ""])
        for item in bundle.extracted_paths[:200]:
            summary_sections.append(f"- `{item}`")
        if len(bundle.extracted_paths) > 200:
            summary_sections.append(f"- ... `{len(bundle.extracted_paths) - 200}` more path references")

    return "\n".join(
        [
            f"# Artifact Bundle {bundle.bundle_id}",
            "",
            "## Metadata",
            "",
            *metadata_lines,
            "",
            *summary_sections,
            "",
        ]
    )


def load_json_if_possible(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        return payload
    return None


def artifact_metadata_summaries(bundle: ArtifactBundle) -> list[tuple[str, str, pathlib.Path]]:
    summaries: list[tuple[str, str, pathlib.Path]] = []
    for path in sorted(bundle.text_files):
        if not path.name.endswith(".metadata.json"):
            continue
        payload = load_json_if_possible(path)
        if not payload:
            continue
        artifact_type = str(payload.get("artifactType") or "ARTIFACT_TYPE_OTHER")
        summary = str(payload.get("summary") or "").strip()
        if summary:
            summaries.append((artifact_type, summary, path))
    return summaries


def artifact_type_rank(artifact_type: str) -> int:
    order = {
        "ARTIFACT_TYPE_WALKTHROUGH": 0,
        "ARTIFACT_TYPE_TASK": 1,
        "ARTIFACT_TYPE_IMPLEMENTATION_PLAN": 2,
        "ARTIFACT_TYPE_OTHER": 3,
    }
    return order.get(artifact_type, 99)


def load_bundle_text(bundle: ArtifactBundle, file_name: str) -> str:
    for path in bundle.text_files:
        if path.name != file_name:
            continue
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""
    return ""


def markdown_lead_paragraph(text: str) -> str:
    paragraphs: list[str] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                paragraph = " ".join(current).strip()
                if paragraph:
                    paragraphs.append(paragraph)
                current = []
            continue
        if line.startswith("#"):
            continue
        if line.startswith("- [") or line.startswith("- ") or line.startswith("* ") or re.match(r"\d+\.\s", line):
            if current:
                paragraph = " ".join(current).strip()
                if paragraph:
                    paragraphs.append(paragraph)
                current = []
            continue
        current.append(line)
    if current:
        paragraph = " ".join(current).strip()
        if paragraph:
            paragraphs.append(paragraph)
    return paragraphs[0] if paragraphs else ""


def bundle_request_summary(bundle: ArtifactBundle) -> str:
    implementation_text = load_bundle_text(bundle, "implementation_plan.md")
    lead = markdown_lead_paragraph(implementation_text)
    if lead:
        return lead
    for artifact_type, summary, _ in sorted(
        artifact_metadata_summaries(bundle),
        key=lambda item: (artifact_type_rank(item[0]), item[2].name.lower()),
    ):
        if artifact_type == "ARTIFACT_TYPE_IMPLEMENTATION_PLAN":
            return summary
    if implementation_text:
        return normalize_title(implementation_text[:400], bundle.bundle_id)
    return "No explicit user request could be reconstructed from the stored artifacts."


def bundle_output_summary(bundle: ArtifactBundle) -> str:
    walkthrough_text = load_bundle_text(bundle, "walkthrough.md")
    lead = markdown_lead_paragraph(walkthrough_text)
    if lead:
        return lead
    for artifact_type, summary, _ in sorted(
        artifact_metadata_summaries(bundle),
        key=lambda item: (artifact_type_rank(item[0]), item[2].name.lower()),
    ):
        if artifact_type in {"ARTIFACT_TYPE_WALKTHROUGH", "ARTIFACT_TYPE_TASK"}:
            return summary
    task_text = load_bundle_text(bundle, "task.md")
    lead = markdown_lead_paragraph(task_text)
    if lead:
        return lead
    return "No explicit assistant output summary could be reconstructed from the stored artifacts."


def task_checkbox_summary(bundle: ArtifactBundle) -> str | None:
    task_text = load_bundle_text(bundle, "task.md")
    if not task_text:
        return None
    completed = len(re.findall(r"(?m)^[ \t]*[-*][ \t]+\[x\]", task_text))
    pending = len(re.findall(r"(?m)^[ \t]*[-*][ \t]+\[ \]", task_text))
    if completed == 0 and pending == 0:
        return None
    return f"Completed `{completed}` / Pending `{pending}`"


def bundle_title(bundle: ArtifactBundle) -> str:
    summaries = sorted(
        artifact_metadata_summaries(bundle),
        key=lambda item: (artifact_type_rank(item[0]), len(item[1])),
    )
    if summaries:
        return normalize_title(summaries[0][1], bundle.bundle_id)

    preferred_names = ("walkthrough.md", "task.md", "implementation_plan.md")
    for name in preferred_names:
        for path in bundle.text_files:
            if path.name != name:
                continue
            try:
                body = path.read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                body = ""
            if not body:
                continue
            first_line = next((line.strip("# ").strip() for line in body.splitlines() if line.strip()), "")
            if first_line:
                return normalize_title(first_line, bundle.bundle_id)

    return f"Artifact Conversation {bundle.bundle_id}"


def bundle_timestamp(bundle: ArtifactBundle) -> str | None:
    candidates = [path for path in bundle.text_files + bundle.media_files + bundle.source_paths if path.exists()]
    if not candidates:
        return None
    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    return dt.datetime.fromtimestamp(latest.stat().st_mtime, tz=dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def render_reconstructed_conversation_markdown(
    bundle: ArtifactBundle,
    title: str,
    artifact_index_rel: pathlib.Path,
    text_page_links: list[tuple[str, pathlib.Path]],
    media_page_rel: pathlib.Path | None,
    media_assets: list[tuple[pathlib.Path, pathlib.Path]],
) -> str:
    request_summary = bundle_request_summary(bundle)
    output_summary = bundle_output_summary(bundle)
    task_summary = task_checkbox_summary(bundle)
    file_link_map = {file_title: rel_path for file_title, rel_path in text_page_links}
    metadata_lines = [
        f"- Workspace: `{bundle.workspace.name}`",
        f"- Workspace Identifier: `{bundle.workspace.identifier}`",
        f"- Conversation Type: `artifact-backed`",
        f"- Bundle ID: `{bundle.bundle_id}`",
        f"- Inferred Updated: `{bundle_timestamp(bundle) or '-'}`",
    ]
    if bundle.workspace.root:
        metadata_lines.append(f"- Workspace Root: `{bundle.workspace.root}`")
    if bundle.brain_dir:
        metadata_lines.append(f"- Brain Directory: `{bundle.brain_dir}`")
    if bundle.annotation_path:
        metadata_lines.append(f"- Annotation: `{bundle.annotation_path}`")
    if bundle.browser_recording_dir:
        metadata_lines.append(f"- Browser Recording Directory: `{bundle.browser_recording_dir}`")
    if bundle.evidence_counts:
        evidence_text = ", ".join(f"{key}={value}" for key, value in bundle.evidence_counts.most_common())
        metadata_lines.append(f"- Workspace Evidence: `{evidence_text}`")

    sections = [
        f"# {title}",
        "",
        "## Metadata",
        "",
        *metadata_lines,
        "",
        "## Artifact Bundle",
        "",
        f"- [Artifact Index]({artifact_index_rel.as_posix()})",
    ]
    if media_page_rel is not None:
        sections.append(f"- [Media Inventory]({media_page_rel.as_posix()})")
    sections.extend(["", "## Reconstructed Conversation", ""])

    sections.extend(["### User Request (inferred)", "", request_summary, ""])
    if "implementation_plan.md" in file_link_map:
        sections.append(f"- Related Plan: [implementation_plan.md]({file_link_map['implementation_plan.md'].as_posix()})")
        sections.append("")

    sections.extend(["### Assistant Output (reconstructed)", "", output_summary, ""])
    if task_summary:
        sections.append(f"- Task Status: {task_summary}")
    if "walkthrough.md" in file_link_map:
        sections.append(f"- Walkthrough: [walkthrough.md]({file_link_map['walkthrough.md'].as_posix()})")
    if "task.md" in file_link_map:
        sections.append(f"- Task List: [task.md]({file_link_map['task.md'].as_posix()})")
    sections.extend(["", "## Artifact Summaries", ""])

    summaries = artifact_metadata_summaries(bundle)
    if summaries:
        for artifact_type, summary, source_path in sorted(
            summaries,
            key=lambda item: (artifact_type_rank(item[0]), item[2].name.lower()),
        ):
            sections.append(f"### {artifact_type}")
            sections.append("")
            sections.append(f"- Source: `{source_path}`")
            sections.append("")
            sections.append(summary)
            sections.append("")
    else:
        sections.append("_No metadata summaries found._")
        sections.append("")

    sections.extend(["## Files", ""])
    if text_page_links:
        for file_title, rel_path in text_page_links:
            sections.append(f"- [{file_title}]({rel_path.as_posix()})")
    else:
        sections.append("_No text artifact files exported._")
    if media_assets:
        sections.extend(["", "## Visual Assets", ""])
        for source_path, rel_path in media_assets[:12]:
            sections.append(f"- [{source_path.name}]({rel_path.as_posix()})")
            if is_image_file(source_path):
                sections.extend(["", f"![{source_path.name}]({rel_path.as_posix()})", ""])
    sections.append("")
    return "\n".join(sections)


def render_live_conversation_markdown(
    conversation: LiveConversationRecord,
    title: str,
    args: argparse.Namespace,
    *,
    bundle: ArtifactBundle | None,
    artifact_index_rel: pathlib.Path | None,
    media_page_rel: pathlib.Path | None,
    media_assets: list[tuple[pathlib.Path, pathlib.Path]],
    source_output_map: dict[str, pathlib.Path],
    page_path: pathlib.Path,
) -> str:
    metadata_lines = [
        f"- Workspace: `{conversation.workspace.name}`",
        f"- Workspace Identifier: `{conversation.workspace.identifier}`",
        f"- Conversation ID: `{conversation.conversation_id}`",
        f"- Trajectory ID: `{conversation.trajectory_id}`",
        f"- Created: `{format_iso_timestamp(conversation.summary.get('createdTime')) or '-'}`",
        f"- Updated: `{format_iso_timestamp(conversation.summary.get('lastModifiedTime')) or '-'}`",
        f"- Status: `{conversation.summary.get('status') or '-'}`",
        f"- Step Count: `{conversation.summary.get('stepCount') or len(conversation.trajectory.get('steps') or [])}`",
        f"- Live RPC Source: `{conversation.source_kind} pid={conversation.source_pid} port={conversation.source_port}`",
    ]
    if conversation.workspace.root:
        metadata_lines.append(f"- Workspace Root: `{conversation.workspace.root}`")
    if conversation.workspace.aliases:
        metadata_lines.append(
            "- Workspace Aliases: " + ", ".join(f"`{alias}`" for alias in sorted(conversation.workspace.aliases))
        )
    if bundle is not None:
        metadata_lines.append(f"- Related Artifact Bundle: `{bundle.bundle_id}`")

    transcript_sections: list[str] = []
    trace_sections: list[str] = []
    for step in conversation.trajectory.get("steps") or []:
        if not isinstance(step, dict):
            continue
        transcript_section = render_live_transcript_step(step, args.max_tool_output_chars)
        if transcript_section:
            transcript_sections.append(
                rewrite_export_links(transcript_section.rstrip(), page_path=page_path, source_output_map=source_output_map)
            )
        trace_section = render_live_trace_step(step, args.max_tool_output_chars)
        if trace_section:
            trace_sections.append(
                rewrite_export_links(trace_section.rstrip(), page_path=page_path, source_output_map=source_output_map)
            )

    transcript = "\n\n".join(section for section in transcript_sections if section).strip()
    if not transcript:
        transcript = "_No explicit user/assistant turns were recovered from the live trajectory._"

    trace = "\n\n".join(section for section in trace_sections if section).strip()
    if not trace:
        trace = "_No execution trace was recovered._"

    parts = [
        f"# {title}",
        "",
        "## Metadata",
        "",
        *metadata_lines,
        "",
    ]
    summary_text = str(conversation.summary.get("summary") or "").strip()
    if summary_text:
        parts.extend(["## Summary", "", summary_text, ""])
    if artifact_index_rel is not None:
        parts.extend(["## Related Artifacts", "", f"- [Artifact Index]({artifact_index_rel.as_posix()})"])
        if media_page_rel is not None:
            parts.append(f"- [Media Inventory]({media_page_rel.as_posix()})")
        parts.append("")
    parts.extend(["## Transcript", "", transcript, "", "## Execution Trace", "", trace, ""])
    if media_assets:
        parts.extend(["## Visual Assets", ""])
        for source_path, rel_path in media_assets[:12]:
            parts.append(f"- [{source_path.name}]({rel_path.as_posix()})")
            if is_image_file(source_path):
                parts.extend(["", f"![{source_path.name}]({rel_path.as_posix()})", ""])
        parts.append("")
    return "\n".join(parts)


def render_code_tracker_snapshot_index(snapshot: CodeTrackerSnapshot, file_links: list[tuple[str, pathlib.Path]]) -> str:
    lines = [
        f"# Code Tracker Snapshot {snapshot.snapshot_id}",
        "",
        "## Metadata",
        "",
        f"- Workspace: `{snapshot.workspace.name}`",
        f"- Workspace Identifier: `{snapshot.workspace.identifier}`",
        f"- Source Directory: `{snapshot.snapshot_dir}`",
        f"- Files Exported: `{len(file_links)}`",
        "",
        "## Files",
        "",
    ]
    for title, rel_path in file_links:
        lines.append(f"- [{title}]({rel_path.as_posix()})")
    lines.append("")
    return "\n".join(lines)


def build_generated_pages(
    output_dir: pathlib.Path,
    sessions: list[SessionRecord],
    live_conversations: list[LiveConversationRecord],
    bundles: list[ArtifactBundle],
    snapshots: list[CodeTrackerSnapshot],
    args: argparse.Namespace,
) -> list[GeneratedPage]:
    pages: list[GeneratedPage] = []
    workspace_pages: dict[str, list[tuple[str, pathlib.Path, str]]] = defaultdict(list)
    workspace_artifact_index_links: dict[str, list[tuple[str, pathlib.Path]]] = defaultdict(list)
    workspace_tracker_links: dict[str, list[tuple[str, pathlib.Path]]] = defaultdict(list)
    workspaces_dir = output_dir / "workspaces"
    source_output_map: dict[str, pathlib.Path] = {}

    bundle_text_targets: dict[tuple[str, pathlib.Path], pathlib.Path] = {}
    bundle_media_targets: dict[tuple[str, pathlib.Path], pathlib.Path] = {}
    tracker_targets: dict[tuple[str, pathlib.Path], pathlib.Path] = {}
    bundle_by_id = {bundle.bundle_id: bundle for bundle in bundles}
    live_ids = {conversation.conversation_id for conversation in live_conversations}

    for session in sessions:
        if session.session_id in live_ids:
            continue
        title = choose_session_title(session)
        updated_prefix = (format_iso_timestamp(session.conversation.get("lastUpdated")) or "unknown")[:10]
        file_name = f"{updated_prefix}-{session.session_id}-{slugify(title)[:80]}.md"
        file_path = workspaces_dir / session.workspace.slug / "conversations" / file_name
        pages.append(
            GeneratedPage(
                item_id=f"conversation:{session.session_id}",
                file_path=file_path,
                content=render_session_markdown(session, title, args),
            )
        )
        workspace_pages[session.workspace.slug].append((title, file_path, f"conversation `{session.session_id}`"))

    for bundle in bundles:
        bundle_dir = workspaces_dir / bundle.workspace.slug / "artifacts" / bundle.bundle_id
        for index, source_path in enumerate(bundle.text_files, start=1):
            rel_name = sanitize_output_basename(source_path, index)
            target_path = bundle_dir / "files" / rel_name
            bundle_text_targets[(bundle.bundle_id, source_path)] = target_path
            source_output_map[source_map_key(source_path)] = target_path

        for index, source_path in enumerate(bundle.media_files, start=1):
            target_path = bundle_dir / "media" / media_output_name(source_path, index)
            bundle_media_targets[(bundle.bundle_id, source_path)] = target_path
            source_output_map[source_map_key(source_path)] = target_path

    for snapshot in snapshots:
        snapshot_dir = workspaces_dir / snapshot.workspace.slug / "code_tracker" / snapshot.snapshot_id
        for index, source_path in enumerate(snapshot.files, start=1):
            target_path = snapshot_dir / "files" / sanitize_output_basename(source_path, index)
            tracker_targets[(snapshot.snapshot_id, source_path)] = target_path
            source_output_map[source_map_key(source_path)] = target_path

    for bundle in bundles:
        bundle_dir = workspaces_dir / bundle.workspace.slug / "artifacts" / bundle.bundle_id
        text_links: list[tuple[str, pathlib.Path]] = []
        media_assets: list[tuple[pathlib.Path, pathlib.Path]] = []
        for source_path in bundle.text_files:
            target_path = bundle_text_targets[(bundle.bundle_id, source_path)]
            try:
                body = source_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                body = ""
            title = source_path.name
            text_links.append((title, target_path.relative_to(bundle_dir)))
            pages.append(
                GeneratedPage(
                    item_id=f"artifact-file:{bundle.bundle_id}:{source_path}",
                    file_path=target_path,
                    content=render_text_file_page(
                        title,
                        source_path,
                        body,
                        page_path=target_path,
                        source_output_map=source_output_map,
                    ),
                )
            )

        for source_path in bundle.media_files:
            target_path = bundle_media_targets[(bundle.bundle_id, source_path)]
            media_assets.append((source_path, target_path.relative_to(bundle_dir)))
            try:
                content_bytes = source_path.read_bytes()
            except OSError:
                continue
            pages.append(
                GeneratedPage(
                    item_id=f"artifact-media-file:{bundle.bundle_id}:{source_path}",
                    file_path=target_path,
                    content=content_bytes,
                )
            )

        media_page_path: pathlib.Path | None = None
        if media_assets:
            media_page_path = bundle_dir / "media.md"
            media_lines = [
                f"# Media Inventory {bundle.bundle_id}",
                "",
                "## Metadata",
                "",
                f"- Workspace: `{bundle.workspace.name}`",
                f"- Bundle ID: `{bundle.bundle_id}`",
                f"- Media Files: `{len(media_assets)}`",
                "",
                "## Files",
                "",
                *render_media_inventory([(source, bundle_dir / rel_path) for source, rel_path in media_assets]),
                "",
            ]
            pages.append(
                GeneratedPage(
                    item_id=f"artifact-media:{bundle.bundle_id}",
                    file_path=media_page_path,
                    content="\n".join(media_lines),
                )
            )

        bundle_index_path = bundle_dir / "index.md"
        pages.append(
            GeneratedPage(
                item_id=f"artifact-bundle:{bundle.bundle_id}",
                file_path=bundle_index_path,
                content=render_artifact_bundle_index(bundle, text_links, media_page_path.relative_to(bundle_dir) if media_page_path else None),
            )
        )
        workspace_artifact_index_links[bundle.workspace.slug].append(
            (f"{bundle.bundle_id}", bundle_index_path)
        )

    for conversation in live_conversations:
        title = choose_live_conversation_title(conversation)
        updated_prefix = (format_iso_timestamp(conversation.summary.get("lastModifiedTime")) or "unknown")[:10]
        conversation_path = (
            workspaces_dir
            / conversation.workspace.slug
            / "conversations"
            / f"{updated_prefix}-{conversation.conversation_id}-{slugify(title)[:80]}.md"
        )
        bundle = bundle_by_id.get(conversation.conversation_id)
        bundle_dir = None
        artifact_index_rel = None
        media_page_rel = None
        media_assets: list[tuple[pathlib.Path, pathlib.Path]] = []
        if bundle is not None:
            bundle_dir = workspaces_dir / bundle.workspace.slug / "artifacts" / bundle.bundle_id
            artifact_index_rel = relative_link_path(bundle_dir / "index.md", conversation_path.parent)
            media_page_path = bundle_dir / "media.md"
            if media_page_path.exists() or any(source_path.exists() for source_path in bundle.media_files):
                if bundle.media_files:
                    media_page_rel = relative_link_path(media_page_path, conversation_path.parent)
                    media_assets = [
                        (
                            source_path,
                            relative_link_path(bundle_media_targets[(bundle.bundle_id, source_path)], conversation_path.parent),
                        )
                        for source_path in bundle.media_files
                        if (bundle.bundle_id, source_path) in bundle_media_targets
                    ]
        pages.append(
            GeneratedPage(
                item_id=f"conversation-live:{conversation.conversation_id}",
                file_path=conversation_path,
                content=render_live_conversation_markdown(
                    conversation,
                    title,
                    args,
                    bundle=bundle,
                    artifact_index_rel=artifact_index_rel,
                    media_page_rel=media_page_rel,
                    media_assets=media_assets,
                    source_output_map=source_output_map,
                    page_path=conversation_path,
                ),
            )
        )
        workspace_pages[conversation.workspace.slug].append(
            (title, conversation_path, f"live conversation `{conversation.conversation_id}`")
        )

    fallback_bundle_ids = {bundle.bundle_id for bundle in bundles if bundle.bundle_id not in live_ids}
    for bundle in bundles:
        if bundle.bundle_id not in fallback_bundle_ids:
            continue
        conversation_title = bundle_title(bundle)
        updated_prefix = (bundle_timestamp(bundle) or "unknown")[:10]
        bundle_dir = workspaces_dir / bundle.workspace.slug / "artifacts" / bundle.bundle_id
        bundle_index_path = bundle_dir / "index.md"
        media_page_path = bundle_dir / "media.md"
        text_links = [
            (source_path.name, bundle_text_targets[(bundle.bundle_id, source_path)].relative_to(bundle_dir))
            for source_path in bundle.text_files
            if (bundle.bundle_id, source_path) in bundle_text_targets
        ]
        media_assets = [
            (source_path, bundle_media_targets[(bundle.bundle_id, source_path)].relative_to(bundle_dir))
            for source_path in bundle.media_files
            if (bundle.bundle_id, source_path) in bundle_media_targets
        ]
        conversation_path = (
            workspaces_dir
            / bundle.workspace.slug
            / "conversations"
            / f"{updated_prefix}-{bundle.bundle_id}-{slugify(conversation_title)[:80]}.md"
        )
        pages.append(
            GeneratedPage(
                item_id=f"conversation-artifact:{bundle.bundle_id}",
                file_path=conversation_path,
                content=render_reconstructed_conversation_markdown(
                    bundle,
                    conversation_title,
                    relative_link_path(bundle_index_path, conversation_path.parent),
                    [
                        (title, relative_link_path(bundle_dir / rel_path, conversation_path.parent))
                        for title, rel_path in text_links
                    ],
                    relative_link_path(media_page_path, conversation_path.parent) if media_page_path else None,
                    [
                        (source_path, relative_link_path(bundle_dir / rel_path, conversation_path.parent))
                        for source_path, rel_path in media_assets
                    ],
                ),
            )
        )
        workspace_pages[bundle.workspace.slug].append(
            (conversation_title, conversation_path, f"artifact-backed conversation `{bundle.bundle_id}`")
        )

    for snapshot in snapshots:
        snapshot_dir = workspaces_dir / snapshot.workspace.slug / "code_tracker" / snapshot.snapshot_id
        file_links: list[tuple[str, pathlib.Path]] = []
        for source_path in snapshot.files:
            try:
                body = source_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                body = ""
            title = source_path.name
            target_path = tracker_targets[(snapshot.snapshot_id, source_path)]
            file_links.append((title, target_path.relative_to(snapshot_dir)))
            pages.append(
                GeneratedPage(
                    item_id=f"code-tracker-file:{snapshot.snapshot_id}:{source_path}",
                    file_path=target_path,
                    content=render_text_file_page(
                        title,
                        source_path,
                        body,
                        page_path=target_path,
                        source_output_map=source_output_map,
                    ),
                )
            )
        snapshot_index_path = snapshot_dir / "index.md"
        pages.append(
            GeneratedPage(
                item_id=f"code-tracker-snapshot:{snapshot.snapshot_id}",
                file_path=snapshot_index_path,
                content=render_code_tracker_snapshot_index(snapshot, file_links),
            )
        )
        workspace_tracker_links[snapshot.workspace.slug].append((snapshot.snapshot_id, snapshot_index_path))

    workspaces_by_slug: dict[str, WorkspaceInfo] = {}
    for session in sessions:
        if session.session_id not in live_ids:
            workspaces_by_slug[session.workspace.slug] = session.workspace
    for conversation in live_conversations:
        workspaces_by_slug[conversation.workspace.slug] = conversation.workspace
    for bundle in bundles:
        workspaces_by_slug[bundle.workspace.slug] = bundle.workspace
    for snapshot in snapshots:
        workspaces_by_slug[snapshot.workspace.slug] = snapshot.workspace

    exported_raw_sessions = sum(1 for session in sessions if session.session_id not in live_ids)
    exported_reconstructed = len(fallback_bundle_ids)
    root_lines = [
        "# Antigravity Workspace Export",
        "",
        f"- Gemini home: `{args.gemini_home}`",
        f"- Workspaces exported: `{len(workspaces_by_slug)}`",
        f"- Conversations exported: `{exported_raw_sessions + len(live_conversations) + exported_reconstructed}`",
        f"- Live RPC conversations: `{len(live_conversations)}`",
        f"- Raw chat fallback conversations: `{exported_raw_sessions}`",
        f"- Artifact-backed fallback conversations: `{exported_reconstructed}`",
        f"- Artifact bundles exported: `{len(bundles)}`",
        f"- Code tracker snapshots exported: `{len(snapshots)}`",
        "",
        "## Workspaces",
        "",
    ]

    for workspace_slug, workspace in sorted(workspaces_by_slug.items(), key=lambda item: item[1].name.lower()):
        workspace_index_path = workspaces_dir / workspace_slug / "index.md"
        root_lines.append(f"- [{workspace.name}]({workspace_index_path.relative_to(output_dir).as_posix()})")

        conversation_items = sorted(workspace_pages.get(workspace_slug, []), key=lambda item: item[0].lower())
        artifact_items = sorted(workspace_artifact_index_links.get(workspace_slug, []), key=lambda item: item[0].lower())
        tracker_items = sorted(workspace_tracker_links.get(workspace_slug, []), key=lambda item: item[0].lower())

        workspace_lines = [
            f"# {workspace.name}",
            "",
            "## Metadata",
            "",
            f"- Workspace Identifier: `{workspace.identifier}`",
            f"- Conversations: `{len(conversation_items)}`",
            f"- Live Conversations: `{sum(1 for _, _, detail in conversation_items if detail.startswith('live conversation '))}`",
            f"- Raw Chat Fallback Conversations: `{sum(1 for _, _, detail in conversation_items if detail.startswith('conversation '))}`",
            f"- Artifact-backed Fallback Conversations: `{sum(1 for _, _, detail in conversation_items if detail.startswith('artifact-backed conversation '))}`",
            f"- Artifact Bundles: `{len(artifact_items)}`",
            f"- Code Tracker Snapshots: `{len(tracker_items)}`",
        ]
        if workspace.root:
            workspace_lines.append(f"- Workspace Root: `{workspace.root}`")
        if workspace.aliases:
            workspace_lines.append("- Aliases: " + ", ".join(f"`{alias}`" for alias in sorted(workspace.aliases)))
        workspace_lines.extend(["", "## Conversations", ""])
        if conversation_items:
            for title, path, detail in conversation_items:
                workspace_lines.append(
                    f"- [{title}]({path.relative_to(workspaces_dir / workspace_slug).as_posix()}) | {detail}"
                )
        else:
            workspace_lines.append("_No conversations exported._")

        workspace_lines.extend(["", "## Artifact Bundles", ""])
        if artifact_items:
            for bundle_id, path in artifact_items:
                workspace_lines.append(
                    f"- [{bundle_id}]({path.relative_to(workspaces_dir / workspace_slug).as_posix()})"
                )
        else:
            workspace_lines.append("_No workspace artifacts exported._")

        workspace_lines.extend(["", "## Code Tracker", ""])
        if tracker_items:
            for snapshot_id, path in tracker_items:
                workspace_lines.append(
                    f"- [{snapshot_id}]({path.relative_to(workspaces_dir / workspace_slug).as_posix()})"
                )
        else:
            workspace_lines.append("_No code tracker snapshots exported._")
        workspace_lines.append("")
        pages.append(
            GeneratedPage(
                item_id=f"workspace-index:{workspace_slug}",
                file_path=workspace_index_path,
                content="\n".join(workspace_lines),
            )
        )

    pages.append(
        GeneratedPage(
            item_id="root-index",
            file_path=output_dir / "index.md",
            content="\n".join(root_lines) + "\n",
        )
    )
    return pages


def remove_stale_generated_files(output_dir: pathlib.Path, pages: list[GeneratedPage]) -> None:
    expected_paths = {page.file_path.resolve() for page in pages}
    expected_paths.add((output_dir / "index.md").resolve())

    workspaces_dir = output_dir / "workspaces"
    if workspaces_dir.exists():
        for path in sorted(workspaces_dir.rglob("*"), reverse=True):
            resolved = path.resolve()
            if path.is_file() and resolved not in expected_paths:
                path.unlink()
            elif path.is_dir():
                try:
                    next(path.iterdir())
                except StopIteration:
                    path.rmdir()


def state_scope(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "session_ids": list(args.session_ids),
        "workspace_ids": list(args.workspace_ids),
        "limit": args.limit,
        "include_system_only": bool(args.include_system_only),
        "include_subagents": bool(args.include_subagents),
        "max_tool_output_chars": args.max_tool_output_chars,
    }


def load_export_state(output_dir: pathlib.Path, gemini_home: pathlib.Path, args: argparse.Namespace) -> dict[str, ExportStateRecord]:
    if args.no_incremental:
        return {}
    state_path = output_dir / ".antigravity-export-state.json"
    if not state_path.exists():
        return {}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if payload.get("version") != 3:
        return {}
    if payload.get("gemini_home") != str(gemini_home):
        return {}
    if payload.get("scope") != state_scope(args):
        return {}
    records: dict[str, ExportStateRecord] = {}
    for item_id, record in (payload.get("items") or {}).items():
        try:
            records[item_id] = ExportStateRecord(
                item_id=item_id,
                file_path=str(record["file_path"]),
                signature=str(record["signature"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return records


def save_export_state(
    output_dir: pathlib.Path,
    gemini_home: pathlib.Path,
    args: argparse.Namespace,
    records: dict[str, ExportStateRecord],
) -> None:
    payload = {
        "version": 3,
        "gemini_home": str(gemini_home),
        "scope": state_scope(args),
        "saved_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "items": {
            item_id: {"file_path": record.file_path, "signature": record.signature}
            for item_id, record in sorted(records.items())
        },
    }
    write_text(output_dir / ".antigravity-export-state.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def write_generated_pages(
    output_dir: pathlib.Path,
    gemini_home: pathlib.Path,
    args: argparse.Namespace,
    pages: list[GeneratedPage],
) -> int:
    existing_state = load_export_state(output_dir, gemini_home, args)
    next_state: dict[str, ExportStateRecord] = {}
    seen_item_ids: set[str] = set()
    processed = 0

    for page in pages:
        seen_item_ids.add(page.item_id)
        signature = generated_file_signature(page.content)
        relative_path = page.file_path.relative_to(output_dir).as_posix()
        cached = existing_state.get(page.item_id)
        can_reuse = (
            cached is not None
            and cached.signature == signature
            and cached.file_path == relative_path
            and (output_dir / cached.file_path).exists()
        )
        if not can_reuse:
            if cached is not None and cached.file_path != relative_path:
                remove_file_if_exists(output_dir / cached.file_path)
            if isinstance(page.content, bytes):
                page.file_path.parent.mkdir(parents=True, exist_ok=True)
                page.file_path.write_bytes(page.content)
            else:
                write_text(page.file_path, page.content if page.content.endswith("\n") else page.content + "\n")
            processed += 1
        next_state[page.item_id] = ExportStateRecord(
            item_id=page.item_id,
            file_path=relative_path,
            signature=signature,
        )

    for item_id, record in existing_state.items():
        if item_id not in seen_item_ids:
            remove_file_if_exists(output_dir / record.file_path)

    save_export_state(output_dir, gemini_home, args, next_state)
    return processed


def export_workspace_data(args: argparse.Namespace) -> ExportRunResult:
    gemini_home = pathlib.Path(args.gemini_home).expanduser().resolve()
    output_dir = pathlib.Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "workspaces").mkdir(parents=True, exist_ok=True)

    catalog = WorkspaceCatalog()
    discover_base_workspaces(gemini_home, catalog)

    sessions = [session for session in discover_sessions(args, gemini_home, catalog) if should_export_session(session, args)]
    bundles = discover_artifact_bundles(gemini_home, catalog, set(args.workspace_ids))
    snapshots = discover_code_tracker_snapshots(gemini_home, catalog, set(args.workspace_ids))
    live_conversations = discover_live_conversations(args, gemini_home, catalog)

    pages = build_generated_pages(output_dir, sessions, live_conversations, bundles, snapshots, args)
    remove_stale_generated_files(output_dir, pages)
    processed_pages = write_generated_pages(output_dir, gemini_home, args, pages)
    prune_empty_dirs(output_dir / "workspaces")

    live_ids = {conversation.conversation_id for conversation in live_conversations}
    exported_workspaces = {session.workspace.slug for session in sessions if session.session_id not in live_ids}
    exported_workspaces.update(conversation.workspace.slug for conversation in live_conversations)
    exported_workspaces.update(bundle.workspace.slug for bundle in bundles)
    exported_workspaces.update(snapshot.workspace.slug for snapshot in snapshots)
    raw_fallback_count = sum(1 for session in sessions if session.session_id not in live_ids)
    reconstructed_fallback_count = sum(1 for bundle in bundles if bundle.bundle_id not in live_ids)
    return ExportRunResult(
        output_dir=output_dir,
        page_count=len(pages),
        processed_pages=processed_pages,
        workspace_count=len(exported_workspaces),
        live_conversation_count=len(live_conversations),
        raw_conversation_count=raw_fallback_count,
        reconstructed_conversation_count=reconstructed_fallback_count,
        conversation_count=len(live_conversations) + raw_fallback_count + reconstructed_fallback_count,
        artifact_count=len(bundles),
        code_tracker_count=len(snapshots),
    )


def main() -> None:
    args = parse_args()
    result = export_workspace_data(args)
    print(
        textwrap.dedent(
            f"""\
            Exported {result.workspace_count} workspace(s)
            Conversations: {result.conversation_count}
            Live RPC conversations: {result.live_conversation_count}
            Raw chat fallback conversations: {result.raw_conversation_count}
            Artifact-backed fallback conversations: {result.reconstructed_conversation_count}
            Artifact bundles: {result.artifact_count}
            Code tracker snapshots: {result.code_tracker_count}
            Pages generated: {result.page_count}
            Reprocessed this run: {result.processed_pages}
            Output directory: {result.output_dir}
            Root index: {result.output_dir / 'index.md'}
            """
        ).strip()
    )


if __name__ == "__main__":
    main()
