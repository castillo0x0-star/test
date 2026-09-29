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
GAMES_COUNT_CACHE_VERSION = 1
RANKING_CACHE_VERSION = 10
BCP_SEARCH_AREAS = (
    {"center": {"lat": 40.2, "long": -3.6}, "distance": 700},
    {"center": {"lat": 28.1, "long": -15.5}, "distance": 350},
)
SPAIN_COUNTRY_NAMES = {"es", "españa", "spain"}
K_FACTOR = 32
DEFAULT_ELO = 1700
CACHE_MIGRATION_LOOKBACK_DAYS = 30
INCREMENTAL_DELAY_DAYS = 7
INCREMENTAL_EVENT_LOOKBACK_DAYS = 30

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


def normalize_player_identity(item, name):
    user_id = item.get("userId")
    if not user_id and isinstance(item.get("user"), dict):
        user_id = item["user"].get("id")
    if user_id:
        return f"user:{str(user_id).strip()}"
    return f"name:{name.casefold()}"


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

    cleaned_by_identity = {}
    identity_by_name = {}
    for index, item in enumerate(participants):
        if not isinstance(item, dict):
            continue

        name = normalize_player_name(item)
        if not name:
            continue
        identity = normalize_player_identity(item, name)

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
        games_played = len(games) if isinstance(games, list) else None
        losses = (
            sum(1 for game in games if isinstance(game, dict) and game.get("gameResult") == 0)
            if isinstance(games, list)
            else extract_metric_value(item, "Losses")
        )

        candidate = {
            "identity": identity,
            "name": name,
            "placement": placement,
            "event_date": item.get("_event_date", ""),
            "points": points,
            "wins": extract_metric_value(item, "Wins"),
            "losses": losses,
            "games_played": games_played,
        }
        existing = cleaned_by_identity.get(identity)
        if existing:
            existing["placement"] = min(existing["placement"], placement)
            existing["points"] += points or 0
            if candidate["event_date"] >= existing["event_date"]:
                existing["name"] = name
                existing["event_date"] = candidate["event_date"]
            if candidate["wins"] is not None:
                existing["wins"] = (existing["wins"] or 0) + candidate["wins"]
            if candidate["losses"] is not None:
                existing["losses"] = (existing["losses"] or 0) + candidate["losses"]
            if candidate["games_played"] is not None:
                existing["games_played"] = (existing["games_played"] or 0) + candidate["games_played"]
        else:
            cleaned_by_identity[identity] = candidate
        identity_by_name.setdefault(name.casefold(), identity)

    if not cleaned_by_identity:
        return []

    ranked = sorted(cleaned_by_identity.values(), key=lambda item: item["placement"])
    initial_ratings = initial_ratings or {}
    elo = {
        player["identity"]: initial_ratings.get(
            player["identity"],
            initial_ratings.get(player["name"].casefold(), DEFAULT_ELO),
        )
        for player in ranked
    }
    match_counts = {player["identity"]: 0 for player in ranked}

    for match in sorted(matches or [], key=lambda item: (item.get("event_date", ""), item["round"])):
        player_identity = (
            f"user:{match['player_id']}" if match.get("player_id") else None
        )
        opponent_identity = (
            f"user:{match['opponent_id']}" if match.get("opponent_id") else None
        )
        if player_identity not in elo:
            player_identity = identity_by_name.get(match["player"].casefold())
        if opponent_identity not in elo:
            opponent_identity = identity_by_name.get(match["opponent"].casefold())
        if player_identity not in elo or opponent_identity not in elo:
            unknown_name = match["player"] if player_identity not in elo else match["opponent"]
            raise ValueError(f"No se encuentra en el roster el jugador {unknown_name!r}.")
        if player_identity == opponent_identity:
            raise ValueError("Un jugador no puede enfrentarse a sí mismo.")

        expected = 1 / (1 + 10 ** ((elo[opponent_identity] - elo[player_identity]) / 400))
        score20 = max(
            0.0,
            min(20.0, 10 + (match["player_points"] - match["opponent_points"]) / 5),
        )
        actual = score20 / 20
        change = K_FACTOR * (actual - expected)
        elo[player_identity] += change
        elo[opponent_identity] -= change
        match_counts[player_identity] += 1
        match_counts[opponent_identity] += 1

    players = []
    for player in ranked:
        identity = player["identity"]
        players.append({
            "name": player["name"],
            "user_id": (
                player["identity"].split(":", 1)[1]
                if player["identity"].startswith("user:")
                else None
            ),
            "elo": round(elo[identity], 2),
            "games_played": (
                match_counts[identity]
                if matches
                else player["games_played"] or 0
            ),
            "points": player["points"],
        })

    return sorted(players, key=lambda item: (-item["elo"], -item["games_played"], item["name"]))


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

    player1_data = pairing.get("player1") or {}
    player2_data = pairing.get("player2") or {}
    player1 = normalize_player_name(player1_data)
    player2 = normalize_player_name(player2_data)
    player1_id = str(player1_data.get("userId") or "").strip() or None
    player2_id = str(player2_data.get("userId") or "").strip() or None
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
        or (player1_id and player1_id == player2_id)
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
        "pairing_id": pairing.get("id") or pairing.get("pairingId"),
        "event_id": pairing.get("eventId"),
        "player": player1,
        "opponent": player2,
        "player_id": player1_id,
        "opponent_id": player2_id,
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


