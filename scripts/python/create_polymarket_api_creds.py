"""
Skapar eller deriverar Polymarket CLOB API‑nycklar via SDK:n.

Användning (från projektroten):

    python -m scripts.python.create_polymarket_api_creds

Krav i .env:
- PRIVATE_KEY              – privata nyckeln för walleten som äger CLOB‑kontot
- POLYGON_SIGNATURE_TYPE   – 0=EOA, 1=POLY_PROXY (Magic/proxy), 2=Gnosis Safe (default 1)
- POLYMARKET_FUNDER_ADDRESS – (valfritt) funder‑address om du använder proxy/safe

Skriptet skriver ut:
- apiKey
- secret
- passphrase

Kopiera dem sedan till:
- USER_API_KEY
- USER_API_SECRET
- USER_API_PASSPHRASE
"""

import os
import json

from dotenv import load_dotenv, find_dotenv
from py_clob_client.client import ClobClient


def main() -> None:
    # Ladda .env så PRIVATE_KEY m.fl. faktiskt finns i os.environ
    load_dotenv(find_dotenv(), override=True)

    private_key = os.getenv("PRIVATE_KEY", "").strip()
    if not private_key:
        raise SystemExit("PRIVATE_KEY saknas i .env – kan inte skapa API‑nycklar.")

    try:
        signature_type = int(os.getenv("POLYGON_SIGNATURE_TYPE", "1"))
    except ValueError:
        signature_type = 1
    funder_address = os.getenv("POLYMARKET_FUNDER_ADDRESS", "").strip() or None

    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=private_key,
        signature_type=signature_type,
        funder=funder_address,
    )

    print("🔑 Skapar/deriverar CLOB API‑credentials via L1 (PRIVATE_KEY)…")
    creds = client.create_or_derive_api_creds()

    # `creds` är normalt ett ApiCreds‑objekt från py_clob_client.
    # För säkerhets skull hanterar vi både objekt‑ och dict‑form.
    api_key = (
        getattr(creds, "key", None)
        or getattr(creds, "apiKey", None)
        or getattr(creds, "api_key", None)
    )
    secret = getattr(creds, "secret", None) or getattr(creds, "apiSecret", None)
    passphrase = getattr(creds, "passphrase", None) or getattr(creds, "apiPassphrase", None)

    if isinstance(creds, dict):
        api_key = api_key or creds.get("apiKey") or creds.get("key") or creds.get("api_key")
        secret = secret or creds.get("secret") or creds.get("apiSecret")
        passphrase = passphrase or creds.get("passphrase") or creds.get("apiPassphrase")

    if not (api_key and secret and passphrase):
        raise SystemExit(f"Misslyckades att extrahera apiKey/secret/passphrase ur svar: {creds!r}")

    out = {
        "USER_API_KEY": api_key,
        "USER_API_SECRET": secret,
        "USER_API_PASSPHRASE": passphrase,
    }

    print("\n✅ Klart – här är dina CLOB API‑nycklar (L2):\n")
    print(json.dumps(out, indent=2))
    print(
        "\nKopiera dessa värden till din .env:\n"
        "  USER_API_KEY=...\n"
        "  USER_API_SECRET=...\n"
        "  USER_API_PASSPHRASE=...\n"
        "Starta sedan om Magnus så att de används.\n"
        "OBS: Lägg aldrig upp .env eller dessa nycklar publikt."
    )


if __name__ == "__main__":
    main()

