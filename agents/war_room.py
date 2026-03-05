import os
import asyncio
import httpx
import re
from datetime import datetime, timezone
from dotenv import load_dotenv

class MagnusWarRoom:
    SHARED_GOAL = (
        "We grow capital by buying low and selling high before resolution. "
        "We only enter when there is a realistic path to selling at a higher price than we paid (net profit); otherwise we do not trade. "
        "We pursue EDGE in ALL categories – Sports, Crypto, Politics, Weather, Business, Tech, Earnings, Geopolitics, etc. "
        "The CATALYST is whatever can move the price in that category: news, data, forecast, event, report, or sentiment. "
        "We focus on VOLATILE markets (price has moved up and down). "
        "We use PRICE PATTERNS – where price sits in its historical range (high/low/avg) – to TIME entry: "
        "buy when price is relatively LOW in the range, sell when it has moved UP toward our target."
    )
    LAWYER_RULES_MAX_LEN = 500  # Trunkera rules för att spara tokens; titel + inledande text räcker för PASS/FAIL

    # Kort hint per kategori: vad som räknas som katalysator – används i Scout för tydlig analysgrund
    CATALYST_HINTS = {
        "Sports": "Catalyst = injury/lineup news, result, form; use LIVE RESEARCH for recent reports.",
        "Crypto": "Catalyst = price move, meme/sentiment, ETF/regulation news; use LIVE RESEARCH for momentum.",
        "Politics": "Catalyst = official statement, vote, ruling; use LIVE RESEARCH for breaking news.",
        "Geopolitics": "Catalyst = statement from leaders, military/UN; use LIVE RESEARCH for developments.",
        "Pop Culture": "Catalyst = nomination, release, award; use LIVE RESEARCH for announcements.",
        "Culture": "Same as Pop Culture: official/fan news; use LIVE RESEARCH.",
        "Business": "Catalyst = M&A, CEO, earnings, regulator; use LIVE RESEARCH for company news.",
        "Economics": "Catalyst = Fed/ECB decision, jobs/inflation data; use LIVE RESEARCH for timing.",
        "Tech": "Catalyst = product/earnings/regulator; use LIVE RESEARCH for milestones.",
        "Weather": "Catalyst = forecast (e.g. Open-Meteo in LIVE RESEARCH); use it to judge outcome likelihood.",
        "Trump": "Catalyst = court ruling, election result, official statement; use LIVE RESEARCH.",
        "Elections": "Catalyst = result, exit poll, authority; use LIVE RESEARCH for updates.",
        "World": "Catalyst = official/UN event; use LIVE RESEARCH.",
        "Earnings": "Catalyst = report date, guidance, consensus; use LIVE RESEARCH for dates.",
        "Mentions": "Catalyst = speech/mention schedule; use LIVE RESEARCH for transcripts.",
        "Unknown": "Catalyst = whatever can move price for this market; use LIVE RESEARCH to find it.",
    }

    # Extra sökord per kategori för Tavily/NewsAPI – ger mer relevant research
    CATEGORY_SEARCH_HINTS = {
        "Sports": "news injury lineup result",
        "Crypto": "price sentiment",
        "Earnings": "earnings report date",
        "Economics": "Fed data release",
        "Geopolitics": "news development",
        "Weather": "forecast",  # Open-Meteo används separat; Tavily som komplement
    }

    def __init__(self):
        load_dotenv()
        self.skip_lawyer = os.getenv("MAGNUS_SKIP_LAWYER", "").strip().lower() in ("1", "true", "yes")
        self.xai_key = os.getenv("XAI_API_KEY")
        self.ds_key = os.getenv("DEEPSEEK_API_KEY")
        self.claude_key = os.getenv("ANTHROPIC_API_KEY")
        self.tavily_key = os.getenv("TAVILY_API_KEY", "").strip()
        self.newsapi_key = os.getenv("NEWSAPI_API_KEY", "").strip()

        self.model_bouncer = os.getenv("MAGNUS_MODEL_BOUNCER", "grok-3-mini")
        self.model_scout = os.getenv("MAGNUS_MODEL_SCOUT", "grok-4-1-fast-non-reasoning")
        self.model_lawyer = os.getenv("MAGNUS_MODEL_LAWYER", "claude-sonnet-4-20250514")
        self.model_quant = os.getenv("MAGNUS_MODEL_QUANT", "deepseek-reasoner")

    async def _fetch_tavily(self, query: str, max_results: int = 5) -> str:
        """Fetches web/news from Tavily. Empty on error or missing key."""
        if not self.tavily_key:
            return ""
        q = (query or "")[:300].strip()
        if not q:
            return ""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": self.tavily_key,
                        "query": q,
                        "search_depth": "basic",
                        "max_results": max_results,
                        "topic": "news",
                        "include_answer": False,
                    },
                    timeout=12.0,
                )
                if resp.status_code != 200:
                    return ""
                data = resp.json()
                results = data.get("results") or []
                lines = []
                for r in results[:max_results]:
                    title = (r.get("title") or "")[:120]
                    content = (r.get("content") or "")[:400]
                    if title or content:
                        lines.append(f"- {title}\n  {content}")
                return "\n".join(lines) if lines else ""
        except Exception:
            return ""

    async def _fetch_newsapi(self, query: str, max_results: int = 5) -> str:
        """Fetches articles from NewsAPI. Empty on error or missing key."""
        if not self.newsapi_key:
            return ""
        q = (query or "")[:200].strip().replace("?", " ").replace("[", " ").replace("]", " ")
        if not q:
            return ""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://newsapi.org/v2/everything",
                    params={"q": q, "apiKey": self.newsapi_key, "pageSize": max_results, "language": "en", "sortBy": "relevancy"},
                    timeout=10.0,
                )
                if resp.status_code != 200:
                    return ""
                data = resp.json()
                articles = data.get("articles") or []
                lines = []
                for a in articles[:max_results]:
                    title = (a.get("title") or "")[:120]
                    desc = (a.get("description") or "")[:300]
                    if title or desc:
                        lines.append(f"- {title}\n  {desc}")
                return "\n".join(lines) if lines else ""
        except Exception:
            return ""

    def _is_weather_market(self, question: str, category: str) -> bool:
        """True if this market is about weather/temperature (so we can fetch forecast as catalyst)."""
        if category == "Weather":
            return True
        q = (question or "").lower()
        return any(
            kw in q for kw in ("temperature", "temp ", "weather", "°c", "°f", "degrees", "highest temp", "lowest temp")
        )

    def _parse_weather_location_and_date(self, question: str, end_date: str | None) -> tuple[str | None, str | None]:
        """Extract location and date for weather forecast. Returns (location, yyyy-mm-dd) or (None, None)."""
        q = (question or "").strip()
        location = None
        if " in " in q:
            part = q.split(" in ", 1)[1]
            location = part.split(" on ")[0].split("?")[0].strip()
            if not location or len(location) > 50:
                location = None
        if not location:
            return None, None
        # Prefer end_date (e.g. 2025-03-06); else try "March 6" in question
        target = None
        if end_date and re.match(r"\d{4}-\d{2}-\d{2}", end_date):
            target = end_date[:10]
        if not target:
            months = {"january": "01", "february": "02", "march": "03", "april": "04", "may": "05", "june": "06",
                      "july": "07", "august": "08", "september": "09", "october": "10", "november": "11", "december": "12"}
            ql = q.lower()
            for name, num in months.items():
                if name in ql:
                    m = re.search(rf"{name}\s+(\d{{1,2}})", ql)
                    if m:
                        day = m.group(1).zfill(2)
                        y = datetime.now(timezone.utc).year
                        target = f"{y}-{num}-{day}"
                    break
        return location, target

    async def _fetch_weather_forecast(self, question: str, end_date: str | None) -> str:
        """Fetches weather forecast from Open-Meteo (free, no key) for location/date. Empty on error."""
        location, target_date = self._parse_weather_location_and_date(question, end_date)
        if not location or not target_date:
            return ""
        try:
            async with httpx.AsyncClient() as client:
                geo = await client.get(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={"name": location, "count": 1},
                    timeout=5.0,
                )
                if geo.status_code != 200 or not (geo_json := geo.json()):
                    return ""
                results = geo_json.get("results") or []
                if not results:
                    return ""
                lat = results[0].get("latitude")
                lon = results[0].get("longitude")
                if lat is None or lon is None:
                    return ""
                fc = await client.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={
                        "latitude": lat, "longitude": lon,
                        "daily": "temperature_2m_max,temperature_2m_min",
                        "timezone": "auto",
                    },
                    timeout=5.0,
                )
                if fc.status_code != 200 or not (fc_json := fc.json()):
                    return ""
                daily = fc_json.get("daily") or {}
                times = daily.get("time") or []
                try:
                    idx = times.index(target_date)
                except ValueError:
                    idx = 0 if times else -1
                if idx < 0:
                    return ""
                max_t = daily.get("temperature_2m_max")
                min_t = daily.get("temperature_2m_min")
                max_val = max_t[idx] if isinstance(max_t, list) and len(max_t) > idx else None
                min_val = min_t[idx] if isinstance(min_t, list) and len(min_t) > idx else None
                if max_val is None and min_val is None:
                    return ""
                parts = [f"Weather forecast for {location} on {target_date} (Open-Meteo):"]
                if max_val is not None:
                    parts.append(f" max {float(max_val):.0f}°C")
                if min_val is not None:
                    parts.append(f" min {float(min_val):.0f}°C")
                return "".join(parts).strip()
        except Exception:
            return ""

    async def _fetch_research_snippet(self, question: str, category: str, end_date: str | None = None) -> str:
        """Runs Tavily, NewsAPI and (for weather markets) Open-Meteo forecast; combines into snippet for Scout."""
        query = (question or "").strip()[:300]
        if not query:
            return ""
        # Kategori-medveten query: lägg till sökord som ger mer relevant research
        hint = self.CATEGORY_SEARCH_HINTS.get(category, "")
        search_query = f"{query} {hint}".strip() if hint else query
        tavily_task = self._fetch_tavily(search_query, max_results=4)
        news_task = self._fetch_newsapi(search_query, max_results=4)
        if self._is_weather_market(question, category):
            tavily_text, news_text, weather_text = await asyncio.gather(
                tavily_task, news_task, self._fetch_weather_forecast(question, end_date)
            )
        else:
            tavily_text, news_text = await asyncio.gather(tavily_task, news_task)
            weather_text = ""
        parts = []
        if weather_text:
            parts.append("Weather (Open-Meteo forecast – use as catalyst for price move):\n" + weather_text)
        if tavily_text:
            parts.append("Web/News (Tavily):\n" + tavily_text)
        if news_text:
            parts.append("News (NewsAPI):\n" + news_text)
        return "\n\n".join(parts) if parts else ""

    async def _grok_bouncer(self, question: str, end_date: str, category: str = "Unknown") -> bool:
        """Step 1: Gatekeeper. Category-specific time horizon check."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        url = "https://api.x.ai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.xai_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.model_bouncer,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"You are a time gatekeeper. Today: {today}. We buy low and sell high BEFORE resolution; we do not hold to expiry.\n"
                        "We trade ALL categories (Sports, Crypto, Politics, Weather, Business, Tech, Earnings, Geopolitics, etc.) when there is edge.\n"
                        "Rule: PASS so the market can reach Scout + Quant. Only FAIL if there is clearly NO time left (resolution in the past or in under ~12 hours).\n"
                        "For ANY category: PASS if end date is at least ~12 hours away. When borderline: PASS (Quant decides).\n"
                        "Do NOT FAIL for 'uncertainty' or because resolution is far in the future. Only FAIL when time-to-resolution is clearly too short to trade.\n"
                        "Reply ONLY with exactly: PASS or FAIL"
                    )
                },
                {"role": "user", "content": f"Category: {category}. Market: {question}. End date/resolution: {end_date}."}
            ],
            "temperature": 0.1
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, headers=headers, json=payload, timeout=12.0)
                if resp.status_code != 200:
                    body = resp.text
                    if resp.status_code == 429 or "rate limit" in (body or "").lower() or "insufficient" in (body or "").lower():
                        print("   ⚠️ Bouncer API: rate limit eller slut på krediter (Grok). PASS för att inte blocka.")
                    return False
                return "PASS" in (resp.json() or {}).get("choices", [{}])[0].get("message", {}).get("content", "").strip().upper()
        except httpx.TimeoutException:
            print("   ⚠️ Bouncer API: timeout.")
            return False
        except Exception:
            return False

    async def _claude_lawyer(self, question: str, rules: str, category: str = "Unknown") -> dict:
        """Step 2: Lawyer. Lätt regelkoll: tradable + tydlig resolution. Returnerar {passed, criteria_summary}."""
        rules_short = (rules or "").strip()[: self.LAWYER_RULES_MAX_LEN]
        if (rules or "").strip() and len((rules or "").strip()) > self.LAWYER_RULES_MAX_LEN:
            rules_short = rules_short.rstrip() + "…"
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": self.claude_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        payload = {
            "model": self.model_lawyer,
            "max_tokens": 80,
            "messages": [{"role": "user", "content": (
                f"Our goal: {MagnusWarRoom.SHARED_GOAL}\n\n"
                "Your role: quick check if this market is tradable and has clear resolution (we buy low, sell high before resolution; we do not hold to outcome).\n\n"
                f"Category: {category}\n"
                f"Market: {question}\n\n"
                f"Rules (excerpt): {rules_short}\n\n"
                "PASS if the question and rules are clear enough for the market to be tradable and to resolve without dispute. "
                "FAIL only if rules are clearly vague, resolution is purely subjective with no source, or the market is known for manipulation. When in doubt: PASS (Quant decides).\n\n"
                "Reply with:\nPASS or FAIL\nIf PASS, on the next line: CRITERIA: [One line: what kind of catalyst/event drives the price, e.g. official statement, match result, closing price on exchange X.]"
            )}]
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, headers=headers, json=payload, timeout=20.0)
                if resp.status_code != 200:
                    body = (resp.text or "").lower()
                    if resp.status_code == 429 or "rate limit" in body or "overloaded" in body or "insufficient" in body:
                        print("   ⚠️ Lawyer API: rate limit eller slut på krediter (Claude). FAIL för denna kandidat.")
                    return {"passed": False, "criteria_summary": ""}
                data = resp.json()
                text = (data.get("content") or [{}])[0].get("text", "").strip()
                passed = "PASS" in text.upper()
                criteria_summary = ""
                if passed:
                    key = "CRITERIA:" if "CRITERIA:" in text.upper() else "KRITERIER:" if "KRITERIER:" in text.upper() else None
                    if key:
                        idx = text.upper().find(key)
                        if idx >= 0:
                            criteria_summary = text[idx + len(key):].strip().split("\n")[0][:400]
                return {"passed": passed, "criteria_summary": criteria_summary}
        except httpx.TimeoutException:
            print("   ⚠️ Lawyer API: timeout.")
            return {"passed": False, "criteria_summary": ""}
        except Exception:
            return {"passed": False, "criteria_summary": ""}

    # Scout: category profiles (news, X, history)
    SPANAR_PROFILES = {
        "Sports": (
            "You are a specialist scout for SPORTS. Sources: news on lineups, injury reports (questionable/out/doubtful), "
            "away stats, fixture density (fatigue), head-to-head and recent results. X: official clubs, sports journalists, insiders. "
            "SCORE 1–10: How likely is it that this market can make us money (e.g. injury/form edge that drives the price up)? "
            "Reply format: SCORE: [1-10] | INFO: [Your findings]"
        ),
        "Crypto": (
            "You are a specialist scout for CRYPTO. Sources: price data (CoinGecko/CoinMarketCap), on-chain (whale movements, exchange flows), "
            "X: trending memes, sentiment, 'vibe check', inflection dates for ETF/regulation. History: recent highs/lows, volume. "
            "SCORE 1–10: How likely is it that this market can make us money (momentum/catalyst that drives the price up)? "
            "Reply format: SCORE: [1-10] | INFO: [Your findings]"
        ),
        "Politics": (
            "You are a specialist scout for POLITICS. Sources: official statements, election authorities, voting results, breaking news. "
            "X: verified accounts for politicians and news organisations, direct quotes. Ignore speculation and opinion. "
            "SCORE 1–10: How likely is it that this market can make us money (clear event that drives the price)? "
            "Reply format: SCORE: [1-10] | INFO: [Your findings]"
        ),
        "Geopolitics": (
            "You are a specialist scout for GEOPOLITICS. Sources: statements from heads of state, military sources, UN/MSF. "
            "X: rumours and reports not yet in mainstream media, local sources. History: past escalations, agreements. "
            "SCORE 1–10: How likely is it that this market can make us money (news/catalyst that drives the price)? "
            "Reply format: SCORE: [1-10] | INFO: [Your findings]"
        ),
        "Pop Culture": (
            "You are a specialist scout for POP CULTURE. Sources: official announcements, award juries (Oscars, Grammys), streaming/box office. "
            "X: celebrities, fan communities, rumours about releases or winners. History: past winners, trends. "
            "SCORE 1–10: How likely is it that this market can make us money (event that drives the price up)? "
            "Reply format: SCORE: [1-10] | INFO: [Your findings]"
        ),
        "Culture": (
            "You are a specialist scout for CULTURE (entertainment, awards, music, film, TV). Same as Pop Culture: official sources, "
            "award juries, X and fan rumours. SCORE 1–10: How likely is it that this market can make us money? "
            "Reply format: SCORE: [1-10] | INFO: [Your findings]"
        ),
        "Business": (
            "You are a specialist scout for BUSINESS. Sources: company reports, news on CEO changes, M&A, IPO, regulators. "
            "X: company accounts, financial press, analysts. History: past price reactions to similar news. "
            "SCORE 1–10: How likely is it that this market can make us money (news that drives the price)? "
            "Reply format: SCORE: [1-10] | INFO: [Your findings]"
        ),
        "Economics": (
            "You are a specialist scout for ECONOMICS. Sources: central banks (Fed, ECB), BLS/statistics agencies, inflation/GDP/employment. "
            "X: Fed speakers, economics commentators. History: past decisions and market reactions. "
            "SCORE 1–10: How likely is it that this market can make us money (data/statement that drives the price)? "
            "Reply format: SCORE: [1-10] | INFO: [Your findings]"
        ),
        "Tech": (
            "You are a specialist scout for TECH. Sources: product launches, earnings, regulators (EU/US). "
            "X: companies, tech journalists, developers. History: past milestones and price reactions. "
            "SCORE 1–10: How likely is it that this market can make us money (milestone/news that drives the price)? "
            "Reply format: SCORE: [1-10] | INFO: [Your findings]"
        ),
        "Weather": (
            "You are a specialist scout for WEATHER. PRIMARY SOURCE: use the LIVE RESEARCH above – if it includes "
            "Open-Meteo (or other) forecast with max/min temperature for the relevant date and location, that IS the "
            "catalyst. Compare forecast to the market question (e.g. 'Will temp reach X°C?'): if forecast is near or "
            "above threshold, price can move up as others react. Also: official weather services, X. "
            "SCORE 1–10: How likely is it that this market can make us money (forecast/weather development that drives the price)? "
            "Reply format: SCORE: [1-10] | INFO: [Your findings]"
        ),
        "Trump": (
            "You are a specialist scout for TRUMP-RELATED MARKETS. Sources: official statements, courts, election authorities. "
            "X: verified accounts, news organisations. Focus on factual events (rulings, nominations, election results). "
            "SCORE 1–10: How likely is it that this market can make us money (clear event that drives the price)? "
            "Reply format: SCORE: [1-10] | INFO: [Your findings]"
        ),
        "Elections": (
            "You are a specialist scout for ELECTIONS (global). Sources: election authorities, official results, exit polls where reliable. "
            "X: local journalists and verified accounts. History: past elections in the same region. "
            "SCORE 1–10: How likely is it that this market can make us money (election development that drives the price)? "
            "Reply format: SCORE: [1-10] | INFO: [Your findings]"
        ),
        "World": (
            "You are a specialist scout for WORLD (global leaders, events). Sources: official sources, UN, local authorities. "
            "X: reliable news accounts and insiders. History: similar events. "
            "SCORE 1–10: How likely is it that this market can make us money (event that drives the price)? "
            "Reply format: SCORE: [1-10] | INFO: [Your findings]"
        ),
        "Earnings": (
            "You are a specialist scout for EARNINGS (quarterly reports). Sources: company IR, consensus estimates, past reports. "
            "X: analysts, financial press. History: beat/miss and price reactions. "
            "SCORE 1–10: How likely is it that this market can make us money (report/guidance that drives the price)? "
            "Reply format: SCORE: [1-10] | INFO: [Your findings]"
        ),
        "Mentions": (
            "You are a specialist scout for MARKETS BASED ON MENTIONS (Fed, speeches, video milestones). Sources: official transcripts, "
            "schedules, historical frequency. X: accounts that report speeches/mentions. "
            "SCORE 1–10: How likely is it that this market can make us money (mention/event that drives the price)? "
            "Reply format: SCORE: [1-10] | INFO: [Your findings]"
        ),
    }

    async def _grok_radar(self, question: str, category: str, research_snippet: str = "") -> dict:
        """Step 3: Scout. Category-specific profiles; research_snippet = live Tavily/NewsAPI data."""
        url = "https://api.x.ai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.xai_key}", "Content-Type": "application/json"}
        profile = self.SPANAR_PROFILES.get(category) or (
            "You are a specialist scout. Use news, X and available history to assess the market. "
            "SCORE 1–10: How likely is it that this market can make us money (catalyst that drives the price up)? "
            "Reply format: SCORE: [1-10] | INFO: [Your findings]"
        )
        catalyst_hint = self.CATALYST_HINTS.get(category, self.CATALYST_HINTS["Unknown"])
        user_content = ""
        if research_snippet and research_snippet.strip():
            user_content = (
                "LIVE RESEARCH (recent web, news, or forecast – primary basis for your assessment):\n"
                f"{research_snippet.strip()[:2000]}\n\n"
            )
        user_content += (
            f"ANALYSIS FOUNDATION for this category: {catalyst_hint}\n\n"
            f"Analyse the current situation for: {question}. "
            "Is there anything that can drive the price UP so we can buy now and sell at a higher price later (net profit)? "
            "We do not need to believe the outcome will be true – only that the price has potential to move up. Use the LIVE RESEARCH above and the analysis foundation."
        )
        payload = {
            "model": self.model_scout,
            "messages": [
                {"role": "system", "content": (
                    f"Our goal: {self.SHARED_GOAL} "
                    "Your role: information scout. We must sell at a profit (price higher than we paid); only score high if you see a realistic path for the price to move UP so we can sell dear. "
                    "We prioritise markets that show volatility (price has moved up and down); your findings help us decide if there are catalysts to time our entry. "
                    f"{profile}"
                )},
                {"role": "user", "content": user_content}
            ],
            "temperature": 0.3
        }
        default_out = {"score": 5, "summary": "No scout data (API error or timeout)."}
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, headers=headers, json=payload, timeout=20.0)
                if resp.status_code != 200:
                    body = (resp.text or "").lower()
                    if resp.status_code == 429 or "rate limit" in body or "insufficient" in body:
                        print("   ⚠️ Scout API: rate limit eller slut på krediter (Grok). Använder score 5.")
                    return default_out
                text = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                score, summary = 5, ""
                if "SCORE:" in text:
                    parts = text.split("SCORE:", 1)[1].strip()
                    num_part = parts.split("|")[0].strip()
                    try:
                        score = min(10, max(1, int(re.search(r"\d+", num_part).group())))
                    except (ValueError, AttributeError):
                        pass
                if "INFO:" in text:
                    summary = text.split("INFO:", 1)[1].strip()[:500]
                if not summary:
                    summary = text[:500] if text else default_out["summary"]
                return {"score": score, "summary": summary}
        except httpx.TimeoutException:
            print("   ⚠️ Scout API: timeout.")
            return default_out
        except Exception:
            return default_out

    def _format_price_context(self, price_context: dict, price: float) -> str:
        """Formats price context for prompt: position in range, volatility, near-low signals."""
        if not price_context:
            return ""
        pc = price_context
        lines = [
            f"Price now {price} is {pc.get('price_vs_avg', '?')} (history: high={pc.get('high')} low={pc.get('low')} avg={pc.get('avg')}).",
            f"Historical range: {pc.get('range_pct', 0)}% (high volatility = larger move potential).",
        ]
        if pc.get("in_lower_half"):
            lines.append("Price is in the LOWER half of the range – has potentially been this low before and moved up.")
        if pc.get("near_historical_low"):
            lines.append("Price is NEAR historical lows – possible 'bottom' situation if a catalyst exists.")
        if pc.get("change_1h") is not None:
            lines.append(f"Change in the last hour: {pc.get('change_1h')}%.")
        if (pc.get("range_pct") or 0) == 0 and pc.get("high") == pc.get("low"):
            lines.append("No historical range data – base entry on price vs value and catalyst only.")
        else:
            lines.append("Use this to judge if we are at a relative low in the range (good entry) or near the top (avoid).")
        return " ".join(lines)

    # Quant: category-specific logic – can price move up? tradability
    LOGIC_ENGINE = {
        "Sports": "Assess fatigue and injuries: can the price move up (e.g. injury/form edge)? Require large edge if key players are missing.",
        "Crypto": "Combine trend and momentum. Can the price move up (memes, catalyst)? Hype 9+ allows higher deviation; otherwise require clear discount.",
        "Geopolitics": "Focus on whether news/catalyst can drive the price up (we sell before resolution). Require a plausible catalyst; if none, REJECT.",
        "Politics": "Can news/catalyst drive the price up? REJECT only if the market clearly moves without news (manipulation).",
        "Pop Culture": "Can the price move up (nominations, rumours, releases)? Require clear catalyst; avoid pure guesswork.",
        "Culture": "Same as Pop Culture: can official sources/fan rumours drive the price? Require edge vs average.",
        "Business": "Can reports/M&A/CEO news drive the price up? Require concrete news that is not already priced in.",
        "Economics": "Can Fed/data drive the price? Prefer clear timing and source; REJECT only on clearly vague macro questions.",
        "Tech": "Can milestone/earnings drive the price up? Prefer date; avoid long-term speculation.",
        "Weather": "Weather FORECAST (e.g. Open-Meteo in research/Scout) IS the catalyst: use forecast max/min to judge if outcome is likely. If forecast supports the outcome (e.g. forecast temp near or above threshold), BUY when price is below that probability; only REJECT if forecast clearly contradicts or no forecast data.",
        "Trump": "Can factual events (rulings, elections) drive the price up? REJECT only on pure speculation with no catalyst.",
        "Elections": "Can election development/exit polls drive the price? Require clear resolution and source.",
        "World": "Can a concrete event (official sources, UN) drive the price up?",
        "Earnings": "Can report/guidance drive the price? Prefer date and company; REJECT only if clearly unclear.",
        "Mentions": "Can Fed/speech/video drive the price? Prefer schedules and frequency; REJECT only on clearly vague mentions.",
        "Unknown": "Same as other categories: identify what can move the price (news, data, event). Use Scout output and LIVE RESEARCH as catalyst. BUY when price below value and plausible path up; REJECT only when clearly no catalyst or no time.",
    }

    async def _deepseek_quant(self, question: str, price: float, hype_data: dict, stats: dict, category: str, similar_analyses: str = "", days_until_end: float | None = None, price_context: dict | None = None, criteria_summary: str = "", spread_pct: float | None = None, bid: float | None = None, ask: float | None = None, uncertain_market: bool = False, event_markets_context: str = "") -> dict:
        """Step 4: Quant. Uses spread, stats, context; analyses risk and makes buy decision."""
        url = "https://api.deepseek.com/chat/completions"
        headers = {"Authorization": f"Bearer {self.ds_key}", "Content-Type": "application/json"}
        specific_logic = self.LOGIC_ENGINE.get(category, self.LOGIC_ENGINE["Unknown"])
        spread_cap = 12 if uncertain_market else 15  # något generösare – Quant väger in edge

        prompt = (
            f"Our goal: {self.SHARED_GOAL} "
            "Your role: decision-maker for buy/sell timing. We trade ALL categories; the catalyst is what can move the price in that category (see LOGIC TO FOLLOW). "
            "You are the only one who outputs BUY or REJECT; you must use PRICE CONTEXT for timing.\n\n"
            "CORE PRINCIPLE: We want to buy CHEAP and sell DEAR. That means: buy CHEAPER THAN IT IS WORTH (current price lower than our assessed value), and sell at a higher price = profit. "
            "We sell before resolution; we do not need to believe the outcome will be true – only that the price can go up. "
            "BUY when the current price is meaningfully below what you judge the outcome to be worth (your MAX_PRICE) and there is a realistic path for price to move up. When in doubt: set MAX_PRICE LOWER rather than REJECT; only REJECT when there is clearly no edge or no time. We prefer to BUY when there is any plausible edge – REJECT only when there is clearly no way to sell at a profit (e.g. no catalyst, no time, or price already at ceiling).\n\n"
            "NON-NEGOTIABLE: We must sell at a profit (price strictly above our entry). Only BUY if there is a realistic path to selling at a price above entry before resolution. "
            "MAX_PRICE must be a SELLABLE level we can plausibly reach (what buyers might pay), not a theoretical fair value we may never see. "
            "If the only way to 'win' would be to hold to resolution, REJECT – we exit before resolution and must realise profit in the secondary market.\n\n"
            "STRATEGY: We only look at volatile markets (this one has sufficient historical range). Use STATS and PRICE CONTEXT (high, low, avg, where price sits in the range) to spot PATTERNS: BUY when price is relatively LOW in the range so we can sell when it has moved up; REJECT if price is already HIGH in the range with little upside left.\n\n"
            f"CATEGORY: {category}\n"
            f"LOGIC TO FOLLOW: {specific_logic}\n"
            "Use historical range and where the current price sits (vs low/avg/high): prefer BUY when price is in the lower part of the range or when there is a plausible catalyst; only REJECT when price is clearly at the top of the range with no upside and no catalyst.\n\n"
        )
        if uncertain_market:
            prompt += "Uncertain market: require clear edge and low spread. For Geopolitics, Crypto and Earnings: require especially clear edge in an uncertain market.\n"
        prompt += (
            f"QUESTION: {question}\n"
            f"PRICE NOW: {price} (decimal 0–1, e.g. 0.55 = 55¢)\n"
            f"STATS (high/low/avg/change_1h): {stats}\n"
        )
        if stats.get("high") == stats.get("low") and stats.get("high") is not None:
            prompt += "Note: No historical range (high=low). Do NOT REJECT solely for 'at historical high' – judge by price vs your MAX_PRICE and catalyst only.\n"
        if criteria_summary and criteria_summary.strip():
            prompt += f"LAWYER'S CRITERIA ANALYSIS: Use to assess whether there are clear catalysts that can move the price, and whether the market is tradable. Set MAX_PRICE based on how high the price can move (buyers/sentiment), not only probability that Yes wins. {criteria_summary.strip()}\n"
        if days_until_end is not None:
            prompt += f"CLOSE: Market closes in {days_until_end} days. Is there REASONABLE TIME left for the price to move up to profit before close? REJECT only if clearly no time (e.g. under 1 day with no imminent catalyst).\n"
        if price_context:
            prompt += f"PRICE CONTEXT: {self._format_price_context(price_context, price)}\n"
        if spread_pct is not None:
            b, a = bid or 0, ask or 0
            prompt += f"SPREAD: {spread_pct}% (bid={b:.3f} ask={a:.3f}). With high spread it is harder to sell at a good price later; illiquid market carries risk. REJECT if spread > {spread_cap}% unless you see a very clear edge.\n"
        prompt += f"SCOUT OUTPUT FROM GROK: {hype_data['summary']}\n"
        if event_markets_context and event_markets_context.strip():
            prompt += (
                "\nOTHER MARKETS IN THIS EVENT (research on same event – use to find the market with best profit chance):\n"
                f"{event_markets_context.strip()}\n"
                "Use this: If the crowd favours other outcomes in the event, our market may need a clear catalyst to move up – consider that, but if this market has price below value and a plausible catalyst, BUY is still OK. "
                "Prefer the market in the event with best (1) price below value and (2) likelihood buyers push price up; if another outcome clearly dominates, you may REJECT this one, but one good candidate per event is enough.\n\n"
            )
        if similar_analyses and similar_analyses.strip():
            prompt += f"\nSIMILAR PAST ANALYSES (use as reference, not as requirement):\n{similar_analyses.strip()}\n\n"
        prompt += (
            f"RULES: BUY only if (1) current price is CHEAPER THAN IT IS WORTH (current price < your MAX_PRICE with margin), AND (2) realistic path to selling at a profit (price moves up), AND (3) enough time left, AND (4) the rules are clear enough for the market to be tradable and to resolve (avoid vague/manipulated markets), AND (5) spread is acceptable (if spread > {spread_cap}% a clear edge is required; otherwise REJECT), AND (6) MAX_PRICE is a sellable level we can plausibly reach – not a hope value. "
            "REJECT if the price is not cheap, no realistic path to net profit, too little time, the market is untradable or manipulated, or spread too high without clear edge.\n"
            "MAX_PRICE must be a decimal between 0.01 and 0.99 (not percent).\n"
            "Reply exactly:\nACTION: [BUY or REJECT]\nMAX_PRICE: [number 0.01–0.99]\nREASON: [One short sentence only]"
        )
        
        timeout_sec = 90.0
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(url, headers=headers, json={"model": self.model_quant, "messages": [{"role": "user", "content": prompt}]}, timeout=timeout_sec)
                    body = resp.json()
                    if resp.status_code >= 400 or body.get("error"):
                        err = body.get("error") or body.get("message") or resp.text or f"HTTP {resp.status_code}"
                        if isinstance(err, dict):
                            err_str = (err.get("message") or err.get("error") or str(err))[:120]
                        else:
                            err_str = (err if isinstance(err, str) else str(err))[:120]
                        if "insufficient balance" in err_str.lower() or "insufficient_balance" in err_str.lower():
                            return {"action": "REJECT", "max_price": 0.0, "reason": "Quant API: DeepSeek-kontot har slut på krediter – sätt in på platform.deepseek.com"}
                        return {"action": "REJECT", "max_price": 0.0, "reason": f"API Error: {err_str}"}
                    raw = body.get("choices", [{}])[0].get("message", {}).get("content") or ""
                    result = raw.strip()
                    # Strip markdown codeblock if model responds with ```
                    if "```" in result:
                        result = re.sub(r"^```\w*\n?", "", result)
                        result = re.sub(r"\n?```\s*$", "", result).strip()
                    action, max_price, reason = "REJECT", 0.0, "Parse error"
                    upper = result.upper()
                    if "ACTION: BUY" in upper or "ACTION:BUY" in upper:
                        action = "BUY"
                    elif "ACTION: REJECT" in upper or "ACTION:REJECT" in upper:
                        action = "REJECT"
                    for line in result.split("\n"):
                        line_strip = line.strip()
                        if re.search(r"ACTION\s*:\s*(BUY|REJECT)", line_strip, re.I):
                            action = "BUY" if re.search(r"ACTION\s*:\s*BUY", line_strip, re.I) else "REJECT"
                        if "MAX_PRICE:" in line or "MAX_PRICE" in line:
                            m = re.search(r"MAX_PRICE\s*:\s*([0-9.]+)", line, re.I)
                            if m:
                                try: max_price = float(m.group(1))
                                except ValueError: pass
                        if "REASON:" in line:
                            reason = line.split("REASON:", 1)[1].strip()[:300]
                    if reason == "Parse error" and result:
                        reason = result[:200].replace("\n", " ")
                    return {"action": action, "max_price": max_price, "reason": reason}
            except httpx.TimeoutException:
                if attempt < max_attempts - 1:
                    await asyncio.sleep(2)
                    continue
                return {"action": "REJECT", "max_price": 0.0, "reason": f"API Error: DeepSeek timeout (no response within {int(timeout_sec)}s); tried {max_attempts} times"}
            except Exception as e:
                err_msg = (str(e) or repr(e) or type(e).__name__).strip()[:80] or "Unknown error"
                return {"action": "REJECT", "max_price": 0.0, "reason": f"API Error: {err_msg}"}

    async def evaluate_market(self, market_data: dict, skip_bouncer: bool = False) -> dict:
        q, stats = market_data.get('question', 'Unknown'), market_data.get('stats', {})
        print(f"\n🧠 WAR ROOM ANALYSIS: {q}")
        
        category = market_data.get('category', 'Unknown')
        # Skip Bouncer if candidate from scanner (already passed Gatekeeper)
        if skip_bouncer:
            bouncer_ok = True
            lawyer_result = None if self.skip_lawyer else await self._claude_lawyer(q, market_data.get('rules', ''), category=category)
        else:
            if self.skip_lawyer:
                bouncer_ok = await self._grok_bouncer(q, market_data.get('end_date', 'Unknown'), category=category)
                lawyer_result = None
            else:
                bouncer_ok, lawyer_result = await asyncio.gather(
                    self._grok_bouncer(q, market_data.get('end_date', 'Unknown'), category=category),
                    self._claude_lawyer(q, market_data.get('rules', ''), category=category),
                )
        if not bouncer_ok:
            return {"action": "REJECT", "reason": "Filtered by (Gatekeeper)", "hype_score": 0}
        if not self.skip_lawyer and lawyer_result is not None and not lawyer_result.get("passed"):
            return {"action": "REJECT", "reason": "Filtered by (Lawyer)", "hype_score": 0}
        criteria_summary = (lawyer_result or {}).get("criteria_summary") or ""
        if self.skip_lawyer:
            print(f"   ⚖️ Lawyer: skipped (MAGNUS_SKIP_LAWYER)")

        # Live research (Tavily + NewsAPI + för vädermarknader: Open-Meteo-prognos) som indata till Scout
        research_snippet = await self._fetch_research_snippet(q, category, market_data.get("end_date"))
        if research_snippet:
            if self._is_weather_market(q, category) and "Open-Meteo" in research_snippet:
                print(f"   📡 Live research inkl. väderprognos (Open-Meteo) till Scout.")
            else:
                print(f"   📡 Live research (Tavily/NewsAPI) included in Scout.")

        # Scout (category-specific hype score)
        hype = await self._grok_radar(q, category, research_snippet=research_snippet)
        print(f"   🔥 Hype Score: {hype['score']}/10")

        # Quant (volatility, time left, price context)
        similar_analyses = (market_data.get("similar_analyses") or "").strip()
        days_until_end = market_data.get("days_until_end")
        price_context = market_data.get("price_context") or {}
        if similar_analyses:
            print(f"   📚 Using {len(similar_analyses.split(chr(10)))} similar analyses from history.")
        if days_until_end is not None:
            print(f"   ⏱️ Time to close: {days_until_end} days.")
        if price_context.get("price_vs_avg"):
            print(f"   📉 Price vs history: {price_context.get('price_vs_avg')} (range {price_context.get('range_pct', 0)}%)")
        spread_pct = market_data.get("spread_pct")
        if spread_pct is not None:
            print(f"   📊 Spread: {spread_pct}% (bid/ask)")
        if criteria_summary:
            print(f"   ⚖️ Lawyer criteria: {criteria_summary[:60]}…")
        if (market_data.get("event_markets_context") or "").strip():
            print(f"   📋 Event context: other markets in same event included for comparison.")
        print(f"   🧮 DeepSeek computing edge (this may take 30s)...", end="", flush=True)
        decision = await self._deepseek_quant(
            q, market_data.get('current_price', 0.5), hype, stats, category,
            similar_analyses=similar_analyses, days_until_end=days_until_end, price_context=price_context,
            criteria_summary=criteria_summary,
            spread_pct=market_data.get("spread_pct"), bid=market_data.get("bid"), ask=market_data.get("ask"),
            uncertain_market=bool(market_data.get("uncertain_market")),
            event_markets_context=(market_data.get("event_markets_context") or "").strip(),
        )
        print(" Done!")
        decision["hype_score"] = hype["score"]

        if decision['action'] == "BUY":
            print(f"   ✅ APPROVED FOR BUY! (Max price: {decision['max_price']})")
        else:
            print(f"   ❌ REJECTED: {decision['reason']}")

        return decision

    def _process_history(self, history_data: list) -> dict:
        """Computes stats from history to avoid pitfalls."""
        if not history_data: return {"high": 0, "low": 0, "avg": 0, "change_1h": 0}
        prices = [float(h['p']) for h in history_data]
        old_p = prices[-12] if len(prices) > 12 else prices[0]
        return {
            "high": round(max(prices), 3), 
            "low": round(min(prices), 3), 
            "avg": round(sum(prices)/len(prices), 3), 
            "change_1h": round(((prices[-1]-old_p)/old_p)*100, 1)
        }