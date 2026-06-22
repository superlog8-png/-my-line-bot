from __future__ import annotations

import html
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests


REQUEST_TIMEOUT = 15
LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
CTEE_SITE = "https://www.ctee.com.tw/"
CNA_FINANCE_FEED = "https://feeds.feedburner.com/rsscna/finance"
CNA_TECH_FEED = "https://feeds.feedburner.com/rsscna/technology"
CNA_WORLD_FEED = "https://feeds.feedburner.com/rsscna/intworld"
CNA_POLITICS_FEED = "https://feeds.feedburner.com/rsscna/politics"
CNA_SOCIAL_FEED = "https://feeds.feedburner.com/rsscna/social"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}
try:
    TAIPEI_TZ = ZoneInfo("Asia/Taipei")
except ZoneInfoNotFoundError:
    TAIPEI_TZ = timezone(timedelta(hours=8))


@dataclass
class NewsItem:
    title: str
    link: str
    source: str
    published_at: str


@dataclass
class DigestSection:
    key: str
    title: str
    subtitle: str
    bullets: list[str]
    impact: str
    sources: list[str]

    def render(self) -> str:
        bullet_text = "\n".join(f"- {bullet}" for bullet in self.bullets)
        source_text = "、".join(self.sources)
        return (
            f"{self.title}\n"
            f"小標題：{self.subtitle}\n"
            f"{bullet_text}\n"
            f"可能影響：{self.impact}\n"
            f"資料來源：{source_text}"
        )


def _today_tw() -> str:
    return datetime.now(TAIPEI_TZ).strftime("%Y/%m/%d")


