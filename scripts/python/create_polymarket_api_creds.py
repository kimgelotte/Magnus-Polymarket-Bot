"""
Creates or derives Polymarket CLOB API keys via the SDK.

Usage (from project root):

    python -m scripts.python.create_polymarket_api_creds

Requirements in .env:
- PRIVATE_KEY              – private key for wallet that owns CLOB account
- POLYGON_SIGNATURE_TYPE   – 0=EOA, 1=POLY_PROXY (Magic/proxy), 2=Gnosis Safe (default 1)
- POLYMARKET_FUNDER_ADDRESS – (optional) funder address if using proxy/safe

Script outputs:
- apiKey
- secret
- passphrase

Copy them to:
- USER_API_KEY
- USER_API_SECRET
- USER_API_PASSPHRASE
"""

import os
import json

from dotenv import load_dotenv, find_dotenv
from py_clob_client.client import ClobClient


def main() -> None:
    # Load .env so PRIVATE_KEY etc. are in os.environ
    load_dotenv(find_dotenv(), override=True)

    private_key = os.getenv("PRIVATE_KEY", "").strip()
    if not private_key:
        raise SystemExit("PRIVATE_KEY missing in .env – cannot create API keys.")

    try:
        signature_type = int(os.getenv("POLYGON_SIGNATURE_TYPE", "2"))
    except ValueError:
        signature_type = 2
    funder_address = os.getenv("POLYMARKET_FUNDER_ADDRESS", "").strip() or None
    if signature_type == 0:
        from eth_account import Account
        funder_address = Account.from_key(private_key).address
    elif signature_type in (1, 2) and not (funder_address and funder_address.startswith("0x")):
        raise SystemExit(
            "POLYMARKET_FUNDER_ADDRESS required for signature type 1/2 (proxy). "
            "Find the address at polymarket.com/settings."
        )

    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=private_key,
        signature_type=signature_type,
        funder=funder_address,
    )

    print("🔑 Creating/deriving CLOB API credentials via L1 (PRIVATE_KEY)…")
    creds = client.create_or_derive_api_creds()

    # `creds` is normally an ApiCreds object from py_clob_client (fields: api_key, api_secret, api_passphrase).
    # For safety we handle both object and dict form.
    api_key = (
        getattr(creds, "key", None)
        or getattr(creds, "apiKey", None)
        or getattr(creds, "api_key", None)
    )
    secret = (
        getattr(creds, "secret", None)
        or getattr(creds, "apiSecret", None)
        or getattr(creds, "api_secret", None)
    )
    passphrase = (
        getattr(creds, "passphrase", None)
        or getattr(creds, "apiPassphrase", None)
        or getattr(creds, "api_passphrase", None)
    )

    if isinstance(creds, dict):
        api_key = api_key or creds.get("apiKey") or creds.get("key") or creds.get("api_key")
        secret = secret or creds.get("secret") or creds.get("apiSecret") or creds.get("api_secret")
        passphrase = passphrase or creds.get("passphrase") or creds.get("apiPassphrase") or creds.get("api_passphrase")

    if not (api_key and secret and passphrase):
        raise SystemExit(f"Failed to extract apiKey/secret/passphrase from response: {creds!r}")

    out = {
        "USER_API_KEY": api_key,
        "USER_API_SECRET": secret,
        "USER_API_PASSPHRASE": passphrase,
    }

    print("\n✅ Done – here are your CLOB API keys (L2):\n")
    print(json.dumps(out, indent=2))
    print(
        "\nCopy these values to your .env:\n"
        "  USER_API_KEY=...\n"
        "  USER_API_SECRET=...\n"
        "  USER_API_PASSPHRASE=...\n"
        "Then restart Magnus so they are used.\n"
        "NOTE: Never publish .env or these keys publicly."
    )


if __name__ == "__main__":
    main()

