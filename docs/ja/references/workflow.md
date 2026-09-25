<!-- translated-from: references/workflow.md sha256:a4881e955b45b0c968a56154ce3a7b80640679cd0e2bfe6fde5592cc387fb0bc -->

> この文書は [references/workflow.md](../../../references/workflow.md) の日本語訳です。内容が食い違うときは英語版が正です。

<a id="workflow"></a>

# ワークフロー

各ステージの詳細です。スキル本体には短い版があります。要約だけでは足りず、
あるステージをより慎重に進める必要があるときにこちらを読んでください。

<a id="stage-selection"></a>

## ステージの選択

判断するのはオーケストレーターです。ほとんどの場合、次の 2 つの問いで決まります。

1. **その変更は機械的か？** 正しい編集内容がリクエストから明らかで、影響範囲が
   局所的であれば、design を省略します。
2. **リクエストで触れられていないものを壊す可能性があるか？** もしあるなら（スキーマ、
   API、共有モジュール、並行処理、認証など）、差分がどれほど小さくても design を実行します。

ステージを省略するのは、明言する価値のある判断です。「design を省略: doc コメント内の
1 行のタイポ」は役に立つ一文ですが、黙って省略するのはそうではありません。

<a id="artifacts"></a>

## 成果物

```
.ai/
├── .gitignore                      # `*`, written once, covers every workflow
├── current.json                    # the workflow this directory last resolved
└── workflows/
    └── <workflow-id>/
        ├── plan.md                 # Architect output
        ├── execution/
        │   ├── design-request.md   # prompt you wrote for the Architect
        │   ├── design-fix-brief.md # generated from accepted design findings
        │   ├── design-revise-request.md
        │   ├── implement-request.md
        │   └── fix-brief.md        # generated from accepted findings
        ├── reviews/
        │   ├── review-target.diff  # frozen snapshot
        │   ├── review-target.json  # strategy, files, sha256
        │   ├── review-surrounding.json  # enclosing symbols, only with review.context.surrounding: enclosing
        │   ├── <reviewer-id>.md    # one per reviewer
        │   ├── consolidated.md
        │   ├── consolidated.json
        │   └── design/             # the design review, counted separately
        │       ├── review-target.md    # the frozen plan
        │       ├── review-target.json  # plan, request, sha256
        │       ├── <reviewer-id>.md
        │       ├── consolidated.md
        │       └── consolidated.json
        └── state.json              # stage events, resolved model ids, plan approval
```

**ワークフローごとに 1 つのディレクトリ。** 以前は、同じチェックアウトで作業する
2 つのセッションが `plan.md`、レビューレポート、予算、ラウンドカウンターを共有しており、
しかもどちらも自分の存在を知らせませんでした。そのため、最初のセッションの plan は
上書きされ、その予算はもう一方に消費されていました。id はコマンドごとに、
`--workflow`、`DEV_ORCHESTRA_WORKFLOW`、ホストのセッション id（12 文字にハッシュ化
されるため、他ツールの内部識別子がこちらのパスに入り込みません）、`current.json` の順に
解決され、最後に新しい id が割り当てられます。

コマンドは成果物をコンテナ相対パスで指定するため、`--output
.ai/plan.md` は *このワークフローの plan* を意味し、そのディレクトリに保存されます。
`.ai/` の外のパスや、すでにワークフローを指定しているパスは、書かれたとおりに使われます。

**分離されるのは記録であって、作業ではありません。** implementer は作業ツリーを編集し、
レビュアーはその同じツリーの `git diff` を読みます。そしてそれはチェックアウトごとに
1 つしかありません。ここで同時に実行されている 2 つのワークフローは、依然として
互いの途中までの編集を目にします。本当に並行して進める作業では、ワークフローごとに
専用の worktree を用意してください。

```
git worktree add ../feature-x feature-x
```

これは別のリポジトリルートになるので、`.ai/` も別になります。
`workflow list` は、ここで稼働中と思われる他のワークフローを挙げ、各コマンドも、
ディレクトリが分かれていることで誤解を招かないよう、その旨を伝えます。

0.4.0 より前のバージョンからアップグレードすると、フラットな `.ai/` は最初に実行された
ワークフローに引き継がれるため、中断していたワークフローも plan とレポートを失いません。

