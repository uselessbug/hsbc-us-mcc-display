#!/usr/bin/env python3
"""Build the HSBC US Mastercard MCC dictionary from authoritative/public references.

Primary source:
- Mastercard Quick Reference Booklet - Merchant Edition

Secondary references are never allowed to overwrite Mastercard. They may only
supply a description for a code that the current Mastercard QRB itself
references in the global AB-program listing but does not otherwise describe,
and only when both institutional references agree.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

MASTERCARD_URL = (
    "https://www.mastercard.com/content/dam/mccom/shared/business/support/"
    "rules-pdfs/mastercard-quick-reference-booklet-merchant.pdf"
)
SAN_ANTONIO_URL = (
    "https://www.sanantonio.gov/Portals/0/Files/Purchasing/PCard/"
    "MerchantCategoryCodes.pdf"
)
FLORIDA_DFS_URL = "https://fs.fldfs.com/iwpapps/pcard/docs/MCCs.pdf"

MIN_ENTRIES = 500
MAX_ENTRIES = 1600
MIN_REFERENCE_ENTRIES = 300
SECONDARY_AGREEMENT = 0.82

KNOWN = {
    "0742": ("veterinary",),
    "4121": ("taxi", "limousine"),
    "4511": ("air",),
    "5812": ("restaurant", "eating"),
    "6555": ("rebate", "reward"),
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


def download(url: str, path: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; HSBC-MCC-DB-Updater/1.0)",
            "Accept": "application/pdf,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        if response.status != 200:
            raise RuntimeError(f"Download failed for {url}: HTTP {response.status}")
        data = response.read()

    if not data.startswith(b"%PDF-"):
        raise RuntimeError(f"Source did not return a PDF: {url}")
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


def expand_code_list(value: str) -> list[str] | None:
    """Parse a line that consists only of MCCs/ranges separated by commas."""
    value = value.replace("–", "-").replace("—", "-").strip()
    if not re.fullmatch(
        r"\d{4}(?:\s*-\s*\d{4})?(?:\s*,\s*\d{4}(?:\s*-\s*\d{4})?)*",
        value,
    ):
        return None

    result: list[str] = []
    for token in re.split(r"\s*,\s*", value):
        if "-" not in token:
            result.append(token)
            continue
        start_s, end_s = re.split(r"\s*-\s*", token)
        start, end = int(start_s), int(end_s)
        if end < start or end - start > 1000:
            return None
        result.extend(f"{number:04d}" for number in range(start, end + 1))
    return result


def global_ab_programs(text: str) -> tuple[dict[str, dict[str, object]], set[str]]:
    """Parse Mastercard's global AB-program -> MCC listing.

    This section is useful because some network-level MCCs, notably 6555, are
    listed by an AB program even though they have no extended MCC chapter.
    """
    start = text.find("All AB programs")
    if start < 0:
        raise RuntimeError("Unable to find Mastercard global AB-program listing")

    end_markers = (
        "Country-specific AB programs with acceptor business codes",
        "Country-specific AB programs",
        "Processing exceptions",
    )
    ends = [text.find(marker, start + 1) for marker in end_markers]
    ends = [position for position in ends if position > start]
    segment = text[start : min(ends) if ends else len(text)]

    programs: dict[str, dict[str, object]] = {}
    current: str | None = None
    header = re.compile(r"\b([A-Z][A-Z0-9]{3}):\s*(.+?)\s*$")

    for raw in segment.splitlines():
        line = clean_description(raw)
        if not line:
            continue

        match = header.search(line)
        if match:
            code, desc = match.groups()
            # Strip a page-number/footer prefix that pdftotext can occasionally
            # leave attached to the first program on a page.
            desc = clean_description(desc)
            programs[code] = {"description": desc, "mccs": []}
            current = code
            continue

        if current is None:
            continue

        codes = expand_code_list(line)
        if codes is not None:
            programs[current]["mccs"].extend(codes)

    ab_codes: set[str] = set()
    for program in programs.values():
        ab_codes.update(program["mccs"])

    if "I001" not in programs or "6555" not in programs["I001"]["mccs"]:
        raise RuntimeError("Mastercard AB-program parser did not find I001 -> MCC 6555")

    return programs, ab_codes


def extract_mastercard(
    text: str,
) -> tuple[dict[str, str], dict[str, dict[str, str]], set[str]]:
    entries: dict[str, str] = {}
    provenance: dict[str, dict[str, str]] = {}

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
            if old is None or len(desc) < len(old):
                entries[code] = desc
                provenance[code] = {"source": "mastercard-qrb-extended"}
            break

    for start, end, desc in RANGES:
        for number in range(start, end + 1):
            code = f"{number:04d}"
            if code not in entries:
                entries[code] = desc
                provenance[code] = {"source": "mastercard-qrb-industry-range"}

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
            provenance[code] = {"source": "mastercard-qrb-industry-specific"}

    programs, ab_codes = global_ab_programs(text)

    # A singleton AB program provides an unambiguous current-Mastercard label
    # for an MCC that may not have an extended chapter. I001 -> 6555 is the
    # important example for HSBC Elite rideshare rebates.
    for program_code, program in programs.items():
        mccs = list(dict.fromkeys(program["mccs"]))
        if len(mccs) != 1:
            continue
        code = mccs[0]
        if code in entries:
            continue
        desc = clean_description(str(program["description"]))
        if len(desc) < 3:
            continue
        entries[code] = desc
        provenance[code] = {
            "source": "mastercard-qrb-ab-program",
            "program": program_code,
        }

    return (
        dict(sorted(entries.items(), key=lambda item: int(item[0]))),
        provenance,
        ab_codes,
    )


def extract_institutional_reference(text: str, source: str) -> dict[str, str]:
    """Extract a simple four-digit MCC -> description table from a public PDF."""
    result: dict[str, str] = {}

    for raw in text.splitlines():
        match = re.match(r"^\s*(\d{4})\s+(.+?)\s*$", raw)
        if not match:
            continue

        code, desc = match.groups()
        desc = re.sub(r"\s+I\s+I\s+I\b.*$", "", desc)
        if source == "san-antonio-pcard":
            desc = re.sub(r"\s+INCLUDE\s*$", "", desc, flags=re.IGNORECASE)
        elif source == "florida-dfs":
            desc = re.sub(r"\s+[ARP]\s*$", "", desc)

        desc = clean_description(desc)
        if len(desc) < 3:
            continue

        # First occurrence is sufficient and avoids footer/table extraction noise.
        result.setdefault(code, desc)

    if len(result) < MIN_REFERENCE_ENTRIES:
        raise RuntimeError(
            f"{source} parser found only {len(result)} MCC entries; refusing to use it"
        )
    return result


def canonical_description(value: str) -> str:
    value = value.upper().replace("&", " AND ")
    value = re.sub(r"[^A-Z0-9]+", " ", value)
    words = []
    for word in value.split():
        if len(word) > 4 and word.endswith("S"):
            word = word[:-1]
        words.append(word)
    return " ".join(words)


def descriptions_agree(left: str, right: str) -> bool:
    a = canonical_description(left)
    b = canonical_description(right)
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= SECONDARY_AGREEMENT


def apply_secondary_consensus(
    entries: dict[str, str],
    provenance: dict[str, dict[str, str]],
    ab_codes: set[str],
    san_antonio: dict[str, str],
    florida: dict[str, str],
) -> dict[str, dict[str, object]]:
    """Add only QRB-referenced missing MCCs confirmed by both institutions."""
    added: dict[str, dict[str, object]] = {}

    for code in sorted(ab_codes, key=int):
        if code in entries:
            continue
        left = san_antonio.get(code)
        right = florida.get(code)
        if not left or not right or not descriptions_agree(left, right):
            continue

        entries[code] = left
        provenance[code] = {
            "source": "institutional-consensus",
            "references": "san-antonio-pcard,florida-dfs",
        }
        added[code] = {
            "description": left,
            "sanAntonio": left,
            "floridaDfs": right,
        }

    return added


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
            "url": MASTERCARD_URL,
            "documentDate": version,
            "method": "official-pdf-text+global-ab-programs",
            "secondaryReferences": [
                SAN_ANTONIO_URL,
                FLORIDA_DFS_URL,
            ],
        },
        "mcc": dict(sorted(entries.items(), key=lambda item: int(item[0]))),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_report(
    *,
    version: str,
    entries: dict[str, str],
    provenance: dict[str, dict[str, str]],
    ab_codes: set[str],
    san_antonio: dict[str, str],
    florida: dict[str, str],
    secondary_added: dict[str, dict[str, object]],
    output: Path,
) -> None:
    ab_additions = {
        code: {
            "description": entries[code],
            **provenance[code],
            "corroboratedBy": [
                name
                for name, table in (
                    ("san-antonio-pcard", san_antonio),
                    ("florida-dfs", florida),
                )
                if code in table and descriptions_agree(entries[code], table[code])
            ],
        }
        for code in sorted(entries, key=int)
        if provenance.get(code, {}).get("source") == "mastercard-qrb-ab-program"
    }

    secondary_only_agreements = {
        code: {
            "sanAntonio": san_antonio[code],
            "floridaDfs": florida[code],
        }
        for code in sorted(set(san_antonio) & set(florida), key=int)
        if code not in entries
        and descriptions_agree(san_antonio[code], florida[code])
    }

    report = {
        "schemaVersion": 1,
        "version": version,
        "sources": {
            "mastercardQrb": MASTERCARD_URL,
            "sanAntonioPcard": SAN_ANTONIO_URL,
            "floridaDfsPcard": FLORIDA_DFS_URL,
        },
        "counts": {
            "published": len(entries),
            "mastercardGlobalAbReferenced": len(ab_codes),
            "sanAntonioReference": len(san_antonio),
            "floridaDfsReference": len(florida),
            "mastercardAbProgramAdditions": len(ab_additions),
            "secondaryConsensusAdditions": len(secondary_added),
            "secondaryConsensusNotInCurrentMastercard": len(secondary_only_agreements),
        },
        "mastercardAbProgramAdditions": ab_additions,
        "secondaryConsensusAdditions": secondary_added,
        "secondaryConsensusNotInCurrentMastercard": secondary_only_agreements,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("data/mcc-mastercard.json"))
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("data/mcc-source-report.json"),
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        source_specs = {
            "mastercard": (MASTERCARD_URL, root / "mastercard-qrb.pdf"),
            "san-antonio": (SAN_ANTONIO_URL, root / "san-antonio-mcc.pdf"),
            "florida": (FLORIDA_DFS_URL, root / "florida-mcc.pdf"),
        }

        texts: dict[str, str] = {}
        for name, (url, pdf) in source_specs.items():
            txt = root / f"{name}.txt"
            print(f"Downloading {url}")
            download(url, pdf)
            texts[name] = pdf_text(pdf, txt)

        version = document_date(texts["mastercard"])
        entries, provenance, ab_codes = extract_mastercard(texts["mastercard"])

        san_antonio = extract_institutional_reference(
            texts["san-antonio"], "san-antonio-pcard"
        )
        florida = extract_institutional_reference(texts["florida"], "florida-dfs")

        secondary_added = apply_secondary_consensus(
            entries,
            provenance,
            ab_codes,
            san_antonio,
            florida,
        )

        validate(entries)

        print(f"Mastercard document date: {version}")
        print(f"Published MCC entries: {len(entries)}")
        print(
            "MCC 6555:",
            entries["6555"],
            f"({provenance['6555'].get('source')})",
        )
        print(f"Secondary consensus additions: {len(secondary_added)}")

        write_database(entries, version, args.output)
        write_report(
            version=version,
            entries=entries,
            provenance=provenance,
            ab_codes=ab_codes,
            san_antonio=san_antonio,
            florida=florida,
            secondary_added=secondary_added,
            output=args.report,
        )
        print(f"Wrote {args.output}")
        print(f"Wrote {args.report}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
