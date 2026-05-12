#!/usr/bin/env python3
"""
Price Attribution Engine v2 — Multi-Factor Catalyst Analysis
When an alert triggers, fetches context from multiple sources and uses LLM to identify the catalyst.
Sources:
  - Finnhub company-news (US stocks, free tier)
  - MarketAux news (HK/A stocks, free tier 100/day)
  - Finnhub recommendation-trends (analyst consensus, US)
  - Earnings calendar proximity (existing layer 14)
  - Volume context (current vs daily average from Sina)

All code is defensive — any source failure silently degrades.
"""
import sys
import json
import time
import re
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(line_buffering=True)

# === API Keys ===
FINNHUB_KEY = "d6n74gpr01qir35jdoagd6n74gpr01qir35jdob0"
MARKETAUX_KEY = "I5ceElzzr8fOizcswE6aH9IclGgBVcI4GQ3dNnmh"
DASHSCOPE_ENV = Path.home() / ".hermes" / ".env"

# === Paths ===
EARNINGS_CACHE = Path.home() / ".hermes" / "data" / "earnings_calendar.json"


def load_dashscope_key():
    """Read DASHSCOPE_API_KEY from .env."""
    if not DASHSCOPE_ENV.exists():
        return None
    with open(DASHSCOPE_ENV) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line.startswith("DASHSCOPE_API_KEY="):
                return line.split("=", 1)[1]
    return None


