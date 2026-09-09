#!/usr/bin/env python3
"""
Spansh-only ring/hotspot finder for Elite Dangerous.

Input:
  - TXT: one system per line
  - CSV: column named System / system_name / Star system, otherwise first column

Output:
  - spansh_results.csv
  - summary.json

The output preserves every input system.
For systems with hotspots, one row is written for each ring + material signal.
For rings with no hotspot signals, a row is still written with an empty material.
"""

import argparse
import csv
import json
import os
import time
from pathlib import Path

import requests

SPANSH_URL = "https://spansh.co.uk/api/bodies/search"
USER_AGENT = "Hotspots-Spansh-Only/1.2"

SYSTEM_COLUMN_CANDIDATES = {
    "system",
    "system_name",
    "system name",
    "star system",
    "star_system",
}


def norm(value):
    return " ".join((value or "").strip().lower().split())


def read_systems(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    systems = []

    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return []

            selected = None
            for name in reader.fieldnames:
                if norm(name) in SYSTEM_COLUMN_CANDIDATES:
                    selected = name
                    break

            if selected is None:
                selected = reader.fieldnames[0]

            for row in reader:
                value = (row.get(selected) or "").strip()
                if value:
                    systems.append(value)
    else:
        with path.open("r", encoding="utf-8-sig") as f:
            for line in f:
                value = line.strip()
                if value and not value.startswith("#"):
                    systems.append(value)

    # Remove duplicates while preserving original order.
    seen = set()
    unique = []
    for system in systems:
        key = norm(system)
        if key not in seen:
            seen.add(key)
            unique.append(system)

    return unique


def deduplicate_systems(systems):
    seen = set()
    unique = []

    for system in systems:
        value = str(system).strip()

        if not value:
            continue

        key = norm(value)

        if key not in seen:
            seen.add(key)
            unique.append(value)

    return unique


def fetch_systems_from_sheet(session, sheet_url, token, retries):
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            response = session.get(
                sheet_url,
                params={
                    "action": "input",
                    "token": token,
                },
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()

            if not data.get("ok"):
                raise RuntimeError(
                    data.get("error", "Apps Script returned ok=false")
                )

            systems = deduplicate_systems(data.get("systems", []))

            if not systems:
                raise RuntimeError(
                    "No systems found in Expansion strategy!F5:F"
                )

            return systems

        except Exception as exc:
            last_error = exc

            if attempt < retries:
                wait = 2 ** attempt
                print(
                    f"Sheet input request failed ({attempt}/{retries}); "
                    f"retry in {wait}s: {exc}"
                )
                time.sleep(wait)

    raise last_error


def post_results_to_sheet(
    session,
    sheet_url,
    token,
    clean_rows,
    summary,
    retries,
):
    payload = {
        "token": token,
        "rows": clean_rows,
        "summary": summary,
    }

    last_error = None

    for attempt in range(1, retries + 1):
        try:
            response = session.post(
                sheet_url,
                json=payload,
                timeout=90,
            )
            response.raise_for_status()
            data = response.json()

            if not data.get("ok"):
                raise RuntimeError(
                    data.get("error", "Apps Script returned ok=false")
                )

            return data

        except Exception as exc:
            last_error = exc

            if attempt < retries:
                wait = 2 ** attempt
                print(
                    f"Sheet output request failed ({attempt}/{retries}); "
                    f"retry in {wait}s: {exc}"
                )
                time.sleep(wait)

    raise last_error


def chunks(values, size):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def request_page(session, systems, page, page_size, retries):
    payload = {
        "filters": {
            "system_name": {
                "value": systems
            }
        },
        "size": page_size,
        "page": page,
    }

    last_error = None

    for attempt in range(1, retries + 1):
        try:
            response = session.post(
                SPANSH_URL,
                json=payload,
                timeout=90,
            )
            response.raise_for_status()
            return response.json()

        except Exception as exc:
            last_error = exc

            if attempt < retries:
                wait = 2 ** attempt
                print(
                    f"    request failed ({attempt}/{retries}); "
                    f"retry in {wait}s: {exc}"
                )
                time.sleep(wait)

    raise last_error


def fetch_batch(session, systems, delay, page_size, retries):
    bodies_by_system = {}
    page = 0

    while True:
        data = request_page(
            session=session,
            systems=systems,
            page=page,
            page_size=page_size,
            retries=retries,
        )

        bodies = data.get("results", []) or []
        total = int(data.get("count", 0) or 0)

        for body in bodies:
            system_name = (body.get("system_name") or "").strip()
            if not system_name:
                continue

            bodies_by_system.setdefault(norm(system_name), []).append(body)

        if not bodies or (page + 1) * page_size >= total:
            break

        page += 1
        time.sleep(delay)

    return bodies_by_system


def query_all_systems(session, systems, batch_size, delay, page_size, retries):
    all_bodies = {}
    unresolved = []

    batches = list(chunks(systems, batch_size))

    for batch_index, batch in enumerate(batches, 1):
        print(
            f"[{batch_index}/{len(batches)}] "
            f"Querying {len(batch)} systems..."
        )

        try:
            result = fetch_batch(
                session,
                batch,
                delay=delay,
                page_size=page_size,
                retries=retries,
            )
            all_bodies.update(result)

        except Exception as exc:
            print(f"  Batch failed after retries: {exc}")
            print("  Falling back to one-system-at-a-time queries...")

            for item_index, system in enumerate(batch, 1):
                print(f"    [{item_index}/{len(batch)}] {system}")

                try:
                    result = fetch_batch(
                        session,
                        [system],
                        delay=delay,
                        page_size=page_size,
                        retries=retries,
                    )
                    all_bodies.update(result)

                except Exception as single_exc:
                    print(f"      UNRESOLVED: {single_exc}")
                    unresolved.append(system)

                time.sleep(delay)

        if batch_index < len(batches):
            time.sleep(delay)

    return all_bodies, unresolved


def make_output_rows(systems, bodies_by_system, unresolved):
    unresolved_keys = {norm(x) for x in unresolved}
    rows = []

    for order, requested_system in enumerate(systems, 1):
        key = norm(requested_system)

        if key in unresolved_keys:
            rows.append({
                "Input Order": order,
                "System": requested_system,
                "Status": "UNKNOWN_API_ERROR",
                "Body": "",
                "Ring": "",
                "Ring Type": "",
                "Reserve Level": "",
                "LS Distance": "",
                "Material": "",
                "Hotspot Count": "",
            })
            continue

        bodies = bodies_by_system.get(key, [])

        if not bodies:
            rows.append({
                "Input Order": order,
                "System": requested_system,
                "Status": "SYSTEM_NOT_FOUND",
                "Body": "",
                "Ring": "",
                "Ring Type": "",
                "Reserve Level": "",
                "LS Distance": "",
                "Material": "",
                "Hotspot Count": "",
            })
            continue

        system_has_ring = False
        system_has_hotspot = False

        for body in bodies:
            body_name = (body.get("name") or "").strip()
            reserve = body.get("reserve_level", "")
            ls_distance = body.get("distance_to_arrival", "")
            rings = body.get("rings", []) or []

            for ring in rings:
                system_has_ring = True

                ring_name = (ring.get("name") or "").strip()
                ring_type = ring.get("type", "")
                signals = ring.get("signals", []) or []

                if not signals:
                    rows.append({
                        "Input Order": order,
                        "System": requested_system,
                        "Status": "RING_NO_HOTSPOTS",
                        "Body": body_name,
                        "Ring": ring_name,
                        "Ring Type": ring_type,
                        "Reserve Level": reserve,
                        "LS Distance": ls_distance,
                        "Material": "",
                        "Hotspot Count": "",
                    })
                    continue

                for signal in signals:
                    material = (signal.get("name") or "").strip()
                    if not material:
                        continue

                    system_has_hotspot = True

                    rows.append({
                        "Input Order": order,
                        "System": requested_system,
                        "Status": "HOTSPOT_FOUND",
                        "Body": body_name,
                        "Ring": ring_name,
                        "Ring Type": ring_type,
                        "Reserve Level": reserve,
                        "LS Distance": ls_distance,
                        "Material": material,
                        "Hotspot Count": signal.get("count", 0),
                    })

        if not system_has_ring:
            rows.append({
                "Input Order": order,
                "System": requested_system,
                "Status": "NO_RINGS",
                "Body": "",
                "Ring": "",
                "Ring Type": "",
                "Reserve Level": "",
                "LS Distance": "",
                "Material": "",
                "Hotspot Count": "",
            })

        elif system_has_ring and not system_has_hotspot:
            # Individual ring rows already exist as RING_NO_HOTSPOTS.
            pass

    return rows


def write_raw_csv(path: Path, rows):
    fields = [
        "Input Order",
        "System",
        "Status",
        "Body",
        "Ring",
        "Ring Type",
        "Reserve Level",
        "LS Distance",
        "Material",
        "Hotspot Count",
    ]

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def make_clean_rows(rows):
    """
    Human-readable version:
    - System appears only on the first row of that system block.
    - Body/Ring/Ring Type/Reserve/LS appear only on the first row
      of that ring block.
    - Material and Hotspot Count remain on every hotspot row.
    """
    clean_rows = []

    previous_system = None
    previous_ring_key = None

    for original in rows:
        row = dict(original)

        current_system = row.get("System", "")
        current_ring = row.get("Ring", "")
        current_body = row.get("Body", "")
        current_ring_key = (
            current_system,
            current_body,
            current_ring,
        )

        if current_system == previous_system:
            row["System"] = ""
            row["Input Order"] = ""
        else:
            previous_system = current_system
            previous_ring_key = None

        if current_ring and current_ring_key == previous_ring_key:
            row["Body"] = ""
            row["Ring"] = ""
            row["Ring Type"] = ""
            row["Reserve Level"] = ""
            row["LS Distance"] = ""
            row["Status"] = ""
        else:
            if current_ring:
                previous_ring_key = current_ring_key

        clean_rows.append(row)

    return clean_rows


def write_clean_csv(path: Path, clean_rows):
    fields = [
        "Input Order",
        "System",
        "Status",
        "Body",
        "Ring",
        "Ring Type",
        "Reserve Level",
        "LS Distance",
        "Material",
        "Hotspot Count",
    ]

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(clean_rows)


def build_summary(systems, rows, unresolved):
    systems_with_hotspots = {
        row["System"]
        for row in rows
        if row["Status"] == "HOTSPOT_FOUND"
    }

    systems_with_rings_no_hotspots = {
        row["System"]
        for row in rows
        if row["Status"] == "RING_NO_HOTSPOTS"
        and row["System"] not in systems_with_hotspots
    }

    systems_no_rings = {
        row["System"]
        for row in rows
        if row["Status"] == "NO_RINGS"
    }

    systems_not_found = {
        row["System"]
        for row in rows
        if row["Status"] == "SYSTEM_NOT_FOUND"
    }

    unique_rings = {
        (row["System"], row["Ring"])
        for row in rows
        if row["Ring"]
    }

    hotspot_records = [
        row
        for row in rows
        if row["Status"] == "HOTSPOT_FOUND"
    ]

    hotspot_total = sum(
        int(row["Hotspot Count"] or 0)
        for row in hotspot_records
    )

    return {
        "systems_input": len(systems),
        "systems_with_hotspots": len(systems_with_hotspots),
        "systems_with_rings_but_no_hotspots": len(
            systems_with_rings_no_hotspots
        ),
        "systems_with_no_rings": len(systems_no_rings),
        "systems_not_found": len(systems_not_found),
        "unresolved_system_queries": len(unresolved),
        "unresolved_systems": unresolved,
        "rings_returned": len(unique_rings),
        "hotspot_material_records": len(hotspot_records),
        "hotspots_total_count": hotspot_total,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Get rings and hotspot signals from Spansh for a list of Elite Dangerous systems."
    )

    parser.add_argument(
        "input",
        nargs="?",
        help="Optional TXT or CSV containing system names. "
             "If SHEET_WEBAPP_URL is set, Google Sheets is used instead.",
    )
    parser.add_argument(
        "--sheet-url",
        default=os.getenv("SHEET_WEBAPP_URL", ""),
        help="Apps Script Web App URL. Defaults to SHEET_WEBAPP_URL.",
    )
    parser.add_argument(
        "--sheet-token",
        default=os.getenv("SHEET_TOKEN", ""),
        help="Shared token. Defaults to SHEET_TOKEN.",
    )
    parser.add_argument(
        "--out",
        default="spansh_results.csv",
        help="Human-readable output CSV path",
    )
    parser.add_argument(
        "--summary",
        default="summary.json",
        help="Summary JSON path",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=25,
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.6,
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=500,
    )

    args = parser.parse_args()

    output_path = Path(args.out)
    raw_output_path = output_path.with_name(
        output_path.stem + "_raw" + output_path.suffix
    )
    summary_path = Path(args.summary)

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    })

    if args.sheet_url:
        if not args.sheet_token:
            raise SystemExit(
                "SHEET_WEBAPP_URL is set but SHEET_TOKEN is missing."
            )

        print("Reading systems from Google Sheet...")

        systems = fetch_systems_from_sheet(
            session=session,
            sheet_url=args.sheet_url,
            token=args.sheet_token,
            retries=args.retries,
        )

        input_source = "Google Sheet: Expansion strategy!F5:F"

    elif args.input:
        input_path = Path(args.input)

        systems = deduplicate_systems(
            read_systems(input_path)
        )

        input_source = str(input_path)

    else:
        raise SystemExit(
            "No input source. Set SHEET_WEBAPP_URL/SHEET_TOKEN "
            "or provide a TXT/CSV input file."
        )

    if not systems:
        raise SystemExit("No systems found.")

    print(f"Input source: {input_source}")
    print(f"Systems loaded: {len(systems)}")

    bodies_by_system, unresolved = query_all_systems(
        session=session,
        systems=systems,
        batch_size=args.batch_size,
        delay=args.delay,
        page_size=args.page_size,
        retries=args.retries,
    )

    rows = make_output_rows(
        systems=systems,
        bodies_by_system=bodies_by_system,
        unresolved=unresolved,
    )

    clean_rows = make_clean_rows(rows)

    write_raw_csv(raw_output_path, rows)
    write_clean_csv(output_path, clean_rows)

    summary = build_summary(
        systems=systems,
        rows=rows,
        unresolved=unresolved,
    )

    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if args.sheet_url:
        print()
        print("Writing results to Google Sheet...")

        result = post_results_to_sheet(
            session=session,
            sheet_url=args.sheet_url,
            token=args.sheet_token,
            clean_rows=clean_rows,
            summary=summary,
            retries=args.retries,
        )

        print(
            f"Sheet updated: "
            f"{result.get('rows_written', 0)} data rows "
            f"written to Expansion strategy!L5:T"
        )

    print()
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print()
    print(f"Clean results: {output_path.resolve()}")
    print(f"Raw results: {raw_output_path.resolve()}")
    print(f"Summary: {summary_path.resolve()}")


if __name__ == "__main__":
    main()
