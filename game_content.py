# game_content.py — standaard locaties en teams voor Stadsspel Aalst
#
# Afbeeldingen bij opdrachten worden opgeslagen in static/seed_images/ en
# bij opstart automatisch gekopieerd naar static/uploads/ (zie seed_locations).

DEFAULT_LOCATIONS = [
    {
        "name": "Grote Markt",
        "emoji": "🏛️",
        "challenge": (
            'Maak hier een foto met de groep en eventueel met voorbijgangers '
            'en doe jullie beste "Zwette maan"-imitatie.'
        ),
        "arrival_hint": "Welkom bij de Zwette Maan! ",
        "answer_type": "photo",
        "secret_code": "",
        "seed_image": None,
    },
    {
        "name": "Vredeplein",
        "emoji": "🕊️",
        "challenge": (
            "Zoek dit beeld.\n"
            "Wat is de naam van de winkel hiertegenover?\n"
            "6 letters"
        ),
        "arrival_hint": "",
        "answer_type": "code",
        "secret_code": "STEPPE",
        "seed_image": "vredeplein_opdracht.jpg",
    },
    {
        "name": "Lange Zoutstraat",
        "emoji": "🧂",
        "challenge": (
            "Vind een vrouw zo mooi als die op de foto.\n"
            "Ga ermee samen met de kast op de foto."
        ),
        "arrival_hint": "",
        "answer_type": "photo",
        "secret_code": "",
        "seed_image": "zoutstraat_opdracht.jpg",
    },
    {
        "name": "Keizersplein",
        "emoji": "👑",
        "challenge": (
            "Tel de verschillende pyramides op het plein en stuur mij via een bericht "
            "de beste pyramide die jullie met jullie team kunnen bouwen."
        ),
        "arrival_hint": "",
        "answer_type": "code",
        "secret_code": "CODE4",
        "seed_image": None,
    },
    {
        "name": "Utopia",
        "emoji": "📚",
        "challenge": (
            "Maak een leuke foto bij het standbeeld.\n"
            "Neem voorbeeld aan deze:"
        ),
        "arrival_hint": "",
        "answer_type": "photo",
        "secret_code": "",
        "seed_image": "utopia_opdracht.jpg",
    },
    {
        "name": "Graanmarkt",
        "emoji": "🌾",
        "challenge": (
            "Welk dier staat er op de punt van de speer van de vrouw van het standbeeld?"
        ),
        "arrival_hint": "",
        "answer_type": "code",
        "secret_code": "LEEUW",
        "seed_image": None,
    },
    {
        "name": "Sint-Martinus kerk",
        "emoji": "⛪️",
        "challenge": (
            "Maak een foto waarop de kerk zo groot mogelijk lijkt en de fotograaf "
            "en andere mensen van de groep zo klein mogelijk."
        ),
        "arrival_hint": "",
        "answer_type": "photo",
        "secret_code": "",
        "seed_image": None,
    },
    {
        "name": "Pieter Van Aelst Gallerij",
        "emoji": "🛍️",
        "challenge": (
            "De echte mode-straat van Aalst. Maak een foto van een modeshow door de straat."
        ),
        "arrival_hint": "",
        "answer_type": "photo",
        "secret_code": "",
        "seed_image": None,
    },
    {
        "name": "Oude Stadhuis",
        "emoji": "⛩️",
        "challenge": (
            "Zoek ons Ondinneke.\n"
            "Maak er een gezellige foto mee."
        ),
        "arrival_hint": "",
        "answer_type": "photo",
        "secret_code": "",
        "seed_image": "oudstadhuis_opdracht.jpg",
    },
]

TEAM_COLORS = [
    {"name": "Team Rood",      "color": "#e74c3c", "bg": "#fdecea", "label": "rood"},
    {"name": "Team Blauw",     "color": "#2980b9", "bg": "#eaf4fb", "label": "blauw"},
    {"name": "Team Groen",     "color": "#27ae60", "bg": "#eafaf1", "label": "groen"},
    {"name": "Team Geel",      "color": "#d4ac0d", "bg": "#fefde7", "label": "geel"},
    {"name": "Team Paars",     "color": "#8e44ad", "bg": "#f5eef8", "label": "paars"},
    {"name": "Team Oranje",    "color": "#ca6f1e", "bg": "#fdf2e9", "label": "oranje"},
    {"name": "Team Roze",      "color": "#cb4335", "bg": "#fdedec", "label": "roze"},
    {"name": "Team Turquoise", "color": "#17a589", "bg": "#e8f8f5", "label": "turquoise"},
    {"name": "Team Grijs",     "color": "#616a6b", "bg": "#f2f3f4", "label": "grijs"},
    {"name": "Team Bruin",     "color": "#784212", "bg": "#fdf5e6", "label": "bruin"},
]

FINISH_MESSAGE = (
    "🎉 Jullie hebben alle locaties bezocht! Keer terug naar het startpunt "
    "om als winnaars binnen te komen. Tot zo meteen!"
)
