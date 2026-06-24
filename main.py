import html
import hashlib
import os
import re
import sys
import textwrap
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from io import BytesIO
from time import time
from urllib.parse import quote

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from PIL import Image, ImageDraw, ImageFont

from daily_digest import (
    broadcast_digest_messages,
    build_daily_digest_messages,
    build_daily_digest_section,
)


app = FastAPI()

LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
MASSAGE_LINE_TOKEN = os.getenv("MASSAGE_LINE_CHANNEL_ACCESS_TOKEN")
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"
BOT_PROFILE = os.getenv("BOT_PROFILE", "main").strip().lower()
SERVICE_BASE_URL = os.getenv("SERVICE_BASE_URL", "https://my-line-bot-yuht.onrender.com").rstrip("/")
PRICE_LIST_IMAGE_PATH = os.path.join(os.path.dirname(__file__), "assets", "price-list.jpg")
PRICE_LIST_IMAGE_URL = f"{SERVICE_BASE_URL}/price-list.jpg"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
TWSE_STOCK_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://mis.twse.com.tw/stock/fibest.jsp?lang=zh_tw",
}
REQUEST_TIMEOUT = 12
MAX_ITEMS = 6
QUOTE_CACHE_TTL_SECONDS = 45
CHART_CACHE_TTL_SECONDS = 600
NEWS_CACHE_TTL_SECONDS = 600
CJK_FONT_URL = "https://github.com/googlefonts/noto-cjk/raw/main/Sans/OTF/TraditionalChinese/NotoSansCJKtc-Regular.otf"
CJK_FONT_PATH = "/tmp/NotoSansCJKtc-Regular.otf"
FONT_DOWNLOAD_ATTEMPTED = False

QUOTE_CACHE = {}
CHART_CACHE = {}
NEWS_CACHE = {}
LINK_CACHE = {}


def cache_get(cache: dict, key: str, ttl_seconds: int):
    cached = cache.get(key)
    if not cached:
        return None
    saved_at, value = cached
    if time() - saved_at > ttl_seconds:
        cache.pop(key, None)
        return None
    return value


def cache_set(cache: dict, key: str, value):
    cache[key] = (time(), value)
    return value


def make_short_link(url: str) -> str:
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    LINK_CACHE[key] = url
    return f"{SERVICE_BASE_URL}/r/{key}"


CATEGORY_ALIASES = {
    "財金重點": {"財金", "財金重點", "財經", "財經重點", "請給我財金重點", "請給我財經"},
    "科技趨勢": {"科技", "科技趨勢", "請給我科技趨勢", "請給我科技"},
    "虛擬貨幣": {"虛擬貨幣", "加密貨幣", "幣圈", "crypto", "請給我虛擬貨幣"},
    "台灣新聞": {"台灣", "台灣新聞", "請給我台灣新聞"},
    "國際新聞": {"國際", "國際新聞", "請給我國際新聞"},
    "財經新聞": {"財經新聞", "財經", "財經要聞", "財經重點", "金融新聞", "投資新聞"},
    "社會新聞": {"社會新聞", "社會", "台灣社會", "社會要聞", "民生新聞"},
    "國際財經": {"國際財經", "全球財經", "國際金融", "全球市場", "國際股市"},
    "國際社會": {"國際社會", "全球社會", "國際民生", "國際事件", "世界社會"},
    "星座運勢": {"星座", "星座運勢", "運勢", "請給我星座運勢"},
}

CATEGORY_QUERIES = {
    "財金重點": "site:ctee.com.tw 工商時報 財經 OR 股市 OR 投資 OR 產業 OR 國際財經",
    "科技趨勢": "AI 半導體 科技趨勢 台積電 NVIDIA 雲端 資安 最新",
    "虛擬貨幣": "Bitcoin Ethereum ETF 虛擬貨幣 加密貨幣 監管 最新",
    "台灣新聞": "台灣 最新 新聞 政治 經濟 社會 產業",
    "國際新聞": "國際 最新 新聞 G7 地緣政治 能源 烏克蘭 中東",
    "財經新聞": "site:ctee.com.tw/news 工商時報 財經 股市 投資 金融 產業 最新",
    "社會新聞": "台灣 社會新聞 民生 治安 交通 教育 生活 最新",
    "國際財經": "國際財經 全球市場 美股 Fed 匯率 油價 經濟 最新",
    "國際社會": "國際 新聞 災害 犯罪 人權 最新",
}

