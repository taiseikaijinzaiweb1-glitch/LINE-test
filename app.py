"""LINE Webhook（GitHub経由版・テスト用）.

今までの app.py との違い:
- 質問が来てもすぐには返信しない。GitHubリポジトリに質問ファイルを1つ作るだけ。
- 親指（Muse）が定期的に（または手動で）GitHubを見て、未回答のファイルに回答を書き込む。
- このサーバーが30秒おきにGitHubを見て、回答済みのファイルを LINE Push API で送る。
- 【v2 変更】ポーラーは app.py から外し、別プロセスの poller.py に移した。
  gunicorn ワーカーのスレッドとして動かすと、WORKER TIMEOUT でワーカーごと
  殺されたときにポーラーも一緒に消えてしまうため。app.py は Webhook 専用になった。
  起動は start_render.sh から行う（poller.py を先に起動してから gunicorn）。

置き場: GitHubリポジトリ taiseikaijinzaiweb1-glitch / LINE-test、ブランチ main
  フォルダ: queue/
  ファイル: 1つの質問に1つ。例 queue/20261106-173000-a1b2.json
  中身: {"datetime": "2026-11-06 17:30:00", "userId": "...", "question": "...",
          "status": "未回答", "answer": "", "sent": false}
  状態の流れ: 未回答 -> 回答済み（親指が書く） -> 送信済み（このサーバーが送ったあと）

環境変数（Render の Environment Variables に入れる。値はチャットに貼らない）:
  LINE_CHANNEL_SECRET
  LINE_CHANNEL_ACCESS_TOKEN
  GITHUB_TOKEN          … GitHub の Personal Access Token（Contents: Read and write）
  GITHUB_REPO           … 既定は taiseikaijinzaiweb1-glitch/LINE-test
  GITHUB_BRANCH         … 既定は main

追加のライブラリは要らない。GitHub API は標準ライブラリの urllib だけで呼ぶ。
起動は今までと同じ: gunicorn app:app --bind 0.0.0.0:$PORT
このファイルを GitHub の app.py として上書きして使うこと。
上書きする前に、今の app.py を app_immediate.py として残しておくこと。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import random
import string
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, jsonify, request

from qa import RuleBook

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("line-qa-bot-github")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DOCX = BASE_DIR / "rules.docx"

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
GITHUB_API = "https://api.github.com"


def load_dotenv(path: Path = BASE_DIR / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()

CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
RULES_DOCX = os.environ.get("RULES_DOCX", str(DEFAULT_DOCX))
PORT = int(os.environ.get("PORT", "8000"))

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "taiseikaijinzaiweb1-glitch/LINE-test")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
QUEUE_DIR = "queue"

try:
    RULEBOOK = RuleBook.load(RULES_DOCX)
    log.info("就業規則を読み込みました: %s（%d条）", RULES_DOCX, len(RULEBOOK.articles))
except Exception as exc:
    RULEBOOK = None  # type: ignore[assignment]
    log.error("就業規則の読み込みに失敗しました: %s", exc)

app = Flask(__name__)


def gh_request(method: str, path: str, payload: dict | None = None) -> dict | list | None:
    """GitHub REST API を呼ぶ。失敗時は None を返してログに残す。"""
    if not GITHUB_TOKEN:
        log.warning("GITHUB_TOKEN 未設定のため GitHub API を呼びません")
        return None
    url = f"{GITHUB_API}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        log.error("GitHub API %s %s: HTTP %s %s", method, path, exc.code, exc.read()[:200])
        return None
    except Exception as exc:
        log.error("GitHub API 呼び出しに失敗しました: %s", exc)
        return None


def create_question_file(user_id: str, question: str) -> bool:
    now = datetime.now(timezone(timedelta(hours=9)))
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    fname = f"{now.strftime('%Y%m%d-%H%M%S')}-{rand}.json"
    path = f"{QUEUE_DIR}/{fname}"
    body = {
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "userId": user_id,
        "question": question,
        "status": "未回答",
        "answer": "",
        "sent": False,
    }
    content = base64.b64encode(json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")).decode("utf-8")
    res = gh_request(
        "PUT",
        f"/repos/{GITHUB_REPO}/contents/{path}",
        {"message": f"Add LINE question {fname}", "content": content, "branch": GITHUB_BRANCH},
    )
    if res is None:
        return False
    log.info("GitHubに質問ファイルを作りました: %s", path)
    return True


def push_message(user_id: str, text: str) -> bool:
    if not CHANNEL_ACCESS_TOKEN:
        log.warning("LINE_CHANNEL_ACCESS_TOKEN 未設定のため Push を送りません")
        return False
    payload = json.dumps(
        {"to": user_id, "messages": [{"type": "text", "text": text}]},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        LINE_PUSH_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("LINE push API: HTTP %s", resp.status)
            return True
    except Exception as exc:
        log.error("LINE push API 呼び出しに失敗しました: %s", exc)
        return False


def verify_signature(body: bytes, signature: str | None) -> bool:
    if not CHANNEL_SECRET or not signature:
        return False
    digest = hmac.new(CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


@app.get("/health")
def health():
    return jsonify(
        {
            "status": "ok" if RULEBOOK else "rules-load-error",
            "mode": "github-queue",
            "articles": len(RULEBOOK.articles) if RULEBOOK else 0,
            "channel_secret_configured": bool(CHANNEL_SECRET),
            "channel_access_token_configured": bool(CHANNEL_ACCESS_TOKEN),
            "github_token_configured": bool(GITHUB_TOKEN),
            "github_repo": GITHUB_REPO,
        }
    )


@app.get("/")
def index():
    return jsonify({"service": "line-qa-bot github-queue (test)", "health": "/health", "webhook": "/webhook"})


@app.post("/webhook")
def webhook():
    body = request.get_data()
    signature = request.headers.get("X-Line-Signature")
    if not verify_signature(body, signature):
        log.warning("署名検証に失敗しました。")
        return jsonify({"error": "invalid signature"}), 403
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        return jsonify({"error": "invalid json"}), 400

    for event in data.get("events", []):
        if event.get("type") != "message":
            continue
        message = event.get("message", {})
        if message.get("type") != "text":
            continue
        question = message.get("text", "").strip()
        user_id = event.get("source", {}).get("userId", "")
        if not question or not user_id:
            continue
        log.info("Q: %r (user=%s)", question[:60], user_id[:8])
        # すぐ返信しない。GitHubにファイルを作るだけ。LINEには200をすぐ返す。
        threading.Thread(target=create_question_file, args=(user_id, question), daemon=True).start()

    return jsonify({"status": "ok"}), 200


# ポーラーはここでは立てない。別プロセスの poller.py が担当する。
# （v2: gunicorn ワーカーのスレッドだと WORKER TIMEOUT で一緒に消えるため）

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
