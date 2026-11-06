"""LINE Messaging API Webhook プロトタイプ（テスト用）.

アカウント: 架空就業規則Q&A test（基本ID @847rcvod）
チャネルID: 親エージェント経由で別途共有されるものを使う。
  ※ チャネルID・シークレット・アクセストークンはこのファイルには書かない。
  すべて環境変数（ローカルでは .env、Renderでは Environment Variables）で渡すこと。

エンドポイント:
  GET  /health   稼働確認用。条文の読み込み件数を返す。
  POST /webhook  LINEからのイベント受信。署名検証→Q&A回答→reply API。

起動:  ./start.sh   （ローカル用。venv作成＋依存インストール＋ .env 読込＋起動）
Render: gunicorn app:app --bind 0.0.0.0:$PORT  （render.yaml 参照）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, request

from qa import NOT_FOUND_REPLY, RuleBook

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("line-qa-bot")

BASE_DIR = Path(__file__).resolve().parent
# 参照する就業規則 docx。Renderではこのファイルと同階層の rules.docx を使う。
# 環境変数 RULES_DOCX があればそちらを優先する。
DEFAULT_DOCX = BASE_DIR / "rules.docx"

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"


def load_dotenv(path: Path = BASE_DIR / ".env") -> None:
    """簡易 .env 読込。既存の環境変数を上書きしない。"""
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

try:
    RULEBOOK = RuleBook.load(RULES_DOCX)
    log.info("就業規則を読み込みました: %s（%d条）", RULES_DOCX, len(RULEBOOK.articles))
except Exception as exc:  # 起動は止めず、/health で分かるようにする
    RULEBOOK = None  # type: ignore[assignment]
    log.error("就業規則の読み込みに失敗しました: %s", exc)

app = Flask(__name__)


def verify_signature(body: bytes, signature: str | None) -> bool:
    """X-Line-Signature を HMAC-SHA256 で検証する。"""
    if not CHANNEL_SECRET or not signature:
        return False
    digest = hmac.new(
        CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def reply_message(reply_token: str, text: str) -> None:
    """LINE Reply API で1件返信する。トークン未設定時は送らずログだけ残す。"""
    if not CHANNEL_ACCESS_TOKEN:
        log.warning("LINE_CHANNEL_ACCESS_TOKEN 未設定のため返信を送りません: %r", text[:80])
        return
    payload = json.dumps(
        {"replyToken": reply_token, "messages": [{"type": "text", "text": text}]},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        LINE_REPLY_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            log.info("LINE reply API: HTTP %s", resp.status)
    except Exception as exc:
        log.error("LINE reply API 呼び出しに失敗しました: %s", exc)


@app.get("/health")
def health():
    return jsonify(
        {
            "status": "ok" if RULEBOOK else "rules-load-error",
            "articles": len(RULEBOOK.articles) if RULEBOOK else 0,
            "channel_secret_configured": bool(CHANNEL_SECRET),
            "channel_access_token_configured": bool(CHANNEL_ACCESS_TOKEN),
        }
    )


@app.get("/")
def index():
    return jsonify({"service": "line-qa-bot (test)", "health": "/health", "webhook": "/webhook"})


@app.post("/webhook")
def webhook():
    body = request.get_data()
    signature = request.headers.get("X-Line-Signature")
    if not verify_signature(body, signature):
        log.warning("署名検証に失敗しました。X-Line-Signature が不正か未設定です。")
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
        reply_token = event.get("replyToken")
        question = message.get("text", "").strip()
        if not reply_token or not question:
            continue
        if RULEBOOK is None:
            answer = "就業規則を読み込めていません。しばらくしてからもう一度お試しください。"
        else:
            answer = RULEBOOK.answer(question)
        log.info("Q: %r -> A: %r", question[:60], answer[:80])
        # Reply API は別スレッドで呼ぶ。LINEへの200応答を遅らせないため。
        threading.Thread(
            target=reply_message, args=(reply_token, answer), daemon=True
        ).start()

    # LINEには必ず200を返す（返信失敗でも再送ループを避けるため）。
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    log.info(
        "起動: port=%d secret=%s token=%s articles=%s",
        PORT,
        "set" if CHANNEL_SECRET else "MISSING",
        "set" if CHANNEL_ACCESS_TOKEN else "MISSING",
        len(RULEBOOK.articles) if RULEBOOK else "load-error",
    )
    app.run(host="0.0.0.0", port=PORT)
