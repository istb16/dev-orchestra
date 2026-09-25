<!-- translated-from: references/providers.md sha256:4c0c07ab1ee917d7bdd3b38c7c0f39ca16c07d86e372540fd97e266516e0d4c3 -->

> この文書は [references/providers.md](../../../references/providers.md) の日本語訳です。内容が食い違うときは英語版が正です。

<a id="providers"></a>

# Providers

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
    def run(prompt, mode, cwd, model_spec, timeout, extra_args) -> RunResult
```

`detect`、`version`、`list_models` はプロセスごとにメモ化されるため、doctor やウィザードは CLI を再起動することなく何度でも問い合わせられます。

<a id="modes"></a>

### モード

| モード | 意味 | 作業ツリーが書き込み可能である必要があるか |
| --- | --- | --- |
| `plan` | 調査と設計 | いいえ — 読み取り専用 |
| `implement` | コードとテストを書く | はい |
| `review` | レビューレポートを作成する | いいえ — 読み取り専用 |

アダプタは、モードをそれぞれの CLI が読み取り専用と呼ぶものに変換します。この対応付けは、アダプタがスキルの他の部分と交わす契約です。ファイルを編集できてしまう `review` の実行は、アダプタのバグです。

<a id="progress-and-the-idle-deadline"></a>

### 進捗とアイドル期限

アダプタが `streams_progress = True` を設定するのは、そのアダプタが組み立てたコマンドの正常な実行が、*作業中に*出力を出す場合だけです。これは想定ではなく、測定して確かめなければなりません。誤ってそう宣言すると、遅いながらも動いているエージェントが強制終了されてしまいます。同梱の 2 つのアダプタはどちらもストリーミングしますが、理由は異なります。Codex はネイティブにストリーミングし、Claude はアダプタが `stream-json` を要求しているためです。ロールごとに `options.idle_timeout` で期限を上書きできます。測定結果については `references/limits.md` を参照してください。

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
| 非対話実行 | `claude -p --output-format stream-json --verbose`、プロンプトは stdin で渡す |
| モデル | `--model <alias-or-name>`。family が `default` の場合は省略 |
| モデルの検出 | CLI 自身の `--model` のヘルプテキストに示されるエイリアスを解析 |
| `plan` / `review` | `--permission-mode plan --disallowed-tools Edit,Write,NotebookEdit` |
| `implement` | `--permission-mode acceptEdits` |
| 認証 | 環境を継承。`ANTHROPIC_API_KEY`、`CLAUDE_CODE_OAUTH_TOKEN`、または CLI の認証情報ファイルで有無を検出 |

`opus`、`sonnet`、`fable` などのエイリアスはすでに「その family の最新スナップショット」を意味するため、`version: latest` はエイリアスをそのまま渡すだけです。完全なモデル名（`claude-opus-5`）も family として受け付けられ、そのまま渡されます。built-in のフォールバックリストは `claude --help` を読み取れない場合にのみ使われ、エイリアスだけを含みます。日付付きのスナップショット ID は決して含みません。

プロンプトは引数ではなく **stdin** で送られます。これにより、コマンドラインの長さ制限や、シェルごとのクォートの違いを回避できます。

出力形式が `text` ではなく `stream-json` なのには理由が 1 つあります。測定したところ、`text` は実行がほぼ終わるまで何も出力しない（8.9 秒の実行で最初の出力が 8.1 秒時点）ため、固まったエージェントと忙しいエージェントを見分ける方法がありません。ストリーミング形式は実行中ずっと `system`/`thinking_tokens` イベントを出力し、アイドル期限はこれを監視します。最終的な回答は `result` イベントから取得し、なければアシスタントのテキストブロック、さらに生の stdout へとフォールバックします。そのため、スキーマが変わっても出力が失われるのではなく、段階的に劣化するだけで済みます。`options.output_format: text` で元に戻すこともできますが、停止検出が犠牲になります。これがデフォルトではない理由です。

このアダプタが受け付けるパーミッションモードは、モデルのエイリアスとまったく同じように CLI 自身の `--permission-mode` のヘルプテキストから読み取られます。そのため、`options.permission_mode` は実際にインストールされているものに対して検証されます。

`acceptEdits` はファイル編集を自動承認しますが、シェルコマンドは承認しません。そのため、テストスイートの実行を求められた Implementer が実行できないことがあります。回避策は 2 つあり、推奨順に次のとおりです。

1. プロジェクト自身の `.claude/settings.json` でコマンドを許可リストに入れる（`permissions.allow`: `Bash(pytest:*)`）。範囲が狭く、設定がプロジェクトとともに管理されます。
2. そのロールだけ、より緩いモードを設定する:

```yaml
implementer:
  provider: claude
  options:
    permission_mode: bypassPermissions
