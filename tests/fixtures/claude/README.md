# Claude CLI fixtures

Output of real `claude -p --output-format stream-json --verbose` runs
(claude 2.1.283, sonnet, with the adapter's read-only flags), copied from the
release gate for resuming a session and redacted. The originals stay out of
the repository: they carry machine-specific values.

| File | Run |
| --- | --- |
| `resume-rejected.stdout`, `resume-rejected.stderr` | `--resume=00000000-0000-4000-8000-000000000000 --fork-session`, a session that does not exist. Exit code 1 (a constant in the tests). |
| `resume-rejected-other-error.stdout` | The same line with a different `errors` entry, to show that only the missing-session sentence counts as a rejection. Not a recording. |
| `resume-write-probe.jsonl` | A forked, resumed session asked to write a file. It called no tool and wrote nothing. |

## Redaction

Kept: the fields the tests assert and the envelope the parser reads. Numbers
(usage, `total_cost_usd`), the `errors` sentence with the requested id, and
the answer text are verbatim.

- Every `session_id` is replaced: `bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb` in the
  rejected run, `aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa` in the write probe.
- `uuid` is dropped from every event.
- Write probe, lines kept: `system` (init), the `assistant` event holding the
  text block, and `result`. Dropped: `thinking_tokens` and `post_turn_summary`
  system events, the `assistant` event holding only a thinking block (and its
  signature), and `rate_limit_event`.
- `system` (init) keeps `type`, `subtype`, `cwd` (replaced by
  `/sandbox/gate137`), `session_id`, `tools`, `mcp_servers`, `model`,
  `permissionMode`, `apiKeySource`, `claude_code_version` and `output_style`.
  Installed commands, agents, skills, plugins, capabilities and local paths
  are dropped.
- `assistant` keeps `type`, `session_id` and `message.{model, type, role,
  content, usage}`; message and request ids are dropped.
- `result` keeps `type`, `subtype`, `is_error`, `num_turns`, `session_id`,
  `total_cost_usd`, `usage`, `modelUsage`, `result` and `duration_ms`.

If the adapter starts reading a field dropped here, copy it again from a new
recording rather than inventing it.
