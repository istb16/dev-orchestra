<!-- translated-from: references/cli.md sha256:1c8568ce7f41b6349bc3b7ed19d9507891cca075e5a7444e3daff2ec6f56f794 -->

> この文書は [references/cli.md](../../../references/cli.md) の日本語訳です。内容が食い違うときは英語版が正です。

<a id="cli-reference"></a>

# CLI リファレンス

```
python scripts/dev_orchestra.py <command> [options]
bin/dev-orchestra <command> [options]           # POSIX wrapper
bin\dev-orchestra.ps1 <command> [options]       # Windows wrapper
```

インタプリタの名前が `python3` しかない環境では、`python` を `python3` と読み替えてください。
ラッパーはインタプリタを自分で探します。POSIX では `python3`、次に `python` の順、Windows では
`python`、`py`、`python3` の順です。Windows では PATH 上の `python3` はたいていインタプリタではなく
Microsoft Store のエイリアスだからです。

グローバルオプション: `--cwd <dir>`（そのディレクトリから実行したものとして動作します）、
`--workflow <id>`（これらの成果物が属するワークフロー。後述）、
`--version`。

終了コード: `0` 成功、`1` 操作は実行されたが結果が否定的（設定が無効、スナップショットが空、
すべてのレビュアーが失敗、ロールの実行が失敗）、`2` 使い方または設定のエラー、`3` 予算が尽きて
コマンドが実行を拒否した、`4` ジョブがまだ実行中のまま `jobs wait` が戻った、`5` plan が承認されて
おらず `design.require_approval` が有効、`130` 中断。

<a id="config"></a>

## config

| コマンド | 説明 |
| --- | --- |
| `config show [--scope effective\|global\|project] [--json]` | 設定を表示します。デフォルトは `effective`（マージ済み）です。スコープを指定すると、そのレイヤーをディスク上にあるとおりに表示し、たいていはずっと短くなります。`Providers:` 行は、参照している各 provider がどこから来ているか（built-in、ユーザーモジュールのパス、または adapter なし）を示します。`--json` では同じ内容が `providers` の下に入ります。 |
| `config path` | 両方のレイヤーの場所を表示します。 |
| `config setup [--scope global\|project] [--defaults] [--force]` | セットアップウィザードです。`--defaults` は何も上書きしないため、ファイルには `version: 1` だけが入り、すべての値が組み込みのデフォルトに従います。`--force` は TTY がなくてもプロンプトを表示します。 |
| `config reset [--scope …] [--delete]` | このレイヤーの上書きを消去します（ファイルは残り、`version` だけが入った状態になります）。`--delete` を付けるとファイルを削除します。 |
| `config prune [--scope …] [--dry-run]` | レイヤーが持つ値のうち、継承される値と等しいものを削除します。すべてのデフォルトを保持している 0.6.0 より前に書かれたファイル向けです。`--dry-run` は書き込まずに一覧表示します。 |
| `config set <path> <value> [--scope …] [--raw]` | 値を 1 つ設定します。パスは `a.b.c` と `reviewers[0].role` をサポートします。インデックス付きの編集では、リストの残りを下のレイヤーからコピーします。末尾を超えたインデックスは終了コード 2 で終了します。 |
| `config validate [--json]` | 有効な設定を検証します。無効な場合は終了コード 1 です。 |

```bash
dev-orchestra config set implementer.model.family opus
dev-orchestra config set review.max_review_iterations 3
dev-orchestra config set --scope project workspace.dir .agent-work
dev-orchestra config set --raw review.note "3 reviewers"
```

保存されたファイルには、そのファイルで設定されたものだけが入ります。それ以外はすべて下のレイヤーから
解決されるため、改善されたデフォルトがそのファイルにも反映されます。`config reset --delete` は、
削除したファイルが隠していた 2 つ目のプロジェクトファイル（`.dev-orchestra.yaml` の隣にある
`.dev-orchestra.yml`、または親ディレクトリにあるもの）を表に出すことがあります。`config reset`
はファイルをその場に残すので、引き続きそれを隠したままになります。

<a id="model"></a>

## model

| コマンド | 説明 |
| --- | --- |
| `model list [--provider <name>] [--json]` | インストールされている CLI が公開しているモデルを、それぞれの検出元（`cli-help`、`cli-catalog`、`cli-config`、`cli-default`、`builtin-fallback`）とともに表示します。例外を送出した adapter は報告され、残りは引き続き一覧表示されますが、終了ステータスは 1 になります。`--json` のすべてのエントリは同じキーを持ち、`origin` と `adapter_error`（正常に動作した場合は `null`）も含みます。 |

<a id="reviewer"></a>

## reviewer

| コマンド | 説明 |
| --- | --- |
| `reviewer list [--json]` | 設定されているパネルを一覧表示します。 |
| `reviewer add --provider <p> [--model <family>] [--role <r>] [--id <id>] [--pin <model-id>] [--scope …]` | レビュアーを追加します。id を省略すると生成されます（`codex-security`、`codex-security-2`、…）。 |
| `reviewer remove <id\|role\|position> [--scope …]` | id、一意なロール、または 1 始まりの位置で削除します。 |
| `reviewer set <selector> [--provider] [--model] [--role] [--id] [--pin] [--scope …]` | 既存のレビュアーを変更します。 |

```bash
dev-orchestra reviewer add --provider codex --role security
dev-orchestra reviewer add --provider claude --role database --id db-review
dev-orchestra reviewer set 2 --role performance
dev-orchestra reviewer remove db-review
```

<a id="doctor"></a>

## doctor

| コマンド | 説明 |
| --- | --- |
| `doctor [--json] [--fast] [--strict]` | CLI、認証情報の有無、設定、そして各ロールのモデルが解決できるかを診断します。`--fast` はモデルの検出を省略します。`--strict` は問題が見つかった場合に終了コード 1 で終了します。 |

認証情報の値は決して表示しません。認証情報が存在するように見えるかどうかだけを表示します。

「Pinned at a value the built-in default has moved off」セクションは、ファイルが固定している設定の
うち、その後推奨値が変わったものを一覧表示します。これは報告であって書き換えではありません。
意図的な選択と継承されたデフォルトは、ディスク上では見分けがつかないからです。`config prune` は、
求められれば、現在のデフォルトと等しいものを削除します。`reviewers` については何も言いません。
パネルは誰のデフォルトでもないからです。

各 provider ブロックには `Source:` 行があり、`built-in` または `user module <path>` と表示されます。
**User providers** ブロックは常に表示されます。ユーザー adapter をインポートするディレクトリ（または
それが存在しないこと、あるいは `DEV_ORCHESTRA_NO_USER_PROVIDERS` で無効化されていること）、
インポートされたもの、そして読み込みに失敗したすべてのファイル（これも問題として扱われます）を
示します。診断中に例外を送出した adapter には、トレースバックの代わりに
`Installed: unknown (adapter failed)` と `Adapter error:` 行が表示され、それを使うロールには
`adapter-error` が表示されます。`--json` では `providers.<name>.origin`、
`providers.<name>.adapter_error` と、トップレベルの `user_providers` になります。
`references/providers.md` を参照してください。

<a id="run"></a>

## run

