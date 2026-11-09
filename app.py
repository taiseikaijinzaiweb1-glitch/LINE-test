"""LINE Webhook（検知専用版・テスト用）.

目的: Webhookで届いた質問をすぐMuse側に知らせるための受け口。
  返信はこのサーバーでは送らない。Muse側（Hookワーカー）が Push API で送る。

動き:
- POST /webhook: 署名を検証し、テキストメッセージの最新1件をメモリに保存する。
  予備としてGitHubの queue/ にも質問ファイルを1つ作る（従来のv2と同じ）。
  GitHubへの書き込みに失敗しても、メモリへの保存と200応答は続ける。
- GET /latest-question: 最新の質問を返す。Hookが5秒おきに読む。
  {"status": "ok", "question": ..., "userId": ..., "received_at": ...,
   "replyToken": ..., "detected_at": ...} または {"status": "empty"}
- GET /health: 動作確認用。mode は "webhook-detect"。

置き場: GitHubリポジトリ taiseikaijinzaiweb1-glitch / LINE-test、ブランチ main
  このファイルを GitHub の app.py として上書きして使うこと。
  起動は今までと同じ start_render.sh（poller.py は残るが、GitHubに
  回答済みファイルが無ければ何も送らないため、HookのPushと重複しない）。

環境変数（Render の Environment Variables に入れる。値はチャットに貼らない）:
  LINE_CHANNEL_SECRET
  LINE_CHANNEL_ACCESS_TOKEN（このアプリでは使わない。PushはMuse側で行う）
  GITHUB_TOKEN          … 予備のキュー書き込み用（無くても検知は動く）
  GITHUB_REPO           … 既定は taiseikaijinzaiweb1-glitch/LINE-test
  GITHUB_BRANCH         … 既定は main
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
log = logging.getLogger("line-qa-bot-detect")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DOCX = BASE_DIR / "rules.docx"

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

# 最新の質問1件（メモリ内のみ。gunicornはワーカー1のため共有できる）
_LATEST_LOCK = threading.Lock()
LATEST: dict = {"status": "empty"}


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


def create_question_file(user_id: str, question: str, now: datetime) -> bool:
    """予備としてGitHubの queue/ に質問ファイルを1つ作る。失敗しても検知は続く。"""
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
            "mode": "webhook-detect",
            "articles": len(RULEBOOK.articles) if RULEBOOK else 0,
            "channel_secret_configured": bool(CHANNEL_SECRET),
            "github_token_configured": bool(GITHUB_TOKEN),
            "github_repo": GITHUB_REPO,
        }
    )


@app.get("/")
def index():
    return jsonify({"service": "line-qa-bot webhook-detect (test)", "health": "/health", "latest": "/latest-question", "webhook": "/webhook"})


@app.get("/latest-question")
def latest_question():
    with _LATEST_LOCK:
        return jsonify(dict(LATEST))


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
        now = datetime.now(timezone(timedelta(hours=9)))
        received_at = now.strftime("%Y-%m-%dT%H:%M")
        record = {
            "status": "ok",
            "question": question,
            "userId": user_id,
            "received_at": received_at,
            "replyToken": event.get("replyToken", ""),
            "detected_at": now.isoformat(timespec="seconds"),
        }
        with _LATEST_LOCK:
            LATEST.clear()
            LATEST.update(record)
        log.info("Q: %r (user=%s)", question[:60], user_id[:8])
        # 返信は送らない。予備のキュー書き込みだけ別スレッドで行う。
        threading.Thread(target=create_question_file, args=(user_id, question, now), daemon=True).start()

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