def report_progress(progress_callback, message):
    if progress_callback:
        progress_callback(message)


def load_bcp_spain_events(start_year, local_zone, cutoff_local, start_utc, end_utc, progress_callback=None):
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
    for area_index, area in enumerate(BCP_SEARCH_AREAS, start=1):
        next_key = None
        page_number = 0
        for _ in range(100):
            page_number += 1
            params = dict(base_params)
            params["location"] = json.dumps({
                **area,
                "distanceType": "kms",
            }, separators=(",", ":"))
            if next_key:
                params["nextKey"] = next_key
            payload = fetch_json(f"{BCP_V2_API_ROOT}events", params, {"client-id": BCP_CLIENT_ID})
            report_progress(
                progress_callback,
                f"Eventos: zona {area_index}/{len(BCP_SEARCH_AREAS)}, página {page_number}...",
            )
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

    sorted_events = sorted(
        events_by_id.values(),
        key=lambda event: (event.get("dates") or {}).get("start", ""),
    )
    report_progress(progress_callback, f"Eventos encontrados: {len(sorted_events)}.")
    return sorted_events


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
    for participant in participants:
        if isinstance(participant, dict):
            participant["_event_date"] = event_date
    for match in matches:
        match["event_date"] = event_date
    return participants, matches


def load_bcp_roster_game_counts(progress_callback=None):
    start_year, end_year, local_zone, cutoff_local, start_utc, end_utc = ranking_period_window()
    events = load_bcp_spain_events(start_year, local_zone, cutoff_local, start_utc, end_utc)
    counts = {}
    failed_events = 0

    def fetch_event_counts(event):
        event_id = event["id"]
        for attempt in range(3):
            try:
                payload = fetch_json(
                    f"{BCP_API_ROOT}events/{event_id}/players",
                    {"placings": "true"},
                    {"client-id": BCP_CLIENT_ID},
                )
                counts_for_event = {}
                for participant in extract_players_from_payload(payload):
                    name = normalize_player_name(participant)
                    games = participant.get("games")
                    if not isinstance(games, list):
                        games = participant.get("total_games")
                    if name and isinstance(games, list):
                        key = name.casefold()
                        counts_for_event[key] = counts_for_event.get(key, 0) + len(games)
                return counts_for_event
            except Exception as exc:
                if isinstance(exc, HTTPError) and 400 <= exc.code < 500 and exc.code != 429:
                    raise
                if attempt == 2:
                    raise
                time_module.sleep(0.5 * (attempt + 1))

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_event_counts, event): event for event in events}
        completed = 0
        for future in as_completed(futures):
            completed += 1
            try:
                event_counts = future.result()
            except Exception as exc:
                failed_events += 1
                print(f"BCP roster game counts unavailable ({futures[future].get('id')}): {exc}")
                continue
            for name, count in event_counts.items():
                counts[name] = counts.get(name, 0) + count
            if completed == 1 or completed % 25 == 0 or completed == len(events):
                report_progress(progress_callback, f"Rosters consultados: {completed}/{len(events)}.")

    return counts, len(events) - failed_events, failed_events


