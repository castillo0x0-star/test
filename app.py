import hashlib
import json
import os
import re
import sqlite3
import ssl
import time as time_module
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html import unescape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "players.json"
MATCHES_FILE = ROOT / "matches.json"
CACHE_DB_FILE = ROOT / "ranking_cache.sqlite3"
JSON_URL = os.environ.get("PLAYERS_JSON_URL") or os.environ.get("JSON_URL")
PORT = 8000
BCP_ROOT = "https://lrs9glzzsf.execute-api.us-east-1.amazonaws.com/prod/"
BCP_API_ROOT = "https://newprod-api.bestcoastpairings.com/v1/"
BCP_V2_API_ROOT = "https://newprod-api.bestcoastpairings.com/v2/"
BCP_CLIENT_ID = "web-app"
BCP_GAME_SYSTEM_ID = "WGMSzfKFYA"
RANKING_START_YEAR = 2025
BCP_SEARCH_AREAS = (
    {"center": {"lat": 40.2, "long": -3.6}, "distance": 700},
    {"center": {"lat": 28.1, "long": -15.5}, "distance": 350},
)
SPAIN_COUNTRY_NAMES = {"es", "españa", "spain"}
K_FACTOR = 32
DEFAULT_ELO = 1700
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
RANKING_CACHE_VERSION = 6
PLAYERS_CACHE = {"key": None, "expires_at": 0, "payload": None}

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

    cleaned_by_name = {}
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

        candidate = {
            "name": name,
            "placement": placement,
            "points": points,
            "wins": extract_metric_value(item, "Wins"),
            "losses": losses,
        }
        name_key = name.casefold()
        existing = cleaned_by_name.get(name_key)
        if existing:
            existing["placement"] = min(existing["placement"], placement)
            existing["points"] += points or 0
            if candidate["wins"] is not None:
                existing["wins"] = (existing["wins"] or 0) + candidate["wins"]
            if candidate["losses"] is not None:
                existing["losses"] = (existing["losses"] or 0) + candidate["losses"]
        else:
            cleaned_by_name[name_key] = candidate

    if not cleaned_by_name:
        return []

    ranked = sorted(cleaned_by_name.values(), key=lambda item: item["placement"])
    canonical_names = {player["name"].casefold(): player["name"] for player in ranked}
    initial_ratings = initial_ratings or {}
    elo = {
        player["name"]: initial_ratings.get(player["name"].casefold(), DEFAULT_ELO)
        for player in ranked
    }

    for match in sorted(matches or [], key=lambda item: (item.get("event_date", ""), item["round"])):
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


def fetch_bcp_pairings(params):
    pairings_by_id = {}
    next_key = None
    for _ in range(100):
        page_params = {
            **params,
            "limit": 100,
            "pairingType": "Pairing",
            "expand[]": ["player1", "player2", "player1Game", "player2Game"],
        }
        if next_key:
            page_params["nextKey"] = next_key
        payload = fetch_json(
            f"{BCP_API_ROOT}pairings",
            page_params,
            {"client-id": BCP_CLIENT_ID},
        )
        page = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(page, list):
            raise ValueError("BCP no devolvió una lista de emparejamientos.")
        added_pairing = False
        for pairing in page:
            pairing_id = pairing.get("id") or pairing.get("pairingId")
            if pairing_id and pairing_id not in pairings_by_id:
                pairings_by_id[pairing_id] = pairing
                added_pairing = True
        if not added_pairing:
            break
        updated_next_key = payload.get("nextKey")
        if not updated_next_key or updated_next_key == next_key:
            break
        next_key = updated_next_key
    return list(pairings_by_id.values())


def pairing_to_match(pairing):
    if not isinstance(pairing, dict) or not pairing.get("isDone"):
        return None

    player1 = normalize_player_name(pairing.get("player1"))
    player2 = normalize_player_name(pairing.get("player2"))
    metadata = pairing.get("metaData") or {}
    game1 = pairing.get("player1Game") or {}
    game2 = pairing.get("player2Game") or {}
    points1 = metadata.get("p1-victoryPoints")
    points2 = metadata.get("p2-victoryPoints")
    if points1 is None:
        points1 = game1.get("totalMoVVictoryPoints")
    if points2 is None:
        points2 = game2.get("totalMoVVictoryPoints")
    if points1 is None:
        points1 = game1.get("gamePoints", metadata.get("p1-gamePoints"))
    if points2 is None:
        points2 = game2.get("gamePoints", metadata.get("p2-gamePoints"))
    if (
        not player1
        or not player2
        or player1.casefold() == player2.casefold()
        or points1 is None
        or points2 is None
    ):
        return None

    try:
        points1 = float(points1)
        points2 = float(points2)
    except (TypeError, ValueError):
        return None

    return {
        "round": int(pairing.get("round", 1)),
        "player": player1,
        "opponent": player2,
        "player_points": points1,
        "opponent_points": points2,
    }