`.ai/` には初回使用時に `*` を含む `.gitignore` が作られるので、成果物はユーザーの
commit に入りません。成果物をレビュー可能にしたいチームはこのファイルを削除して
ディレクトリを commit できますし、決して含めたくないチームはリポジトリ自身の
`.gitignore` に `.ai/` を追加できます。変更した場合は、どちらにしたかを伝えてください。

`.ai/` 内のものも `.dev-orchestra.yaml` も、レビューのスナップショットに入ることは
ありません。スキル自身のファイルはレビュー対象の変更ではないからです。

<a id="design"></a>

## Design

design リクエストは自分で書いてください。Architect はこの会話のコンテキストを
一切持たずに開始します。

```markdown
# Design request

## Goal
<what the user asked for, in your words>

## What I already know
- Entry point: app/controllers/orders_controller.rb:42
- Related: app/services/pricing.rb, spec/services/pricing_spec.rb
- The project uses <framework/conventions you observed>

## Constraints
- Must stay backward compatible with the v1 API
- No new dependencies

## Out of scope
- <anything you have already ruled out, and why>

## Deliverable
Print the complete plan to stdout as Markdown, with these sections: Goal,
Current Behavior, Investigation, Root Cause, Proposed Change, Files to Modify,
Data/API Impact, Compatibility, Test Strategy, Risks, Implementation Steps.
The caller captures stdout. Do not write it to a file: this role runs in plan
mode. Do not modify any file.
```

plan は stdout に出力するよう求め、それ以外の場所には求めないでください。`--output` は
ロールが出力した内容を保存します。そしてこのロールは plan モードで動作し、自身の plans
ディレクトリの外には書き込めません。ファイルに書くよう指示されると、あなたが決して
目にしないファイルに書き込み、そうしたと報告します。そしてその *報告* こそが
`.ai/plan.md` に保存され、それがもっともらしいために、design レビューがそのまま
それをレビューしてしまいます。

```bash
dev-orchestra run architect \
  --prompt-file .ai/execution/design-request.md \
  --output .ai/plan.md
```

plan は先に渡す前に読んでください。曖昧である、コードベースと矛盾している、あるいは
リスクの高い部分を飛ばしている場合は、一度だけ差し戻します。2 回目もまだ不十分なら、
黙って即興で補うのではなく、レポートでその旨を伝えてください。

<a id="design-review"></a>

## Design レビュー

`review.design.enabled` が true でない限り無効です。どちらなのかは `status` が報告します。
同じパネルが、コードが書かれる前に `.ai/plan.md` をコードベースに照らして評価します。

```bash
dev-orchestra review run --design
dev-orchestra review show --design
dev-orchestra review triage --design F1 --status accepted --note "confirmed"
dev-orchestra review fix-brief --design --output .ai/execution/design-fix-brief.md
dev-orchestra review status --design
```

トリアージはコードレビューとまったく同じように行います。plan が何を主張しているかを読み、
コードと照合し、判断します。その後、修正リクエストは自分で書いてください。Architect は
再びコンテキストなしで開始するので、brief だけではプロンプトになりません。

```markdown
# Revise the plan

<the original design request, unchanged>

Read .ai/plan.md and revise it. Keep every section it already has.

<paste .ai/execution/design-fix-brief.md here>

For each finding: say whether you addressed it and how, or why it is not a
problem. Do not widen the scope beyond the original request.

Print the complete revised plan to stdout as Markdown. The caller captures
stdout. Do not write it to a file: this role runs in plan mode.
```

```bash
dev-orchestra run architect \
  --prompt-file .ai/execution/design-revise-request.md \
  --output .ai/plan.md
```

修正の実行が stall した場合、タイムアウトした場合、または失敗した場合、`.ai/plan.md` は
元のまま残ります。この実行は自身の入力を書き換えるよう求められているので、失敗した実行が
その入力を消費してはならないからです。実行が出力した内容は `.ai/plan.md.rejected` に
保存されます。次の試行を使う前にそれを読んでください。それは今回の試行のもので、以前の
試行のものは削除されています。コマンドは書き込みを拒否した場合には必ず非ゼロで終了するので、
連鎖させた `review run --design` が古い plan を新しい plan としてレビューすることはありません。

