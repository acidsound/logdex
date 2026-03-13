# Conversation Exporters

이 저장소에는 두 개의 exporter 가 있습니다.

- `export_codex_threads.py`: Codex 로컬 스레드 export
- `export_antigravity_conversations.py`: Antigravity / Gemini CLI 로컬 workspace export

## Codex

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

## Antigravity

`export_antigravity_conversations.py` 는 Antigravity 가 Gemini CLI 와 공유하는 로컬 저장소를 읽어, 각 workspace 에 소속된 conversation, artifact bundle, code tracker snapshot 을 사람이 읽기 쉬운 Markdown 으로 내보냅니다.

구현은 `google-gemini/gemini-cli` 의 conversation 저장 구조를 기준으로 맞췄습니다. 기본적으로 아래 데이터를 함께 사용합니다.

- `~/.gemini/tmp/<workspace>/chats/session-*.json`: conversation 본문
- `~/.gemini/tmp/<workspace>/logs.json`: slash command / log-only user input 보강
- `~/.gemini/projects.json`, `.project_root`: workspace slug, legacy hash, 실제 workspace root 매핑 복원
- `~/.gemini/antigravity/brain/<bundle-id>`: walkthrough, task, plan, 기타 artifact markdown/json
- `~/.gemini/antigravity/annotations/<bundle-id>.pbtxt`: artifact annotation
- `~/.gemini/antigravity/browser_recordings/<bundle-id>`: browser recording metadata 와 관련 파일 inventory
- `~/.gemini/antigravity/code_tracker/active/<workspace>_<hash>`: code tracker snapshot
- 실행 중인 Antigravity language server, 또는 필요할 때 exporter 가 headless 로 띄운 standalone language server: 실제 cascade trajectory step transcript

내보내기 결과는 workspace 기준으로 묶입니다.

- 루트 `index.md`: workspace 목록
- `workspaces/<workspace>/index.md`: 해당 workspace 의 conversation / artifact / code tracker 목록
- `workspaces/<workspace>/conversations/*.md`: conversation 별 Markdown 문서
  - Antigravity 앱이 실행 중이면 live RPC transcript 를 우선 사용해서 실제 `USER_INPUT` / assistant step 을 export
  - 앱이 꺼져 있어도 bundled standalone language server 를 잠깐 띄워 real transcript 를 시도
  - raw chat store 에 conversation 이 있으면 transcript 기반으로 export
  - chat transcript 가 없더라도 `brain/<bundle-id>` artifact summary 를 기반으로 artifact-backed conversation 을 추가 생성
- `workspaces/<workspace>/artifacts/<bundle>/index.md`: artifact bundle 요약
- `workspaces/<workspace>/artifacts/<bundle>/files/*.md`: artifact 텍스트 파일 본문
- `workspaces/<workspace>/artifacts/<bundle>/media.md`: browser recording 등 비텍스트 파일 inventory
- `workspaces/<workspace>/code_tracker/<snapshot>/index.md`: code tracker snapshot 요약
- `workspaces/<workspace>/code_tracker/<snapshot>/files/*.md`: code tracker 파일 본문

같은 `--output-dir` 로 반복 실행하면 기본적으로 증분 export 로 동작합니다. exporter 는 해당 디렉터리에 `.antigravity-export-state.json` 을 저장하고, 바뀐 page 만 다시 처리합니다. 해시 디렉터리와 slug 디렉터리에 같은 conversation 이 중복 저장된 경우에는 conversation ID 기준으로 하나만 남기고, 이전 export 형식에서 남은 stale 파일도 정리합니다.

기본 사용법:

```sh
python3 export_antigravity_conversations.py --output-dir antigravity-export
```

가장 세세하게 workspace 전체를 훑고 싶다면 아래 실행을 권장합니다.

```sh
python3 export_antigravity_conversations.py \
  --output-dir antigravity-export \
  --include-system-only \
  --include-subagents
```

이 조합은 일반 conversation 외에 user/gemini 메시지가 없는 system-only conversation 과 `kind=subagent` conversation 까지 함께 포함합니다.
또한 exporter 는 먼저 실행 중인 Antigravity app language server 에 붙고, 그게 없으면 플랫폼에 맞는 bundled `language_server* --standalone` 를 headless 로 잠깐 띄워 실제 trajectory transcript 를 시도합니다. 두 경로 모두 실패한 conversation 에 대해서만 raw chat 또는 bundle metadata 기반 fallback 으로 내려갑니다.
기본 후보 경로는 macOS / Linux / Windows 용으로 나뉘며, 필요하면 `ANTIGRAVITY_EDITOR_APP_ROOT`, `ANTIGRAVITY_LANGUAGE_SERVER_PATH`, `ANTIGRAVITY_LANGUAGE_SERVER_CERT_PATH` 환경변수로 override 할 수 있습니다.

유용한 옵션 예시:

```sh
python3 export_antigravity_conversations.py --workspace-id nanoclaw
python3 export_antigravity_conversations.py --session-id cdc4c134-5bb8-4e52-a22f-200a5c6881e5
python3 export_antigravity_conversations.py --include-system-only
python3 export_antigravity_conversations.py --workspace-id acidapps --output-dir antigravity-acidapps
python3 export_antigravity_conversations.py --output-dir antigravity-export-20260311 --no-incremental
```

전체 옵션:

| 옵션 | 인자 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `--gemini-home` | 경로 | `~/.gemini` | Gemini / Antigravity 가 공유하는 홈 디렉터리를 지정합니다. |
| `--output-dir` | 경로 | `antigravity-export` | export 결과를 저장할 디렉터리입니다. |
| `--session-id` | conversation ID | 없음 | 특정 conversation 만 export 합니다. 여러 번 지정할 수 있습니다. artifact/code tracker 범위에는 영향을 주지 않습니다. |
| `--workspace-id` | workspace ID | 없음 | 특정 workspace(slug, code-tracker prefix, legacy hash, 또는 원래 mixed-case 식별자) 의 데이터를 export 합니다. 여러 번 지정할 수 있습니다. |
| `--limit` | 숫자 | 없음 | 최신순으로 최대 몇 개 conversation 까지 처리할지 제한합니다. |
| `--include-system-only` | 플래그 | 꺼짐 | user/gemini 메시지가 없는 info/warning/error 전용 conversation 도 포함합니다. |
| `--include-subagents` | 플래그 | 꺼짐 | subagent kind conversation 도 포함합니다. |
| `--max-tool-output-chars` | 숫자 | `4000` | 각 tool result 블록에서 유지할 최대 문자 수입니다. |
| `--no-standalone-ls` | 플래그 | 꺼짐 | 실행 중인 앱 language server 가 없을 때 bundled standalone language server 를 자동으로 띄우지 않습니다. |
| `--no-incremental` | 플래그 | 꺼짐 | `.antigravity-export-state.json` 을 무시하고 전체를 다시 생성합니다. |
