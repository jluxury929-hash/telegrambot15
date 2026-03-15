"""
Create a new EVM wallet for Polygon (for WALLET_SEED in .env).
Run once: py create_wallet_seed.py
Then copy either the mnemonic OR the private key into .env as WALLET_SEED.
"""
from eth_account import Account

Account.enable_unaudited_hdwallet_features()
account, mnemonic = Account.create_with_mnemonic()

print("=" * 60)
print("NEW EVM WALLET (use on Polygon)")
print("=" * 60)
print()
print("ADDRESS (receive USDC.e / MATIC here):")
print(account.address)
print()
print("PRIVATE KEY (for .env WALLET_SEED):")
print(account.key.hex())
print()
print("MNEMONIC (12 words, alternative for .env WALLET_SEED):")
print(mnemonic)
print()
print("=" * 60)
print("Next steps:")
print("1. Add to .env:  WALLET_SEED=" + account.key.hex())
print("   (or use the mnemonic line above as WALLET_SEED=word1 word2 ...)")
print("2. Send USDC.e (Polygon) to:", account.address)
print("3. Restart the bot.")
print("=" * 60)
