"""就業規則 docx の読み込みと、キーワードによる簡易 Q&A.

方針（テスト用）:
- 回答の根拠はこの就業規則だけ。条文をそのまま抜き出して返す。
- 質問に合うトピックが見つからない、または金額・時期など規則に
  書かれていないことを聞かれた場合は、推測せず「書かれていません」と返す。
- docx の解析は標準ライブラリだけ（zipfile + ElementTree）で行う。
  python-docx は不要。表のセルも文書順の段落として拾われる。
"""

from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

ARTICLE_RE = re.compile(r"^第(\d+)条[（(]([^）)]+)[）)]")
NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

# 質問キーワード -> 参照する条番号（優先順）。最初にマッチしたものを使う。
# 並び順は「より具体的なトピックが先」になるよう調整してある。
TOPICS: list[dict] = [
    {"keywords": ["試用期間", "試用"], "articles": [6]},
    {"keywords": ["夜勤手当"], "articles": [32]},
    {"keywords": ["深夜", "深夜勤務", "深夜割増", "22時"], "articles": [15]},
    {"keywords": ["日勤", "早出", "遅出", "夜勤", "勤務時間", "所定労働時間", "何時から", "何時まで"],
     "articles": [9, 8]},
    {"keywords": ["シフト", "勤務表", "勤務希望"], "articles": [12]},
    {"keywords": ["休憩"], "articles": [10]},
    {"keywords": ["週休", "休日"], "articles": [11]},
    {"keywords": ["時間外", "残業"], "articles": [13]},
    {"keywords": ["休日勤務", "休日出勤"], "articles": [14]},
    {"keywords": ["有給", "有休", "年次有給", "年休"], "articles": [16, 17]},
    {"keywords": ["結婚休暇", "結婚"], "articles": [19]},
    {"keywords": ["配偶者の出産", "出産休暇"], "articles": [19]},
    {"keywords": ["忌引", "忌引き"], "articles": [19]},
    {"keywords": ["特別休暇"], "articles": [19]},
    {"keywords": ["産前", "産後", "産休"], "articles": [20]},
    {"keywords": ["育児休業", "育休"], "articles": [21]},
    {"keywords": ["介護休業", "介護"], "articles": [22]},
    {"keywords": ["子の看護", "看護休暇", "子が病気"], "articles": [23]},
    {"keywords": ["病気休暇"], "articles": [24]},
    {"keywords": ["遅刻", "早退"], "articles": [25]},
    {"keywords": ["欠勤", "体調不良", "休む", "休みたい"], "articles": [26]},
    {"keywords": ["無断欠勤"], "articles": [27]},
    {"keywords": ["給料日", "給与の支払日", "支給日", "給料", "給与"], "articles": [30, 29]},
    {"keywords": ["通勤手当"], "articles": [31]},
    {"keywords": ["資格手当"], "articles": [33]},
    {"keywords": ["賞与", "ボーナス"], "articles": [34]},
    {"keywords": ["昇給"], "articles": [35]},
    {"keywords": ["守秘義務", "守秘"], "articles": [38]},
    {"keywords": ["個人情報", "スマホに保存", "持ち出し"], "articles": [39]},
    {"keywords": ["SNS"], "articles": [40]},
    {"keywords": ["ハラスメント", "相談窓口"], "articles": [45, 44]},
    {"keywords": ["健康診断"], "articles": [46]},
    {"keywords": ["休職"], "articles": [50, 51]},
    {"keywords": ["復職"], "articles": [52]},
    {"keywords": ["退職", "辞め", "辞職"], "articles": [53]},
    {"keywords": ["定年"], "articles": [54]},
    {"keywords": ["解雇"], "articles": [55]},
    {"keywords": ["懲戒"], "articles": [57]},
    {"keywords": ["福利厚生"], "articles": [59]},
    {"keywords": ["研修"], "articles": [60]},
]

HR_CONTACT = "人事総務部（TEL：xxx-xxxx-xxxx）"
NOT_FOUND_REPLY = (
    "ご質問に関する情報は、就業規則に見当たりませんでした。"
    f"お手数ですが、{HR_CONTACT}までご連絡ください。"
)
AMOUNT_WORDS = ("いくら", "金額", "何円", "いつもらえる", "いつ支給")


@dataclass
class Article:
    number: int
    title: str
    text: str = ""  # 条文本文（見出し行を除く）

    def excerpt(self, limit: int = 500) -> str:
        body = re.sub(r"\s+", " ", self.text).strip()
        if len(body) > limit:
            body = body[:limit] + "…"
        return body


@dataclass
class RuleBook:
    path: Path
    articles: dict[int, Article] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "RuleBook":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"就業規則ファイルが見つかりません: {path}")
        paragraphs = _docx_paragraphs(path)
        book = cls(path=path)
        current: Article | None = None
        for para in paragraphs:
            if re.match(r"^第\d+章", para):
                continue  # 章見出しは条文本文に含めない
            m = ARTICLE_RE.match(para)
            if m:
                current = Article(number=int(m.group(1)), title=m.group(2))
                book.articles[current.number] = current
                continue
            if current is not None and para:
                current.text += para + " "
        return book

    def answer(self, question: str) -> str:
        """質問への回答文を返す。根拠条文つき。見つからなければ NOT_FOUND_REPLY。"""
        q = question.strip()
        best: dict | None = None
        best_score = 0
        for topic in TOPICS:
            score = sum(q.count(kw) for kw in topic["keywords"])
            if score > best_score:
                best, best_score = topic, score
        if best is None:
            return NOT_FOUND_REPLY

        parts: list[str] = []
        for num in best["articles"]:
            art = self.articles.get(num)
            if art is None:
                continue
            parts.append(f"（第{art.number}条 {art.title}）\n{art.excerpt()}")
        if not parts:
            return NOT_FOUND_REPLY
        reply = "\n".join(parts)
        # 金額・時期を聞かれており、条文が具体額を定めていない場合
        if any(w in q for w in AMOUNT_WORDS) and (
            "別途定める" in reply
            or "法人の定める基準" in reply
            or "給与規程による" in reply
        ):
            reply += f"\nなお、金額・時期はこの規則には書かれていません。お手数ですが、{HR_CONTACT}までご連絡ください。"
        return reply


def _docx_paragraphs(path: Path) -> list[str]:
    """docx（zip 内の word/document.xml）から段落テキストを文書順に抜く。"""
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    out: list[str] = []
    for para in root.findall(".//w:p", NS):
        text = "".join(n.text or "" for n in para.findall(".//w:t", NS)).strip()
        if text:
            out.append(text)
    return out
