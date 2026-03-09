# Magnus V4 – Optimeringsplan: Fler kandidater & maximal vinst

**Syfte:** Öka antalet marknader som når analys, samt optimera analysen och köpbesluten för maximal vinst.

---

## Del 1: Öka vad vi tar in för analys

### 1.1 Scanner – filter som blockerar

| Filter | Nuvarande | Åtgärd | Risk |
|--------|-----------|--------|------|
| **Ask-likviditet** | `min_ask = max(1, 5×pris)` – hårdkodat | Lägg till `MAGNUS_MIN_ASK_USDC` (default 0.5) och `MAGNUS_MIN_ASK_MULTIPLIER` (default 3) | FOK kan misslyckas om ask för tunn |
| **Bid-likviditet** | Relaxed: >0; annars ≥3 USDC | Du har redan 1 USDC – OK | – |
| **Dagar kvar** | Relaxed: ≥0.08 (~2h) | Sätt `MAGNUS_SCANNER_MIN_DAYS_RELAXED=0` för att släppa igenom allt | Kortlivade marknader, högre risk |
| **Ref-price** | ref_price i [0.01, 0.99] | Sätt `MAGNUS_SCANNER_REF_PRICE_FILTER=0` | Fler kandidater, inkl. extrema priser |
| **Spread** | max 95% | Redan mycket högt – OK | – |
| **Range %** | ≥ min_range_pct (0) | Redan av – OK | – |
| **Up or Down** | Hårdkodat skip | Överväg: tillåt om spread/liq OK (optional) | Brusmarknader |
| **Deduplication** | 6h TTL (21600s) | Sänk till 1–2h (`MAGNUS_DEDUP_TTL_SECONDS=3600`) | Samma marknad kan komma oftare |

**Rekommenderade .env-ändringar (Scanner):**
```env
# Fler kandidater – mjukare ask-krav (kräver kodändring för env-stöd)
# MAGNUS_SCANNER_MIN_DAYS_RELAXED=0
# MAGNUS_SCANNER_REF_PRICE_FILTER=0
# MAGNUS_DEDUP_TTL_SECONDS=3600
```

### 1.2 Kodändringar för Scanner

1. **Ask-likviditet konfigurerbar** – i `scanner.py` rad 400:
   ```python
   min_ask_usdc = max(
       float(os.getenv("MAGNUS_MIN_ASK_USDC", "1.0")),
       5.0 * (best_ask or current_price or 0.01) * float(os.getenv("MAGNUS_MIN_ASK_MULTIPLIER", "1.0"))
   )
   ```
   Eller enklare: `MAGNUS_MIN_ASK_MULTIPLIER=0.6` → effektivt 3×pris istället för 5×.

2. **Event limit** – du har redan 2500. Öka till 3000–5000 om API tillåter.

---

## Del 2: Optimera analysen för fler BUY

### 2.1 War Room – vad som driver REJECT

| Stadium | Vad som blockerar | Åtgärd |
|---------|-------------------|--------|
| **Bouncer** | <12h kvar, resolution i det förflutna | Skippas redan i scanner (`MAGNUS_SKIP_BOUNCER_IN_SCANNER=1`) |
| **Lawyer** | Oklara regler, manipulation | Skippas redan (`MAGNUS_SKIP_LAWYER=1`) |
| **Quant** | Ingen edge, för lite tid, spread > cap, pris vid taket | – |
| **Fallback-heuristik** | hype≥6, in_lower_half/near_low, range≥3%, days≥0.5, spread OK | Justera trösklar |

### 2.2 Quant – prompt & spread_cap

- **Spread_cap:** 20–25% (uncertain) / 28–35% (normal). Högre = färre REJECT pga spread.
- **Prompt:** Redan "prefer BUY with conservative MAX_PRICE" – bra.
- **Åtgärd:** Lägg till env `MAGNUS_QUANT_SPREAD_CAP` (default 35) så vi kan höja till 40–45 för fler BUY.

### 2.3 Fallback-heuristik (trade.py rad 556–575)

Nuvarande: hype≥6, in_lower_half/near_low, range≥3%, days≥0.5.

**Åtgärd:** Gör trösklar konfigurerbara:
```env
MAGNUS_FALLBACK_HYPE_MIN=5
MAGNUS_FALLBACK_RANGE_MIN=2
MAGNUS_FALLBACK_DAYS_MIN=0.25
```

### 2.4 require_below_avg

Du har `MAGNUS_REQUIRE_BELOW_AVG=0` – bra, fler når köp.

---

## Del 3: Optimera för maximal vinst på köp

### 3.1 Kelly & insatsstorlek

| Parametrar | Nuvarande | Åtgärd |
|------------|-----------|--------|
| **Kelly-fraktion** | High-risk: 0.20 (uncertain: 0.10); Övriga: 0.30 (uncertain: 0.45) | Överväg `MAGNUS_KELLY_FRACTION` och `MAGNUS_KELLY_HIGH_RISK` för att justera |
| **min_bet_pct_balance** | 8% | OK – säkerställer minst 8% av saldo per köp |
| **max_bet_usdc** | 100 | Öka till 150–200 om saldo tillåter (`MAGNUS_MAX_BET_USDC`) |
| **min_gross_profit_usdc** | 0.05 | Sänk till 0.03 för fler små köp som fortfarande är lönsamma |

### 3.2 Dynamic target (säljpris)

`dynamic_target.py` – högre target = mer vinst per trade, men färre fills.

| Faktor | Effekt |
|--------|--------|
| **base_target_pct** | Grundmål (trade.py: profit_target) |
| **high_target_pct** | Vid fill < 0.30 (trade.py: profit_target_high = 10%) |
| **price_high_threshold** | 0.30 – under detta används high_target |

