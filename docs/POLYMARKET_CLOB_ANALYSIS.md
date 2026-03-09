# Polymarket CLOB – djupanalys av API och proxy-problematik

## 1. Autentiseringsmodell (officiell dokumentation)

### L1 (Private Key)
- **Syfte:** Skapa/derivera API-credentials, signera ordrar
- **Headers:** `POLY_ADDRESS` = "Polygon signer address", `POLY_SIGNATURE` (EIP-712), `POLY_TIMESTAMP`, `POLY_NONCE`
- **EIP-712-struktur:** `address: signingAddress` – alltid signer (EOA)
- **Endpoints:** `POST /auth/api-key`, `GET /auth/derive-api-key`

### L2 (API Key)
- **Syfte:** Autentisera trading-anrop (post order, cancel, heartbeat, balance)
- **Headers:** `POLY_ADDRESS` = "Polygon signer address", `POLY_API_KEY`, `POLY_PASSPHRASE`, `POLY_SIGNATURE` (HMAC), `POLY_TIMESTAMP`
- **OpenAPI:** `POLY_ADDRESS` = "Ethereum address associated with the API key"

**Dokumentationen anger konsekvent "Polygon signer address" – ingen explicit beskrivning av funder för proxy.**

---

## 2. Order-validering (från Error Codes & OpenAPI)

Vid `POST /order` kan följande fel returneras:

| Fel | Betydelse |
|-----|------------|
| `the order owner has to be the owner of the API KEY` | **maker** i ordern måste matcha adressen som API-nyckeln är kopplad till |
| `the order signer address has to be the address of the API KEY` | **signer** i ordern måste matcha adressen som API-nyckeln är kopplad till |
| `not enough balance / allowance` | Otillräcklig balans eller allowance |

### Proxy-scenario (signature_type=2)
- **Order:** `maker` = funder (proxy), `signer` = EOA
- **API-nyckel:** Skapas med L1 där `POLY_ADDRESS` = signer (EOA) → nyckeln kopplas till EOA

**Konflikt:**
- Om API-nyckeln är kopplad till **EOA**: maker (funder) ≠ API key owner → `the order owner has to be the owner of the API KEY`
- Om API-nyckeln är kopplad till **funder**: signer (EOA) ≠ API key owner → `the order signer address has to be the address of the API KEY`

Dokumentationen ger ingen tydlig vägledning för hur båda kraven ska uppfyllas samtidigt för proxy.

---

## 3. Balance-allowance

**OpenAPI-spec:**
> "The address is determined from the API key authentication **and signature type**."

**Query-parametrar:**
- `signature_type` (0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE) – "Signature type for address derivation"
- `asset_type`, `token_id`

**Tolkning:** Servern kan härleda vilken adress som ska användas för balance/allowance utifrån API-nyckel + `signature_type`. Det är dock oklart om detta gäller även för `POST /order` eller bara för `GET /balance-allowance`.

---

## 4. GitHub Issue #248 (clob-client, TypeScript)

**Beskrivning:** För Magic wallet (signature_type=1) sätts `POLY_ADDRESS` till signer i stället för funder, vilket ger 401.

**Påstående:** "The API key is associated with the **funderAddress** (Polymarket profile address)"

**Workaround:** Överskrida `POLY_ADDRESS` till funder via axios-interceptor.

**OBS:** Gäller TypeScript-klienten och Magic (type 1). Python-klienten och Gnosis Safe (type 2) är inte explicit behandlade.

---

## 5. Vad dokumentationen inte säger

1. **API-nyckel för funder:** Hur skapas/deriveras en API-nyckel kopplad till funder när L1 alltid använder signer i EIP-712?
2. **POLY_ADDRESS vid create/derive:** Kan `POLY_ADDRESS` sättas till funder vid L1-anrop, och accepterar servern det när signaturerna kommer från EOA?
3. **POST /order och balance:** Använder order-valideringen `POLY_ADDRESS` eller `maker` för balance-check?
4. **signature_type för POST /order:** Skickas `signature_type` i request body (order-objektet) – använder servern det för att härleda rätt adress för balance/allowance?

---

## 6. Slutsatser

| Fråga | Svar |
|-------|------|
| Stöder officiell dokumentation POLY_ADDRESS=funder? | **Nej** – endast "Polygon signer address" nämns |
| Kan vi med säkerhet skapa API-nyckel för funder via L1? | **Nej** – EIP-712 signerar med signer.address(); oklart om servern accepterar POLY_ADDRESS=funder |
| Är POLYMARKET_FUNDER_API_KEY en dokumenterad lösning? | **Nej** – ingen referens i Polymarket-dokumentationen |
| Vad säger balance-allowance-specen? | Adressen bestäms av "API key authentication and signature type" – möjlig koppling till proxy, men inte specificerad för order placement |

---

## 7. Rekommendation / Implementerad lösning

**Implementerad (mars 2026):** Vi patchar nu `POLY_ADDRESS` till funder för `execute_sell_order` (samma mönster som `get_open_orders`, `get_usdc_balance`). Detta löser "not enough balance" för proxy – CLOB kollar balance på rätt konto (funder). Om 401 uppstår (API key bound till EOA) behåller vi fallback: användaren kör `restore-sell-orders` eller lägger manuellt.

**Rekommenderade steg vid fortsatta problem:**
1. Kontakta Polymarket (Discord/support) och fråga explicit hur API-nycklar och POLY_ADDRESS ska hanteras för proxy (Gnosis Safe, type 2).
2. Be om dokumentation eller exempel för: create/derive API key för proxy, samt korrekt POLY_ADDRESS för POST /order.
3. Öppna eller följ upp en GitHub-issue i py-clob-client om samma problem som #248 men för type 2.
