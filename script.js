let players = [];

const searchForm = document.getElementById("searchForm");
const playerNameInput = document.getElementById("playerName");
const playerCard = document.getElementById("playerCard");
const playerNameCard = document.getElementById("playerNameCard");
const playerElo = document.getElementById("playerElo");
const playerWins = document.getElementById("playerWins");
const playerLosses = document.getElementById("playerLosses");
const leaderboardBody = document.getElementById("leaderboardBody");
const rankingStatus = document.getElementById("rankingStatus");

function formatElo(value) {
  return Number(value).toFixed(2).replace(".", ",");
}

function renderLeaderboard() {
  const sorted = [...players].sort((a, b) => b.elo - a.elo);

  leaderboardBody.innerHTML = "";

  sorted.forEach((player, index) => {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td class="rank">${index + 1}</td>
      <td>${player.name}</td>
      <td class="elo">${formatElo(player.elo)}</td>
      <td>${player.wins ?? "-"}</td>
      <td>${player.losses ?? "-"}</td>
    `;
    leaderboardBody.appendChild(row);
  });
}

function searchPlayer(name) {
  const normalized = name.trim();

  if (!normalized) {
    playerCard.classList.add("hidden");
    return;
  }

  const match = players.find(
    (player) => player.name.toLowerCase() === normalized.toLowerCase()
  );

  if (!match) {
    playerCard.classList.remove("hidden");
    playerNameCard.textContent = `"${normalized}" no encontrado`;
    playerElo.textContent = "-";
    playerWins.textContent = "-";
    playerLosses.textContent = "-";
    return;
  }

  playerCard.classList.remove("hidden");
  playerNameCard.textContent = match.name;
  playerElo.textContent = formatElo(match.elo);
  playerWins.textContent = match.wins ?? "-";
  playerLosses.textContent = match.losses ?? "-";
}

async function loadPlayers() {
  try {
    const response = await fetch('/api/players');
    const data = await response.json();
    players = data.players || [];
    rankingStatus.textContent = data.message || "";
    if (players.length) {
      renderLeaderboard();
    } else {
      leaderboardBody.innerHTML = `<tr><td colspan="5">${data.message || 'No hay jugadores disponibles.'}</td></tr>`;
    }
  } catch (error) {
    console.error('Error cargando jugadores:', error);
    leaderboardBody.innerHTML =
      '<tr><td colspan="5">No se pudieron cargar los datos del torneo.</td></tr>';
  }
}

searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  searchPlayer(playerNameInput.value);
});

loadPlayers();
