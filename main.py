import html
import os
import re
import sys
import textwrap
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from io import BytesIO
from urllib.parse import quote

import requests
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from PIL import Image, ImageDraw, ImageFont


app = FastAPI()

LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"
SERVICE_BASE_URL = os.getenv("SERVICE_BASE_URL", "https://my-line-bot-yuht.onrender.com").rstrip("/")
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
TWSE_STOCK_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://mis.twse.com.tw/stock/fibest.jsp?lang=zh_tw",
}
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

STOCK_PREFIXES = ("股票", "股價", "查股票", "查股價", "代碼")


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


def resolve_stock_symbols(user_text: str) -> list[str]:
    text = normalize_text(user_text).upper()
    for prefix in STOCK_PREFIXES:
        text = text.replace(prefix.upper(), " ")
    text = re.sub(r"[^A-Z0-9.^\s]", " ", text)
    tokens = [token.strip() for token in text.split() if token.strip()]

    symbols = []
    for token in tokens:
        if token in {"請給我", "幫我查"}:
            continue
        if re.fullmatch(r"\d{4,6}", token):
            symbols.append(f"{token}.TW")
        elif re.fullmatch(r"[A-Z]{1,5}(\.[A-Z]{1,3})?", token):
            symbols.append(token)
        elif re.fullmatch(r"\^[A-Z0-9]{1,8}", token):
            symbols.append(token)
    return symbols[:3]


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


