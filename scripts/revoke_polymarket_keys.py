"""
One-off helper script to rotate Polymarket USER API credentials.

It uses the L1 `create_api_key()` method on `ClobClient`:
- Per Polymarket docs, each wallet can only have ONE active API key at a time.
- Creating a new key invalidates the old one.

Requirements:
- Run in same environment as Magnus (so PRIVATE_KEY and Python deps exist).

Usage:
    cd /home/kim/agents
    python scripts/revoke_polymarket_keys.py

Script outputs new USER_* values, which you then paste into .env.
"""

import os

from dotenv import load_dotenv
from py_clob_client.client import ClobClient


def main() -> None:
    # Load .env so PRIVATE_KEY and other variables are in environment.
    load_dotenv()

    host = "https://clob.polymarket.com"
    chain_id = 137

    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        print("❌ PRIVATE_KEY missing in environment – cannot create new API key.")
        return

    print("🚨 Rotating Polymarket USER API key via L1 create_or_derive_api_key() …")
    client = ClobClient(host=host, chain_id=chain_id, key=private_key)

    try:
        # Try first with create_or_derive_api_key (fetches existing or creates new),
        # fallback to create_api_key if older client version.
        if hasattr(client, "create_or_derive_api_key"):
            api_creds = client.create_or_derive_api_key()
        else:
            api_creds = client.create_api_key()
    except Exception as e:
        print(f"❌ create_api_key() failed: {e}")
        return

    # api_creds can be an object or dict – handle both.
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
        print(f"❌ Got unexpected response from create_api_key(): {api_creds!r}")
        return

    print("\n✅ New USER API credentials created.\n")
    print("⚠️  IMPORTANT: Update your .env with these values and discard the old ones:\n")
    print(f"USER_API_KEY={key}")
    print(f"USER_SECRET={secret}")
    print(f"USER_PASSPHRASE={passphrase}\n")
    print("After updating .env, restart Magnus so it uses the new keys.")


if __name__ == "__main__":
    main()

