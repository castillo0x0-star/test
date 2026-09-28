import json
import os
import re
import ssl
from html import unescape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "players.json"
MATCHES_FILE = ROOT / "matches.json"
JSON_URL = os.environ.get("PLAYERS_JSON_URL") or os.environ.get("JSON_URL")
PORT = 8000
BCP_ROOT = "https://lrs9glzzsf.execute-api.us-east-1.amazonaws.com/prod/"
BCP_API_ROOT = "https://newprod-api.bestcoastpairings.com/v1/"
BCP_CLIENT_ID = "web-app"
K_FACTOR = 32
DEFAULT_ELO = 1500

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}

def normalize_player_name(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        first_name = value.get("firstName", "")
        last_name = value.get("lastName", "")
        if first_name or last_name:
            return " ".join(part for part in (str(first_name).strip(), str(last_name).strip()) if part)
        for key in ("name", "playerName", "displayName", "fullName", "username", "player"):
            if key in value and value[key]:
                if isinstance(value[key], dict):
                    nested_name = normalize_player_name(value[key])
                    if nested_name:
                        return nested_name
                return str(value[key]).strip()
        for nested_key in ("user", "player1", "player2"):
            if nested_key in value:
                nested_name = normalize_player_name(value[nested_key])
                if nested_name:
                    return nested_name
        return ""
    if isinstance(value, list):
        for item in value:
            candidate = normalize_player_name(item)
            if candidate:
                return candidate
        return ""
    return str(value).strip()


def normalize_placement(value, fallback_index):
    if value is None:
        return fallback_index + 1
    if isinstance(value, str):
        value = value.strip()
        if value.isdigit():
            return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        for key in ("placement", "rank", "position", "placing", "standing"):
            if key in value and value[key] is not None:
                return normalize_placement(value[key], fallback_index)
    return fallback_index + 1


def extract_metric_value(item, metric_name):
    for collection_name in ("total_metrics", "metrics", "overall_metrics"):
        metrics = item.get(collection_name)
        if not isinstance(metrics, list):
            continue
        for metric in metrics:
            if isinstance(metric, dict) and metric.get("name", "").lower() == metric_name.lower():
                return metric.get("value")
    return None


def load_match_results():
    if not MATCHES_FILE.exists():
        return [], {}, None

    try:
        with MATCHES_FILE.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        return [], {}, f"No se pudo leer matches.json: {exc}"

    if isinstance(payload, list):
        raw_matches = payload
        raw_ratings = {}
    elif isinstance(payload, dict):
        raw_matches = payload.get("matches", [])
        raw_ratings = payload.get("initial_ratings", {})
    else:
        return [], {}, "matches.json debe ser una lista o un objeto con matches."

    if not isinstance(raw_matches, list) or not isinstance(raw_ratings, dict):
        return [], {}, "matches.json debe contener una lista matches y un objeto initial_ratings."

    matches = []
    for index, raw_match in enumerate(raw_matches, start=1):
        if not isinstance(raw_match, dict):
            return [], {}, f"La partida {index} no es un objeto válido."

        player = normalize_player_name(raw_match.get("player"))
        opponent = normalize_player_name(raw_match.get("opponent"))
        if not player or not opponent:
            return [], {}, f"La partida {index} necesita player y opponent."

        try:
            match = {
                "round": int(raw_match.get("round", 1)),
                "player": player,
                "opponent": opponent,
                "player_points": float(raw_match["player_points"]),
                "opponent_points": float(raw_match["opponent_points"]),
            }
        except (KeyError, TypeError, ValueError):
            return [], {}, (
                f"La partida {index} necesita round, player_points y opponent_points numéricos."
            )
        matches.append(match)

    initial_ratings = {}
    for name, rating in raw_ratings.items():
        try:
            initial_ratings[str(name).strip().casefold()] = float(rating)
        except (TypeError, ValueError):
            return [], {}, f"El ELO inicial de {name} no es numérico."

    return matches, initial_ratings, None


def extract_players_from_payload(payload):
    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        for key in ("players", "participants", "entrants", "roster", "entries", "standings", "active", "list"):
            if key in payload and isinstance(payload[key], list):
                return payload[key]
        for nested_key in ("data", "result", "results", "payload"):
            value = payload.get(nested_key)
            if value is not None:
                nested = extract_players_from_payload(value)
                if nested:
                    return nested
    return []


def calculate_tournament_elo(participants, matches=None, initial_ratings=None):
    if not participants:
        return []

    cleaned = []
    for index, item in enumerate(participants):
        if not isinstance(item, dict):
            continue

        name = normalize_player_name(item)
        if not name:
            continue

        placement = normalize_placement(
            item.get("placement")
            or item.get("placing")
            or item.get("overallPlacing")
            or item.get("rank")
            or item.get("position")
            or item.get("standing"),
            index,
        )

        points = item.get("points")
        if points is None:
            points = 0

        games = item.get("games")
        if not isinstance(games, list):
            games = item.get("total_games")
        losses = (
            sum(1 for game in games if isinstance(game, dict) and game.get("gameResult") == 0)
            if isinstance(games, list)
            else extract_metric_value(item, "Losses")
        )

        cleaned.append({
            "name": name,
            "placement": placement,
            "points": points,
            "wins": extract_metric_value(item, "Wins"),
            "losses": losses,
        })

    if not cleaned:
        return []

    ranked = sorted(cleaned, key=lambda item: item["placement"])
    canonical_names = {player["name"].casefold(): player["name"] for player in ranked}
    initial_ratings = initial_ratings or {}
    elo = {
        player["name"]: initial_ratings.get(player["name"].casefold(), DEFAULT_ELO)
        for player in ranked
    }

    for match in sorted(matches or [], key=lambda item: item["round"]):
        player_name = canonical_names.get(match["player"].casefold())
        opponent_name = canonical_names.get(match["opponent"].casefold())
        if not player_name or not opponent_name:
            unknown_name = match["player"] if not player_name else match["opponent"]
            raise ValueError(f"No se encuentra en el roster el jugador {unknown_name!r}.")
        if player_name == opponent_name:
            raise ValueError("Un jugador no puede enfrentarse a sí mismo.")

        expected = 1 / (1 + 10 ** ((elo[opponent_name] - elo[player_name]) / 400))
        score20 = max(
            0.0,
            min(20.0, 10 + (match["player_points"] - match["opponent_points"]) / 5),
        )
        actual = score20 / 20
        change = K_FACTOR * (actual - expected)
        elo[player_name] += change
        elo[opponent_name] -= change

    players = []
    for player in ranked:
        name = player["name"]
        players.append({
            "name": name,
            "elo": round(elo[name], 2),
            "wins": player["wins"],
            "losses": player["losses"],
            "points": player["points"],
        })

    return sorted(players, key=lambda item: (-item["elo"], -(item["wins"] or 0), item["name"]))


def load_bcp_match_results(event_id):
    payload = fetch_json(
        f"{BCP_API_ROOT}pairings",
        {
            "limit": 100,
            "eventId": event_id,
            "pairingType": "Pairing",
            "expand[]": ["player1", "player2", "player1Game", "player2Game"],
        },
        {"client-id": BCP_CLIENT_ID},
    )
    pairings = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(pairings, list):
        raise ValueError("BCP no devolvió una lista de emparejamientos.")

    matches = []
    for pairing in pairings:
        if not isinstance(pairing, dict) or not pairing.get("isDone"):
            continue

        player1 = normalize_player_name(pairing.get("player1"))
        player2 = normalize_player_name(pairing.get("player2"))
        metadata = pairing.get("metaData") or {}
        game1 = pairing.get("player1Game") or {}
        game2 = pairing.get("player2Game") or {}
        points1 = game1.get("gamePoints", metadata.get("p1-gamePoints"))
        points2 = game2.get("gamePoints", metadata.get("p2-gamePoints"))
        if not player1 or not player2 or points1 is None or points2 is None:
            continue

        matches.append({
            "round": int(pairing.get("round", 1)),
            "player": player1,
            "opponent": player2,
            "player_points": float(points1),
            "opponent_points": float(points2),
        })

    if not matches:
        raise ValueError("BCP no devolvió partidas completadas con sus dos puntuaciones.")
    return matches


def fetch_json(url, params=None, headers=None):
    if params:
        url = f"{url}?{urlencode(params, doseq=True)}"

    request_headers = {**REQUEST_HEADERS, **(headers or {})}
    request = Request(url, headers=request_headers)
    with urlopen(request, timeout=20, context=ssl._create_unverified_context()) as response:
        payload = response.read().decode("utf-8")
        return json.loads(payload)


def load_players_from_json_file(matches=None, initial_ratings=None):
    if not DATA_FILE.exists():
        return None

    try:
        with DATA_FILE.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"JSON file data unavailable: {exc}")
        return None

    roster = extract_players_from_payload(payload)
    if not roster:
        return None

    try:
        calculated = calculate_tournament_elo(roster, matches, initial_ratings)
    except ValueError as exc:
        print(f"Player data unavailable: {exc}")
        return None
    if not calculated:
        return None

    return calculated