STOCK_PREFIXES = ("股票", "股價", "查股票", "查股價", "代碼")
PRICE_LIST_KEYWORDS = {
    "按摩",
    "價目",
    "價目表",
    "價格",
    "價錢",
    "收費",
    "服務項目",
    "按摩價目",
    "按摩價格",
    "按摩價目表",
}


def normalize_text(text: str) -> str:
    return " ".join((text or "").strip().split()).lower()


def is_price_list_request(user_text: str) -> bool:
    normalized = normalize_text(user_text)
    return any(keyword in normalized for keyword in PRICE_LIST_KEYWORDS)


def build_price_list_messages() -> list[dict]:
    return [
        {
            "type": "image",
            "originalContentUrl": PRICE_LIST_IMAGE_URL,
            "previewImageUrl": PRICE_LIST_IMAGE_URL,
        },
        {
            "type": "text",
            "text": "這是紳士按摩價目表。可直接點圖片放大查看，也可以回覆想預約的服務項目。",
        },
    ]


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
        code = symbol.removesuffix(".TW")
        try:
            return fetch_taiwan_stock_quote(code)
        except ValueError:
            try:
                return fetch_yahoo_stock_quote(f"{code}.TW")
            except (requests.RequestException, ValueError):
                return fetch_yahoo_stock_quote(f"{code}.TWO")
    return fetch_yahoo_stock_quote(symbol)


def get_cached_stock_quote(symbol: str) -> dict:
    key = symbol.upper()
    cached = cache_get(QUOTE_CACHE, key, QUOTE_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    return cache_set(QUOTE_CACHE, key, fetch_stock_quote(key))


def fetch_yahoo_stock_quote(symbol: str) -> dict:
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
        "chart_symbol": meta.get("symbol", symbol),
    }