これは `budgets.architect` から試行を 1 回消費します。design レビューに独自の予算キーが
ないのはこのためです。`review run --design` による再レビューは、`review status --design`
がそう示したときだけ行ってください。書き換えられた plan はハッシュが変わるので、次の
ラウンドは導出されるものであり、渡すものではありません。

`review.design.max_iterations` に達したラウンドでも、修正は行われます。
この上限が数えるのはレビューであり、拒否するのはその修正の再レビューであって、
修正そのものではありません。修正が行われるまで、`status` は `continue` を示し、
`review status --design` は accepted の指摘をもう一度取り込むよう示します
（`final_revision: pending`）。plan が凍結された `review-target.md` と異なるものになるか、
そのラウンドの後に architect が plan を `--output` として応答すると、`status` は
`stop-and-report` を示します（`final_revision: done`）。レビューされていない修正版を、
未解決の指摘とともに提示して尋ねてください。既知の問題に答えが出ていない plan を実装する
ことこそ、このステージが防ぐために存在する誤りだからです。修正に使える `budgets.architect`
の試行が残っていない場合は、ただちに停止となり（`blocked`）、問うべきは、指摘を残したまま
承認するか、試行を空けるかです。すでに承認済みの plan（`approved`。その後
`design.require_approval` が無効にされていても同様）や、すでに実装済みの plan
（`implemented`）には変更を求めません。取り込まれるのは accepted の指摘だけです。
`accepted` のものが 1 つもない場合（未トリアージ、`needs-triage`、
`needs-investigation`）、予算を使い切った時点でただちに停止します（`unaccepted`）。前の
ラウンドとまったく同じ指摘を見つけたラウンドでも、修正は省略されません。繰り返しである
ことはその横に示され、修正が行われた時点で停止の理由になります。

残るのは直前の plan だけで、`reviews/design/review-target.md` に凍結されます。
書き換えるとそれより古いものはすべて上書きされます。

<a id="approval"></a>

## 承認

`design.require_approval` が有効な場合（デフォルト）、ユーザーが plan を承認するまで
何も実装されません。ユーザーに提示するのは次のものです。

- plan の **Goal**、**Proposed Change**、**Files to Modify**、**Risks**
- 未解決の design 指摘と、それがこの plan のレビューから出たものか、以前の修正版の
  レビューから出たものか（`design approve` がどちらかを示します）

そのうえで尋ねてください。記録するのはユーザーの明示的な yes だけで、`dev-orchestra design
approve` で記録します。自分の判断で記録したり、拒否を回避するために記録したりしては
いけません。変更を求められた場合は、plan を修正し（design レビューが有効なら再レビューも
行い）、改めて尋ねてください。修正版はハッシュが変わるので、以前の承認はもう有効では
ありません。

未解決の指摘を残したまま design レビューの予算を使い切ると、最後のラウンドの修正が
行われた時点で `stop-and-report` となり、そのレポートはこの問いで締めくくられます。
修正された plan を提示し、以前の修正版のレビューから出た未解決の指摘を挙げ、尋ねて
ください。architect が plan を変更しなかった場合は、指摘を残したまま承認するか、指摘を
トリアージし直すかを尋ねます。修正に使える architect の試行が残っていなかった場合は、
指摘を残したまま承認するか、試行を空けて（`budget reset`）修正するかを尋ねます。
ユーザーが承認すれば停止には答えが出たことになり、`status` は承認された plan について
その理由を取り下げます。

plan が承認されていない間、`run implementer` は拒否します（exit 5）。これは予算を消費する
前、かつ `--detach` がワーカーを起動する前に行われます。ワーカーも改めて確認し、拒否の
内容全体をそのジョブに書き込みます。`--force` は適用されません。plan がない（design
ステージがない）場合、ゲートはありません。

承認の対象は plan のテキストそのもの（sha256）と、承認が与えられた時点の design レビューの
ラウンドです。plan が変更された場合（`plan-changed`）や、その後に `review run --design` が
再度実行された場合（同じ plan に対してであっても）（`reviewed-since`）、承認は stale に
なります。再集約やトリアージではそうなりません。

`status --json` はこれを `design_approval` の下に報告します。

