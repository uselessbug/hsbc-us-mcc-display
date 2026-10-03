# HSBC US Credit Card MCC Display

Tampermonkey userscript for the HSBC US / FirstData credit-card transaction-history page.

## What it does

- Intercepts HSBC/FirstData `postedtransactions` responses and shows the actual posted Mastercard MCC next to each visible transaction.
- Ignores FirstData's auxiliary zero-dollar FX rows (`transactionCode.display` is empty for those records).
- Matches visible rows using date + signed amount + transaction type + normalized description.
- Avoids modifying or wrapping HSBC's original transaction-type DOM nodes.
- Uses Mastercard's MCC descriptions from a separately updated JSON database.
- Caches that database locally in Tampermonkey and refreshes it in the background every 7 days.
- Keeps the last valid local database if a refresh fails.

HSBC US credit cards are Mastercard products, so this project intentionally maintains only the Mastercard MCC table.

## Install

Install the userscript from:

https://raw.githubusercontent.com/uselessbug/hsbc-us-mcc-display/main/hsbc-mcc-display.user.js

Tampermonkey will also use that URL for userscript updates.

## MCC database

The published database is:

https://raw.githubusercontent.com/uselessbug/hsbc-us-mcc-display/main/data/mcc-mastercard.json

`data/mcc-mastercard.json` is generated primarily from Mastercard's official **Quick Reference Booklet - Merchant Edition**. The updater extracts the extended MCC headings, industry-specific airline/car-rental/lodging tables, and Mastercard's global AB-program listing. Public institutional MCC lists from the City of San Antonio P-Card program and Florida DFS are used only as secondary references for QRB-referenced codes that still lack a description; they never overwrite a Mastercard description. The build also writes `data/mcc-source-report.json` so source/provenance decisions can be audited.

The database is intentionally separate from the userscript. Updating MCC descriptions therefore does not require a new userscript release.

A scheduled GitHub Actions workflow checks the Mastercard source weekly. It commits only when the generated JSON actually changes. Changes to the updater or its workflow also trigger a build, so a fresh repository populates the bootstrap database automatically.

Primary source:

https://www.mastercard.com/content/dam/mccom/shared/business/support/rules-pdfs/mastercard-quick-reference-booklet-merchant.pdf

Secondary institutional references:

- https://www.sanantonio.gov/Portals/0/Files/Purchasing/PCard/MerchantCategoryCodes.pdf
- https://fs.fldfs.com/iwpapps/pcard/docs/MCCs.pdf

## Privacy

The userscript sends no HSBC transaction data to GitHub or Mastercard.

The only third-party request added by the script is a periodic anonymous GET for the static MCC JSON on `raw.githubusercontent.com`. Posted transaction data stays in the browser and is used only to match MCCs to the rows already rendered by HSBC.

## Why pending transactions are not shown

HSBC/FirstData's `pendingtransactions` response does not expose `merchantCategoryCode` (or a transaction identifier usable with the posted-transaction detail endpoint). This project therefore displays authoritative MCCs only after a transaction is posted rather than guessing from merchant names.

## Development

Local syntax checks:

```bash
node --check hsbc-mcc-display.user.js
python -m py_compile scripts/update_mcc.py
python -m json.tool data/mcc-mastercard.json >/dev/null
```

To rebuild the database locally, install Poppler (`pdftotext`) and run:

```bash
python scripts/update_mcc.py
```