def ranking_data_through(response):
    message = response.get("message", "")
    month_numbers = {
        name: index
        for index, name in enumerate((
            "enero", "febrero", "marzo", "abril", "mayo", "junio",
            "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
        ), start=1)
    }
    match = re.search(r"hasta el (\d{1,2}) de ([a-záéíóúñ]+) de (\d{4})", message, re.IGNORECASE)
    if match:
        day, month_name, year = match.groups()
        month = month_numbers.get(month_name.casefold())
        if month:
            try:
                zone = ZoneInfo("Europe/Madrid")
            except ZoneInfoNotFoundError:
                zone = datetime.now().astimezone().tzinfo or timezone.utc
            return datetime(int(year), month, int(day), 23, 59, 59, tzinfo=zone)

    cached_at = response.get("cached_at")
    if cached_at:
        try:
            cached_time = datetime.fromisoformat(cached_at)
            if cached_time.tzinfo is None:
                cached_time = cached_time.replace(tzinfo=timezone.utc)
            return cached_time.astimezone(ZoneInfo("Europe/Madrid")) - timedelta(days=1)
        except (TypeError, ValueError):
            pass

    try:
        local_zone = ZoneInfo("Europe/Madrid")
    except ZoneInfoNotFoundError:
        local_zone = datetime.now().astimezone().tzinfo or timezone.utc
    return datetime.now(local_zone) - timedelta(days=1)


def ensure_incremental_tables(connection):
    connection.execute(
        "CREATE TABLE IF NOT EXISTS incremental_state ("
        "state_key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS incremental_pairings ("
        "pairing_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, applied_at REAL NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS incremental_pending_events ("
        "event_id TEXT PRIMARY KEY, event_json TEXT NOT NULL)"
    )


def load_incremental_identity_directory(events, progress_callback=None):
    ids_by_name = {}
    latest_name_by_id = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(fetch_json,
                f"{BCP_API_ROOT}events/{event['id']}/players",
                {"placings": "true"},
                {"client-id": BCP_CLIENT_ID},
            ): event
            for event in events
        }
        completed = 0
        for future in as_completed(futures):
            completed += 1
            event = futures[future]
            event_date = (event.get("dates") or {}).get("start", "")
            try:
                participants = extract_players_from_payload(future.result())
            except Exception as exc:
                print(f"BCP identity bootstrap unavailable ({event.get('id')}): {exc}")
                if completed == 1 or completed % 25 == 0 or completed == len(events):
                    report_progress(progress_callback, f"Rosters para asociar IDs: {completed}/{len(events)}.")
                continue

            for participant in participants:
                name = normalize_player_name(participant)
                user_id = participant.get("userId") or (
                    participant.get("user", {}).get("id")
                    if isinstance(participant.get("user"), dict)
                    else None
                )
                if not name or not user_id:
                    continue
                user_id = str(user_id).strip()
                ids_by_name.setdefault(name.casefold(), set()).add(user_id)
                previous = latest_name_by_id.get(user_id)
                if previous is None or event_date >= previous[0]:
                    latest_name_by_id[user_id] = (event_date, name)
            if completed == 1 or completed % 25 == 0 or completed == len(events):
                report_progress(progress_callback, f"Rosters para asociar IDs: {completed}/{len(events)}.")

    return ids_by_name, latest_name_by_id


