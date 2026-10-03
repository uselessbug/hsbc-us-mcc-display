#!/usr/bin/env python3
"""Build a deterministic Mastercard MCC dictionary from public references.

Primary source:
- Mastercard Quick Reference Booklet - Merchant Edition (QRB)

Secondary institutional references:
- City of San Antonio P-Card MCC table
- Florida DFS PCard MCC table

Mastercard always wins. Secondary references may only fill a QRB-referenced
code that still lacks a Mastercard description, and only when both independent
institutional tables agree. Parsers are fail-closed: source-format drift must
raise instead of silently publishing a suspicious database.
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
MAX_ENTRIES = 1700
MIN_REFERENCE_ENTRIES = {
    "san-antonio-pcard": 850,
    "florida-dfs": 850,
}
MIN_REFERENCE_OVERLAP = 800
MIN_GLOBAL_AB_CODES = 1005
MIN_NAMED_INDUSTRY_CODES = 400
SECONDARY_AGREEMENT = 0.82

INDUSTRY_SENTINELS = {
    "3000": ("united",),
    "3043": ("aer lingus",),
    "3374": ("rent",),
    "3412": ("rent",),
    "3514": ("amerisuites",),
    "3530": ("renaissance",),
    "3850": ("breezbay",),
}

KNOWN = {
    "0742": ("veterinary",),
    "4121": ("taxi", "limousine"),
    "4511": ("air",),
    "5812": ("restaurant", "eating"),
    "6555": ("rebate", "reward"),
    "7011": ("hotel", "motel", "lodging"),
}

COMPLETE_TITLE_CHECKS = {
    "1740": ("contractors",),
    "4813": ("long-distance", "key entry"),
    "4814": ("recurring phone services",),
    "5813": ("alcoholic beverages",),
    "7372": ("integrated systems design services",),
    "7802": ("u.s. region only",),
    "7997": ("private golf courses",),
    "9406": ("excluding u.s. region",),
}

COUNTRY_AB_SENTINELS = {
    "1443",
    "1484",
    "7987",
    "7988",
    "7989",
    "9407",
    "9702",
    "9753",
    "9950",
}

REFERENCE_SENTINELS = {
    "0742": ("veterinary",),
    "5812": ("restaurant", "eating"),
    "6536": ("moneysend", "money send"),
    "6555": ("rebate", "reward"),
}

RANGES = (
    (3000, 3350, "Airlines, Air Carriers"),
    (3351, 3500, "Car Rental Agencies"),
    (3501, 3999, "Lodging: Hotels, Motels, Resorts"),
)

PROGRAM_HEADER = re.compile(r"^\s*([A-Z][A-Z0-9]{3}):\s*(.+?)\s*$")
MCC_RANGE = re.compile(
    r"(?<![A-Z0-9])(\d{4})(?:\s*[-–—]\s*(\d{4}))?(?![A-Z0-9])"
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
    value = value.replace("\x02", " ")
    value = re.sub(r"[.·]{3,}\s*\d*\s*$", "", value)
    value = re.sub(r"\s+", " ", value).strip(" \t:;-")
    return value


def qrb_noise(value: str) -> bool:
    line = clean_description(value)
    if not line:
        return True
    return (
        line.startswith("Quick Reference Booklet")
        or line.startswith("© 1990")
        or line == "Acceptor business codes (MCCs)"
        or line == "AB program listing with acceptor business codes (MCCs)"
        or line == "Country-specific AB programs with acceptor business codes (MCCs)"
        or line == "Industry Specific Acceptor Business Codes (MCCs)"
    )


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


def mcc_mentions(value: str) -> set[str]:
    """Return every explicit four-digit MCC/range mentioned in a line."""
    result: set[str] = set()
    for match in MCC_RANGE.finditer(value):
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if end < start or end - start > 1000:
            continue
        result.update(f"{number:04d}" for number in range(start, end + 1))
    return result


def direct_code_list(value: str) -> list[str] | None:
    """Parse a body that contains only comma-separated MCCs/ranges."""
    normalized = value.replace("–", "-").replace("—", "-").strip(" ,.;")
    if not normalized:
        return []
    if not re.fullmatch(
        r"\d{4}(?:\s*-\s*\d{4})?"
        r"(?:\s*,\s*\d{4}(?:\s*-\s*\d{4})?)*",
        normalized,
    ):
        return None

    result: list[str] = []
    for token in re.split(r"\s*,\s*", normalized):
        if "-" not in token:
            result.append(token)
            continue
        start_s, end_s = re.split(r"\s*-\s*", token)
        start, end = int(start_s), int(end_s)
        if end < start or end - start > 1000:
            return None
        result.extend(f"{number:04d}" for number in range(start, end + 1))
    return result


def section(text: str, start_marker: str, end_marker: str | None = None) -> str:
    start = text.find(start_marker)
    if start < 0:
        raise RuntimeError(f"Unable to find QRB section: {start_marker}")
    if end_marker is None:
        return text[start:]
    end = text.find(end_marker, start + len(start_marker))
    if end < 0:
        raise RuntimeError(f"Unable to find QRB section end: {end_marker}")
    return text[start:end]


def parse_ab_programs(segment: str) -> dict[str, dict[str, object]]:
    """Parse AB program headers plus their multi-line bodies.

    We intentionally retain free-form body text. Direct MCC mentions are
    extracted from every continuation line, so line wrapping and trailing commas
    do not lose codes. Program-to-program references are recorded separately.
    """
    programs: dict[str, dict[str, object]] = {}
    current: str | None = None

    for raw in segment.splitlines():
        if qrb_noise(raw):
            continue
        line = clean_description(raw)
        match = PROGRAM_HEADER.match(line)
        if match:
            code, desc = match.groups()
            programs[code] = {
                "description": clean_description(desc),
                "body": [],
                "mccs": set(),
                "programRefs": set(),
            }
            current = code
            continue

        if current is None:
            continue

        programs[current]["body"].append(line)
        programs[current]["mccs"].update(mcc_mentions(line))

    known_programs = set(programs)
    for code, program in programs.items():
        body_text = " ".join(program["body"])
        refs = {
            token
            for token in re.findall(r"\b[A-Z][A-Z0-9]{3}\b", body_text)
            if token in known_programs and token != code
        }
        program["programRefs"] = refs

    return programs


def program_codes(programs: dict[str, dict[str, object]]) -> set[str]:
    result: set[str] = set()
    for program in programs.values():
        result.update(program["mccs"])
    return result


def program_direct_membership(program: dict[str, object]) -> list[str] | None:
    """Return exact direct membership only when the body is a plain code list."""
    body = " ".join(program["body"])
    return direct_code_list(body)


def parse_extended_titles(text: str) -> dict[str, str]:
    """Parse full multi-line 'Description of MCC ####:' titles.

    QRB frequently wraps titles across physical lines. Accumulate continuation
    lines until the TCC/MCC-description block begins instead of truncating the
    first line.
    """
    lines = text.splitlines()
    result: dict[str, str] = {}
    start_re = re.compile(
        r"^\s*Description\s+of\s+MCC\s+(\d{4})\s*:\s*(.*?)\s*$",
        re.IGNORECASE,
    )

    for index, raw in enumerate(lines):
        match = start_re.match(raw)
        if not match:
            continue

        code, first = match.groups()
        parts = [clean_description(first)] if clean_description(first) else []

        for offset in range(index + 1, min(index + 10, len(lines))):
            candidate_raw = lines[offset]
            candidate = clean_description(candidate_raw)
            if qrb_noise(candidate_raw):
                continue
            if (
                re.match(r"^TCC\b", candidate, re.IGNORECASE)
                or re.match(r"^MCC\s+Description\b", candidate, re.IGNORECASE)
                or re.match(r"^MCC\s+Category\b", candidate, re.IGNORECASE)
                or re.match(r"^AB\s+Programs\b", candidate, re.IGNORECASE)
                or re.match(r"^Country-specific\s*:", candidate, re.IGNORECASE)
                or start_re.match(candidate_raw)
                or re.match(r"^MCC\s+\d{4}\s*:", candidate, re.IGNORECASE)
            ):
                break
            if candidate:
                parts.append(candidate)

        title = clean_description(" ".join(parts))
        if len(title) < 3:
            raise RuntimeError(f"Empty/invalid extended title for MCC {code}")

        old = result.get(code)
        if old and old.casefold() != title.casefold():
            raise RuntimeError(
                f"Conflicting QRB extended titles for MCC {code}: {old!r} vs {title!r}"
            )
        result[code] = title

    if len(result) < 250:
        raise RuntimeError(
            f"QRB extended-title parser found only {len(result)} MCC descriptions"
        )
    return result


def parse_industry_specific(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    row = re.compile(r"(?:^|\s)(\d{4})\s+([A-Z])\s+(.+?)\s*$")

    for raw in text.splitlines():
        match = row.search(raw)
        if not match:
            continue
        code, _kind, desc = match.groups()
        if not 3000 <= int(code) <= 3999:
            continue
        desc = clean_description(desc)
        if len(desc) < 3:
            continue
        old = result.get(code)
        if old and old.casefold() != desc.casefold():
            # Prefer the longer label only when one extraction is a prefix of the
            # other; otherwise fail closed on a real conflict.
            a, b = old.casefold(), desc.casefold()
            if a in b:
                result[code] = desc
            elif b not in a:
                raise RuntimeError(
                    f"Conflicting industry labels for MCC {code}: {old!r} vs {desc!r}"
                )
        else:
            result[code] = desc

    if len(result) < MIN_NAMED_INDUSTRY_CODES:
        raise RuntimeError(
            f"QRB industry-specific parser found only {len(result)} named codes; "
            f"expected at least {MIN_NAMED_INDUSTRY_CODES}"
        )

    for code, expected in INDUSTRY_SENTINELS.items():
        desc = result.get(code, "").lower()
        if not desc or not any(token in desc for token in expected):
            raise RuntimeError(
                f"QRB industry-specific sentinel {code} missing/unexpected: "
                f"{result.get(code)!r}"
            )
    return result


def extract_mastercard(
    text: str,
) -> tuple[
    dict[str, str],
    dict[str, dict[str, str]],
    set[str],
    set[str],
]:
    entries: dict[str, str] = {}
    provenance: dict[str, dict[str, str]] = {}

    extended = parse_extended_titles(text)
    for code, desc in extended.items():
        entries[code] = desc
        provenance[code] = {"source": "mastercard-qrb-extended"}

    for start, end, desc in RANGES:
        for number in range(start, end + 1):
            code = f"{number:04d}"
            if code not in entries:
                entries[code] = desc
                provenance[code] = {"source": "mastercard-qrb-industry-range"}

    industry = parse_industry_specific(text)
    for code, desc in industry.items():
        entries[code] = desc
        provenance[code] = {"source": "mastercard-qrb-industry-specific"}

    global_segment = section(
        text,
        "All AB programs",
        "Country-specific AB programs with acceptor business codes (MCCs)",
    )
    global_programs = parse_ab_programs(global_segment)
    global_codes = program_codes(global_programs)

    country_segment = section(
        text,
        "Country-specific AB programs with acceptor business codes (MCCs)",
    )
    country_programs = parse_ab_programs(country_segment)
    country_codes = program_codes(country_programs)

    if len(global_codes) < MIN_GLOBAL_AB_CODES:
        raise RuntimeError(
            f"Global AB parser found only {len(global_codes)} explicit MCCs; "
            f"expected at least {MIN_GLOBAL_AB_CODES}"
        )

    missing_country_sentinels = COUNTRY_AB_SENTINELS - country_codes
    if missing_country_sentinels:
        raise RuntimeError(
            "Country-specific AB parser missed known QRB MCCs: "
            + ", ".join(sorted(missing_country_sentinels))
        )

    # A singleton AB program provides an unambiguous Mastercard label for a
    # network-level MCC that may not have an extended chapter. Only use a
    # literal/direct code-list body; never infer a description from an "except"
    # rule or another program reference.
    for program_code, program in global_programs.items():
        membership = program_direct_membership(program)
        if membership is None or len(set(membership)) != 1:
            continue
        code = membership[0]
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
        global_codes,
        country_codes,
    )


def clean_reference_description(desc: str, source: str) -> str:
    desc = desc.replace("\x02", " ")
    if source == "san-antonio-pcard":
        desc = re.sub(
            r"\s+I\s+I\s+I(?:\s+.*)?$",
            "",
            desc,
            flags=re.IGNORECASE,
        )
        desc = re.sub(r'\s+"?INCLUDE"?\s*$', "", desc, flags=re.IGNORECASE)
    elif source == "florida-dfs":
        desc = re.sub(r"\s+[ARP]\s*$", "", desc)
    return clean_description(desc)


def san_antonio_column_rows(text: str) -> dict[str, str]:
    """Recover San Antonio pages whose PDF reading order emits MCCs as a column."""
    recovered: dict[str, str] = {}

    for page_no, page in enumerate(text.split("\f"), start=1):
        lines = page.splitlines()
        header_index = next(
            (
                i
                for i, line in enumerate(lines)
                if "MCC DESCRIPTION" in clean_description(line).upper()
            ),
            None,
        )
        if header_index is None:
            continue

        codes = [
            line.strip()
            for line in lines[:header_index]
            if re.fullmatch(r"\d{4}", line.strip())
        ]
        if len(codes) < 5:
            continue

        descriptions: list[str] = []
        for raw in lines[header_index + 1 :]:
            line = clean_description(raw)
            if not line:
                continue
            if re.fullmatch(r"\d{4}", line):
                continue
            if (
                line.startswith("MERCHANT CATEGORY")
                or line.startswith("CODE (MCC)")
                or line.startswith("I I I")
                or line.startswith('"INCLUDE" indicates')
                or line.startswith("MCC is not restricted")
                or re.match(r"^Page \d+ of \d+", line)
            ):
                continue

            desc = clean_reference_description(line, "san-antonio-pcard")
            if len(desc) >= 3:
                descriptions.append(desc)
            if len(descriptions) >= len(codes):
                break

        if len(descriptions) != len(codes):
            raise RuntimeError(
                f"san-antonio-pcard column page {page_no} has "
                f"{len(codes)} MCCs but {len(descriptions)} descriptions"
            )

        for code, desc in zip(codes, descriptions):
            previous = recovered.get(code)
            if (
                previous
                and canonical_description(previous) != canonical_description(desc)
            ):
                raise RuntimeError(
                    f"san-antonio-pcard column conflict for MCC {code}: "
                    f"{previous!r} vs {desc!r}"
                )
            recovered[code] = desc

    return recovered


def extract_institutional_reference(text: str, source: str) -> dict[str, str]:
    """Extract MCC->description rows with conflict detection and sentinels."""
    candidates: dict[str, list[str]] = {}

    for raw in text.splitlines():
        match = re.match(r"^\s*(\d{4})\s+(.+?)\s*$", raw)
        if not match:
            continue
        code, desc = match.groups()
        desc = clean_reference_description(desc, source)
        if len(desc) < 3:
            continue
        candidates.setdefault(code, []).append(desc)

    if source == "san-antonio-pcard":
        for code, desc in san_antonio_column_rows(text).items():
            candidates.setdefault(code, []).append(desc)

    result: dict[str, str] = {}
    conflicts: dict[str, list[str]] = {}

    for code, values in candidates.items():
        unique: list[str] = []
        for value in values:
            if not any(value.casefold() == old.casefold() for old in unique):
                unique.append(value)

        if len(unique) == 1:
            result[code] = unique[0]
            continue

        # PDF extraction can repeat a row with a truncated copy. Accept only
        # prefix-compatible duplicates, preferring the longest text.
        longest = max(unique, key=len)
        if all(
            canonical_description(value) in canonical_description(longest)
            or canonical_description(longest) in canonical_description(value)
            for value in unique
        ):
            result[code] = longest
        else:
            conflicts[code] = unique

    if conflicts:
        preview = "; ".join(
            f"{code}: {values!r}"
            for code, values in list(sorted(conflicts.items()))[:5]
        )
        raise RuntimeError(f"{source} produced conflicting MCC rows: {preview}")

    minimum = MIN_REFERENCE_ENTRIES[source]
    if len(result) < minimum:
        raise RuntimeError(
            f"{source} parser found only {len(result)} MCC entries; "
            f"expected at least {minimum}"
        )

    for code, expected in REFERENCE_SENTINELS.items():
        desc = result.get(code, "").lower()
        if not desc or not any(token in desc for token in expected):
            raise RuntimeError(
                f"{source} sentinel MCC {code} missing/unexpected: {result.get(code)!r}"
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
    if a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= SECONDARY_AGREEMENT


def validate_reference_pair(
    san_antonio: dict[str, str],
    florida: dict[str, str],
) -> None:
    overlap = set(san_antonio) & set(florida)
    if len(overlap) < MIN_REFERENCE_OVERLAP:
        raise RuntimeError(
            f"Institutional MCC overlap dropped to {len(overlap)}; "
            f"expected at least {MIN_REFERENCE_OVERLAP}"
        )


def apply_secondary_consensus(
    entries: dict[str, str],
    provenance: dict[str, dict[str, str]],
    qrb_codes: set[str],
    san_antonio: dict[str, str],
    florida: dict[str, str],
) -> dict[str, dict[str, object]]:
    """Add only QRB-referenced missing MCCs confirmed by both institutions."""
    added: dict[str, dict[str, object]] = {}

    for code in sorted(qrb_codes, key=int):
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


def validate(
    entries: dict[str, str],
    global_codes: set[str],
    country_codes: set[str],
) -> None:
    if not MIN_ENTRIES <= len(entries) <= MAX_ENTRIES:
        raise RuntimeError(f"Unexpected MCC count: {len(entries)}")

    for code, expected in KNOWN.items():
        desc = entries.get(code, "").lower()
        if not desc or not any(word in desc for word in expected):
            raise RuntimeError(f"Known MCC {code} missing or unexpected: {entries.get(code)!r}")

    for code, expected_parts in COMPLETE_TITLE_CHECKS.items():
        desc = entries.get(code, "").lower()
        if not desc or not all(part in desc for part in expected_parts):
            raise RuntimeError(
                f"QRB multiline title for MCC {code} appears truncated: "
                f"{entries.get(code)!r}"
            )

    if len(global_codes) < MIN_GLOBAL_AB_CODES:
        raise RuntimeError("Global AB code count failed final validation")
    if not COUNTRY_AB_SENTINELS <= country_codes:
        raise RuntimeError("Country-specific AB sentinels failed final validation")

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
            "method": "official-pdf-structured-parser",
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
    global_codes: set[str],
    country_codes: set[str],
    san_antonio: dict[str, str],
    florida: dict[str, str],
    secondary_added: dict[str, dict[str, object]],
    output: Path,
) -> None:
    qrb_codes = global_codes | country_codes
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

    qrb_without_description = sorted(qrb_codes - set(entries), key=int)

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
        "schemaVersion": 2,
        "version": version,
        "sources": {
            "mastercardQrb": MASTERCARD_URL,
            "sanAntonioPcard": SAN_ANTONIO_URL,
            "floridaDfsPcard": FLORIDA_DFS_URL,
        },
        "counts": {
            "published": len(entries),
            "qrbGlobalAbReferenced": len(global_codes),
            "qrbCountrySpecificAbReferenced": len(country_codes),
            "qrbAnyAbReferenced": len(qrb_codes),
            "qrbReferencedWithoutDescription": len(qrb_without_description),
            "sanAntonioReference": len(san_antonio),
            "floridaDfsReference": len(florida),
            "institutionalReferenceOverlap": len(set(san_antonio) & set(florida)),
            "mastercardAbProgramAdditions": len(ab_additions),
            "secondaryConsensusAdditions": len(secondary_added),
            "secondaryConsensusNotInPublishedDb": len(secondary_only_agreements),
        },
        "mastercardAbProgramAdditions": ab_additions,
        "secondaryConsensusAdditions": secondary_added,
        "qrbReferencedWithoutDescription": qrb_without_description,
        "secondaryConsensusNotInPublishedDb": secondary_only_agreements,
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
        entries, provenance, global_codes, country_codes = extract_mastercard(
            texts["mastercard"]
        )

        san_antonio = extract_institutional_reference(
            texts["san-antonio"], "san-antonio-pcard"
        )
        florida = extract_institutional_reference(texts["florida"], "florida-dfs")
        validate_reference_pair(san_antonio, florida)

        qrb_codes = global_codes | country_codes
        secondary_added = apply_secondary_consensus(
            entries,
            provenance,
            qrb_codes,
            san_antonio,
            florida,
        )

        validate(entries, global_codes, country_codes)

        print(f"Mastercard document date: {version}")
        print(f"Published MCC entries: {len(entries)}")
        print(f"Global AB referenced MCCs: {len(global_codes)}")
        print(f"Country-specific AB referenced MCCs: {len(country_codes)}")
        print(f"Any QRB AB referenced MCCs: {len(qrb_codes)}")
        print(
            "MCC 6555:",
            entries["6555"],
            f"({provenance['6555'].get('source')})",
        )
        print(f"San Antonio parsed MCCs: {len(san_antonio)}")
        print(f"Florida DFS parsed MCCs: {len(florida)}")
        print(
            "Institutional overlap:",
            len(set(san_antonio) & set(florida)),
        )
        print(f"Secondary consensus additions: {len(secondary_added)}")

        write_database(entries, version, args.output)
        write_report(
            version=version,
            entries=entries,
            provenance=provenance,
            global_codes=global_codes,
            country_codes=country_codes,
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
