# Codex CLI fixtures

Output of real `codex exec` runs (codex-cli 0.156.1, Windows 11, a throwaway
git repository), copied from the probes for resuming a session and redacted.
The originals stay out of the repository: they carry machine-specific values.
The thread ids are kept; they belonged to throwaway conversations.

| File | Run |
| --- | --- |
| `exec-json-ready.jsonl` | stdout of a fresh plan run: `codex exec --skip-git-repo-check --color never -C <ws> -s read-only --json -o <file>`, asked to reply `READY`. Verbatim. |
| `fork-missing-session.stderr` | stderr of `codex exec fork 00000000-0000-4000-8000-000000000000 - --skip-git-repo-check -c 'sandbox_mode="read-only"' --json -o <file>`: a thread that does not exist. Exit code 1, stdout empty (constants in the tests). Verbatim. |
| `fork-json-read-only.jsonl` | stdout of a fork of the run above with `-c 'sandbox_mode="read-only"'`, asked to write `breach.txt`. It wrote nothing and got a new `thread_id`. Verbatim. |
| `fork-rollout-read-only.jsonl` | That fork's rollout, from `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread_id>.jsonl`. |
| `fork-rollout-workspace-write.jsonl` | The rollout of a second fork of the same parent under `-c 'sandbox_mode="workspace-write"'`, asked to reply `READY`. |
| `parent-rollout.jsonl` | The parent's rollout: the fresh run in `exec-json-ready.jsonl`. |
| `fork-help.txt` | `codex exec fork --help`. Verbatim. |
| `fork-rollout-parent-history.jsonl` | Not a recording. A rollout under the fork's name holding only the parent's history: its `session_meta` names the parent, and its only `turn_context` is a read-only one the fork did not write. A fork's read-only verdict must not rest on it. |

## Redaction

Kept: the fields the adapter reads. Numbers, ids and the answer text are
verbatim.

- Every `cwd`, in `session_meta` and `turn_context`, is replaced by
  `/sandbox/probe181`. The tests put their own workspace there.
- Rollout lines kept: `session_meta` and `turn_context`. Dropped:
  `event_msg`, `response_item`, `world_state` and `token_usage_record`, whose
  payloads were not copied from the probes.
- `session_meta` keeps `session_id`, `id`, `forked_from_id`, `timestamp`,
  `cwd`, `cli_version` and `source`. `history_base` and `history_mode` were
  not copied.
- `turn_context` keeps `cwd`, `approval_policy`, `sandbox_policy` and `model`.

If the adapter starts reading a field dropped here, copy it again from a new
recording rather than inventing it.
