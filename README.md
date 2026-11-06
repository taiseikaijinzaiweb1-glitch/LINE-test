# LINE Q&A返信ボット — プロトタイプ（テスト用）

アカウント: 架空就業規則Q&A test（基本ID `@847rcvod`）
参照資料: 架空の「医療法人青葉会 就業規則（テスト用サンプル）」2026年4月1日制定・全61条
（実在の病院の規則ではなく、法令適合性は保証されていないテスト資料）

## これは何か

LINEの友だちからのテキストメッセージを受け取り、就業規則の条文だけを根拠に
日本語で返信するWebhookサーバーのプロトタイプ。

- `POST /webhook` — LINEからのイベント受信。`X-Line-Signature` を
  HMAC-SHA256で検証し、テキストには `qa.py` の簡易Q&Aで回答して
  LINE Reply API（`https://api.line.me/v2/bot/message/reply`）を呼ぶ。
- `GET /health` — 稼働確認。条文の読み込み件数と、秘密情報の設定有無（値そのものは出さない）を返す。
- 回答方針: 規則にないことは推測せず「ご質問に関する情報は、就業規則に見当たりませんでした。お手数ですが、人事総務部（TEL：xxx-xxxx-xxxx）までご連絡ください。」と返す（テスト用のため電話番号は xxx-xxxx-xxxx と表示）。
  金額・時期を聞かれて条文が「別途定める」としか書いていない場合もその旨を明記する。

## ファイル一覧

| ファイル | 内容 |
|---|---|
| `app.py` | Flaskアプリ本体（/webhook, /health, 署名検証, Reply API呼び出し） |
| `qa.py` | 就業規則docxの読み込み（標準ライブラリのみ）とキーワードQ&Aロジック |
| `rules.docx` | 就業規則（テスト用サンプル）の同梱版。app.pyの既定の参照先 |
| `requirements.txt` | `Flask>=3.0`、`gunicorn>=22.0` |
| `render.yaml` | Render用設定（FreeプランのWeb Service、gunicorn起動、/health） |
| `DEPLOY.md` | Render無料枠で数回だけ試す手順書 |
| `.env.example` | 環境変数の雛形（プレースホルダのみ。秘密値は書かない） |
| `start.sh` | venv作成＋依存インストール＋起動 |
| `start-tunnel.sh` | cloudflared Quick Tunnelで公開HTTPS化 |
| `bin/cloudflared` | cloudflared 2026.10.7（linux-amd64。GitHubリリースから取得済み） |
| `.gitignore` | `.env`, `.venv/` を除外 |

秘密値（チャネルシークレット、チャネルアクセストークン）はどのファイルにも書かないこと。
`.env` は各自の手元でのみ作成する。

## Renderへデプロイする場合

Render無料枠で数回だけ試す手順は [DEPLOY.md](DEPLOY.md) を参照。
`render.yaml`（Freeプラン）、`rules.docx`（就業規則の同梱版）、gunicorn対応済み。

## 起動方法

```bash
cd ~/workspace/goals/line-official-account-q-a-replies/prototype
cp .env.example .env
# .env を編集して LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を入れる
./start.sh
# → http://localhost:8000 で待ち受け。/health で確認:
curl http://localhost:8000/health
```

トークン未設定でもサーバーは起動する。その場合は返信を送らずログだけ残す
（署名検証とQ&Aロジックのテストができる）。

## Webhook URLの設定方法

1. サーバーを起動する（`./start.sh`）。
2. 別ターミナルで `./start-tunnel.sh` を実行する。表示されるURLを控える。
   例: `https://abcd-1234.trycloudflare.com`
3. LINE Developersコンソール > 対象チャネル > Messaging APIタブ >
   Webhook設定で `https://<表示されたホスト>/webhook` を入力し、「更新」と「検証」を押す。
   - 検証が成功すれば署名検証と疎通はOK（検証時は返信は送られない）。
   - 「Use webhook」をオンにする。
4. 公式アカウントの「応答設定」で、自動応答メッセージとあいさつメッセージをオフにする
   （オンのままだとボット返信と二重になる）。
5. テスト用の個人LINEから公式アカウントを友だち追加し、質問を送る。

## 公開URLと常駐化の調査結果（2026-11-06確認）

**結論: このVMから直接、安定した公開HTTPSのWebhook URLを出す方法は確認できなかった。**
下記の通り、cloudflaredは出口プロキシにブロックされ、localtunnelはURL発行まで到達しない。
プロトタイプ自体は完成しており、外部ホストに置けばそのまま動く。
トンネルをVM内で無理に維持するのではなく、外部ホスティングへの移行が推奨。

