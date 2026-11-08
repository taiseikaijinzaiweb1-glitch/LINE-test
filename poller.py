"""LINE Push ポーラー（単独プロセス版・テスト用）.

なぜ別プロセスか:
- 従来は app.py の中で gunicorn ワーカーのスレッドとしてポーラーを動かしていた。
  Render のログに WORKER TIMEOUT / SIGKILL が出てワーカーが殺され・再起動する動きがあり、
  そのたびにポーラーのスレッドも一緒に消えるため、回答済みファイルが拾われないままになる。
- このスクリプトは gunicorn とは別のプロセスで動き、ワーカーの生死に影響されない。
  起動は start.sh から: `python poller.py &` のあと gunicorn を起動する。

動き: 30秒おきに GitHub の queue/ を見て、回答済み・未送信のファイルを
LINE Push API で送り、送れたら GitHub のファイルを「送信済み・sent:true」に書き戻す。
何もなくても1巡ごとに件数をログに出すので、Logs に何も出なければポーラー自体が
動いていない、と切り分けられる。

環境変数（Render の Environment Variables。値はチャットに貼らない）:
  LINE_CHANNEL_ACCESS_TOKEN
  GITHUB_TOKEN
  GITHUB_REPO           … 既定は taiseikaijinzaiweb1-glitch/LINE-test
  GITHUB_BRANCH         … 既定は main
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("line-qa-poller")

CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "taiseikaijinzaiweb1-glitch/LINE-test")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
QUEUE_DIR = "queue"

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
GITHUB_API = "https://api.github.com"


def gh_request(method: str, path: str, payload: dict | None = None) -> dict | list | None:
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
        log.error("GitHub API %s %s: HTTP %s %s", method, path, exc.code, exc.read()[:300])
        return None
    except Exception as exc:
        log.error("GitHub API 呼び出しに失敗しました: %s", exc)
        return None


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
    except urllib.error.HTTPError as exc:
        # 旧版は例外の文字だけしか残さなかった。LINE のエラー本文（code/message）が
        # 原因切り分けの決め手になるので、必ず残す。
        log.error("LINE push API: HTTP %s %s", exc.code, exc.read()[:500])
        return False
    except Exception as exc:
        log.error("LINE push API 呼び出しに失敗しました: %s", exc)
        return False


def poll_once() -> None:
    listing = gh_request("GET", f"/repos/{GITHUB_REPO}/contents/{QUEUE_DIR}?ref={GITHUB_BRANCH}")
    if not isinstance(listing, list):
        log.warning("queue/ の一覧が取れませんでした（GitHub API の応答を確認してください）")
        return
    pending = 0
    for item in listing:
        if item.get("type") != "file" or not item.get("name", "").endswith(".json"):
            continue
        path = item["path"]
        detail = gh_request("GET", f"/repos/{GITHUB_REPO}/contents/{path}?ref={GITHUB_BRANCH}")
        if not isinstance(detail, dict) or "content" not in detail:
            continue
        try:
            body = json.loads(base64.b64decode(detail["content"]).decode("utf-8"))
        except Exception:
            continue
        if body.get("status") == "回答済み" and not body.get("sent") and body.get("answer", "").strip() and body.get("userId"):
            pending += 1
            log.info("回答済みのファイル %s を Push します", path)
            if push_message(body["userId"], body["answer"]):
                body["status"] = "送信済み"
                body["sent"] = True
                content = base64.b64encode(
                    json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
                ).decode("utf-8")
                gh_request(
                    "PUT",
                    f"/repos/{GITHUB_REPO}/contents/{path}",
                    {
                        "message": f"Mark sent {path}",
                        "content": content,
                        "sha": detail.get("sha"),
                        "branch": GITHUB_BRANCH,
                    },
                )
    log.info("queue/ を確認しました: %d件, 回答済み・未送信 %d件", len(listing), pending)


def main() -> None:
    log.info("ポーラーを開始します repo=%s branch=%s", GITHUB_REPO, GITHUB_BRANCH)
    while True:
        try:
            poll_once()
        except Exception as exc:  # 1巡の失敗でプロセスごと死なせない
            log.error("GitHubの確認中にエラー: %s", exc)
        time.sleep(30)


if __name__ == "__main__":
    main()
