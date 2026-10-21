<!-- translated-from: references/json.md sha256:dff8715874cc5a162bffbfb7610ff6eef9c3f2e03ad193131be773b5b44500bc -->

> この文書は [references/json.md](../../../references/json.md) の日本語訳です。内容が食い違うときは英語版が正です。

<a id="json-output"></a>

# `--json` の出力

<!-- contents: start -->

**目次**

- [表の読み方](#reading-the-tables)
- [設定と準備](#configuration-and-setup)
  - [`config show`](#config-show)
  - [`config validate`](#config-validate)
  - [`config suggest-roles`](#config-suggest-roles)
  - [`model list`](#model-list)
  - [`reviewer list`](#reviewer-list)
  - [`doctor`](#doctor)
  - [`hooks status`](#hooks-status)
- [実行とジョブ](#runs-and-jobs)
  - [`run`](#run)
  - [`jobs list`](#jobs-list)
  - [`jobs show`](#jobs-show)
  - [`jobs wait`](#jobs-wait)
- [レビュー](#reviews)
  - [`review snapshot`](#review-snapshot)
  - [`review run`](#review-run)
  - [`review consolidate`](#review-consolidate)
  - [`review show`](#review-show)
  - [`review status`](#review-status)
  - [`design approve`](#design-approve)
  - [`design questions show`](#design-questions-show)
- [ワークフローの状態](#workflow-state)
  - [`status`](#status)
  - [`state show`](#state-show)
  - [`budget show`](#budget-show)
  - [`tokens show`](#tokens-show)
  - [`optimization report`](#optimization-report)
  - [`progress record`](#progress-record)
  - [`workflow list`](#workflow-list)
  - [`workflow show`](#workflow-show)
  - [`summary`](#summary)

<!-- contents: end -->

各コマンドが `--json` で出すものを 1 か所にまとめます。標準出力に出る値の型、そのトップレベルのキー、決まった値のどれかを取るキーの値です。対象は、[cli](cli.md) が `--json` を取ると挙げているコマンドです。コマンドが何をするか、文章の出力に何が出るかは cli にあります。

このうち何を約束するかは README の「互換性」にあります。キーは追加されることがありますが、消したり、名前を変えたり、意味や型を変えたりはしません。決まった値のどれかを取るキーについてここに挙げた値は保ちます。`notes` と `warnings` の中の文は文章であり、対象外です。

<a id="reading-the-tables"></a>

## 表の読み方

- 標準出力に出るのは JSON の値 1 つで、ほかのものをすべて標準エラーに出したあとに出します。見出しのすぐ下の行が **List**（項目ごとに 1 つのエントリ、それぞれオブジェクト）か **Mapping**（名前をキーにしたオブジェクトで、値はそれぞれオブジェクト）と言っていなければ、**オブジェクト**です。リストとマッピングでは、表は各エントリのキーを挙げます。
- 意味が **Only** で始まるキーは、そのときだけ出ます。ほかのキーは、表の下の文が例外を挙げていない限り、いつも出ます。`tests/test_json_keys.py` は各コマンドをこの表に照らして確かめます。いつも出るキーはすべて出ていること、テストが出ていると見たキーはすべてここに挙がっていることです。
- 型: `string`（文字列）、`integer`（整数）、`number`（整数または小数）、`boolean`（真偽値）、`object`（オブジェクト）、`list`（リスト）、そしてキーが空になりうるときの `null`。パスは `string` で、意味に相対と書いていなければ絶対パスです。時刻は UTC の `string` で、`2026-10-12T04:31:27Z` の形です。
- 入れ子のオブジェクトのキーは、短く挙げられるものはここに挙げます。それ以外は [cli](cli.md) のそのコマンドの節か、ここからリンクした文書にあります。パスの横の `*_real` キー（`global_real`、`path_real`、`record_real` など）は、Windows の Microsoft Store の Python のもとでのように、ファイルが実際には別の場所にあるときだけ出ます（[configuration](configuration.md)）。
- 報告するものができる前に断られたコマンド（使い方の誤り、断られた実行やラウンド）は、標準出力に何も出しません。理由は終了コードと標準エラーで分かります。

<a id="configuration-and-setup"></a>

## 設定と準備

<a id="config-show"></a>

### `config show`

Object.

| キー | 型 | 意味 |
| --- | --- | --- |
| `source` | string | 何を表示しているか。`effective (project: <path or none>, global: <path or none>)`、または `--scope` のレイヤーのファイル。 |
| `config` | object | 設定。マージしたもの、`--scope` のときはそのレイヤーのディスク上のとおりのもの。 |
| `providers` | object | 設定が参照するすべての provider をキーにし、それぞれどこから来ているか `{"kind", "path"}`（`kind` は `builtin` か `user`）、adapter のない名前は `null`。 |
| `preset` | object | 有効なプリセット。`name`（ないときは `null`）、`source`（`implicit`、`global`、`invalid`）、`notes`（文字列のリスト。フィットし直した点）。 |
| `project` | string or null | 有効なプロジェクトファイル。`--scope` では出ません。 |
| `global` | string or null | 有効なグローバルファイル。`--scope` では出ません。 |
| `reviewer_origins` | list of strings | コードのパネルの各席がどこに書かれたか（パネルの順）。`fit`、`global`、`project`、`global extra`、`project extra`。`--scope` では出ません。 |
| `design_reviewer_origins` | list of strings | Only: effective の表示で、フィットかファイルが design パネルを設定しているときだけ。その各席がどこに書かれたか（`fit design`、`global design`、`project design`、`global design extra`、`project design extra`）。コードのパネルから写した席はその席のラベル。 |
| `source_real` | string | Only: `--scope global` で、ファイルが別の場所にあるときだけ。 |
| `global_real` | string | Only: effective の表示で、グローバルファイルが別の場所にあるときだけ。 |

<a id="config-validate"></a>

### `config validate`

Object.

| キー | 型 | 意味 |
| --- | --- | --- |
| `valid` | boolean | 有効な設定が正しいかどうか。正しくなければ終了コードは 1。 |
| `problems` | list of strings | 正しくない理由。 |
| `warnings` | list of strings | 正しくなくはしないが、何かを断るか無視させるもの。[cli](cli.md#config) の `config validate` を参照。 |

<a id="config-suggest-roles"></a>

### `config suggest-roles`

Object. 注記は標準エラーにも出ます。

| キー | 型 | 意味 |
| --- | --- | --- |
| `root` | string | 一覧のルート。リポジトリ、またはリポジトリの外ならそのディレクトリ。 |
| `source` | string | ファイルの一覧の取り方。`git`（`git ls-files`）か `walk`。 |
| `files` | integer | 一覧にしたファイルの数。 |
| `truncated` | boolean | 一覧が上限で止まったかどうか。 |
| `notes` | list of strings | 一覧が自分について言うこと。 |
| `suggestions` | list | 提案するレビュアーごとに 1 つのオブジェクト。`role`、`id`、`provider`、`family`、`paths`（その `when.paths`）、`evidence`（文字列のリスト）、`matches`、`withheld_matches`（整数）。 |
| `skipped` | list | 提案しなかったロールごとに 1 つのオブジェクト。`role` と `reason`。 |
| `written` | string or null | `--write` が追記したプロジェクトファイル。`--write` がなければ `null`。 |

<a id="model-list"></a>

### `model list`

Mapping. provider 名をキーにし、知っているすべての provider（`--provider` のときはその 1 つ）。adapter が例外を送出したときは終了コードが 1 です。

| キー | 型 | 意味 |
| --- | --- | --- |
| `installed` | boolean | その CLI が PATH にあるかどうか。 |
| `version` | string or null | CLI のバージョン。入っていないか読めなかったときは `null`。 |
| `models` | list | 公開しているモデル。入っていなければ `[]`。`value`（CLI に渡すもの）、`family`、`label`、`source`（`cli-help`、`cli-catalog`、`cli-config`、`cli-default`、`builtin-fallback`）。 |
| `families` | list | モデルが日付入りの id である CLI について、設定に書く family ごとの `{"family", "resolves_to"}`。それ以外は `[]`。 |
| `fallback_updated` | string or null | adapter の予備の一覧を最後に確かめた日。adapter が先に例外を送出したときは `null`。 |
| `adapter_error` | string or null | adapter が送出した例外。正常に動いたときは `null`。 |
| `origin` | object or null | `{"kind", "path"}`。`kind` は `builtin` か `user`、`path` はユーザーモジュール。 |

<a id="reviewer-list"></a>

### `reviewer list`

List. コードのパネル（`--design` では design パネル）の席ごとに 1 つのエントリ、パネルの順。

| キー | 型 | 意味 |
| --- | --- | --- |
| `id` | string | 席の id。 |
| `provider` | string | その provider。 |
| `model` | object | 設定どおりのモデル。`family`、`version`、固定したときは `id`。 |
| `role` | string | そのロール。 |
| `origin` | string | どこに書かれたか。`config show` の `reviewer_origins` と同じ名前。 |

席は、設定にあるほかのキー（`when`、`high_risk_model`、`relevance`、`options`）もすべて持ちます。

<a id="doctor"></a>

### `doctor`

Object. `--strict` は `problems` が空でなければ 1 で終わります。

| キー | 型 | 意味 |
| --- | --- | --- |
| `platform` | object | `system`、`release`、`python`。Microsoft Store の Python では `store_python: true`。 |
| `providers` | object | provider 名をキーにし、その検出結果、`origin`、`read_only_enforcement`、`resume_support`、`live_check`、`adapter_error` など。[doctor](cli.md#doctor) を参照。 |
| `config` | object | `status`（`ok`、`invalid`、`error`）、有効なファイル（`global`、`project_override`）、`preset`、`problems`、`warnings`、`loosened`。 |
| `roles` | object | `orchestrator`、`architect`、`implementer`、`review_fixer` をキーにし、各席の provider、モデル、`status`（`ok`、`missing`、`unknown-provider`、`cli-missing`、`unresolvable-model`、`adapter-error`）。 |
| `reviewers` | list | コードのパネル。各席はロールと同じ形で、`id`、`role`、`origin`、当てはまるときは `when`、`condition`、`paths`、`high_risk_model`、`relevance`、`read_only`。 |
| `problems` | list of strings | おかしいところ。 |
| `notes` | list of strings | 知っておくとよいが、おかしくはないこと。`--strict` は数えません。 |
| `user_providers` | object | ユーザー adapter のディレクトリ。`directory`、`enabled`、`present`、`loaded`（`name`、`path`、`module`）、`errors`（`path`、`error`: 読み込みに失敗したファイル）、`problems`（`path`、`name`、`problem`: 読み込まれたが、改名していない `_resolve_latest` のように動かない部分のある adapter）。 |
| `antigravity` | object | `root`、`live`（このチェックアウトである導入先）、`autoload`（Antigravity がそこから読み込むもの）。 |
| `design_panel_source` | string | design パネルがどこから来るか。`fit`、`code`、`global`、`project`。 |
| `design_reviewers` | list | design パネル。`reviewers` と同じように診断します。コードのパネルから写した席は `id`、`role`、`origin` だけ。 |
| `language` | object | `reply`（タグか `null`）、`rewrite`、`layer`（`project`、`global`、`default`）、`check`（`script`、`words`、`latin`、`none`）、`hosts`（`claude` は `hooks status --json` が出すもの、`codex` と `agy` は `status: not-enforced`）。 |

設定を読めないときは診断を早めに終えます。`config` は `{"status": "error", "error", "global", "project_override"}` になり、`roles` と `reviewers` は空で、`design_panel_source`、`design_reviewers`、`language` は出ません。

<a id="hooks-status"></a>

### `hooks status`

Object. 状態にかかわらず終了コードは 0 です。

| キー | 型 | 意味 |
| --- | --- | --- |
| `settings_path` | string | 見た Claude Code の設定ファイル。 |
| `status` | string | `installed`、`stale`、`not-installed`、`unreadable`。 |
| `reasons` | list of strings | 古い理由。`python-differs`、`python-missing`、`relay-missing`、`relay-outdated`、`plugin-root-differs`（記録が別のチェックアウトを指す）、`record-differs`（記録がないか、別の設定ディレクトリや設定ファイルを指す）、`events-missing`（あるイベントにこちらのフックがないか 2 つ以上ある）、`entries-differ`（`install` が書く形の専用グループに入っていない。マッチャー・タイムアウト・引数が違う、または別のイベントにある）のどれか。 |
| `python` | string or null | エントリが実行するコマンド。 |
| `relay` | string | relay スクリプトのある場所、または置かれる場所。 |
| `plugin_root` | string or null | relay の記録が指すチェックアウト。 |
| `events` | list of strings | こちらのフックがあるイベント。 |
| `hooks_disabled` | boolean | 設定が `disableAllHooks` を設定しているかどうか。 |
| `error` | string or null | 設定ファイルを読めなかった理由。 |

<a id="runs-and-jobs"></a>

## 実行とジョブ

<a id="run"></a>

### `run`

Object. 実行のあとの結果で、キーとそれが入る条件は終わったジョブの記録と同じです。`--detach` のときは代わりに開始したジョブを出します（`jobs show` を参照）。`--print-command` のときはコマンド行を文章で出します。終了コードは `--json` がないときと同じです。

| キー | 型 | 意味 |
| --- | --- | --- |
| `stage` | string | 実行したロール。 |
| `status` | string | `succeeded` か `failed`。実行がどうだったか。 |
| `exit_code` | integer or null | CLI の終了コード。 |
| `stalled` | boolean | 出力がなくて止められたかどうか。 |
| `timed_out` | boolean | 全体の期限で止められたかどうか。 |
| `duration_seconds` | number | 実行にかかった時間。 |
| `model` | string or null | 解決したとおりの、実行したモデル。 |
| `session_id` | string or null | CLI が報告したときの、そのセッション id。 |
| `output` | string | Only: `--output` がないときだけ。モデルの答え（ジョブでは `.out` ファイルに残ります）。 |
| `answered` | boolean | `succeeded` で何か出力した実行だけ `true`。 |
| `error` | string | Only: 失敗して何か出力したときだけ。標準エラーの始めの部分。 |
| `resume` | object | Only: `--resume` のときだけ。`requested`、`mode`（`resumed` か `fresh`）、`resumed_from`、`reason`（[cli](cli.md#revising-the-plan-in-the-architects-own-session---resume) にある決まった文のどれか）、`outcome`、信頼して再開した実行では `trust: "newer"`。 |
| `warnings` | list of strings | Only: adapter に実行の警告があったときだけ。 |
| `suspended_seconds` | number | Only: 実行中にマシンがスリープしたときだけ。 |
| `output_written` | boolean | Only: `--output` への書き込みを断ったときだけ。`false`。 |
| `output_target` | string | Only: `--output` への書き込みを断ったときだけ。そのまま残したファイル。 |
| `rejected_file` | string or null | Only: `--output` への書き込みを断ったときだけ。実行が出したものを残した場所。何も残さなかったときは `null`。 |

<a id="jobs-list"></a>

### `jobs list`

List. 記録されたジョブごとに 1 つのエントリ、新しい順。それぞれ、`jobs show` が `activity` なしで出すジョブの記録です。

| キー | 型 | 意味 |
| --- | --- | --- |
| `id` | string | ジョブ id。 |
| `stage` | string | 実行するロール。 |
| `status` | string | `starting`、`running`、`succeeded`、`failed`、`cancelled`、`abandoned`。 |
| `started_at` | string | 開始した時刻。 |
| `output_file` | string | 実行の答えを書く場所。 |

<a id="jobs-show"></a>

### `jobs show`

Object. ジョブの記録です。

| キー | 型 | 意味 |
| --- | --- | --- |
| `id` | string | ジョブ id。 |
| `stage` | string | 実行するロール。 |
| `status` | string | `jobs list` と同じ。 |
| `started_at` | string | 開始した時刻。 |
| `output_file` | string | 実行の答えを書く場所。 |
| `activity` | object | Only: ジョブに activity のファイルができてからだけ。`count`、`context_tokens`、`elapsed_seconds`、`lines`（`n`、`s`、`line`）、`dropped`、`omitted`、`quiet_seconds`（新しいツール行が出ていない秒数。ジョブが終わった後は `null`）、`may_be_stuck`（`quiet_seconds` が `progress.quiet_notice_seconds` に達したか。`0` で止まります）。[jobs](cli.md#jobs) を参照。 |

記録には、ジョブが進むにつれて書いたもの（`command`、`timeout_seconds`、`prompt_file`、`force`、`pid`、`claimed_at`、`finished_at`）と、終わったあとは `run --json` が持つ結果のキー（`output` と `answered` を除く）も入ります。

<a id="jobs-wait"></a>

### `jobs wait`

Object. ジョブが終わるか待ちが終わったときの、`jobs show` が出すとおりのジョブの記録です。

| キー | 型 | 意味 |
| --- | --- | --- |
| `id` | string | ジョブ id。 |
| `stage` | string | 実行するロール。 |
| `status` | string | `jobs list` と同じ。 |
| `started_at` | string | 開始した時刻。 |
| `output_file` | string | 実行の答えを書く場所。 |
| `waited_out` | boolean | Only: ジョブがまだ動いているうちに待ちが終わったときだけ。`true`（終了コード 4）。 |
| `activity` | object | Only: ジョブに activity のファイルができてからだけ。`jobs show` と同じ。 |

<a id="reviews"></a>

## レビュー

<a id="review-snapshot"></a>

### `review snapshot`

Object. `reviews/review-target.json` が持つスナップショットのメタデータと、サイズの上限についての 3 つのキーです。

| キー | 型 | 意味 |
| --- | --- | --- |
| `generated_at` | string | 取った時刻。 |
| `round_id` | string | スナップショットごとに新しい id。 |
| `strategy` | string | diff の取り方。`git diff HEAD` など。 |
| `base` | string or null | 渡した `--base`。なければ `null`。 |
| `head` | string | `HEAD` があったコミット。 |
| `base_commit` | string | diff を取った基準のコミット。 |
| `tree` | string | git の tree オブジェクトとしての作業ツリー。 |
| `incremental_from` | string | このラウンドが修正だけに絞る元のラウンドの tree。全体のスナップショットでは `""`。 |
| `full_diff` | string | 差分だけのラウンドでの変更全体の diff。それ以外は `""`。 |
| `files` | list of strings | レビュアーに見せるファイル。 |
| `changed_paths` | list of strings | 変更が触れるすべてのパス。送らないファイルと改名元も含みます。 |
| `condition_paths` | list of strings | パスで絞り込むレビュアーを照らし合わせるパス。 |
| `untracked_included` | list of strings | diff に入れた未追跡のファイル。 |
| `withheld` | list | diff に入れなかったファイル。それぞれ `path` と、一致した `pattern` か読まなかった `reason`。 |
| `exclude_patterns` | list of strings | 有効な `review.exclude` のパターン。 |
| `bytes` | integer | diff のバイト数。 |
| `lines_added` | integer | 追加した行数。 |
| `lines_deleted` | integer | 削除した行数。 |
| `sha256` | string | diff の sha256。 |
| `empty` | boolean | 何も変わっていないかどうか（終了コード 1）。 |
| `change_chars` | integer | `review.context.max_chars` が測る大きさ。 |
| `max_chars` | integer | `review.context.max_chars`。 |
| `over_context` | boolean | `change_chars` がそれを超えているかどうか。 |
| `surrounding` | object | Only: 囲んでいるシンボルを凍結したときだけ。凍結したものと、取り出さなかったもの。 |

<a id="review-run"></a>

### `review run`

Object.

| キー | 型 | 意味 |
| --- | --- | --- |
| `ok` | integer | `ok` で戻ったレビュアーの数。 |
| `failed` | integer | 失敗したレビュアーの数。 |
| `partial` | integer | 変更をファイルで渡されたレビュアーの数。 |
| `reviewers` | list | 実行したレビュアーごとに 1 つのエントリ。`id`、`provider`、`role`、`model`、`status`（`ok`、`partial`、`failed`、`stalled`、`unparsed`）、`error`、`duration_seconds`、`findings`、`report`、`delivery`（`inline` か `file`）、`change_chars`、`inline_chars`、`snapshot`、`over_budget`、`budget_chars`、`usage`、当てはまるときは `model_slot`、`warnings`。 |
| `counts` | object | まとめたレポートの数。`review show` と同じ。 |
| `optimization` | object | ラウンドが決めたこと。コード: `requested_level`、`level`（`aggressive`、`balanced`、`quality`）、`escalated`、`high_risk`、`gate`（`allow`、`warn`、`refuse`）、`test_status`、`max_findings`、`reviewer_limit`、`files`、`lines`、`conditional`、`declared`。`--design`: `level`、`high_risk`、`declared`、`conditional`、`files`。 |
| `plan` | string | Only: `--design` のときだけ。レビューした plan。 |
| `surrounding` | object | Only: `review.context.surrounding: enclosing` が有効なコードのラウンドだけ。取り入れた文脈と外した文脈。 |
| `measurement` | object | Only: `--surrounding` のときだけ。`surrounding`、`snapshot`、`tree`、`head`、`base`、`workflow`、`epoch`、`rerun`、`inputs`。 |

断られたラウンド（終了コード 3）は標準出力に何も出しません。

<a id="review-consolidate"></a>

### `review consolidate`

Object. `consolidated.json` が持つとおりの、まとめたレポートです。

| キー | 型 | 意味 |
| --- | --- | --- |
| `generated_at` | string | 作った時刻。 |
| `iteration` | integer | ラウンド。 |
| `lineage` | string | ラウンドを数える単位のワークフロー、ブランチ、基準。 |
| `snapshot` | object | レビューしたもの。`sha256`、`files`、`round_id`、`over_budget`、`budget_chars`。 |
| `coverage` | object | `round` と `change`（それぞれ `complete`、`unverified`、`none`）、`unverified_since`、`change_chars`、`inline_chars`。[カバレッジ](reviews.md#coverage) を参照。 |
| `reviewers` | list | 各レビュアーのエントリ。`review run` と同じ。 |
| `counts` | object | `findings_total`、`by_severity`、`duplicates_merged`、`duplicate_candidates`、`reviewers_total`、`reviewers_ok`、`reviewers_partial`、`reviewers_failed`、そしてその 4 つに `snapshot_` を付けたもの。 |
| `duplicate_candidates` | list | 同じかもしれない指摘の組。 |
| `findings` | list | 指摘ごとに 1 つのエントリ。`id`、`key`、`severity`（`critical`、`high`、`medium`、`low`）、`file`、`line`、`category`、`problem`、`impact`、`evidence`、`recommended_fix`、`reported_by`、`duplicate_count`、`triage`（`needs-triage`、`accepted`、`rejected`、`duplicate`、`needs-investigation`）、`triage_note`、トリアージしたあとは `triage_set_at`。 |
| `surrounding` | object | Only: ラウンドが周りの文脈を運んだときだけ。 |
| `measurement` | object | Only: `review run --surrounding` のあとだけ。`triage_at_build` を含みます。 |

<a id="review-show"></a>

### `review show`

Object. `review consolidate` と同じ、まとめたレポートです。`--accepted` のときは、`findings` は受け入れたものだけです。

| キー | 型 | 意味 |
| --- | --- | --- |
| `generated_at` | string | 作った時刻。 |
| `iteration` | integer | ラウンド。 |
| `lineage` | string | ラウンドを数える単位のワークフロー、ブランチ、基準。 |
| `snapshot` | object | `review consolidate` と同じ。 |
| `coverage` | object | `review consolidate` と同じ。 |
| `reviewers` | list | `review consolidate` と同じ。 |
| `counts` | object | `review consolidate` と同じ。 |
| `duplicate_candidates` | list | `review consolidate` と同じ。 |
| `findings` | list | `review consolidate` と同じ。 |
| `surrounding` | object | Only: ラウンドが周りの文脈を運んだときだけ。 |
| `measurement` | object | Only: `review run --surrounding` のあとだけ。 |

<a id="review-status"></a>

### `review status`

Object. `--design` のときは、下に挙げたコードの 2 つのキーの代わりに、ラウンドの予算が `max_iterations`、最後の手当てが `final_revision` になります。

| キー | 型 | 意味 |
| --- | --- | --- |
| `iteration` | integer | ラウンド。 |
| `max_review_iterations` | integer | ラウンドの予算。`--design` では `max_iterations`。 |
| `blocking` | list of strings | 解決していないブロッキングの指摘の id。 |
| `blocking_count` | integer | その数。 |
| `accepted_count` | integer | 受け入れた指摘の数。 |
| `re_review_recommended` | boolean | もう 1 ラウンドが必要で、予算の内かどうか。 |
| `iteration_budget_exhausted` | boolean | ラウンドの予算を使い切ったかどうか。 |
| `coverage` | object or null | `review consolidate` と同じ。持たないレポートでは `null`。 |
| `reviewers_partial` | integer | このスナップショットのレビュアーのうち、変更をファイルで渡された数。 |
| `over_budget` | boolean | `--force` が `review.context.max_chars` を超えさせたから走ったラウンドかどうか。 |
| `counts` | object | `review consolidate` と同じ。 |
| `final_fix` | string or null | 予算を使い切ったあとの、最後のラウンドの修正の状態。`pending`、`retest`、`done`、`blocked`、`unaccepted`。それまでは `null`。`--design` では `final_revision` で、`pending`、`done`、`blocked`、`approved`、`implemented`、`unaccepted`。 |
| `final_fix_pending` | boolean | その手当てがまだ残っているかどうか。`--design` では `final_revision_pending`。 |
| `surrounding` | object | Only: ラウンドが周りの文脈を運んだときだけ。 |
| `enabled` | boolean | Only: `--design` のときだけ。この工程を実行するかどうか。 |
| `mode` | string | Only: `--design` のときだけ。`on`、`off`、`auto`。 |
| `reason` | string or null | Only: `--design` のときだけ。`auto` がそう決めた理由。`on` と `off` では `null`。 |
| `reviewers` | list of strings | Only: `--design` のときだけ。design パネルの id。 |
| `panel_source` | string | Only: `--design` のときだけ。`fit`、`code`、`global`、`project`。 |
| `optimization` | object | Only: `--design` のときだけ。次の design ラウンドが決めること。 |

<a id="design-approve"></a>

### `design approve`

Object. `state.json` が `design_approval` に持つとおりの承認です。

| キー | 型 | 意味 |
| --- | --- | --- |
| `sha256` | string | plan の sha256。 |
| `plan` | string | リポジトリからの相対パスの plan。 |
| `approved_at` | string | 承認した時刻。 |
| `open_findings` | list of strings | まだ開いている design の指摘の id。 |
| `open_findings_of_current_plan` | boolean or null | それがこの plan の文面のレビューから来たものかどうか。開いているものがなければ `null`。 |
| `design_round` | string or null | 承認の対象になった design のラウンド。なければ `null`。 |
| `decisions_sha256` | string or null | その時点の design の質問への回答の sha256。質問をしていなければ `null`。 |
| `already_approved` | boolean | この plan がこのラウンドとこれらの回答に対してすでに承認されていて、何も記録しなかったかどうか。 |
| `workflow` | string | ワークフロー。 |

<a id="design-questions-show"></a>

### `design questions show`

Object. plan を書く前にした質問で、`state.json` が `design_questions` に持つとおりのものです。
[Design の質問](workflow.md#design-questions) を参照してください。

| キー | 型 | 意味 |
| --- | --- | --- |
| `mode` | string | `design.ask_questions` から決まる `off`、`auto`、`on`。 |
| `state` | string | `off`、`none`、`open`、`round-required`、`answered`。 |
| `pending` | boolean | 未回答の質問がある間だけ `true`。 |
| `questions` | list | 質問ごと、古い順（`--open` では未回答のものだけ）。`id`、`round`、`by`（`orchestrator` か `architect`）、`topic`、`question`、`why`、`options`（`key`、`text`）、`recommended`（`key`、`reason`。または `null`）、`asked_at`、`answer`（`kind`（`option`、`text`、`delegated`）、`text`、`answered_at`。未回答の間は `null`）、`history`（以前の回答。それぞれ `revised_at` 付き）。 |
| `rounds` | list | architect のラウンドごと。`round`、`by`、`recorded_at`、`count`。 |
| `decisions_file` | string or null | リポジトリからの相対パスの `decisions.md`。何か記録されるまでは `null`。 |
| `workflow` | string | ワークフロー。 |

<a id="workflow-state"></a>

## ワークフローの状態

<a id="status"></a>

### `status`

Object.

| キー | 型 | 意味 |
| --- | --- | --- |
| `verdict` | string | `continue` か `stop-and-report`。 |
| `reasons` | list of strings | `stop-and-report` である理由。 |
| `stalls` | list | 止まっているか死んでいるように見える実行中の工程。`token`、`stage`、`started_at`、`elapsed_seconds`、`deadline_seconds`、`pid`、`process_alive`、`reason`。 |
| `abandoned_stages` | list of strings | この呼び出しが死んでいると見て `abandoned` にした工程。 |
| `in_flight` | object | いま動いているもの。トークンをキーにします。 |
| `review` | object | `iteration`、`max_review_iterations`、`blocking`、`accepted`、`refused_for_size`、`identical_rounds`、`final_fix`、`final_fix_pending`（値は `review status` と同じ）。 |
| `design_review` | object | `enabled`、`mode`、`reason`、`iteration`、`max_iterations`、`blocking`、`accepted`、`refused_for_size`、`identical_rounds`、`final_revision`、`final_revision_pending`、`optimization`（値は `review status --design` と同じ）。 |
| `design_approval` | object | `required`、`state`（`pending`、`stale`、`approved`、`no-plan`、`not-required`、`implemented-unapproved`）、`pending`、`stale_reason`（`plan-changed`、`reviewed-since`、`decisions-changed`、`null`）など。[承認](workflow.md#approval) を参照。 |
| `design_questions` | object | `mode`、`state`（`off`、`none`、`open`、`round-required`、`answered`）、`pending`、`open`、`answered`、`total`、`architect_rounds`、`max_rounds`、`decisions_file`。[Design の質問](workflow.md#design-questions) を参照。 |
| `progress` | object | `updates`（`on`、`off`、`auto` のいずれか）、`interval_seconds`、`quiet_notice_seconds`。[進み具合の報告](workflow.md#progress-reports) を参照。 |
| `budgets` | object | 工程ごとの `used`、`limit`、`remaining`。 |
| `total_delegated_runs` | object | `used` と `limit`。 |
| `runtime_remaining_seconds` | integer or null | 委任した実行時間の残り（切り上げ）。上限がなければ `null`。 |
| `runtime` | object | `used`、`limit`、`remaining`、`suspended`。 |
| `tokens` | object | `tokens show --json` が出すもの。 |
| `optimization` | object | 次の `review run` が決めること。`review run` と同じ。 |
| `workflow` | string | ワークフロー。 |
| `also_active` | list of strings | この作業ツリーで動いているように見えるほかのワークフロー。 |

<a id="state-show"></a>

### `state show`

Object. `state.json` そのままです。

| キー | 型 | 意味 |
| --- | --- | --- |
| `version` | integer | `1`。 |
| `runs` | list | ファイルの形式のために残しているもので、何も書き込みません。 |
| `ledger` | object | 予算の台帳。`epoch`、`started_at`、`attempts`、`in_flight`、`signatures`、`total_delegated_runs`、`runtime_seconds`、`tokens` など。 |
| `updated_at` | string | ファイルを最後に書いた時刻。 |
| `events` | list | 記録された工程のイベントすべて、古い順。`stage`、`status`、`at`、そして工程がほかに記録したもの。 |
| `design_approval` | object | Only: plan を承認したあとだけ。`design approve --json` が出すものから `already_approved` と `workflow` を除いたもの。 |
| `design_questions` | object | Only: design の質問かラウンドを記録したあとだけ。`design questions show --json` が出すとおりの `questions` と `rounds`。 |

<a id="budget-show"></a>

### `budget show`

Object.

| キー | 型 | 意味 |
| --- | --- | --- |
| `started_at` | string | 予算が始まった時刻。 |
| `budgets` | object | 工程ごとの `used`、`limit`、`remaining`。 |
| `total_delegated_runs` | object | `used` と `limit`。 |
| `runtime_remaining_seconds` | integer or null | 委任した実行時間の残り（切り上げ）。上限がなければ `null`。 |
| `runtime` | object | `used`、`limit`、`remaining`、`suspended`。 |
| `signatures` | object | 工程ごとに、同じ結果を続けて出した回数。 |
| `in_flight` | object | いま動いているもの。トークンをキーにします。 |
| `stalls` | list | `status` と同じ。 |
| `tokens` | object | `tokens show --json` が出すもの。 |

<a id="tokens-show"></a>

### `tokens show`

Object.

| キー | 型 | 意味 |
| --- | --- | --- |
| `by_stage` | object | 工程ごとの集計。 |
| `by_label` | object | ラベルごとの集計。レビュアーの id、`architect:resumed`、tier のラベル。 |
| `totals` | object | 合計。`runs`、`measured_runs`、`priced_runs`、`tool_reported_runs`、`input_tokens`、`output_tokens`、`total_tokens`、`cache_read_tokens`、`cache_write_tokens`、`billed_tokens`、`cost_usd`、`prompt_chars`、`tool_uses`、`tool_output_chars`、`tool_uses_by_name`。 |
| `complete` | boolean | すべての実行がトークンを報告したかどうか。`false` なら合計は下限です。 |
| `priced` | boolean | すべての実行が費用を報告したかどうか。 |

<a id="optimization-report"></a>

### `optimization report`

Object. `by_context` から下のキーは [optimization](cli.md#optimization) で説明しています。

| キー | 型 | 意味 |
| --- | --- | --- |
| `rounds` | integer | 記録されたコードレビューのラウンド。 |
| `ran` | integer | そのうち走ったもの。 |
| `refused` | integer | そのうち断られたもの。 |
| `refused_by` | object | 断ったものごとの数。`gate`、`context`。 |
| `levels` | object | 有効だったレベルごとのラウンド数。 |
| `gates` | object | ゲートの判定ごとのラウンド数。`allow`、`warn`、`refuse`。 |
| `escalated` | integer | 高リスクのパスで引き上げたラウンド。 |
| `escalation_patterns` | object | パターンごとの引き上げの数。 |
| `always_escalated` | boolean | すべてのラウンドを引き上げたかどうか。 |
| `panel_reduced` | integer | パネルを減らしたラウンド。 |
| `conditional` | object | `added`、`left_out`、`declared_rounds`。 |
| `relevance` | object | コードのラウンドに出番のなかったロール。`judged`、`added`、`left_out`、`left_out_by_level`、`estimated_saving`。 |
| `design_relevance` | object | design のラウンドについての同じもの。 |
| `rounds_without_a_test_result` | integer | ゲートのためのテスト結果が記録されていないラウンド。 |
| `reviewer_runs` | integer | コードレビューのレビュアーの実行。 |
| `measured_runs` | integer | そのうち使用量を報告したもの。 |
| `billed_tokens` | integer | それらの課金トークン。 |
| `billed_per_round` | number or null | 走ったラウンドあたり。 |
| `estimated_saving` | number | 断ったラウンドについての見積もり。 |
| `design_rounds` | integer | 走った design のラウンド。 |
| `design_refused` | integer | 断った design のラウンド。 |
| `design_reviewer_runs` | integer | design のレビュアーの実行。 |
| `design_measured_runs` | integer | そのうち使用量を報告したもの。 |
| `design_billed_tokens` | integer | それらの課金トークン。 |
| `design_billed_per_round` | number or null | 走った design のラウンドあたり。 |
| `tool_reported_runs` | integer | ツールの使用を報告したコードの実行。 |
| `tool_output_reported_runs` | integer | ツールの出力を報告したコードの実行。 |
| `tool_uses` | integer | それらのツール呼び出し。 |
| `tool_output_chars` | integer | それらの観測したツールの出力。 |
| `tool_uses_per_run` | number or null | 報告した実行あたり。 |
| `tool_output_chars_per_run` | number or null | 出力を報告した実行あたり。 |
| `design_tool_reported_runs` | integer | design の実行についての `tool_reported_runs`。 |
| `design_tool_output_reported_runs` | integer | design の実行についての `tool_output_reported_runs`。 |
| `design_tool_uses` | integer | design の実行についての `tool_uses`。 |
| `design_tool_output_chars` | integer | design の実行についての `tool_output_chars`。 |
| `design_tool_uses_per_run` | number or null | design の実行についての `tool_uses_per_run`。 |
| `design_tool_output_chars_per_run` | number or null | design の実行についての `tool_output_chars_per_run`。 |
| `by_context` | object | 周りの文脈の `with` と `without` に分けたコードのラウンド。 |
| `paired` | object | 1 つのスナップショットを文脈ありとなしで実行した組。`pairs`、`pairs_total`、`pairs_listed`、`with`、`without`、`delta`、`note`。 |
| `scorecard` | object | `code`、`design`、`total`。各レビュアーが報告したもの、決めたこと、かかった費用。 |
| `architect_revisions` | object | `attempts`、`resumed`、`fresh`、`fallbacks`。 |

<a id="progress-record"></a>

### `progress record`

Object.

| キー | 型 | 意味 |
| --- | --- | --- |
| `stage` | string | 記録した工程。 |
| `repeats` | integer | このシグネチャを続けて出した回数。 |
| `stop` | boolean | それが上限に達して、ループを止めるべきかどうか。 |

<a id="workflow-list"></a>

### `workflow list`

Object.

| キー | 型 | 意味 |
| --- | --- | --- |
| `current` | string | このコマンドが解決したワークフロー。 |
| `workflows` | list | ここにあるすべてのワークフロー、最近動いた順。`workflow`、`dir`、`started_at`、`updated_at`、`last_activity_monotonic`、`last_stage`、`last_status`、`in_flight`（工程のリスト）、`runs`。 |

<a id="workflow-show"></a>

### `workflow show`

Object.

| キー | 型 | 意味 |
| --- | --- | --- |
| `workflow` | string | ワークフローの id。 |
| `origin` | string | どの決まりで選んだか。`requested`（`--workflow`）、`environment`（`DEV_ORCHESTRA_WORKFLOW`）、`session`（ホストのセッション id）、`pointer`（`current.json`）、`new`。 |
| `dir` | string | その成果物のディレクトリ。 |
| `container` | string | `.ai/` のディレクトリ。 |
| `root` | string | リポジトリのルート。 |
| `also_active` | list of strings | この作業ツリーで動いているように見えるほかのワークフロー。 |

<a id="summary"></a>

### `summary`

Object.

| キー | 型 | 意味 |
| --- | --- | --- |
| `stages` | object | 工程ごとに最後に記録した状態。 |
| `counts` | object | コードレビューの数。`review consolidate` と同じ。レポートがなければ `{}`。 |
| `design_counts` | object | design レビューの `reviewers_ok` と `reviewers_total`。design のレポートがなければ `{}`。 |
| `tokens` | object | `tokens show --json` が出すもの。 |
| `models` | object | ロールごとの `provider`、`family`、`version`。 |
| `reviewers` | list | `id`、`provider`、`model`（テキストに出るとおり）、`status`。並びはテキストと同じ。 |
| `skipped` | object | `refused`、`refused_by`、`design_refused`、`panel_reduced`。名前は `optimization report --json` と同じ。 |
