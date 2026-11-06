# Render無料枠でLINE Q&Aボットを数回だけ試す手順

対象：架空就業規則Q&A test（テスト用）。無料枠だけで終わらせる前提。
この手順では秘密値をチャットやコード、GitHubに貼らないこと。

## 0. 用意するもの

- GitHubアカウント（なければ作る）
- Renderアカウント（GitHub連携で作れる。クレジットカード不要）
- チャネルシークレットとチャネルアクセストークンの値
  （LINE Developersコンソールで確認する。Renderの画面で直接入れる）

## 1. GitHubにコードを置く

1. GitHubで新しいリポジトリを作る（PublicでもPrivateでもよい。READMEの追加は不要）。
2. `prototype/` の中身をリポジトリの直下に置く。直下に来るもの：`app.py`、`qa.py`、`rules.docx`、`requirements.txt`、`render.yaml`、`DEPLOY.md`、`README.md`、`.env.example`、`.gitignore`。
3. 入れてはいけないもの：`.env`（作っていたら含めない）、`.venv/`、`bin/`（cloudflaredはRenderでは不要）。

## 2. RenderでWeb Serviceを作る

1. [Render](https://dashboard.render.com/)にGitHubアカウントでログインする。
2. 「New」→「Web Service」を選び、手順1のリポジトリを選択する。
3. `render.yaml` があると設定が自動で入る。入らない場合は手で入れる：
   - Name：`line-qa-bot-test`
   - Region：`Singapore`（日本から一番近い）
   - Build Command：`pip install -r requirements.txt`
   - Start Command：`gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 60`
   - Plan：**Free** を選ぶ（有料プランを選ばないこと）
4. 「Environment Variables」で2つ追加する（値はRenderの画面で直接入れる）：
   - `LINE_CHANNEL_SECRET` ＝ チャネルシークレット
   - `LINE_CHANNEL_ACCESS_TOKEN` ＝ チャネルアクセストークン
5. 「Create Web Service」を押す。デプロイ完了まで2〜3分待つ。

## 3. URLを確認する

1. デプロイ後に表示されるURL（例：`https://line-qa-bot-test.onrender.com`）を開く。
2. `/health` を開く（例：`https://line-qa-bot-test.onrender.com/health`）。
3. `{"status":"ok","articles":61,...}` と出ればOK。`articles` が61でない・エラーが出る場合は、デプロイログを確認する。
4. 注意：Freeプランは15分アクセスがないとスリープする。テスト直前に `/health` を開いて起こしてから質問すること。起こすのに1分ほどかかる。

## 4. LINE側にWebhook URLを設定する

1. LINE Developersコンソールで対象チャネルを開き、「Messaging API」タブを開く。
2. Webhook設定に `https://<Renderのホスト>/webhook` を入れる（例：`https://line-qa-bot-test.onrender.com/webhook`）。「更新」を押す。
3. 「検証」を押す。成功すれば署名検証と疎通はOK（検証時は返信は送られない）。
4. 「Use webhook」をオンにする。
5. LINE Official Account Managerの「応答設定」で、自動応答メッセージとあいさつメッセージをオフにする（オンのままだとボット返信と二重になる）。

## 5. 数問だけテストする

1. 個人のLINEから公式アカウントを友だち追加する。
2. 数問だけ送ってみる。例：「給料日はいつ？」「試用期間はどれくらい？」「賞与はいくら？」（最後は「この規則には書かれていません」の定型文が返る想定）。
3. 返信が来ないときの確認：Renderのサービスがスリープしていないか（`/health` を開いて起こす）、Webhookの「検証」が成功しているか、「Use webhook」がオンか、環境変数が入っているか。
4. LINEの無料プランは月200通まで。数問なら余裕で収まる。

## 6. 終わったら止める

1. Renderダッシュボードで対象サービスを開き、「Suspend」（停止）か「Delete」（削除）する。無料枠で終わらせるため、置きっぱなしにしないこと。
2. LINE Developersコンソールで「Use webhook」をオフに戻す（任意）。

## トラブル時のメモ

- デプロイが失敗する：Renderの「Logs」で原因を確認する。`requirements.txt` の中身とPythonバージョンが疑わしい場合は親指に伝える。
- Webhookの「検証」が失敗する：URLが `https://<ホスト>/webhook` になっているか、環境変数 `LINE_CHANNEL_SECRET` が正しいか確認する。
- 最初の返信だけ遅い／来ない：Freeプランのスリープからの復帰（約1分）の可能性がある。`/health` を開いて起こしてから、もう一度質問する。
