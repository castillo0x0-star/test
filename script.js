let players = [];
let rankedPlayers = [];
let currentPage = 0;
let activeSuggestionIndex = -1;
let selectedPlayerRank = null;
let selectedPlayerName = null;
let sortColumn = "elo";
let sortDirection = -1;

const PAGE_SIZE = 10;
const SUGGESTION_MIN_LENGTH = 3;
const MAX_SUGGESTIONS = 8;

const searchForm = document.getElementById("searchForm");
const playerNameInput = document.getElementById("playerName");
const playerSuggestions = document.getElementById("playerSuggestions");
const playerCard = document.getElementById("playerCard");
const playerRank = document.getElementById("playerRank");
const playerNameCard = document.getElementById("playerNameCard");
const playerElo = document.getElementById("playerElo");
const playerGames = document.getElementById("playerGames");
const leaderboardBody = document.getElementById("leaderboardBody");
const rankingUpdatedAt = document.getElementById("rankingUpdatedAt");
const rankingPagination = document.getElementById("rankingPagination");
const previousPageButton = document.getElementById("previousPage");
const nextPageButton = document.getElementById("nextPage");
const pageIndicator = document.getElementById("pageIndicator");
const pageJumpForm = document.getElementById("pageJumpForm");
const pageJumpInput = document.getElementById("pageJumpInput");
const cancelPageJumpButton = document.getElementById("cancelPageJump");
const sortEloButton = document.getElementById("sortElo");
const sortGamesButton = document.getElementById("sortGames");
const eloHeader = document.getElementById("eloHeader");
const gamesHeader = document.getElementById("gamesHeader");

function formatElo(value) {
  return Number(value).toFixed(2).replace(".", ",");
}

function updateSortedPlayers() {
  rankedPlayers = [...players].sort((first, second) => {
    const firstValue = Number(first[sortColumn]) || 0;
    const secondValue = Number(second[sortColumn]) || 0;
    if (firstValue !== secondValue) return (firstValue - secondValue) * sortDirection;

    const eloDifference = (Number(first.elo) || 0) - (Number(second.elo) || 0);
    if (eloDifference) return sortColumn === "elo" ? -eloDifference : -eloDifference;
    return first.name.localeCompare(second.name);
  });

  eloHeader.setAttribute("aria-sort", sortColumn === "elo"
    ? (sortDirection < 0 ? "descending" : "ascending")
    : "none");
  gamesHeader.setAttribute("aria-sort", sortColumn === "games_played"
    ? (sortDirection < 0 ? "descending" : "ascending")
    : "none");
  document.querySelector("#sortElo .sort-indicator").textContent = sortColumn === "elo"
    ? (sortDirection < 0 ? "↓" : "↑")
    : "";
  document.querySelector("#sortGames .sort-indicator").textContent = sortColumn === "games_played"
    ? (sortDirection < 0 ? "↓" : "↑")
    : "";
}

function setSort(column) {
  if (sortColumn === column) {
    sortDirection *= -1;
  } else {
    sortColumn = column;
    sortDirection = -1;
  }

  updateSortedPlayers();
  if (selectedPlayerName) {
    selectedPlayerRank = rankedPlayers.findIndex((player) => player.name === selectedPlayerName) + 1;
    currentPage = Math.floor((selectedPlayerRank - 1) / PAGE_SIZE);
  }
  renderLeaderboard();
  renderSuggestions(playerNameInput.value);
}

function normalizeSearchText(value) {
  return value
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase()
    .trim();
}

function playerMatchesSearch(player, normalizedQuery) {
  return [player.name, ...(player.aliases || [])].some((name) =>
    normalizeSearchText(name).includes(normalizedQuery)
  );
}

function closeSuggestions() {
  playerSuggestions.classList.add("hidden");
  playerNameInput.setAttribute("aria-expanded", "false");
  playerNameInput.removeAttribute("aria-activedescendant");
  activeSuggestionIndex = -1;
}