def parse_bcp_event_html(html_text):
    if not html_text:
        return []

    cleaned = re.sub(r"<script.*?</script>", " ", html_text, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<style.*?</style>", " ", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = cleaned.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    cleaned = re.sub(r"</(p|div|li|tr|td|h1|h2|h3|h4|h5|h6)>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", "\n", cleaned)
    text = unescape(cleaned)
    text = re.sub(r"\r+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    lines = []
    for line in text.splitlines():
        clean = re.sub(r"\s+", " ", line).strip()
        if clean:
            lines.append(clean)

    metadata_words = {
        "overview", "roster", "pairings", "placings", "torneo", "w40k",
        "items", "cookie", "privacy", "terms", "connect", "quick", "partners",
        "home", "events", "ticketing", "patch", "subscription", "login",
        "support", "company", "copyright", "join", "my", "warhammer"
    }

    players = []
    i = 0
    while i < len(lines) - 1:
        line = lines[i]
        if not re.fullmatch(r"\d{1,2}", line):
            i += 1
            continue

        placement = int(line)
        candidate = lines[i + 1] if i + 1 < len(lines) else ""
        if not candidate or not re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ' .-]+", candidate):
            i += 1
            continue

        candidate_lower = candidate.lower()
        if candidate_lower in metadata_words or candidate_lower.startswith("adeptus") or candidate_lower.startswith("death") or candidate_lower.startswith("space") or candidate_lower.startswith("dark") or candidate_lower.startswith("grey") or candidate_lower.startswith("chaos") or candidate_lower.startswith("thousand") or candidate_lower.startswith("tyranids") or candidate_lower.startswith("necrons") or candidate_lower.startswith("orks") or candidate_lower.startswith("aeldari") or candidate_lower.startswith("t'au"):
            i += 1
            continue

        players.append({"name": candidate.strip(), "placement": placement})
        i += 2

    dedup = []
    seen = set()
    for item in players:
        key = (item["placement"], item["name"].lower())
        if key in seen:
            continue
        seen.add(key)
        dedup.append(item)

    return dedup


def load_players_from_url(url, matches=None, initial_ratings=None):
    if not url:
        return None, None, None

    elo_message = None
    try:
        if "bestcoastpairings.com/event/" in url:
            event_id = urlparse(url).path.rstrip("/").split("/")[-1]
            payload = fetch_json(
                f"{BCP_API_ROOT}events/{event_id}/players",
                {"placings": "true"},
                {"client-id": BCP_CLIENT_ID},
            )
            if matches:
                elo_message = f"ELO calculado con {len(matches)} partidas de matches.json, K={K_FACTOR}."
            else:
                matches = load_bcp_match_results(event_id)
                elo_message = f"ELO calculado con {len(matches)} partidas de BCP, K={K_FACTOR}."
        else:
            payload = fetch_json(url)

    except Exception as exc:
        print(f"Remote JSON unavailable: {exc}")
        return None, f"No se pudo consultar la URL: {type(exc).__name__}: {exc}", None

    roster = extract_players_from_payload(payload)
    if not roster:
        return None, "La URL respondió, pero no contiene una lista de jugadores reconocida.", None

    try:
        calculated = calculate_tournament_elo(roster, matches, initial_ratings)
    except ValueError as exc:
        return None, f"No se pudo calcular el ELO: {exc}", None
    if not calculated:
        return None, "La URL respondió, pero no se pudieron interpretar sus jugadores.", None

    return calculated, None, elo_message


def load_players_from_bcp():
    try:
        events = fetch_json(BCP_ROOT + "eventlistings", {"startDate": "2020-01-01", "gameType": "1"})
        if not isinstance(events, list):
            raise ValueError("eventlistings did not return a list")

        stats = {}
        for event in events[:25]:
            event_id = event.get("eventObjId") or event.get("id") or event.get("eventId")
            if not event_id:
                continue

            pairings = fetch_json(BCP_ROOT + "pairings", {
                "eventId": event_id,
                "inclScorecard": "true",
                "inclGameSystem": "false",
                "inclEvent": "false",
            })

            if not isinstance(pairings, list):
                continue

            participants = []
            for pairing in pairings:
                p1 = normalize_player_name(pairing.get("player1"))
                p2 = normalize_player_name(pairing.get("player2"))
                if p1:
                    participants.append({"name": p1, "placement": 1})
                if p2:
                    participants.append({"name": p2, "placement": 2})

            if participants:
                calculated = calculate_tournament_elo(participants)
                for player in calculated:
                    current = stats.setdefault(player["name"], {"elo": DEFAULT_ELO, "wins": 0, "losses": 0})
                    current["elo"] = round((current["elo"] + player["elo"]) / 2)
                    current["wins"] += player["wins"]
                    current["losses"] += player["losses"]

        if stats:
            players = []
            for name, values in stats.items():
                players.append({
                    "name": name,
                    "elo": values["elo"],
                    "wins": values["wins"],
                    "losses": values["losses"],
                })
            return sorted(players, key=lambda item: (-item["elo"], -item["wins"], item["name"]))
    except Exception as exc:
        print(f"BCP data unavailable: {exc}")

    return []


def get_players_response():
    matches, initial_ratings, matches_error = load_match_results()

    if JSON_URL:
        remote_players, load_error, elo_message = load_players_from_url(JSON_URL, matches, initial_ratings)
        if remote_players:
            message = elo_message or f"ELO inicial 1500; no se aplicaron partidas."
            if matches_error:
                message = f"{message} Se ignoró matches.json: {matches_error}"
            return {"players": remote_players, "source": "tournament", "message": message}
        return {
            "players": [],
            "source": "unavailable",
            "message": load_error or "No se pudieron cargar las clasificaciones del torneo.",
        }

    local_players = load_players_from_json_file(matches, initial_ratings)
    if local_players:
        return {"players": local_players, "source": "local_json"}

    bcp_players = load_players_from_bcp()
    if bcp_players:
        return {"players": bcp_players, "source": "bcp"}
    return {
        "players": [],
        "source": "unavailable",
        "message": "No hay datos de torneo disponibles.",
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/players":
            payload = json.dumps(get_players_response()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)
            return

        if path in ("", "/"):
            file_path = ROOT / "index.html"
        else:
            file_path = ROOT / path.lstrip("/")

        if not file_path.exists() or file_path.is_dir():
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"404 - Not found")
            return

        content = file_path.read_bytes()
        mime = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(file_path.suffix.lower(), "application/octet-stream")

        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Servidor activo en http://localhost:{PORT}")
    server.serve_forever()
