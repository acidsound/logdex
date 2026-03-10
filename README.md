# Codex 스레드 내보내기

`export_codex_threads.py` 는 `~/.codex` 아래의 로컬 Codex 상태 데이터베이스를 읽고, 각 스레드의 `rollout_path` 를 해석하여 스레드마다 하나의 Markdown 파일을 생성합니다.

내보내기 결과는 프로젝트별로 묶입니다.

- 루트 `index.md`: 프로젝트 목록
- `projects/<project>/index.md`: 해당 프로젝트의 스레드 목록
- `projects/<project>/*.md`: 스레드별 Markdown 문서

스레드 안에 tool call / tool output 으로부터 복원 가능한 `git commit` 또는 `git push` 활동이 있으면, 해당 스레드 Markdown 상단에 `Git Activity` 요약 표가 추가됩니다.

같은 `--output-dir` 로 반복 실행하면 기본적으로 증분 export 로 동작합니다. exporter 는 해당 디렉터리에 `.codex-export-state.json` 을 저장하고, SQLite 의 `updated_at` 값이 바뀐 스레드만 다시 처리합니다. 전체를 강제로 다시 생성하려면 `--no-incremental` 옵션을 사용하면 됩니다.

## `--thread-id` 확인 방법

Codex 앱에서는 현재 스레드의 session ID(thread ID) 를 직접 복사할 수 있습니다.

1. 현재 스레드 화면에서 `...` 메뉴를 누른 뒤 session ID 를 복사합니다.

2. 단축키 `cmd+option+C` 로 현재 스레드의 session ID 를 복사합니다.

3. UI 대신 로컬 데이터에서 확인하려면 최근 스레드 목록을 SQLite 에서 조회할 수 있습니다:

```sh
sqlite3 -header -column ~/.codex/state_5.sqlite "select id, title, updated_at from threads order by updated_at desc limit 20;"
```

여기서 `title` 을 보고 원하는 스레드를 찾은 뒤 `id` 값을 `--thread-id` 로 사용하면 됩니다.

4. rollout 파일명에서 직접 확인할 수도 있습니다:

```sh
find ~/.codex/sessions -type f | sort | tail -n 20
```

rollout 파일명은 보통 아래 형태이며, 마지막 UUID 부분이 thread ID 입니다.

```text
rollout-2026-03-10T20-07-24-019cd76e-2138-7a03-bc53-4df562e9a83a.jsonl
```

5. 이미 export 를 한 뒤라면, 생성된 스레드 Markdown 상단의 `Metadata` 섹션에 `Thread ID` 가 들어 있습니다.

기본 사용법:

```sh
python3 export_codex_threads.py --output-dir codex-export
```

유용한 옵션 예시:

```sh
python3 export_codex_threads.py --archived include --include-tool-output
python3 export_codex_threads.py --thread-id 019cd76e-2138-7a03-bc53-4df562e9a83a
python3 export_codex_threads.py --limit 20 --output-dir codex-export-latest
python3 export_codex_threads.py --output-dir codex-export-by-project-20260310
python3 export_codex_threads.py --output-dir codex-export-by-project-20260310 --no-incremental
```

전체 옵션:

| 옵션 | 인자 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `--codex-home` | 경로 | `~/.codex` | Codex 홈 디렉터리를 지정합니다. |
| `--db-path` | 경로 | 최신 `state_*.sqlite` 자동 선택 | 특정 SQLite 상태 DB 파일을 직접 지정합니다. |
| `--output-dir` | 경로 | `codex-export` | export 결과를 저장할 디렉터리입니다. |
| `--archived` | `include` / `exclude` / `only` | `include` | 보관된 스레드를 포함할지, 제외할지, 보관본만 내보낼지 지정합니다. |
| `--thread-id` | 스레드 ID | 없음 | 특정 thread ID 만 export 합니다. 여러 번 지정할 수 있습니다. |
| `--limit` | 숫자 | 없음 | 최신순으로 최대 몇 개 스레드까지 처리할지 제한합니다. |
| `--include-tool-output` | 플래그 | 꺼짐 | tool output 본문까지 Markdown 에 포함합니다. |
| `--max-tool-output-chars` | 숫자 | `4000` | 각 tool output 블록에서 유지할 최대 문자 수입니다. |
| `--no-incremental` | 플래그 | 꺼짐 | `.codex-export-state.json` 을 무시하고 전체를 다시 생성합니다. |