def load_bcp_match_results(event_id, participants=None):
    pairings = fetch_bcp_pairings({"eventId": event_id})
    if not pairings and participants:
        pairings_by_id = {}
        participants_with_games = [
            participant
            for participant in participants
            if participant.get("id") and participant.get("games")
        ]
        if participants_with_games:
            with ThreadPoolExecutor(max_workers=min(4, len(participants_with_games))) as executor:
                futures = {
                    executor.submit(fetch_bcp_pairings, {"playerId": participant["id"]}): participant["id"]
                    for participant in participants_with_games
                }
                for future in as_completed(futures):
                    player_id = futures[future]
                    try:
                        player_pairings = future.result()
                    except Exception as exc:
                        print(f"BCP player pairings unavailable ({player_id}): {exc}")
                        continue
                    for pairing in player_pairings:
                        if pairing.get("eventId") != event_id:
                            continue
                        pairing_id = pairing.get("id") or pairing.get("pairingId")
                        if pairing_id:
                            pairings_by_id[pairing_id] = pairing
        pairings = list(pairings_by_id.values())

    matches = []
    for pairing in pairings:
        match = pairing_to_match(pairing)
        if match:
            matches.append(match)

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
            roster = extract_players_from_payload(payload)
            if not roster:
                return None, "La URL respondió, pero no contiene una lista de jugadores reconocida.", None
            if matches:
                elo_message = f"ELO calculado con {len(matches)} partidas de matches.json, K={K_FACTOR}."
            else:
                matches = load_bcp_match_results(event_id, roster)
                elo_message = f"ELO calculado con {len(matches)} partidas de BCP, K={K_FACTOR}."
        else:
            payload = fetch_json(url)
            roster = extract_players_from_payload(payload)

    except Exception as exc:
        print(f"Remote JSON unavailable: {exc}")
        return None, f"No se pudo consultar la URL: {type(exc).__name__}: {exc}", None

    if not roster:
        return None, "La URL respondió, pero no contiene una lista de jugadores reconocida.", None

    try:
        calculated = calculate_tournament_elo(roster, matches, initial_ratings)
    except ValueError as exc:
        return None, f"No se pudo calcular el ELO: {exc}", None
    if not calculated:
        return None, "La URL respondió, pero no se pudieron interpretar sus jugadores.", None

    return calculated, None, elo_message


def ranking_period_window():
    try:
        local_zone = ZoneInfo("Europe/Madrid")
    except ZoneInfoNotFoundError:
        local_zone = datetime.now().astimezone().tzinfo or timezone.utc

    local_now = datetime.now(local_zone)
    first_day = datetime(RANKING_START_YEAR, 1, 1, tzinfo=local_zone)
    start_utc = first_day.astimezone(timezone.utc)
    end_utc = local_now.astimezone(timezone.utc)
    return RANKING_START_YEAR, local_now.year, local_zone, local_now, start_utc, end_utc


def load_bcp_spain_events(start_year, local_zone, cutoff_local, start_utc, end_utc):
    base_params = {
        "limit": 100,
        "startDate": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDate": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sortKey": "eventDate",
        "sortAscending": "true",
        "sortAsc": "true",
        "gameSystemId": BCP_GAME_SYSTEM_ID,
        "excludeOnline": "true",
        "distanceType": "kms",
        "eventStatus": "all",
    }

    events_by_id = {}
    for area in BCP_SEARCH_AREAS:
        next_key = None
        for _ in range(100):
            params = dict(base_params)
            params["location"] = json.dumps({
                **area,
                "distanceType": "kms",
            }, separators=(",", ":"))
            if next_key:
                params["nextKey"] = next_key
            payload = fetch_json(f"{BCP_V2_API_ROOT}events", params, {"client-id": BCP_CLIENT_ID})
            page_events = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(page_events, list) or not page_events:
                break

            for event in page_events:
                country = str((event.get("location") or {}).get("country", "")).strip().casefold()
                if event.get("isOnlineEvent") or country not in SPAIN_COUNTRY_NAMES:
                    continue
                start_value = (event.get("dates") or {}).get("start")
                if not start_value:
                    continue
                try:
                    event_start = datetime.fromisoformat(start_value.replace("Z", "+00:00"))
                    event_start = event_start.astimezone(local_zone)
                except (TypeError, ValueError):
                    continue
                if event_start.year < start_year or event_start > cutoff_local:
                    continue
                if event.get("id"):
                    events_by_id[event["id"]] = event

            new_next_key = payload.get("nextKey") if isinstance(payload, dict) else None
            if not new_next_key or new_next_key == next_key:
                break
            next_key = new_next_key

    return sorted(
        events_by_id.values(),
        key=lambda event: (event.get("dates") or {}).get("start", ""),
    )