def initialize_incremental_state(cache_key, response, progress_callback=None):
    with sqlite3.connect(CACHE_DB_FILE, timeout=10) as connection:
        ensure_incremental_tables(connection)
        state = connection.execute(
            "SELECT value FROM incremental_state WHERE state_key = 'last_sync_at'"
        ).fetchone()
        if state:
            return response

    report_progress(progress_callback, "Inicializando IDs BCP a partir del ranking guardado...")
    start_year, _, local_zone, cutoff_local, start_utc, end_utc = ranking_period_window()
    events = load_bcp_spain_events(
        start_year, local_zone, cutoff_local, start_utc, end_utc, progress_callback
    )
    ids_by_name, latest_name_by_id = load_incremental_identity_directory(events, progress_callback)
    for player in response.get("players", []):
        matching_ids = ids_by_name.get(player["name"].casefold(), set())
        if len(matching_ids) == 1:
            user_id = next(iter(matching_ids))
            player["user_id"] = user_id
            latest = latest_name_by_id.get(user_id)
            if latest:
                player["name"] = latest[1]
        else:
            player["user_id"] = None

    last_sync_at = ranking_data_through(response).isoformat()
    with sqlite3.connect(CACHE_DB_FILE, timeout=10) as connection:
        ensure_incremental_tables(connection)
        connection.execute(
            "INSERT OR REPLACE INTO incremental_state (state_key, value) VALUES ('last_sync_at', ?)",
            (last_sync_at,),
        )

    response["incremental_initialized_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_persisted_players(cache_key, response)
    report_progress(progress_callback, f"Checkpoint inicial guardado: {len(response.get('players', []))} jugadores.")
    return response


def read_incremental_sync_state():
    with sqlite3.connect(CACHE_DB_FILE, timeout=10) as connection:
        ensure_incremental_tables(connection)
        state = connection.execute(
            "SELECT value FROM incremental_state WHERE state_key = 'last_sync_at'"
        ).fetchone()
        pending = connection.execute(
            "SELECT event_id, event_json FROM incremental_pending_events"
        ).fetchall()
        processed = {
            row[0] for row in connection.execute("SELECT pairing_id FROM incremental_pairings")
        }
    return (datetime.fromisoformat(state[0]) if state else None), pending, processed


def player_for_incremental_match(players, by_user_id, by_name, name, user_id=None):
    name = str(name or "").strip()
    user_id = str(user_id or "").strip() or None

    if user_id and user_id in by_user_id:
        player = by_user_id[user_id]
        if name:
            old_key = player["name"].casefold()
            player["name"] = name
            by_name.setdefault(old_key, []).append(player)
            by_name.setdefault(name.casefold(), []).append(player)
        return player

    candidates = by_name.get(name.casefold(), []) if name else []
    unique_candidates = list({id(candidate): candidate for candidate in candidates}.values())
    if len(unique_candidates) == 1:
        player = unique_candidates[0]
        if user_id:
            player["user_id"] = user_id
            by_user_id[user_id] = player
        if name:
            player["name"] = name
        return player

    player = {
        "name": name or f"Jugador {user_id or 'sin ID'}",
        "user_id": user_id,
        "elo": DEFAULT_ELO,
        "games_played": 0,
        "points": 0,
    }
    players.append(player)
    by_name.setdefault(player["name"].casefold(), []).append(player)
    if user_id:
        by_user_id[user_id] = player
    return player


def commit_incremental_update(cache_key, response, last_sync, new_pairings, pending_events):
    response["cached_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    serialized = json.dumps(response, ensure_ascii=False)
    with sqlite3.connect(CACHE_DB_FILE, timeout=30) as connection:
        ensure_incremental_tables(connection)
        connection.execute(
            "INSERT OR REPLACE INTO player_cache (cache_key, updated_at, payload) VALUES (?, ?, ?)",
            (cache_key, time_module.time(), serialized),
        )
        connection.execute(
            "INSERT OR REPLACE INTO incremental_state (state_key, value) VALUES ('last_sync_at', ?)",
            (last_sync.isoformat(),),
        )
        for pairing_id, event_id in new_pairings:
            connection.execute(
                "INSERT OR IGNORE INTO incremental_pairings (pairing_id, event_id, applied_at) VALUES (?, ?, ?)",
                (pairing_id, event_id, time_module.time()),
            )
        connection.execute("DELETE FROM incremental_pending_events")
        connection.executemany(
            "INSERT INTO incremental_pending_events (event_id, event_json) VALUES (?, ?)",
            [(event_id, json.dumps(event, ensure_ascii=False)) for event_id, event in pending_events.items()],
        )


def refresh_players_incrementally(progress_callback=None):
    report_progress(progress_callback, "Leyendo ranking y checkpoint guardados...")
    cache_key = players_cache_key()
    response = load_persisted_players(cache_key)
    if not response or response.get("source") != "bcp_spain":
        raise RuntimeError("No hay un ranking BCP guardado para usar como base.")

    last_sync, pending_rows, processed_pairings = read_incremental_sync_state()
    if last_sync is None:
        response = initialize_incremental_state(cache_key, response, progress_callback)
        last_sync, pending_rows, processed_pairings = read_incremental_sync_state()
    if last_sync is None:
        raise RuntimeError("No se pudo inicializar el checkpoint incremental.")

    start_year, _, local_zone, local_now, start_utc, _ = ranking_period_window()
    safe_cutoff = local_now - timedelta(days=INCREMENTAL_DELAY_DAYS)

    if safe_cutoff <= last_sync:
        response["update_message"] = (
            f"Sin torneos elegibles: el ranking ya llega al {last_sync.strftime('%d/%m/%Y')} "
            f"y se esperan {INCREMENTAL_DELAY_DAYS} días."
        )
        return response

    safe_cutoff_utc = safe_cutoff.astimezone(timezone.utc)
    events = load_bcp_spain_events(
        start_year, local_zone, safe_cutoff, start_utc, safe_cutoff_utc, progress_callback
    )
    events_by_id = {event["id"]: event for event in events}
    pending_events = {event_id: json.loads(event_json) for event_id, event_json in pending_rows}
    candidates = {}

    for event_id, event in pending_events.items():
        if event_id in events_by_id:
            event = events_by_id[event_id]
        candidates[event_id] = event

    for event in events:
        event_id = event["id"]
        event_start = incremental_event_datetime(event, "start", local_zone)
        if event_start and event_start > last_sync:
            candidates[event_id] = event
    report_progress(progress_callback, f"Eventos que requieren revisión: {len(candidates)}.")

    players = response.get("players", [])
    by_user_id = {
        str(player["user_id"]): player
        for player in players
        if player.get("user_id")
    }
    by_name = {}
    for player in players:
        by_name.setdefault(player["name"].casefold(), []).append(player)

    new_pairings = []
    pending_next = {}
    unresolved_starts = []
    new_game_count = 0
    completed_events = 0
    failed_events = []
    stale_pending_before = local_now - timedelta(days=INCREMENTAL_EVENT_LOOKBACK_DAYS)

    ordered_candidates = sorted(
        candidates.items(),
        key=lambda pair: (pair[1].get("dates") or {}).get("start", ""),
    )
    for event_index, (event_id, event) in enumerate(ordered_candidates, start=1):
        event_start = incremental_event_datetime(event, "start", local_zone)
        event_end = incremental_event_datetime(event, "end", local_zone) or event_start
        if event_start and event_start <= last_sync and event_id not in pending_events:
            continue
        if event_end and event_end > safe_cutoff:
            pending_next[event_id] = event
            if event_start:
                unresolved_starts.append(event_start)
            continue

        report_progress(
            progress_callback,
            f"[{event_index}/{len(ordered_candidates)}] Procesando {event.get('name', event_id)}...",
        )
        try:
            participants, matches = load_bcp_event_data(event)
        except Exception as exc:
            print(f"Incremental BCP event unavailable ({event_id}): {exc}")
            failed_events.append(event_id)
            pending_next[event_id] = event
            if event_start:
                unresolved_starts.append(event_start)
            continue

        for participant in participants:
            games = participant.get("games") or participant.get("total_games")
            if not games:
                continue
            participant_name = normalize_player_name(participant)
            participant_id = participant.get("userId") or (
                participant.get("user", {}).get("id")
                if isinstance(participant.get("user"), dict)
                else None
            )
            player_for_incremental_match(players, by_user_id, by_name, participant_name, participant_id)

        event_new_games = 0
        for match in sorted(matches, key=lambda item: item["round"]):
            pairing_id = match.get("pairing_id") or (
                f"{event_id}:{match['round']}:"
                f"{match.get('player_id') or match['player'].casefold()}:"
                f"{match.get('opponent_id') or match['opponent'].casefold()}"
            )
            if pairing_id in processed_pairings:
                continue

            first = player_for_incremental_match(
                players, by_user_id, by_name, match["player"], match.get("player_id")
            )
            second = player_for_incremental_match(
                players, by_user_id, by_name, match["opponent"], match.get("opponent_id")
            )
            if first is second:
                continue

            expected = 1 / (1 + 10 ** ((second["elo"] - first["elo"]) / 400))
            score20 = max(
                0.0,
                min(20.0, 10 + (match["player_points"] - match["opponent_points"]) / 5),
            )
            change = K_FACTOR * (score20 / 20 - expected)
            first["elo"] = round(first["elo"] + change, 2)
            second["elo"] = round(second["elo"] - change, 2)
            first["games_played"] = first.get("games_played", 0) + 1
            second["games_played"] = second.get("games_played", 0) + 1
            processed_pairings.add(pairing_id)
            new_pairings.append((pairing_id, event_id))
            event_new_games += 1

        new_game_count += event_new_games
        if event_new_games:
            completed_events += 1
        if event_end and event_end >= stale_pending_before:
            pending_next[event_id] = event
        elif not matches:
            failed_events.append(event_id)
            if event_start:
                unresolved_starts.append(event_start)
        if event_index == 1 or event_index % 10 == 0 or event_index == len(ordered_candidates):
            report_progress(
                progress_callback,
                f"Progreso: {event_index}/{len(ordered_candidates)} eventos; "
                f"{new_game_count} partidas nuevas.",
            )

    if unresolved_starts:
        next_sync = min([safe_cutoff] + [event_start - timedelta(seconds=1) for event_start in unresolved_starts])
    else:
        next_sync = safe_cutoff

    players.sort(key=lambda player: (-player["elo"], -player.get("games_played", 0), player["name"]))
    response["players"] = players
    response["message"] = (
        f"Actualización incremental: {new_game_count} partidas nuevas de {completed_events} torneos; "
        f"datos hasta el {next_sync.strftime('%d/%m/%Y')}, margen de {INCREMENTAL_DELAY_DAYS} días."
    )
    response["incremental_new_games"] = new_game_count
    response["default_elo"] = DEFAULT_ELO
    response["games_count_version"] = GAMES_COUNT_CACHE_VERSION
    if failed_events:
        response["message"] += f" {len(failed_events)} torneos quedan pendientes."

    commit_incremental_update(cache_key, response, next_sync, new_pairings, pending_next)
    report_progress(
        progress_callback,
        f"Actualización guardada: {new_game_count} partidas, checkpoint {next_sync.strftime('%d/%m/%Y')}.",
    )
    return response


def load_players_from_spain_since_2025(matches_override, initial_ratings, progress_callback=None):
    start_year, end_year, local_zone, cutoff_local, start_utc, end_utc = ranking_period_window()
    report_progress(progress_callback, "Buscando torneos de España...")
    events = load_bcp_spain_events(
        start_year, local_zone, cutoff_local, start_utc, end_utc, progress_callback
    )
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
        for completed, future in enumerate(as_completed(futures), start=1):
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
            if completed == 1 or completed % 10 == 0 or completed == len(events):
                report_progress(
                    progress_callback,
                    f"Eventos descargados: {completed}/{len(events)}; "
                    f"partidas válidas: {len(event_matches)}.",
                )

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


def players_cache_key(cache_date=None, cache_version=None):
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
        "ranking_start_year": RANKING_START_YEAR,
        "ranking_cache_version": cache_version or RANKING_CACHE_VERSION,
        "default_elo": DEFAULT_ELO,
        "game_system": BCP_GAME_SYSTEM_ID,
        "areas": BCP_SEARCH_AREAS,
        "k_factor": K_FACTOR,
        "matches_mtime": matches_mtime,
    }
    if cache_date:
        settings["date"] = cache_date
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
            migrated = False
            if not row:
                try:
                    local_zone = ZoneInfo("Europe/Madrid")
                except ZoneInfoNotFoundError:
                    local_zone = datetime.now().astimezone().tzinfo or timezone.utc
                today = datetime.now(local_zone).date()
                legacy_versions = range(RANKING_CACHE_VERSION, max(RANKING_CACHE_VERSION - 5, 0), -1)
                for days_back in range(CACHE_MIGRATION_LOOKBACK_DAYS + 1):
                    previous_date = (today - timedelta(days=days_back)).isoformat()
                    for legacy_version in legacy_versions:
                        legacy_key = players_cache_key(previous_date, legacy_version)
                        row = connection.execute(
                            "SELECT updated_at, payload FROM player_cache WHERE cache_key = ?",
                            (legacy_key,),
                        ).fetchone()
                        if row:
                            migrated = True
                            break
                    if row:
                        break
        if row:
            payload = json.loads(row[1])
            payload["cached_at"] = datetime.fromtimestamp(row[0], timezone.utc).isoformat()
            if migrated:
                save_persisted_players(cache_key, payload)
            return payload
    except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"Persistent ranking cache unavailable: {exc}")
    return None


