import logging
import time
from datetime import datetime

from app import ROOT, refresh_players_incrementally

LOG_FILE = ROOT / "update_ranking.log"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    encoding="utf-8",
)


def report_progress(message):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    print(line, flush=True)
    logging.info(message)


def main():
    started = time.monotonic()
    report_progress("Actualizando torneos elegibles desde el último checkpoint (con 7 días de margen)...")

    try:
        response = refresh_players_incrementally(progress_callback=report_progress)
    except Exception as exc:
        report_progress(f"No se pudo actualizar el ranking: {type(exc).__name__}: {exc}")
        return 1

    players = response.get("players", [])
    if not players:
        report_progress(response.get("message", "BCP no devolvió jugadores."))
        return 1

    report_progress(response.get("update_message") or response.get("message", "Ranking actualizado."))
    report_progress(f"Jugadores guardados: {len(players)}")
    report_progress(f"Actualizado: {response.get('cached_at', 'fecha no disponible')}")
    report_progress(f"Duración: {time.monotonic() - started:.1f} segundos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