| フィールド | 意味 |
| --- | --- |
| `required` | `design.require_approval` |
| `state` | `pending`、`stale`、`approved`、`no-plan`、`not-required`、または `implemented-unapproved` |
| `pending` | ユーザーに尋ねる必要があるとき（`pending` または `stale`）に `true` |
| `stale_reason` | `plan-changed`、`reviewed-since`、または `null` |
| `plan_sha256`, `approved_sha256`, `approved_at` | 現在の plan と、承認された plan |
| `matches_current_plan` | 承認された sha が現在の plan のものかどうか。両方がそろっていなければ `null` |
| `reviewed_since_approval` | 承認後に design レビューのラウンドが実行されたかどうか。承認がなければ `null` |
| `open_findings`, `open_findings_of_current_plan` | ブロッキングな design 指摘と、そのラウンドがこの plan テキストをレビューしたかどうか |
| `design_review_exhausted` | 指摘が未解決のまま design レビューの予算を使い切った |

その横で、`design_review.final_revision` は最後のラウンドの修正の状況を示し
（`pending`、`done`、`blocked`、`approved`、`implemented`、`unaccepted`、または上限到達前は
`null`）、`final_revision_pending` は修正がまだ残っている間 `true` になり、
`identical_rounds` は同じ指摘を見つけたラウンドが何回連続したかを示します。

`implemented-unapproved` は、plan ファイルが最後に書き込まれた後に implementer が最後に
`ok` で終了し、承認の記録がないワークフローです。つまり、ゲートが導入された時点ですでに
実装済みだったワークフローです。`status` は尋ねるべきことはないと示すので、以降の
ステージのたびに問題として挙がることはありません。これは免除ではなく、このワークフローで
implementer を再度実行すると `pending` と同様に拒否されます。失敗した implementer の実行は
決して数えられず、その後に書き込まれた plan については尋ねることになります。

<a id="implementation"></a>

## 実装

```markdown
# Implementation request

Follow the plan in .ai/plan.md.

Rules:
- Match the conventions already in this codebase; do not introduce new ones.
- Make the minimal change that satisfies the plan.
- No unrelated refactoring, renaming, or formatting.
- Add or update the tests the plan's Test Strategy calls for.
- Run those tests and report the result.
- If the plan is wrong or impossible, STOP and explain why. Do not redesign.

Report: files changed, tests added/updated, test output, anything you could not do.
```

```bash
dev-orchestra run implementer --prompt-file .ai/execution/implement-request.md
```

implementer の失敗は致命的です。停止し、何が起きたかを報告し、ユーザーが調べられる状態で
ツリーを残してください。

<a id="test"></a>

## テスト

リポジトリから見つけたプロジェクト自身のコマンドを使います（`package.json`、
`Makefile`、`Rakefile`、`pyproject.toml`、CI 設定、CONTRIBUTING）。変更をカバーする
最も狭いコマンドを優先し、時間が許せば範囲を広げます。テストコマンドを決して
でっち上げないでください。見つからなければ、その旨を伝えてください。

テストスイートが失敗したらパイプラインは停止します。修正するか報告してください。
壊れたツリーをレビューしてはいけません。

<a id="reviews"></a>

## レビュー

```bash
dev-orchestra review snapshot   # freeze it
dev-orchestra review run        # fan out
```

`review snapshot` はデフォルトで作業ツリーと `HEAD` の差分を取り、未追跡ファイルも
取り込むので、新規作成したモジュールもレビューされます。ブランチの分岐点以降のすべてを
レビューするには `--base <rev>` を使います。

```bash
dev-orchestra review snapshot --base main
```

出力スキーマ、重複排除、トリアージについては `references/reviews.md` を参照してください。

<a id="fix"></a>

## 修正

```bash
dev-orchestra review fix-brief --output .ai/execution/fix-brief.md
dev-orchestra run review_fixer --prompt-file .ai/execution/fix-brief.md
```

生成された brief の先頭に、自分の指示を追加してください。

```markdown
For each finding below:
1. Read the cited code as it is now.
2. Verify the finding is still true. If it is not, say so and change nothing.
3. Fix valid findings with the smallest correct change.
4. Add a test where the finding exposes a coverage gap.
5. Run the relevant tests plus lint/type checks, and report the output.

Do not fix anything that is not listed here.
```

<a id="re-test-and-re-review"></a>

## 再テストと再レビュー

```bash
dev-orchestra review status
```

```json
{
  "iteration": 1,
  "max_review_iterations": 2,
  "blocking": ["F1"],
  "re_review_recommended": true,
  "iteration_budget_exhausted": false,
  "final_fix": null,
  "final_fix_pending": false
}
```

