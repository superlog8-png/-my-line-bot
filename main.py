import html
import os
import sys
import textwrap
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

import requests
import uvicorn
from fastapi import FastAPI, Request


app = FastAPI()

LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
REQUEST_TIMEOUT = 12
MAX_ITEMS = 6


CATEGORY_ALIASES = {
    "財金重點": {"財金", "財金重點", "財經", "財經重點", "請給我財金重點", "請給我財經"},
    "科技趨勢": {"科技", "科技趨勢", "請給我科技趨勢", "請給我科技"},
    "虛擬貨幣": {"虛擬貨幣", "加密貨幣", "幣圈", "crypto", "請給我虛擬貨幣"},
    "台灣新聞": {"台灣", "台灣新聞", "請給我台灣新聞"},
    "國際新聞": {"國際", "國際新聞", "請給我國際新聞"},
    "星座運勢": {"星座", "星座運勢", "運勢", "請給我星座運勢"},
}

CATEGORY_QUERIES = {
    "財金重點": "site:ctee.com.tw 工商時報 財經 OR 股市 OR 投資 OR 產業 OR 國際財經",
    "科技趨勢": "AI 半導體 科技趨勢 台積電 NVIDIA 雲端 資安 最新",
    "虛擬貨幣": "Bitcoin Ethereum ETF 虛擬貨幣 加密貨幣 監管 最新",
    "台灣新聞": "台灣 最新 新聞 政治 經濟 社會 產業",
    "國際新聞": "國際 最新 新聞 G7 地緣政治 能源 烏克蘭 中東",
}


def normalize_text(text: str) -> str:
    return " ".join((text or "").strip().split()).lower()


def resolve_category(user_text: str) -> str | None:
    normalized = normalize_text(user_text)
    for category, aliases in CATEGORY_ALIASES.items():
        if normalized in {normalize_text(alias) for alias in aliases}:
            return category
    for category, aliases in CATEGORY_ALIASES.items():
        if any(normalize_text(alias) in normalized for alias in aliases):
            return category
    return None


