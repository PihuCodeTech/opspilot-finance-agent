# Datasets

Every dataset is **synthetic**: vendors, people, addresses and bank account numbers are generated (Faker and random digits) and correspond to no real organisation or account. No real invoice is in this repository.

## Splits (generator version 1)

| Split | Seed | Invoices | Clean | Purpose | Manifest SHA-256 |
|-------|------|---------:|------:|---------|------------------|
| `dev` | 1001 | 200 | 80 | Building and tuning both systems. **Scores on this set are never reported as results.** | `94ecd3483d023497f31d6748e969d6ea6477a4a6dfe782595152a3484a764516` |
| `final` | 2002 | 600 | 240 | The evaluation set. **Frozen before any prompt tuning**; contains the held-out variants below | `b264806a971ca1d9f9f9796a8bf584330f7765cdf8e57bbb43d6ac0b4fa8d7b8` |
| `adversarial` | 3003 | 50 | 0 | Instructions hidden in invoice notes and PO line descriptions, each on a real exception | `8cb892f64f1e4e15281765ff8ab6f4864fbeb382e35abf1689f842992d7c4b3b` |
| `demo` | 4004 | 24 | 12 | The live UI | `e6cbdafb18453534b01fa15a0ba4d6e4f5b05ba316cd107488575f69aada6e6f` |

- **Final-set archive:** `final-g1-s2002.tar.gz`, SHA-256 `c2c6799d1f39c0e8c3fbc899f973e29c9175cc4a437c3abba826ba5d780593d3` (published as a GitHub Release asset; byte-reproducible from the bundle).
- **Pre-declared subset:** [`final-subset-150.txt`](final-subset-150.txt), 150 item keys, SHA-256 `c17f2deae626799c582ecb42b7be21a97b7da23729d9ea3172eaa66af86bd956`. Drawn once with seed 5005, stratified by each item's first exception type (clean invoices form their own stratum). The evaluation step chooses between the full final set and this subset based on measured free-tier quota. It is never re-drawn.

Each exception split has ≥ 40 invoices per exception type in `final`, and about 10% of exception invoices carry two exceptions.

## Exception definitions

The tolerances come from `src/opspilot/policy/config.toml`. Money is compared after rounding half-up to cents, and boundaries are inclusive on the accepted side.

| Exception | Raised when |
|-----------|-------------|
| `MISSING_OR_UNKNOWN_PO` | The invoice has no PO number, one that doesn't exist, or one belonging to another vendor. PO-dependent checks (price, quantity, approval level) then have nothing to compare and aren't applied |
| `PRICE_MISMATCH` | (invoice unit price − PO unit price) ÷ PO unit price × 100, rounded to 2 dp, is above 2.00 |
| `QTY_OVER_RECEIVED` | Quantity already billed on that PO line in the ledger, plus this invoice's quantity, exceeds the quantity received |
| `ARITHMETIC_MISMATCH` | A line total differs from quantity × unit price by more than $0.01, or the lines don't sum to the subtotal, or subtotal + tax doesn't equal the total, within $0.05 |
| `TAX_MISCALCULATED` | Stated tax differs from subtotal × the vendor's tax rate by more than $0.01 |
| `DUPLICATE_INVOICE` | A ledger invoice from the same vendor has the same invoice number after normalisation (lower-case, letters and digits only); **or** has a total within ±0.5%, an invoice date within 30 days and the same set of lines (each line identified by SKU, or description where no SKU is printed, with its quantity) |
| `VENDOR_NOT_APPROVED` | The vendor isn't on the approved list |
| `APPROVAL_LIMIT_EXCEEDED` | The approval amount, meaning the larger of the stated total and the total recomputed from lines and tax, exceeds the PO's level limit (level 1 $10,000; level 2 $50,000), or exceeds $50,000 without a recorded controller approval on the PO |
| `BANK_DETAILS_CHANGED` | The bank account printed on the invoice differs from the vendor master's current account |

"Ledger" means the dataset's already-processed invoices. Checks never consider other incoming invoices, so results don't depend on processing order.

## Held-out variants (final set only)

The dev set contains none of these; tests enforce both directions.

| Variant | What it is |
|---------|------------|
| Layout `sidebar` | A sixth invoice layout: vendor and payment details in a left column, totals above the lines, line value as the first column |
| Paraphrase set B | A second set of product descriptions that never appears in dev |
| `price_boundary` | Unit price exactly 2.01% above the PO |
| `qty_boundary` | One unit more than received |
| `arith_boundary` | Total off by $0.06 |
| `tax_boundary` | Tax off by $0.02 |
| `dup_near_suffix` | Resubmission with a suffix (`…-R`) |
| `dup_exact_case_spacing` | Same number with different case and spacing |

Variation in clean invoices (all splits): description paraphrases, partial deliveries and split billing, prices within ±2% of the PO, several tax rates, legitimate past bank changes in the vendor master, and five layouts that differ in field labels, date and number formats, and column order.

## Labels

Labels are written by the generator from its injection plan, never by an LLM. A scenario is kept only when the generator's reference evaluator finds exactly the injected exceptions, and the complete dataset is re-checked at the end. The test suite holds a third, independent implementation that must agree on every invoice.

Each item carries:
- **exceptions:** type and subject (`header` or `line:N`)
- **acceptable actions:** the agent allowlist for those findings
- **flags:** clean and adversarial
- **variant tags**
- **extraction truth:** invoice number, date, vendor, PO, subtotal, tax, total, bank last 4, line count, layout

Labels are stored in `labels.json` and `dataset_items.labels`. Only the scorer role can read them: no inference process mounts or queries them.

## Bundle format and commands

```
<split>-g<generator version>-s<seed>/
  manifest.json   seed, version, SHA-256 of every file
  world.json      vendors, POs, receipts, ledger, incoming invoices (synthetic account numbers)
  labels.json     labels (scorer only)
  documents/      one PDF per incoming invoice
```

```bash
docker compose run --rm tools opspilot generate --split dev          # renders PDFs (needs Pango)
docker compose run --rm tools opspilot dataset verify data/datasets/dev-g1-s1001
docker compose run --rm tools opspilot dataset load   data/datasets/dev-g1-s1001
docker compose run --rm tools opspilot dataset freeze data/datasets/final-g1-s2002
docker compose run --rm tools opspilot dataset subset data/datasets/final-g1-s2002
docker compose run --rm tools opspilot dataset archive data/datasets/final-g1-s2002
```

When loading, account numbers are reduced to a keyed fingerprint plus their last 4 digits. The database never stores a full number.