def fetch_stock_quote(symbol: str) -> dict:
    if symbol.endswith(".TW"):
        return fetch_taiwan_stock_quote(symbol.removesuffix(".TW"))

    params = {
        "range": "1d",
        "interval": "1m",
        "includePrePost": "false",
    }
    response = requests.get(
        YAHOO_CHART_URL.format(symbol=symbol),
        params=params,
        headers=REQUEST_HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    result = data.get("chart", {}).get("result") or []
    if not result:
        raise ValueError(f"查無 {symbol} 報價")

    meta = result[0].get("meta", {})
    quote_data = (result[0].get("indicators", {}).get("quote") or [{}])[0]
    closes = [price for price in quote_data.get("close", []) if price is not None]
    current = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
    previous = meta.get("chartPreviousClose") or meta.get("previousClose")
    if current is None or previous is None:
        raise ValueError(f"{symbol} 報價資料不完整")

    change = current - previous
    percent = (change / previous) * 100 if previous else 0
    return {
        "symbol": meta.get("symbol", symbol),
        "name": meta.get("longName") or meta.get("shortName") or meta.get("symbol", symbol),
        "currency": meta.get("currency", ""),
        "price": current,
        "previous": previous,
        "change": change,
        "percent": percent,
        "exchange": meta.get("exchangeName") or meta.get("fullExchangeName", ""),
        "time": datetime.fromtimestamp(meta.get("regularMarketTime", datetime.now().timestamp())).strftime("%Y/%m/%d %H:%M"),
        "source": "Yahoo Finance",
    }


def fetch_taiwan_stock_quote(code: str) -> dict:
    errors = []
    for exchange, label in (("tse", "TWSE"), ("otc", "TPEx")):
        params = {
            "ex_ch": f"{exchange}_{code}.tw",
            "json": "1",
            "delay": "0",
        }
        try:
            response = requests.get(
                TWSE_STOCK_URL,
                params=params,
                headers=REQUEST_HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
            items = data.get("msgArray") or []
            if not items:
                continue

            item = items[0]
            current = parse_float(item.get("z")) or parse_float(item.get("pz"))
            previous = parse_float(item.get("y"))
            if current is None or previous is None:
                continue

            date_text = item.get("d") or datetime.now().strftime("%Y%m%d")
            time_text = item.get("t") or item.get("%") or ""
            change = current - previous
            return {
                "symbol": f"{code}.TW",
                "name": item.get("n") or code,
                "currency": "TWD",
                "price": current,
                "previous": previous,
                "change": change,
                "percent": (change / previous) * 100 if previous else 0,
                "exchange": label,
                "time": format_twse_time(date_text, time_text),
                "source": label,
            }
        except (requests.RequestException, ValueError) as exc:
            errors.append(str(exc))

    raise ValueError("; ".join(errors) or f"查無 {code} 台股報價")


def parse_float(value: str | None) -> float | None:
    try:
        if value in (None, "", "-", "--"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def format_twse_time(date_text: str, time_text: str) -> str:
    try:
        value = datetime.strptime(f"{date_text} {time_text}", "%Y%m%d %H:%M:%S")
        return value.strftime("%Y/%m/%d %H:%M")
    except ValueError:
        return f"{date_text} {time_text}".strip()


def stock_text(quote_data: dict) -> str:
    icon = "▲" if quote_data["change"] > 0 else "▼" if quote_data["change"] < 0 else "▬"
    direction = "上漲" if quote_data["change"] > 0 else "下跌" if quote_data["change"] < 0 else "平盤"
    return "\n".join(
        [
            f"{icon} {quote_data['name']} ({quote_data['symbol']})",
            f"現價：{quote_data['price']:.2f} {quote_data['currency']}",
            f"今日{direction}：{quote_data['change']:+.2f} ({quote_data['percent']:+.2f}%)",
            f"昨收：{quote_data['previous']:.2f}",
            f"交易所：{quote_data['exchange']}",
            f"時間：{quote_data['time']}",
            f"資料來源：{quote_data['source']}",
        ]
    )


def build_stock_messages(user_text: str) -> list[dict] | None:
    symbols = resolve_stock_symbols(user_text)
    if not symbols:
        return None

    messages = []
    fallback_lines = []
    for symbol in symbols:
        try:
            quote_data = fetch_stock_quote(symbol)
        except (requests.RequestException, ValueError) as exc:
            fallback_lines.append(f"{symbol}\n目前查不到報價資料：{exc}")
            continue

        encoded = quote(quote_data["symbol"], safe="")
        image_url = f"{SERVICE_BASE_URL}/stock-image/{encoded}.png"
        messages.append(
            {
                "type": "image",
                "originalContentUrl": image_url,
                "previewImageUrl": image_url,
            }
        )
        fallback_lines.append(stock_text(quote_data))

    if fallback_lines:
        messages.append({"type": "text", "text": trim_for_line("\n\n".join(fallback_lines))})

    return messages or [{"type": "text", "text": "目前查不到報價資料，請確認股票代碼是否正確。"}]


def build_stock_reply(user_text: str) -> str | None:
    symbols = resolve_stock_symbols(user_text)
    if not symbols:
        return None

    replies = []
    for symbol in symbols:
        try:
            quote_data = fetch_stock_quote(symbol)
            replies.append(stock_text(quote_data))
        except (requests.RequestException, ValueError) as exc:
            replies.append(f"{symbol}\n目前查不到報價資料：{exc}")

    return trim_for_line("\n\n".join(replies))


def get_font(size: int) -> ImageFont.ImageFont:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_stock_image(quote_data: dict) -> bytes:
    width, height = 1040, 560
    is_up = quote_data["change"] > 0
    is_down = quote_data["change"] < 0
    accent = (220, 38, 38) if is_up else (22, 163, 74) if is_down else (100, 116, 139)
    bg = (248, 250, 252)
    card = (255, 255, 255)
    text = (15, 23, 42)
    muted = (100, 116, 139)

    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((40, 40, width - 40, height - 40), radius=36, fill=card)
    draw.rectangle((40, 40, 72, height - 40), fill=accent)

    title_font = get_font(54)
    symbol_font = get_font(34)
    price_font = get_font(82)
    label_font = get_font(30)
    small_font = get_font(24)

    symbol = quote_data["symbol"]
    name = quote_data["name"]
    safe_name = name if len(name) <= 28 else f"{name[:28]}..."
    direction = "UP" if is_up else "DOWN" if is_down else "FLAT"
    arrow = "UP" if is_up else "DOWN" if is_down else "FLAT"

    draw.text((110, 90), safe_name, fill=text, font=title_font)
    draw.text((112, 155), f"{symbol} | {quote_data['exchange']}", fill=muted, font=symbol_font)

    draw.text((110, 230), f"{quote_data['price']:.2f}", fill=text, font=price_font)
    draw.text((430, 265), quote_data["currency"], fill=muted, font=label_font)

    change_text = f"{arrow} {direction} {quote_data['change']:+.2f} ({quote_data['percent']:+.2f}%)"
    draw.rounded_rectangle((110, 355, 610, 420), radius=24, fill=accent)
    draw.text((135, 371), change_text, fill=(255, 255, 255), font=label_font)

    draw.text((110, 455), f"Previous close: {quote_data['previous']:.2f}", fill=muted, font=small_font)
    draw.text((520, 455), f"Time: {quote_data['time']}", fill=muted, font=small_font)
    draw.text((110, 495), f"Source: {quote_data['source']}", fill=muted, font=small_font)

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


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
        impact = infer_general_impact(category)

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


def infer_general_impact(category: str) -> str:
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
    stock_reply = build_stock_reply(user_text)
    if stock_reply:
        return stock_reply

    category = resolve_category(user_text)
    if category == "星座運勢":
        return build_horoscope()
    if category in CATEGORY_QUERIES:
        return build_news_summary(category)

    options = "、".join(CATEGORY_ALIASES.keys())
    return f"請輸入想查看的圖文選單項目：{options}\n也可以直接輸入股票代碼，例如：2330、2317、TSLA、AAPL。"


def build_line_messages(user_text: str) -> list[dict]:
    stock_messages = build_stock_messages(user_text)
    if stock_messages:
        return stock_messages
    return [{"type": "text", "text": get_news_by_category(user_text)}]


def build_daily_digest() -> list[str]:
    return [
        build_news_summary("財金重點"),
        build_news_summary("科技趨勢"),
        build_news_summary("虛擬貨幣"),
        build_news_summary("台灣新聞"),
        build_news_summary("國際新聞"),
        build_horoscope(),
    ]


def reply_to_line(reply_token: str, messages: list[dict]) -> None:
    if not LINE_TOKEN:
        raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN is not set")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_TOKEN}",
    }
    payload = {
        "replyToken": reply_token,
        "messages": messages[:5],
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


@app.get("/stock-image/{symbol}.png")
async def stock_image(symbol: str):
    quote_data = fetch_stock_quote(symbol.upper())
    return StreamingResponse(BytesIO(render_stock_image(quote_data)), media_type="image/png")


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

        reply_token = event.get("replyToken")
        if reply_token:
            reply_to_line(reply_token, build_line_messages(message.get("text", "")))

    return {"status": "ok"}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "broadcast":
        broadcast_to_line(build_daily_digest())
        print("Daily digest broadcast sent.")
        raise SystemExit(0)

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
