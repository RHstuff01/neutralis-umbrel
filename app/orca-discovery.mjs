import { fetchPositionsForOwner, WhirlpoolDeployment } from "@orca-so/whirlpools";
import { address, createSolanaRpc } from "@solana/kit";

const wallet = process.argv[2];
const rpcUrl = process.env.SOLANA_RPC_URL;
if (!wallet || !rpcUrl) throw new Error("Carteira ou RPC Solana ausente");

const rpc = createSolanaRpc(rpcUrl);
const deployments = [WhirlpoolDeployment.mainnet, WhirlpoolDeployment.mainnetImmutable];
const discovered = [];

for (const deployment of deployments) {
  const rows = await fetchPositionsForOwner(rpc, address(wallet), deployment);
  for (const row of rows) {
    if (!row.isPositionBundle) {
      discovered.push({
        positionAddress: String(row.address),
        positionMint: String(row.data.positionMint),
        programId: String(deployment.programId),
      });
      continue;
    }
    for (const position of row.positions) {
      discovered.push({
        positionAddress: String(position.address),
        positionMint: String(position.data.positionMint),
        programId: String(deployment.programId),
        positionBundleAddress: String(row.address),
        positionBundleMint: String(row.data.positionBundleMint),
      });
    }
  }
}

process.stdout.write(JSON.stringify({ positions: discovered }));
