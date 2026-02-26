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
        "We focus on VOLATILE markets (price has moved up and down). "
        "We use PRICE PATTERNS – where price sits in its historical range (high/low/avg) – to TIME entry: "
        "buy when price is relatively LOW in the range, sell when it has moved UP toward our target."
    )

    def __init__(self):
        load_dotenv()
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

    async def _fetch_research_snippet(self, question: str, category: str) -> str:
        """Runs Tavily and NewsAPI in parallel, combines into snippet for Scout. Empty if no keys or error."""
        query = (question or "").strip()[:300]
        if not query:
            return ""
        tavily_task = self._fetch_tavily(query, max_results=4)
        news_task = self._fetch_newsapi(query, max_results=4)
        tavily_text, news_text = await asyncio.gather(tavily_task, news_task)
        parts = []
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
                        f"Your role: time-horizon gatekeeper. Today is {today}. "
                        f"Our goal: {self.SHARED_GOAL} "
                        "Your purpose: ensure there is enough time left for the price to move and for us to sell at a higher price before resolution (net profit); without that we do not trade.\n"
                        "PREFERRED categories (high liquidity, clear catalysts – be slightly more generous with PASS): Sports, Elections, Politics.\n"
                        "HIGH-RISK categories (volatile price-level markets, vague geopolitical outcomes – require more time cushion, lean towards FAIL if tight): Crypto price-level, Geopolitics.\n"
                        "PASS: Enough time left for the price to move up and for us to sell at a profit "
                        "(Sports/Elections/Politics: even 12h+ is OK if event is imminent; Crypto price-level: at least 2 days; Geopolitics: at least 3 days; Other: within ~2 days).\n"
                        "FAIL: Too little time (we cannot reach a profitable sell price in time), or resolution too far out (too much uncertainty).\n"
                        "Reply ONLY with 'PASS' or 'FAIL'."
                    )
                },
                {"role": "user", "content": f"Category: {category}. Market: {question}. End date/resolution: {end_date}."}
            ],
            "temperature": 0.1
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, headers=headers, json=payload, timeout=12.0)
                if resp.status_code != 200: return False
                return "PASS" in resp.json()['choices'][0]['message']['content'].strip().upper()
        except Exception:
            return False

    async def _claude_lawyer(self, question: str, rules: str, category: str = "Unknown") -> dict:
        """Step 2: Lawyer. Category-specific rules/criteria analysis. Returns {passed, criteria_summary}."""
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": self.claude_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        payload = {
            "model": self.model_lawyer,
            "max_tokens": 220,
            "messages": [{"role": "user", "content": (
                f"Our goal: {MagnusWarRoom.SHARED_GOAL}\n\n"
                "Your role: assess whether this market is tradable and has clear resolution criteria. "
                "Your purpose: so we can predict what drives the price and sell at a profit before resolution; avoid markets where our buy-low/sell-high timing fails due to vague or manipulated rules.\n\n"
                f"Category: {category}\n"
                f"Market/question: {question}\n\n"
                f"Rules/criteria for the market: {rules}\n\n"
                "Task 1: Analyze the criteria carefully. Often more is required than something reaching a value – e.g. 'Bitcoin over X' may require CLOSING there, or being there 1 min, or a specific source at a specific time. Identify exactly what is required for the market to resolve to Yes (so we understand what drives the price).\n"
                "Task 2: PASS if the question and rules are clear enough for the market to be tradable and to resolve without dispute. FAIL if the rules are vague, if resolution depends on subjective judgment without a clear source, or if the market is known for manipulation.\n\n"
                "Reply with:\nPASS or FAIL\nIf PASS, on the next line write: CRITERIA: [Short summary of what is required to win. Max 2 sentences.]"
            )}]
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, headers=headers, json=payload, timeout=20.0)
                data = resp.json()
                text = data["content"][0]["text"].strip()
                passed = "PASS" in text.upper()
                criteria_summary = ""
                if passed:
                    key = "CRITERIA:" if "CRITERIA:" in text.upper() else "KRITERIER:" if "KRITERIER:" in text.upper() else None
                    if key:
                        idx = text.upper().find(key)
                        if idx >= 0:
                            criteria_summary = text[idx + len(key):].strip().split("\n")[0][:400]
                return {"passed": passed, "criteria_summary": criteria_summary}
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
            "You are a specialist scout for WEATHER. Sources: official weather services (NOAA, etc.), satellite/models. "
            "X: meteorologists, local reports. History: similar periods, season. "
            "SCORE 1–10: How likely is it that this market can make us money (weather development that drives the price)? "
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
        user_content = ""
        if research_snippet and research_snippet.strip():
            user_content = (
                "LIVE RESEARCH (recent web and news – use this if relevant):\n"
                f"{research_snippet.strip()[:2000]}\n\n"
            )
        user_content += (
            f"Analyse the current situation for: {question}. "
            "Is there anything that can drive the price UP so we can buy now and sell at a higher price later (net profit)? "
            "We do not need to believe the outcome will be true – only that the price has potential to move up. Use news, X and history (and the LIVE RESEARCH above if provided)."
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
        lines.append("Use this to judge if we are at a relative low in the range (good entry) or near the top (avoid).")
        return " ".join(lines)

    # Quant: category-specific logic – can price move up? tradability
    LOGIC_ENGINE = {
        "Sports": "Assess fatigue and injuries: can the price move up (e.g. injury/form edge)? Require large edge if key players are missing.",
        "Crypto": "Combine trend and momentum. Can the price move up (memes, catalyst)? Hype 9+ allows higher deviation; otherwise require clear discount.",
        "Geopolitics": "Focus on whether news/catalyst can drive the price up (we sell before resolution). REJECT if no clear catalyst that can move the price.",
        "Politics": "Can news/catalyst drive the price up? REJECT if the market moves without news (manipulation).",
        "Pop Culture": "Can the price move up (nominations, rumours, releases)? Require clear catalyst; avoid pure guesswork.",
        "Culture": "Same as Pop Culture: can official sources/fan rumours drive the price? Require edge vs average.",
        "Business": "Can reports/M&A/CEO news drive the price up? Require concrete news that is not already priced in.",
        "Economics": "Can Fed/data drive the price? Require clear timing and source. REJECT on vague macro questions.",
        "Tech": "Can milestone/earnings drive the price up? Require date; avoid long-term speculation.",
        "Weather": "Can weather development drive the price? Require official sources and date. REJECT on vague seasonal questions.",
        "Trump": "Can factual events (rulings, elections) drive the price up? REJECT on pure speculation.",
        "Elections": "Can election development/exit polls drive the price? Require clear resolution and source.",
        "World": "Can a concrete event (official sources, UN) drive the price up?",
        "Earnings": "Can report/guidance drive the price? Require date and company. REJECT if unclear.",
        "Mentions": "Can Fed/speech/video drive the price? Require schedules and frequency. REJECT on vague mentions.",
        "Unknown": "Require clear edge and price below value. Assess whether the price can move up (tradability).",
    }

    async def _deepseek_quant(self, question: str, price: float, hype_data: dict, stats: dict, category: str, similar_analyses: str = "", days_until_end: float | None = None, price_context: dict | None = None, criteria_summary: str = "", spread_pct: float | None = None, bid: float | None = None, ask: float | None = None, uncertain_market: bool = False, event_markets_context: str = "") -> dict:
        """Step 4: Quant. Uses spread, stats, context; analyses risk and makes buy decision."""
        url = "https://api.deepseek.com/chat/completions"
        headers = {"Authorization": f"Bearer {self.ds_key}", "Content-Type": "application/json"}
        specific_logic = self.LOGIC_ENGINE.get(category, self.LOGIC_ENGINE["Unknown"])
        spread_cap = 10 if uncertain_market else 12

        prompt = (
            f"Our goal: {self.SHARED_GOAL} "
            "Your role: decision-maker for buy/sell timing. You are the only one who outputs BUY or REJECT; you must use PRICE CONTEXT for timing.\n\n"
            "CORE PRINCIPLE: We want to buy CHEAP and sell DEAR. That means: buy CHEAPER THAN IT IS WORTH (current price lower than our assessed value), and sell at a higher price = profit. "
            "We sell before resolution; we do not need to believe the outcome will be true – only that the price can go up. "
            "BUY only if the current price is CLEARLY LOWER than what you judge the outcome to be worth (your MAX_PRICE) – otherwise we are not buying cheap and get no profit. When in doubt: REJECT.\n\n"
            "NON-NEGOTIABLE: We must sell at a profit (price strictly above our entry). Only BUY if there is a realistic path to selling at a price above entry before resolution. "
            "MAX_PRICE must be a SELLABLE level we can plausibly reach (what buyers might pay), not a theoretical fair value we may never see. "
            "If the only way to 'win' would be to hold to resolution, REJECT – we exit before resolution and must realise profit in the secondary market.\n\n"
            "STRATEGY: We only look at volatile markets (this one has sufficient historical range). Use STATS and PRICE CONTEXT (high, low, avg, where price sits in the range) to spot PATTERNS: BUY when price is relatively LOW in the range so we can sell when it has moved up; REJECT if price is already HIGH in the range with little upside left.\n\n"
            f"CATEGORY: {category}\n"
            f"LOGIC TO FOLLOW: {specific_logic}\n"
            "Use historical range and where the current price sits (vs low/avg/high): prefer BUY when price is in the lower part of the range with clear upside; REJECT when price is already high in the range.\n\n"
        )
        if uncertain_market:
            prompt += "Uncertain market: require clear edge and low spread. For Geopolitics, Crypto and Earnings: require especially clear edge in an uncertain market.\n"
        prompt += (
            f"QUESTION: {question}\n"
            f"PRICE NOW: {price} (decimal 0–1, e.g. 0.55 = 55¢)\n"
            f"STATS (high/low/avg/change_1h): {stats}\n"
        )
        if criteria_summary and criteria_summary.strip():
            prompt += f"LAWYER'S CRITERIA ANALYSIS: Use to assess whether there are clear catalysts that can move the price, and whether the market is tradable. Set MAX_PRICE based on how high the price can move (buyers/sentiment), not only probability that Yes wins. {criteria_summary.strip()}\n"
        if days_until_end is not None:
            prompt += f"CLOSE: Market closes in {days_until_end} days. Is there REASONABLE TIME left for the price to move up to profit before close? If no or < 1 day, REJECT.\n"
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
                "Use this: If the crowd is betting on other outcomes in the event and our market is the one nobody wants, price may not move up – REJECT unless there is a clear catalyst for our outcome. "
                "We want the market in the event that has the best combination of (1) price below value and (2) likelihood that buyers will push price up; if another outcome in the event clearly has better resolution chance or more flow, prefer REJECT for this one.\n\n"
            )
        if similar_analyses and similar_analyses.strip():
            prompt += f"\nSIMILAR PAST ANALYSES (use as reference, not as requirement):\n{similar_analyses.strip()}\n\n"
        prompt += (
            f"RULES: BUY only if (1) current price is CHEAPER THAN IT IS WORTH (current price < your MAX_PRICE with margin), AND (2) realistic path to selling at a profit (price moves up), AND (3) enough time left, AND (4) the rules are clear enough for the market to be tradable and to resolve (avoid vague/manipulated markets), AND (5) spread is acceptable (if spread > {spread_cap}% a clear edge is required; otherwise REJECT), AND (6) MAX_PRICE is a sellable level we can plausibly reach – not a hope value. "
            "REJECT if the price is not cheap, no realistic path to net profit, too little time, the market is untradable or manipulated, or spread too high without clear edge.\n"
            "MAX_PRICE must be a decimal between 0.01 and 0.99 (not percent).\n"
            "Reply exactly:\nACTION: [BUY or REJECT]\nMAX_PRICE: [number 0.01–0.99]\nREASON: [Short justification]"
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
                        err_str = (err if isinstance(err, str) else str(err))[:80]
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
            lawyer_result = await self._claude_lawyer(q, market_data.get('rules', ''), category=category)
        else:
            bouncer_ok, lawyer_result = await asyncio.gather(
                self._grok_bouncer(q, market_data.get('end_date', 'Unknown'), category=category),
                self._claude_lawyer(q, market_data.get('rules', ''), category=category),
            )
        if not bouncer_ok:
            return {"action": "REJECT", "reason": "Filtered by (Gatekeeper)", "hype_score": 0}
        if not lawyer_result.get("passed"):
            return {"action": "REJECT", "reason": "Filtered by (Lawyer)", "hype_score": 0}
        criteria_summary = lawyer_result.get("criteria_summary") or ""

        # Live research (Tavily + NewsAPI) as input for Scout
        research_snippet = await self._fetch_research_snippet(q, category)
        if research_snippet:
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