```

または、1 回の実行だけその場で指定します。

```bash
dev-orchestra run implementer --prompt-file plan.md --extra --permission-mode bypassPermissions
```

`--extra` はそれ以降のすべてをそのまま CLI に転送し、後に指定したフラグが優先されます。いずれの場合も、`plan` モードと `review` モードは読み取り専用のままです。アダプタは設計上、これらのモードでは制限を緩める `permission_mode` を無視します。

<a id="codex-adapter"></a>

## Codex アダプタ

`codex` 0.154.x で検証済みです。

| 項目 | 方法 |
| --- | --- |
| 非対話実行 | `codex exec --skip-git-repo-check --color never -C <cwd>`、プロンプトは stdin で渡す |
| モデル | `-m <model>`。`recommended-coding` family の場合は**省略** |
| モデルの検出 | `codex debug models`（CLI 自身のカタログ、0.154 以降）と `$CODEX_HOME/config.toml` の `model` キー。`hide` が付いたモデルはスキップ |
| `plan` / `review` | `-s read-only` |
| `implement` | `-s workspace-write --approve-for-me` |
| 最終的な回答 | イベントストリームから抜き出すのではなく、`-o <file>` で取得 |
| 認証 | 環境を継承。`OPENAI_API_KEY` または `$CODEX_HOME/auth.json` で有無を検出 |

ロールのオプション: `sandbox`（`read-only` / `workspace-write` / `danger-full-access`）と `approve`（`false` にすると `--approve-for-me` を外します）。どちらも `plan` と `review` では無視され、これらは常に `-s read-only` を使います。

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
3. `_discover_models`、`_resolve_latest`、`build_command`、`auth_status` を実装します。`run` をオーバーライドするのは、CLI が特別な出力の取得を必要とする場合だけです。
4. `plan` と `review` が本当に読み取り専用のモードに対応付けられていることを確認します。
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
    name = "mycli"  # what `provider:` takes in config.yaml; must not be claude, codex or mock
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
- built-in の名前（`claude`、`codex`、`mock`）は拒否されます。built-in が優先されます。
- 2 つのファイルが同じ名前を提供する場合は、ソート順で先のものが優先され、後のものは報告されます。
- 自分で `register()` を呼んだり、レジストリに触れたりしないでください。ローダーは `build_provider()` が返したものを、最初の呼び出しで読み取った名前で登録します。インポート時、または `build_provider()` から（ローダーによる呼び出しでも、それ以降の呼び出しでも）自分で何かを登録するモジュールは拒否され、変更した内容は元に戻されます。これは、`register()` の呼び出しを残したままプラグインからコピーしたアダプタのような、うっかりしたミスを捕まえるためのものです。これを回避するように書かれたモジュールに対する防御ではありません。そのようなモジュールはプロセス内の何でも再束縛できます（下記参照）。
- `build_provider()` や `__init__` から CLI を起動しないでください。また、モジュールレベルで `sys.exit()` を呼ばないでください。このファイルはすべてのコマンドでインポートされます。読み込み中に送出された `SystemExit` は、他のものと同様に読み込みエラーとして記録されます。

<a id="when-it-goes-wrong"></a>

### うまくいかないとき

インポートに失敗したファイル、契約を破ったファイル、拒否されたファイルが CLI を止めることはありません。他のすべてのコマンドはそれを除いて処理を続け、`dev-orchestra doctor` はそれを **User providers** の下にエラーとともに一覧表示し、問題の 1 つとしても挙げます（そのため `doctor --strict` は失敗します）。`doctor` は、診断中に（`detect()`、`list_models()`、またはモデル解決から）例外を送出したアダプタも捕捉し、元のファイルに対して `adapter-error` として報告します。アダプタを書いたら、何よりも先に `doctor` を実行してください。

このディレクトリはサンドボックスではなく、信頼されたコードを置く場所です。その中のすべてのファイルは、CLI が起動するたびに、あなたの権限で CLI 自身のプロセスにインポートされ、CLI が行うあらゆることを変更できます。自分で実行してもよいと思えるコードだけを置いてください。`doctor` が常にその場所とインポートしたものを表示するのはそのためです。壊れたアダプタが邪魔になっている場合など、ディレクトリを完全にスキップするには `DEV_ORCHESTRA_NO_USER_PROVIDERS=1` を設定します。これが設定されている間、`config validate`、`config show`、`doctor` は、見つけられない provider の横にその旨を表示するため、設定済みのユーザーアダプタがファイルの欠落と誤解されることはありません。

同じプロセス内でディレクトリを再度読み込む（`load_user_providers()`）と、メモ化された検出結果もクリアされるため、編集したアダプタが新たに検出されます。

<a id="interface-stability"></a>

### インターフェースの安定性

`base.Provider` とその周辺の型（`ModelCandidate`、`ResolvedModel`、`RunResult`、`Usage`、`Detection`）はプラグインの内部のものであり、マイナーバージョン間で変更される可能性があります。プラグインのバージョンを固定するか、更新後に `dev-orchestra doctor` を実行して、アダプタがまだ読み込めることを確認してください。`run()` のシグネチャは最も変わりやすい部分です。`tests/test_provider_contract.py` は、上記の例を含むすべてのアダプタがこれに従っていることを検証します。

<a id="failure-semantics"></a>

## 失敗時の挙動

| 状況 | 結果 |
| --- | --- |
| CLI が PATH にない | 分かりやすいメッセージ付きの `RunResult(ok=False, exit_code=127)` |
| CLI を実行できない | `exit_code=126` |
| タイムアウト | `exit_code=124`、`timed_out=True` — 報告されるだけで、例外は送出されない |
| 0 以外の終了コード | `ok=False`、stderr を取得して秘匿化 |
| 解決できないモデル | 何かが実行される前に `ModelResolutionError` |

取得したすべてのストリームは `redact()` を通ります。これは、何かが `.ai/` やコンソールに届く前に、認証情報のような形の部分文字列を消去します。
