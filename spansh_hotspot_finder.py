#!/usr/bin/env python3

import csv
import json
import os
import time
from pathlib import Path

import requests


SPANSH_URL = "https://spansh.co.uk/api/bodies/search"
USER_AGENT = "Hotspots-Finder/2.0"

SHEET_URL = os.getenv("SHEET_WEBAPP_URL", "").strip()

BATCH_SIZE = 25
PAGE_SIZE = 500
DELAY = 1.6
RETRIES = 3


# ============================================================
# GENERIC HELPERS
# ============================================================

def norm(value):
    return " ".join(str(value or "").strip().lower().split())


def deduplicate(values):
    seen = set()
    result = []

    for value in values:
        value = str(value or "").strip()

        if not value:
            continue

        key = norm(value)

        if key not in seen:
            seen.add(key)
            result.append(value)

    return result


def chunks(values, size):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def request_with_retries(method, url, **kwargs):
    last_error = None

    for attempt in range(1, RETRIES + 1):
        try:
            response = requests.request(
                method,
                url,
                timeout=90,
                **kwargs,
            )

            response.raise_for_status()
            return response

        except Exception as exc:
            last_error = exc

            if attempt < RETRIES:
                wait = 2 ** attempt
                print(
                    f"Request failed ({attempt}/{RETRIES}); "
                    f"retry in {wait}s: {exc}"
                )
                time.sleep(wait)

    raise last_error


# ============================================================
# GOOGLE SHEET INPUT
# ============================================================

def get_sheet_input():
    if not SHEET_URL:
        raise RuntimeError(
            "Missing GitHub secret SHEET_WEBAPP_URL"
        )

    response = request_with_retries(
        "GET",
        SHEET_URL,
        params={
            "action": "hotspots_input"
        },
    )

    data = response.json()

    if data.get("status") != "ok":
        raise RuntimeError(
            data.get("message", "Apps Script input error")
        )

    systems = deduplicate(
        data.get("systems", [])
    )

    hotspots_enabled = bool(
        data.get("hotspots", False)
    )

    planets_enabled = bool(
        data.get("planets", False)
    )

    if not systems:
        raise RuntimeError(
            "No systems found in Hotspots Finder!B2:B"
        )

    if not hotspots_enabled and not planets_enabled:
        raise RuntimeError(
            "Both Hotspots and Planets checkboxes are disabled."
        )

    return (
        systems,
        hotspots_enabled,
        planets_enabled,
    )


# ============================================================
# SPANSH
# ============================================================

def request_spansh_page(
    systems,
    page,
):
    payload = {
        "filters": {
            "system_name": {
                "value": systems
            }
        },
        "size": PAGE_SIZE,
        "page": page,
    }

    response = request_with_retries(
        "POST",
        SPANSH_URL,
        json=payload,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )

    return response.json()


def fetch_batch(systems):
    bodies_by_system = {}
    page = 0

    while True:
        data = request_spansh_page(
            systems,
            page,
        )

        bodies = (
            data.get("results", [])
            or []
        )

        total = int(
            data.get("count", 0)
            or 0
        )

        for body in bodies:
            system_name = str(
                body.get(
                    "system_name",
                    ""
                )
                or ""
            ).strip()

            if not system_name:
                continue

            bodies_by_system.setdefault(
                norm(system_name),
                []
            ).append(body)

        if (
            not bodies
            or
            (page + 1) * PAGE_SIZE
            >= total
        ):
            break

        page += 1
        time.sleep(DELAY)

    return bodies_by_system


