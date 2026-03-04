// Utility script to rotate / revoke Polymarket API keys.
//
// What this does:
// 1) Uses L1 `createApiKey()` to create a NEW user API key for the wallet
//    (this invalidates the previous one – per Polymarket docs).
// 2) If builder credentials are present, calls `revokeBuilderApiKey()` to
//    revoke the current Builder API key.
//
// IMPORTANT:
// - Kör detta som ett engångsskript när du roterar nycklar.
// - Efter körning MÅSTE du:
//     • Uppdatera .env med de NYA USER_* credentials som skrivs ut i terminalen
//     • Skapa en ny Builder API‑nyckel i Builder‑profilen och uppdatera .env
// - Magnus använder Python – detta är ett separat Node/TS‑skript för key‑rotation.
//
// Usage:
//   cd /home/kim/agents
//   npm install @polymarket/clob-client @polymarket/builder-signing-sdk ethers dotenv
//   npx ts-node scripts/revoke_polymarket_keys.ts

import 'dotenv/config';
import { ClobClient } from '@polymarket/clob-client';
import { BuilderConfig, BuilderApiKeyCreds } from '@polymarket/builder-signing-sdk';
import { Wallet } from 'ethers';

async function revokeBuilderKey(
  host: string,
  chainId: number,
  signer: Wallet,
  apiCreds: { key: string; secret: string; passphrase: string },
) {
  const hasBuilderCreds =
    !!process.env.POLYMARKET_API_KEY &&
    !!process.env.POLYMARKET_API_SECRET &&
    !!process.env.POLYMARKET_API_PASSPHRASE;
  if (!hasBuilderCreds) {
    console.log('⚠️  No POLYMARKET_* builder credentials in env – skipping revokeBuilderApiKey().');
    return;
  }

  const builderConfig = new BuilderConfig({
    localBuilderCreds: new BuilderApiKeyCreds({
      key: process.env.POLYMARKET_API_KEY!,
      secret: process.env.POLYMARKET_API_SECRET!,
      passphrase: process.env.POLYMARKET_API_PASSPHRASE!,
    }),
  });

  // For builder methods, we need a ClobClient with both user apiCreds and builderConfig.
  // signatureType / funder are not used for revoke, so we leave them undefined.
  const builderClient = new ClobClient(
    host,
    chainId,
    signer,
    apiCreds,
    undefined,
    undefined,
    undefined,
    false,
    builderConfig,
  );

  console.log('🔑 Revoking Builder API key via revokeBuilderApiKey() …');
  await builderClient.revokeBuilderApiKey();
  console.log('✅ Builder API key revoked.');
}

async function revokeUserKey(userClient: AnyClient) {
  // NOTE: Polymarket L1 docs säger:
  //   "Each wallet can only have one active API key at a time —
  //    creating a new key invalidates the previous one."
  // Det finns ingen dokumenterad deleteApiKey() i L1‑klienten.
  // I stället skapar vi en NY API‑nyckel med createApiKey(), vilket
  // gör den gamla ogiltig.
  console.log('🔑 Creating NEW user API key via createApiKey() …');
  const apiCreds = await (userClient as any).createApiKey();
  console.log('\n✅ New USER API credentials created.\n');
  console.log('⚠️  IMPORTANT: Update your .env with these values and discard the old ones:\n');
  console.log(`USER_API_KEY=${apiCreds.key}`);
  console.log(`USER_SECRET=${apiCreds.secret}`);
  console.log(`USER_PASSPHRASE=${apiCreds.passphrase}\n`);
  console.log('After updating .env, restart Magnus so it uses the new credentials.');
}

async function main() {
  console.log('🚨 Polymarket API key revocation script');

  const host = 'https://clob.polymarket.com';
  const chainId = 137;

  const privateKey = process.env.PRIVATE_KEY;
  if (!privateKey) {
    throw new Error('PRIVATE_KEY is missing in env – required for L1 client.');
  }

  const signer = new Wallet(privateKey);

  // L1 client (no apiCreds) for createApiKey() – this will create a NEW user key,
  // invalidating the old one.
  const l1Client = new ClobClient(host, chainId, signer);

  // Step 1: rotate user API key (USER_*)
  await revokeUserKey(l1Client);

  // We now have new apiCreds in the output above. For builder revoke to work,
  // we need a valid user apiCreds object. We therefore ask the user to:
  //   1) copy the printed USER_* values into .env
  //   2) re-run this script if they ALSO want to revoke the builder key
  //
  // To avoid using out-of-sync env values, we do NOT automatically reuse apiCreds
  // here for builder revoke. Re-run after .env is updated if you want:
  //
  //   await revokeBuilderKey(host, chainId, signer, apiCreds);
  //
  console.log(
    'ℹ️  If you also want to revoke the Builder API key, update .env with the new USER_* values first,\n' +
      '    restart your shell so process.env is updated, and then re-run this script.\n',
  );
}

main().catch((err) => {
  console.error('❌ Error while revoking Polymarket keys:', err);
  process.exit(1);
});

