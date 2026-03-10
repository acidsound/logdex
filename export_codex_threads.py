#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sqlite3
import textwrap
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Any


TOOL_OUTPUT_HEADER_RE = re.compile(
    r"^Chunk ID:\s+(?P<chunk>\S+)\n"
    r"Wall time:\s+(?P<wall_time>[0-9.]+)\s+seconds\n"
    r"Process exited with code\s+(?P<exit_code>-?[0-9]+)\n"
    r"Original token count:\s+(?P<token_count>[0-9]+)\n"
    r"Output:\n",
    re.DOTALL,
)
GIT_COMMIT_CMD_RE = re.compile(r"\bgit\s+commit\b")
GIT_PUSH_CMD_RE = re.compile(r"\bgit\s+push\b")
GIT_ANY_CMD_RE = re.compile(r"\bgit\b")
GIT_COMMIT_OUTPUT_RE = re.compile(
    r"^\[[^\]]* (?P<hash>[0-9a-f]{7,40})\] (?P<message>.+)$", re.MULTILINE
)
GIT_LOG_LINE_RE = re.compile(
    r"^(?P<hash>[0-9a-f]{7,40})(?: \([^)]+\))? (?P<message>.+)$", re.MULTILINE
)


@dataclass
class TranscriptEvent:
    timestamp: str | None
    kind: str
    label: str
    body: str
    metadata: dict[str, Any]


@dataclass
class GitActivity:
    commit_hash: str
    message: str
    timestamp: str | None
    workdir: str | None
    committed: bool = False
    pushed: bool = False


@dataclass
class ExportedThread:
    row: sqlite3.Row
    file_path: pathlib.Path
    title: str
    project_name: str
    project_slug: str


@dataclass
class ExportStateRecord:
    thread_id: str
    updated_at: int
    file_path: str
    title: str
    project_name: str
    project_slug: str
    archived: bool


@dataclass
class ExportRunResult:
    output_dir: pathlib.Path
    exported_threads: list[ExportedThread]
    processed_threads: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Codex thread history into per-thread Markdown files."
    )
    parser.add_argument(
        "--codex-home",
        default=str(pathlib.Path.home() / ".codex"),
        help="Path to CODEX_HOME. Defaults to ~/.codex.",
    )
    parser.add_argument(
        "--db-path",
        help="Path to a specific state SQLite database. Defaults to the newest state_*.sqlite in CODEX_HOME.",
    )
    parser.add_argument(
        "--output-dir",
        default="codex-export",
        help="Directory where Markdown files will be written.",
    )
    parser.add_argument(
        "--archived",
        choices=("include", "exclude", "only"),
        default="include",
        help="Whether to export active threads, archived threads, or both.",
    )
    parser.add_argument(
        "--thread-id",
        action="append",
        dest="thread_ids",
        default=[],
        help="Export only the specified thread ID. Repeat to export multiple threads.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of threads to export, ordered by updated_at descending.",
    )
    parser.add_argument(
        "--include-tool-output",
        action="store_true",
        help="Include tool outputs in the Markdown export.",
    )
    parser.add_argument(
        "--max-tool-output-chars",
        type=int,
        default=4000,
        help="Maximum number of characters to keep for each tool output block.",
    )
    parser.add_argument(
        "--no-incremental",
        action="store_true",
        help="Force a full rebuild instead of reusing the last export state in the output directory.",
    )
    return parser.parse_args()


