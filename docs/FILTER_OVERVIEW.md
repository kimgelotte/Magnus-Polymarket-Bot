# Magnus V4 – Filteröversikt

Kedjan från Gamma/CLOB till faktiskt köp. Alla värden kan överstyras via `.env` där det står (env).

---

## 1. Scanner – eventnivå (innan vi tittar på token)

| Filter | Regel | Env / kod |
|--------|--------|-----------|
| Titel | Skippa om titel innehåller "up or down" | hårdkodat |
| Titel | Skippa om titel matchar `skip_title_patterns`: "elon musk" + "tweet", ev. "bitcoin" | `MAGNUS_SKIP_BITCOIN=1` |
| Event | Skippa om event har inga `markets` | – |
| Sport | Skippa sport-event om `now > startDate + 4h` | hårdkodat |
| Marknad | Skippa om vi redan har öppen position i den marknaden (`_allow_market_scan`) | – |

---

## 2. Scanner – per token (pre-filter → kön)

| Filter | Gräns | Env |
|--------|--------|-----|
| **Pris** | `min_entry_price ≤ pris ≤ max_entry_price` | `MAGNUS_MIN_ENTRY_PRICE=0.001`, `MAGNUS_MAX_ENTRY_PRICE=0.999` |
| **Likviditet** | Bid-likviditet (USDC) ≥ `min_bid_liquidity_usdc` | `MAGNUS_MIN_BID_LIQUIDITY=3.0` |
| **Spread** | Spread % ≤ `max_spread_pct` | `MAGNUS_MAX_SPREAD_PCT=95.0` |
| **Dagar kvar** | `days_until_end ≥ min_days` (beroende på kategori) | – |
| | Preferred (Sports, Elections, Politics): min 0.4 dagar | |
| | High-risk + price-event: min 1.2 dagar | |
| | High-risk: min 1.0 dag | |
| | Övriga: min 0.8 dagar | |
| **Range** | `range_pct ≥ min_range_pct` | `MAGNUS_MIN_RANGE_PCT=0` (av = kräver inte volatilitet) |
| **1h-förändring** | Om `min_change_1h_pct > 0`: kräv \|change_1h\| ≥ det värdet | `MAGNUS_MIN_CHANGE_1H_PCT` (default 0 = av) |
| **Dedup** | Samma (market_id, token_id) får inte läggas i kön igen inom TTL | `MAGNUS_DEDUP_TTL_SECONDS` |
| **Kön full** | Max 500 kandidater i kön | hårdkodat |

Pris kommer först från **Gamma `outcomePrices`** (samma som webben), annars CLOB.  
Om `MAGNUS_SKIP_BOUNCER_IN_SCANNER=1` (default) körs inte Bouncer i scannern – alla som passerar ovan läggs i kön.

---

## 3. War Room (AI) – per kandidat

| Steg | Vad som händer | Vid fail |
|------|----------------|----------|
| **Gatekeeper (Bouncer)** | Grok: är marknaden värd att analysera? (tid kvar, typ) | REJECT: "Filtered by (Gatekeeper)" |
| **Lawyer** | Claude: är reglerna tydliga, kan marknaden resolvas? | REJECT: "Filtered by (Lawyer)" |
| **Scout** | Grok: hype-score 0–10, kategorispecifik | – |
| **Quant** | DeepSeek: BUY/REJECT, MAX_PRICE, REASON (edge, tid, spread, range) | REJECT med angiven orsak |

---

## 4. Efter War Room – beslut till köp

Även om War Room säger BUY kan vi ändå skippa köp:

| Filter | Regel | Källa |
|--------|--------|--------|
| Event redan full | Sport/Crypto/Earnings: max 1 position per event. Övriga: max `max_positions_per_event` (2) | `balanced_event_categories`, `max_positions_per_event` |
| Korrelation | För många positioner i samma kategori | `PortfolioRiskManager.check_correlation`, `MAGNUS_MAX_CORRELATED` |
| Momentum | High-risk + negativ 1h-förändring &lt; -5% → skippa | hårdkodat |
| Tid kvar | `days_until_end < 0.5` → skippa | hårdkodat |
| Max positioner | Öppna positioner ≥ `max_open_positions` (15) | `max_open_positions` |
| Pris vs snitt | Om `require_below_avg`: kräv pris i nedre halvan / under snitt (default av) | `MAGNUS_REQUIRE_BELOW_AVG=0` |
| Köp när Quant BUY | Kräv bara `current_price ≤ ai_max_price` (ingen extra edge-spärr) | – |
| Kelly / bet | bet = max(Kelly, 8% saldo); cap `max_bet_usdc` (100); minst $1 och 5 andelar | `MAGNUS_MIN_BET_PCT_BALANCE`, `max_bet_usdc` |
| Pris flyttat | Skippa bara om `price_now > ai_max_price + tolerance` (default 5¢) | `MAGNUS_PRICE_MOVE_TOLERANCE=0.05` |
| Prisband vid order | `min_entry_price ≤ price_now ≤ price_cap` (price_cap 0.85 high-risk, annars max_entry_price) | `MAGNUS_MIN/MAX_ENTRY_PRICE` |

---

## Snabbreferens – env som styr filtren

| Env | Default | Effekt |
|-----|--------|--------|
| `MAGNUS_MIN_ENTRY_PRICE` | 0.001 | Min pris (0–1) för att komma in i scanner |
| `MAGNUS_MAX_ENTRY_PRICE` | 0.999 | Max pris för att komma in |
| `MAGNUS_MIN_BID_LIQUIDITY` | 3.0 | Min budlikviditet (USDC) |
| `MAGNUS_MAX_SPREAD_PCT` | 95.0 | Max spread % (bid-ask) i scannern |
| `MAGNUS_MIN_RANGE_PCT` | 0 | Min historisk range % (0 = kräv inte) |
| `MAGNUS_REQUIRE_BELOW_AVG` | 1 | Kräv pris under snitt / i nedre halvan (vid köp) |
| `MAGNUS_SKIP_BOUNCER_IN_SCANNER` | 1 | 1 = ingen Bouncer i scanner, alla som passerar pre-filter → kön |
| `MAGNUS_SKIP_BITCOIN` | 1 | 1 = skippa marknader med "bitcoin" i titel |
| `MAGNUS_REQUIRE_BELOW_AVG` | 1 | 0 = kräv inte pris under snitt (fler köp; Quant godkänner ändå) |
| `MAGNUS_MIN_EDGE` | 0.018 | Min edge (0–1) för att köpa, t.ex. 0.018 ≈ 1.8¢ |

Diagnostik i terminalen: `Filtrerade bort: price=X, liq=Y, spread=Z, days=…` visar var token stoppas i scannern.
