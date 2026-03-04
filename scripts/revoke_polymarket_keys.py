"""
One-off helper script to rotate Polymarket USER API credentials.

It uses the L1 `create_api_key()` method on `ClobClient`:
- Per Polymarket docs, varje wallet kan bara ha EN aktiv API-nyckel åt gången.
- Att skapa en ny nyckel gör den gamla ogiltig.

Krav:
- Körs i samma miljö som Magnus (så PRIVATE_KEY och Python-deps finns).

Användning:
    cd /home/kim/agents
    python scripts/revoke_polymarket_keys.py

Scriptet skriver ut nya USER_* värden, som du sedan klistrar in i .env.
"""

import os

from dotenv import load_dotenv
from py_clob_client.client import ClobClient


def main() -> None:
    # Ladda .env så PRIVATE_KEY och övriga variabler finns i environment.
    load_dotenv()

    host = "https://clob.polymarket.com"
    chain_id = 137

    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        print("❌ PRIVATE_KEY saknas i environment – kan inte skapa ny API-nyckel.")
        return

    print("🚨 Rotating Polymarket USER API key via L1 create_or_derive_api_key() …")
    client = ClobClient(host=host, chain_id=chain_id, key=private_key)

    try:
        # Försök först med create_or_derive_api_key (hämtar befintlig eller skapar ny),
        # fallback till create_api_key om äldre version av klienten.
        if hasattr(client, "create_or_derive_api_key"):
            api_creds = client.create_or_derive_api_key()
        else:
            api_creds = client.create_api_key()
    except Exception as e:
        print(f"❌ create_api_key() failed: {e}")
        return

    # api_creds kan vara ett objekt eller en dict – hantera båda.
    def _get(field: str) -> str:
        if hasattr(api_creds, field):
            return str(getattr(api_creds, field))
        if isinstance(api_creds, dict) and field in api_creds:
            return str(api_creds[field])
        return ""

    key = _get("key")
    secret = _get("secret")
    passphrase = _get("passphrase")

    if not key or not secret or not passphrase:
        print(f"❌ Fick oväntat svar från create_api_key(): {api_creds!r}")
        return

    print("\n✅ New USER API credentials created.\n")
    print("⚠️  IMPORTANT: Update your .env with these values and discard the old ones:\n")
    print(f"USER_API_KEY={key}")
    print(f"USER_SECRET={secret}")
    print(f"USER_PASSPHRASE={passphrase}\n")
    print("Efter att du uppdaterat .env, starta om Magnus så han använder de nya nycklarna.")


if __name__ == "__main__":
    main()