def load_bcp_event_data(event):
    event_id = event["id"]
    participants = []
    matches = []
    for attempt in range(3):
        try:
            player_payload = fetch_json(
                f"{BCP_API_ROOT}events/{event_id}/players",
                {"placings": "true"},
                {"client-id": BCP_CLIENT_ID},
            )
            participants = extract_players_from_payload(player_payload)
            matches = load_bcp_match_results(event_id, participants)
            break
        except Exception as exc:
            if isinstance(exc, HTTPError) and 400 <= exc.code < 500 and exc.code != 429:
                raise
            if attempt == 2:
                raise
            time_module.sleep(0.5 * (attempt + 1))

    event_date = (event.get("dates") or {}).get("start", "")
    for match in matches:
        match["event_date"] = event_date
    return participants, matches


def load_players_from_spain_since_2025(matches_override, initial_ratings):
    start_year, end_year, local_zone, cutoff_local, start_utc, end_utc = ranking_period_window()
    events = load_bcp_spain_events(start_year, local_zone, cutoff_local, start_utc, end_utc)
    if not events:
        return [], (
            f"No se encontraron torneos presenciales de Warhammer 40.000 en España "
            f"desde {start_year} hasta hoy."
        )

    participants = []
    event_matches = []
    failed_events = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(load_bcp_event_data, event): event for event in events}
        for future in as_completed(futures):
            event = futures[future]
            try:
                event_participants, matches = future.result()
            except Exception as exc:
                failed_events.append(
                    f"{event.get('name', event.get('id', 'evento'))}: {type(exc).__name__}: {exc}"
                )
                print(f"BCP event unavailable ({event.get('id')}): {exc}")
                continue
            participants.extend(event_participants)
            event_matches.extend(matches)

    matches = matches_override or event_matches
    known_names = {normalize_player_name(item).casefold() for item in participants if normalize_player_name(item)}
    for match in matches:
        for name in (match["player"], match["opponent"]):
            if name.casefold() not in known_names:
                participants.append({"name": name, "placement": 999})
                known_names.add(name.casefold())

    try:
        players = calculate_tournament_elo(participants, matches, initial_ratings)
    except ValueError as exc:
        return [], f"No se pudo calcular el ELO: {exc}"

    if not players:
        return [], "Los torneos no devolvieron jugadores para calcular el ELO."

    month_names = (
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    )
    month_name = month_names[cutoff_local.month - 1]
    period_label = f"desde enero de {start_year} hasta el {cutoff_local.day} de {month_name} de {end_year}"
    if matches_override:
        message = (
            f"ELO calculado con {len(matches)} partidas de matches.json; "
            f"roster de {len(events)} torneos presenciales de España, {period_label}."
        )
    elif matches:
        message = (
            f"ELO calculado con {len(matches)} partidas de {len(events)} torneos presenciales "
            f"de España, {period_label}, K={K_FACTOR}."
        )
    else:
        message = (
            f"Sin partidas completadas; ELO inicial {DEFAULT_ELO} para {len(players)} jugadores, "
            f"{period_label}."
        )
    if failed_events:
        message += f" Error en {len(failed_events)} torneos: {'; '.join(failed_events)}."
    return players, message


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


