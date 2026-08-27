import assert from "node:assert/strict";

const ONE_MILLION = 1_000_000n;
const BPS = 10_000n;
const ATOMS_PER_USD_CENT = 100_000_000_000n;

function ceilDiv(n, d) {
  assert(n >= 0n && d > 0n);
  return n === 0n ? 0n : (n + d - 1n) / d;
}

function priceTurn(providerCostNumerator, revision) {
  const baseCostAtoms = ceilDiv(
    providerCostNumerator * revision.usdAtomsPerCnyMinor,
    ONE_MILLION,
  );
  const exactChargeAtoms = ceilDiv(
    baseCostAtoms * (BPS + revision.marginBps),
    BPS,
  );
  return { baseCostAtoms, exactChargeAtoms };
}

function holdMinor(modalityTokenCeilings, prices, revision) {
  const numerator = Object.keys(prices).reduce(
    (sum, key) => sum + modalityTokenCeilings[key] * prices[key],
    0n,
  );
  return ceilDiv(priceTurn(numerator, revision).exactChargeAtoms, ATOMS_PER_USD_CENT);
}

const fixtureRevision = {
  id: "tokenseller-qwen35-usd-v1",
  usdAtomsPerCnyMinor: 14_880_267_833n,
  marginBps: 1_000n,
};

assert.deepEqual(priceTurn(127_750n, fixtureRevision), {
  baseCostAtoms: 1_900_954_216n,
  exactChargeAtoms: 2_091_049_638n,
});
assert.deepEqual(priceTurn(38_260n, fixtureRevision), {
  baseCostAtoms: 569_319_048n,
  exactChargeAtoms: 626_250_953n,
});

const prices = { inputText: 330n, inputAudio: 2_700n, outputText: 2_000n, outputAudio: 10_700n };
const ceilings = { inputText: 8_000n, inputAudio: 16_000n, outputText: 4_000n, outputAudio: 8_000n };
const hold = holdMinor(ceilings, prices, fixtureRevision);
assert(hold > 0n);
assert(hold * ATOMS_PER_USD_CENT >= priceTurn(
  Object.keys(prices).reduce((sum, key) => sum + ceilings[key] * prices[key], 0n),
  fixtureRevision,
).exactChargeAtoms);

console.log(JSON.stringify({
  status: "SP-FX-ATOMS-PASS",
  fixtureRevision: {
    id: fixtureRevision.id,
    usdAtomsPerCnyMinor: fixtureRevision.usdAtomsPerCnyMinor.toString(),
    marginBps: fixtureRevision.marginBps.toString(),
  },
  completed: { baseCostAtoms: "1900954216", exactChargeAtoms: "2091049638" },
  cancelled: { baseCostAtoms: "569319048", exactChargeAtoms: "626250953" },
  holdMinor: hold.toString(),
}));