def fetch_stock_history(symbol: str, quote_data: dict | None = None) -> list[dict]:
    chart_symbol = (quote_data or {}).get("chart_symbol") or symbol
    params = {
        "range": "3mo",
        "interval": "1d",
        "includePrePost": "false",
    }
    response = requests.get(
        YAHOO_CHART_URL.format(symbol=chart_symbol),
        params=params,
        headers=REQUEST_HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    result = data.get("chart", {}).get("result") or []
    if not result:
        raise ValueError(f"No chart data for {chart_symbol}")

    timestamps = result[0].get("timestamp") or []
    quote_series = (result[0].get("indicators", {}).get("quote") or [{}])[0]
    opens = quote_series.get("open") or []
    highs = quote_series.get("high") or []
    lows = quote_series.get("low") or []
    closes = quote_series.get("close") or []
    volumes = quote_series.get("volume") or []

    candles = []
    for index, timestamp in enumerate(timestamps):
        try:
            candle = {
                "date": datetime.fromtimestamp(timestamp).strftime("%m/%d"),
                "open": opens[index],
                "high": highs[index],
                "low": lows[index],
                "close": closes[index],
                "volume": volumes[index] if index < len(volumes) else None,
            }
        except IndexError:
            continue
        if all(candle[key] is not None for key in ("open", "high", "low", "close")):
            candles.append(candle)

    if not candles:
        raise ValueError(f"No usable chart data for {chart_symbol}")
    return candles[-60:]


def get_cached_stock_history(symbol: str, quote_data: dict | None = None) -> list[dict]:
    chart_symbol = (quote_data or {}).get("chart_symbol") or symbol
    key = chart_symbol.upper()
    cached = cache_get(CHART_CACHE, key, CHART_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    return cache_set(CHART_CACHE, key, fetch_stock_history(symbol, quote_data))


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
                "chart_symbol": f"{code}.TWO" if label == "TPEx" else f"{code}.TW",
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

    bubbles = []
    fallback_lines = []
    for symbol in symbols:
        try:
            quote_data = get_cached_stock_quote(symbol)
        except (requests.RequestException, ValueError) as exc:
            fallback_lines.append(f"{symbol}\n目前查不到報價資料：{exc}")
            continue

        encoded = quote(quote_data["symbol"], safe="")
        chart_url = f"{SERVICE_BASE_URL}/stock-chart/{encoded}.png"
        detail_url = f"{SERVICE_BASE_URL}/stock-detail/{encoded}"
        bubbles.extend(build_stock_flex_bubbles(quote_data, chart_url, detail_url))
        fallback_lines.append(stock_text(quote_data))

    if bubbles:
        return [
            {
                "type": "flex",
                "altText": "股票報價與K線圖",
                "contents": {
                    "type": "carousel",
                    "contents": bubbles[:12],
                },
            }
        ]

    return [{"type": "text", "text": trim_for_line("\n\n".join(fallback_lines) or "目前查不到報價資料，請確認股票代碼是否正確。")}]


def build_stock_flex_bubbles(quote_data: dict, chart_url: str, detail_url: str) -> list[dict]:
    is_up = quote_data["change"] > 0
    is_down = quote_data["change"] < 0
    color = "#DC2626" if is_up else "#16A34A" if is_down else "#64748B"
    icon = "▲" if is_up else "▼" if is_down else "▬"
    direction = "上漲" if is_up else "下跌" if is_down else "平盤"
    title = f"{quote_data['name']} ({quote_data['symbol']})"
    subtitle = f"{icon} 今日{direction} {quote_data['change']:+.2f} ({quote_data['percent']:+.2f}%)"

    summary_bubble = {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {"type": "text", "text": title, "weight": "bold", "size": "xl", "wrap": True},
                {"type": "text", "text": subtitle, "weight": "bold", "size": "lg", "color": color},
                {"type": "separator"},
                stock_flex_row("現價", f"{quote_data['price']:.2f} {quote_data['currency']}"),
                stock_flex_row("昨收", f"{quote_data['previous']:.2f}"),
                stock_flex_row("交易所", quote_data["exchange"]),
                stock_flex_row("時間", quote_data["time"]),
                stock_flex_row("來源", quote_data["source"]),
            ],
        },
    }

    chart_bubble = {
        "type": "bubble",
        "size": "mega",
        "hero": {
            "type": "image",
            "url": chart_url,
            "size": "full",
            "aspectRatio": "16:9",
            "aspectMode": "cover",
            "action": {
                "type": "uri",
                "label": "查看詳細資訊",
                "uri": detail_url,
            },
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {"type": "text", "text": f"{quote_data['symbol']} 3個月日K線", "weight": "bold", "size": "lg"},
                {"type": "text", "text": "紅K代表收高，綠K代表收低。", "size": "sm", "color": "#64748B", "wrap": True},
            ],
        },
    }

    return [summary_bubble, chart_bubble]


def stock_flex_row(label: str, value: str) -> dict:
    return {
        "type": "box",
        "layout": "baseline",
        "contents": [
            {"type": "text", "text": label, "size": "sm", "color": "#64748B", "flex": 2},
            {"type": "text", "text": value, "size": "sm", "color": "#111827", "wrap": True, "flex": 5},
        ],
    }


def build_stock_reply(user_text: str) -> str | None:
    symbols = resolve_stock_symbols(user_text)
    if not symbols:
        return None

    replies = []
    for symbol in symbols:
        try:
            quote_data = get_cached_stock_quote(symbol)
            replies.append(stock_text(quote_data))
        except (requests.RequestException, ValueError) as exc:
            replies.append(f"{symbol}\n目前查不到報價資料：{exc}")

    return trim_for_line("\n\n".join(replies))


