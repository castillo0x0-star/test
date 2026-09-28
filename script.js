let players = [];
let currentPage = 0;

const PAGE_SIZE = 10;

const searchForm = document.getElementById("searchForm");
const playerNameInput = document.getElementById("playerName");
const playerCard = document.getElementById("playerCard");
const playerRank = document.getElementById("playerRank");
const playerNameCard = document.getElementById("playerNameCard");
const playerElo = document.getElementById("playerElo");
const playerWins = document.getElementById("playerWins");
const playerLosses = document.getElementById("playerLosses");
const leaderboardBody = document.getElementById("leaderboardBody");
const rankingStatus = document.getElementById("rankingStatus");
const rankingPagination = document.getElementById("rankingPagination");
const previousPageButton = document.getElementById("previousPage");
const nextPageButton = document.getElementById("nextPage");
const pageIndicator = document.getElementById("pageIndicator");

function formatElo(value) {
  return Number(value).toFixed(2).replace(".", ",");
}

function normalizeSearchText(value) {
  return value
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase()
    .trim();
}

function renderLeaderboard() {
  const sorted = [...players].sort((a, b) => b.elo - a.elo);
  const pageCount = Math.ceil(sorted.length / PAGE_SIZE);
  currentPage = Math.min(currentPage, Math.max(pageCount - 1, 0));
  const start = currentPage * PAGE_SIZE;
  const visiblePlayers = sorted.slice(start, start + PAGE_SIZE);

  leaderboardBody.innerHTML = "";

  visiblePlayers.forEach((player, index) => {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td class="rank">${start + index + 1}</td>
      <td>${player.name}</td>
      <td class="elo">${formatElo(player.elo)}</td>
      <td>${player.wins ?? "-"}</td>
      <td>${player.losses ?? "-"}</td>
    `;
    leaderboardBody.appendChild(row);
  });

  rankingPagination.hidden = pageCount <= 1;
  previousPageButton.disabled = currentPage === 0;
  nextPageButton.disabled = currentPage >= pageCount - 1;
  pageIndicator.textContent = sorted.length
    ? `${start + 1}-${Math.min(start + PAGE_SIZE, sorted.length)} de ${sorted.length}`
    : "0 resultados";
}

function searchPlayer(name) {
  const normalized = normalizeSearchText(name);

  if (!normalized) {
    playerCard.classList.add("hidden");
    return;
  }

  const sortedPlayers = [...players].sort((a, b) => b.elo - a.elo);
  const matchIndex = sortedPlayers.findIndex((player) =>
    normalizeSearchText(player.name).includes(normalized)
  );
  const match = sortedPlayers[matchIndex];

  if (!match) {
    playerCard.classList.remove("hidden");
    playerRank.textContent = "Sin puesto";
    playerNameCard.textContent = `"${name.trim()}" no encontrado`;
    playerElo.textContent = "-";
    playerWins.textContent = "-";
    playerLosses.textContent = "-";
    return;
  }

  playerCard.classList.remove("hidden");
  playerRank.textContent = `Puesto #${matchIndex + 1}`;
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
    currentPage = 0;
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

previousPageButton.addEventListener("click", () => {
  if (currentPage > 0) {
    currentPage -= 1;
    renderLeaderboard();
  }
});

nextPageButton.addEventListener("click", () => {
  if ((currentPage + 1) * PAGE_SIZE < players.length) {
    currentPage += 1;
    renderLeaderboard();
  }
});

loadPlayers();
