#!/usr/bin/env python3
"""Generate a deterministic Mastercard MCC dictionary from the official Quick Reference Booklet PDF."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

PDF_URL = (
    "https://www.mastercard.com/content/dam/mccom/shared/business/support/"
    "rules-pdfs/mastercard-quick-reference-booklet-merchant.pdf"
)

MIN_ENTRIES = 500
MAX_ENTRIES = 1600

KNOWN = {
    "0742": ("veterinary",),
    "4121": ("taxi", "limousine"),
    "4511": ("air",),
    "5812": ("restaurant", "eating"),
    "7011": ("hotel", "motel", "lodging"),
}

# Mastercard groups these industry-specific codes under range descriptions in the
# extended section. Specific carrier/rental/hotel names found later in the PDF
# override these generic labels.
RANGES = (
    (3000, 3350, "Airlines, Air Carriers"),
    (3351, 3500, "Car Rental Agencies"),
    (3501, 3999, "Lodging: Hotels, Motels, Resorts"),
)


def run(*args: str) -> str:
    return subprocess.run(
        args,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ).stdout


def download(path: Path) -> None:
    request = urllib.request.Request(
        PDF_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; HSBC-MCC-DB-Updater/1.0)",
            "Accept": "application/pdf,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        if response.status != 200:
            raise RuntimeError(f"Download failed: HTTP {response.status}")
        data = response.read()

    if not data.startswith(b"%PDF-"):
        raise RuntimeError("Mastercard source did not return a PDF")
    path.write_bytes(data)


def pdf_text(pdf: Path, output: Path) -> str:
    if not shutil.which("pdftotext"):
        raise RuntimeError("pdftotext is required (install poppler-utils)")
    run("pdftotext", "-layout", str(pdf), str(output))
    return output.read_text(encoding="utf-8", errors="replace")


def clean_description(value: str) -> str:
    value = value.replace("\u00ad", "")
    value = re.sub(r"[.·]{3,}\s*\d*\s*$", "", value)
    value = re.sub(r"\s+", " ", value).strip(" \t:;-")
    return value


def document_date(text: str) -> str:
    match = re.search(
        r"\b(\d{1,2})\s+"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
        r"(20\d{2})\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        raise RuntimeError("Unable to determine Mastercard document date")
    return dt.datetime.strptime(" ".join(match.groups()), "%d %B %Y").date().isoformat()


def extract(text: str) -> dict[str, str]:
    entries: dict[str, str] = {}

    # General MCC headings in the extended section. These also occur in the TOC
    # and page footers; after normalization they resolve to the same short label.
    heading_patterns = (
        re.compile(r"^\s*MCC\s+(\d{4})\s*[:\-–—]\s*(.+?)\s*$", re.IGNORECASE),
        re.compile(r"^\s*Description\s+of\s+MCC\s+(\d{4})\s*[:\-–—]\s*(.+?)\s*$", re.IGNORECASE),
    )

    for raw in text.splitlines():
        for pattern in heading_patterns:
            match = pattern.match(raw)
            if not match:
                continue
            code, desc = match.groups()
            desc = clean_description(desc)
            if len(desc) < 3:
                break
            old = entries.get(code)
            # Prefer a clean/short heading over a noisy TOC extraction.
            if old is None or len(desc) < len(old):
                entries[code] = desc
            break

    # Fill the three Mastercard industry-specific ranges. This guarantees useful
    # descriptions even when a particular brand code is absent from the industry
    # table in a future booklet revision.
    for start, end, desc in RANGES:
        for number in range(start, end + 1):
            entries.setdefault(f"{number:04d}", desc)

    # The Industry Specific MCC table contains rows such as:
    #   3000 X United Airlines: UNITED
    # It provides a better description than the generic range label.
    industry = re.compile(r"^\s*(\d{4})\s+[A-Z]\s+(.+?)\s*$")
    for raw in text.splitlines():
        match = industry.match(raw)
        if not match:
            continue
        code, desc = match.groups()
        if not (3000 <= int(code) <= 3999):
            continue
        desc = clean_description(desc)
        if len(desc) >= 3:
            entries[code] = desc

    return dict(sorted(entries.items(), key=lambda item: int(item[0])))


def validate(entries: dict[str, str]) -> None:
    if not MIN_ENTRIES <= len(entries) <= MAX_ENTRIES:
        raise RuntimeError(f"Unexpected MCC count: {len(entries)}")

    for code, expected in KNOWN.items():
        desc = entries.get(code, "").lower()
        if not desc or not any(word in desc for word in expected):
            raise RuntimeError(f"Known MCC {code} missing or unexpected: {entries.get(code)!r}")

    for code, desc in entries.items():
        if not re.fullmatch(r"\d{4}", code):
            raise RuntimeError(f"Invalid MCC key: {code!r}")
        if not isinstance(desc, str) or not desc.strip():
            raise RuntimeError(f"Invalid description for MCC {code}")


def write_database(entries: dict[str, str], version: str, output: Path) -> None:
    payload = {
        "schemaVersion": 1,
        "version": version,
        "source": {
            "network": "Mastercard",
            "document": "Quick Reference Booklet - Merchant Edition",
            "url": PDF_URL,
            "documentDate": version,
            "method": "official-pdf-text",
        },
        "mcc": entries,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("data/mcc-mastercard.json"))
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdf = root / "mastercard-qrb.pdf"
        txt = root / "mastercard-qrb.txt"

        print(f"Downloading {PDF_URL}")
        download(pdf)
        text = pdf_text(pdf, txt)
        version = document_date(text)
        entries = extract(text)
        validate(entries)

        print(f"Mastercard document date: {version}")
        print(f"Validated {len(entries)} MCC entries")
        write_database(entries, version, args.output)
        print(f"Wrote {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
