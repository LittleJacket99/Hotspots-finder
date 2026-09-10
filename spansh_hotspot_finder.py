#!/usr/bin/env python3

import csv
import json
import os
import time
from pathlib import Path

import requests


SPANSH_URL = "https://spansh.co.uk/api/bodies/search"
USER_AGENT = "Hotspots-Finder/2.5"

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


def norm_filter(value):
    return norm(value).replace("-", " ")


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
            data.get(
                "message",
                "Apps Script input error",
            )
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

    filters = data.get("filters", {}) or {}

    ring_types = {
        "icy": bool(filters.get("icy", False)),
        "metallic": bool(filters.get("metallic", False)),
        "metal rich": bool(filters.get("metal_rich", False)),
        "rocky": bool(filters.get("rocky", False)),
    }

    materials = {
        "platinum": bool(filters.get("platinum", False)),
        "bromellite": bool(filters.get("bromellite", False)),
        "monazite": bool(filters.get("monazite", False)),
    }

    only_pristine = bool(
        filters.get("only_pristine", False)
    )

    only_landables = bool(
        filters.get("only_landables", False)
    )

    if not systems:
        raise RuntimeError(
            "No systems found in Hotspots Finder!C2:C"
        )

    if not hotspots_enabled and not planets_enabled:
        raise RuntimeError(
            "Both Hotspots and Planets checkboxes are disabled."
        )

    return {
        "systems": systems,
        "hotspots_enabled": hotspots_enabled,
        "planets_enabled": planets_enabled,
        "ring_types": ring_types,
        "materials": materials,
        "only_pristine": only_pristine,
        "only_landables": only_landables,
    }


# ============================================================
# SPANSH
# ============================================================

def request_spansh_page(systems, page):
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
                body.get("system_name", "")
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
            (page + 1) * PAGE_SIZE >= total
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
                    [],
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
                            [],
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


def empty_hotspot_status(system, status):
    return {
        "System": system,
        "Status": status,
        "Body": "",
        "Ring": "",
        "Ring Type": "",
        "Reserve Level": "",
        "LS Distance": "",
        "Material": "",
        "Hotspot Count": "",
    }


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
            raw_rows.append(
                empty_hotspot_status(
                    system,
                    "UNKNOWN_API_ERROR",
                )
            )
            continue

        bodies = bodies_by_system.get(
            key,
            [],
        )

        if not bodies:
            raw_rows.append(
                empty_hotspot_status(
                    system,
                    "SYSTEM_NOT_FOUND",
                )
            )
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
                    ring.get("signals", [])
                    or []
                )

                if not signals:
                    raw_rows.append({
                        "System": system,
                        "Status": "NO_HOTSPOT / NOT SCANNED",
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
                        signal.get("name", "")
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
                        "Status": "NO_HOTSPOT / NOT SCANNED",
                        "Body": body_name,
                        "Ring": ring_name,
                        "Ring Type": ring_type,
                        "Reserve Level": reserve,
                        "LS Distance": ls_distance,
                        "Material": "",
                        "Hotspot Count": "",
                    })

        if not has_ring:
            raw_rows.append(
                empty_hotspot_status(
                    system,
                    "NO_RINGS",
                )
            )

    return raw_rows


def filter_hotspot_rows(
    systems,
    raw_rows,
    ring_types,
    materials,
    only_pristine,
):
    selected_ring_types = {
        norm_filter(name)
        for name, enabled in ring_types.items()
        if enabled
    }

    selected_materials = {
        norm_filter(name)
        for name, enabled in materials.items()
        if enabled
    }

    system_level_statuses = {
        "UNKNOWN_API_ERROR",
        "SYSTEM_NOT_FOUND",
        "NO_RINGS",
    }

    rows_by_system = {
        norm(system): []
        for system in systems
    }

    for row in raw_rows:
        rows_by_system.setdefault(
            norm(row["System"]),
            [],
        ).append(row)

    filtered = []

    for system in systems:
        source_rows = rows_by_system.get(
            norm(system),
            [],
        )

        kept = []

        for row in source_rows:
            status = row["Status"]

            if status in system_level_statuses:
                kept.append(row)
                continue

            # Ring type filter.
            if selected_ring_types:
                if (
                    norm_filter(
                        row["Ring Type"]
                    )
                    not in selected_ring_types
                ):
                    continue

            # Reserve filter.
            if only_pristine:
                if norm_filter(
                    row["Reserve Level"]
                ) != "pristine":
                    continue

            # Material filter:
            # if one or more materials are selected, only actual
            # HOTSPOT_FOUND rows matching those materials survive.
            if selected_materials:
                if status != "HOTSPOT_FOUND":
                    continue

                if (
                    norm_filter(
                        row["Material"]
                    )
                    not in selected_materials
                ):
                    continue

            kept.append(row)

        if kept:
            filtered.extend(kept)
        else:
            filtered.append(
                empty_hotspot_status(
                    system,
                    "NO_MATCHING_HOTSPOTS",
                )
            )

    return filtered


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
    "Landable",
    "LS Distance",
]