ラウンドは自動的に進みます。`review run` はラウンドをスナップショットから導出するので、
新しいスナップショットは新しいラウンドになり、同じスナップショットを再実行した場合
（たとえばレビュアーが失敗した後など）は現在のラウンドのままです。`--iteration` は、
意図的にそれを上書きする場合にだけ渡してください。

再レビューは `re_review_recommended` が true の場合にだけ、しかも新たに
`review snapshot` を取った後にだけ行ってください。予算を使い切ったら、もう一度修正し、
再テストしてそれを記録し、報告します。再レビューはしません。上限に達したラウンドでも
修正（`final_fix: pending`、`status` は `continue` を示します）と再テスト
（`retest`、修正が最後の `review_fixer` の試行を使った場合でも引き続き `continue`）は
行われます。修正の後に `test` の結果が記録されると（`done`）、`status` は
`stop-and-report` を示します。残っている指摘を重大度と場所とともに報告し、ユーザーに
判断を委ねてください。前のラウンドの指摘を繰り返した最終ラウンドでも修正は行われ、
繰り返しであることはその後で停止の理由になります。修正に使える `review_fixer` の試行が
残っていない場合はただちに停止となり（`blocked`）、修正すべき `accepted` の指摘が
1 つもない場合も同様です（`unaccepted`）。
予算を超えてループし続けると、実行は高くつくだけで何も生まないものになってしまいます。

<a id="recording-and-reporting"></a>

## 記録と報告

`run` を通じて委譲されたステージは自動的に記録されます。サマリーが完全になるよう、
それ以外は自分で記録してください。

```bash
dev-orchestra state record test ok --detail command="pytest -q" passed=128
dev-orchestra state record test ok --detail phase=re-test
dev-orchestra summary
```

再テストも `test` として記録してください。レビューゲートと `status` の再テスト確認は、
ステージ `test` を読みます。`re-test` ステージは `status` には再テストとして受け付け
られますが、ゲートからは決して見えません。

最終レポートには次のものを挙げます。実行したステージ、省略したステージとその理由、
テスト結果、失敗を含むレビューの結果、トリアージの件数、変更したファイル、そして
未解決のまま残っているもの。

<a id="when-something-goes-wrong"></a>

## 問題が起きたとき

| 状況 | 対応 |
| --- | --- |
| 必要な CLI がない | 報告し、まだ動くステージを実行します。自分でインストールしてはいけません |
| モデルが解決できない | 設定を修正するかユーザーに尋ねます。推測で代用してはいけません |
| レビュアーが 1 つ失敗した | 続行し、`N successful, M failed` と報告します |
| すべてのレビュアーが失敗した | レビューステージを失敗として扱います。変更がレビュー済みだと主張してはいけません |
| ラウンドが `partial` で返ってきた | 変更本体がファイルとして渡されたということです。指摘は本物ですが、レビューはクリーンではありません。指摘をトリアージし、そのラウンドは完全にはレビューされていないと報告します。解消方法は `review status` が示します |
| implementer または fixer が失敗した | パイプラインを停止して報告します |
| スナップショットが空 | レビューするものがありません。実装が実際に何かを書き込んだか確認してください |
| `review run --design` が plan がないと言う | design ステージを省略したのであれば、design レビューも一緒に省略します。そうでなければ先に Architect を実行します |
| 修正後にテストが失敗した | 出力とともに失敗を報告します。やみくもに修正を続けてはいけません |
| 実行が `stalled` で返ってきた | kill されるまで何も出力しなかったということです。失敗として報告し、警告された場合は孤児プロセスがないか確認します |
| コマンドが exit 3 で終了した | 予算を使い切っています。未解決のものを報告します。再試行せず、`--force` にも手を出さないでください |
| `run implementer` が exit 5 で終了した | plan が承認されていないか、承認後に変更された／レビューされたということです。plan を提示してユーザーに尋ね、yes なら `design approve` で記録します。再試行せず、自分で承認してはいけません |
| デタッチされた implementer ジョブが "not approved" で `failed` になった | 原因は同じで、ワーカーが検出したものです。ユーザーが承認したら、もう一度開始します |
| `status` が `stop-and-report` を示す | 停止します。予算、stall、未解決の指摘はすでに考慮済みです |
