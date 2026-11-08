#!/bin/sh
# Render 用の起動スクリプト（ローカル用の start.sh とは別）。
# Render の Start Command には `sh start_render.sh` と入れること。
# ポーラーを別プロセスで先に立ち上げてから gunicorn を起動する。
# gunicorn の --timeout は 60 から 120 に延ばしてある（ワーカーの強制終了を減らすため）。
python poller.py &
exec gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120
