# Conversation Exporters

This repository contains two exporters.

- `export_codex_threads.py`: exports local Codex threads
- `export_antigravity_conversations.py`: exports local Antigravity / Gemini CLI workspace data

## Codex

`export_codex_threads.py` reads the local Codex state database under `~/.codex`, resolves each thread's `rollout_path`, and writes one Markdown file per thread.

The export is grouped by project:

- root `index.md`: project list
- `projects/<project>/index.md`: thread list for that project
- `projects/<project>/*.md`: per-thread Markdown

If a thread contains `git commit` or `git push` activity that can be reconstructed from tool calls and outputs, the thread Markdown starts with a `Git Activity` summary table.

Repeated runs against the same `--output-dir` are incremental by default. The exporter stores `.codex-export-state.json` in that directory and only reprocesses threads whose SQLite `updated_at` changed. Use `--no-incremental` to force a full rebuild.

## How to find `--thread-id`

In the Codex app, you can copy the current session ID (thread ID) directly.

1. In the current thread, open the `...` menu and copy the session ID.

2. Use the `cmd+option+C` shortcut to copy the current session ID.

3. If you want to resolve it from local data instead, query recent threads from SQLite:

```sh
sqlite3 -header -column ~/.codex/state_5.sqlite "select id, title, updated_at from threads order by updated_at desc limit 20;"
```

Find the matching thread by `title`, then use the `id` value as `--thread-id`.

4. You can also read it from the rollout filename:

```sh
find ~/.codex/sessions -type f | sort | tail -n 20
```

Rollout files usually look like this, and the final UUID portion is the thread ID:

```text
rollout-2026-03-10T20-07-24-019cd76e-2138-7a03-bc53-4df562e9a83a.jsonl
```

5. If you already exported once, each generated thread Markdown file includes `Thread ID` in its `Metadata` section.

Basic usage:

```sh
python3 export_codex_threads.py --output-dir codex-export
```

Useful options:

```sh
python3 export_codex_threads.py --archived include --include-tool-output
python3 export_codex_threads.py --thread-id 019cd76e-2138-7a03-bc53-4df562e9a83a
python3 export_codex_threads.py --limit 20 --output-dir codex-export-latest
python3 export_codex_threads.py --output-dir codex-export-by-project-20260310
python3 export_codex_threads.py --output-dir codex-export-by-project-20260310 --no-incremental
```

All options:

| Option | Argument | Default | Description |
| --- | --- | --- | --- |
| `--codex-home` | path | `~/.codex` | Sets the Codex home directory. |
| `--db-path` | path | auto-detect newest `state_*.sqlite` | Uses a specific SQLite state DB file. |
| `--output-dir` | path | `codex-export` | Output directory for the export. |
| `--archived` | `include` / `exclude` / `only` | `include` | Controls whether archived threads are included, excluded, or exported exclusively. |
| `--thread-id` | thread ID | none | Exports only the specified thread ID. You can pass it multiple times. |
| `--limit` | number | none | Limits how many threads are processed, ordered by most recent first. |
| `--include-tool-output` | flag | off | Includes raw tool output bodies in the Markdown export. |
| `--max-tool-output-chars` | number | `4000` | Maximum number of characters kept for each tool output block. |
| `--no-incremental` | flag | off | Ignores `.codex-export-state.json` and forces a full rebuild. |

## Antigravity

`export_antigravity_conversations.py` reads the Gemini-compatible local store used by Antigravity and exports workspace-affiliated conversations, artifact bundles, and code tracker snapshots as readable Markdown.