| コマンド | 説明 |
| --- | --- |
| `run <role> [--prompt <text>\|--prompt-file <path>] [--tier <name>] [--mode plan\|implement\|review] [--output <path>] [--timeout <s>] [--idle-timeout <s>] [--detach] [--force] [--json] [--print-command] [--extra …]` | 設定されたロールを 1 つ実行します。`<role>` は `orchestrator`、`architect`、`implementer`、`review_fixer`、またはレビュアーの id です。`--tier` はそのロールに設定された `model_tiers` の 1 つを選びます。未知のものはデフォルトのモデルで実行されるのではなく拒否されます。そのステージの予算から試行を 1 回消費し、予算が尽きていれば `--force` がない限り拒否します（終了コード 3）。`run implementer` は、plan が存在し承認されていない間は拒否します（終了コード 5）。これは親プロセスで行われ、detach されたワーカーでも再度行われます。ワーカーでの拒否はそのままジョブレコードに記録されます。`--force` は適用されません。 |

プロンプトは stdin からパイプで渡すこともできます（`--prompt-file -` は明示的に stdin を読みます）。
デフォルトのモード: architect/orchestrator は `plan`、implementer と review_fixer は `implement`、
レビュアーは `review` です。`--print-command` は、実行せずに正確な CLI の呼び出しを表示します。
`--extra` は残りのすべての引数をそのまま provider CLI に渡します。

空のプロンプトは、何かが委譲される前に拒否されます（終了コード 1）。そのため試行は消費されません。
対象は、存在しない `--prompt-file`、存在するが空のもの、明示的な `--prompt ""`、そして何も運ばなかった
パイプです。メッセージはそのどれだったかを示し、パスを書かれたとおりに示すとともに、どこを探したかも
示します。以前は読み込めない `--prompt-file` が空のプロンプトとして読まれ、それが委譲され、provider CLI
が自分の stdin について文句を言う応答が返っていました。

`--timeout` は全体の期限です。`--idle-timeout` は *出力がない* 状態の期限です。固まったエージェントは
静かになり、遅いだけのエージェントは出力を続けるので、これを使えば stall を全体の期限ではなく数分で
検出できます。これは進捗をストリームする provider にのみ適用され（両方の adapter が該当します。
`references/providers.md` を参照）、それ以外では推測せずに無視されます。

`--output` は、実行が成功して何かを出力した場合にのみ、実行の stdout を書き込みます。stall した、
タイムアウトした、または失敗した実行では既存のファイルはまったくそのまま残り、その旨が stderr に
表示されます。書き込み先はたいてい、その実行が修正を求められたファイルなので、断片で上書きすると
入力が失われてしまいます。拒否された実行が出力したものは、ターゲットの隣に `<output>.rejected` として
保存され、同じメッセージでその名前が示されます。以前の試行が残したサイドカーファイルは、今回のものとして
読まれないよう、残さずに削除されます。書き込みが拒否された場合は、実行そのものが成功していても終了
コード 1 になります。`--output` は、指定したファイルにこの実行の結果が入っていることを約束するもの
なので、後続のコマンドが古いファイルを新しいものとして読んではならないからです。detach された実行では
同じことがジョブレコードに記録され、`jobs show` がそれを報告します。その stderr はどこにも出力されない
からです。`--output` がない場合は、結果にかかわらず stdout が表示され、実行が失敗すればどちらの場合でも
終了コードは 1 です。終了コード 0 で終わった実行が空白以外何も出力しなかった場合も 1 になります。これは
`--output` が書き込みを拒否するのと同じ判断です。呼び出し側がどちらを使ったかは、実行が応答したか
どうかとは関係がないからです。そのメッセージはロールを示し、生の stderr の冒頭を引用します。プロンプトを
拒否した CLI がその理由を述べるのはそこだからです。
実行の終了イベントは同じ判断を `answered` として記録します（`ok` の実行で何かを出力した場合にのみ
`true`）。これにより `status` は、修正や fix を、沈黙の上の終了コード 0 と区別できます。

`--detach` は実行を独自のプロセスで開始し、すぐにジョブ id を返すので、呼び出しがブロックすることは
ありません。後述の `jobs` を参照してください。

```bash
dev-orchestra run architect --prompt-file .ai/execution/design-request.md --output .ai/plan.md
dev-orchestra run implementer --print-command
echo "explain the failure" | dev-orchestra run orchestrator
```

<a id="review"></a>

## review