function renderSuggestions(query) {
  const normalized = normalizeSearchText(query);
  playerSuggestions.innerHTML = "";

  if (normalized.length < SUGGESTION_MIN_LENGTH) {
    closeSuggestions();
    return;
  }

  const matches = rankedPlayers
    .map((player, index) => ({ player, rank: index + 1 }))
    .filter(({ player }) => playerMatchesSearch(player, normalized))
    .slice(0, MAX_SUGGESTIONS);

  if (!matches.length) {
    closeSuggestions();
    return;
  }

  matches.forEach(({ player, rank }, index) => {
    const option = document.createElement("button");
    option.type = "button";
    option.id = `playerSuggestion${index}`;
    option.className = "player-suggestion";
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", "false");

    const name = document.createElement("span");
    name.textContent = player.name;

    const meta = document.createElement("span");
    meta.className = "suggestion-meta";
    meta.textContent = `#${rank} · ${formatElo(player.elo)}`;

    option.append(name, meta);
    option.addEventListener("mousedown", (event) => event.preventDefault());
    option.addEventListener("click", () => selectSuggestion(index));
    playerSuggestions.appendChild(option);
  });

  activeSuggestionIndex = -1;
  playerSuggestions.classList.remove("hidden");
  playerNameInput.setAttribute("aria-expanded", "true");
}

function setActiveSuggestion(index) {
  const options = playerSuggestions.querySelectorAll('[role="option"]');
  if (!options.length) return;

  activeSuggestionIndex = (index + options.length) % options.length;
  options.forEach((option, optionIndex) => {
    const isActive = optionIndex === activeSuggestionIndex;
    option.setAttribute("aria-selected", String(isActive));
    if (isActive) {
      playerNameInput.setAttribute("aria-activedescendant", option.id);
      option.scrollIntoView({ block: "nearest" });
    }
  });
}

function selectSuggestion(index) {
  const player = rankedPlayers
    .filter((candidate) => playerMatchesSearch(candidate, normalizeSearchText(playerNameInput.value)))
    .slice(0, MAX_SUGGESTIONS)[index];
  if (!player) return;

  playerNameInput.value = player.name;
  closeSuggestions();
  searchPlayer(player.name);
}

function renderLeaderboard() {
  const pageCount = Math.ceil(rankedPlayers.length / PAGE_SIZE);
  currentPage = Math.min(currentPage, Math.max(pageCount - 1, 0));
  const start = currentPage * PAGE_SIZE;
  const visiblePlayers = rankedPlayers.slice(start, start + PAGE_SIZE);

  leaderboardBody.innerHTML = "";

  visiblePlayers.forEach((player, index) => {
    const rank = start + index + 1;
    const row = document.createElement("tr");
    if (rank === selectedPlayerRank) row.classList.add("selected-player");
    row.innerHTML = `
      <td class="rank">${rank}</td>
      <td>${player.name}</td>
      <td class="elo">${formatElo(player.elo)}</td>
      <td>${player.games_played ?? 0}</td>
    `;
    leaderboardBody.appendChild(row);
  });

  rankingPagination.hidden = pageCount <= 1;
  previousPageButton.disabled = currentPage === 0;
  nextPageButton.disabled = currentPage >= pageCount - 1;
  pageIndicator.textContent = rankedPlayers.length
    ? `${start + 1}-${Math.min(start + PAGE_SIZE, rankedPlayers.length)} de ${rankedPlayers.length}`
    : "0 resultados";
  pageIndicator.setAttribute(
    "aria-label",
    `Ir a un puesto; se muestran ${start + 1} a ${Math.min(start + PAGE_SIZE, rankedPlayers.length)} de ${rankedPlayers.length}`
  );
  pageJumpInput.max = Math.max(rankedPlayers.length, 1);
  pageJumpInput.value = String(start + 1);
  closePageJump();
}

function closePageJump() {
  pageIndicator.hidden = false;
  pageJumpForm.hidden = true;
}

