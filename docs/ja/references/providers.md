<!-- translated-from: references/providers.md sha256:c43b7de3caaeb3d316e21fcea7385be72c02a7686cc003ebbc10df318a91052f -->

> この文書は [references/providers.md](../../../references/providers.md) の日本語訳です。内容が食い違うときは英語版が正です。

<a id="providers"></a>

# Providers

<!-- contents: start -->

**目次**

- [インターフェース](#the-interface)
  - [モード](#modes)
  - [進捗とアイドル期限](#progress-and-the-idle-deadline)
  - [モデル解決の契約](#model-resolution-contract)
- [Claude Code アダプタ](#claude-code-adapter)
  - [セッションの継続](#resuming-a-session)
- [Codex アダプタ](#codex-adapter)
- [Antigravity CLI アダプタ](#antigravity-cli-adapter)
- [Mock アダプタ](#mock-adapter)
- [CLI の追加](#adding-a-cli)
- [プラグインを編集せずに CLI を追加する](#adding-a-cli-without-editing-the-plugin)
  - [契約](#the-contract)
  - [ルール](#rules)
  - [プリセットのフィットに加わる](#taking-part-in-preset-fitting)
  - [うまくいかないとき](#when-it-goes-wrong)
  - [インターフェースの安定性](#interface-stability)
- [失敗時の挙動](#failure-semantics)

<!-- contents: end -->

provider アダプタは、特定の CLI とのやり取りの方法を知っている唯一の場所です。アダプタは次の 2 か所のいずれかに置かれます。

- **Built-in**: `scripts/orchestrator/providers/`。そのパッケージの `__init__.py` で登録されます。これらはプラグインに同梱されています。
- **独自のもの**: `<config dir>/providers/*.py`。プラグインの外にあるため、プラグインを更新しても残ります。[Adding a CLI without editing the plugin](#adding-a-cli-without-editing-the-plugin) を参照してください。

<a id="the-interface"></a>

## インターフェース

```python
class Provider:
    name: str                 # config key, e.g. "claude"
    display_name: str
    executable: str           # command looked up on PATH
    fallback_models: Sequence[ModelCandidate]
    fallback_updated: str     # date the fallback list was last checked

    def detect() -> Detection                  # installed? version? auth present?
    def version() -> tuple[str | None, str | None]
    def list_models() -> list[ModelCandidate]  # discovered from the installed CLI
    def resolve_model(spec) -> ResolvedModel   # family + policy -> CLI argument
    def build_command(mode, resolved, cwd, extra_args) -> list[str]
    def command_line(mode, resolved, cwd, extra_args, options, resume_session) -> list[str]   # do not override
    def run(prompt, mode, cwd, model_spec, timeout, extra_args, resume_session) -> RunResult  # do not override
    def _launch(prompt, mode, cwd, model_spec, timeout, extra_args, resume_session) -> RunResult
    def around_launch(launch, proceed) -> RunResult             # default: proceed(launch)
    def refused_read_only_args(raw_args, source) -> list[str]   # default: refuse all
    def read_only_enforcement() -> dict                         # default: "unspecified"
    static_enforcement: bool                                    # default: False
    preset_family: str | None                                   # default: None (not fitted to presets)
    local_only_options: Sequence[str]                           # default: ()
    def run_warnings(outcome, mode) -> list[str]                # default: []
    def activity_of(line, cwd) -> Activity                      # default: nothing
    def config_families() -> list[tuple[str, str]]              # default: []

    supports_resume: bool                                       # default: False
    required_resume_checks: Sequence[str]                       # default: every check verified.py names
    resume_flags: Sequence[str]                                 # default: ()
    resume_help_unread: str                                     # detail when resume_help_text() is None
    resume_flags_missing: str                                   # detail when flags are missing; default: "%s not advertised"
    resume_version_unread: str                                  # detail when version() gives nothing
    def resume_support(root) -> dict                            # default: "unsupported" / "unspecified"
    def verified_resume() -> dict | None                        # default: None (off the shared rule)
    def resume_help_text() -> str | None                        # default: None
    def resume_advertises(help_text, flag) -> bool              # default: False
    def resume_mechanism() -> str                               # default: read_only_enforcement()["mechanism"]
    def resume_args(session_id) -> list[str]                    # default: NotImplementedError
    def resume_command(mode, resolved, cwd, extra_args, options, session_id) -> list[str]  # default: build_command + resume_args
    def resume_rejected(outcome, mode, options, session_id) -> bool   # default: False
    def parse_session(outcome) -> dict                          # session_id, context_tokens, init

class Launch(NamedTuple):     # one run, as around_launch is handed it
    prompt, mode, cwd, model_spec, timeout, extra_args, env, options,
    idle_timeout, resume_session, command_kwargs
    own_args: tuple[str, ...] = ()   # the adapter's own arguments, after the caller's

def stdout_events(outcome) -> tuple[dict, ...]   # stdout's JSON lines, decoded once per run
```

`detect`、`version`、`list_models` はプロセスごとにメモ化されるため、doctor やウィザードは CLI を再起動することなく何度でも問い合わせられます。

終わった実行を読むフック（`resume_rejected`、`parse_session`、`run_warnings`、`postprocess`、`parse_usage`）は、その JSON 行を `stdout_events(outcome)` から受け取れます。これは `outcome.stdout` を 1 回だけデコードし、どのフックにも同じイベントを渡します。イベントは共有されるので、フックはそれを書き換えてはなりません。

`run` はすべてのアダプタが共有するゲートです。`plan` または `review` の実行では、呼び出し元の生引数（`options.args` と `--extra`）を、アダプタが自分の引数を足す前にアダプタの許可リストと照合し、そのうえで `_launch` を呼びます。`_launch` は実行を `Launch` として `around_launch` に渡します。CLI の実行の前後に行う作業（一時ファイル、プロンプトのファイル、出力の確認）があるアダプタは、`around_launch` をオーバーライドし、`launch._replace(...)` を `proceed` に渡します。CLI を起動するのは `proceed` です。変えてよいのは `prompt`、`cwd`、`model_spec`、`timeout`、`env`、`idle_timeout`、`command_kwargs`、`own_args` だけです。`mode`、`resume_session`、`extra_args`、`options` は `run` がすでにゲートにかけているため、どれかを変えると何かを組み立てる前に `ValueError` になります。アダプタ自身の引数は `own_args` に入れます。これは呼び出し元の引数の後に続き、ゲートにはかかりません。組み込みアダプタのサブクラスでのオーバーライドは、`super().around_launch(launch, proceed)` を通さなければなりません。そうしないと、Codex のワークスペースと読み取り専用のフォークの確認や、agy のプロンプトのファイルが飛ばされます。`_launch` も引き続きオーバーライドでき、`super()._launch(...)` を呼ぶオーバーライドは `around_launch` を通りますが、すべてのキーワードを挙げて渡さなければなりません。`run` をオーバーライドしたアダプタはこのゲートを通らないため、ゲートを自分で持たなければなりません。

セッションの継続（`run architect --resume`）はアダプタごとのオプトインです。`resume_session` はキーワード引数として `run` から `_launch` を経て `command_line` に渡され、`command_line` は `resume_command(...)` を呼びます。既定ではこれまでどおり `build_command` を呼び、その後ろにアダプタ自身の `resume_args(session_id)` を足します。CLI が別の形のコマンドで継続するアダプタ（Codex）はこれをオーバーライドします。これは生引数ではないので、コマンドを組み立てる前に `run` がかける許可リストを通ることはなく、`implement` の実行では `run` が拒否します。オーケストレーターがこれを送るのは、`supports_resume` を宣言し、かつ `resume_support(root)` が `verified`、または `trusted`（合格した版より新しく、メジャー版が同じ版で、それを `newer_than` が示す。その版自体は確認されていない）を報告するアダプタに対してだけです。`run` は値があるときだけキーワードを `_launch` に渡すので、以前のシグネチャで `_launch` をオーバーライドしているアダプタでも新規の実行はこれまでどおり動きます。継続に対応するアダプタは、このキーワードを受け取って base に渡さなければなりません。共通のルールで継続するアダプタは、`resume_support` を base に任せます。オーバーライドするのは、モジュールの `VERIFIED_RESUME` を返さなければならない `verified_resume()`（`smoke_live.py` がこの名前で表を探すため）、フラグを挙げているはずのヘルプを返す `resume_help_text()`、そのヘルプに合わせた自前の照合である `resume_advertises(help_text, flag)`（base のものは False を返すため）です。空でない `resume_flags` を設定し、報告の文言として `resume_help_unread`、`resume_flags_missing`、`resume_version_unread` を設定することもできます。すべてのフラグが挙がっていて版が読めたら、base は `verified.resume_trust(...)` に表と、アダプタの `resume_mechanism()`（記録が保証するフラグ。継続のコマンドが新規と異なるのでなければ、新規の読み取り専用の仕組み）と `required_resume_checks`（版が合格しなければならない確認。これより少なく挙げない限り `verified.py` が挙げるすべて）を渡し、その答えを `resume_report(version, trust)` で報告にします。アダプタは継続の実行を自分で始めないこともできます。その場合 `around_launch` は `proceed` を呼ばずに `resume_rejected=True` かつ `invoked=False` の結果を返し、オーケストレーターは CLI による拒否と同じく新規で 1 回だけ走らせます。実行後、base は `parse_session(outcome)` に、実行が終わったセッション、最後の文脈の大きさ（`context_tokens`）、セッション開始時に CLI が報告した内容（`init`）を問い合わせます。継続した実行に限り `resume_rejected(outcome, mode, options, session_id)` も問い合わせます。これは、求めたセッションが存在しないという正の兆候があるときだけ True を返さなければなりません。オーケストレーターはその場合、新規の実行に試行を 1 回使うからです。結果は `RunResult.session_id`、`context_tokens`、`session_init`、`resume_rejected` に入ります。

`run_warnings(outcome, mode)` は、終わった実行について結果にかかわらず伝えるべきこと（拒否されたツール、成功でない status など）です。base はこの一覧を `RunResult.warnings` に保持し、stderr の先頭にも置きます。`run` と `review run` は stderr が表示されない成功時にもこれを表示し、実行ログとジョブの記録に残します。`activity_of(line, cwd)` は、CLI の実行中に stdout の 1 行が示すもの（`jobs wait`、`review run --progress`）で、`Activity(lines, context_tokens)` を返します。聞き手がいるときだけ呼ばれるので、そうでなければ `execute` はこれまでとまったく同じに呼ばれます。各行は入力から自分で組み立てず、`activity.tool_line(name, input, cwd)` で作ってください。モデルのテキストや自由記述の引数を締め出すのはこの許可リストです。返した行はその後も整形と伏せ字の処理を受けますが、それは二重の備えにすぎません。このフックは CLI の出力を読むスレッドとは別のスレッドで呼ばれ、例外を出してもその行が失われるだけです。不具合のあるフックで失うのはアクティビティだけで、実行そのものは失いません。デフォルトでは何も示しません。`static_enforcement = True` は、`read_only_enforcement()` がサブプロセスを必要としない定数であることを示します。そのため `doctor` は `--fast` のときも CLI がインストールされていないときもそれを報告し、設定コマンドは CLI を探さずにそれをもとに警告します。`preset_family` は、プリセットがそのアダプタをフィットできるようにします（[Taking part in preset fitting](#taking-part-in-preset-fitting) を参照）。どちらもクラスから読まれるので、インスタンスに設定した値は無視されます。プリセットから読み取り専用の席を得るには静的な報告が必要です。フィットは読み込みのたびに行われ、CLI を起動してはならないからです。`preset_family`、`static_enforcement`、`which()` は宣言どおりに信頼されます。`which()` は PATH の検索のままでなければならず、静的な報告はプロセスを起動してはなりません。フィットの側ではそれを見分けられないからです。見分けられるもの（例外、マッピングでない報告、未知の status）は、アダプタを席から外します。`local_only_options` は、書き込みロールが global 設定か `--extra` からだけ受け取るオプションを挙げます。そうしたアダプタでは、書き込みロールの project ファイルのオプションは一切使われません（[Antigravity CLI adapter](#antigravity-cli-adapter) を参照）。`config_families()` は、一覧に出るモデルが日付入りの id で、いずれ古くなる CLI のために、設定に書くべき family を `(family, 今それが解決される先)` の形で返します。`dev-orchestra model list` はこれをモデルの後に表示します。

<a id="modes"></a>

### モード

| モード | 意味 | 作業ツリーが書き込み可能である必要があるか |
| --- | --- | --- |
| `plan` | 調査と設計 | いいえ — 読み取り専用 |
| `implement` | コードとテストを書く | はい |
| `review` | レビューレポートを作成する | いいえ — 読み取り専用 |

アダプタは、モードをそれぞれの CLI が読み取り専用と呼ぶものに変換します。この対応付けは、アダプタがスキルの他の部分と交わす契約です。ファイルを編集できてしまう `review` の実行は、アダプタのバグです。読み取り専用とは、CLI が書き込みを止めることであって、プロンプトがモデルに書かないよう頼むことではありません。

built-in の各アダプタが何を、何によって強制しているか:

| | Claude | Codex | agy |
| --- | --- | --- | --- |
| Edit / Write ツール | 拒否（`--disallowed-tools`。`--tools` にも含まれない） | OS サンドボックス（`-s read-only`） | **止まらない**（実測） |
| シェルでの書き込み | シェルがない。存在するのは `Read`、`Grep`、`Glob` だけ | OS サンドボックス（実測） | `--dangerously-skip-permissions` なしのヘッドレスモードでは拒否される（実測） |
| MCP ツール（Slack、Drive など） | なし（`--strict-mcp-config`） | **未確認** | 未確認 |
| 設定ファイルのフック | 実行されない（`--restricted` がユーザー・プロジェクト・ローカルの設定を無視する） | 未確認 | 未確認 |
| 作業ディレクトリ外の読み取り | 作業ディレクトリと `--add-dir` の中に閉じ込められる（`--restricted`） | 閉じ込められない | 閉じ込められない（実測） |
| 生引数（`options.args`、`--extra`） | `--add-dir <path>` だけ | なし | なし |

`read_only_enforcement()` はこれをアダプタごとに報告し、`dev-orchestra doctor` がそれを表示します。`verified`（CLI が書き込みと外部への副作用を止める）、`partial`（書き込みは止めるが、外部への副作用は未確認）、`unenforced`（CLI に読み取り専用のモードがないと実測された。実行は警告付きで進む）、`unsupported`（強制に必要なものを CLI が提示していない）、`unverified`（それを確認できなかった）、`unspecified`（アダプタが何も報告しない）のいずれかです。`unsupported` または `unverified` の CLI での `plan` や `review` の実行は、弱い形で走らせるのではなく拒否されます（exit 2）。`unenforced` の CLI での実行は、その席が global 設定から来ていれば警告付きで走り、project ファイルから来ていれば拒否されます。設定コマンドは CLI を起動しないので、これを静的な報告からだけ判断します。報告が静的でないアダプタについては、`run` と `review run` がその場で得た報告にもとづいて project ファイルの席を拒否します。

<a id="progress-and-the-idle-deadline"></a>

### 進捗とアイドル期限

アダプタが `streams_progress = True` を設定するのは、そのアダプタが組み立てたコマンドの正常な実行が、*作業中に*出力を出す場合だけです。これは想定ではなく、測定して確かめなければなりません。誤ってそう宣言すると、遅いながらも動いているエージェントが強制終了されてしまいます。これを設定するのは、`stream-json` を要求している Claude のアダプタだけで、アイドル期限を取るのも Claude だけです。Codex と agy のアダプタは `streams_progress` を設定せず、アイドル期限を取らないので、その実行には全体の期限しかありません。agy は `stream-json` の出力からツールの動きを報告しますが、モデルが考えている間も、最後ではないモデルのステップの間（ツール呼び出しの引数を生成している間も含む）も何も出力しないので、黙っていることは止まっていることを意味しません。Claude のロールは `options.idle_timeout` で期限を上書きできます。このオプションは Claude だけのものです。測定結果については `references/limits.md` を参照してください。

<a id="model-resolution-contract"></a>

### モデル解決の契約

`resolve_model` は `ResolvedModel` を返し、その `argument` は次のいずれかです。

- インストール済みの CLI が提示した、またはユーザーが明示的に固定したモデル名
- `None`。これは「モデルのフラグを省略し、CLI に選ばせる」という意味です。

それ以外の場合は `ModelResolutionError` を送出しなければなりません。**アダプタは、自分が確認していないモデル名を決して組み立てません。** これにより、古くなったカタログを同梱することなく、モデルのリリースをまたいでスキルが動き続けます。

<a id="claude-code-adapter"></a>

## Claude Code アダプタ

`claude` 2.1.x で検証済みです。

| 項目 | 方法 |
| --- | --- |
| 非対話実行 | `claude -p --output-format stream-json --verbose --include-partial-messages`、プロンプトは stdin で渡す。最後のフラグは `--help` に載っている場合だけ |
| モデル | `--model <alias-or-name>`。family が `default` の場合は省略 |
| モデルの検出 | CLI 自身の `--model` のヘルプテキストに示されるエイリアスを解析 |
| `plan` / `review` | `--permission-mode plan --disallowed-tools Edit,Write,NotebookEdit --tools Read,Grep,Glob --strict-mcp-config --restricted` |
| `implement` | `--permission-mode acceptEdits` |
| 継続（`run architect --resume`） | 読み取り専用のコマンドに `--resume=<id> --fork-session` を足したもの。継続したセッションを読み取り専用のまま保つと確認された CLI の版、またはそれより新しい版でのみ |
| 認証 | 環境を継承。`ANTHROPIC_API_KEY`、`CLAUDE_CODE_OAUTH_TOKEN`、または CLI の認証情報ファイルで有無を検出 |

読み取り専用のフラグは claude 2.1.283 で実測しました。plan モードと 3 つのツールの拒否だけでは実行は止まりませんでした。`Write` を拒否された実行は `Bash` でファイルを書き、Slack へのメッセージ送信のような MCP ツールにも到達できました。`--tools Read,Grep,Glob --strict-mcp-config` を付けると、セッションが持つのはその 3 つのツールだけになり、MCP サーバーはなくなります。resume したセッションでも同じです。リポジトリの `.claude/settings.json` にあるコマンドフックはそれでも実行されましたが、`--restricted` で止まりました。

`--restricted` は、レビュー対象のブランチが自分の設定を持ち込めないようにするためのもので、知っておくべき帰結があります。

- これらの実行では、ユーザー・プロジェクト・ローカルの `settings.json` は読まれません。そこにあるフック、`env`、`apiKeyHelper`、`permissions.allow`、`permissions.additionalDirectories`、モデルの既定値、そして **`permissions.deny`** も効きません。`Read(./.env)` のような deny ルールでリポジトリ内の秘密をモデルから遠ざけていた場合、それは architect やレビュアーには効かなくなります。管理設定（managed settings）は引き続き効くので、そうしたルールはそこへ移してください。implementer と review fixer は変わりません。
- `Read`、`Grep`、`Glob` は作業ディレクトリと `--add-dir` の中に閉じ込められます。claude 2.1.283 で実測: `--restricted` 付きでは、作業ディレクトリ外の絶対パスの Read、そのディレクトリの Grep と Glob がすべて失敗し、付けなければ同じ実行がファイルを読みます。シンボリックリンクは未検証です。

`claude --help` に `--tools`、`--strict-mcp-config`、`--restricted` が載っていない CLI では、`plan` と `review` の実行は拒否され、`doctor` は `NOT ENFORCEABLE` と表示します。`--help` を読めない CLI では `UNVERIFIED` として拒否されます。`claude --help` はプロセスごとに 1 回だけ読まれ、モデルとパーミッションモードの検出と共有されます。

<a id="resuming-a-session"></a>

### セッションの継続

`run architect --resume` は直前の architect のセッションを継続します。アダプタは同じ読み取り専用のコマンドに `--resume=<id> --fork-session` を足します。`=` の形にするのは `--resume` が値を省略できるオプションだからで、fork するのは継続元のセッションをそのまま残すためです。id として受け付けるのは UUID だけです。claude 2.1.283 で読み取り専用のフラグとともに実測: fork したセッションは `Glob`、`Grep`、`Read` のツール、MCP サーバーなし、パーミッションモード `plan` で開始し（init イベント）、新しいセッション id で走り、ファイルを書くよう求められても何も書きませんでした。もう存在しないセッションは、`result` イベント 1 つで exit 1 になります。ターンはなく、使用量はゼロで、`errors` に求めた id を示す一文があります。アダプタはこれで拒否を見分けます（`stream-json` の場合のみ。`text` と `json` にはそのような兆候がなく、その場合の拒否は通常の失敗として報告されます）。

継続したセッションがこれらの制限を保つかどうかは CLI の版の性質なので、版ごとに 2 層で確認します。

- **アダプタに同梱された表**、`providers/claude.py` の `VERIFIED_RESUME`: リリース前に確認した版です。各エントリは確認したときの読み取り専用のフラグを記録しており、フラグを変えると確認をやり直すまですべてのエントリが無効になります。
- **このマシンの記録**、`<config dir>/verified/claude-resume.json`: `python scripts/smoke_live.py --provider claude` だけが書きます。インストール済みの CLI に対して、継続したセッションのツール・MCP サーバー・パーミッションモード、fork、存在しないセッション、閉じ込め、そして新規でも継続でもリポジトリのフックが走らないことを確認し、その版を合格（`versions`）または不合格（`failed`）として、フラグと確認名とともに記録します。ここに記録された不合格は同梱の表に優先します。記録の実パスがワークスペースの中にある場合（`DEV_ORCHESTRA_HOME` がチェックアウト内を指している場合）、記録は読まれず書かれもしません。表は引き続き使われます。

どちらかの層にある版は `verified` です。Claude Code はリリースや実機確認より速く更新されるので、どちらにもない版でも、どちらかにある版より新しく、メジャー版が同じなら信頼に基づいて継続します（`trusted`）。そうした版のうち最も新しいものがその `newer_than` で、同じ版なら表より記録が優先します。ただし、このマシンに記録された不合格が間にある場合は別です。新しいメジャー版（`2.1.285` から `3.0.0`）は、古いメジャー版の合格では信頼せず、その版自体が合格してから継続します。

- **ローカルの不合格は、次のローカルの合格までのすべての版を止めます。** 現在の版以下で、かつ現在の版より下でこのマシンが合格とした最も新しい版より上の版がここで不合格と記録されていれば、現在の版は継続しません（`unverified`。不合格の版を示します）。同梱の表にある版でも同じです。これを覆すのは、それより上の版についてこのマシンに記録された合格だけで、表では覆りません。版として読めない不合格の版も止めます。現在の版が読めないときは、ローカルの不合格がひとつでもあれば止めます（詳細には、順序を付けられないと出ます）。
- **記録を読めなければ、新しい版は何も信頼しません。** 記録を読めないとき（ワークスペースの中にある、JSON でない、未知のスキーマ、オブジェクトでない不合格の項目）は、それ自体が表にある版だけが継続します。このマシンの不合格が分からないまま、新しい版を信頼することはありません。
- 版は `claude --version` の最初のドット区切りの数字の並びで比べます（`2.1.285 (Claude Code)`）。その並びに `-beta.1` のような接尾辞が付いていれば文字列全体が読めず（後ろにある別の数字 `(build 2026.09.30)` を代わりに読むことはしません）、それ自体がどちらかの層にあるときだけ継続します。

どちらの層のどの版よりも古い版は新規に走り、`(unverified)` と示します。`resume_support(root)` がどれに当たるかを報告し（`status`、`detail`、`version`、`source`、`record`、`verified_at`、`newer_than`、`missing`）、`doctor` がそれを `Resume:` 行に表示します。信頼に基づく版の行は、その版自体は確認されていないと示します。このリスクは受け入れています。新しい版で fork したセッションが制限を失っても、その版でスクリプトを実行するまでは信頼に基づいて継続します。

`opus`、`sonnet`、`fable` などのエイリアスはすでに「その family の最新スナップショット」を意味するため、`version: latest` はエイリアスをそのまま渡すだけです。完全なモデル名（`claude-opus-5`）も family として受け付けられ、そのまま渡されます。built-in のフォールバックリストは `claude --help` を読み取れない場合にのみ使われ、エイリアスだけを含みます。日付付きのスナップショット ID は決して含みません。

プロンプトは引数ではなく **stdin** で送られます。これにより、コマンドラインの長さ制限や、シェルごとのクォートの違いを回避できます。

出力形式が `text` ではなく `stream-json` なのには理由が 1 つあります。測定したところ、`text` は実行がほぼ終わるまで何も出力しない（8.9 秒の実行で最初の出力が 8.1 秒時点）ため、固まったエージェントと忙しいエージェントを見分ける方法がありません。ストリーミング形式はモデルが考えている間 `system`/`thinking_tokens` イベントを出力し、アイドル期限はこれを監視します。ただしこのイベントは回答が始まると止まり、17k 文字の回答では 141 秒間 1 行も出ないことを測定しました。そこでアダプタは、`claude --help` に載っていれば `--include-partial-messages` を付けます（推測では付けません）。回答が `stream_event` のチャンクとしてストリームされるようになり、同じプロンプトで最大の間隔は 1.7 秒、stdout は約 8 倍になりました。最終的な回答は `result` イベントから取得し、なければアシスタントのテキストブロック（と、途中で kill されたメッセージのストリーム済みテキスト）、さらに生の stdout へとフォールバックします。そのため、スキーマが変わっても出力が失われるのではなく、段階的に劣化するだけで済みます。`options.output_format: text` で元に戻すこともできますが、停止検出が犠牲になります。これがデフォルトではない理由です。

このアダプタが受け付けるパーミッションモードは、モデルのエイリアスとまったく同じように CLI 自身の `--permission-mode` のヘルプテキストから読み取られます。そのため、`options.permission_mode` は実際にインストールされているものに対して検証されます。

`acceptEdits` はファイル編集を自動承認しますが、シェルコマンドは承認しません。そのため、テストスイートの実行を求められた Implementer が実行できないことがあります。回避策は 2 つあり、推奨順に次のとおりです。

1. プロジェクト自身の `.claude/settings.json` でコマンドを許可リストに入れる（`permissions.allow`: `Bash(pytest:*)`）。範囲が狭く、設定がプロジェクトとともに管理されます。
2. そのロールだけ、より緩いモードを **global** 設定で設定する（`config set --scope global implementer.options.permission_mode bypassPermissions`）:

```yaml
implementer:
  provider: claude
  options:
    permission_mode: bypassPermissions
```

書き込みロールは、`options.permission_mode` と `options.args` を agy の `skip_permissions` と同じく global 設定か `--extra` からだけ受け取ります。project ファイルはレビュー対象のブランチと一緒に持ち込まれうるもので、ブランチが自分の implementer の確認を切れてはいけないからです。project ファイルが implementer、review fixer、またはそのいずれかの tier で `options.permission_mode` を（値を問わず）挙げるか、何らかの `options.args` を設定すると、そのロールの `implement` の実行は何も消費する前に拒否され、`config validate` と `doctor` がそう伝えます。

または、1 回の実行だけその場で指定します。

```bash
dev-orchestra run implementer --prompt-file plan.md --extra --permission-mode bypassPermissions
```

`--extra` は、`implement` の実行ではそれ以降のすべてをそのまま CLI に転送します。`plan` と `review` では、アダプタは設計上、制限を緩める `permission_mode` を無視し、`--add-dir <path>`（または `--add-dir=<path>`）以外の生引数をすべて拒否します。実行は何も消費する前に exit 2 で終わります。`--add-dir` を受け付けるのは global 設定と `--extra` からだけで、project ファイルの `options.args` は読み取り専用のロールでは中身を問わず拒否されます。拒否メッセージはフラグ名、位置、出所を示し、値は決して表示しません。

<a id="codex-adapter"></a>

## Codex アダプタ

`codex` 0.156.x で検証済みです。

| 項目 | 方法 |
| --- | --- |
| 非対話実行 | `codex exec --skip-git-repo-check --color never -C <cwd>`、プロンプトは stdin で渡す |
| モデル | `-m <model>`。`recommended-coding` family の場合は**省略** |
| モデルの検出 | `codex debug models`（CLI 自身のカタログ、0.154 以降）と `$CODEX_HOME/config.toml` の `model` キー。`hide` が付いたモデルはスキップ |
| `plan` / `review` | `-s read-only`。`plan` は `--json` を足す |
| `implement` | `-s workspace-write --approve-for-me` |
| 最終的な回答 | イベントストリームから抜き出すのではなく、`-o <file>` で取得。`-o` が空の `plan` の実行は失敗 |
| セッションと使用量 | `plan`: 最初の `thread.started` イベントの `thread_id` と、`turn.completed` の `usage`。それ以外は散文の `tokens used` のフッター |
| 継続（`run architect --resume`） | 後述の fork。`VERIFIED_RESUME` にある版（codex-cli 0.156.1）か、Claude と同じ決まりでそれより新しい版でのみ |
| 認証 | 環境を継承。`OPENAI_API_KEY` または `$CODEX_HOME/auth.json` で有無を検出 |

ロールのオプション: `sandbox`（`read-only` / `workspace-write` / `danger-full-access`）と `approve`（`false` にすると `--approve-for-me` を外します）。どちらも `plan` と `review` では無視され、これらは常に `-s read-only` を使います。書き込みロールでは、どちらも、また `options.args` も、global 設定か `--extra` からだけ受け取ります。`sandbox: danger-full-access` は sandbox なしで動き、project ファイルはレビュー対象のブランチと一緒に持ち込まれうるからです。project ファイルが implementer、review fixer、またはそのいずれかの tier でどちらかを挙げるか、`options.args` を書くと、値を問わずそのロールの `implement` の実行は拒否されます。

読み取り専用サンドボックスがシェルでの書き込みを拒否することは実測しました（「Access to the path ... is denied」、Windows）。MCP サーバーは確認していないため、外部への副作用は対象外です。`doctor` は Codex を `partial` と報告します。`plan` や `review` の実行は生引数を一切受け付けません。`-s`、`-sdanger-full-access`、`-c sandbox_mode=...`、`--profile` は、どう綴っても拒否されます。アダプタ自身が付ける `-o` は生引数ではありません。

`plan` の実行は `--json` を足し、JSONL のイベントを出力させます（0.156.1 で実測）。セッション id は最初の `thread.started` イベントの `thread_id` で、UUID のときだけ受け付けます。使用量は `turn.completed` から取ります。その `input_tokens` は `cached_input_tokens` を含むので、キャッシュの読み込みを差し引いて `cache_read_tokens` として記録します。`output_tokens` は出力されたままです。`--json` のもとでは散文のフッターは出力されず、イベントストリームを回答とみなすことはありません。`-o` のファイルが空の `plan` の実行は失敗します。`review` と `implement` の実行は変わりません。

Codex は、セッションを fork して継続します。`python scripts/smoke_live.py --provider codex` が確認（`stays read-only`、`resumes read-only`、`forks the session`、`reports a missing session`、`ignores repository config on resume`）に合格し、その版が `providers/codex.py` の `VERIFIED_RESUME` に入ると、その版を信用します（codex-cli 0.156.1、2026-09-30）。同じメジャー版でそれより新しい版は、上の決まりで信用します。スクリプトは、クラスが `resume_args` を上書きしている adapter すべてに、`supports_resume` の値にかかわらずこれらの確認を行うので、adapter で継続を切っても、版の確認は止まりません。組み立てるコマンドは次のとおりです。

```bash
codex exec fork <id> - --skip-git-repo-check --ignore-user-config -c 'sandbox_mode="read-only"' -m <model> --json -o <file>
```

- `-` は stdin から読むプロンプトです。`-o` を渡した fork にはこれが必要です。`fork` は `-s`、`-C`、`--color` を取りません。プロセスはワークスペースで走ります。
- **実行前の防止。** `--ignore-user-config` は、ユーザーの `config.toml`（既定のプロファイルや `sandbox_mode`）が fork を緩めないようにし、`-c sandbox_mode="read-only"` がサンドボックスを設定します。これは `config.toml` のモデルとプロバイダーの設定も落とすので、`-m` を必ず渡します。解決したモデル、`recommended-coding` なら `config.toml` の `model`（親セッションが走ったモデル）です。どちらもなければ fork は拒否されます（`codex: a forked session runs under --ignore-user-config and needs a model; ...`）。`config.toml` の独自の `model_provider` やベース URL も落ちるので、そうした fork は（認証やプロバイダーのエラーで）失敗し、書き込み可能で走ることはなく、次の `--resume` は新規に走ります。明示的な family か `config.toml` の `model` を設定するか、新規での改訂を受け入れてください。
- **このワークスペースのセッションだけ。** fork の前に、親の rollout（`$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<timestamp>-<id>.jsonl`）の `session_meta` が親を示し、その `cwd` が（リンクと大文字小文字を解決したうえで）このワークスペースでなければなりません。そうでなければ fork は拒否されます（`codex: the session to resume was not started in this workspace`）。パスは比較するだけで、保存も表示もしません。どちらの拒否も、セッションが拒否されたときと同じ道をたどります。失敗のイベントと新規の実行 1 回です。ただし何も走っていないので、新規の実行は別の試行を使わず、すでに確保した試行を使います。rollout がまったくないセッションもここで、Codex が起動する前に拒否され、`smoke_live.py` はこれを `reports a missing session` として数えます。Codex がインストールされていなければ、拒否されたセッションではなく、見つからない CLI として報告されます（終了コード 127）。
- **実行後の検出。** fork の rollout が fork 自身とその親を示し（`session_meta.id`、`forked_from_id`）、すべての `turn_context` で `sandbox_policy.type: read-only` を示さなければなりません。そうでなければ実行は失敗し、その回答は使われず（`the forked session's filesystem sandbox could not be confirmed read-only (...)`）、次の `--resume` は新規に走ります。これは書き込みを事後に見つけるもので、防ぐものではありません。防ぐのは上のフラグです。
- 存在しないスレッドは、`thread.started` なしで exit 1 になり、stderr に `thread/fork failed: no rollout found for thread id <id>` が出ます。アダプタはこれで拒否を見分けます。CLI がそれを伝えるのは stderr だけなので、stderr を読みます。

対象はファイルシステムで、新規の読み取り専用の実行と同じです（`partial`）。MCP サーバーと外部への副作用は確認していません。`--ignore-user-config` はおそらくユーザーの `config.toml` の MCP サーバーも読み込まなくしますが、測定しておらず、そうは主張しません。リポジトリ自身の `.codex/config.toml` は、Codex が信頼していないチェックアウトでは一切読み込まれませんでした。ユーザーが信頼済みにしたチェックアウトは測定しておらず、フラグをオンにする前に手で確認します。

`recommended-coding` family は意図的に `-m` フラグ*なし*に解決されます。これが「現在推奨されているコーディングモデルを使う」と正直に伝える方法です。CLI 自身のデフォルトは、定義上、現行のものだからです。

それ以外の family は、*インストール済みの* CLI が保証するものでなければなりません。つまり、その `config.toml` にあるモデルか、`codex debug models` が出力するカタログのスラッグのいずれかです。`dev-orchestra model list` は、そのマシンで利用できるものを正確に表示します（カタログ由来のものは `source=cli-catalog`）。CLI が知らない family は、推測されるのではなく拒否されます。

```yaml
reviewers:
  - id: codex-independent
    provider: codex
    model:
      family: gpt-5.6-terra   # only if `dev-orchestra model list` shows it
      version: latest
    role: general
```

古い CLI はカタログを出力しません。その場合は特定のモデルを手動で固定する必要があり、それによってモデルも固定されます。これは意図してから行ってください。

```yaml
reviewers:
  - id: codex-pinned
    provider: codex
    model:
      family: gpt-something
      version: pinned
      id: gpt-something
    role: general
```

<a id="antigravity-cli-adapter"></a>

## Antigravity CLI アダプタ

Windows 上の `agy` 1.2.13 で検証済みで、`stream-json` の出力は 1.2.16 で検証済みです。implementer と review fixer 向けです。

| 項目 | 方法 |
| --- | --- |
| 非対話実行 | `agy --output-format stream-json [--model <id>] -p "Read the file .ai/agy-prompt-<pid>-<random>.md ..."`。`-p` は最後。実行の進行に合わせて JSON の行を出力し、最後に `result` を出す |
| モデル | `--model <id>`。`default` family の場合は**省略** |
| モデルの検出 | `agy models`（ネットワークが必要）が出力する `id<TAB>name` の行。それ以外は読まない |
| `plan` / `review` | 同じコマンド。agy には読み取り専用のモードがないので、これらの実行は**強制されない** |
| `implement` | 同じコマンドに、global 設定の `options.skip_permissions: true` があれば `--dangerously-skip-permissions` を足したもの |
| 最終的な回答 | `result` の行の `response`。結果がない、状態が `SUCCESS` でない、または応答が空のときは、実行は失敗になる（`agy: no answer: ...` の警告）。そうした実行の途中までのテキストは `run --output` の `.rejected` ファイルにだけ入り、回答にはならない。JSON でない stdout の行と、結果の `error` は stderr へ |
| 使用量 | 結果の `usage.input_tokens`、`output_tokens`、`cache_read_tokens`。`tool_uses` と `tool_uses_by_name` はツールのステップから。出力の文字数と費用はない |
| 継続 | 外している。`--resume` は新規に走る（後述） |
| 進捗 | ツールの動きとコンテキストの大きさは出る。アイドル期限はない（`streams_progress = False`） |
| ツールの動き | `view_file` は `Read <path>`、`write_to_file` は `Write <path>`、`run_command` は `Bash: <program>`。そのほかの素直な名前はそのまま、`mcp__s__t` は `s.t`、素直でない名前は `tool` |
| 認証 | 検出しない。実行が失敗するなら一度 `agy` を起動してサインインする |

family は名前であって id ではありません。id は、実行を解決するときにこのマシンで `agy models` が出力したものから選びます。

| family | 解決先 |
| --- | --- |
| `default`（`""`、`recommended`、`auto` も） | `--model` なし。agy が選ぶ。サブプロセスが要らないので、プリセットと `reviewer add` はこれを使う |
| `gemini-flash`、`gemini-pro` | 一覧にある最新の `gemini-<major>.<minor>-<kind>[-<effort>]`。版の順、次に `high`、接尾辞なし、`medium`、`low` の順 |
| `gemini-flash-low`、`-medium`、`-high`、`gemini-pro-low`、`-high` | その接尾辞を持つ、一覧にある最新の版 |
| `agy models` が出力する id | その id をそのまま |

それ以外は拒否されます（`ModelResolutionError`）。`agy models` を実行できない間は名前付きの family もすべて拒否され、オフラインで解決できるのは `default` だけです。`dev-orchestra model list --provider agy` は、`agy models` が表示する id を並べたあと、設定に書くものとして、上の family のうちこのマシンで解決できるものを、今選ばれる id とともに表示します。書き込んだ id は新しいモデルに追従しません。

**実測したこと。** `-p` はプロンプトを値として取ります。`-p` の後に何もないと exit 2 になります。stdin は読まれません。`-p -` は文字どおりの `-` を送り、`-p ""` は、`status` が `ERROR` で `error` が空のプロンプトを示す JSON オブジェクトを出して exit 1 になります。そこでプロンプトは常にワークスペースの中のファイル `.ai/agy-prompt-<pid>-<random>.md`（モードのあるプラットフォームでは所有者だけが読める）に書き、`-p` にはそれを読むよう指示する文だけを載せます。プロンプトそのものは載せません。コマンドラインはローカルのどのプロセスからも読めるからです。`run --print-command` は同じコマンドを、ファイル名をプレースホルダーにして表示します。`.ai` がリンクであるか、ほかの場所に解決される場合は拒否されます（exit 2、何も起動しない）。このファイルは実行の終わりに削除されます。実行の前に削除されるのは、プロセス id がもう動いていないファイルだけです。このプロセスのもの（レビュアーは並列に走る）や、動いている別の実行のもの、名前にプロセス id のないものは削除しません。したがって外から kill された実行は、そのプロンプト（計画、差分、その他プロンプトに入っていたもの）を、後の実行がそのプロセスの終了に気づくか、手で削除するまで `.ai/` に残します。`.ai/` は既定で git から外されています。ユーザーのホームの下には何も書きません。`usage.output_tokens` にはすでに `thinking_tokens` が含まれているので（`gemini-3.1-pro-high` の実行で input 12527、output 215、thinking 212、total 12742。これは input と output の和）、thinking は上乗せしません。些細なプロンプトでも入力は約 12k〜25k トークンかかります。

1.2.16 では、`stream-json` は 1 行に 1 つの JSON オブジェクトを出力します。`init`、続いてステップごとの `step_update`（ツールのステップは始まりと終わりに 1 回ずつ、モデルのステップは最後の行にそのステップの使用量、回答は `text_delta` の断片として）、最後に `json` が出力するのと同じオブジェクトである `result` です。結果の使用量はステップの合計に等しいので、トークンは結果だけから読みます。モデルのステップの `input_tokens` はキャッシュから読まなかった部分なので、`input_tokens + cache_read_tokens` がコンテキストの大きさです。1 回の実行で 13671、14125、14517 と増えました。モデルが考えている間は何も出力されません。測った実行では間隔は最大 6 秒でしたが、アイドル期限を決めるには標本が短すぎます。ツールのステップの `output` は要約（`4 lines, 17 bytes`）なので、出力の文字数は数えません。ヘッドレスモードで拒否されたシェルコマンドは、状態 `SUCCESS`、空の `response`、`denied_actions` で終わります。この実行は `-p` を使わず stdin 経由で記録したもので、`-p` でも同じ形になることはスモークチェック「names a denied command」が確かめます。この形式は文書化されていないので、名前が変わればどれも安全側に倒れます。`result` 以外から回答を取ることはありません。agy の版は確かめません。`stream-json` を受け付けない古い CLI では、実行は失敗し、stderr に CLI 自身のエラーが残ります。

**読み取り専用の実行は強制されません。** agy 1.2.13 では、`--mode plan`、`--mode plan --sandbox`、`--agent research` のいずれもファイルを書き、ワークスペースの外を読みました。plan モードでは回答が返答から外れました。そのため `--mode` は渡さず、`read_only_enforcement()` は `unenforced` を報告します。agy での plan や review の実行（orchestrator、architect、そのいずれかの tier、レビュアー）は、**作業ツリー、`.ai/`（`state.json` の承認記録、計画、スナップショット、他のレビュアーのレポートを含む）、`.git/`、リポジトリの外のファイルを変更でき、dev-orchestra はその実行が何をしたかを後から確かめません。** そうした席は global 設定からだけ受け付けられ（あなた自身の選択として、またはフィットの対象の CLI が agy だけのマシンでの global のプリセットのフィットとして）、設定する場所と実行する場所のすべてで警告されます。`config set`、`reviewer add`、`reviewer set`、`config validate`、セットアップウィザード、`doctor`（注記として）、`run`、`review run`、`review run --design`、そして実行の記録です。同じ席が project ファイルにあれば拒否されます（`run` では exit 2、ラウンドでは失敗したレビュアー、`doctor` では問題）。project ファイルはレビュー対象のブランチと一緒にやってくることがあり、そのブランチが書き込みのできるレビュアーを自分で選べてしまうからです。ウィザードの既定値は、agy をこれらの席に置きません。プリセットが置くのは、Claude も Codex も、席に就けるユーザーアダプタも PATH にないときだけで、そこへ置くフィットの注記はどれも、外す方法（そのロールを設定するか、`reviewers` を並べるかを global ファイルで行う）で終わります。

**継続は意図して外しています。** 継続のルールが問うのは継続したセッションが読み取り専用を保つかどうかですが、agy の plan の実行は `unenforced` なので、版で絞っても何も守れません。`--conversation <id>` は fork せずに元の会話を続けます。agy の architect は警告付きの、global 設定だけの席です。そして有効にするには、1 回 12k〜25k トークンの実機確認が 3 つ要ります。後で必要になれば、agy は `verified_resume()`、`resume_help_text()`、`resume_advertises()` をオーバーライドし、`resume_flags` を設定し、`required_resume_checks = ("reports a missing session",)` を設定することになります。

**implementer のパーミッション。** `--dangerously-skip-permissions` なしでは、ヘッドレスモードでファイルの編集は走り、シェルコマンドは拒否されました。そのため、バイパスを有効にしない限り、agy の implementer はテストを実行できません。有効にするとコマンドが走りました。結果の `denied_actions` は実行の警告になり、成功時にも表示されて、有効にする方法を示します。拒否された操作があるだけでは実行は失敗になりません。implementer は拒否があってもタスクを終えられることがあるからです。拒否された実行が失敗になるのは、応答が空だからです。バイパスは **global** 設定の `options.skip_permissions: true`（既定は `false`）か、1 回の実行だけなら `--extra --dangerously-skip-permissions` です。

```yaml
implementer:
  provider: agy
  model:
    family: default
    version: latest
  options:
    skip_permissions: true
```

agy の書き込みロールでは、`options` の何ひとつとして project ファイルからは受け取りません。project ファイルが implementer、review fixer、またはそのいずれかの tier で `options.skip_permissions` を（値を問わず）挙げるか、何らかの `options.args` を設定すると、そのロールの `implement` の実行は何も消費する前に拒否され、`config validate` と `doctor` がそう伝えます。フラグの綴りは調べないので、project の `options.args` にある `--dangerously-skip-permissions=true` や `-dangerously-skip-permissions` も他のものと同様に拒否されます。これは Claude の読み取り専用の生引数と同じ理屈で、Claude の `permission_mode` と Codex の `sandbox` / `approve` も同じように扱います。`plan` と `review` では `skip_permissions` は無視され、`doctor` はそれを無視されたものとして報告します。

<a id="mock-adapter"></a>

## Mock アダプタ

テストとドライラン用のオフラインアダプタです。プロセスを起動することはありません。

| 環境変数 | 効果 |
| --- | --- |
| `DEV_ORCHESTRA_MOCK_DIR` | `<mode>.txt` の定型応答（`review.txt`、`implement.txt`、`plan.txt`）を置くディレクトリ |
| `DEV_ORCHESTRA_MOCK_RESPONSE` | インラインの定型応答 |
| `DEV_ORCHESTRA_MOCK_FAIL` | `1` にするとすべての実行が失敗します。それ以外の値の場合は、プロンプトにその値を含む実行だけが失敗します（例: 1 つのレビュアー ID） |

また、常に「インストール済み」として扱われ、モデル family `unresolvable` は意図的に `ModelResolutionError` を送出します。そのため、provider の CLI がまったくないマシンでも失敗時の経路を通すことができます。

レビュアーを `--provider mock` に切り替えるのが、トークンを消費せずにパイプラインを端から端まで試す最も安上がりな方法です。

<a id="adding-a-cli"></a>

## CLI の追加

このセクションは、*プラグインに同梱する*アダプタを対象としています。プラグインを編集せずに独自の CLI を使う場合は [Adding a CLI without editing the plugin](#adding-a-cli-without-editing-the-plugin) を参照してください。手順は同じで、ファイルの置き場所だけが異なります。

1. 出発点として `providers/codex.py` をコピーします。
2. **実際の CLI の `--help` を読みます。** フラグを記憶に頼って書かないでください。それがアダプタが腐っていく原因です。検証したバージョンをモジュールの docstring に記録します。
3. `_discover_models`、`_resolve_latest`、`build_command`、`auth_status` を実装します。`around_launch` をオーバーライドするのは、特別な出力の取得のように、CLI の実行の前後に作業がある場合だけです。`launch._replace(...)` を `proceed` に渡し、変えるのは `prompt`、`cwd`、`model_spec`、`timeout`、`env`、`idle_timeout`、`command_kwargs`、`own_args` だけにしてください（`mode`、`resume_session`、`extra_args`、`options` は `run` がすでにゲートにかけているため、変えると例外になります）。アダプタ自身の引数は `own_args` で足します。組み込みアダプタのサブクラスでは `super().around_launch(launch, proceed)` を通してください。**`run` ではなく `around_launch` をオーバーライドしてください**。`run` をオーバーライドしたアダプタは、読み取り専用の生引数ゲートを自分で持つことになります。
4. `plan` と `review` が本当に読み取り専用のモード、つまりモデルの協力なしに CLI が強制するモードに対応付けられていることを確認します。読み取り専用の実行に生引数が必要なら `refused_read_only_args` を実装し（既定ではすべて拒否）、CLI が何を強制するかを示す `read_only_enforcement` を実装します。実装しなければ `doctor` は `not reported by this adapter` と表示します。
5. 登録します:

```python
# providers/__init__.py
def _bootstrap() -> None:
    from . import claude, codex, mock, yourcli

    for module, cls in (..., (yourcli, yourcli.YourProvider)):
        register(cls.name, module.build_provider, ProviderOrigin("builtin", None, module.__name__))
```

6. `tests/test_providers.py` にならってテストを追加します。エイリアスの解析、latest と pinned の解決、推測の拒否、読み取り専用と implement のコマンドの形、CLI がない場合の処理です。実際の CLI を呼び出すテストがあってはいけません。

`dev-orchestra model list --provider yourcli` と `dev-orchestra doctor` は、新しいアダプタを自動的に認識します。

<a id="adding-a-cli-without-editing-the-plugin"></a>

## プラグインを編集せずに CLI を追加する

プラグインはバージョンごとのキャッシュにインストールされるため、`scripts/orchestrator/providers/` に追加したアダプタは次の更新で消えますが、それを参照する `config.yaml` は残ります。代わりに、アダプタは設定ディレクトリに置いてください。そこにあるアダプタは起動時、built-in の後にインポートされます。

| プラットフォーム | ディレクトリ |
| --- | --- |
| Windows | `%APPDATA%\dev-orchestra\providers\` |
| macOS / Linux | `$XDG_CONFIG_HOME/dev-orchestra/providers/`（デフォルトは `~/.config/dev-orchestra/providers/`） |
| 任意（`DEV_ORCHESTRA_HOME` が設定されている場合） | `$DEV_ORCHESTRA_HOME/providers/` |

`DEV_ORCHESTRA_CONFIG` ではこのディレクトリは移動しません。この変数は 1 つのファイルを指すもので、そのファイルはプロジェクトのチェックアウト内にあることもあり、その隣にあるコードは勝手にインポートしてよいものではないからです。`dev-orchestra doctor` は、読み込むディレクトリを、それが存在するかどうかにかかわらず表示します。

<a id="the-contract"></a>

### 契約

このディレクトリ内の各 `<name>.py` は、`orchestrator.providers.base.Provider` のインスタンスを返すモジュールレベルの `build_provider(executable=None)` を定義します。これは読み込み時に 1 回呼ばれ、そのインスタンスの `name`（小文字の英字、数字、`.`、`_`、`-`）が `config.yaml` の `provider:` に指定する値になります。`.base` からではなく `orchestrator.providers.base` からインポートしてください。このファイルはパッケージの一部ではないため、相対インポートは失敗します。したがって、built-in のアダプタをコピーする場合は、そのインポート行 1 行を変更することになります。

stdin でプロンプトを読み取る CLI 向けの最小限のアダプタは次のとおりです。

```python
"""Adapter for the ``mycli`` CLI. Verified against mycli 1.2.0."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from orchestrator.providers.base import (
    READ_ONLY_MODES,
    ModelCandidate,
    ModelResolutionError,
    Provider,
    ResolvedModel,
)


class MyCliProvider(Provider):
    name = "mycli"  # what `provider:` takes in config.yaml; must not be agy, claude, codex or mock
    display_name = "My CLI"
    executable = "mycli"

    fallback_models = (ModelCandidate("", "default", "CLI default", "builtin-fallback"),)
    fallback_updated = "2026-09-24"

    def _resolve_latest(self, family: str) -> ResolvedModel:
        if family in ("", "default"):
            return ResolvedModel(self.name, "default", "latest", None, "mycli default", "cli-default")
        raise ModelResolutionError(
            "mycli: %r is not something the installed CLI vouches for; "
            "pin it with model.version: pinned and model.id" % family
        )

    def build_command(
        self,
        mode: str,
        resolved: ResolvedModel,
        cwd: str,
        extra_args: Sequence[str] = (),
        options: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        # The prompt arrives on stdin; plan and review must be read-only.
        command = [self.executable, "run", "--cwd", cwd]
        if mode in READ_ONLY_MODES:
            command.append("--read-only")
        if resolved.argument:
            command += ["--model", resolved.argument]
        command += self.option_args(options)
        command += list(extra_args)
        return command


def build_provider(executable: Optional[str] = None) -> MyCliProvider:
    return MyCliProvider(executable)
```

あとは、他の provider と同じように参照します。

```yaml
reviewers:
  - id: mycli-general
    provider: mycli
    model:
      family: default
      version: latest
    role: general
```

<a id="rules"></a>

### ルール

- ファイルはソート順に読み込まれます。`_` または `.` で始まる名前、`.py` で終わらないもの、ディレクトリ（パッケージ）はスキップされます。ファイル名は識別子にしてください（`my.cli.py` ではなく `my_cli.py`）。
- built-in の名前（`agy`、`claude`、`codex`、`mock`）は拒否されます。built-in が優先されます。
- 2 つのファイルが同じ名前を提供する場合は、ソート順で先のものが優先され、後のものは報告されます。
- 自分で `register()` を呼んだり、レジストリに触れたりしないでください。ローダーは `build_provider()` が返したものを、最初の呼び出しで読み取った名前で登録します。インポート時、または `build_provider()` から（ローダーによる呼び出しでも、それ以降の呼び出しでも）自分で何かを登録するモジュールは拒否され、変更した内容は元に戻されます。これは、`register()` の呼び出しを残したままプラグインからコピーしたアダプタのような、うっかりしたミスを捕まえるためのものです。これを回避するように書かれたモジュールに対する防御ではありません。そのようなモジュールはプロセス内の何でも再束縛できます（下記参照）。
- `build_provider()` や `__init__` から CLI を起動しないでください。また、モジュールレベルで `sys.exit()` を呼ばないでください。このファイルはすべてのコマンドでインポートされます。読み込み中に送出された `SystemExit` は、他のものと同様に読み込みエラーとして記録されます。

<a id="taking-part-in-preset-fitting"></a>

### プリセットのフィットに加わる

ユーザーアダプタは、クラスが `preset_family` を宣言しない限り [プリセット](configuration.md#presets) から外されます。`preset_family` は、プリセットがそのアダプタに与えるすべての枠で使う family です。フィットは読み込みのたびに行われるので、この family は上の例の `default` のように、CLI を起動せずに解決できなければなりません。宣言があれば、アダプタは built-in の CLI が欠けているところにだけフィットされます。

- **implementer と review fixer**: Claude も Codex も agy も PATH にないとき。
- **orchestrator、architect、レビュアーの席**: Claude も Codex も PATH になく、かつ `static_enforcement = True` で `read_only_enforcement()` が `verified` か `partial` のときだけ。サブプロセスを必要とする報告（たとえば `--help` を読むもの）は、何を報告するとしても条件を満たしません。フィットは CLI を起動してはならないからです。満たさなければ、これらの席は agy がインストールされていれば agy へ、そうでなければ書かれたとおりに展開されます。アダプタが受け取る席はどれも宣言した family になるので、安い枠（`standard` の sonnet の security と test のレビュアー）は受け取りません。安い枠は Claude にだけ配られます。

```python
class MyCliProvider(Provider):
    ...
    preset_family = "default"  # the family every preset slot gets; must resolve offline
    static_enforcement = True  # read_only_enforcement() below is a constant

    def read_only_enforcement(self) -> Dict[str, Any]:
        return {"status": "partial", "mechanism": "--read-only", "detail": "what it does not cover"}
```

席に就けるアダプタが複数あれば名前順に扱われ、レビュアーの席は Claude と Codex のときと同じように順に配られます。そのため、project ファイルが `implementer` をそのうちの 1 つにすると、どれから配り始めるかが決まります。ファイルの内容でアダプタをオプトインさせたり、その強制の度合いを上げたりすることはできません。どちらもクラスから来ます。空でない文字列でない `preset_family` は、書き込みロールも含めてアダプタをすべてのプリセットから外します。前後の空白は取り除かれます。`doctor` はアダプタのブロックに `Preset fitting:` の行を出し、どのロールに就けるか、その理由を示します。`doctor --json` では `providers.<name>.preset_fit` です。`DEV_ORCHESTRA_NO_USER_PROVIDERS=1` は、他のすべてからと同じく、フィットからもすべてのユーザーアダプタを外します。

<a id="when-it-goes-wrong"></a>

### うまくいかないとき

インポートに失敗したファイル、契約を破ったファイル、拒否されたファイルが CLI を止めることはありません。他のすべてのコマンドはそれを除いて処理を続け、`dev-orchestra doctor` はそれを **User providers** の下にエラーとともに一覧表示し、問題の 1 つとしても挙げます（そのため `doctor --strict` は失敗します）。`doctor` は、診断中に（`detect()`、`list_models()`、またはモデル解決から）例外を送出したアダプタも捕捉し、元のファイルに対して `adapter-error` として報告します。アダプタを書いたら、何よりも先に `doctor` を実行してください。

このディレクトリはサンドボックスではなく、信頼されたコードを置く場所です。その中のすべてのファイルは、CLI が起動するたびに、あなたの権限で CLI 自身のプロセスにインポートされ、CLI が行うあらゆることを変更できます。自分で実行してもよいと思えるコードだけを置いてください。`doctor` が常にその場所とインポートしたものを表示するのはそのためです。壊れたアダプタが邪魔になっている場合など、ディレクトリを完全にスキップするには `DEV_ORCHESTRA_NO_USER_PROVIDERS=1` を設定します。これが設定されている間、`config validate`、`config show`、`doctor` は、見つけられない provider の横にその旨を表示するため、設定済みのユーザーアダプタがファイルの欠落と誤解されることはありません。

同じプロセス内でディレクトリを再度読み込む（`load_user_providers()`）と、メモ化された検出結果もクリアされるため、編集したアダプタが新たに検出されます。

<a id="interface-stability"></a>

### インターフェースの安定性

`base.Provider` とその周辺の型（`ModelCandidate`、`ResolvedModel`、`RunResult`、`Usage`、`Detection`）はプラグインの内部のものであり、マイナーバージョン間で変更される可能性があります。プラグインのバージョンを固定するか、更新後に `dev-orchestra doctor` を実行して、アダプタがまだ読み込めることを確認してください。`run()`、`_launch()`、`around_launch()` のシグネチャと `Launch` のフィールドは最も変わりやすい部分です。`tests/test_provider_contract.py` は、上記の例を含むすべてのアダプタがこれらに従っていることを検証します。

上記の例は `build_command` しか実装していませんが、それでも読み取り専用のゲートは効きます。ゲートはアダプタが継承する `run` にあるので、生引数を伴う `plan` や `review` の実行は拒否され（base の許可リストは空）、`read_only_enforcement` を実装するまで `doctor` は `not reported by this adapter` と報告します。その `--read-only` フラグが本当に書き込みを止めるかどうかはアダプタの責任で、ここでは何も検証しません。

<a id="failure-semantics"></a>

## 失敗時の挙動

| 状況 | 結果 |
| --- | --- |
| CLI が PATH にない | 分かりやすいメッセージ付きの `RunResult(ok=False, exit_code=127)` |
| CLI を実行できない | `exit_code=126` |
| タイムアウト | `exit_code=124`、`timed_out=True` — 報告されるだけで、例外は送出されない |
| 0 以外の終了コード | `ok=False`、stderr を取得して秘匿化 |
| 解決できないモデル | 何かが実行される前に `ModelResolutionError` |
| `plan` / `review` で拒否された生引数 | `exit_code=2`、`invoked=False`。フラグ名は示すが値は示さない 1 行 |
| 読み取り専用の強制が `unsupported` / `unverified` | `exit_code=2`、`invoked=False`。CLI がインストールされている場合だけ |
| 読み取り専用の強制が `unenforced` | 実行は進む。警告は `RunResult.warnings` と stderr の先頭に入る |

取得したすべてのストリームは `redact()` を通ります。これは、何かが `.ai/` やコンソールに届く前に、認証情報のような形の部分文字列を消去します。
