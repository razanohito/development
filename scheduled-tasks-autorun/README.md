# 定期タスクを確認待ちで止めずに最後まで動かす設定

Claude デスクトップアプリの「Scheduled tasks（定期タスク）」は PC 内の
`C:\Users\schun\.claude\scheduled-tasks\<タスク名>\SKILL.md` に保存されています。
クラウドのセッションからはこのフォルダに届かないため、PC 上で下のスクリプトを 1 回実行してください。

## 手順 1〜4：スクリプトを実行（自動）

PowerShell で、このフォルダに移動して実行します。

```powershell
# まず確認だけ（何も変更しない）。結果は scheduled_tasks_report.md に保存されます
python scheduled_tasks_autorun.py

# 内容に問題なければ適用
python scheduled_tasks_autorun.py --apply
```

`python` が見つからない場合は `py scheduled_tasks_autorun.py --apply` を使ってください。
定期タスクの実行履歴が「0 セッション」と出た場合は `--all-sessions` を付けると、
すべての履歴から使ったツールを集計します。

スクリプトがやること：

| 手順 | 内容 |
|---|---|
| 1 | `scheduled-tasks\*\SKILL.md` から全タスクの名前・説明・指示文を表にする。実行時刻はアプリの設定ファイルから探し、見つからなければ「アプリで確認」と表示 |
| 2 | `.claude\projects\**\*.jsonl`（実行履歴）から、定期タスク実行で使われたツールと、拒否・停止されたツールを集計 |
| 3 | `settings.json` を `settings.json.bak` にバックアップしてから `permissions.allow` に追記（既存設定は残す）。取り消せない操作は `permissions.ask` に入れて確認を残す |
| 4 | 各 `SKILL.md` を `SKILL.md.bak` にバックアップしてから、指示文の最後に「途中で質問せず最後まで完了させる」文を追記（2 回実行しても二重には追記しません） |

### allow に入れず、確認を残す操作（permissions.ask）

`ask` は `allow` より優先されるため、以前「常に許可」を押していた操作でも確認が出るようになります。

- 削除：`rm` / `rmdir` / `del` / `Remove-Item` / `git rm` / `git clean`、Gmail の `trash_*`・`delete_*`、Drive の `trash_file`、履歴で見つかった MCP の削除系ツール（例：`delete_post`）
- メール送信：Gmail の `send_message` / `reply` / `forward`
- クラウドワークス／ランサーズへの応募送信：コマンドに `crowdworks`・`lancers` と `apply`・`submit`・`応募` を含むもの
- `git push --force` / `-f` / `--force-with-lease`
- 加えて Drive の `share_file`（外部への共有は取り消しにくいため）

## 手順 5：権限モードを変更（アプリで操作）

定期タスクの権限モードは SKILL.md ではなくアプリ側に保存されているので、画面で変更します。

1. デスクトップアプリの Code タブで定期タスク（Scheduled）の一覧を開く
2. タスクを選んで **Edit** を押す
3. 指示文の入力欄の下にある権限モードの選択を **Auto** にする
   （Auto が選べない場合は **Accept edits**。手順 3 の allow と組み合わせると確認が減ります）
4. **Save** を押す。これを全タスクで行う

`Bypass permissions` は使わないでください。確認を残したい操作まで止まらずに実行される可能性があります。

## 手順 6：テスト実行

1. タスクの画面で **Run now** を押す
2. 最後まで動いたか、報告に「完了したこと／保留したこと／理由」があるか確認する
3. 途中で許可確認が出た場合は、もう一度 `python scheduled_tasks_autorun.py --apply` を実行する。
   新しく使われたツールが履歴から見つかり、allow に追加されます
