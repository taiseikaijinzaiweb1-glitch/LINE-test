"""LINE Webhook（ハイブリッド版・テスト用）.

動き:
- POST /webhook: 署名を検証する。
  1) まず固定返答に当てはまるか見る。当てはまれば Reply API ですぐ返す。
     Reply API は月200通の枠を使わない。1回限り・1分以内に使う。
  2) 当てはまらなければ、検知専用版と同じく最新1件をメモリに保存し、
     Hook（5秒おき）が読む GET /latest-question で親指（Muse）に回す。
     親指は Push API で返す（1通で枠を1通使う）。
  固定で返した質問は /latest-question に入れないため、親指が重複して
  返すことはない。Reply に失敗したときだけ、親指へ回す側に落とす。
- GET /latest-question: 親指へ回す最新の質問を返す。または {"status":"empty"}
- GET /health: mode は "hybrid-fixed"。

置き場: GitHubリポジトリ taiseikaijinzaiweb1-glitch / LINE-test、ブランチ main
  このファイルを GitHub の app.py として上書きして使うこと。
  起動は今までと同じ start_render.sh（poller.py は残るが、GitHubに
  回答済みファイルが無ければ何も送らないため、HookのPushと重複しない）。

環境変数（Render の Environment Variables に入れる。値はチャットに貼らない）:
  LINE_CHANNEL_SECRET
  LINE_CHANNEL_ACCESS_TOKEN … 固定返答の Reply 用（親指のPushとは別に使う）
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
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, jsonify, request

from qa import RuleBook

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("line-qa-bot-hybrid")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DOCX = BASE_DIR / "rules.docx"

GITHUB_API = "https://api.github.com"
REPLY_URL = "https://api.line.me/v2/bot/message/reply"


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

# 最新の質問1件（親指へ回すものだけ。gunicornはワーカー1のため共有できる）
_LATEST_LOCK = threading.Lock()
LATEST: dict = {"status": "empty"}

# --- 固定返答データ（hidden_files/monitor/fixed_replies.py から生成。手で直さない）---
FIXED = [
  {
    "id": "育休",
    "title": "育休",
    "exact": [
      "育休",
      "育児休業",
      "育児休暇",
      "育休について教えて",
      "育児休業について",
      "育休について"
    ],
    "contains": [
      "育児休業",
      "育児休暇",
      "育休"
    ],
    "reply": "育児休業は取得できますよ。申請は、原則として休業開始予定日の1か月前までに行ってください。（第21条 育児休業）"
  },
  {
    "id": "給料日",
    "title": "給料日",
    "exact": [
      "給料日",
      "給料日はいつ",
      "給与の支払日",
      "給与の支払日はいつ",
      "給与はいつ",
      "いつ振り込まれるの",
      "お給料はいつ振り込まれるの"
    ],
    "contains": [
      "給料日",
      "給与の支払日",
      "いつ振り込まれる",
      "いつ支給される"
    ],
    "reply": "給与は毎月末日締め、翌月25日に支給します。支給日が金融機関の休業日にあたる場合は、原則としてその前営業日に支給します。（第30条 給与の支払日）"
  },
  {
    "id": "お給料",
    "title": "お給料（構成＋支払日）",
    "exact": [
      "お給料",
      "お給料について",
      "お給料についておしえて"
    ],
    "contains": [],
    "reply": "お給料は、基本給と各種手当などで構成されています。具体的な金額や支給条件は、雇用契約と給与規程によります。お支払いは毎月末日締めで、翌月25日に支給されます。（第29条 給与、第30条 給与の支払日）"
  },
  {
    "id": "採用",
    "title": "採用",
    "exact": [
      "採用について",
      "採用"
    ],
    "contains": [
      "採用について"
    ],
    "reply": "採用は、選考・面接・資格・健康確認を経て決定されます。医療資格職の方は、資格証明書の提出が必要です。（第4条 採用）"
  },
  {
    "id": "提出書類",
    "title": "提出書類",
    "exact": [
      "提出書類",
      "提出書類について"
    ],
    "contains": [
      "提出書類"
    ],
    "reply": "提出書類は、履歴書や資格証明書の写し、住民票記載事項証明書、給与振込口座届、扶養控除等申告書などです。詳しい条件は、雇用形態や雇用契約によって異なる場合があります。（第5条 提出書類）"
  },
  {
    "id": "試用期間",
    "title": "試用期間",
    "exact": [
      "試用期間",
      "試用期間は",
      "試用期間について"
    ],
    "contains": [
      "試用期間"
    ],
    "reply": "試用期間は、採用日から3か月です。業務遂行能力、勤務態度、健康状態などを総合的に判断し、必要に応じて延長する場合があります。（第6条 試用期間）"
  },
  {
    "id": "賞与",
    "title": "賞与",
    "exact": [
      "賞与",
      "ボーナス",
      "賞与について",
      "賞与はある"
    ],
    "contains": [
      "賞与",
      "ボーナス"
    ],
    "reply": "賞与は、経営状況や職員の勤務成績などを考慮して、支給することがあります。支給時期や支給額は別途定めます。（第34条 賞与）"
  },
  {
    "id": "有給休暇",
    "title": "有給休暇",
    "exact": [
      "有給",
      "有給休暇",
      "年次有給休暇",
      "有給について",
      "有給休暇について",
      "有給は何日"
    ],
    "contains": [
      "有給休暇",
      "年次有給休暇",
      "有給"
    ],
    "reply": "年次有給休暇は、採用日から6か月間継続勤務し、全労働日の8割以上出勤した職員に10日付与します。取得するときは、原則として取得希望日の3日前までに所属長へ申請してください。（第16条 年次有給休暇）"
  },
  {
    "id": "結婚休暇",
    "title": "結婚休暇",
    "exact": [
      "結婚休暇",
      "結婚休暇について",
      "結婚休暇は何日"
    ],
    "contains": [
      "結婚休暇"
    ],
    "reply": "結婚休暇は5日です。（第19条 特別休暇）"
  },
  {
    "id": "勤務時間",
    "title": "勤務時間",
    "exact": [
      "勤務時間",
      "勤務時間について",
      "日勤",
      "夜勤",
      "日勤の時間",
      "夜勤の時間"
    ],
    "contains": [
      "勤務時間"
    ],
    "reply": "所定労働時間は、原則として1日8時間、週40時間です。日勤は8:30〜17:30、夜勤は16:30〜翌9:00です。（第8条 所定労働時間、第9条 勤務時間の例）"
  },
  {
    "id": "シフト",
    "title": "シフト",
    "exact": [
      "シフト",
      "勤務表",
      "シフトについて",
      "勤務表はいつ",
      "シフトはいつ"
    ],
    "contains": [
      "シフト",
      "勤務表"
    ],
    "reply": "翌月の勤務表は、原則として前月25日までに職員へ通知します。勤務希望の提出期限は、原則として前月10日までです。（第12条 シフト勤務）"
  },
  {
    "id": "通勤手当",
    "title": "通勤手当",
    "exact": [
      "通勤手当",
      "通勤手当について"
    ],
    "contains": [
      "通勤手当"
    ],
    "reply": "通勤手当は、公共交通機関や自家用車で通勤される方に、法人の基準に基づいて支給されます。詳しい金額や条件は、法人の基準によります。（第31条 通勤手当）"
  },
  {
    "id": "遅刻・早退",
    "title": "遅刻・早退",
    "exact": [
      "遅刻",
      "早退",
      "遅刻について",
      "早退について",
      "早退の扱いは"
    ],
    "contains": [
      "遅刻",
      "早退"
    ],
    "reply": "遅刻や早退をするときは、原則として事前に所属長へ申し出てください。やむを得ない場合は、できるだけ速やかに連絡してください。（第25条 遅刻および早退）"
  },
  {
    "id": "欠勤",
    "title": "欠勤",
    "exact": [
      "欠勤",
      "欠勤について",
      "欠勤の連絡"
    ],
    "contains": [
      "欠勤"
    ],
    "reply": "やむを得ない理由で欠勤する場合は、始業時刻までに所属長へ連絡してください。連続3日以上の欠勤は診断書等の提出を求めることがあるよ、と規則には書かれています。ご注意ください。（第26条 欠勤）"
  },
  {
    "id": "勤務中の外出",
    "title": "勤務中の外出",
    "exact": [
      "勤務中の外出",
      "勤務中の外出について"
    ],
    "contains": [
      "勤務中の外出"
    ],
    "reply": "勤務時間中の外出は、事前に所属長の許可が必要です。（第28条 勤務中の外出）"
  },
  {
    "id": "健康診断",
    "title": "健康診断",
    "exact": [
      "健康診断",
      "健康診断について"
    ],
    "contains": [
      "健康診断"
    ],
    "reply": "法人が実施する定期健康診断を受診してください。必要があれば再検査や医師の指導を受けることになります。（第46条 健康診断）"
  },
  {
    "id": "患者への対応",
    "title": "患者への対応",
    "exact": [
      "患者への対応",
      "患者への対応について"
    ],
    "contains": [
      "患者への対応"
    ],
    "reply": "患者の人格・権利を尊重し、差別的な言動や不適切な対応をしないことです。相談や苦情は、必要に応じて所属長へ報告します。（第37条 患者への対応）"
  },
  {
    "id": "ハラスメント",
    "title": "ハラスメント",
    "exact": [
      "ハラスメント",
      "ハラスメントについて",
      "パワハラ",
      "セクハラ",
      "相談窓口",
      "ハラスメント相談"
    ],
    "contains": [
      "ハラスメント",
      "パワハラ",
      "セクハラ"
    ],
    "reply": "職場でのパワーハラスメントやセクシュアルハラスメントなどは禁止です。相談は、人事総務部または法人が指定する相談窓口で受け付けています。相談したことを理由に不利益な取扱いをすることは禁止されています。（第44条 ハラスメントの禁止、第45条 ハラスメント相談窓口）"
  },
  {
    "id": "休職",
    "title": "休職",
    "exact": [
      "休職",
      "休職について",
      "休職の期間"
    ],
    "contains": [
      "休職"
    ],
    "reply": "休職は、業務外のけがや病気で一定期間働けないときや、心身の不調で通常の業務が難しいときなどに、法人が命じることがあります。休職の期間は、勤続年数や理由を考えて個別に決められます。（第50条 休職、第51条 休職期間）"
  },
  {
    "id": "定年",
    "title": "定年",
    "exact": [
      "定年",
      "定年は",
      "定年について",
      "定年は何歳"
    ],
    "contains": [
      "定年"
    ],
    "reply": "定年は満65歳です。定年に達した日の属する月の末日をもって退職となります。ご本人が希望し、法人が認めた場合は、法令に基づき継続雇用を行うこともあります。（第54条 定年）"
  },
  {
    "id": "退職",
    "title": "退職",
    "exact": [
      "退職",
      "退職について",
      "退職したい",
      "退職届",
      "退職届はいつまで"
    ],
    "contains": [
      "退職"
    ],
    "reply": "自己都合で退職する場合は、原則として退職希望日の30日前までに退職届を提出してください。（第53条 自己都合退職）"
  },
  {
    "id": "SNS",
    "title": "SNS",
    "exact": [
      "SNS",
      "SNSについて",
      "SNSの利用",
      "SNSは使える"
    ],
    "contains": [
      "SNS"
    ],
    "reply": "SNSやインターネット上に、患者、職員、病院内部の情報を投稿してはいけません。匿名での投稿でも、個人や施設が特定される情報の公開は禁止です。（第40条 SNSの利用）"
  },
  {
    "id": "福利厚生",
    "title": "福利厚生",
    "exact": [
      "福利厚生",
      "福利厚生について"
    ],
    "contains": [
      "福利厚生"
    ],
    "reply": "職員食堂、定期健康診断、予防接種の補助、制服の貸与、研修・資格取得支援、慶弔見舞金制度などを設けることがあります。条件や支給額は別途定めます。（第59条 福利厚生）"
  }
]
GREETINGS = [
  {
    "id": "あいさつ・こんにちは",
    "exact": [
      "こんにちは",
      "こんにちわ"
    ],
    "reply": "こんにちは。ご質問があればお気軽にどうぞ。育休や給料日、有給など、就業規則についてお答えします。"
  },
  {
    "id": "あいさつ・こんばんは",
    "exact": [
      "こんばんは",
      "こんばんわ"
    ],
    "reply": "こんばんは。ご質問があればお気軽にどうぞ。育休や給料日、有給など、就業規則についてお答えします。"
  },
  {
    "id": "あいさつ・おはよう",
    "exact": [
      "おはよう",
      "おはようございます"
    ],
    "reply": "おはようございます。ご質問があればお気軽にどうぞ。育休や給料日、有給など、就業規則についてお答えします。"
  },
  {
    "id": "あいさつ・ありがとう",
    "exact": [
      "ありがとう",
      "ありがとうございます",
      "ありがとうございました"
    ],
    "reply": "どういたしまして。ほかにご質問があれば、いつでもどうぞ。"
  }
]
SUMMARIES = {
  "1": {
    "title": "目的",
    "summary": "労働条件・服務規律・職場秩序を定めるための規則。"
  },
  "2": {
    "title": "適用範囲",
    "summary": "医師・看護師・介護職員・薬剤師・検査技師・事務職員など、法人が雇用する職員に適用。パート・契約職員は雇用契約による。"
  },
  "3": {
    "title": "規則の遵守",
    "summary": "規則と諸規程を守り、誠実に職務を行う。"
  },
  "4": {
    "title": "採用",
    "summary": "選考・面接・資格・健康確認を経て決定。医療資格職は資格証明書を提出。"
  },
  "5": {
    "title": "提出書類",
    "summary": "履歴書、資格証明書の写し、住民票記載事項証明書、給与振込口座届、扶養控除等申告書など。"
  },
  "6": {
    "title": "試用期間",
    "summary": "採用日から3か月。能力・態度・健康で判断し、延長あり。試用期間中の条件は雇用契約による。"
  },
  "7": {
    "title": "人事異動",
    "summary": "業務上必要があれば配置転換・部署異動・職種変更・勤務場所変更を命じることがある。正当な理由なく拒否できない。"
  },
  "8": {
    "title": "所定労働時間",
    "summary": "原則1日8時間、週40時間。職種・勤務形態により異なる場合あり。"
  },
  "9": {
    "title": "勤務時間の例",
    "summary": "日勤8:30〜17:30、早出7:00〜16:00、遅出12:00〜21:00、夜勤16:30〜翌9:00。休憩は日勤・早出・遅出60分、夜勤120分。"
  },
  "10": {
    "title": "休憩時間",
    "summary": "労働時間に応じて法令に基づく休憩を与える。原則自由に利用できる。"
  },
  "11": {
    "title": "休日",
    "summary": "週休2日が基本。祝日はシフトで調整、年末年始は勤務体制で設定。24時間体制のため休日の曜日は人により異なる。"
  },
  "12": {
    "title": "シフト勤務",
    "summary": "翌月の勤務表は原則前月25日までに通知。勤務希望の提出期限は原則前月10日まで。"
  },
  "13": {
    "title": "時間外勤務",
    "summary": "業務上必要があれば法令・労使協定の範囲内で命じることがある。実績を申告する。"
  },
  "14": {
    "title": "休日勤務",
    "summary": "業務上必要があれば法令・労使協定に基づき命じることがある。割増賃金を支給。"
  },
  "15": {
    "title": "深夜勤務",
    "summary": "22時〜翌5時は深夜割増賃金を支給。"
  },
  "16": {
    "title": "年次有給休暇",
    "summary": "採用から6か月継続勤務し全労働日の8割以上出勤で10日付与。その後は勤続年数に応じて法令に基づき付与。取得希望日の3日前までに所属長へ申請。"
  },
  "17": {
    "title": "有給休暇の取得単位",
    "summary": "原則1日単位。法人が認めれば半日・時間単位も可。"
  },
  "18": {
    "title": "有給休暇の計画的取得",
    "summary": "年10日以上付与される職員は年5日以上の取得を確保。"
  },
  "19": {
    "title": "特別休暇",
    "summary": "結婚休暇5日、配偶者の出産休暇2日、忌引（配偶者・父母・子）5日、忌引（祖父母・兄弟姉妹）2日、産前産後休業は法令による。"
  },
  "20": {
    "title": "産前産後休業",
    "summary": "出産予定日の6週間前（多胎は14週間前）から本人が請求すれば休業可。産後8週間は原則就業させない。"
  },
  "21": {
    "title": "育児休業",
    "summary": "育児・介護休業法と法人規程に基づき取得できる。申請は原則休業開始予定日の1か月前まで。"
  },
  "22": {
    "title": "介護休業",
    "summary": "要介護状態の家族を介護するため法令に基づき取得できる。日数・申請方法は育児・介護休業法と法人規程による。"
  },
  "23": {
    "title": "子の看護等休暇",
    "summary": "小学校3年生修了までの子を養育する職員は法令に基づき取得できる。"
  },
  "24": {
    "title": "病気休暇",
    "summary": "病気・負傷で勤務できない場合は速やかに所属長へ連絡。有給・無給は給与規程による。"
  },
  "25": {
    "title": "遅刻および早退",
    "summary": "原則事前に所属長へ申し出る。やむを得ない場合は速やかに連絡。"
  },
  "26": {
    "title": "欠勤",
    "summary": "やむを得ない理由の欠勤は始業時刻までに所属長へ連絡。連続3日以上の欠勤は診断書等の提出を求めることがある。"
  },
  "27": {
    "title": "無断欠勤",
    "summary": "正当な理由なく連絡せず欠勤すると無断欠勤。継続すれば懲戒処分の対象になることがある。"
  },
  "28": {
    "title": "勤務中の外出",
    "summary": "勤務時間中の外出は事前に所属長の許可が必要。"
  },
  "29": {
    "title": "給与",
    "summary": "基本給・各種手当・その他支給項目で構成。金額・支給条件は雇用契約と給与規程による。"
  },
  "30": {
    "title": "給与の支払日",
    "summary": "毎月末日締め、翌月25日に支給。支給日が金融機関の休業日なら原則前営業日に支給。"
  },
  "31": {
    "title": "通勤手当",
    "summary": "公共交通機関・自家用車での通勤者に、法人の基準に基づき支給。"
  },
  "32": {
    "title": "夜勤手当",
    "summary": "夜勤勤務者に法人の基準に従い支給。"
  },
  "33": {
    "title": "資格手当",
    "summary": "法人が指定する医療資格・専門資格を持ち業務に活用する職員に支給する場合がある。"
  },
  "34": {
    "title": "賞与",
    "summary": "経営状況・勤務成績等を考慮し支給することがある。時期・額は別途定める。"
  },
  "35": {
    "title": "昇給",
    "summary": "勤務成績・能力・勤続年数・経営状況等を考慮して決定。"
  },
  "36": {
    "title": "基本的な服務",
    "summary": "医療従事者としての自覚を持ち、患者・家族に誠実かつ丁寧に対応する。"
  },
  "37": {
    "title": "患者への対応",
    "summary": "患者の人格・権利を尊重し、差別的な言動や不適切な対応をしない。相談・苦情は必要に応じて所属長へ報告。"
  },
  "38": {
    "title": "守秘義務",
    "summary": "業務上知り得た患者・職員の個人情報、診療情報、法人の機密情報を正当な理由なく第三者に漏らさない。退職後も継続。"
  },
  "39": {
    "title": "個人情報の取扱い",
    "summary": "患者情報は業務上必要な範囲でのみ取り扱う。持ち出し、私物スマホへの保存、SNSへの投稿、不要な閲覧、第三者への提供はしない。"
  },
  "40": {
    "title": "SNSの利用",
    "summary": "SNS・インターネット上に患者・職員・病院内部の情報を投稿しない。匿名でも、個人や施設が特定される情報の公開は禁止。"
  },
  "41": {
    "title": "身だしなみ",
    "summary": "清潔感のある服装、職種に応じた制服・身だしなみ基準を守る。アクセサリー・爪の長さ等に制限がある場合がある。"
  },
  "42": {
    "title": "感染対策",
    "summary": "感染対策マニュアル・衛生管理規程を守る。感染症の疑いや発熱があれば速やかに所属長へ報告。"
  },
  "43": {
    "title": "飲酒および喫煙",
    "summary": "勤務中の飲酒は禁止。喫煙は指定場所でのみ。飲酒の影響が残る状態での勤務は禁止。"
  },
  "44": {
    "title": "ハラスメントの禁止",
    "summary": "パワハラ、セクハラ、妊娠・出産・育児休業等に関するハラスメント、その他就業環境を害する言動は禁止。"
  },
  "45": {
    "title": "ハラスメント相談窓口",
    "summary": "人事総務部または法人が指定する相談窓口で受け付ける。相談者のプライバシーを保護し、相談を理由とする不利益な取扱いを禁止。"
  },
  "46": {
    "title": "健康診断",
    "summary": "法人が実施する定期健康診断を受診する。必要があれば再検査や医師の指導を受ける。"
  },
  "47": {
    "title": "安全衛生",
    "summary": "労働災害・医療事故の防止に努める。事故や危険を発見したら速やかに所属長へ報告。"
  },
  "48": {
    "title": "医療事故およびインシデント報告",
    "summary": "医療事故、誤薬、転倒、針刺し事故などのインシデントは、定める報告手順に従い速やかに報告。隠蔽してはならない。"
  },
  "49": {
    "title": "感染症への対応",
    "summary": "感染症に罹患した場合や濃厚接触の可能性がある場合は、所属長または感染管理担当部署へ報告。出勤の可否は医師の判断・法令・感染対策基準による。"
  },
  "50": {
    "title": "休職",
    "summary": "業務外の傷病で一定期間勤務できない場合、心身の不調で通常の業務遂行が困難な場合、その他法人が必要と認めた場合、法人は休職を命じることがある。"
  },
  "51": {
    "title": "休職期間",
    "summary": "勤続年数・休職理由等を考慮し、個別に決定する。"
  },
  "52": {
    "title": "復職",
    "summary": "復職希望者は医師の診断書等を提出。法人は健康状態・業務遂行能力を確認して可否を決定。必要に応じて短時間勤務や業務内容の調整を行う場合がある。"
  },
  "53": {
    "title": "自己都合退職",
    "summary": "自己都合退職は原則退職希望日の30日前までに退職届を提出。"
  },
  "54": {
    "title": "定年",
    "summary": "満65歳。定年に達した日の属する月の末日をもって退職。希望し法人が認めれば法令に基づき継続雇用を行うことがある。"
  },
  "55": {
    "title": "解雇",
    "summary": "勤務成績が著しく不良で改善の見込みがない場合、心身の状態で業務継続が困難な場合、重大な服務規律違反があった場合、事業運営上やむを得ない事情がある場合、法令に従い解雇を検討することがある。"
  },
  "56": {
    "title": "退職時の手続き",
    "summary": "制服・名札・職員証等の返却、業務資料・電子データの返却または削除、担当業務の引き継ぎなどを行う。"
  },
  "57": {
    "title": "懲戒の種類",
    "summary": "戒告、譴責、減給、出勤停止、懲戒解雇。違反の内容・程度に応じて決める。"
  },
  "58": {
    "title": "懲戒事由",
    "summary": "無断欠勤、患者情報の不正な持ち出し・漏洩、医療記録の改ざん、暴力・暴言・ハラスメント、重大な指示違反、法人の信用を著しく損なう行為など。"
  },
  "59": {
    "title": "福利厚生",
    "summary": "職員食堂、職員健康診断、予防接種の補助、制服の貸与、研修・資格取得支援、慶弔見舞金制度を設けることがある。条件・支給額は別途定める。"
  },
  "60": {
    "title": "研修",
    "summary": "業務上必要な研修・法人が指定する教育訓練に参加する。医療安全、感染対策、個人情報保護、接遇の研修は定期的に実施。"
  },
  "61": {
    "title": "規則の改定",
    "summary": "法令の改正、社会情勢、法人の運営状況等を踏まえ、必要に応じて改定する。附則：2026年4月1日から施行。定めのない事項は労働基準法その他関係法令と法人の諸規程による。"
  }
}
COMPLAINT_GUARD = ["が低い", "が少ない", "不満", "おかしい", "間違", "なぜ", "どうして"]
GREETING_PREFIXES = ["こんにちは", "こんばんは", "おはようございます", "おはよう", "こんにちわ", "こんばんわ"]


def _norm(s: str) -> str:
    import re as _re
    return _re.sub(r"[\s？?！!。．、，,・~〜]+", "", s)


def match_fixed(question: str):
    """固定返答に当てはまれば (reply_dict, reason)、当てはまらなければ (None, reason)。"""
    import re as _re
    q_raw = question.strip()
    q = _norm(q_raw)
    if not q:
        return None, "空のメッセージ"
    for g in GREETINGS:
        if q in [_norm(e) for e in g["exact"]]:
            return {"id": g["id"], "title": "あいさつ", "reply": g["reply"]}, "あいさつ・お礼のみ"
    for prefix in GREETING_PREFIXES:
        if q.startswith(_norm(prefix)) and len(q) > len(_norm(prefix)):
            rest = q_raw[len(prefix):].lstrip("、，, ")
            if rest:
                return match_fixed(rest)
    if any(g in q_raw for g in COMPLAINT_GUARD):
        return None, "不満・疑問の声の可能性があるため親指へ"
    m = _re.search(r"(?:第)?(\d+)条", q_raw)
    if m:
        num = int(m.group(1))
        a = SUMMARIES.get(str(num))
        if a:
            return {"id": f"第{num}条", "title": a["title"],
                    "reply": f"{a['summary']}（第{num}条 {a['title']}）"}, "条番号で直接指定"
    hits = []
    for entry in FIXED:
        if q in [_norm(e) for e in entry["exact"]]:
            hits.append((entry, "完全一致"))
            continue
        for kw in entry["contains"]:
            if kw and kw in q_raw:
                hits.append((entry, "キーワードを含む"))
                break
    ids = {h[0]["id"] for h in hits}
    if len(ids) == 1:
        entry, why = hits[0]
        return {"id": entry["id"], "title": entry["title"], "reply": entry["reply"]}, why
    if len(ids) > 1:
        return None, f"複数の固定返答に当てはまるため親指へ（{', '.join(sorted(ids))}）"
    return None, "固定返答に当てはまらず親指へ"


def send_reply(reply_token: str, text: str) -> bool:
    """Reply API で1通返す。枠は使わない。成功したら True。"""
    if not CHANNEL_ACCESS_TOKEN:
        log.error("LINE_CHANNEL_ACCESS_TOKEN 未設定のため Reply できない")
        return False
    payload = json.dumps(
        {"replyToken": reply_token, "messages": [{"type": "text", "text": text}]},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        REPLY_URL, data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as exc:
        log.error("Reply API HTTP %s %s", exc.code, exc.read()[:200])
        return False
    except Exception as exc:
        log.error("Reply API 呼び出しに失敗しました: %s", exc)
        return False


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
            "mode": "hybrid-fixed",
            "articles": len(RULEBOOK.articles) if RULEBOOK else 0,
            "fixed_replies": len(FIXED) + len(GREETINGS),
            "channel_secret_configured": bool(CHANNEL_SECRET),
            "channel_access_token_configured": bool(CHANNEL_ACCESS_TOKEN),
            "github_token_configured": bool(GITHUB_TOKEN),
            "github_repo": GITHUB_REPO,
        }
    )


@app.get("/")
def index():
    return jsonify({"service": "line-qa-bot hybrid-fixed (test)", "health": "/health", "latest": "/latest-question", "webhook": "/webhook"})


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
        reply_token = event.get("replyToken", "")
        if not question or not user_id:
            continue
        now = datetime.now(timezone(timedelta(hours=9)))
        received_at = now.strftime("%Y-%m-%dT%H:%M")

        # 1) 固定返答に当てはまれば Reply API ですぐ返す（枠は使わない）。
        fixed, why = match_fixed(question)
        if fixed and reply_token:
            if send_reply(reply_token, fixed["reply"]):
                log.info("FIXED %s (%s): %r", fixed["id"], why, question[:60])
                continue
            log.warning("Reply に失敗したため親指へ回す: %r", question[:60])

        # 2) 当てはまらなければ親指へ回す（最新1件をメモリに保存）。
        record = {
            "status": "ok",
            "question": question,
            "userId": user_id,
            "received_at": received_at,
            "replyToken": reply_token,
            "detected_at": now.isoformat(timespec="seconds"),
        }
        with _LATEST_LOCK:
            LATEST.clear()
            LATEST.update(record)
        log.info("MUSE %s: %r (user=%s)", why, question[:60], user_id[:8])
        # 予備のキュー書き込みだけ別スレッドで行う。
        threading.Thread(target=create_question_file, args=(user_id, question, now), daemon=True).start()

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