def fetch_google_news(query: str, max_items: int = MAX_ITEMS) -> list[dict]:
    params = {
        "q": query,
        "hl": "zh-TW",
        "gl": "TW",
        "ceid": "TW:zh-Hant",
    }
    response = requests.get(GOOGLE_NEWS_RSS, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    root = ET.fromstring(response.content)
    items = []
    for item in root.findall(".//item")[:max_items]:
        title = clean_title(item.findtext("title", default=""))
        link = item.findtext("link", default="")
        source = item.findtext("source", default="")
        published_at = parse_pub_date(item.findtext("pubDate", default=""))
        items.append(
            {
                "title": title,
                "link": link,
                "source": source or "Google News",
                "published_at": published_at,
            }
        )
    return items


def clean_title(title: str) -> str:
    title = html.unescape(title or "").strip()
    if " - " in title:
        title = title.rsplit(" - ", 1)[0].strip()
    return title


def parse_pub_date(value: str) -> str:
    try:
        return parsedate_to_datetime(value).strftime("%Y/%m/%d")
    except (TypeError, ValueError, IndexError):
        return datetime.now().strftime("%Y/%m/%d")


def build_news_summary(category: str) -> str:
    query = CATEGORY_QUERIES[category]
    try:
        items = fetch_google_news(query)
    except requests.RequestException as exc:
        if category == "財金重點":
            return (
                "財金重點｜工商時報統整\n"
                "工商時報來源目前不可取用，且替代新聞查詢失敗。\n"
                f"原因：{exc}\n"
                "請稍後再試。"
            )
        return f"{category}\n目前新聞來源暫時無法取用，請稍後再試。\n原因：{exc}"

    if not items:
        unavailable = "工商時報來源目前不可取用。" if category == "財金重點" else ""
        return f"{category}\n{unavailable}目前查無可整理的即時新聞。"

    if category == "財金重點":
        title = "財金重點｜工商時報統整"
        lead = "指定優先參考工商時報 ctee.com.tw，以下為多則財經、股市、產業與國際財經新聞的統整。"
        impact = infer_finance_impact(items)
    else:
        title = category
        lead = "以下整理自最新公開新聞結果。"
        impact = infer_general_impact(category, items)

    bullets = "\n".join(f"- {item['title']}" for item in items[:5])
    sources = "\n".join(
        f"{index}. {item['source']}｜{item['published_at']}\n{item['link']}"
        for index, item in enumerate(items[:3], start=1)
    )

    return trim_for_line(
        f"{title}\n"
        f"{lead}\n\n"
        f"重點：\n{bullets}\n\n"
        f"統整觀察：\n{impact}\n\n"
        f"來源：\n{sources}"
    )


def infer_finance_impact(items: list[dict]) -> str:
    text = " ".join(item["title"] for item in items)
    points = []
    if any(keyword in text for keyword in ["台積電", "半導體", "AI", "輝達", "NVIDIA"]):
        points.append("AI、半導體與台股電子供應鏈仍是資金關注主軸。")
    if any(keyword in text for keyword in ["匯率", "利率", "央行", "Fed", "美元"]):
        points.append("匯率與利率變化可能牽動外資流向與高估值資產。")
    if any(keyword in text for keyword in ["油", "能源", "中東", "航運"]):
        points.append("能源與地緣政治變化可能影響通膨、運輸與原物料成本。")
    if not points:
        points.append("短線可觀察資金是否集中在大型權值股，以及題材股是否有輪動。")
    return "\n".join(f"- {point}" for point in points)


def infer_general_impact(category: str, items: list[dict]) -> str:
    if category == "科技趨勢":
        return "- 觀察 AI、半導體與雲端投資是否延續，並留意估值與產能壓力。"
    if category == "虛擬貨幣":
        return "- 觀察 BTC、ETH 價格區間、ETF 資金流與監管消息對風險偏好的影響。"
    if category == "台灣新聞":
        return "- 觀察政策、產業與民生議題對台股、企業營運與日常生活的影響。"
    if category == "國際新聞":
        return "- 觀察地緣政治、能源與外交事件對全球市場和供應鏈的影響。"
    return "- 觀察後續發展與是否形成連續性議題。"


def build_horoscope() -> str:
    today = datetime.now().strftime("%Y/%m/%d")
    signs = [
        ("牡羊", "行動力提升，適合先處理最重要的任務。"),
        ("金牛", "財務與工作節奏宜保守，避免臨時衝動消費。"),
        ("雙子", "溝通運佳，適合協調合作或整理資訊。"),
        ("巨蟹", "情緒較敏銳，先穩住步調再做決定。"),
        ("獅子", "表現機會增加，但要留意承諾不要過滿。"),
        ("處女", "細節處理順手，適合完成待辦與修正錯誤。"),
        ("天秤", "人際互動活躍，適合談合作與交換想法。"),
        ("天蠍", "直覺強，適合檢查風險與調整策略。"),
        ("射手", "適合學習與規劃，但財務上先避免冒進。"),
        ("魔羯", "工作責任增加，穩定推進會比求快更好。"),
        ("水瓶", "新想法容易出現，適合測試小規模改變。"),
        ("雙魚", "創意與同理心提升，適合表達與關懷他人。"),
    ]
    lines = "\n".join(f"- {sign}：{message}" for sign, message in signs)
    return f"星座運勢｜{today}\n娛樂參考，請勿作為重大決策依據。\n\n{lines}"


def trim_for_line(text: str, limit: int = 4800) -> str:
    text = textwrap.dedent(text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n...內容已截短"


def get_news_by_category(user_text: str) -> str:
    category = resolve_category(user_text)
    if category == "星座運勢":
        return build_horoscope()
    if category in CATEGORY_QUERIES:
        return build_news_summary(category)

    options = "、".join(CATEGORY_ALIASES.keys())
    return f"請輸入想查看的圖文選單項目：{options}"


def build_daily_digest() -> list[str]:
    return [
        build_news_summary("財金重點"),
        build_news_summary("科技趨勢"),
        build_news_summary("虛擬貨幣"),
        build_news_summary("台灣新聞"),
        build_news_summary("國際新聞"),
        build_horoscope(),
    ]


def reply_to_line(reply_token: str, response_text: str) -> None:
    if not LINE_TOKEN:
        raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN is not set")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_TOKEN}",
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": response_text}],
    }
    response = requests.post(
        LINE_REPLY_URL,
        headers=headers,
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()


def broadcast_to_line(messages: list[str]) -> None:
    if not LINE_TOKEN:
        raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN is not set")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_TOKEN}",
    }

    for text in messages:
        payload = {"messages": [{"type": "text", "text": trim_for_line(text)}]}
        response = requests.post(
            LINE_BROADCAST_URL,
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()


@app.get("/")
async def root():
    return {"message": "LINE Bot Server is running!"}


@app.get("/preview/{category}")
async def preview(category: str):
    return {"category": category, "message": get_news_by_category(category)}


@app.post("/broadcast/daily")
async def broadcast_daily():
    messages = build_daily_digest()
    broadcast_to_line(messages)
    return {"status": "ok", "message_count": len(messages)}


@app.post("/webhook")
async def handle_webhook(request: Request):
    body = await request.json()
    events = body.get("events", [])

    for event in events:
        if event.get("type") != "message":
            continue
        message = event.get("message", {})
        if message.get("type") != "text":
            continue

        response_text = get_news_by_category(message.get("text", ""))
        reply_token = event.get("replyToken")
        if reply_token:
            reply_to_line(reply_token, response_text)

    return {"status": "ok"}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "broadcast":
        broadcast_to_line(build_daily_digest())
        print("Daily digest broadcast sent.")
        raise SystemExit(0)

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
