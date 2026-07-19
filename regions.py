"""
regions.py — Shared region config for the suburban civil court trackers.

Imported by region_engine.py and send_local_digest.py. Single source of truth
for area definitions, zip sets, and digest flags.
"""

# ── Area definitions ──────────────────────────────────────────────────────────

AREAS = {
    "lower_merion": {
        "name": "Lower Merion and Radnor",
        "output_html": "montco_lm_dashboard.html",
        "zips": {
            "19003", "19004", "19008", "19010", "19035", "19041",
            "19066", "19072", "19073", "19083", "19085", "19087", "19096",
        },
        "zip_names": {
            "19003": "Ardmore",        "19004": "Bala Cynwyd",
            "19008": "Broomall",       "19010": "Bryn Mawr",
            "19035": "Gladwyne",       "19041": "Haverford",
            "19066": "Merion Station", "19072": "Narberth",
            "19073": "Newtown Square", "19083": "Havertown",
            "19085": "Villanova",      "19087": "Wayne",
            "19096": "Wynnewood",
        },
        "cities": set(),
        "blurb": ("Ardmore, Bala Cynwyd, Broomall, Bryn Mawr, Gladwyne, Haverford, "
                  "Havertown, Merion Station, Narberth, Newtown Square, Villanova, "
                  "Wayne and Wynnewood"),
        "in_digest": True,
        "accent": "#1b3a4b",
    },
    "greater_media": {
        "name": "Greater Media",
        "output_html": "delco_media_dashboard.html",
        "zips": {"19063", "19065", "19081", "19086", "19091"},
        "zip_names": {
            "19063": "Media", "19065": "Media", "19081": "Swarthmore",
            "19086": "Wallingford", "19091": "Media",
        },
        "cities": {"media", "swarthmore", "wallingford"},
        "blurb": ("Media (19063, 19065, 19091), Swarthmore (19081) and "
                  "Wallingford (19086)"),
        "in_digest": True,
        "accent": "#2c3e50",
    },
    "abington": {
        "name": "Abington and Cheltenham",
        "output_html": "montco_abington_dashboard.html",
        "zips": {"19001", "19006", "19012", "19027", "19038", "19046", "19090", "19095"},
        "zip_names": {
            "19001": "Abington",          "19006": "Huntingdon Valley",
            "19012": "Cheltenham",        "19027": "Elkins Park",
            "19038": "Glenside",          "19046": "Jenkintown",
            "19090": "Willow Grove",      "19095": "Wyncote",
        },
        "cities": set(),
        "blurb": ("Abington, Cheltenham, Huntingdon Valley, Elkins Park, Glenside, "
                  "Jenkintown, Willow Grove and Wyncote"),
        "in_digest": True,
        "accent": "#6a4c93",
        # Routed separately from the dsagner digest: agutman + jrohan (2026-07-18,
        # after Av approved the test email). Does not go to the dsagner group.
        "recipients": {"to": ["agutman@inquirer.com", "jrohan@inquirer.com"], "cc": []},
    },
}

UNION_ZIPS = set().union(*(a["zips"] for a in AREAS.values()))
UNION_CITIES = set().union(*(a["cities"] for a in AREAS.values()))

# ── Shared helpers ────────────────────────────────────────────────────────────

FORECLOSURE_KEYWORDS = ("mortgage foreclosure", "foreclosure", "mortgage", "ejectment")


def is_foreclosure(record):
    ct = (record.get("case_type") or "").lower()
    return any(kw in ct for kw in FORECLOSURE_KEYWORDS)


def party_in(party, zips, cities):
    if party.get("zip") in zips:
        return True
    return bool(cities and party.get("city", "").strip().lower() in cities)
