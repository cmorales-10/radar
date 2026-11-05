"""Free public data sources. Stdlib only. Every fetched value is untrusted data."""
import concurrent.futures
import http.cookiejar
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
SYMBOL = re.compile(r"^[A-Z]{1,5}$")
YAHOO = "https://query1.finance.yahoo.com"


def _get(url, user_agent=BROWSER_UA, opener=None, retries=2):
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "*/*"})
    opened = opener.open if opener else urllib.request.urlopen
    for attempt in range(retries + 1):
        try:
            with opened(request, timeout=20) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == retries:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
        time.sleep(2 * (attempt + 1))


def _get_json(url, **kwargs):
    return json.loads(_get(url, **kwargs))


def reddit_mentions():
    """ApeWisdom's ranking of tickers mentioned across stock subreddits."""
    results = _get_json("https://apewisdom.io/api/v1.0/filter/all-stocks/page/1")["results"]
    return {
        r["ticker"]: {
            "reddit_rank": r.get("rank"),
            "reddit_mentions": r.get("mentions"),
            "reddit_mentions_24h_ago": r.get("mentions_24h_ago"),
        }
        for r in results
        if SYMBOL.match(r.get("ticker", ""))
    }


def stocktwits_trending():
    symbols = _get_json("https://api.stocktwits.com/api/2/trending/symbols.json")["symbols"]
    return {s["symbol"] for s in symbols if s.get("instrument_class") == "Stock" and SYMBOL.match(s["symbol"])}


def yahoo_trending():
    quotes = _get_json(f"{YAHOO}/v1/finance/trending/US?count=25")["finance"]["result"][0]["quotes"]
    return {q["symbol"] for q in quotes if SYMBOL.match(q["symbol"])}


def yahoo_screener(screen_id, count=25):
    url = f"{YAHOO}/v1/finance/screener/predefined/saved?scrIds={screen_id}&count={count}"
    quotes = _get_json(url)["finance"]["result"][0]["quotes"]
    return {q["symbol"]: _quote_fields(q) for q in quotes if SYMBOL.match(q["symbol"])}


def _quote_fields(quote):
    return {"market_cap": quote.get("marketCap"), "earnings_ts": quote.get("earningsTimestamp")}


def yahoo_quotes(symbols):
    """Market cap and earnings date for any symbol. This endpoint needs a session crumb."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        _get("https://fc.yahoo.com", opener=opener, retries=0)
    except urllib.error.HTTPError:
        pass  # The page answers 404 but still sets the cookie.
    crumb = _get(f"{YAHOO}/v1/test/getcrumb", opener=opener).decode()
    quotes = {}
    for start in range(0, len(symbols), 50):
        batch = ",".join(symbols[start:start + 50])
        url = f"{YAHOO}/v7/finance/quote?symbols={batch}&crumb={urllib.parse.quote(crumb)}"
        for q in _get_json(url, opener=opener)["quoteResponse"]["result"]:
            quotes[q["symbol"]] = _quote_fields(q)
    return quotes


def yahoo_chart(symbol, range_="3mo"):
    result = _get_json(f"{YAHOO}/v8/finance/chart/{symbol}?range={range_}&interval=1d")["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    bars = [
        {"ts": ts, "close": close, "volume": volume}
        for ts, close, volume in zip(result.get("timestamp", []), quote.get("close", []), quote.get("volume", []))
    ]
    meta = result["meta"]
    # A bar still in progress has no close yet, so the latest trade stands in for it.
    if bars and bars[-1]["close"] is None:
        bars[-1]["close"] = meta.get("regularMarketPrice")
    return {"meta": meta, "bars": bars}


def yahoo_charts(symbols, workers=6):
    """Charts by symbol, plus the failures, so one bad ticker cannot stop a scan."""
    charts, failed = {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(yahoo_chart, s): s for s in symbols}
        for future in concurrent.futures.as_completed(futures):
            symbol = futures[future]
            try:
                charts[symbol] = future.result()
            except Exception:
                failed.append(symbol)
    return charts, failed


def headlines(symbol, now, max_age_days, limit=3):
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
    items = []
    for item in ET.fromstring(_get(url)).iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published = item.findtext("pubDate")
        if not title or not link.startswith("https://") or not published:
            continue
        try:
            when = parsedate_to_datetime(published)
        except (TypeError, ValueError):
            continue
        if (now - when).total_seconds() <= max_age_days * 86400:
            items.append({"title": title[:200], "link": link, "ts": int(when.timestamp())})
    items.sort(key=lambda h: h["ts"], reverse=True)
    return items[:limit]


def _sec_user_agent():
    contact = os.environ.get("SEC_CONTACT", "").strip()
    return f"AutoRobin Radar {contact}".strip()


def sec_ciks():
    data = _get_json("https://www.sec.gov/files/company_tickers.json", user_agent=_sec_user_agent())
    return {v["ticker"]: v["cik_str"] for v in data.values()}


def parse_form4(xml_bytes):
    """Open-market buys and sells from one Form 4. Awards and option exercises are ignored."""
    root = ET.fromstring(xml_bytes)
    owner = root.findtext("reportingOwner/reportingOwnerId/rptOwnerCik") or "?"
    bought = sold = 0.0
    for tx in root.iter("nonDerivativeTransaction"):
        code = tx.findtext("transactionCoding/transactionCode")
        try:
            shares = float(tx.findtext("transactionAmounts/transactionShares/value") or 0)
            price = float(tx.findtext("transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        if code == "P":
            bought += shares * price
        elif code == "S":
            sold += shares * price
    return owner, bought, sold


def insider_activity(cik, since_date, min_purchase_usd, max_filings=8):
    """Summarize Form 4 filings since a date, for one company, from SEC EDGAR."""
    agent = _sec_user_agent()
    recent = _get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", user_agent=agent)["filings"]["recent"]
    buyers = set()
    buy_value = sell_value = 0.0
    filings = 0
    for form, filed, accession, document in zip(
        recent["form"], recent["filingDate"], recent["accessionNumber"], recent["primaryDocument"]
    ):
        if filed < since_date:
            break  # EDGAR lists filings newest first.
        if form != "4" or filings >= max_filings:
            continue
        filings += 1
        name = document.split("/")[-1]
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{name}"
        time.sleep(0.15)  # SEC asks for no more than 10 requests per second.
        try:
            owner, bought, sold = parse_form4(_get(url, user_agent=agent))
        except (urllib.error.URLError, ET.ParseError):
            continue
        if bought >= min_purchase_usd:
            buyers.add(owner)
            buy_value += bought
        sell_value += sold
    return {"buyers": len(buyers), "buy_value": round(buy_value), "sell_value": round(sell_value)}