def empty_planet_status(system, status):
    return {
        "System": system,
        "Status": status,
        "Body": "",
        "Planet Type": "",
        "Landable": "",
        "LS Distance": "",
    }


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
            raw_rows.append(
                empty_planet_status(
                    system,
                    "UNKNOWN_API_ERROR",
                )
            )
            continue

        bodies = bodies_by_system.get(
            key,
            [],
        )

        if not bodies:
            raw_rows.append(
                empty_planet_status(
                    system,
                    "SYSTEM_NOT_FOUND",
                )
            )
            continue

        planets = []

        for body in bodies:
            body_type = norm(
                body.get("type", "")
            )

            if body_type != "planet":
                continue

            # Spansh Body Search uses "is_landable".
            # Keep "landable" as a fallback for compatibility.
            landable_value = body.get(
                "is_landable",
                body.get(
                    "landable",
                    None,
                ),
            )

            if landable_value is True:
                landable = "Yes"
            elif landable_value is False:
                landable = "No"
            else:
                landable = ""

            planets.append({
                "System": system,
                "Status": "PLANET_FOUND",
                "Body": str(
                    body.get("name", "")
                    or ""
                ).strip(),
                "Planet Type": str(
                    body.get("subtype", "")
                    or ""
                ).strip(),
                "Landable": landable,
                "LS Distance":
                    body.get(
                        "distance_to_arrival",
                        "",
                    ),
            })

        if planets:
            raw_rows.extend(planets)
        else:
            raw_rows.append(
                empty_planet_status(
                    system,
                    "NO_PLANETS",
                )
            )

    return raw_rows


def filter_planet_rows(
    systems,
    raw_rows,
    only_landables,
):
    if not only_landables:
        return raw_rows

    system_level_statuses = {
        "UNKNOWN_API_ERROR",
        "SYSTEM_NOT_FOUND",
        "NO_PLANETS",
    }

    rows_by_system = {
        norm(system): []
        for system in systems
    }

    for row in raw_rows:
        rows_by_system.setdefault(
            norm(row["System"]),
            [],
        ).append(row)

    filtered = []

    for system in systems:
        source_rows = rows_by_system.get(
            norm(system),
            [],
        )

        kept = []

        for row in source_rows:
            if row["Status"] in system_level_statuses:
                kept.append(row)
                continue

            if (
                row["Status"] == "PLANET_FOUND"
                and row["Landable"] == "Yes"
            ):
                kept.append(row)

        if kept:
            filtered.extend(kept)
        else:
            filtered.append(
                empty_planet_status(
                    system,
                    "NO_LANDABLE_PLANETS",
                )
            )

    return filtered


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
        and planets_enabled
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

            # One empty separator column between blocks.
            result.append(
                left + [""] + right
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
            "action": "hotspots_write",
            "values": values,
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
    config,
    hotspot_rows,
    planet_rows,
    unresolved,
):
    hotspot_records = [
        row
        for row in hotspot_rows
        if row["Status"] == "HOTSPOT_FOUND"
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
        for row in planet_rows
        if row["Status"] == "PLANET_FOUND"
    ]

    selected_ring_types = [
        name
        for name, enabled
        in config["ring_types"].items()
        if enabled
    ]

    selected_materials = [
        name
        for name, enabled
        in config["materials"].items()
        if enabled
    ]

    return {
        "systems_input":
            len(config["systems"]),

        "hotspots_enabled":
            config["hotspots_enabled"],

        "planets_enabled":
            config["planets_enabled"],

        "ring_type_filters":
            selected_ring_types,

        "material_filters":
            selected_materials,

        "only_pristine":
            config["only_pristine"],

        "only_landables":
            config["only_landables"],

        "hotspot_material_records_after_filters":
            len(hotspot_records)
            if config["hotspots_enabled"]
            else 0,

        "hotspots_total_count_after_filters":
            hotspot_total
            if config["hotspots_enabled"]
            else 0,

        "planets_found_after_filters":
            len(planets_found)
            if config["planets_enabled"]
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
    config = get_sheet_input()

    systems = config["systems"]

    print(
        f"Systems loaded: {len(systems)}"
    )

    print(
        f"Hotspots: "
        f"{config['hotspots_enabled']}"
    )

    print(
        f"Planets: "
        f"{config['planets_enabled']}"
    )

    print(
        "Ring type filters: "
        + (
            ", ".join(
                name
                for name, enabled
                in config["ring_types"].items()
                if enabled
            )
            or "ALL"
        )
    )

    print(
        "Material filters: "
        + (
            ", ".join(
                name
                for name, enabled
                in config["materials"].items()
                if enabled
            )
            or "ALL"
        )
    )

    print(
        f"Only pristine: "
        f"{config['only_pristine']}"
    )

    print(
        f"Only landables: "
        f"{config['only_landables']}"
    )

    bodies_by_system, unresolved = (
        query_all_systems(
            systems
        )
    )

    hotspot_filtered = []
    hotspot_clean = []

    planet_filtered = []
    planet_clean = []

    if config["hotspots_enabled"]:
        hotspot_raw = build_hotspot_rows(
            systems,
            bodies_by_system,
            unresolved,
        )

        hotspot_filtered = (
            filter_hotspot_rows(
                systems,
                hotspot_raw,
                config["ring_types"],
                config["materials"],
                config["only_pristine"],
            )
        )

        hotspot_clean = (
            clean_hotspot_rows(
                hotspot_filtered
            )
        )

        write_dict_csv(
            "spansh_hotspots.csv",
            HOTSPOT_HEADERS,
            hotspot_clean,
        )

    if config["planets_enabled"]:
        planet_raw = build_planet_rows(
            systems,
            bodies_by_system,
            unresolved,
        )

        planet_filtered = (
            filter_planet_rows(
                systems,
                planet_raw,
                config["only_landables"],
            )
        )

        planet_clean = (
            clean_planet_rows(
                planet_filtered
            )
        )

        write_dict_csv(
            "spansh_planets.csv",
            PLANET_HEADERS,
            planet_clean,
        )

    sheet_values = build_sheet_values(
        config["hotspots_enabled"],
        config["planets_enabled"],
        hotspot_clean,
        planet_clean,
    )

    write_matrix_csv(
        "spansh_results.csv",
        sheet_values,
    )

    summary = build_summary(
        config,
        hotspot_filtered,
        planet_filtered,
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
