#!/usr/bin/env python3
"""Map cities/ZIPs to core 8-county region."""

# Known cities/towns in each core county (lowercase)
COUNTY_CITIES = {
    "phila": {
        "philadelphia",
    },
    "delaware": {
        "media", "chester", "springfield", "aston", "brookhaven", "concord",
        "drexel hill", "folcroft", "garnet valley", "glenolden", "haverford",
        "lansdowne", "marcus hook", "newtown square", "radnor", "ridley park",
        "sharon hill", "swarthmore", "upper darby", "wallingford", "wayne",
        "yeadon", "broomall", "havertown", "rose valley", "rutledge",
        "essington", "morton", "prospect park", "ridley", "secane", "thornton",
        "villanova", "edgmont", "darby",
    },
    "chester": {
        "west chester", "exton", "berwyn", "malvern", "frazer", "paoli",
        "phoenixville", "coatesville", "kennett square", "chesterbrook",
        "chadds ford", "devon", "downingtown", "glenmoore", "honey brook",
        "oxford", "spring city", "west grove", "lionville", "atglen",
        "avondale", "elverson", "embreeville", "kemblesville", "landenberg",
        "lincoln university", "modena", "parkesburg", "thorndale",
        "toughkenamon", "uwchland", "valley forge", "westtown",
    },
    "bucks": {
        "doylestown", "bensalem", "bristol", "buckingham", "chalfont",
        "fairless hills", "holland", "levittown", "morrisville", "new hope",
        "newtown", "perkasie", "quakertown", "sellersville", "trevose",
        "warminster", "warrington", "yardley", "langhorne", "feasterville",
        "richboro", "southampton", "wycombe", "ottsville", "pipersville",
        "pineville", "kintnersville", "ivyland",
    },
    "montgomery": {
        "norristown", "king of prussia", "plymouth meeting", "lansdale",
        "pottstown", "conshohocken", "blue bell", "audubon", "bala cynwyd",
        "wynnewood", "ardmore", "bryn mawr", "glenside", "jenkintown",
        "abington", "ambler", "cheltenham", "collegeville", "east norriton",
        "elkins park", "fort washington", "harleysville", "hatfield",
        "horsham", "huntingdon valley", "limerick", "lower gwynedd",
        "lower merion", "phoenixville", "royersford", "schwenksville",
        "skippack", "souderton", "spring house", "telford", "trappe",
        "whitemarsh", "willow grove", "worcester", "wyncote", "wyndmoor",
        "merion station", "narberth", "oreland", "penn valley", "rosemont",
        "gladwyne", "villanova",  # straddles delco
        "colmar", "chalfont",
        "flourtown", "fort washington", "gilbertsville", "green lane",
        "halfway house", "hatboro", "lederach", "marshallton",
        "north wales", "oaks", "perkiomenville", "plymouth", "pottstown",
        "salford", "sanatoga", "skippack", "spring mount", "sumneytown",
    },
    "camden": {
        "camden", "cherry hill", "voorhees", "sicklerville", "haddonfield",
        "pennsauken", "audubon", "bellmawr", "berlin", "blackwood",
        "brooklawn", "clementon", "collingswood", "gibbsboro", "gloucester city",
        "haddon", "haddon heights", "haddon township", "hi-nella",
        "laurel springs", "lawnside", "lindenwold", "magnolia", "merchantville",
        "mount ephraim", "oaklyn", "pine hill", "runnemede", "somerdale",
        "stratford", "tavistock", "waterford", "westmont", "winslow",
        "woodlynne",
    },
    "burlington": {
        "mount laurel", "marlton", "moorestown", "burlington", "cinnaminson",
        "delran", "hainesport", "maple shade", "medford", "riverside",
        "riverton", "westampton", "willingboro", "evesham", "lumberton",
        "browns mills", "bordentown", "florence", "delanco", "edgewater park",
        "mansfield", "mount holly", "new hanover", "north hanover",
        "palmyra", "pemberton", "shamong", "southampton", "springfield",
        "tabernacle", "wrightstown", "fieldsboro", "beverly", "eastampton",
    },
    "gloucester": {
        "glassboro", "mullica hill", "pitman", "sewell", "washington township",
        "west deptford", "williamstown", "woodbury", "mantua", "clayton",
        "deptford", "east greenwich", "elk", "franklin", "greenwich",
        "harrison", "logan", "monroe", "national park", "newfield",
        "paulsboro", "south harrison", "swedesboro", "westville",
        "woodbury heights", "woolwich",
    },
}

# Build reverse map: city -> county (lowercase city -> county)
CITY_TO_COUNTY = {}
for county, cities in COUNTY_CITIES.items():
    for c in cities:
        # If duplicate, prefer first
        if c not in CITY_TO_COUNTY:
            CITY_TO_COUNTY[c] = county

# Bordering / occasionally-used / harder ones
AMBIGUOUS = {
    "wayne": "delaware",  # also chester; default delco (Lincoln, BlackRock, etc.)
    "bryn mawr": "montgomery",  # also delco
    "villanova": "delaware",  # straddles
    "springfield": "delaware",  # Philly area Springfield is in delco
    "warrington": "bucks",  # could be montco border
    "phoenixville": "chester",
    "marlton": "burlington",  # Evesham township
    "audubon": "camden",  # NJ audubon — there's also a PA Audubon (montco)
}

def classify_city(city, state):
    """Return county code, or None if not in core region."""
    if not city: return None
    c = city.lower().strip()
    if state == "PA":
        # Check PA counties
        for county in ("phila", "delaware", "chester", "bucks", "montgomery"):
            if c in COUNTY_CITIES[county]:
                return county
        # Audubon disambiguation — Audubon PA is montco
        if c == "audubon" and state == "PA":
            return "montgomery"
    if state == "NJ":
        for county in ("camden", "burlington", "gloucester"):
            if c in COUNTY_CITIES[county]:
                return county
        if c == "audubon" and state == "NJ":
            return "camden"
    return None