| コマンド | 説明 |
| --- | --- |
| `review snapshot [--base <rev>] [--no-untracked] [--surrounding none\|enclosing] [--json]` | レビュー対象の変更を固定します。空の場合は終了コード 1 です。`review.context.max_chars` を超える変更には警告が出ますが、それでも書き込まれます。スナップショットを取ること自体は何も消費せず、拒否するのは消費するコマンドの役目だからです。`--json` は同じことを数値で示します: `change_chars`、`max_chars`、`over_context`。`review.context.surrounding: enclosing` のときは、各 hunk を囲むシンボルも、diff を取ったツリーから `review-surrounding.json` に固定し、固定したシンボル数と文字数、抽出しなかったファイルの数とその理由を示す `context:` 行を出力します。メタデータには `surrounding` ブロックが加わります。`--surrounding` はこのスナップショットに限って設定を上書きします: **`enclosing` は設定が `none` でもこのスナップショットについて候補を凍結します**。この凍結が無いと `review run --surrounding enclosing` は拒否されます。`none` は何も凍結せず、古い凍結ファイルを削除します。設定そのものは変わりません。`references/reviews.md` と [周辺コンテキストの効果を測る](limits.md#measuring-what-surrounding-context-does) を参照してください。 |
| `review run [--design] [--request <path>] [--iteration N] [--only <ids/roles>] [--sequential] [--context <text>] [--base <rev>] [--timeout <s>] [--idle-timeout <s>] [--force] [--surrounding none\|enclosing] [--json]` | スナップショットに対してすべてのレビュアーを実行し、レポートと統合結果を書き込みます。終了コード 1 になるのは、`ok` で戻ったレビュアーが 1 人もいない場合だけです。すべてのレビュアーが失敗した場合や、変更本体が大きすぎてインライン化できずファイルとして渡されたラウンドがこれにあたり、後者はクリーンではなく `partial` として記録されます。ラウンドは `--iteration` が指定されない限りスナップショットから導出され、`review.max_review_iterations` を超えるラウンドは `--force` がない限り拒否されます（終了コード 3）。上限に達したラウンドでも fix と再テストは行われ、拒否されるのは再レビューだけです。最適化ゲートに拒否されたラウンド（テストが失敗として記録されている）も終了コード 3 で終了し、`optimization report` が数えられるよう `refused` として記録されます。`review.context.max_chars`（400,000）を超える変更本体も同様です。何もレビューされず、メッセージはサイズ、上限、上限内に収める方法を示し、ラウンドは `refused_by: "context"` として記録されます。`--force` を付けると構わず実行し、そのラウンドは報告されるすべての場所で `over_budget` として記録されます。本体をプロンプトに入れるかパスとして渡すかは `review.context.inline_chars`（400,000。デフォルトでは同じ数値）で決まり、各レビュアーのエントリには判断に使われた値が記録されます。`budgets.max_runtime_seconds` 分の委譲実行時間を使い切った場合も同様にラウンドは拒否され（パネルはその最大の消費者です）、メッセージはどの予算だったかを示します。`--only` は一部だけを実行しますが、統合はすべてのレビュアーの現在のレポートに対して行うので、何も失われません。`review.context.surrounding: enclosing` のときは、固定されたシンボルを `review.context.surrounding_chars` と、diff が両方の上限の下に残す分の範囲で採用し、`Surrounding context:` 行が採用した数と除外した数とその理由を示し、`--json` にはラウンドの `surrounding` レコードが入ります。上限が計測するサイズは、diff に採用したコンテキストを足したものになります。`--surrounding none\|enclosing` は、1 つのスナップショットをコンテキストあり・なしでレビューするために、この run に限って `review.context.surrounding` を上書きします（[周辺コンテキストの効果を測る](limits.md#measuring-what-surrounding-context-does) を参照してください）。設定は変わらず、行は `(--surrounding enclosing for this run)` または `Surrounding context: none (--surrounding none for this run; review.context.surrounding unchanged)` となります。次の場合は何も課金される前に終了コード 2 で拒否されます: `--design` と併用したとき。incremental ラウンド（再レビューのプロンプトには実行時点の accepted findings が載るので、2 本の run はコンテキスト以外でも違ってしまう）。`enclosing` で何も採用されないとき（理由を問わない: `review snapshot --surrounding enclosing` で凍結していないスナップショット、候補なし、ファイル渡し、予算なし）。同じスナップショットに対する 2 本目の run（間に `budget reset` を挟んでも同じ）で、前回の run が組み立てた後に finding のトリアージまたはトリアージのメモが設定されたとき（前のラウンドから引き継がれたものは数えない）。同じスナップショットの再実行はラウンドを進めず、findings の署名を登録しないので、ペアが「何も変えなかった修正」に見えることはありません。1 本目は通常どおり登録します。lineage が変わった後の run や、`--iteration` で別のラウンドを指定した run は再実行ではなく、署名を登録します。run のイベントと `--json` には `measurement` ブロック（`surrounding`、完全な `snapshot` sha256、凍結した `tree`、`head`、`base`、`workflow` ディレクトリ、予算の `epoch`、`rerun`、そして `inputs`: `context_sha256`、`max_findings`、`inline_chars`、`max_chars`、`force`）が加わり、`consolidated.json` には `triage_at_build`（キーごとの各 finding の `triage` と `triage_note`）を持つ `measurement` が加わります。フラグが無ければ、これらは何も書かれません。 |
| `review consolidate [--design] [--iteration N] [--json]` | 既存のレポートを再解析し、統合結果を再構築します。 |
| `review show [--design] [--accepted] [--json]` | 統合されたレビューを表示します。 |
| `review triage [--design] <ids…> --status <status> [--note <text>]` | トリアージの判断を記録します。判断のたびに、`needs-triage` も含めて指摘に `triage_set_at` を刻むので、指摘を戻したことと一度も判断していないことが区別できます。 |
| `review fix-brief [--design] [--output <path>]` | fixer 向けに、受け入れた指摘のブリーフを出力します。 |
| `review status [--design] [--json]` | 再レビューが必要かどうか、イテレーション予算、そしてラウンドの `coverage` を示します。`coverage` には `round`、`change`、ラウンドの計測に使われた `inline_chars` に加え、`unverified` を解消するための操作が含まれます。変更を絞るか `review.context.inline_chars` を引き上げ、その後スナップショットを取り直すことです。また、その上限がラウンドに記録されたサイズを超えて引き上げられた後は、同じスナップショットが今ならインライン化されるので、それに対して `review run` を実行すればよいだけだということも示します。`over_budget` は、そのラウンドが `--force` で `review.context.max_chars` を超えて送られたためにだけ実行されたことを示します。周辺コンテキストを運んだラウンドでは `surrounding context:` 行が加わり（レビュアーごとに渡されたものが違う場合はレビュアーごとに 1 行）、除外されたシンボルを最大 5 つまで名前で示します。`--json` にはレポートの `surrounding` ブロックが入ります。ラウンドの予算を使い切った後は、そのラウンドの最後のパスがどこまで進んでいるかも示します。これは台帳、実行ログ、承認状態から読み取られます（何も消去されません）。`final_fix` は `pending`（もう一度 fix する）、`retest`（fix 済み。再テストを記録する）、`done`、`blocked`（`review_fixer` の試行が残っていない）、`--design` の場合の `final_revision` は `pending`（もう一度修正する）、`done`、`blocked`（`architect` の試行が残っていない）、`approved`、`implemented` のいずれかです。どちらも上限に達する前は `null` で、`final_fix_pending` / `final_revision_pending` フラグを伴います。最後の行は次のステップを示し、前のラウンドの指摘を繰り返したラウンドについて注記します。`references/reviews.md` を参照してください。 |

`--design` を付けると、これらすべてが *design* レビューに切り替わります。実装前に `.ai/plan.md` を
同じパネルで評価するもので、独自のレポート、ラウンドカウンター、トリアージが `.ai/reviews/design/`
の下にあります。`review run --design` は diff ではなく plan そのものを固定し（`review snapshot --design`
はなく、git も必要ありません）、plan が応えるリクエスト（`--request <path>`、デフォルトは
`.ai/execution/design-request.md`。存在しない場合は注記されるだけで致命的ではありません）と一緒に
ハッシュします。plan がない場合は終了コード 2、`review.design.max_iterations` を超えるラウンドは
`--force` がない限り終了コード 3（上限に達したラウンドでも修正は行われ、拒否されるのは再レビュー
だけです）、すべてのレビュアーが失敗した場合は終了コード 1 です。`review.context.max_chars` は plan
*と* リクエストを合わせて計測されます。どちらもすべてのレビュアーのプロンプトに入るからです。また、
ラウンドは plan が固定される前に拒否されるので、前のラウンドのレポートとトリアージは報告のために
そのまま残ります。最適化ゲートとパネルの削減は適用されず、`--base` は無視されます。
`review.design.enabled` が false のときに実行すると、注記を表示したうえで続行します。この設定は
orchestrator がそのステージを実行するかどうかを示すものであり、あなたが実行してよいかどうかを示すもの
ではないからです。`references/reviews.md` を参照してください。

`review status --json` は、予算をその取得元の設定の名前で報告します。`--design` なしでは
`max_review_iterations`、ありでは `max_iterations` です。ペイロードの残りはどちらでも同じです。

`consolidated.json` を書くたびに -- `review run`、`review consolidate`、`review triage` のいずれも、
`--design` の有無を問わず -- `reviews/rounds/<sha12>-<round_id>.json`（design レビューでは
`reviews/design/rounds/`）にもコピーを書きます。コピーを上書きするのは同じラウンドへの後の書き込み
だけなので、ラウンドの指摘とトリアージは次のラウンドの後も残ります。`optimization report` がこれを
読みます。背後にスナップショットの無いレポートにはコピーを作りません。現在の凍結の後に前のラウンドの
id のまま作られたレポート -- どのレビュアーもレビューを返さなかった design ラウンドや、ラウンドの
レビュアーが戻る前の `review consolidate` -- にも作りません。同じツリーや plan を凍結し直した場合、
それは前のラウンドのコピーだからです。

`review snapshot` は、同じツリーの 2 回目のスナップショットも含め、すべての code スナップショットに
`reviews/review-target.json` 内の新しい `round_id` を与え、`review run` がそれを統合レポートの
`snapshot` と run イベントに写します。`review run --design` は、同じ plan の再実行も含め、すべての
ラウンドに `reviews/design/review-target.json` 内の新しい `round_id` を与えます。
`review consolidate --design` と `review triage --design` はそれに手を付けません。統合レポートが
ラウンドを示すのは、`review run --design` でそのラウンドのすべてのレビュアーが戻った後だけです。
`review consolidate --design` は、より新しいラウンドが実行中であっても、前のレポートが示していた
ラウンドを保持します。承認は、その時点で最新だったラウンドに対して与えられます。

<a id="design"></a>

## design

| コマンド | 説明 |
| --- | --- |
| `design approve [--json]` | 現時点の `.ai/plan.md` に対するユーザーの承認を（plan 単体の sha256 で）、承認の対象となった design レビューのラウンドと一緒に記録します。その後の修正や、その後に実行された design レビューがあれば、再度承認が必要です。ラウンドは統合レポートを持つ最後のものであり、そこに示された指摘と合わせて読み取られます。`review run --design` のラウンドが開始されたもののまだレポートがない間（実行中、またはクラッシュした場合）は、何も記録せず終了コード 2 で終了します。どのレビュアーのレビューもなく最後まで実行されたラウンド（すべてのレビュアーが失敗した）の場合は、代わりにそのラウンドに対して承認し、plan に design レビューの指摘がまったくないことを注記します。これにより、失敗し続けるパネルのせいでユーザーが先に進めなくなることはありません。未解決の design の指摘（重大度にかかわらず、却下されておらず重複ともされていないものすべて）は表示されますが、拒否はされません。それらを承知で承認するかどうかはユーザーの判断です。plan の以前の版に対するレビューからの指摘（固定された `review-target.md` が plan と異なる）はその旨がラベル付けされます。同じ plan を同じラウンドに対して再度承認すると `already approved` と表示し、何も記録しません。plan がない場合は終了コード 2 です。ユーザーが会話の中で了承した後にだけ実行してください。 |

承認は `state.json` の `design_approval` の下に保存されます。書き込むのはこのコマンドだけで、隣に
`design_approval` / `approved` イベントが記録されます。`state record` で偽造することはできません。
`budget reset` は承認を保持し、`workflow remove` は削除します。ラウンドに id が付く前に書かれたラウンドは
ラウンドなしとして扱われます。それに対して承認しても何も記録されず、次の `review run --design` で承認は
古くなります。

<a id="status"></a>

## status

| コマンド | 説明 |
| --- | --- |
| `status [--json]` | *続行か停止か* に答える唯一のコマンドです。理由付きの `continue` / `stop-and-report` の判定、stall または放棄されたステージ、実行中のもの、残りの予算、未解決のレビュー指摘を報告します。 |

これを読み取ると、プロセスがなくなった実行中エントリも消去されます。そのため、結果を記録せずに終了した
ステージは、永遠に実行中に見えるのではなく `abandoned` として表示されます。

サイズを理由に拒否されたラウンドは、実際に変更をレビューするラウンド（コードでも design でも）がある
までは `stop-and-report` の理由になります。それ以外に解消する手段はありません。どちらの種類であれ後続の
拒否でも、`status` 自身がプロセスのなくなったステージを消去するときに書き込む `abandoned` エントリでも
解消されません。拒否は前のラウンドの統合結果をそのまま残すので、このルールがなければ、大きすぎる変更が
別のもののクリーンなレビューを根拠に `continue` と答え続けてしまいます。
理由にはサイズと上限が示されます。`--json` では `review.refused_for_size` と
`design_review.refused_for_size` が同じ 2 つの数値を持ちます。

`design_approval` と `Plan approval:` 行は、plan をまだユーザーに提示する必要があるかどうかを示します
（`references/workflow.md`）。これらは理由ではなく、判定も変えません。それを強制するのは
`run implementer` です。唯一の例外は逆方向に働きます。ユーザーが現在の plan を承認した後は、未解決の
指摘を残したまま使い切った design レビューの予算は理由ではなくなります。ユーザーはそれらを見せられた
うえで先に進むと決めたからです。

使い切ったラウンドの予算は、上限に達したラウンドが最後のパスを終えるまでは理由になりません。
Design（`design_review.final_revision`）: `pending`（指摘をもう一度 plan に反映する。再レビューは
しない）は `continue` です。`done`（plan が固定されたものと異なる、または architect がそのラウンドの後に
plan を指す `--output` 付きで応答した）は `stop-and-report` で、`; revised after the last round, not re-reviewed --
present the plan and ask` または `; the architect left the plan unchanged …` が付きます。
`blocked`（architect の試行が残っていない）はただちに停止します。`approved`（`design.require_approval`
が有効かどうかにかかわらず、この plan とラウンドに対する承認が記録されている）は理由を出しません。
`implemented`（implementer がすでにこの plan で実行された）と `unaccepted`（未解決の指摘のどれも
`accepted` ではなく、反映するものがない）は通常の理由のままです。Code（`review.final_fix`）: `pending`
（もう一度 fix する）と `retest`（fix 済み。fix の後に記録される `test` または `re-test` の結果を待っている）
は `continue` です。`done` は `stop-and-report` で、`; fixed and re-tested
after the last round, not re-reviewed -- report` が付きます。`blocked`（`review_fixer`
の試行が残っていない）はただちに停止します。`unaccepted`（fix すべき受け入れ済みのものがない）は通常の
理由のままです。plan が書かれている場合、`architect has no attempts left` が理由になるのは、予算内の
design ラウンドにまだ修正すべきブロッキングの指摘がある間だけです。`Review:` 行と `Design review:` 行は、
最後のパスがまだ残っているときにその旨を示します。

2 つの理由もそれを待ちます。`the last (design) review round found exactly
what the previous one found` は、修正、または fix とその再テストがまだ残っている間は保留されます。その間は
`review` / `design_review` の `identical_rounds` がカウントを持ち、人間向けの行には `identical to the
previous round` と表示されます。そして `<stage> has no attempts left` は、まだ必要なステージについてだけ
出されます。`architect` は plan がない間だけ（まだ残っている修正はそれ自身の理由でその旨を示し、保留中の
承認の行には変更のための試行が残っていないことが注記されます）、`review_fixer` は fix が行われていない
間だけです。

```bash
dev-orchestra status --json
```

```json
{
  "verdict": "stop-and-report",
  "reasons": ["review budget spent (2/2 rounds) with 1 finding(s) still open; fixed and re-tested after the last round, not re-reviewed -- report"],
  "stalls": [],
  "review": {"iteration": 2, "max_review_iterations": 2, "blocking": ["F1"], "accepted": 1, "refused_for_size": null, "identical_rounds": 1, "final_fix": "done", "final_fix_pending": false},
  "design_review": {"enabled": false, "iteration": 0, "max_iterations": 2, "blocking": [], "accepted": 0, "identical_rounds": 0, "final_revision": null, "final_revision_pending": false},
  "budgets": {"implementer": {"used": 2, "limit": 5, "remaining": 3}}
}
```

<a id="jobs"></a>

## jobs

detach された実行です。期限はエージェントが異常な振る舞いを続けられる時間を制限しますが、実行中は
呼び出し側がその呼び出しの *中に* います。orchestrator 自身がエージェントである場合、長いブロックは
クラッシュと区別がつきません。detach するとそれがなくなります。作業は別の場所で実行され、待機には
自分で決めた期限を設けられます。

| コマンド | 説明 |
| --- | --- |
| `jobs list [--json]` | 記録されているすべてのジョブを新しい順に表示します。 |
| `jobs show <id> [--output] [--json]` | 1 つのジョブを表示します。オプションでその出力も表示します。 |
| `jobs wait <id> [--timeout <s>] [--poll <s>] [--json]` | 待機しますが、`--timeout`（デフォルト 60 秒）より長くは待ちません。待機が終わった時点でジョブがまだ実行中であれば終了コード 4 で終了します。これはエラーではなく通常の結果です。ジョブが `--output` の書き込みを拒否した場合は、フォアグラウンドの実行と同様に終了コード 1 で終了します。 |
| `jobs cancel <id>` | 実行中のジョブとそのプロセスツリーを停止します。 |

```bash
id=$(dev-orchestra run implementer --prompt-file plan.md --detach --json | jq -r .id)
dev-orchestra jobs wait "$id" --timeout 120   # exit 4 means "still going"
dev-orchestra jobs show "$id" --output
```

ジョブの一生は `.ai/jobs/` の下にある 1 つの JSON ファイルで表され、ワーカーが書き込みます。そのため、
親プロセスが終了しても進捗は失われません。結果を記録せずにワーカープロセスがなくなったジョブは、永遠に
実行中に見えるのではなく `abandoned` として報告されます。

<a id="budget"></a>

## budget

| コマンド | 説明 |
| --- | --- |
| `budget show [--json]` | ステージごとに使った試行回数、委譲実行の合計、使用した委譲実行時間を表示します。 |
| `budget consume <stage> [--force]` | orchestrator が自分で実行するステージ（特に `test`）の試行を 1 回確保します。予算が尽きていれば終了コード 3 で終了します。 |
| `budget reset` | ラウンドカウンターを含め、予算を最初からやり直します。トークンの集計は保持されます。これは作業にかかったコストの記録であって予算ではなく、どのリセットでも消去されません。台帳が `budgets.session_idle_reset_seconds` の間アイドル状態だった場合にも自動的に行われます。 |

`run` と `review run` は自分の予算を消費するので、`budget consume` が必要なのは orchestrator が直接行う
ステージだけです。

<a id="tokens"></a>

## tokens

| コマンド | 説明 |
| --- | --- |
| `tokens show [--json]` | ワークフローが消費した量を、ステージごと、レビュアーごとに表示します。 |

これは集計であって予算ではありません。ここにあるものが実行を拒否することはありません。強制される
`budget` とは意図的に分けてあります。一方を他方として読むことこそ、この分離が防ごうとしている誤りです。

集計は現在の予算ではなく **ワークフロー全体** を対象とします。`budget reset` とアイドルリセットは予算を
最初からやり直しますが、集計は引き継ぎます。そのため、ここでの合計は作業が経てきたすべてのリセットに
またがり、どのリセットでも消去されません。消去されるのはワークフローを削除したとき（`workflow remove`）
だけです。したがって、別の集計とは別のワークフローのことです。
`dev-orchestra --workflow <id>` はその作業に独自の `.ai/workflows/<id>/` を与え、それとともに独自の
台帳、予算、集計を与えます。

数値は委譲先の CLI から得られるので、その完全さは CLI がどれだけ情報を出すかに左右されます。
Claude Code は、`result` イベントで input、output、cache-read、cache-write の数と cost を報告します。
Codex は合計を 1 つだけ文章で出力し、言い回しが変わるとそれは誤った値ではなく未報告になります。そのため
すべての列は `meas.`（そのステージの実行のうち何かを報告したものの数）と対になっています。報告しなかった
ものがある場合、出力には合計が *下限* であると示されます。

`billed` は input + output + cache-write の合計です。キャッシュの読み取りは意図的に除外しています。
その費用は新規の input の約 10 分の 1 であり、含めると、キャッシュがよく効いたステージが高価なステージ
より上位に来てしまうからです。金額には `cost` を使ってください。

`prompt_chars` は dev-orchestra 自身が組み立てたプロンプトのサイズです。input のうちこのリポジトリが
短くできる唯一の部分であり、そのため合計とは別に数えています。

`tools` と `tool out` は、委譲先のエージェントがツールを使って行ったことです。何回ツールを呼び出したか、
そしてそのツールが何文字を返したかです。これらは Claude Code のイベントストリームから得られます。
**Codex はどちらも報告しません**。フッターには、これらの列がいくつの実行をカバーしているかが示されます。
この 2 つの列では、`-` は *実行が報告しなかった* ことを、`0` は *実行がツールを使わなかったと報告した*
ことを意味します。これはトークンの列にはない区別であり、何も開かなかったレビュアーを見えるようにする
区別です。

**`tool out` は観測されたツールの出力であり、読んだソースではありません。** 200 行のファイルに `wc -l`
を実行したレビュアーは 3 文字と数えられ、同じファイルに `cat` を実行したレビュアーはその全体が数えられ
ます。CLI の外からはこの 2 つを区別できないので、**レビュアーがどれだけのソースを読んだかは、ここからは
まったく知りようがありません**。これらの数値は代理指標であり、同種のもの同士（同じレビュアー id の、
変更の前と後）を比較するには役立ちますが、何が読まれたかを述べるためのものではありません。

ツール呼び出しは名前にかかわらずすべて数えられ、内訳は `tool_uses_by_name`（`--json`、および各実行の
`usage`）に保持されます。`Read` だけを数えるとひどく過小に数えることになります。review モードが拒否する
のは `Edit,Write,NotebookEdit` だけなので、`Bash`、`Grep`、`Glob` はどれもファイルを読む正当な方法
です。実際、これを設計している間に計測した実行は、`wc -l` でファイルを読み、`Read` を一度も呼び出しません
でした。

これらのカウントができる前に記録された実行は、ツールを使ったかどうかを示せないので、使わなかったものと
してではなく、そのように報告されます。そのような集計に書き込まれる最初の実行は（自分自身がツールを報告
するかどうかにかかわらず）`tool_reported_runs` を作成します。これにより、カウントより前の実行の数は、
存在しないキーから読み取られるのではなく `tool_unknown_runs` に保持され、その注意書きはその後に記録される
すべての実行を経ても残ります。

`options.output_format: json` を設定した Claude のロールも、ツールの活動を報告しません。この形式は
`result` オブジェクトを 1 つ出力するだけでツールのイベントを一切出さないので、その実行は計測された `0`
ではなく `-` になります。

```bash
dev-orchestra tokens show
dev-orchestra tokens show --json
```

<a id="optimization"></a>

## optimization

| コマンド | 説明 |
| --- | --- |
| `optimization report [--json]` | このプロジェクトが記録したすべてのレビューラウンドにわたって `optimization.level` が何を決めたか、どの高リスクパターンがそれをエスカレートさせたか、そしてレビュアーが何を課金されたかを表示します。 |

台帳ではなく実行ログから読み取ります。レベルの効果は *率*（どれだけの頻度でラウンドを拒否したか、
どれだけの頻度でパネルを削減したか）であり、率にはラウンドが必要です。`budget reset` は新しい台帳を
始めますが、イベントログは蓄積を続けます。

同じ理由で、現在のワークフローだけでなく `.ai/` 内の **すべてのワークフロー** を読み取ります。1 つの
ワークフローは数ラウンドにすぎず、それでは率になりません。`--workflow <id>` は 1 つに絞り込みます。
これは「このリポジトリで」ではなく「この作業で、レベルが何をしたか」に答えます。

```
Review rounds recorded: 14 (12 ran, 2 refused)
  levels in force        aggressive x14
  gate verdicts          allow x12, refuse x2
  panel reduced          5
  escalated (high risk)  3

Reviewer runs: 19 (19 reported usage), 823,104 billed
  68,592 billed per round that ran

Estimated saving from 2 refused round(s): ~137,184 billed tokens.
An estimate: what a round that did not happen would have cost is
unknowable, so this is the mean of the 12 that did.
```

`Reviewer runs:` より上はすべてコードレビューだけのものです。`review.design` のラウンドは plan に対して
レビューされ、plan には計測する diff もゲートの判断に使うテスト結果もないので、それらについてはどの
レベルも何も決めていません。それらはコストには現れますが、どの率にも現れません。design レビューが有効
だと、消費量は次のように分かれます。

```
Review rounds recorded: 4 (4 ran, 0 refused)
  levels in force        balanced x4
  gate verdicts          allow x4
  panel reduced          0
  escalated (high risk)  0

Reviewer runs: 12 (12 reported usage), 909,313 billed
  code review            8 (8 reported usage), 558,884 billed over 4 round(s), 139,721 each
  design review          4 (4 reported usage), 350,429 billed over 2 round(s), 175,214 each
```

この 2 つが一緒に平均されることはありません。plan に対するラウンドと diff に対するラウンドは同じ単位の
作業ではないので、両方にまたがる数値はどちらも表しません。design レビューが無効な場合は design の行は
ありません。実行されなかったステージの `0` は、計測を装ったノイズだからです。`--json` では、
`reviewer_runs`、`measured_runs`、`billed_tokens`、`billed_per_round` はこれまでどおりコードレビューの
数値であり、design のものは `design_rounds`、`design_reviewer_runs`、`design_measured_runs`、
`design_billed_tokens`、`design_billed_per_round` です。`design_rounds` は *実行された* ラウンドを
数えます。失敗した、または kill の後に放棄されたラウンドは何も課金されていないので、ここではラウンドでも
なく、その除数でもありません。

レビュアーがツールで何をしたかを報告した場合は、次のブロックが続きます。

```
Tool activity, per run and only over the runs that reported it:
  code review            6.5 use(s)/run, 41,300.0 observed output chars/run (4 of 8 run(s) reported)
```

分母は `tool_reported_runs` であって決して `reviewer_runs` ではなく、そのために表示されています。Codex は
ツールの活動を報告しないので、パネル全体で割ると、パネルの構成以外に理由もなく数値が半分になってしまい
ます。そして、これらの数値が存在する目的である比較が、レビュアーが追加または削除されるたびに動いて
しまいます。何も報告されていなければ、ゼロの並んだ行ではなく行そのものがありません。

**観測された出力はツールが返したものであり、読んだソースではありません**。`wc -l` は 200 行のファイルに
対して 3 文字を返します。`--json` では `tool_reported_runs`、`tool_uses`、`tool_output_chars`、
`tool_uses_per_run`、`tool_output_chars_per_run`、そしてその隣に `design_` を前に付けた 5 つがあります。

コードラウンドが一度でも[周辺コンテキスト](reviews.md#surrounding-context)を運ぶと、実行されたコード
ラウンドが 2 つに分かれて表示されます。

```
Surrounding context (review.context.surrounding), code review rounds only:
  with context           3 round(s), 6 run(s), 412,300 billed, 137,433 per round; 4.0 use(s)/run, 12,400.0 observed output chars/run (3 of 6 run(s) reported); 114,360 context chars adopted, 9,812 left out
                         per run and 1k chars of change (3 sized round(s), 61,200 chars): 3,368.5 billed over 6 billed run(s), 607.8 observed output chars over 3 reporting run(s)
  without context        9 round(s), 18 run(s), 1,522,880 billed, 169,208 per round; 9.6 use(s)/run, 44,120.0 observed output chars/run (9 of 18 run(s) reported)
                         per run and 1k chars of change (7 sized round(s), 210,400 chars): 2,851.7 billed over 14 billed run(s), 1,425.9 observed output chars over 7 reporting run(s)
```

`with` になるのは、実際にどれかのレビュアーにコンテキストが示されたラウンドだけです。設定が on でも
何も採用しなかったラウンドは `without` です。**比べるのは各群の 2 行目で、1 行目ではありません。**
生の数値は変更ごとの大きさとパネルの大きさに連動して動きます — 小さな変更はレビュアー 1 人に削減される
のが普通です — そのため 2 行目は、変更のサイズを各数値を報告した実行数で重み付けしたもので割ります。
課金は `change_chars × 課金を報告した実行数` で、ツール出力は `change_chars × ツールを報告した実行数`
で割り、どちらもサイズを記録したラウンドだけを対象にします。コンテキストを運んだラウンドが存在する
までは何も表示されません。`--json` には `by_context.with` と `by_context.without` が常に含まれ、それぞれ
`rounds`、`reviewer_runs`、`measured_runs`、`billed_tokens`、`billed_per_round`、ツールの数値、
`sized_rounds`、`change_chars`、`sized_billed_tokens`、`billed_run_change_chars`、`sized_billed_runs`、
`sized_tool_output_chars`、`tool_run_change_chars`、`sized_tool_runs`、
`billed_per_run_per_1k_change_chars`、`tool_output_chars_per_run_per_1k_change_chars`、`adopted_chars`、
`trimmed_chars` を持ちます。`tokens show` は台帳の累計なのでラウンドを分けられず、変更はありません。
ラウンドごとの比較はこちらで行います。

この分割は異なる変更どうしを比べます。1 つのスナップショットを `review run --surrounding none` と
`--surrounding enclosing` の両方でレビューすると
（[周辺コンテキストの効果を測る](limits.md#measuring-what-surrounding-context-does) を参照してください）、
その 2 本の run を対にしたブロックが出ます:

```
Paired on one snapshot (--surrounding none vs enclosing: the same frozen diff, tree, panel and prompt inputs):
  3f2a9c1b7e04 in issue-54   change 12,400 chars; panel claude-general (claude, opus), codex-general (codex, gpt-5); 3,100 context chars adopted (3,521 as carried), 0 left out
      with:    2 run(s), 41,200 billed, 20,600.0 per run; 3.0 use(s)/run, 9,800.0 observed output chars/run (1 of 2 run(s) reported)
      without: 2 run(s), 45,900 billed, 22,950.0 per run; 6.0 use(s)/run, 22,100.0 observed output chars/run (1 of 2 run(s) reported)
      delta:   -2,350.0 billed/run, -3.0 use(s)/run, -12,300.0 observed output chars/run
  total, 1 pair(s) counted   with: 20,600.0 billed/run, 3.0 use(s)/run, 9,800.0 observed output chars/run; without: 22,950.0, 6.0, 22,100.0; delta: -2,350.0, -3.0, -12,300.0
```

その後に、ペアが何を揃え、何を揃えていないかの注記が続きます。対になるのは `--surrounding` 付きで
実行した run だけです。側はフラグの値で決まり、ペアの鍵は workflow ディレクトリ、完全なスナップショット
sha256、凍結したツリー、`head`、`base` で、予算の epoch は決して含みません。各側で最新の run が対に
なります。実行あたりの課金は usage を報告した run で、ツールの数値はツールを報告した run で割ります。
次の場合、ペアは一覧には載りますが合計からは外れ、その理由が 1 行目の末尾に付きます:

- パネルが違う（`panels differ (without: ...)`）: `(id, provider, model, role)` の集合が等しくない（role はプロンプトを変える）。
- どちらかの側のレビュアー run が届かなかった（`not delivered (with: codex-general failed)`）:
  `ok` 以外の status。
- 2 本の run のプロンプト入力が違った（`inputs differ (context, max_findings)`）。
- enclosing の run が何も採用しなかった（`nothing adopted`）。

`--json` には `paired` が常に含まれます: `pairs`（それぞれ `workflow`、両側の `epoch`、`snapshot`、
`tree`、`same_panel`、`delivered`、`same_inputs`、`nothing_adopted`、`counted`、`panel`、
`without_panel`、`undelivered`、`inputs_differ`、`change_chars`、`adopted_chars`、`context_chars`、
`trimmed_chars`、`with`、`without`、`delta` を持つ）、`pairs_total`（counted なペアの数）、
`pairs_listed`、counted なペアについての合計 `with`、`without`、`delta`、そして `note` です。各側は
`reviewer_runs`、`measured_runs`、`billed_tokens`、`billed_per_run`、`tool_reported_runs`、
`tool_uses`、`tool_output_chars`、`tool_uses_per_run`、`tool_output_chars_per_run` を持ちます。
`delta` は実行あたりの with − without で、どちらかの側に割る対象が無ければ `null` です。
`by_context` は変わりません。

ラウンドのレポートを 1 つでも読めると、stage ごとに採点表が続きます。各レビュアーが何を報告し、
オーナーがそれをどう判断し、その run にいくら掛かったかです。指摘とトリアージは全ラウンドの
アーカイブされたレポート（`reviews/rounds/`、[再レビュー](reviews.md#re-review) を参照）から、
run とコストは run ログから読み、両者をラウンドごとに突き合わせます:

```
Reviewer scorecard, code review: 18 of 27 recorded round(s) had a report to read.
  claude-general         49 reported: 43 accepted, 1 rejected, 4 duplicate, 1 open; 37 found alone (33 accepted)
                         18 run(s), 2,794,838 billed, $42.07 over 18 priced run(s); 2% rejected, 64,996 billed / $0.98 per accepted
  codex-general          25 reported: 15 accepted, 2 rejected, 6 duplicate, 2 open; 16 found alone (11 accepted)
                         17 run(s), 1,166,386 billed, no cost reported; 9% rejected, 77,759 billed per accepted, $ -
  localllm-qwen          22 reported: 1 accepted, 19 rejected, 2 duplicate, 0 open; 20 found alone (1 accepted)
                         9 run(s) (6 failed), nothing reported; 86% rejected, per accepted withheld under 10 accepted
  panel                  96 reported: 59 accepted, 22 rejected, 12 duplicate, 3 open
                         44 run(s), 3,961,224 billed, $42.07 over 18 of 44 run(s); 24% rejected, 67,139 billed / $0.71 per accepted

Review effort, code and design together: 128 accepted over 28 of 46 recorded round(s); 5,614,101 billed,
  $71.90 over 30 priced run(s); 43,860 billed / $0.56 per accepted
```

各ブロックの後には、その数字が何を主張できるかの注記が付きます。ラウンドの区別の仕方と、イベントが
自分のレポートを見つける方法:

- **ラウンドとは凍結 1 回** で、スナップショットの sha256 の先頭 12 文字と、与えられた `round_id` で
  名付けられます。同じツリーや plan を 2 回凍結すると sha は繰り返し、ラウンドは 2 つです。`--only` の
  再実行と `--surrounding` の対の 2 回の run は 1 ラウンドです。そのイベントはすべてコストを足し、
  指摘とトリアージは最後の run のものです。
- **`round_id` を持つイベント** は、ちょうどそのラウンドのレポートに一致します。イベントがそれを
  持つ前に記録されたものは、その sha のレポートがちょうど 1 つのときだけ一致します。同じ sha の
  レポートが 2 つあれば 2 ラウンドであり、新しい方を取ると古いラウンドのコストを新しいラウンドの
  指摘に付けてしまいます。レビュアーエントリにスナップショットの印が無いイベントは、iteration が
  等しく、より確かなものがまだ取っていなければ live のレポートに一致します。
- **読めるレポートの無いラウンドは、コストも含めてすべての数字から除かれます。** コストと指摘が
  同じラウンドを指すようにするためです。0.11.0 より前はワークフローの最後のラウンドしか残らず、
  失われたラウンドは指摘が直される前の変更をレビューしていました。残ったものに対する率は偏っており、
  偏りの向きは分かりません。見出しは、記録されたラウンドのうちいくつを読めたかを示します。どの
  レビュアーもレビューを返さなかったラウンドは穴ではなく、レポートがあっても読みません -- code の
  ラウンドは誰かが戻ったかどうかに関わらずレポートを書き、その中身はそのラウンドのレビュアーが
  書いたものではないからです。その run とコストは数えられ、見出しはこれを別に数えます。
- **指摘はワークフローと stage ごとに、`key` で 1 回だけ数えます。** 誰も直さなかった指摘は
  毎ラウンド戻ってくるからです。ラウンドはそのイベントが記録された順に取り、最後の *明示的な*
  トリアージが勝ちます -- `accepted`、`rejected`、
  `duplicate`、`needs-investigation`、または `triage_set_at` を持つもの。印の無い `needs-triage` は
  再構築されたレポートが指摘に与える既定値で、採用を取り消しません。印のあるものは
  `review triage --status needs-triage` で、取り消します。未判断は `open` です。
- **found alone は、そのレビュアーを外したときに失われ得る数の上限 (upper bound) です**: その
  レビュアーだけが報告し、どのラウンドでも、別レビュアーの指摘と重複候補として結ばれ、かつどちらかが
  `duplicate` とトリアージされた、ということが無いもの。候補リンクで結ばれなかった重複は、
  それでも数えられます。
- **率は判定済み（`accepted`、`rejected`、`duplicate`）10 件から、1 採用あたりの数字は採用 10 件から
  印字します**。それ未満では件数だけが出ます。1 採用あたりの数字は読めたラウンドだけに対する値で、
  読めなかったラウンドはそれをどちらにも動かし得ます。コスト合計は床 (floor) です: 何も報告しなかった
  run は何も足さず、トークンは報告したが価格を報告しなかった run はドルを足しません。したがって価格を
  報告しないレビュアーの 1 採用あたりコストは `$0.00` ではなく無しです。
- **code と design の数字は最後の行でだけ合算します。** その分母は採用された指摘、つまりどちらの
  stage が見つけたにせよオーナーが直すと決めた欠陥 1 件です。ラウンドあたりの数字はラウンドで割り、
  ラウンドは plan と diff で違う作業単位です。それでも stage の混合比はこの数字を動かすので、2 つの
  stage のブロックと並べて読みます。

これはレビューが何を買ったかを言うものであって、レビューが悪くなったかどうかを言うものではありません。
1 採用あたりのコストが上がるのは、レビュー対象のコードが良くなったときの姿でもあり、レビュアーが欠陥を
見落とし始めたときの姿でもあり、価格が上がったときの姿でもあります。この数字は 3 つを区別できません。

読めるレポートの無い stage はブロックを出しません。`--json` には `scorecard` が常に含まれ、`code`、
`design`、`total` を持ちます。各 stage は `rounds_recorded`、`rounds_read`、`rounds_unreviewed`、
`rerun_rounds`（コストが入っているラウンドのうち `--surrounding` の対の数）、`workflows_read`、
`findings`、`reviewers`（id ごと）、`panel` を持ちます。レビュアーは `runs`、`failed_runs`、
`measured_runs`、`priced_runs`、`billed_tokens`、`cost_usd`、`reported`、`accepted`、`rejected`、
`duplicate`、`open`、`alone`、`alone_accepted`、`rejection_rate`、`billed_per_accepted`、
`cost_per_accepted` を持ち、最後の 3 つは閾値未満で `null` です。`panel` と `total` は `alone` の 2 つを
除いた同じ列で、2 人のレビュアーが報告した指摘は 1 回と数えます。`total` は 4 つのラウンド数の和も
持ちます。

すべてのラウンドがエスカレートした場合、レポートはそれをはっきり述べます。設定されたレベルは一度も適用
されなかったということであり、その原因となったパターンが示されます。すべてのラウンドでエスカレートして
存在しないも同然になったダイヤルと、一度も作動しないダイヤルは、カウントの上では見分けがつかず、どちら
なのかを示すのはパターンだけです。インフラストラクチャのリポジトリでは `*.tf` が常にマッチしますが、
その絞り込みに使う設定が `optimization.high_risk_paths` です。

節約量は推定値であり、そう明記されています。拒否されたラウンドが *かかっていたであろう* コストは知り
ようがありません（実際には起きていないからです）。そのため、この数値は同じリポジトリで実際に実行された
ラウンドの平均であり、それが最も誠実な代替値です。

テスト結果なしで記録されたラウンドも報告されます。ゲートは `state record test ok|failed` が書き込んだ
ものを読むので、何も書き込まれなかったラウンドでは判断の材料がなく、ゲートが作動したはずがありません。
これは効果のなかったレベルとは別のことであり、合計だけからでは両者を取り違えやすいのです。

<a id="progress"></a>

## progress

| コマンド | 説明 |
| --- | --- |
| `progress record <stage> --signature <text> [--json]` | ステージが生み出したものを記録します。連続して同一のシグネチャは、最後の試行が何も変えなかったことを意味し、コマンドは停止するよう伝えます。 |

```bash
dev-orchestra progress record test --signature "3 failed: test_totals, test_discount, test_coupon"
```

レビューは、未解決の指摘の集合から自分のシグネチャを自動的に登録します。

<a id="workflow"></a>

## workflow

成果物はワークフローごとに 1 つのディレクトリ `.ai/workflows/<id>/` に置かれます。そのため、同じ
チェックアウト内の 2 つのセッションが、plan、レポート、予算、ラウンドカウンターを共有することはもう
ありません。id はコマンドごとに、`--workflow`、`DEV_ORCHESTRA_WORKFLOW`、ホストのセッション id
（12 文字にハッシュ化）、`current.json`、そして最後に新しい id の順で解決されます。どのルールで決まったか
は `workflow show` が示します。

コンテナに対して書かれたパスはワークフローの中で解決されます。`--output .ai/plan.md` は *この*
ワークフローの plan を意味します。`.ai/` の外のパスや、すでにワークフローを指定しているパスは、書かれた
とおりに使われます。

これが分離するのは成果物であって、作業ツリーではありません。1 つのチェックアウトにはファイルの組が 1 つ
しかなく、レビュアーはその `git diff` を読みます。本当に同時に実行される作業では、各ワークフローに独自の
worktree（`git worktree add ../x x`）を与えてください。それは別のルートであり、したがって別の `.ai/` に
なります。

| コマンド | 説明 |
| --- | --- |
| `workflow list [--json]` | ここにあるすべてのワークフローを、最近アクティブだった順に表示し、現在のものと実行中のステージに印を付けます。 |
| `workflow show [--json]` | このコマンドがどのワークフローにいるか、その成果物がどこにあるか、どのルールでそれが選ばれたかを表示します。 |
| `workflow use <id>` | このディレクトリ用に id を記憶します（`current.json`）。セッション id をエクスポートしないホスト向けです。セッション id をエクスポートするホストでは、引き続きそちらが優先されます。 |
| `workflow remove <id> --yes` | 1 つのワークフローの成果物を削除します。`--yes` がなければ拒否し、現在いるワークフローも拒否します。 |

<a id="state--summary"></a>

## state / summary

| コマンド | 説明 |
| --- | --- |
| `state show [--json]` | このプロジェクトで記録されたステージのイベントを表示します。 |
| `state record <stage> <status> [--detail k=v …]` | ステージの結果を追記します（`run` を通して実行されないステージ用）。`state record test ok\|failed` はレビューゲートが読むものです。再テストも同じ方法で記録してください。 |
| `summary [--json]` | 実行終了時のステージとモデルのサマリーを表示します。plan が承認されていれば `design_approval` も含みます。 |

<a id="environment-variables"></a>

## 環境変数

| 変数 | 効果 |
| --- | --- |
| `DEV_ORCHESTRA_CONFIG` | このファイルをそのままグローバル設定レイヤーとして使います |
| `DEV_ORCHESTRA_HOME` | プラットフォームの設定ディレクトリの代わりにこのディレクトリを使います |
| `DEV_ORCHESTRA_WORKFLOW` | 使用するワークフロー。ホストのセッション id より優先されます |
| `DEV_ORCHESTRA_SESSION` | ワークフローを導出するためのセッション id。セッション id をエクスポートしないホスト向けです |
| `DEV_ORCHESTRA_MOCK_DIR` | mock provider 用の定型応答 |
| `DEV_ORCHESTRA_MOCK_RESPONSE` | mock provider 用のインラインの定型応答 |
| `DEV_ORCHESTRA_MOCK_FAIL` | mock の実行を失敗させます（`1` = すべて、それ以外はプロンプトの部分文字列） |
| `CODEX_HOME` | Codex CLI の設定と認証情報を探すときに考慮されます |
| `DEV_ORCHESTRA_TEST_ASSUME_NO_CLI` | テスト専用: 両方の provider CLI を隠し、CI を再現します |