# ── Source 1: Finnhub Company News (US stocks) ──
def fetch_finnhub_company_news(ticker, max_age_hours=48):
    """Fetch company-specific news from Finnhub. Only works for NA companies.
    
    Only returns articles published within max_age_hours to ensure freshness.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(hours=max_age_hours)).strftime("%Y-%m-%d")
    url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={start}&to={today}&token={FINNHUB_KEY}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return []
        items = r.json()
        if not isinstance(items, list):
            return []
        now_ts = time.time()
        results = []
        for item in items[:15]:
            headline = item.get("headline", "")
            if not headline:
                continue
            ts = item.get("datetime", 0)
            age_hours = (now_ts - ts) / 3600 if ts else 999
            if age_hours > max_age_hours:
                continue  # Skip stale articles
            results.append({
                "headline": headline,
                "body": item.get("summary", ""),
                "source": item.get("source", "Finnhub"),
                "url": item.get("url", ""),
                "datetime": ts,
                "age_hours": round(age_hours, 1),
            })
        # Sort by recency (newest first)
        results.sort(key=lambda x: x.get("datetime", 0), reverse=True)
        return results[:5]
    except Exception as e:
        print(f"  [attribution] Finnhub news error: {e}", flush=True)
        return []


# ── Source 2: MarketAux News (HK/A stocks) ──
def fetch_marketaux_news(symbol, limit=5, max_age_hours=48):
    """Fetch news by ticker symbol via MarketAux. Works globally.
    Free tier: 100 requests/day, 3 articles/request.
    
    Only returns articles published within max_age_hours to ensure freshness.
    """
    url = (
        f"https://api.marketaux.com/v1/news/all"
        f"?symbols={symbol}"
        f"&api_token={MARKETAUX_KEY}"
        f"&limit={limit}"
        f"&filter_entities=true"
        f"&language=en,zh"
    )
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
        articles = data.get("data", [])
        now_ts = time.time()
        results = []
        for item in articles[:limit]:
            pub_str = item.get("published_at", "")
            # Parse published_at (ISO format like "2026-05-11T01:29:33.000000Z")
            age_hours = 999
            if pub_str:
                try:
                    pub_str_clean = pub_str.replace("Z", "+00:00")
                    pub_dt = datetime.fromisoformat(pub_str_clean)
                    age_hours = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600
                except Exception:
                    pass
            if age_hours > max_age_hours:
                continue  # Skip stale articles
            results.append({
                "headline": item.get("title", ""),
                "body": item.get("text", "") or item.get("description", ""),
                "source": item.get("source", "MarketAux"),
                "url": item.get("url", ""),
                "datetime": pub_str,
                "age_hours": round(age_hours, 1),
            })
        results.sort(key=lambda x: x.get("age_hours", 999))
        return results[:5]
    except Exception as e:
        print(f"  [attribution] MarketAux error: {e}", flush=True)
        return []


# ── Source 2b: AKShare News (HK/A stocks) ──
def fetch_akshare_news(symbol, max_age_hours=48):
    """Fetch fresh news from AKShare (Eastmoney/Sina sources) for HK/A stocks.
    
    Only returns articles published within max_age_hours.
    """
    try:
        import akshare as ak
        # Clean symbol: remove .HK, .SH, .SZ suffixes
        clean_symbol = symbol.replace(".HK", "").replace(".SH", "").replace(".SZ", "")
        df = ak.stock_news_em(symbol=clean_symbol)
        if df is None or df.empty:
            return []
        now = datetime.now()
        results = []
        for _, row in df.iterrows():
            pub_str = str(row.get("发布时间", ""))
            title = row.get("新闻标题", "")
            if not title:
                continue
            try:
                pub_dt = datetime.strptime(pub_str, "%Y-%m-%d %H:%M:%S")
                age_hours = (now - pub_dt).total_seconds() / 3600
            except Exception:
                age_hours = 999
            if age_hours > max_age_hours:
                continue
            results.append({
                "headline": title,
                "body": row.get("新闻内容", "") or "",
                "source": "AKShare",
                "url": row.get("新闻链接", ""),
                "datetime": pub_str,
                "age_hours": round(age_hours, 1),
            })
        results.sort(key=lambda x: x.get("age_hours", 999))
        return results[:5]
    except Exception as e:
        print(f"  [attribution] AKShare news error: {e}", flush=True)
        return []


# ── Source 3: Finnhub Recommendation Trends (US stocks) ──
def fetch_analyst_consensus(ticker):
    """Fetch analyst recommendation consensus from Finnhub.
    Returns summary string like '32 Buy, 17 StrongBuy, 3 Hold, 1 Sell' or None.
    """
    url = f"https://finnhub.io/api/v1/stock/recommendation?symbol={ticker}&token={FINNHUB_KEY}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, list) or not data:
            return None
        latest = data[0]
        parts = []
        for label in ["strongBuy", "buy", "hold", "sell", "strongSell"]:
            count = latest.get(label, 0)
            if count > 0:
                parts.append(f"{count} {label}")
        return ", ".join(parts) if parts else None
    except Exception as e:
        print(f"  [attribution] Analyst consensus error: {e}", flush=True)
        return None


# ── Source 4: Earnings Calendar Proximity ──
def fetch_earnings_proximity(ticker, name_cn=""):
    """Check if stock has an earnings report within ±5 days.
    Checks both the existing earnings_calendar.json and does a Finnhub lookup.
    """
    # Try local cache first
    nearby = []
    if EARNINGS_CACHE.exists():
        try:
            data = json.loads(EARNINGS_CACHE.read_text())
            now = datetime.now()
            for entry in data:
                report_date_str = entry.get("date", "")
                symbol = entry.get("symbol", "")
                if symbol.upper() != ticker.upper():
                    continue
                try:
                    report_date = datetime.strptime(report_date_str, "%Y-%m-%d")
                    days_until = (report_date - now).days
                    if -5 <= days_until <= 5:
                        nearby.append(f"财报日期: {report_date_str} ({days_until:+d}天)")
                except:
                    pass
        except:
            pass

    # Finnhub earnings calendar fallback
    if not nearby:
        try:
            url = f"https://finnhub.io/api/v1/calendar/earnings?symbol={ticker}&token={FINNHUB_KEY}"
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                for e in data.get("earningsCalendar", []):
                    report_date_str = e.get("date", "")
                    if not report_date_str:
                        continue
                    try:
                        report_date = datetime.strptime(report_date_str, "%Y-%m-%d")
                        now = datetime.now()
                        days_until = (report_date - now).days
                        if -5 <= days_until <= 5:
                            eps_est = e.get("epsEstimated", "")
                            eps_act = e.get("epsActual", "")
                            detail = f"财报日期: {report_date_str}"
                            if eps_est:
                                detail += f" | EPS预估: {eps_est}"
                            if eps_act:
                                detail += f" | EPS实际: {eps_act}"
                            nearby.append(detail)
                    except:
                        pass
        except:
            pass

    return nearby


# ── Source 5: Sina Volume Context ──
def fetch_volume_context(sina_fields):
    """Extract volume context from Sina data.
    Returns volume ratio string or None.
    
    For rt_hk: fields[12]=volume, fields[11]=turnover
    For gb_: fields varies
    For A: fields varies
    """
    try:
        if len(sina_fields) >= 13:
            vol_str = sina_fields[12].replace(",", "")
            turnover_str = sina_fields[11].replace(",", "")
            try:
                volume = float(vol_str)
                if volume > 0:
                    return f"成交量: {volume:,.0f}股 | 成交额: ${float(turnover_str):,.0f}"
            except:
                pass
    except:
        pass
    return None


# ── Source 5b: Technical Indicators (Volume Ratio + 20d High/Low) ──
def _compute_indicators(highs, lows, volumes):
    """Compute 20d high/low and volume ratio from raw data lists."""
    if len(highs) < 20:
        return None
    last_20_h = highs[-20:]
    last_20_l = lows[-20:]
    last_20_v = volumes[-20:]
    vol_avg = sum(last_20_v) / len(last_20_v)
    today_vol = volumes[-1]
    return {
        "high_20d": float(max(last_20_h)),
        "low_20d": float(min(last_20_l)),
        "vol_20d_avg": float(vol_avg),
        "volume_ratio": float(today_vol / vol_avg) if vol_avg > 0 else None,
    }


def _tech_from_sina_a_share(code):
    """Fetch A-share daily K-line data from Sina Finance API.
    
    Returns list of (high, low, volume) tuples sorted by date, or None.
    """
    # Convert code: 600000.SH → sh600000, 300308.SZ → sz300308
    clean = code.replace(".SH", "").replace(".SZ", "")
    if ".SH" in code or (code.startswith("6") and len(clean) == 6):
        sina_sym = f"sh{clean}"
    else:
        sina_sym = f"sz{clean}"
    
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={sina_sym}&scale=240&ma=no&datalen=30"
    )
    headers = {"Referer": "https://finance.sina.com.cn"}
    r = requests.get(url, headers=headers, timeout=10)
    data = r.json()
    if not isinstance(data, list) or len(data) < 20:
        return None
    
    highs = [float(d["high"]) for d in data]
    lows = [float(d["low"]) for d in data]
    volumes = [float(d["volume"]) for d in data]
    return highs, lows, volumes


def _tech_from_yfinance_hk(code):
    """Fetch HK daily data from Yahoo Finance.
    
    Returns list of (high, low, volume) tuples sorted by date, or None.
    """
    import yfinance as yf
    clean = code.replace(".HK", "")
    ticker = yf.Ticker(f"{clean}.HK")
    hist = ticker.history(period="1mo")
    if hist is None or len(hist) < 20:
        return None
    highs = hist["High"].tolist()
    lows = hist["Low"].tolist()
    volumes = hist["Volume"].tolist()
    return highs, lows, volumes


def fetch_technical_indicators(code, market):
    """Compute volume ratio (current vs 20d avg) and 20-day high/low.
    
    Multi-source strategy:
    - A-share: Sina K-line API (primary) → AKShare (fallback)
    - HK:      Yahoo Finance (primary) → AKShare (fallback)
    - US:      AKShare (works fine) with 10s timeout guard
    
    Returns dict with volume_ratio, vol_20d_avg, high_20d, low_20d, or None on failure.
    """
    result = {"volume_ratio": None, "vol_20d_avg": None, "high_20d": None, "low_20d": None}
    prefix = f"  [attribution] tech({code})"

    try:
        # === PRIMARY SOURCES ===
        if market == "A":
            # Sina K-line API — fast, no akshare dependency
            try:
                print(f"{prefix}: trying Sina K-line...", flush=True)
                data = _tech_from_sina_a_share(code)
                if data:
                    highs, lows, volumes = data
                    ind = _compute_indicators(highs, lows, volumes)
                    if ind:
                        result.update(ind)
                        print(f"{prefix}: Sina OK (20d range: {ind['low_20d']}-{ind['high_20d']})", flush=True)
                        # Validate
                        if result["volume_ratio"] is not None or result["high_20d"] is not None:
                            return result
                        return None
                print(f"{prefix}: Sina returned insufficient data, falling back to AKShare", flush=True)
            except Exception as e:
                print(f"{prefix}: Sina failed ({e}), falling back to AKShare", flush=True)

            # Fallback: AKShare with timeout guard
            try:
                import signal
                def _timeout_handler(signum, frame):
                    raise TimeoutError("AKShare A-share call exceeded 10s")

                signal.signal(signal.SIGALRM, _timeout_handler)
                signal.alarm(10)
                import akshare as ak
                clean = code.replace(".SH", "").replace(".SZ", "").lstrip("0")
                df = ak.stock_zh_a_hist(symbol=clean, period="daily", adjust="")
                signal.alarm(0)  # cancel alarm

                if df is not None and not df.empty and len(df) >= 20:
                    date_col = '日期' if '日期' in df.columns else df.columns[0]
                    df = df.sort_values(date_col).tail(25)
                    highs = df['最高'].tolist()
                    lows = df['最低'].tolist()
                    volumes = df['成交量'].tolist()
                    ind = _compute_indicators(highs, lows, volumes)
                    if ind:
                        result.update(ind)
                        print(f"{prefix}: AKShare A-share OK", flush=True)
                        if result["volume_ratio"] is not None or result["high_20d"] is not None:
                            return result
            except TimeoutError as e:
                print(f"{prefix}: AKShare A-share timed out — {e}", flush=True)
            except Exception as e:
                print(f"{prefix}: AKShare A-share failed — {e}", flush=True)

        elif market == "HK":
            # Yahoo Finance primary
            try:
                print(f"{prefix}: trying Yahoo Finance...", flush=True)
                data = _tech_from_yfinance_hk(code)
                if data:
                    highs, lows, volumes = data
                    ind = _compute_indicators(highs, lows, volumes)
                    if ind:
                        result.update(ind)
                        print(f"{prefix}: Yahoo OK (20d range: {ind['low_20d']}-{ind['high_20d']})", flush=True)
                        if result["volume_ratio"] is not None or result["high_20d"] is not None:
                            return result
                print(f"{prefix}: Yahoo returned insufficient data, falling back to AKShare", flush=True)
            except Exception as e:
                print(f"{prefix}: Yahoo failed ({e}), falling back to AKShare", flush=True)

            # Fallback: AKShare with timeout guard
            try:
                import signal
                def _timeout_handler(signum, frame):
                    raise TimeoutError("AKShare HK call exceeded 10s")
                signal.signal(signal.SIGALRM, _timeout_handler)
                signal.alarm(10)
                import akshare as ak
                clean = code.replace(".HK", "").lstrip("0")
                df = ak.stock_hk_hist(symbol=clean, period="daily", adjust="")
                signal.alarm(0)

                if df is not None and not df.empty and len(df) >= 20:
                    date_col = '日期' if '日期' in df.columns else df.columns[0]
                    df = df.sort_values(date_col).tail(25)
                    highs = df['最高'].tolist()
                    lows = df['最低'].tolist()
                    volumes = df['成交量'].tolist()
                    ind = _compute_indicators(highs, lows, volumes)
                    if ind:
                        result.update(ind)
                        print(f"{prefix}: AKShare HK OK", flush=True)
                        if result["volume_ratio"] is not None or result["high_20d"] is not None:
                            return result
            except TimeoutError as e:
                print(f"{prefix}: AKShare HK timed out — {e}", flush=True)
            except Exception as e:
                print(f"{prefix}: AKShare HK failed — {e}", flush=True)

        else:  # US — AKShare works fine, just add timeout guard
            try:
                import signal
                def _timeout_handler(signum, frame):
                    raise TimeoutError("AKShare US call exceeded 10s")
                signal.signal(signal.SIGALRM, _timeout_handler)
                signal.alarm(10)
                import akshare as ak
                clean = code.replace(".O", "")
                df = ak.stock_us_daily(symbol=clean, adjust="")
                signal.alarm(0)

                if df is not None and not df.empty and len(df) >= 20:
                    date_col = 'date' if 'date' in df.columns else df.columns[0]
                    df = df.sort_values(date_col).tail(25)
                    high_col = next((c for c in ['high', 'High'] if c in df.columns), None)
                    low_col = next((c for c in ['low', 'Low'] if c in df.columns), None)
                    vol_col = next((c for c in ['volume', 'Volume'] if c in df.columns), None)
                    if high_col and low_col and vol_col:
                        result["high_20d"] = float(df[high_col].tail(20).max())
                        result["low_20d"] = float(df[low_col].tail(20).min())
                        vol_avg = df[vol_col].tail(20).mean()
                        result["vol_20d_avg"] = float(vol_avg)
                        today_vol = float(df[vol_col].iloc[-1])
                        result["volume_ratio"] = today_vol / vol_avg if vol_avg > 0 else None
                        print(f"{prefix}: AKShare US OK", flush=True)
                        if result["volume_ratio"] is not None or result["high_20d"] is not None:
                            return result
            except TimeoutError as e:
                print(f"{prefix}: AKShare US timed out — {e}", flush=True)
            except Exception as e:
                print(f"{prefix}: AKShare US failed — {e}", flush=True)

    except Exception as e:
        print(f"{prefix}: unexpected error — {e}", flush=True)

    print(f"{prefix}: all sources exhausted, returning None", flush=True)
    return None


# ── Source 6: Market Context (route by stock market) ──
def fetch_market_context(stock_market="US"):
    """Fetch relevant index backdrop based on the stock's market.

    A-shares → 上证指数 + 创业板指/深证成指
    HK       → 恒生指数 (HSI) + 恒生科技
    US       → S&P 500 + Nasdaq 100 (spot during hours, futures off-hours)
    """
    try:
        import yfinance as yf
    except ImportError:
        return None

    # Define market-specific indices
    if stock_market == "A":
        indices = {
            "上证指数": "000001.SS",
            "创业板指": "399006.SZ",
        }
    elif stock_market == "HK":
        indices = {
            "恒生指数": "^HSI",
            "恒生科技": "3067.HK",
        }
    else:  # US
        # Spot during market hours, futures off-hours
        et_now = datetime.now(timezone(timedelta(hours=-4)))
        hour = et_now.hour
        weekday = et_now.weekday()
        is_market_hours = (0 <= weekday <= 4) and (9 <= hour < 16)
        if is_market_hours:
            indices = {
                "S&P 500": "^GSPC",
                "Nasdaq 100": "^IXIC",
            }
        else:
            indices = {
                "S&P 500": "ES=F",
                "Nasdaq 100": "NQ=F",
            }

    result = {}
    for name, ticker in indices.items():
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d")
            if len(hist) >= 2:
                pct = (hist['Close'].iloc[-1] - hist['Close'].iloc[-2]) / hist['Close'].iloc[-2] * 100
                result[name] = round(float(pct), 1)
        except Exception:
            continue
    return result if result else None


# ── Unified Fetch ──
def fetch_attribution(ticker, name, move_pct, market="US", sina_fields=None):
    """Fetch all attribution data from all sources.
    
    FRESHNESS RULE: Only returns articles published within the last 48 hours.
    If no fresh articles are found, attribution is skipped — stale news cannot
    explain today's price movement.
    
    Args:
        ticker: Stock ticker (e.g. "MU", "6869.HK", "002353.SZ")
        name: Chinese name (e.g. "美光科技")
        move_pct: Percentage move (e.g. 8.5)
        market: "US", "HK", or "A"
        sina_fields: Raw Sina fields list for volume context
    
    Returns:
        dict with keys: articles, analyst_consensus, earnings_proximity,
                        volume_context, market_context
    """
    result = {
        "articles": [],
        "analyst_consensus": None,
        "earnings_proximity": [],
        "volume_context": None,
        "market_context": None,
        "technical_indicators": None,
    }

    # Source 1 & 2: News — with 48-hour freshness filter
    if market == "US":
        result["articles"] = fetch_finnhub_company_news(ticker, max_age_hours=48)
    elif market in ("HK", "A"):
        # Try AKShare first (fresh Chinese-language news from Eastmoney/Sina)
        ak_articles = fetch_akshare_news(ticker, max_age_hours=48)
        # Also try MarketAux (English/international sources)
        ma_articles = fetch_marketaux_news(ticker, limit=5, max_age_hours=48)
        # Merge: AKShare first (usually fresher for CN/HK), then MarketAux
        result["articles"] = ak_articles + [a for a in ma_articles if a["headline"] not in [x["headline"] for x in ak_articles]]
        result["articles"] = result["articles"][:5]  # Cap at 5 total

    # Source 3: Analyst consensus (US only, Finnhub free tier)
    if market == "US":
        result["analyst_consensus"] = fetch_analyst_consensus(ticker)

    # Source 4: Earnings proximity (check US ticker for both markets)
    base_ticker = ticker.split(".")[0] if "." in ticker else ticker
    result["earnings_proximity"] = fetch_earnings_proximity(base_ticker, name)

    # Source 5: Volume context from Sina
    if sina_fields:
        result["volume_context"] = fetch_volume_context(sina_fields)

    # Source 6: Market context (route by stock market)
    result["market_context"] = fetch_market_context(market)

    # Source 7: Technical indicators (volume ratio + 20d high/low)
    base_ticker_for_tech = ticker.split(".")[0] if "." in ticker and market == "US" else ticker
    result["technical_indicators"] = fetch_technical_indicators(base_ticker_for_tech, market)

    return result


# ── LLM Attribution ──
def llm_attribution(articles, ticker, name, move_pct,
                    analyst_consensus=None, earnings_proximity=None,
                    volume_context=None, market_context=None,
                    technical_indicators=None):
    """Use LLM to analyze price catalyst with multi-factor context."""
    if not articles and not analyst_consensus and not earnings_proximity:
        return None

    key = load_dashscope_key()
    if not key:
        return None

    # Build context
    sections = []

    # News section — include full article body for deeper analysis
    if articles:
        news_items = []
        for i, a in enumerate(articles[:5], 1):
            headline = a.get("headline", "")
            body = a.get("body", "")
            source = a.get("source", "")
            url = a.get("url", "")
            age = a.get("age_hours")
            pub_time = a.get("datetime", "")
            # Format age indicator
            if age is not None:
                age_tag = f"{age:.0f}h前" if age < 24 else f"{age/24:.1f}天前"
            else:
                age_tag = "时间未知"
            # Truncate body to avoid excessive tokens (keep first 800 chars)
            body_snippet = body[:800] if len(body) > 800 else body
            news_items.append(f"--- [{i}] [{source}] {age_tag} ---")
            news_items.append(f"标题: {headline}")
            if pub_time:
                news_items.append(f"发布时间: {pub_time}")
            if body_snippet:
                news_items.append(f"正文: {body_snippet}")
            if url:
                news_items.append(f"链接: {url}")
        sections.append(f"[新闻]\n" + "\n".join(news_items))

    if analyst_consensus:
        sections.append(f"[分析师评级]\n{analyst_consensus}")

    if earnings_proximity:
        sections.append(f"[财报]\n" + "\n".join(earnings_proximity))

    if volume_context:
        sections.append(f"[成交量]\n{volume_context}")

    if market_context:
        parts = []
        for idx, pct in market_context.items():
            sign = "+" if pct >= 0 else ""
            parts.append(f"{idx}: {sign}{pct}%")
        sections.append(f"[大盘]\n{', '.join(parts)}")

    if technical_indicators:
        tech_parts = []
        if technical_indicators.get("high_20d") is not None and technical_indicators.get("low_20d") is not None:
            tech_parts.append(f"20日区间: {technical_indicators['low_20d']} - {technical_indicators['high_20d']}")
        sections.append(f"[技术面]\n{'; '.join(tech_parts)}" if tech_parts else "")



    context_str = "\n\n".join(sections)

    prompt = f"""股票: {ticker} ({name})