def get_font(size: int) -> ImageFont.ImageFont:
    global FONT_DOWNLOAD_ATTEMPTED
    for path in (
        "C:/Windows/Fonts/msjh.ttc",
        "C:/Windows/Fonts/msjhbd.ttc",
        CJK_FONT_PATH,
        "/opt/render/project/src/NotoSansCJKtc-Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKtc-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    if not FONT_DOWNLOAD_ATTEMPTED:
        FONT_DOWNLOAD_ATTEMPTED = True
        try:
            response = requests.get(CJK_FONT_URL, timeout=8)
            response.raise_for_status()
            with open(CJK_FONT_PATH, "wb") as font_file:
                font_file.write(response.content)
            return ImageFont.truetype(CJK_FONT_PATH, size)
        except (OSError, requests.RequestException):
            pass
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


def render_candlestick_chart(quote_data: dict, candles: list[dict]) -> bytes:
    width, height = 480, 360
    bg = (31, 33, 35)
    grid = (118, 125, 130)
    axis_text = (215, 225, 235)
    muted = (137, 146, 153)
    blue = (86, 166, 255)
    cyan = (24, 210, 224)
    up = (0, 192, 118)
    down = (243, 82, 68)

    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)

    title_font = get_font(19)
    price_font = get_font(18)
    axis_font = get_font(14)
    small_font = get_font(13)

    chart_left, chart_top, chart_right, chart_bottom = 62, 88, 20, 234
    volume_top, volume_bottom = 274, 324
    chart_w = width - chart_left - chart_right
    chart_h = chart_bottom - chart_top
    plot_right = width - chart_right

    name = quote_data["name"]
    if len(name) > 12:
        name = name[:12]
    code = quote_data["symbol"].split(".")[0]
    is_up = quote_data["change"] > 0
    is_down = quote_data["change"] < 0
    change_color = up if is_up else down if is_down else muted
    arrow = "▼" if is_down else "▲" if is_up else "▬"

    draw.text((18, 16), f"{name} ({code})", fill=(245, 245, 245), font=title_font)
    draw.text(
        (18, 42),
        f"{quote_data['price']:.1f} {arrow} {quote_data['change']:+.1f} ({quote_data['percent']:+.2f}%)",
        fill=change_color,
        font=price_font,
    )
    latest_volume = candles[-1].get("volume") or 0
    volume_text = str(int(latest_volume))
    draw.text((386, 41), f"量：{volume_text}", fill=blue, font=small_font)

    highs = [candle["high"] for candle in candles]
    lows = [candle["low"] for candle in candles]
    max_price = max(highs)
    min_price = min(lows)
    padding = (max_price - min_price) * 0.08 or max_price * 0.02 or 1
    max_price += padding
    min_price -= padding

    def y_for(price: float) -> float:
        return chart_top + ((max_price - price) / (max_price - min_price)) * chart_h

    price_ticks = [max_price - (max_price - min_price) * i / 5 for i in range(6)]
    for price in price_ticks:
        y = y_for(price)
        draw.line((chart_left, y, plot_right, y), fill=grid, width=1)
        label = f"{price:.2f}".rstrip("0").rstrip(".")
        draw.text((24, y - 8), label, fill=axis_text, font=axis_font)

    candle_count = len(candles)
    step = chart_w / max(candle_count, 1)
    body_w = max(3, min(7, step * 0.7))

    closes = [candle["close"] for candle in candles]
    ma_values = []
    for index in range(candle_count):
        start = max(0, index - 9)
        ma_values.append(sum(closes[start : index + 1]) / (index - start + 1))

    for index, candle in enumerate(candles):
        x = chart_left + step * index + step / 2
        open_y = y_for(candle["open"])
        close_y = y_for(candle["close"])
        high_y = y_for(candle["high"])
        low_y = y_for(candle["low"])
        color = up if candle["close"] >= candle["open"] else down
        draw.line((x, high_y, x, low_y), fill=color, width=1)
        top_y = min(open_y, close_y)
        bottom_y = max(open_y, close_y)
        if bottom_y - top_y < 2:
            bottom_y = top_y + 2
        draw.rectangle((x - body_w / 2, top_y, x + body_w / 2, bottom_y), fill=color)

    ma_points = [
        (chart_left + step * index + step / 2, y_for(value))
        for index, value in enumerate(ma_values)
    ]
    if len(ma_points) > 1:
        draw.line(ma_points, fill=cyan, width=2)

    volumes = [candle.get("volume") or 0 for candle in candles]
    max_volume = max(volumes) or 1
    draw.text((10, volume_top - 5), f"{int(max_volume)}", fill=muted, font=small_font)
    draw.text((50, volume_bottom - 8), "0", fill=muted, font=small_font)
    draw.line((chart_left, volume_bottom, plot_right, volume_bottom), fill=grid, width=1)

    for index, candle in enumerate(candles):
        x = chart_left + step * index + step / 2
        volume = candle.get("volume") or 0
        bar_h = (volume / max_volume) * (volume_bottom - volume_top)
        color = up if candle["close"] >= candle["open"] else down
        draw.rectangle(
            (x - body_w / 2, volume_bottom - bar_h, x + body_w / 2, volume_bottom),
            fill=color,
        )

    month_positions = []
    seen_months = set()
    for index, candle in enumerate(candles):
        month = candle["date"].split("/")[0]
        if month not in seen_months:
            seen_months.add(month)
            month_positions.append((index, month))
    for index, month in month_positions[-3:]:
        x = chart_left + step * index + step / 2
        draw.text((x - 10, 329), f"{int(month)}月", fill=muted, font=axis_font)

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def build_news_summary(category: str) -> str:
    cached_summary = cache_get(NEWS_CACHE, category, NEWS_CACHE_TTL_SECONDS)
    if cached_summary is not None:
        return cached_summary

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
        f"{index}. {item['source']}｜{item['published_at']} {make_short_link(item['link'])}"
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
    if category == "財經新聞":
        return "- 觀察台股、金融、產業資金流與政策變化對投資情緒的影響。"
    if category == "社會新聞":
        return "- 觀察民生、治安、交通、教育與公共安全議題對日常生活的影響。"
    if category == "國際財經":
        return "- 觀察美股、利率、匯率、能源與全球資金流向對市場風險偏好的影響。"
    if category == "國際社會":
        return "- 觀察災害、社會衝突、人權、民生與跨國事件對區域穩定的影響。"
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
        cached_summary = cache_get(NEWS_CACHE, category, NEWS_CACHE_TTL_SECONDS)
        if cached_summary is not None:
            return cached_summary
        return cache_set(NEWS_CACHE, category, build_news_summary(category))

    options = "、".join(CATEGORY_ALIASES.keys())
    return f"請輸入想查看的圖文選單項目：{options}\n也可以直接輸入股票代碼，例如：2330、2317、TSLA、AAPL。"