def players_cache_key(cache_date=None):
    try:
        local_zone = ZoneInfo("Europe/Madrid")
    except ZoneInfoNotFoundError:
        local_zone = datetime.now().astimezone().tzinfo or timezone.utc

    try:
        matches_mtime = MATCHES_FILE.stat().st_mtime_ns
    except OSError:
        matches_mtime = None

    settings = {
        "source": JSON_URL or "bcp_spain",
        "date": cache_date or datetime.now(local_zone).date().isoformat(),
        "ranking_start_year": RANKING_START_YEAR,
        "ranking_cache_version": RANKING_CACHE_VERSION,
        "default_elo": DEFAULT_ELO,
        "game_system": BCP_GAME_SYSTEM_ID,
        "areas": BCP_SEARCH_AREAS,
        "k_factor": K_FACTOR,
        "matches_mtime": matches_mtime,
    }
    serialized = json.dumps(settings, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def load_persisted_players(cache_key):
    try:
        with sqlite3.connect(CACHE_DB_FILE, timeout=10) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS player_cache ("
                "cache_key TEXT PRIMARY KEY, updated_at REAL NOT NULL, payload TEXT NOT NULL)"
            )
            row = connection.execute(
                "SELECT updated_at, payload FROM player_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if not row or time_module.time() - row[0] >= CACHE_TTL_SECONDS:
                try:
                    local_zone = ZoneInfo("Europe/Madrid")
                except ZoneInfoNotFoundError:
                    local_zone = datetime.now().astimezone().tzinfo or timezone.utc
                today = datetime.now(local_zone).date()
                for days_back in range(1, 8):
                    previous_date = (today - timedelta(days=days_back)).isoformat()
                    previous_key = players_cache_key(previous_date)
                    if previous_key == cache_key:
                        continue
                    row = connection.execute(
                        "SELECT updated_at, payload FROM player_cache WHERE cache_key = ?",
                        (previous_key,),
                    ).fetchone()
                    if row and time_module.time() - row[0] < CACHE_TTL_SECONDS:
                        break
        if row and time_module.time() - row[0] < CACHE_TTL_SECONDS:
            return json.loads(row[1])
    except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"Persistent ranking cache unavailable: {exc}")
    return None


def save_persisted_players(cache_key, payload):
    try:
        serialized = json.dumps(payload, ensure_ascii=False)
        with sqlite3.connect(CACHE_DB_FILE, timeout=10) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS player_cache ("
                "cache_key TEXT PRIMARY KEY, updated_at REAL NOT NULL, payload TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT OR REPLACE INTO player_cache (cache_key, updated_at, payload) VALUES (?, ?, ?)",
                (cache_key, time_module.time(), serialized),
            )
    except (OSError, sqlite3.Error) as exc:
        print(f"Could not save persistent ranking cache: {exc}")


def get_players_response():
    now = time_module.monotonic()
    cache_key = players_cache_key()
    if (
        PLAYERS_CACHE["key"] == cache_key
        and PLAYERS_CACHE["payload"] is not None
        and now < PLAYERS_CACHE["expires_at"]
    ):
        return PLAYERS_CACHE["payload"]

    cached_response = load_persisted_players(cache_key)
    if cached_response is not None:
        cached_default_elo = cached_response.get("default_elo", 1500)
        if cached_default_elo != DEFAULT_ELO:
            rating_adjustment = DEFAULT_ELO - cached_default_elo
            for player in cached_response.get("players", []):
                player["elo"] = round(float(player["elo"]) + rating_adjustment, 2)
            cached_response["default_elo"] = DEFAULT_ELO
            save_persisted_players(cache_key, cached_response)
        PLAYERS_CACHE.update({
            "key": cache_key,
            "expires_at": now + CACHE_TTL_SECONDS,
            "payload": cached_response,
        })
        return cached_response

    matches, initial_ratings, matches_error = load_match_results()

    if JSON_URL:
        remote_players, load_error, elo_message = load_players_from_url(JSON_URL, matches, initial_ratings)
        if remote_players:
            message = elo_message or f"ELO inicial 1500; no se aplicaron partidas."
            if matches_error:
                message = f"{message} Se ignoró matches.json: {matches_error}"
            response = {"players": remote_players, "source": "tournament", "message": message}
        else:
            response = {
                "players": [],
                "source": "unavailable",
                "message": load_error or "No se pudieron cargar las clasificaciones del torneo.",
            }
    else:
        players, message = load_players_from_spain_since_2025(matches, initial_ratings)
        if matches_error:
            message += f" Se ignoró matches.json: {matches_error}"
        response = {
            "players": players,
            "source": "bcp_spain" if players else "unavailable",
            "message": message,
        }

    response["default_elo"] = DEFAULT_ELO
    if response.get("players"):
        save_persisted_players(cache_key, response)
    PLAYERS_CACHE.update({
        "key": cache_key,
        "expires_at": time_module.monotonic() + CACHE_TTL_SECONDS,
        "payload": response,
    })
    return response


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