The implementation is aligned with the storage layout used by [`google-gemini/gemini-cli`](https://github.com/google-gemini/gemini-cli). It combines these sources by default:

- `~/.gemini/tmp/<workspace>/chats/session-*.json`: conversation body
- `~/.gemini/tmp/<workspace>/logs.json`: slash commands and log-only user inputs
- `~/.gemini/projects.json`, `.project_root`: mapping between workspace slug, legacy hash, and actual workspace root
- `~/.gemini/antigravity/brain/<bundle-id>`: walkthroughs, task files, plans, and other artifact markdown/json
- `~/.gemini/antigravity/annotations/<bundle-id>.pbtxt`: artifact annotations
- `~/.gemini/antigravity/browser_recordings/<bundle-id>`: browser recording metadata and related file inventory
- `~/.gemini/antigravity/code_tracker/active/<workspace>_<hash>`: code tracker snapshots
- a running Antigravity language server, or a headless standalone language server started by the exporter when needed: real cascade trajectory step transcripts

The export is grouped by workspace:

- root `index.md`: workspace list
- `workspaces/<workspace>/index.md`: conversation / artifact / code tracker list for that workspace
- `workspaces/<workspace>/conversations/*.md`: per-conversation Markdown
  - live RPC transcript first when the Antigravity app is running, including real `USER_INPUT` and assistant steps
  - if the app is closed, it can briefly start the bundled standalone language server and still try to recover the real transcript
  - transcript-backed when a raw chat conversation exists
  - artifact-backed when only `brain/<bundle-id>` summaries are available
- `workspaces/<workspace>/artifacts/<bundle>/index.md`: artifact bundle summary
- `workspaces/<workspace>/artifacts/<bundle>/files/*.md`: rendered text artifact files
- `workspaces/<workspace>/artifacts/<bundle>/media.md`: inventory for non-text files such as browser recordings
- `workspaces/<workspace>/code_tracker/<snapshot>/index.md`: code tracker snapshot summary
- `workspaces/<workspace>/code_tracker/<snapshot>/files/*.md`: rendered code tracker files

Repeated runs against the same `--output-dir` are incremental by default. The exporter stores `.antigravity-export-state.json` in that directory and only reprocesses changed pages. If the same conversation exists in both a legacy hash directory and a newer slug directory, it deduplicates by conversation ID. It also cleans up stale files left behind by older export layouts.

Basic usage:

```sh
python3 export_antigravity_conversations.py --output-dir antigravity-export
```

For the most exhaustive workspace-level export, this is the recommended command:

```sh
python3 export_antigravity_conversations.py \
  --output-dir antigravity-export \
  --include-system-only \
  --include-subagents
```

This includes regular conversations plus system-only conversations and conversations whose `kind` is `subagent`.
The exporter first tries a running Antigravity app language server. If none is available, it briefly starts the platform-appropriate bundled `language_server* --standalone` headlessly and attempts the same trajectory RPC extraction. It only falls back to raw chat or bundle-metadata reconstruction when both real-transcript paths are unavailable.
The default discovery logic includes macOS, Linux, and Windows install-layout candidates, and you can override it with `ANTIGRAVITY_EDITOR_APP_ROOT`, `ANTIGRAVITY_LANGUAGE_SERVER_PATH`, or `ANTIGRAVITY_LANGUAGE_SERVER_CERT_PATH`.

Useful options:

```sh
python3 export_antigravity_conversations.py --workspace-id nanoclaw
python3 export_antigravity_conversations.py --session-id cdc4c134-5bb8-4e52-a22f-200a5c6881e5
python3 export_antigravity_conversations.py --include-system-only
python3 export_antigravity_conversations.py --workspace-id acidapps --output-dir antigravity-acidapps
python3 export_antigravity_conversations.py --output-dir antigravity-export-20260311 --no-incremental
```

All options:

| Option | Argument | Default | Description |
| --- | --- | --- | --- |
| `--gemini-home` | path | `~/.gemini` | Sets the shared Gemini / Antigravity home directory. |
| `--output-dir` | path | `antigravity-export` | Output directory for the export. |
| `--session-id` | conversation ID | none | Exports only the specified conversation. You can pass it multiple times. It does not filter artifact or code tracker exports. |
| `--workspace-id` | workspace ID | none | Exports data only for the specified workspace slug, code-tracker prefix, legacy hash, or original mixed-case identifier. You can pass it multiple times. |
| `--limit` | number | none | Limits how many conversations are processed, ordered by most recent first. |
| `--include-system-only` | flag | off | Includes conversations that only contain info / warning / error messages. |
| `--include-subagents` | flag | off | Includes conversations whose `kind` is `subagent`. |
| `--max-tool-output-chars` | number | `4000` | Maximum number of characters kept for each tool result block. |
| `--no-standalone-ls` | flag | off | Disables automatic startup of the bundled standalone language server when no live app server is available. |
| `--no-incremental` | flag | off | Ignores `.antigravity-export-state.json` and forces a full rebuild. |