今日涨跌幅: {move_pct:+.1f}%

上下文信息:
{context_str}

你是一个严谨的金融分析师。基于以上信息分析今日价格变动的主要驱动力。

⚠️ 关键规则：
- 只有48小时内的新闻才能解释今日价格变动
- 如果[大盘]数据显示大盘涨跌与该股方向一致，说明可能是板块联动；如果方向相反，说明是个股独立行情
- 如果[技术面]股价接近20日高点，可能是趋势延续
- 不要编造任何不存在于上下文中的事实
- 如果信息不足，回答"无近期催化剂"
- 必须区分「个股催化剂」和「板块/大盘联动」——如果只有大盘数据、没有个股新闻，说明是系统性行情
- **催化剂优先级**：具体产品/技术突破 > 财报 > 分析师评级调整 > 政策/行业事件 > 大盘联动 > 资金流向。不要将「融资买入」「北向资金」等通用资金数据作为主要催化剂，除非确实没有其他具体事件
- 如果存在具体产品或技术新闻（如新产品发布、技术突破、订单），那才是主要驱动力

严格按以下JSON格式输出（不要输出其他内容）：
{{
  "headline": "30字以内的核心结论，指明具体驱动因素（如'AI芯片需求激增推动'或'财报超预期带动'）",
  "detail": "150-250字的详细分析，必须包含：(1)具体催化剂：什么事件/数据驱动了今日涨跌 (2)技术位：当前价格处于20日区间的什么位置 (3)与大盘关系：是独立行情还是跟随大盘/板块。注意：如果缺乏个股新闻但大盘同方向，说明是β行情而非α。",
  "confidence": "高/中/低",
  "source_title": "最关键的一条新闻标题（≤30字），如无新闻则填'无个股催化剂'",
  "source_url": "该新闻的URL，如无则为空字符串"
}}"""

    # Try stronger models in order — attribution requires high factual accuracy
    # Primary: kimi-k2.5 on Coding Plan endpoint (strong reasoning for attribution)
    # Fallback: qwen3.6-plus → qwen-plus
    models = [
        ("kimi-k2.5", "https://coding.dashscope.aliyuncs.com/v1/chat/completions", 45),
        ("qwen3.6-plus", "https://coding.dashscope.aliyuncs.com/v1/chat/completions", 45),
        ("qwen-plus", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", 30),
    ]
    for model, endpoint, timeout_secs in models:
        try:
            r = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "你是金融分析师，擅长识别价格变动的催化剂和归因分析。"},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.3,
                    "max_tokens": 800,
                },
                timeout=timeout_secs
            )
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"].strip()
                # Try to parse JSON response
                try:
                    # Strip markdown code fences if present
                    if content.startswith("```"):
                        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                    parsed = json.loads(content)
                    return parsed
                except (json.JSONDecodeError, IndexError):
                    return content  # Return raw string if parse fails
            elif r.status_code == 429:
                continue
            else:
                print(f"  [attribution] {model} error {r.status_code}", flush=True)
                continue
        except Exception as e:
            print(f"  [attribution] {model} exception: {e}", flush=True)
            continue
    return None


if __name__ == "__main__":
    # Test mode — run attribution for a US and HK stock
    for test in [
        ("MU", "美光科技", 8.5, "US"),
        ("6869.HK", "长飞光纤光缆", 12.8, "HK"),
    ]:
        ticker, name, move, market = test
        print(f"\n=== Testing {ticker} ({name}) +{move}% ===", flush=True)
        data = fetch_attribution(ticker, name, move, market)
        print(f"  News: {len(data['articles'])} articles")
        print(f"  Analyst: {data['analyst_consensus']}")
        print(f"  Earnings: {data['earnings_proximity']}")
        print(f"  Volume: {data['volume_context']}")

        if data["articles"]:
            analysis = llm_attribution(
                data["articles"], ticker, name, move,
                analyst_consensus=data["analyst_consensus"],
                earnings_proximity=data["earnings_proximity"],
            )
            if analysis:
                print(f"  LLM: {analysis}")
            else:
                print("  LLM: skipped (no key or API error)")
        else:
            print("  No articles found — skipping LLM")