def _fetch_text(url: str, *, params: dict | None = None) -> str:
    response = requests.get(
        url,
        params=params,
        headers=DEFAULT_HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    response.encoding = response.encoding or "utf-8"
    return response.text


def _parse_pub_date(value: str) -> str:
    try:
        return parsedate_to_datetime(value).strftime("%Y/%m/%d")
    except Exception:
        return _today_tw()


def _clean_title(title: str) -> str:
    text = html.unescape(title or "").strip()
    if " - " in text:
        text = text.rsplit(" - ", 1)[0].strip()
    return re.sub(r"\s+", " ", text)


def _parse_rss(xml_text: str, default_source: str) -> list[NewsItem]:
    root = ET.fromstring(xml_text)
    items: list[NewsItem] = []
    for node in root.findall(".//item"):
        title = _clean_title(node.findtext("title", default=""))
        link = node.findtext("link", default="").strip()
        source = node.findtext("source", default="").strip() or default_source
        pub_date = _parse_pub_date(node.findtext("pubDate", default=""))
        if title and link:
            items.append(NewsItem(title=title, link=link, source=source, published_at=pub_date))
    return items


def _fetch_rss(url: str, *, default_source: str) -> list[NewsItem]:
    return _parse_rss(_fetch_text(url), default_source)


def _google_news(query: str, *, max_items: int = 8) -> list[NewsItem]:
    xml_text = _fetch_text(
        GOOGLE_NEWS_RSS,
        params={
            "q": query,
            "hl": "zh-TW",
            "gl": "TW",
            "ceid": "TW:zh-Hant",
        },
    )
    return _parse_rss(xml_text, "Google News")[:max_items]


def _dedupe_items(items: list[NewsItem], *, limit: int = 8) -> list[NewsItem]:
    seen: set[str] = set()
    deduped: list[NewsItem] = []
    for item in items:
        key = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", item.title).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def _pattern_bullets(items: list[NewsItem], patterns: list[tuple[str, tuple[str, ...]]]) -> list[str]:
    bullets: list[str] = []
    titles = [item.title for item in items]
    for summary, keywords in patterns:
        if any(_contains_any(title, keywords) for title in titles):
            bullets.append(summary)
    return bullets


def _headline_to_brief(title: str) -> str:
    brief = re.sub(r"[【】「」]", "", title)
    brief = re.sub(r"\s+", " ", brief).strip(" -")
    if len(brief) > 38:
        brief = brief[:37].rstrip() + "…"
    return brief


def _fill_bullets(items: list[NewsItem], bullets: list[str], *, minimum: int = 3, maximum: int = 5) -> list[str]:
    seen = set(bullets)
    for item in items:
        if len(bullets) >= maximum:
            break
        candidate = _headline_to_brief(item.title)
        if candidate not in seen:
            bullets.append(candidate)
            seen.add(candidate)
    if len(bullets) < minimum:
        bullets.append("今日可用公開來源偏少，先以已確認重點整理。")
    return bullets[:maximum]


def _format_sources(items: list[NewsItem], *, prefix: str | None = None, limit: int = 3) -> list[str]:
    labels = []
    if prefix:
        labels.append(prefix)
    for item in items[:limit]:
        labels.append(f"{item.source} {item.published_at} {item.link}")
    return labels


def _fetch_ctee_status() -> tuple[bool, str | None]:
    try:
        response = requests.get(CTEE_SITE, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        if "請稍後再試" in response.text or "Access Denied" in response.text:
            return False, "工商時報來源目前不可取用"
        return True, None
    except requests.RequestException:
        return False, "工商時報來源目前不可取用"


def _fetch_yahoo_quote(symbol: str) -> float | None:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    try:
        response = requests.get(
            url,
            params={"range": "1d", "interval": "1m", "includePrePost": "false"},
            headers=DEFAULT_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            return None
        meta = result[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None:
            return None
        return float(price)
    except Exception:
        return None


def build_finance_section() -> DigestSection:
    ctee_ok, ctee_notice = _fetch_ctee_status()
    if ctee_ok:
        items = _dedupe_items(
            _google_news("site:ctee.com.tw (財經 OR 國際財經 OR 投資理財 OR 股市 OR 產業 OR 科技)"),
            limit=8,
        )
        source_prefix = None
    else:
        fallback_items = _fetch_rss(CNA_FINANCE_FEED, default_source="中央社")
        query_items = _google_news("財經 股市 產業 科技 site:cna.com.tw OR site:moneydj.com OR site:udn.com", max_items=8)
        items = _dedupe_items(fallback_items + query_items, limit=8)
        source_prefix = f"{ctee_notice}；改用公開替代來源"

    patterns = [
        ("資金仍圍繞 AI、半導體與大型權值股，科技權重持續主導市場情緒。", ("AI", "半導體", "晶片", "台積電", "NVIDIA", "輝達")),
        ("油價、匯率與地緣政治消息仍會放大短線震盪，資產輪動速度偏快。", ("油價", "原油", "中東", "荷莫茲", "匯率", "美元")),
        ("外資動向與央行訊號是台股觀察重點，內外資風險偏好仍在拉鋸。", ("外資", "央行", "楊金龍", "升息", "降息", "Fed")),
        ("傳產與出口鏈同步受景氣與成本影響，市場更重視公司實際獲利能見度。", ("出口", "關稅", "航運", "鋼鐵", "塑化", "獲利")),
    ]
    bullets = _fill_bullets(items, _pattern_bullets(items, patterns), minimum=3, maximum=5)
    impact = "科技權值仍是撐盤主軸，但油價、匯率與政策變數會讓短線波動放大，投資上宜更重視資金流向與產業獲利。"
    return DigestSection(
        key="finance",
        title="1. 財金重點",
        subtitle="資金主軸仍在科技，但事件風險左右短線節奏",
        bullets=bullets,
        impact=impact,
        sources=_format_sources(items, prefix=source_prefix),
    )


def build_tech_section() -> DigestSection:
    feed_items = _fetch_rss(CNA_TECH_FEED, default_source="中央社")
    query_items = _google_news("AI 半導體 雲端 電動車 資安 平台 site:cna.com.tw OR site:reuters.com OR site:apnews.com", max_items=8)
    items = _dedupe_items(feed_items + query_items, limit=8)
    patterns = [
        ("AI 資本支出、資料中心與晶片供應鏈仍是科技圈最強主線。", ("AI", "人工智慧", "資料中心", "晶片", "GPU", "半導體")),
        ("大型科技公司持續調整平台與雲端布局，服務整合與商業化速度加快。", ("雲端", "Google", "Microsoft", "Amazon", "Meta", "平台")),
        ("電動車與智慧製造消息持續牽動零組件、電池與自動化鏈評價。", ("電動車", "Tesla", "電池", "自駕", "工廠", "製造")),
        ("資安與監管議題升溫，企業導入新技術時更重視治理與合規。", ("資安", "駭客", "漏洞", "監管", "隱私", "合規")),
    ]
    bullets = _fill_bullets(items, _pattern_bullets(items, patterns), minimum=3, maximum=5)
    impact = "科技股中期敘事仍強，但估值與法規要求同步墊高，題材股更容易出現分化。"
    return DigestSection(
        key="tech",
        title="2. 科技趨勢",
        subtitle="AI 擴張帶動供應鏈，平台與資安同步升溫",
        bullets=bullets,
        impact=impact,
        sources=_format_sources(items),
    )


def build_crypto_section() -> DigestSection:
    items = _dedupe_items(
        _google_news("Bitcoin Ethereum ETF 交易所 監管 site:coindesk.com OR site:theblock.co OR site:decrypt.co", max_items=8)
        + _google_news("Bitcoin Ethereum ETF crypto regulation", max_items=8),
        limit=8,
    )
    patterns = [
        ("Bitcoin 與 Ethereum 走勢仍由 ETF 資金流與美元利率預期主導。", ("Bitcoin", "BTC", "Ethereum", "ETH", "ETF")),
        ("監管與交易所政策訊號持續影響主流幣風險偏好，市場偏向選擇性進場。", ("監管", "SEC", "交易所", "法規", "監理")),
        ("鏈上資金與機構產品更新，反映市場更重視流動性與產品結構。", ("鏈上", "staking", "token", "fund", "資金流", "產品")),
    ]
    seed_bullets: list[str] = []
    btc = _fetch_yahoo_quote("BTC-USD")
    eth = _fetch_yahoo_quote("ETH-USD")
    if btc is not None or eth is not None:
        price_parts = []
        if btc is not None:
            price_parts.append(f"BTC 約 {btc:,.0f} 美元")
        if eth is not None:
            price_parts.append(f"ETH 約 {eth:,.0f} 美元")
        seed_bullets.append("、".join(price_parts) + "，市場先看主流幣能否延續量能。")
    bullets = _fill_bullets(items, seed_bullets + _pattern_bullets(items, patterns), minimum=3, maximum=5)
    impact = "幣市短線仍是高波動資產，若 ETF 淨流入沒有明顯擴大，主流幣較可能維持區間整理。"
    return DigestSection(
        key="crypto",
        title="3. 虛擬貨幣",
        subtitle="主流幣看 ETF 與監管，市場情緒仍偏觀望",
        bullets=bullets,
        impact=impact,
        sources=_format_sources(items),
    )


def build_taiwan_section() -> DigestSection:
    politics = _fetch_rss(CNA_POLITICS_FEED, default_source="中央社")
    finance = _fetch_rss(CNA_FINANCE_FEED, default_source="中央社")
    tech = _fetch_rss(CNA_TECH_FEED, default_source="中央社")
    social = _fetch_rss(CNA_SOCIAL_FEED, default_source="中央社")
    items = _dedupe_items(politics + finance + tech + social, limit=10)
    patterns = [
        ("台灣今日主軸仍圍繞政治與國安議題，對外訊號與內部韌性同步受關注。", ("總統", "國安", "軍演", "共機", "國防", "外交")),
        ("產業與經濟消息持續聚焦科技、出口與投資動能。", ("半導體", "科技", "投資", "出口", "產業", "台積電")),
        ("民生與社會面仍有天氣、災防與公共安全訊息需要留意。", ("地震", "豪雨", "停電", "交通", "災害", "社會")),
    ]
    bullets = _fill_bullets(items, _pattern_bullets(items, patterns), minimum=3, maximum=5)
    impact = "台灣新聞今天偏向安全、產業與民生並進，政策訊號與公共風險管理都會影響市場和社會情緒。"
    return DigestSection(
        key="taiwan",
        title="4. 台灣新聞",
        subtitle="政治國安、產業動能與民生風險同步受關注",
        bullets=bullets,
        impact=impact,
        sources=_format_sources(items),
    )


def build_world_section() -> DigestSection:
    world_feed = _fetch_rss(CNA_WORLD_FEED, default_source="中央社")
    query_items = _google_news("G7 地緣政治 戰爭 外交 能源 氣候 site:cna.com.tw OR site:apnews.com OR site:reuters.com", max_items=8)
    items = _dedupe_items(world_feed + query_items, limit=10)
    patterns = [
        ("G7、烏俄與主要外交場域後續效應仍在延伸，國際安全議題沒有降溫。", ("G7", "烏克蘭", "俄羅斯", "北約", "制裁")),
        ("中東局勢與能源航道風險持續干擾全球油氣、航運與保險成本。", ("中東", "伊朗", "以色列", "荷莫茲", "原油", "天然氣")),
        ("極端氣候與能源轉型議題交錯，歐洲與亞洲都在面對供應與高溫壓力。", ("氣候", "熱浪", "高溫", "能源", "電力", "LNG")),
    ]
    bullets = _fill_bullets(items, _pattern_bullets(items, patterns), minimum=3, maximum=5)
    impact = "國際市場仍容易被戰事、外交與能源供應消息牽動，油氣、航運與風險資產波動可能同步放大。"
    return DigestSection(
        key="world",
        title="5. 國際新聞",
        subtitle="安全、能源與氣候三條主線持續牽動全球",
        bullets=bullets,
        impact=impact,
        sources=_format_sources(items),
    )


HOROSCOPE_SIGNS = [
    ("牡羊", "aries"),
    ("金牛", "taurus"),
    ("雙子", "gemini"),
    ("巨蟹", "cancer"),
    ("獅子", "leo"),
    ("處女", "virgo"),
    ("天秤", "libra"),
    ("天蠍", "scorpio"),
    ("射手", "sagittarius"),
    ("摩羯", "capricorn"),
    ("水瓶", "aquarius"),
    ("雙魚", "pisces"),
]


def _condense_horoscope(text: str) -> str:
    english = re.sub(r"<[^>]+>", "", text)
    english = re.sub(r"\s+", " ", english).strip()
    sentence = re.split(r"(?<=[.!?])\s+", english)[0].strip()
    replacements = [
        (("creative", "inspiration", "authentic"), "適合做自己、展現創意，靈感與人際支持都不錯。"),
        (("work", "career", "plan"), "工作與安排要更有計畫，先穩住節奏再推進。"),
        (("relationship", "love", "heart"), "感情與人際互動有亮點，真誠表達比較有收穫。"),
        (("money", "finance", "budget"), "財務與消費要保守一點，細節先確認再出手。"),
        (("rest", "self-care", "recharge"), "今天適合照顧自己，放慢步調反而更有效率。"),
    ]
    lowered = sentence.lower()
    for keywords, zh in replacements:
        if any(keyword in lowered for keyword in keywords):
            return zh
    if "don't" in lowered or "avoid" in lowered or "caution" in lowered:
        return "今天先求穩不求快，避免衝動決定與硬碰硬。"
    if sentence:
        return "今天重點在調整節奏與情緒，先把最重要的事處理好。"
    return "今天先穩住節奏，保持彈性會比硬推更順。"


def _fetch_horoscope_payload(sign_slug: str) -> tuple[str, str | None]:
    url = f"https://www.astrology.com/horoscope/daily/{quote_plus(sign_slug)}.html"
    html_text = _fetch_text(url)
    date_match = re.search(
        r"Daily Horoscope for ([A-Za-z]+ \d{1,2}, \d{4})",
        html_text,
        flags=re.IGNORECASE,
    )
    source_date = None
    if date_match:
        try:
            source_date = datetime.strptime(date_match.group(1), "%B %d, %Y").strftime("%Y/%m/%d")
        except ValueError:
            source_date = None
    match = re.search(r"<p><span[^>]*>(.*?)</span></p>", html_text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return html.unescape(match.group(1)), source_date
    match = re.search(r"horoscopeText:\s*\"(.*?)\"", html_text, flags=re.DOTALL)
    if match:
        return bytes(match.group(1), "utf-8").decode("unicode_escape"), source_date
    raise ValueError(f"Unable to parse horoscope for {sign_slug}")


def build_horoscope_section() -> DigestSection:
    bullets: list[str] = []
    source_date = None
    for zh_name, slug in HOROSCOPE_SIGNS:
        try:
            text, parsed_date = _fetch_horoscope_payload(slug)
            source_date = source_date or parsed_date
            summary = _condense_horoscope(text)
        except Exception:
            summary = "今天先穩住情緒與步調，避免把事情推得太急。"
        bullets.append(f"{zh_name}：{summary}")
    impact = "娛樂參考即可；適合拿來做互動導流，不建議作為重大決策依據。"
    shown_date = source_date or _today_tw()
    return DigestSection(
        key="horoscope",
        title="6. 星座運勢",
        subtitle=f"12 星座運勢｜娛樂參考｜來源頁日期 {shown_date}",
        bullets=bullets,
        impact=impact,
        sources=[f"Astrology.com {shown_date} https://www.astrology.com/horoscope/daily.html"],
    )


def build_daily_digest_sections() -> list[DigestSection]:
    builders: list[Callable[[], DigestSection]] = [
        build_finance_section,
        build_tech_section,
        build_crypto_section,
        build_taiwan_section,
        build_world_section,
        build_horoscope_section,
    ]
    return [builder() for builder in builders]


def build_daily_digest_messages() -> list[str]:
    return [section.render() for section in build_daily_digest_sections()]


def broadcast_digest_messages(messages: list[str], token: str | None = None) -> dict:
    access_token = token or os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    if not access_token:
        return {
            "ok": False,
            "reason": "缺少環境變數 LINE_CHANNEL_ACCESS_TOKEN，未發送 LINE broadcast。",
        }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }

    for text in messages:
        response = requests.post(
            LINE_BROADCAST_URL,
            headers=headers,
            json={"messages": [{"type": "text", "text": text[:4800]}]},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code >= 400:
            return {
                "ok": False,
                "reason": f"LINE API 回傳錯誤：HTTP {response.status_code}",
            }

    return {"ok": True, "message_count": len(messages)}
