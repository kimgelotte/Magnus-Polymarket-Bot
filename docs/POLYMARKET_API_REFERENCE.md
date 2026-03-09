# Polymarket API – fullständig dokumentationsöversikt

Genomgång av [Polymarket API-dokumentationen](https://docs.polymarket.com) (mars 2026).

---

## 1. API-struktur

| API | URL | Syfte |
|-----|-----|-------|
| **Gamma** | `gamma-api.polymarket.com` | Marknader, events, tags, series, kommentarer, sport, sökning, profiler |
| **Data** | `data-api.polymarket.com` | Positioner, trades, aktivitet, holders, open interest, leaderboards |
| **CLOB** | `clob.polymarket.com` | Orderbook, priser, order placement, cancellations, trading |
| **Bridge** | `bridge.polymarket.com` | Deposits, withdrawals (via fun.xyz) |

---

## 2. Autentisering (CLOB)

### L1 (Private Key)
- **Användning:** Skapa/derivera API-credentials, signera ordrar
- **Headers:** `POLY_ADDRESS` (Polygon signer address), `POLY_SIGNATURE` (EIP-712), `POLY_TIMESTAMP`, `POLY_NONCE`
- **Endpoints:** `POST /auth/api-key`, `GET /auth/derive-api-key`
- **EIP-712:** `address: signingAddress` – alltid signer (EOA)

### L2 (API Key)
- **Användning:** Placera ordrar, cancellera, heartbeat, balance, trades
- **Headers:** `POLY_ADDRESS`, `POLY_API_KEY`, `POLY_PASSPHRASE`, `POLY_SIGNATURE` (HMAC-SHA256), `POLY_TIMESTAMP`
- **OpenAPI:** `POLY_ADDRESS` = "Ethereum address associated with the API key"

### Signature Types & Funder

| Type | Värde | Beskrivning |
|------|-------|-------------|
| EOA | 0 | Standard wallet – funder = EOA |
| POLY_PROXY | 1 | Magic Link (email/Google) – funder = proxy |
| GNOSIS_SAFE | 2 | MetaMask/Privy/Turnkey – funder = proxy |

> "The wallet address displayed to the user on Polymarket.com is the proxy wallet and should be used as the funder."

---

## 3. Ordertyper

| Typ | Beteende | Användning |
|-----|----------|------------|
| **GTC** | Ligger på boken tills fylld eller avbryten | Limit orders |
| **GTD** | Aktiv till angiven expiration | Auto-expire |
| **FOK** | Fyll helt eller avbryt | Market orders |
| **FAK** | Fyll vad som finns, avbryt resten | Partial market |

- **BUY:** ange dollar amount (FOK/FAK) eller size (GTC/GTD)
- **SELL:** ange antal shares

---

## 4. Allowances

> "Before placing an order, your **funder address** must have approved the Exchange contract:"
- **BUY:** USDC.e allowance ≥ spending amount
- **SELL:** conditional token allowance ≥ selling amount

---

## 5. Order-validering (från OpenAPI)

Vid `POST /order` kan följande fel returneras:

| Fel | Betydelse |
|-----|-----------|
| `the order owner has to be the owner of the API KEY` | maker måste matcha API key owner |
| `the order signer address has to be the address of the API KEY` | signer måste matcha API key owner |
| `not enough balance / allowance` | Otillräcklig balans/allowance |
| `INVALID_ORDER_NOT_ENOUGH_BALANCE` | Samma som ovan |

---

## 6. Balance-allowance

**OpenAPI:** "The address is determined from the API key authentication **and signature type**."

**Query-parametrar:**
- `signature_type` (0, 1, 2) – "Signature type for address derivation"
- `asset_type` (COLLATERAL, CONDITIONAL)
- `token_id`

---

## 7. Heartbeat

Om ingen heartbeat inom ~10s avbryts alla öppna ordrar. Skicka var 5:e sekund.

---

## 8. Vad dokumentationen inte specificerar

1. **POLY_ADDRESS för proxy:** Ska det vara signer eller funder vid L2?
2. **API key för funder:** Hur skapas nyckel kopplad till funder när L1 alltid använder signer?
3. **Order placement + proxy:** Hur uppfyller man både "order owner" och "order signer" när maker ≠ signer?

Se [POLYMARKET_CLOB_ANALYSIS.md](./POLYMARKET_CLOB_ANALYSIS.md) för djupanalys av proxy-problematiken.

---

## 9. Källreferenser

- [Authentication](https://docs.polymarket.com/api-reference/authentication)
- [Proxy Wallet](https://docs.polymarket.com/developers/proxy-wallet)
- [Trading Overview](https://docs.polymarket.com/trading/overview)
- [Create Order](https://docs.polymarket.com/trading/orders/create)
- [Order Overview](https://docs.polymarket.com/trading/orders/overview)
- [Error Codes](https://docs.polymarket.com/resources/error-codes)
- [CLOB OpenAPI](https://docs.polymarket.com/api-spec/clob-openapi.yaml)
- [Documentation Index](https://docs.polymarket.com/llms.txt)