def newest_state_db(codex_home: pathlib.Path) -> pathlib.Path:
    candidates = sorted(
        codex_home.glob("state_*.sqlite"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit(f"No state_*.sqlite found under {codex_home}")
    return candidates[0]


def load_name_overrides(codex_home: pathlib.Path) -> dict[str, str]:
    overrides: dict[str, str] = {}
    index_path = codex_home / "session_index.jsonl"
    if not index_path.exists():
        return overrides

    with index_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            thread_id = str(payload.get("id") or "").strip()
            thread_name = str(payload.get("thread_name") or "").strip()
            if thread_id and thread_name:
                overrides[thread_id] = thread_name
    return overrides


def connect_db(db_path: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_threads(
    conn: sqlite3.Connection,
    archived_mode: str,
    thread_ids: list[str],
    limit: int | None,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[Any] = []

    if archived_mode == "exclude":
        clauses.append("archived = 0")
    elif archived_mode == "only":
        clauses.append("archived = 1")

    if thread_ids:
        placeholders = ", ".join("?" for _ in thread_ids)
        clauses.append(f"id IN ({placeholders})")
        params.extend(thread_ids)

    sql = "SELECT * FROM threads"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC, id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    return list(conn.execute(sql, params))


def fetch_logs(conn: sqlite3.Connection, thread_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT ts, ts_nanos, level, target, message
            FROM logs
            WHERE thread_id = ?
            ORDER BY ts ASC, ts_nanos ASC, id ASC
            """,
            (thread_id,),
        )
    )


def format_epoch(epoch_seconds: Any) -> str:
    if epoch_seconds in (None, ""):
        return ""
    timestamp = dt.datetime.fromtimestamp(int(epoch_seconds), tz=dt.timezone.utc)
    return timestamp.astimezone().isoformat(timespec="seconds")


def format_iso_timestamp(timestamp: str | None) -> str | None:
    if not timestamp:
        return None
    candidate = timestamp.strip()
    if not candidate:
        return None
    try:
        parsed = dt.datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return candidate
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone().isoformat(timespec="seconds")


def slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_only).strip("-").lower()
    return slug or "thread"


def normalize_title(text: str, fallback: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned or not any(char.isalnum() for char in cleaned):
        return fallback
    if len(cleaned) > 120:
        cleaned = cleaned[:117].rstrip() + "..."
    return cleaned


def choose_title(row: sqlite3.Row, overrides: dict[str, str]) -> str:
    thread_id = str(row["id"])
    first_user_message = row["first_user_message"] if "first_user_message" in row.keys() else None
    for candidate in (
        overrides.get(thread_id),
        row["title"],
        first_user_message,
    ):
        if candidate and str(candidate).strip():
            return normalize_title(str(candidate), thread_id)
    return thread_id


def infer_project(row: sqlite3.Row) -> tuple[str, str]:
    origin_url = str(row["git_origin_url"] or "").strip() if "git_origin_url" in row.keys() else ""
    cwd = str(row["cwd"] or "").strip()

    if origin_url:
        repo_name = origin_url.rstrip("/").rsplit("/", 1)[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        project_name = normalize_title(repo_name, "unknown-project")
        return project_name, slugify(project_name)

    if cwd:
        cwd_path = pathlib.Path(cwd)
        for candidate in (cwd_path.name, cwd_path.parent.name):
            if candidate:
                project_name = normalize_title(candidate, "unknown-project")
                return project_name, slugify(project_name)

    return "unknown-project", "unknown-project"


def render_content_items(items: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in items:
        item_type = item.get("type")
        if item_type in {"input_text", "output_text"}:
            text = str(item.get("text") or "").strip()
            if text:
                parts.append(text)
            continue
        if item_type == "image_url":
            url = item.get("image_url")
            if url:
                parts.append(f"Image: {url}")
            continue
        if item_type == "local_image":
            path = item.get("path")
            if path:
                parts.append(f"Local image: {path}")
            continue
        if item_type in {"mention", "skill"}:
            label = item.get("name") or item.get("path") or "item"
            path = item.get("path")
            parts.append(f"{item_type}: {label}" + (f" ({path})" if path else ""))
            continue
        parts.append(json.dumps(item, ensure_ascii=False, indent=2))
    return "\n\n".join(part for part in parts if part).strip()


def render_user_payload(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    message = str(payload.get("message") or "").rstrip()
    if message:
        parts.append(message)

    for image in payload.get("images") or []:
        image_url = image.get("image_url") if isinstance(image, dict) else None
        if image_url:
            parts.append(f"Image: {image_url}")

    for image in payload.get("local_images") or []:
        image_path = image.get("path") if isinstance(image, dict) else None
        if image_path:
            parts.append(f"Local image: {image_path}")

    for element in payload.get("text_elements") or []:
        if not isinstance(element, dict):
            continue
        text = element.get("text")
        if text:
            parts.append(str(text).rstrip())

    return "\n\n".join(part for part in parts if part).strip()


def summarize_tool_output(raw_output: Any, max_chars: int) -> tuple[str, str | None]:
    if not isinstance(raw_output, str):
        return json.dumps(raw_output, ensure_ascii=False, indent=2), None

    text = raw_output.replace("\r\n", "\n")
    summary: str | None = None
    match = TOOL_OUTPUT_HEADER_RE.match(text)
    if match:
        summary = (
            f"exit_code={match.group('exit_code')}, "
            f"wall_time={match.group('wall_time')}s, "
            f"tokens={match.group('token_count')}"
        )
        text = text[match.end() :]

    text = text.rstrip()
    if max_chars > 0 and len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "\n…"

    return text, summary


def parse_tool_arguments(arguments_text: str) -> dict[str, Any]:
    if not arguments_text:
        return {}
    try:
        payload = json.loads(arguments_text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def extract_git_commits(output_text: str) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for match in GIT_COMMIT_OUTPUT_RE.finditer(output_text):
        item = (match.group("hash"), match.group("message").strip())
        if item not in seen:
            seen.add(item)
            matches.append(item)

    for match in GIT_LOG_LINE_RE.finditer(output_text):
        message = match.group("message").strip()
        if not message or re.match(r"[0-9a-f]{7,40}\.\.[0-9a-f]{7,40}\s", message):
            continue
        item = (match.group("hash"), message)
        if item not in seen:
            seen.add(item)
            matches.append(item)

    return matches


def parse_rollout(
    rollout_path: pathlib.Path, max_tool_output_chars: int
) -> tuple[list[TranscriptEvent], list[GitActivity]]:
    events: list[TranscriptEvent] = []
    tool_calls: dict[str, dict[str, Any]] = {}
    git_activities: dict[tuple[str, str, str], GitActivity] = {}
    last_commit_by_workdir: dict[str, tuple[str, str, str]] = {}

    with rollout_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue

            try:
                envelope = json.loads(line)
            except json.JSONDecodeError:
                continue

            timestamp = format_iso_timestamp(envelope.get("timestamp"))
            item_type = envelope.get("type")
            payload = envelope.get("payload") or {}

            if item_type == "event_msg" and payload.get("type") == "user_message":
                body = render_user_payload(payload)
                if body:
                    events.append(
                        TranscriptEvent(
                            timestamp=timestamp,
                            kind="user",
                            label="User",
                            body=body,
                            metadata={},
                        )
                    )
                continue

            if item_type != "response_item":
                continue

            payload_type = payload.get("type")
            if payload_type == "message" and payload.get("role") == "assistant":
                body = render_content_items(payload.get("content") or [])
                if body:
                    phase = payload.get("phase")
                    label = "Assistant" if not phase else f"Assistant ({phase})"
                    events.append(
                        TranscriptEvent(
                            timestamp=timestamp,
                            kind="assistant",
                            label=label,
                            body=body,
                            metadata={"phase": phase},
                        )
                    )
                continue

            if payload_type == "function_call":
                name = str(payload.get("name") or "tool")
                call_id = str(payload.get("call_id") or "")
                arguments_text = payload.get("arguments") or ""
                tool_args = parse_tool_arguments(str(arguments_text))
                command_text = str(tool_args.get("cmd") or "")
                workdir = str(tool_args.get("workdir") or "")
                call_info = {
                    "name": name,
                    "command_text": command_text,
                    "workdir": workdir,
                    "has_git_commit": bool(GIT_COMMIT_CMD_RE.search(command_text)),
                    "has_git_push": bool(GIT_PUSH_CMD_RE.search(command_text)),
                    "is_git_related": bool(GIT_ANY_CMD_RE.search(command_text)),
                }
                if call_id:
                    tool_calls[call_id] = call_info
                events.append(
                    TranscriptEvent(
                        timestamp=timestamp,
                        kind="tool_call",
                        label=f"Tool Call `{name}`",
                        body=str(arguments_text).strip(),
                        metadata={"call_id": call_id, "name": name, **call_info},
                    )
                )
                continue

            if payload_type == "function_call_output":
                call_id = str(payload.get("call_id") or "")
                call_info = tool_calls.get(call_id, {})
                tool_name = str(call_info.get("name") or "tool")
                output_text, summary = summarize_tool_output(
                    payload.get("output"), max_tool_output_chars
                )
                command_text = str(call_info.get("command_text") or "")
                workdir = str(call_info.get("workdir") or "")
                commit_matches = extract_git_commits(output_text)
                for commit_hash, message in commit_matches:
                    key = (commit_hash, message, workdir)
                    activity = git_activities.get(key)
                    if activity is None:
                        activity = GitActivity(
                            commit_hash=commit_hash,
                            message=message,
                            timestamp=timestamp,
                            workdir=workdir or None,
                        )
                        git_activities[key] = activity
                    if call_info.get("has_git_commit"):
                        activity.committed = True
                    if call_info.get("has_git_push"):
                        activity.pushed = True
                    if workdir:
                        last_commit_by_workdir[workdir] = key

                if call_info.get("has_git_push") and workdir and not commit_matches:
                    previous_key = last_commit_by_workdir.get(workdir)
                    if previous_key and previous_key in git_activities:
                        git_activities[previous_key].pushed = True

                events.append(
                    TranscriptEvent(
                        timestamp=timestamp,
                        kind="tool_output",
                        label=f"Tool Output `{tool_name}`",
                        body=output_text,
                        metadata={
                            "call_id": call_id,
                            "name": tool_name,
                            "summary": summary,
                            "command_text": command_text,
                            "workdir": workdir,
                        },
                    )
                )
                continue

            if payload_type == "web_search_call":
                action = payload.get("action") or {}
                queries = action.get("queries") or []
                body = "\n".join(f"- {query}" for query in queries) or json.dumps(
                    payload, ensure_ascii=False, indent=2
                )
                events.append(
                    TranscriptEvent(
                        timestamp=timestamp,
                        kind="web_search",
                        label="Web Search",
                        body=body,
                        metadata={"status": payload.get("status")},
                    )
                )

    ordered_git_activities = sorted(
        git_activities.values(),
        key=lambda item: (item.timestamp or "", item.workdir or "", item.commit_hash, item.message),
    )
    return events, ordered_git_activities


def parse_logs_fallback(
    conn: sqlite3.Connection, thread_id: str, max_tool_output_chars: int
) -> tuple[list[TranscriptEvent], list[GitActivity]]:
    events: list[TranscriptEvent] = []
    for row in fetch_logs(conn, thread_id):
        target = str(row["target"] or "")
        message = row["message"]
        timestamp = format_epoch(row["ts"])
        if not message:
            continue

        if target == "codex_core::stream_events_utils" and str(message).startswith("ToolCall: "):
            body = str(message)[len("ToolCall: ") :].strip()
            events.append(
                TranscriptEvent(
                    timestamp=timestamp,
                    kind="tool_call",
                    label="Tool Call",
                    body=body,
                    metadata={"source": "logs"},
                )
            )
            continue

        rendered, _ = summarize_tool_output(message, max_tool_output_chars)
        events.append(
            TranscriptEvent(
                timestamp=timestamp,
                kind="log",
                label=f"Log {row['level']} {target}",
                body=rendered,
                metadata={"level": row["level"], "target": target},
            )
        )

    return events, []


def fence(text: str, language: str = "") -> str:
    matches = re.findall(r"`+", text)
    longest = max((len(match) for match in matches), default=0)
    marker = "`" * max(3, longest + 1)
    suffix = language if language else ""
    return f"{marker}{suffix}\n{text}\n{marker}"


def render_event_markdown(
    event: TranscriptEvent,
    include_tool_output: bool,
) -> str:
    heading = event.label
    if event.timestamp:
        heading = f"{heading} [{event.timestamp}]"

    if event.kind == "tool_output" and not include_tool_output:
        summary = event.metadata.get("summary")
        body = summary or "Output omitted. Re-run with --include-tool-output to include raw tool results."
        return f"## {heading}\n\n{body}\n"

    if event.kind in {"tool_call", "tool_output", "log"}:
        body = event.body if event.body else "(empty)"
        if event.kind == "tool_output":
            summary = event.metadata.get("summary")
            if summary:
                details = f"<details><summary>{summary}</summary>\n\n{fence(body, 'text')}\n\n</details>"
                return f"## {heading}\n\n{details}\n"
        language = "json" if body.lstrip().startswith("{") else "text"
        return f"## {heading}\n\n{fence(body, language)}\n"

    return f"## {heading}\n\n{event.body}\n"


def render_thread_markdown(
    row: sqlite3.Row,
    title: str,
    project_name: str,
    events: list[TranscriptEvent],
    git_activities: list[GitActivity],
    include_tool_output: bool,
) -> str:
    thread_id = str(row["id"])
    metadata_lines = [
        f"- Project: `{project_name}`",
        f"- Thread ID: `{thread_id}`",
        f"- Created: `{format_epoch(row['created_at'])}`",
        f"- Updated: `{format_epoch(row['updated_at'])}`",
        f"- Source: `{row['source']}`",
        f"- Model provider: `{row['model_provider']}`",
        f"- CWD: `{row['cwd']}`",
        f"- Archived: `{bool(row['archived'])}`",
        f"- Rollout path: `{row['rollout_path']}`",
    ]

    if "cli_version" in row.keys() and row["cli_version"]:
        metadata_lines.append(f"- CLI version: `{row['cli_version']}`")
    if "memory_mode" in row.keys() and row["memory_mode"]:
        metadata_lines.append(f"- Memory mode: `{row['memory_mode']}`")
    if row["git_branch"]:
        metadata_lines.append(f"- Git branch: `{row['git_branch']}`")
    if row["git_sha"]:
        metadata_lines.append(f"- Git SHA: `{row['git_sha']}`")
    if row["git_origin_url"]:
        metadata_lines.append(f"- Git remote: `{row['git_origin_url']}`")

    transcript = "\n".join(
        render_event_markdown(event, include_tool_output).rstrip() for event in events
    ).strip()
    if not transcript:
        transcript = "_No readable rollout events found._"

    visible_git_activities = [
        item for item in git_activities if item.committed or item.pushed
    ]
    git_section: list[str] = []
    if visible_git_activities:
        git_section = [
            "## Git Activity",
            "",
            "| Action | Commit | Message | Timestamp | Workdir |",
            "| --- | --- | --- | --- | --- |",
        ]
        for item in visible_git_activities:
            actions: list[str] = []
            if item.committed:
                actions.append("commit")
            if item.pushed:
                actions.append("push")
            action_text = ", ".join(actions) if actions else "git"
            message = item.message.replace("|", "\\|")
            git_section.append(
                f"| {action_text} | `{item.commit_hash}` | {message} | "
                f"`{item.timestamp or '-'}` | `{item.workdir or '-'}` |"
            )
        git_section.append("")

    return "\n".join(
        [
            f"# {title}",
            "",
            *git_section,
            "## Metadata",
            "",
            *metadata_lines,
            "",
            "## Transcript",
            "",
            transcript,
            "",
        ]
    )


def write_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def state_scope(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "archived": args.archived,
        "thread_ids": list(args.thread_ids),
        "limit": args.limit,
    }


def load_export_state(
    output_dir: pathlib.Path, db_path: pathlib.Path, args: argparse.Namespace
) -> dict[str, ExportStateRecord]:
    if args.no_incremental:
        return {}

    state_path = output_dir / ".codex-export-state.json"
    if not state_path.exists():
        return {}

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if payload.get("version") != 1:
        return {}
    if payload.get("db_path") != str(db_path):
        return {}
    if payload.get("scope") != state_scope(args):
        return {}

    records: dict[str, ExportStateRecord] = {}
    for thread_id, record in (payload.get("threads") or {}).items():
        try:
            records[thread_id] = ExportStateRecord(
                thread_id=thread_id,
                updated_at=int(record["updated_at"]),
                file_path=str(record["file_path"]),
                title=str(record["title"]),
                project_name=str(record["project_name"]),
                project_slug=str(record["project_slug"]),
                archived=bool(record["archived"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return records


def save_export_state(
    output_dir: pathlib.Path,
    db_path: pathlib.Path,
    args: argparse.Namespace,
    records: dict[str, ExportStateRecord],
) -> None:
    state_path = output_dir / ".codex-export-state.json"
    payload = {
        "version": 1,
        "db_path": str(db_path),
        "scope": state_scope(args),
        "saved_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "threads": {
            thread_id: {
                "updated_at": record.updated_at,
                "file_path": record.file_path,
                "title": record.title,
                "project_name": record.project_name,
                "project_slug": record.project_slug,
                "archived": record.archived,
            }
            for thread_id, record in sorted(records.items())
        },
    }
    write_text(state_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def remove_file_if_exists(path: pathlib.Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def prune_empty_project_dirs(projects_dir: pathlib.Path) -> None:
    if not projects_dir.exists():
        return
    for path in sorted(projects_dir.rglob("*"), reverse=True):
        if path.is_dir():
            try:
                next(path.iterdir())
            except StopIteration:
                path.rmdir()


def export_threads(args: argparse.Namespace) -> ExportRunResult:
    codex_home = pathlib.Path(args.codex_home).expanduser().resolve()
    db_path = (
        pathlib.Path(args.db_path).expanduser().resolve()
        if args.db_path
        else newest_state_db(codex_home)
    )
    output_dir = pathlib.Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    projects_dir = output_dir / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)

    name_overrides = load_name_overrides(codex_home)
    conn = connect_db(db_path)
    rows = fetch_threads(conn, args.archived, args.thread_ids, args.limit)
    existing_state = load_export_state(output_dir, db_path, args)
    next_state: dict[str, ExportStateRecord] = {}
    processed_threads = 0
    seen_thread_ids: set[str] = set()

    exported: list[ExportedThread] = []
    for row in rows:
        thread_id = str(row["id"])
        seen_thread_ids.add(thread_id)
        title = choose_title(row, name_overrides)
        project_name, project_slug = infer_project(row)
        rollout_path = pathlib.Path(row["rollout_path"])

        filename = (
            f"{format_epoch(row['updated_at'])[:10]}-"
            f"{row['id']}-"
            f"{slugify(title)[:80]}.md"
        )
        project_dir = projects_dir / project_slug
        file_path = project_dir / filename
        relative_path = file_path.relative_to(output_dir).as_posix()
        cached = existing_state.get(thread_id)
        can_reuse = (
            cached is not None
            and cached.updated_at == int(row["updated_at"])
            and cached.file_path == relative_path
            and pathlib.Path(output_dir / cached.file_path).exists()
        )

        if not can_reuse:
            if cached is not None and cached.file_path != relative_path:
                remove_file_if_exists(output_dir / cached.file_path)
            if rollout_path.exists():
                try:
                    events, git_activities = parse_rollout(
                        rollout_path, args.max_tool_output_chars
                    )
                except OSError:
                    events, git_activities = parse_logs_fallback(
                        conn, thread_id, args.max_tool_output_chars
                    )
            else:
                events, git_activities = parse_logs_fallback(
                    conn, thread_id, args.max_tool_output_chars
                )
            markdown = render_thread_markdown(
                row,
                title,
                project_name,
                events,
                git_activities,
                args.include_tool_output,
            )
            write_text(file_path, markdown)
            processed_threads += 1

        next_state[thread_id] = ExportStateRecord(
            thread_id=thread_id,
            updated_at=int(row["updated_at"]),
            file_path=relative_path,
            title=title,
            project_name=project_name,
            project_slug=project_slug,
            archived=bool(row["archived"]),
        )
        exported.append(
            ExportedThread(
                row=row,
                file_path=file_path,
                title=title,
                project_name=project_name,
                project_slug=project_slug,
            )
        )

    for thread_id, record in existing_state.items():
        if thread_id not in seen_thread_ids:
            remove_file_if_exists(output_dir / record.file_path)

    index_lines = [
        "# Codex Thread Export",
        "",
        f"- Generated at: `{dt.datetime.now().astimezone().isoformat(timespec='seconds')}`",
        f"- Codex home: `{codex_home}`",
        f"- SQLite DB: `{db_path}`",
        f"- Threads exported: `{len(exported)}`",
        f"- Threads reprocessed this run: `{processed_threads}`",
        "",
        "## Projects",
        "",
    ]

    grouped: dict[str, list[ExportedThread]] = defaultdict(list)
    for item in exported:
        grouped[item.project_slug].append(item)

    for project_slug in sorted(grouped, key=lambda slug: grouped[slug][0].project_name.lower()):
        project_items = sorted(
            grouped[project_slug],
            key=lambda item: (int(item.row["updated_at"]), str(item.row["id"])),
            reverse=True,
        )
        project_dir = projects_dir / project_slug
        project_index = project_dir / "index.md"
        rel_project_index = project_index.relative_to(output_dir)
        project_name = project_items[0].project_name
        index_lines.append(
            f"- [{project_name}]({rel_project_index.as_posix()})"
            f" | `{len(project_items)}` thread(s)"
        )

        project_lines = [
            f"# {project_name}",
            "",
            f"- Threads: `{len(project_items)}`",
            "",
            "## Threads",
            "",
        ]
        for item in project_items:
            rel_path = item.file_path.relative_to(project_dir)
            project_lines.append(
                f"- [{item.title}]({rel_path.as_posix()})"
                f" | `{item.row['id']}`"
                f" | updated `{format_epoch(item.row['updated_at'])}`"
                f" | archived `{bool(item.row['archived'])}`"
            )
        write_text(project_index, "\n".join(project_lines) + "\n")

    write_text(output_dir / "index.md", "\n".join(index_lines) + "\n")
    save_export_state(output_dir, db_path, args, next_state)
    prune_empty_project_dirs(projects_dir)
    return ExportRunResult(
        output_dir=output_dir,
        exported_threads=exported,
        processed_threads=processed_threads,
    )


def main() -> None:
    args = parse_args()
    result = export_threads(args)
    print(
        textwrap.dedent(
            f"""\
            Exported {len(result.exported_threads)} thread(s)
            Reprocessed this run: {result.processed_threads}
            Output directory: {result.output_dir}
            Root index: {result.output_dir / 'index.md'}
            """
        ).strip()
    )


if __name__ == "__main__":
    main()