def save_persisted_players(cache_key, payload):
    try:
        payload["cached_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
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


def build_players_response(progress_callback=None):
    cache_key = players_cache_key()
    matches, initial_ratings, matches_error = load_match_results()
    report_progress(progress_callback, "Generando el ranking completo desde BCP...")

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
        players, message = load_players_from_spain_since_2025(
            matches, initial_ratings, progress_callback
        )
        if matches_error:
            message += f" Se ignoró matches.json: {matches_error}"
        response = {
            "players": players,
            "source": "bcp_spain" if players else "unavailable",
            "message": message,
        }

    response["default_elo"] = DEFAULT_ELO
    response["games_count_version"] = GAMES_COUNT_CACHE_VERSION
    return response


def refresh_players_cache(progress_callback=None):
    cache_key = players_cache_key()
    response = build_players_response(progress_callback)
    if response.get("players"):
        save_persisted_players(cache_key, response)
        report_progress(progress_callback, "Ranking completo guardado en SQLite.")
    return response


def get_players_response():
    cache_key = players_cache_key()
    cached_response = load_persisted_players(cache_key)
    if cached_response is not None:
        cached_players = cached_response.get("players", [])
        cache_changed = False
        if (
            cached_response.get("source") == "bcp_spain"
            and cached_response.get("games_count_version") != GAMES_COUNT_CACHE_VERSION
        ):
            game_counts, counted_events, failed_events = load_bcp_roster_game_counts()
            for player in cached_players:
                player["games_played"] = game_counts.get(player["name"].casefold(), 0)
            cached_response["games_count_version"] = GAMES_COUNT_CACHE_VERSION
            cached_response["message"] += (
                f" Partidas contadas desde rosters de {counted_events} torneos BCP."
            )
            if failed_events:
                cached_response["message"] += f" No se pudieron consultar {failed_events} torneos."
            cache_changed = True

        cached_default_elo = cached_response.get("default_elo", 1500)
        if cached_default_elo != DEFAULT_ELO:
            rating_adjustment = DEFAULT_ELO - cached_default_elo
            for player in cached_players:
                player["elo"] = round(float(player["elo"]) + rating_adjustment, 2)
            cached_response["default_elo"] = DEFAULT_ELO
            cache_changed = True

        if cache_changed:
            save_persisted_players(cache_key, cached_response)
        return cached_response

    return {
        "players": [],
        "source": "unavailable",
        "message": "Todavía no hay un ranking guardado. Ejecuta update_ranking.cmd para actualizarlo desde BCP.",
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