| 方法 | 結果 |
|---|---|
| cloudflared Quick Tunnel | **不可（確認済み）**。バイナリは取得済み（`bin/cloudflared`, 2026.10.7）だが、トンネル発行時に `https://api.trycloudflare.com/tunnel` へPOSTすると出口プロキシにブロックされる（HTTP 403 "Your request was blocked"）。`tls: first record does not look like a TLS handshake` で失敗。再試行しても同じ。 |
| cloudflared Named Tunnel（固定URL） | 固定URLにするにはCloudflareアカウント＋独自ドメインが必要で、現状ないため不可。Quick Tunnel自体が使えないので併せて不可。 |
| ngrok | 未インストール。アカウント登録とトークンが必要で、無料枠はURLが毎回変わる。cloudflaredと同様にトンネル系エンドポイントが出口でブロックされる可能性が高く、未検証。 |
| localtunnel（npx） | **不可（未達）**。`https://localtunnel.me` 自体は302で到達可能だが、`npx localtunnel --port 8000` を45秒待ってもURLが発行されず無出力のまま。トンネルサーバーへの接続が確立しない模様。 |
| Tailscale | 未接続。かつクライアント専用でインバウンドを受け付けないため、LINEサーバーからのWebhook受け取りには使えない。不可。 |
| VM内蔵 ingress-rev-proxy | Meta内部のSpaces用プロキシで、任意のローカルポートへの転送ルートはない。Webhook用には使えない。不可。 |
| 直接公開（VMの公開IP:443） | 公開IP・ポート開放の手段がこのVMにはない。不可。 |

### 次の選択肢（推奨順）

1. **外部ホスティングにデプロイする（推奨）**。Render / Fly.io / さくらのVPSなどの
   常時稼働ホストにこの `prototype/` を置き、環境変数に秘密値を設定して公開URLを取得する。
   そのURLをLINE DevelopersコンソールのWebhookに設定する。URLは固定になる。
2. 別のネットワーク（出口プロキシを経由しない環境）でcloudflared Quick Tunnelを試す。
   このVMの出口ではブロックされるため、VM内での再試行は期待できない。
3. トンネルがどうしても必要な場合は、ngrokなど別サービスの到達性を個別に検証する
   （アカウント作成が必要なものはユーザー承認を得てから）。

### 常駐化・再起動後の永続性

- Python 3.12.3、Node v24.22.1あり。Flaskはvenvへのpipインストールで動作確認済み（PyPIに到達可能）。
- システムsystemd（PID 1）は動いているが、`systemctl --user` はバスなしで使えない。
  `/etc/systemd/system` へのユニット配置はrootでは可能だが、`~/workspace` 外は
  VM再起動・交換で消える扱いのため、永続化の手段としては当てにできない。
- `crontab` コマンドは未インストール。ユーザー用cronはない。
- **結論: このVMでは「再起動しても自動で復旧する常駐化」は保証できない。**
  運用は「`./start.sh` と `./start-tunnel.sh` を手動で再実行する」前提。
  Muse側のcron（親エージェント管理）で起動スクリプトを定期実行する監視・復旧は検討余地あり。
  本番運用ならRender / Fly.io / Cloudflare Workersなど外部ホスティングへの移行が必要。

## 動作確認（2026-11-06実施）

- `/health` が `{"status":"ok","articles":61,...}` を返すことを確認。
- ダミーの署名付きPOSTで検証が通ること（200）、署名なし・誤った署名では403になることを確認。
- 返信は別スレッドで呼ぶよう修正済み。初回はReply API呼び出しが同期でWebhook応答を
  10秒以上遅らせたため。ダミートークンではReply APIが401を返し、応答は即200になることを確認。
- Q&Aロジックの単体テスト: 試用期間・勤務時間・有給・給与・退職・定年など主要トピックと、
  「賞与はいくら？」「夜勤手当はいくら？」（書かれていませんになること）を確認。
- **公開トンネルは未成功**: cloudflared Quick Tunnelは出口プロキシにブロックされ失敗
  （`api.trycloudflare.com` が403）。localtunnelは45秒待ってもURL発行なし。
  よって外部からのWebhook到達テストは未実施。トンネル経由の疎通は未確認。

## 未解決の課題

- チャネルシークレット未受領 → 署名検証の実値テストはまだできない（ダミー秘密値での検証は済み）。
- チャネルアクセストークンは発行済みで、親エージェントが安全な方法で受け取り済み
  （値はチャットに残さない）。実LINEへの返信テストはWebhook公開後に実施する。
- **Webhook URL未設定 → このVMからは公開URLが出せなかったため、LINE Developersコンソールでの
  設定と「検証」はまだ。外部ホスティングへのデプロイが先決。**
- Quick TunnelのURLは再起動のたびに変わる → その都度LINE側のWebhook URL更新が必要。
  （ただし現状はトンネル自体が使えないため、この注意は外部ホスト移行後は不要になる見込み）
- Quick TunnelのURLは再起動のたびに変わる → その都度LINE側のWebhook URL更新が必要。
- Q&Aはキーワード一致の簡易版。言い換え（例:「給料日」→OKだが「お金が振り込まれる日」→拾えない）には弱い。
  本格化するなら条文の全文検索スコアリングや、LLMに条文を渡して回答させる方式への置き換えが必要。
