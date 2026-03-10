# Codex Thread Export

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