**Åtgärd:** Gör dessa env-konfigurerbara:
```env
MAGNUS_PROFIT_TARGET=0.06
MAGNUS_PROFIT_TARGET_HIGH=0.12
MAGNUS_PRICE_HIGH_THRESHOLD=0.35
```

### 3.3 MAX_PRICE från Quant

Quant sätter MAX_PRICE. Om den är för konservativ tappar vi vinst. Prompten säger redan "conservative MAX_PRICE" – vi kan lägga till: "When edge is clear, set MAX_PRICE to a level that captures most of the upside (e.g. 0.70–0.85 for strong catalysts)."

### 3.4 Stop-loss & recovery

- **stop_loss_pct:** 15% – OK.
- **Stop-loss monitor:** Dedikerad tråd kör `manage_active_trades` var 30:e sekund (`MAGNUS_STOP_LOSS_INTERVAL_SECONDS`).
- **min_hold_hours:** 2h – undvik att sälja för tidigt.
- **Recovery heuristics:** Shadow mode – kan utökas för att sälja snabbare vid momentum nedåt.

---

## Del 4: Ytterligare optimeringar

### 4.1 Batch & genomströmning

| Parametrar | Nuvarande | Åtgärd |
|------------|-----------|--------|
| **MAGNUS_WAR_ROOM_BATCH_SIZE** | 4 | Öka till 6–8 för snabbare genomströmning (fler marknader analyseras per runda) |
| **Sortering** | Billigast först | OK – prioriterar edge |
| **skip_rest vid Gatekeeper/Lawyer** | Ja | Överväg: skippa bara den marknaden, inte hela batchen (om inte redan så) |

### 4.2 Polymarket execution

| Parametrar | Nuvarande | Åtgärd |
|------------|-----------|--------|
| **MAGNUS_BUY_FOK_ONLY** | 1 | Sätt 0 för att tillåta GTC när ingen ask – fler köp på tunnare marknader |
| **price_move_tolerance** | 0.02 | Öka till 0.03 för att undvika skipp pga små prisrörelser under analys |

### 4.3 Portfolio risk

| Parametrar | Nuvarande | Åtgärd |
|------------|-----------|--------|
| **MAGNUS_MAX_CORRELATED** | 5 | Redan höjt – OK |
| **MAGNUS_MAX_DRAWDOWN_PCT** | 100 | Redan av – OK |
| **max_open_positions** | 15 | Överväg 20 om vi vill fler samtidiga bets |
| **max_positions_per_event** | 2 | OK för balanced (Sport/Crypto/Earnings: 1) |

### 4.4 Research

- **MAGNUS_SKIP_RESEARCH=1** – sparar tid, men Quant får mindre kontext. För vädermarknader: Research med Open-Meteo är viktig. Överväg: kör Research endast för Weather + Geopolitics.

---

## Del 5: Implementeringsprioritet

### Fas 1 – Snabba vinster (env + små kodändringar)

1. **Scanner:** Lägg till `MAGNUS_MIN_ASK_MULTIPLIER` (default 1.0, sätt 0.6 för mjukare).
2. **Scanner:** Lägg till `MAGNUS_MIN_ASK_USDC` (default 1.0, sätt 0.5 för fler).
3. **.env:** `MAGNUS_SCANNER_REF_PRICE_FILTER=0`, `MAGNUS_SCANNER_MIN_DAYS_RELAXED=0`.
4. **.env:** `MAGNUS_WAR_ROOM_BATCH_SIZE=6`, `MAGNUS_PRICE_MOVE_TOLERANCE=0.03`.
5. **.env:** `MAGNUS_BUY_FOK_ONLY=0` (om du vill GTC på tunnare marknader).

### Fas 2 – Analysoptimering

6. **Quant:** Env `MAGNUS_QUANT_SPREAD_CAP` (default 35, höj till 40).
7. **Fallback:** Env för hype/range/days-trösklar.
8. **Dynamic target:** Env för profit_target, profit_target_high.

### Fas 3 – Vinstoptimering

9. **Kelly:** Env för kelly_fraction.
10. **max_bet_usdc:** Env `MAGNUS_MAX_BET_USDC`.
11. **Quant-prompt:** Finjustera för mer aggressiv MAX_PRICE vid tydlig edge.

---

## Sammanfattning – alla MAGNUS_* som påverkar flödet

| Variabel | Default | Effekt |
|----------|---------|--------|
| MAGNUS_RELAX_SCANNER_FILTERS | 1 | Mjukare bid/days |
| MAGNUS_SCANNER_MIN_DAYS_RELAXED | 0.08 | Min dagar för scan |
| MAGNUS_SCANNER_REF_PRICE_FILTER | 1 | Ref-price i zon |
| MAGNUS_MIN_BID_LIQUIDITY | 3→1 | Du har 1 |
| MAGNUS_SKIP_BOUNCER_IN_SCANNER | 1 | Bouncer av |
| MAGNUS_SKIP_LAWYER | 1 | Lawyer av |
| MAGNUS_SKIP_RESEARCH | 1 | Research av |
| MAGNUS_REQUIRE_BELOW_AVG | 0 | Kräv inte under snitt |
| MAGNUS_MIN_EDGE | 0.018 | Min edge |
| MAGNUS_MIN_BET_PCT_BALANCE | 0.08 | Min 8% saldo |
| MAGNUS_MAX_BUY_PRICE | 0.6 | Max köppris |
| MAGNUS_BUY_FOK_ONLY | 1 | Endast FOK |
| MAGNUS_WAR_ROOM_BATCH_SIZE | 4 | Batchstorlek |
| MAGNUS_DEDUP_TTL_SECONDS | 3600 | Dedup TTL |
| MAGNUS_SCANNER_EVENT_LIMIT | 2500 | Events per strategy |