def build_line_messages_for_profile(user_text: str, profile: str) -> list[dict]:
    if profile == "massage":
        if is_price_list_request(user_text):
            return build_price_list_messages()
        return [
            {
                "type": "text",
                "text": "歡迎使用紳士按摩官方帳號。\n請輸入：按摩、價目、價目表、價格、收費、服務項目。",
            }
        ]

    return build_main_profile_messages(user_text)


def build_line_messages(user_text: str) -> list[dict]:
    return build_line_messages_for_profile(user_text, BOT_PROFILE)


POSTBACK_CATEGORY_MAP = {
    "finance": "財金重點",
    "tech": "科技趨勢",
    "crypto": "虛擬貨幣",
    "taiwan": "台灣新聞",
    "world": "國際新聞",
    "horoscope": "星座運勢",
    "price-list": "價目表",
    "price_list": "價目表",
}

DIGEST_CATEGORY_MAP = {
    "finance": "finance",
    "財金重點": "finance",
    "tech": "tech",
    "科技趨勢": "tech",
    "crypto": "crypto",
    "虛擬貨幣": "crypto",
    "taiwan": "taiwan",
    "台灣新聞": "taiwan",
    "world": "world",
    "國際新聞": "world",
    "horoscope": "horoscope",
    "星座運勢": "horoscope",
}


