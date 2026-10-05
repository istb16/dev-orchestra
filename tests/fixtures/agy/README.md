# Antigravity CLI fixtures

Output of real `agy --output-format stream-json` runs (agy 1.2.16, Windows 11,
`--model claude-sonnet-4-6`, a throwaway directory holding `input.txt` with
three lines), redacted. The originals stay out of the repository: they carry
machine-specific values. Both runs left out `--dangerously-skip-permissions`.

| File | Run |
| --- | --- |
| `stream-json-tools.jsonl` | `agy --output-format stream-json --model claude-sonnet-4-6 -p "<prompt>"`, asked to read `input.txt` and write its lines reversed to `output.txt`. It used `view_file` and `write_to_file`, and wrote the file. Exit code 0. |
| `stream-json-denied.jsonl` | `agy --input-format stream-json --output-format stream-json --model claude-sonnet-4-6`, given on stdin the single line `{"event":"user","message":{"role":"user","content":"<prompt>"}}`, asked for the line count of `input.txt`. It chose `run_command`, which headless mode denied: the `result` carries `denied_actions`, and its `response` is empty. Exit code 0; stderr said a tool needed the `command` permission. |

The stdin message shape is not in `agy --help`; it was found from the CLI's
error messages (`stream input message is missing the "event" field`,
`stream input "user" message is missing the "message" field`, and a string
`message` refused as not a `StreamInputUserMessage`). An `event` other than
`user` is ignored with a warning.

## Event shapes

One JSON object per line, each with an `event` key and an object under the key
of the same name:

- `init`: `conversation_id`, `init.model`, `init.cwd`, `init.tools`,
  `init.permission_mode` (`request-review`).
- `step_update`: `step_index`, `state` (`ACTIVE` or `DONE`), `step_type`
  (`user_input`, `agent_response`, `tool`). A `tool` step prints once when it
  starts and once when it ends, with `tool_name` and `tool_info.name` and
  `tool_info.parameters` (`view_file`: `AbsolutePath`; `write_to_file`:
  `TargetFile`; `run_command`: `CommandLine`); the `DONE` line adds
  `duration_seconds`, and `view_file`'s adds `tool_info.output`. An
  `agent_response` step's `DONE` line carries that step's `usage`; the final
  answer streams as `text_delta` on `ACTIVE` lines first. Nothing is printed
  while the model thinks: up to six seconds between lines here.
- `result`: the same object `--output-format json` prints (`conversation_id`,
  `status`, `response`, `duration_seconds`, `num_turns`, `usage`, and
  `denied_actions` when any), nested under `result`.

## Redaction

Kept: every event, key and number, in order. The answer text is verbatim but
for the path inside it.

- `conversation_id` is replaced: `aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa` in
  `stream-json-tools.jsonl`, `bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb` in
  `stream-json-denied.jsonl`.
- The working directory is replaced by `C:\sandbox\agy193` (`C:/sandbox/agy193`
  inside the answer's link), wherever it appears.
- `init.tools` keeps six of the sixty names the CLI listed.
- The answer's `text_delta` chunks were joined, redacted, and cut again into
  as many chunks of equal length; the CLI's own cut points are lost.