function searchPlayer(name) {
  const normalized = normalizeSearchText(name);

  if (!normalized) {
    selectedPlayerRank = null;
    selectedPlayerName = null;
    playerCard.classList.add("hidden");
    renderLeaderboard();
    return;
  }

  const matchIndex = rankedPlayers.findIndex((player) =>
    playerMatchesSearch(player, normalized)
  );
  const match = rankedPlayers[matchIndex];

  if (!match) {
    selectedPlayerRank = null;
    selectedPlayerName = null;
    playerCard.classList.remove("hidden");
    playerRank.textContent = "Sin puesto";
    playerNameCard.textContent = `"${name.trim()}" no encontrado`;
    playerElo.textContent = "-";
    playerGames.textContent = "-";
    renderLeaderboard();
    return;
  }

  selectedPlayerRank = matchIndex + 1;
  selectedPlayerName = match.name;
  currentPage = Math.floor(matchIndex / PAGE_SIZE);
  renderLeaderboard();
  playerCard.classList.remove("hidden");
  playerRank.textContent = `Puesto #${matchIndex + 1}`;
  playerNameCard.textContent = match.name;
  playerElo.textContent = formatElo(match.elo);
  playerGames.textContent = match.games_played ?? 0;
  requestAnimationFrame(() => {
    leaderboardBody.querySelector(".selected-player")?.scrollIntoView({
      block: "nearest",
      behavior: "smooth",
    });
  });
}

async function loadPlayers() {
  try {
    const response = await fetch('/api/players');
    const data = await response.json();
    players = data.players || [];
    updateSortedPlayers();
    currentPage = 0;
    if (data.cached_at) {
      const updatedAt = new Date(data.cached_at);
      rankingUpdatedAt.dateTime = updatedAt.toISOString();
      rankingUpdatedAt.textContent = new Intl.DateTimeFormat("es-ES", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(updatedAt);
    } else {
      rankingUpdatedAt.textContent = "sin fecha";
    }
    if (players.length) {
      renderLeaderboard();
      renderSuggestions(playerNameInput.value);
    } else {
      leaderboardBody.innerHTML = `<tr><td colspan="4">${data.message || 'No hay jugadores disponibles.'}</td></tr>`;
    }
  } catch (error) {
    console.error('Error cargando jugadores:', error);
    leaderboardBody.innerHTML =
      '<tr><td colspan="4">No se pudieron cargar los datos del torneo.</td></tr>';
  }
}

searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (activeSuggestionIndex >= 0) {
    selectSuggestion(activeSuggestionIndex);
    return;
  }
  closeSuggestions();
  searchPlayer(playerNameInput.value);
});

playerNameInput.addEventListener("input", () => {
  selectedPlayerRank = null;
  selectedPlayerName = null;
  leaderboardBody.querySelector(".selected-player")?.classList.remove("selected-player");
  playerCard.classList.add("hidden");
  renderSuggestions(playerNameInput.value);
});

sortEloButton.addEventListener("click", () => setSort("elo"));
sortGamesButton.addEventListener("click", () => setSort("games_played"));

playerNameInput.addEventListener("keydown", (event) => {
  if (event.key === "ArrowDown" && !playerSuggestions.classList.contains("hidden")) {
    event.preventDefault();
    setActiveSuggestion(activeSuggestionIndex + 1);
  } else if (event.key === "ArrowUp" && !playerSuggestions.classList.contains("hidden")) {
    event.preventDefault();
    setActiveSuggestion(activeSuggestionIndex <= 0
      ? playerSuggestions.children.length - 1
      : activeSuggestionIndex - 1);
  } else if (event.key === "Escape") {
    closeSuggestions();
  }
});

document.addEventListener("pointerdown", (event) => {
  if (!searchForm.contains(event.target)) closeSuggestions();
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

pageIndicator.addEventListener("click", () => {
  pageIndicator.hidden = true;
  pageJumpForm.hidden = false;
  pageJumpInput.value = String(currentPage * PAGE_SIZE + 1);
  pageJumpInput.focus();
  pageJumpInput.select();
});

pageJumpForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const requestedRank = Number(pageJumpInput.value);
  if (!Number.isInteger(requestedRank) || requestedRank < 1 || requestedRank > rankedPlayers.length) {
    pageJumpInput.reportValidity();
    return;
  }

  selectedPlayerRank = requestedRank;
  currentPage = Math.floor((requestedRank - 1) / PAGE_SIZE);
  renderLeaderboard();
  leaderboardBody.scrollIntoView({ block: "start", behavior: "smooth" });
});

cancelPageJumpButton.addEventListener("click", closePageJump);
pageJumpInput.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closePageJump();
});

loadPlayers();