def query_all_systems(systems):
    all_bodies = {}
    unresolved = []

    batches = list(
        chunks(
            systems,
            BATCH_SIZE,
        )
    )

    for batch_number, batch in enumerate(
        batches,
        1,
    ):
        print(
            f"[{batch_number}/{len(batches)}] "
            f"Querying {len(batch)} systems..."
        )

        try:
            result = fetch_batch(batch)

            for key, bodies in result.items():
                all_bodies.setdefault(
                    key,
                    []
                ).extend(bodies)

        except Exception as exc:
            print(
                "Batch failed after retries: "
                f"{exc}"
            )

            print(
                "Falling back to "
                "one-system-at-a-time..."
            )

            for system in batch:
                try:
                    result = fetch_batch(
                        [system]
                    )

                    for key, bodies in result.items():
                        all_bodies.setdefault(
                            key,
                            []
                        ).extend(bodies)

                except Exception as single_exc:
                    print(
                        f"UNRESOLVED: "
                        f"{system}: "
                        f"{single_exc}"
                    )

                    unresolved.append(
                        system
                    )

                time.sleep(DELAY)

        if batch_number < len(batches):
            time.sleep(DELAY)

    return all_bodies, unresolved


# ============================================================
# HOTSPOTS
# ============================================================

HOTSPOT_HEADERS = [
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


def build_hotspot_rows(
    systems,
    bodies_by_system,
    unresolved,
):
    unresolved_keys = {
        norm(x)
        for x in unresolved
    }

    raw_rows = []

    for system in systems:
        key = norm(system)

        if key in unresolved_keys:
            raw_rows.append({
                "System": system,
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

        bodies = bodies_by_system.get(
            key,
            [],
        )

        if not bodies:
            raw_rows.append({
                "System": system,
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

        has_ring = False

        for body in bodies:
            body_name = str(
                body.get("name", "")
                or ""
            ).strip()

            reserve = body.get(
                "reserve_level",
                "",
            )

            ls_distance = body.get(
                "distance_to_arrival",
                "",
            )

            rings = (
                body.get("rings", [])
                or []
            )

            for ring in rings:
                has_ring = True

                ring_name = str(
                    ring.get("name", "")
                    or ""
                ).strip()

                ring_type = ring.get(
                    "type",
                    "",
                )

                signals = (
                    ring.get(
                        "signals",
                        [],
                    )
                    or []
                )

                if not signals:
                    raw_rows.append({
                        "System": system,
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

                valid_signal = False

                for signal in signals:
                    material = str(
                        signal.get(
                            "name",
                            "",
                        )
                        or ""
                    ).strip()

                    if not material:
                        continue

                    valid_signal = True

                    raw_rows.append({
                        "System": system,
                        "Status": "HOTSPOT_FOUND",
                        "Body": body_name,
                        "Ring": ring_name,
                        "Ring Type": ring_type,
                        "Reserve Level": reserve,
                        "LS Distance": ls_distance,
                        "Material": material,
                        "Hotspot Count":
                            signal.get(
                                "count",
                                0,
                            ),
                    })

                if not valid_signal:
                    raw_rows.append({
                        "System": system,
                        "Status": "RING_NO_HOTSPOTS",
                        "Body": body_name,
                        "Ring": ring_name,
                        "Ring Type": ring_type,
                        "Reserve Level": reserve,
                        "LS Distance": ls_distance,
                        "Material": "",
                        "Hotspot Count": "",
                    })

        if not has_ring:
            raw_rows.append({
                "System": system,
                "Status": "NO_RINGS",
                "Body": "",
                "Ring": "",
                "Ring Type": "",
                "Reserve Level": "",
                "LS Distance": "",
                "Material": "",
                "Hotspot Count": "",
            })

    return raw_rows


def clean_hotspot_rows(raw_rows):
    clean_rows = []

    previous_system = None
    previous_ring_key = None

    for original in raw_rows:
        row = dict(original)

        system = row["System"]
        body = row["Body"]
        ring = row["Ring"]

        ring_key = (
            system,
            body,
            ring,
        )

        if system == previous_system:
            row["System"] = ""
        else:
            previous_system = system
            previous_ring_key = None

        if (
            ring
            and
            ring_key == previous_ring_key
        ):
            row["Status"] = ""
            row["Body"] = ""
            row["Ring"] = ""
            row["Ring Type"] = ""
            row["Reserve Level"] = ""
            row["LS Distance"] = ""

        elif ring:
            previous_ring_key = ring_key

        clean_rows.append(row)

    return clean_rows


# ============================================================
# PLANETS
# ============================================================

PLANET_HEADERS = [
    "System",
    "Status",
    "Body",
    "Planet Type",
    "LS Distance",
]


def build_planet_rows(
    systems,
    bodies_by_system,
    unresolved,
):
    unresolved_keys = {
        norm(x)
        for x in unresolved
    }

    raw_rows = []

    for system in systems:
        key = norm(system)

        if key in unresolved_keys:
            raw_rows.append({
                "System": system,
                "Status": "UNKNOWN_API_ERROR",
                "Body": "",
                "Planet Type": "",
                "LS Distance": "",
            })
            continue

        bodies = bodies_by_system.get(
            key,
            [],
        )

        if not bodies:
            raw_rows.append({
                "System": system,
                "Status": "SYSTEM_NOT_FOUND",
                "Body": "",
                "Planet Type": "",
                "LS Distance": "",
            })
            continue

        planets = []

        for body in bodies:
            body_type = norm(
                body.get("type", "")
            )

            if body_type != "planet":
                continue

            planets.append({
                "System": system,
                "Status": "PLANET_FOUND",
                "Body": str(
                    body.get(
                        "name",
                        "",
                    )
                    or ""
                ).strip(),
                "Planet Type": str(
                    body.get(
                        "subtype",
                        "",
                    )
                    or ""
                ).strip(),
                "LS Distance":
                    body.get(
                        "distance_to_arrival",
                        "",
                    ),
            })

        if planets:
            raw_rows.extend(planets)

        else:
            raw_rows.append({
                "System": system,
                "Status": "NO_PLANETS",
                "Body": "",
                "Planet Type": "",
                "LS Distance": "",
            })

    return raw_rows


def clean_planet_rows(raw_rows):
    clean_rows = []

    previous_system = None

    for original in raw_rows:
        row = dict(original)

        system = row["System"]

        if system == previous_system:
            row["System"] = ""
            row["Status"] = ""
        else:
            previous_system = system

        clean_rows.append(row)

    return clean_rows


# ============================================================
# SHEET MATRIX
# ============================================================

def row_to_values(row, headers):
    return [
        row.get(header, "")
        for header in headers
    ]


def build_sheet_values(
    hotspots_enabled,
    planets_enabled,
    hotspot_rows,
    planet_rows,
):
    hotspot_block = []
    planet_block = []

    if hotspots_enabled:
        hotspot_block = [
            HOTSPOT_HEADERS
        ] + [
            row_to_values(
                row,
                HOTSPOT_HEADERS,
            )
            for row in hotspot_rows
        ]

    if planets_enabled:
        planet_block = [
            PLANET_HEADERS
        ] + [
            row_to_values(
                row,
                PLANET_HEADERS,
            )
            for row in planet_rows
        ]

    if (
        hotspots_enabled
        and
        planets_enabled
    ):
        height = max(
            len(hotspot_block),
            len(planet_block),
        )

        result = []

        for index in range(height):
            left = (
                hotspot_block[index]
                if index < len(hotspot_block)
                else [""] * len(
                    HOTSPOT_HEADERS
                )
            )

            right = (
                planet_block[index]
                if index < len(planet_block)
                else [""] * len(
                    PLANET_HEADERS
                )
            )

            result.append(
                left + right
            )

        return result

    if hotspots_enabled:
        return hotspot_block

    return planet_block


# ============================================================
# CSV
# ============================================================

def write_dict_csv(
    path,
    headers,
    rows,
):
    with Path(path).open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=headers,
        )

        writer.writeheader()
        writer.writerows(rows)


def write_matrix_csv(
    path,
    values,
):
    with Path(path).open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        writer = csv.writer(file)
        writer.writerows(values)


# ============================================================
# GOOGLE SHEET OUTPUT
# ============================================================

def write_sheet(values):
    response = request_with_retries(
        "POST",
        SHEET_URL,
        json={
            "action":
                "hotspots_write",
            "values":
                values,
        },
    )

    data = response.json()

    if data.get("status") != "ok":
        raise RuntimeError(
            data.get(
                "message",
                "Apps Script output error",
            )
        )

    return data


# ============================================================
# SUMMARY
# ============================================================

def build_summary(
    systems,
    hotspots_enabled,
    planets_enabled,
    hotspot_raw,
    planet_raw,
    unresolved,
):
    hotspot_records = [
        row
        for row in hotspot_raw
        if row["Status"]
        == "HOTSPOT_FOUND"
    ]

    hotspot_total = sum(
        int(
            row["Hotspot Count"]
            or 0
        )
        for row in hotspot_records
    )

    planets_found = [
        row
        for row in planet_raw
        if row["Status"]
        == "PLANET_FOUND"
    ]

    return {
        "systems_input":
            len(systems),

        "hotspots_enabled":
            hotspots_enabled,

        "planets_enabled":
            planets_enabled,

        "hotspot_material_records":
            len(hotspot_records)
            if hotspots_enabled
            else 0,

        "hotspots_total_count":
            hotspot_total
            if hotspots_enabled
            else 0,

        "planets_found":
            len(planets_found)
            if planets_enabled
            else 0,

        "unresolved_system_queries":
            len(unresolved),

        "unresolved_systems":
            unresolved,
    }


# ============================================================
# MAIN
# ============================================================

def main():
    (
        systems,
        hotspots_enabled,
        planets_enabled,
    ) = get_sheet_input()

    print(
        f"Systems loaded: "
        f"{len(systems)}"
    )

    print(
        f"Hotspots: "
        f"{hotspots_enabled}"
    )

    print(
        f"Planets: "
        f"{planets_enabled}"
    )

    bodies_by_system, unresolved = (
        query_all_systems(
            systems
        )
    )

    hotspot_raw = []
    hotspot_clean = []

    planet_raw = []
    planet_clean = []

    if hotspots_enabled:
        hotspot_raw = (
            build_hotspot_rows(
                systems,
                bodies_by_system,
                unresolved,
            )
        )

        hotspot_clean = (
            clean_hotspot_rows(
                hotspot_raw
            )
        )

        write_dict_csv(
            "spansh_hotspots.csv",
            HOTSPOT_HEADERS,
            hotspot_clean,
        )

    if planets_enabled:
        planet_raw = (
            build_planet_rows(
                systems,
                bodies_by_system,
                unresolved,
            )
        )

        planet_clean = (
            clean_planet_rows(
                planet_raw
            )
        )

        write_dict_csv(
            "spansh_planets.csv",
            PLANET_HEADERS,
            planet_clean,
        )

    sheet_values = (
        build_sheet_values(
            hotspots_enabled,
            planets_enabled,
            hotspot_clean,
            planet_clean,
        )
    )

    write_matrix_csv(
        "spansh_results.csv",
        sheet_values,
    )

    summary = build_summary(
        systems,
        hotspots_enabled,
        planets_enabled,
        hotspot_raw,
        planet_raw,
        unresolved,
    )

    Path(
        "summary.json"
    ).write_text(
        json.dumps(
            summary,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print()
    print(
        json.dumps(
            summary,
            indent=2,
            ensure_ascii=False,
        )
    )

    print()
    print(
        "Writing results "
        "to Google Sheet..."
    )

    result = write_sheet(
        sheet_values
    )

    print(
        "Sheet updated: "
        f"{result.get('rows', 0)} "
        "data rows."
    )


if __name__ == "__main__":
    main()