def decode_postback_data(data: str) -> str:
    raw = (data or "").strip()
    if not raw:
        return ""

    normalized = raw.replace("&", ";")
    pairs = {}
    for chunk in normalized.split(";"):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        pairs[key.strip().lower()] = value.strip()

    for key in ("category", "menu", "action", "type"):
        value = pairs.get(key, "")
        if value:
            return POSTBACK_CATEGORY_MAP.get(value.lower(), value)

    return POSTBACK_CATEGORY_MAP.get(raw.lower(), raw)


def get_digest_message_by_category(name: str) -> str | None:
    digest_key = DIGEST_CATEGORY_MAP.get((name or "").strip())
    if not digest_key:
        return None

    return build_daily_digest_section(digest_key).render()


def build_main_profile_messages(user_text: str) -> list[dict]:
    digest_message = get_digest_message_by_category(user_text)
    if digest_message:
        return [{"type": "text", "text": trim_for_line(digest_message)}]

    stock_messages = build_stock_messages(user_text)
    if stock_messages:
        return stock_messages

    return [{"type": "text", "text": get_news_by_category(user_text)}]


def build_line_messages_from_event(event: dict, profile: str) -> list[dict] | None:
    event_type = event.get("type")

    if event_type == "message":
        message = event.get("message", {})
        if message.get("type") != "text":
            return None
        return build_line_messages_for_profile(message.get("text", ""), profile)

    if event_type == "postback":
        resolved = decode_postback_data(event.get("postback", {}).get("data", ""))
        if not resolved:
            return [{"type": "text", "text": "此圖文選單項目尚未設定內容。"}]
        return build_line_messages_for_profile(resolved, profile)

    return None


def build_daily_digest() -> list[str]:
    return build_daily_digest_messages()


def reply_to_line_with_token(reply_token: str, messages: list[dict], token: str | None, env_name: str) -> None:
    if not token:
        raise RuntimeError(f"{env_name} is not set")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
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


def reply_to_line(reply_token: str, messages: list[dict]) -> None:
    reply_to_line_with_token(reply_token, messages, LINE_TOKEN, "LINE_CHANNEL_ACCESS_TOKEN")


def reply_to_massage_line(reply_token: str, messages: list[dict]) -> None:
    reply_to_line_with_token(reply_token, messages, MASSAGE_LINE_TOKEN, "MASSAGE_LINE_CHANNEL_ACCESS_TOKEN")


def broadcast_to_line(messages: list[str]) -> None:
    result = broadcast_digest_messages(messages, LINE_TOKEN)
    if not result.get("ok"):
        raise RuntimeError(result["reason"])


@app.get("/")
async def root():
    return {"message": "LINE Bot Server is running!", "profile": BOT_PROFILE}


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "profile": BOT_PROFILE}


@app.get("/preview/{category}")
async def preview(category: str):
    return {"category": category, "message": get_news_by_category(category)}


@app.get("/price-list.jpg")
async def price_list_image():
    return FileResponse(PRICE_LIST_IMAGE_PATH, media_type="image/jpeg")


@app.get("/r/{key}")
async def redirect_short_link(key: str):
    url = LINK_CACHE.get(key)
    if not url:
        raise HTTPException(status_code=404, detail="Link expired")
    return RedirectResponse(url=url, status_code=302)


@app.get("/stock-image/{symbol}.png")
async def stock_image(symbol: str):
    quote_data = get_cached_stock_quote(symbol.upper())
    return StreamingResponse(BytesIO(render_stock_image(quote_data)), media_type="image/png")


@app.get("/stock-chart/{symbol}.png")
async def stock_chart(symbol: str):
    quote_data = get_cached_stock_quote(symbol.upper())
    candles = get_cached_stock_history(symbol.upper(), quote_data)
    return StreamingResponse(BytesIO(render_candlestick_chart(quote_data, candles)), media_type="image/png")


@app.get("/stock-detail/{symbol}", response_class=HTMLResponse)
async def stock_detail(symbol: str):
    quote_data = get_cached_stock_quote(symbol.upper())
    encoded = quote(quote_data["symbol"], safe="")
    chart_url = f"{SERVICE_BASE_URL}/stock-chart/{encoded}.png"
    is_up = quote_data["change"] > 0
    is_down = quote_data["change"] < 0
    color = "#00c076" if is_up else "#f35244" if is_down else "#9ca3af"
    arrow = "▲" if is_up else "▼" if is_down else "▬"
    direction = "上漲" if is_up else "下跌" if is_down else "平盤"
    title = html.escape(f"{quote_data['name']} ({quote_data['symbol']})")
    rows = [
        ("現價", f"{quote_data['price']:.2f} {quote_data['currency']}"),
        ("今日漲跌", f"{arrow} {direction} {quote_data['change']:+.2f} ({quote_data['percent']:+.2f}%)"),
        ("昨收", f"{quote_data['previous']:.2f}"),
        ("交易所", quote_data["exchange"]),
        ("時間", quote_data["time"]),
        ("資料來源", quote_data["source"]),
    ]
    row_html = "\n".join(
        f"<div class='row'><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>"
        for label, value in rows
    )
    return f"""
<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{
      margin: 0;
      background: #111827;
      color: #f9fafb;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans TC", sans-serif;
    }}
    main {{
      max-width: 760px;
      margin: 0 auto;
      padding: 20px;
    }}
    .card {{
      background: #1f2328;
      border-radius: 16px;
      padding: 18px;
      box-shadow: 0 10px 28px rgba(0, 0, 0, .28);
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: 24px;
    }}
    .change {{
      color: {color};
      font-size: 22px;
      font-weight: 700;
      margin-bottom: 14px;
    }}
    img {{
      width: 100%;
      border-radius: 10px;
      display: block;
      background: #202224;
    }}
    .rows {{
      margin-top: 14px;
      border-top: 1px solid #374151;
    }}
    .row {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      padding: 12px 0;
      border-bottom: 1px solid #374151;
    }}
    .row span {{
      color: #9ca3af;
    }}
    .row strong {{
      text-align: right;
    }}
  </style>
</head>
<body>
  <main>
    <section class="card">
      <h1>{title}</h1>
      <div class="change">{html.escape(rows[1][1])}</div>
      <img src="{html.escape(chart_url)}" alt="{title} K線圖">
      <div class="rows">{row_html}</div>
    </section>
  </main>
</body>
</html>
"""


@app.post("/broadcast/daily")
async def broadcast_daily():
    messages = build_daily_digest()
    result = broadcast_digest_messages(messages, LINE_TOKEN)
    if not result.get("ok"):
        return {
            "status": "error",
            "reason": result["reason"],
            "message_count": len(messages),
            "messages": messages,
        }
    return {"status": "ok", "message_count": result["message_count"], "messages": messages}


@app.post("/webhook")
async def handle_webhook(request: Request):
    body = await request.json()
    events = body.get("events", [])

    for event in events:
        reply_token = event.get("replyToken")
        messages = build_line_messages_from_event(event, "main")
        if reply_token and messages:
            reply_to_line(reply_token, messages)

    return {"status": "ok"}


@app.post("/webhook/massage")
async def handle_massage_webhook(request: Request):
    body = await request.json()
    events = body.get("events", [])

    for event in events:
        reply_token = event.get("replyToken")
        messages = build_line_messages_from_event(event, "massage")
        if reply_token and messages:
            reply_to_massage_line(reply_token, messages)

    return {"status": "ok", "profile": "massage"}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "broadcast":
        broadcast_to_line(build_daily_digest())
        print("Daily digest broadcast sent.")
        raise SystemExit(0)

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
