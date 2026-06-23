const form = document.querySelector("#searchForm");
const textRequest = document.querySelector("#textRequest");
const askButton = document.querySelector("#askButton");
const statusPill = document.querySelector("#statusPill");
const resultsTitle = document.querySelector("#resultsTitle");
const summary = document.querySelector("#summary");
const metrics = document.querySelector("#metrics");
const bestPrice = document.querySelector("#bestPrice");
const flightCount = document.querySelector("#flightCount");
const tripType = document.querySelector("#tripType");
const resultsList = document.querySelector("#resultsList");
const routeMap = document.querySelector("#routeMap");
const followUp = document.querySelector("#followUp");
const followUpForm = document.querySelector("#followUpForm");
const followUpInput = document.querySelector("#followUpInput");
const followUpButton = document.querySelector("#followUpButton");
const chatThread = document.querySelector("#chatThread");
const newChatButton = document.querySelector("#newChatButton");
const chatHistoryList = document.querySelector("#chatHistoryList");
const clearChatsButton = document.querySelector("#clearChatsButton");
const fareAlertButton = document.querySelector("#fareAlertButton");
const fareAlertList = document.querySelector("#fareAlertList");
const refreshAlertsButton = document.querySelector("#refreshAlertsButton");
const alertDialog = document.querySelector("#alertDialog");
const alertForm = document.querySelector("#alertForm");
const alertTargetPrice = document.querySelector("#alertTargetPrice");
const alertEmail = document.querySelector("#alertEmail");
const cancelAlertButton = document.querySelector("#cancelAlertButton");
const saveAlertButton = document.querySelector("#saveAlertButton");
let currentSearch = null;
let searchHistory = [];
let currentChatId = null;
let map = null;
let routeLayer = null;
let searchController = null;

const initialSummary =
  "Describe the trip you have in mind and I will search live fares, choose promising dates when you are flexible, and explain the strongest options.";
const chatStorageKey = "cheapFlightsChatsV1";
const maxSavedChats = 8;

renderChatHistory();
loadFareAlerts();

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = textRequest.value.trim();
  if (!text) {
    renderError("Describe the trip you want me to find.");
    textRequest.focus();
    return;
  }

  searchController?.abort();
  searchController = new AbortController();
  const activeController = searchController;
  setLoading(true);
  try {
    const response = await fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
      signal: activeController.signal,
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Search failed.");
    }
    searchHistory = [];
    currentSearch = payload;
    renderResults(payload);
    currentChatId ||= createChatId();
    saveCurrentChat();
  } catch (error) {
    if (error.name !== "AbortError") {
      renderError(error.message);
    }
  } finally {
    if (searchController === activeController) {
      searchController = null;
      setLoading(false);
    }
  }
});

newChatButton.addEventListener("click", startNewChat);
clearChatsButton.addEventListener("click", clearSavedChats);
refreshAlertsButton.addEventListener("click", loadFareAlerts);
fareAlertButton.addEventListener("click", openAlertDialog);
cancelAlertButton.addEventListener("click", () => alertDialog.close());
alertForm.addEventListener("submit", createFareAlert);

followUpForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = followUpInput.value.trim();
  if (!question || !currentSearch) return;

  appendChatMessage("user", question);
  followUpInput.value = "";

  if (isUndoQuestion(question)) {
    restorePreviousSearch();
    return;
  }

  followUpButton.disabled = true;
  followUpButton.textContent = "Thinking...";

  try {
    const response = await fetch("/api/follow-up", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        request: currentSearch.request,
        flights: currentSearch.flights,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "I could not answer that question.");
    if (payload.undo) {
      restorePreviousSearch();
    } else if (payload.refreshed) {
      const priorMessages = collectChatMessages();
      searchHistory.push(currentSearch);
      currentSearch = payload;
      renderResults(payload, false);
      restoreChatMessages(priorMessages);
      appendChatMessage("assistant", payload.answer);
    } else {
      appendChatMessage("assistant", payload.answer);
    }
    saveCurrentChat();
  } catch (error) {
    appendChatMessage("assistant", error.message);
  } finally {
    followUpButton.disabled = false;
    followUpButton.textContent = "Ask";
    followUpInput.focus();
  }
});

function isUndoQuestion(question) {
  return /\b(go back|undo|revert|previous (?:fares|results|search|trip)|restore|remove (?:that|the added|last))\b/i
    .test(question);
}

function restorePreviousSearch() {
  if (!searchHistory.length) {
    appendChatMessage("assistant", "There is no earlier result set to restore.");
    return;
  }

  const previous = searchHistory.pop();
  currentSearch = previous;
  renderResults(previous);
  appendChatMessage("user", "Go back to the previous results.");
  appendChatMessage(
    "assistant",
    "I restored the previous itinerary and fare list. No new fare search was needed."
  );
  saveCurrentChat();
}

function startNewChat() {
  saveCurrentChat();
  searchController?.abort();
  searchController = null;
  currentSearch = null;
  searchHistory = [];
  currentChatId = null;
  textRequest.value = "";
  followUpInput.value = "";
  chatThread.innerHTML = "";
  followUp.hidden = true;
  fareAlertButton.hidden = true;
  metrics.hidden = true;
  routeMap.hidden = true;
  clearMapRoute();
  resultsList.innerHTML = "";
  resultsTitle.textContent = "Ready when you are";
  renderNarrative(initialSummary);
  setLoading(false);
  renderChatHistory();
  textRequest.focus();
}

function openAlertDialog() {
  if (!currentSearch?.flights?.length) return;
  alertTargetPrice.value =
    currentSearch.request.budget_usd || currentSearch.flights[0].price_usd;
  alertDialog.showModal();
  alertTargetPrice.focus();
}

async function createFareAlert(event) {
  event.preventDefault();
  if (!currentSearch) return;
  saveAlertButton.disabled = true;
  saveAlertButton.textContent = "Creating...";
  try {
    const response = await fetch("/api/alerts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        request: currentSearch.request,
        targetPriceUsd: alertTargetPrice.value,
        email: alertEmail.value.trim(),
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not create fare alert.");
    alertDialog.close();
    alertEmail.value = "";
    await loadFareAlerts();
  } catch (error) {
    alertDialog.close();
    appendChatMessage("assistant", error.message);
  } finally {
    saveAlertButton.disabled = false;
    saveAlertButton.textContent = "Create alert";
  }
}

async function loadFareAlerts() {
  try {
    const response = await fetch("/api/alerts");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not load fare alerts.");
    renderFareAlerts(payload.alerts || []);
  } catch (error) {
    fareAlertList.innerHTML = `<p class="chat-history-empty">${escapeHtml(error.message)}</p>`;
  }
}

function renderFareAlerts(alerts) {
  if (!alerts.length) {
    fareAlertList.innerHTML = `<p class="chat-history-empty">No fare alerts yet.</p>`;
    return;
  }
  fareAlertList.innerHTML = "";
  alerts.forEach((alert) => {
    const item = document.createElement("article");
    item.className = `fare-alert-item ${alert.status === "triggered" ? "triggered" : ""}`;
    const current = alert.currentPriceUsd
      ? `Latest ${money(alert.currentPriceUsd)}`
      : "Awaiting first check";
    item.innerHTML = `
      <div class="fare-alert-copy">
        <strong>${escapeHtml(alert.request.origin)} to ${escapeHtml(alert.request.destination)}</strong>
        <span>Target ${money(alert.targetPriceUsd)} &middot; ${escapeHtml(current)}</span>
        <small>${escapeHtml(alertStatus(alert))}</small>
      </div>
      <div class="fare-alert-actions">
        <button type="button" data-alert-check="${escapeAttribute(alert.id)}">Check now</button>
        <button type="button" data-alert-delete="${escapeAttribute(alert.id)}" aria-label="Delete fare alert" title="Delete alert">&times;</button>
      </div>
    `;
    item.querySelector("[data-alert-check]").addEventListener("click", () => checkFareAlert(alert.id));
    item.querySelector("[data-alert-delete]").addEventListener("click", () => deleteFareAlert(alert.id));
    fareAlertList.appendChild(item);
  });
}

function alertStatus(alert) {
  if (alert.status === "triggered") return "Target reached";
  if (alert.status === "error") return alert.lastError || "Check failed";
  if (alert.status === "no_results") return "No fares found on last check";
  if (alert.lastCheckedAt) return `Checked ${formatSavedDate(alert.lastCheckedAt)}`;
  return "Watching";
}

async function checkFareAlert(alertId) {
  const response = await fetch(`/api/alerts/${encodeURIComponent(alertId)}/check`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  const payload = await response.json();
  if (!response.ok) {
    appendChatMessage("assistant", payload.error || "Could not check fare alert.");
  }
  await loadFareAlerts();
}

async function deleteFareAlert(alertId) {
  const response = await fetch(`/api/alerts/${encodeURIComponent(alertId)}`, {
    method: "DELETE",
  });
  const payload = await response.json();
  if (!response.ok) {
    appendChatMessage("assistant", payload.error || "Could not delete fare alert.");
  }
  await loadFareAlerts();
}

function loadSavedChats() {
  try {
    const chats = JSON.parse(localStorage.getItem(chatStorageKey) || "[]");
    return Array.isArray(chats) ? chats : [];
  } catch {
    return [];
  }
}

function saveCurrentChat() {
  if (!currentSearch || !currentChatId) return;

  const chats = loadSavedChats().filter((chat) => chat.id !== currentChatId);
  chats.unshift({
    id: currentChatId,
    title: chatTitle(currentSearch),
    prompt: textRequest.value.trim(),
    savedAt: new Date().toISOString(),
    search: currentSearch,
    undoHistory: searchHistory,
    messages: collectChatMessages(),
  });
  persistChats(chats.slice(0, maxSavedChats));
  renderChatHistory();
}

function persistChats(chats) {
  try {
    localStorage.setItem(chatStorageKey, JSON.stringify(chats));
  } catch {
    const smaller = chats.slice(0, Math.max(1, Math.floor(chats.length / 2)));
    localStorage.setItem(chatStorageKey, JSON.stringify(smaller));
  }
}

function renderChatHistory() {
  const chats = loadSavedChats();
  clearChatsButton.hidden = !chats.length;
  if (!chats.length) {
    chatHistoryList.innerHTML = `<p class="chat-history-empty">No previous chats yet.</p>`;
    return;
  }

  chatHistoryList.innerHTML = "";
  chats.forEach((chat) => {
    const row = document.createElement("div");
    row.className = `chat-history-row ${chat.id === currentChatId ? "active" : ""}`;

    const openButton = document.createElement("button");
    openButton.type = "button";
    openButton.className = "chat-history-open";
    const title = document.createElement("strong");
    title.textContent = chatTitle(chat.search);
    const date = document.createElement("span");
    date.textContent = formatSavedDate(chat.savedAt);
    openButton.append(title, date);
    openButton.addEventListener("click", () => restoreSavedChat(chat.id));

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "chat-history-delete";
    deleteButton.textContent = "×";
    deleteButton.setAttribute("aria-label", `Delete ${chat.title}`);
    deleteButton.title = "Delete chat";
    deleteButton.addEventListener("click", () => deleteSavedChat(chat.id));
    row.append(openButton, deleteButton);
    chatHistoryList.appendChild(row);
  });
}

function restoreSavedChat(chatId) {
  saveCurrentChat();
  const chat = loadSavedChats().find((item) => item.id === chatId);
  if (!chat) return;

  searchController?.abort();
  currentChatId = chat.id;
  currentSearch = chat.search;
  searchHistory = chat.undoHistory || [];
  textRequest.value = chat.prompt || "";
  renderResults(currentSearch);
  restoreChatMessages(chat.messages || []);
  renderChatHistory();
}

function deleteSavedChat(chatId) {
  const chats = loadSavedChats().filter((chat) => chat.id !== chatId);
  persistChats(chats);
  if (currentChatId === chatId) {
    currentChatId = null;
  }
  renderChatHistory();
}

function clearSavedChats() {
  localStorage.removeItem(chatStorageKey);
  currentChatId = null;
  renderChatHistory();
}

function collectChatMessages() {
  return [...chatThread.querySelectorAll(".chat-message")].map((message) => ({
    role: message.classList.contains("user") ? "user" : "assistant",
    text: message.textContent,
  }));
}

function restoreChatMessages(messages) {
  chatThread.innerHTML = "";
  messages.forEach((message) => appendChatMessage(message.role, message.text));
}

function chatTitle(search) {
  const request = search?.request;
  if (!request) return "Flight search";
  const locations = search.locations || [];
  const origin = displayLocation(request.origin, locations);
  const destination = displayLocation(request.destination, locations);
  return `From ${origin} to ${destination}`;
}

function displayLocation(code, locations) {
  const location = locations.find((item) => item.code === code);
  return location?.municipality || location?.name || code;
}

function createChatId() {
  return globalThis.crypto?.randomUUID?.()
    || `chat-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function formatSavedDate(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}

textRequest.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    form.requestSubmit(askButton);
  }
});

function setLoading(isLoading) {
  askButton.disabled = isLoading;
  askButton.textContent = isLoading ? "Finding fares..." : "Find flights";
  statusPill.textContent = isLoading ? "Searching" : "Live fares";
}

function renderResults(payload, resetConversation = true) {
  const flights = payload.flights || [];
  resultsTitle.textContent = flights.length ? routeTitle(payload.request) : "No matching live fares";
  renderNarrative(payload.message);
  if (resetConversation) chatThread.innerHTML = "";
  followUp.hidden = !flights.length;
  fareAlertButton.hidden = !flights.length;
  if (flights.length && resetConversation) {
    appendChatMessage(
      "assistant",
      "I have these results in context. Ask me to compare options, change the trip, or say “go back” to restore the previous fares."
    );
  }
  resultsList.innerHTML = "";
  renderRouteMap(payload.request, flights, payload.locations || []);

  if (!flights.length) {
    metrics.hidden = true;
    resultsList.innerHTML = `<div class="empty-state">Try broadening the budget, travel window, cabin, or stop preferences in your prompt.</div>`;
    return;
  }

  metrics.hidden = false;
  bestPrice.textContent = money(flights[0].price_usd);
  flightCount.textContent = String(flights.length);
  tripType.textContent = labelTripType(payload.request.trip_type);
  resultsList.innerHTML = flights.map((flight, index) => flightCard(flight, index)).join("");
}

function renderNarrative(message) {
  summary.innerHTML = "";
  String(message || "").split(/\n{2,}/).forEach((paragraph) => {
    const element = document.createElement("p");
    element.textContent = paragraph;
    summary.appendChild(element);
  });
}

function renderError(message) {
  currentSearch = null;
  resultsTitle.textContent = "I need one more detail";
  renderNarrative(message);
  followUp.hidden = true;
  fareAlertButton.hidden = true;
  metrics.hidden = true;
  routeMap.hidden = true;
  clearMapRoute();
  resultsList.innerHTML = "";
}

function appendChatMessage(role, text) {
  const message = document.createElement("div");
  message.className = `chat-message ${role}`;
  message.textContent = text;
  chatThread.appendChild(message);
  message.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function routeTitle(request) {
  return `${request.origin} to ${request.destination}`;
}

function flightCard(flight, index) {
  const outbound = segment("Outbound", flight.depart_at, flight.arrive_at);
  const inbound = flight.return_depart_at && flight.return_arrive_at
    ? segment("Return", flight.return_depart_at, flight.return_arrive_at)
    : "";
  const stopText = flight.stops === 0
    ? "Nonstop"
    : `${flight.stops} ${flight.stops === 1 ? "stop" : "stops"}`;
  const reasons = flight.reasons.map((reason) => `<span>${escapeHtml(reason)}</span>`).join("");

  return `
    <article class="flight-card ${index === 0 ? "best" : ""}">
      <div>
        <div class="flight-title">
          <h3>${escapeHtml(flight.airline)}</h3>
          ${index === 0 ? `<span class="badge">Best pick</span>` : ""}
        </div>
        <div class="facts">
          <span>${stopText}</span>
          <span>${flight.bags_included ? "Bags included" : "Bags extra"}</span>
          <span>${formatDuration(flight.totalDurationMinutes)}</span>
        </div>
        <div class="route-grid">${outbound}${inbound}</div>
        <div class="reasons">
          <span>Why it ranked here</span>
          <div class="reason-tags">${reasons}</div>
        </div>
      </div>
      <div class="price-box">
        <div class="price">${money(flight.price_usd)}</div>
        <a class="book-link" href="${escapeAttribute(flight.booking_url)}" target="_blank" rel="noreferrer">Google Flights</a>
      </div>
    </article>
  `;
}

function labelTripType(value) {
  if (value === "multi_city") return "Multi-city";
  if (value === "one_way") return "One way";
  return "Round trip";
}

function renderRouteMap(request, flights, locations) {
  const points = routePoints(request, flights, locations);
  if (points.length < 2) {
    routeMap.hidden = true;
    clearMapRoute();
    return;
  }

  routeMap.hidden = false;
  if (!window.L) return;
  if (!map) {
    map = L.map(routeMap, {
      scrollWheelZoom: false,
      worldCopyJump: true,
    });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors",
    }).addTo(map);
  }

  clearMapRoute();
  routeLayer = L.layerGroup().addTo(map);
  const latLngs = points.map((point) => [point.lat, point.lon]);
  L.polyline(latLngs, {
    color: "#0f7b76",
    weight: 4,
    opacity: 0.9,
    dashArray: "8 8",
  }).addTo(routeLayer);
  points.forEach((point, index) => {
    L.circleMarker([point.lat, point.lon], {
      radius: 7,
      color: "#172026",
      weight: 2,
      fillColor: index === 0 ? "#f2c45b" : "#0f7b76",
      fillOpacity: 1,
    }).bindTooltip(point.code, {
      permanent: true,
      direction: "top",
      offset: [0, -8],
    }).addTo(routeLayer);
  });
  map.fitBounds(L.latLngBounds(latLngs), { padding: [35, 35], maxZoom: 5 });
  window.setTimeout(() => map.invalidateSize(), 0);
}

function clearMapRoute() {
  if (map && routeLayer) {
    map.removeLayer(routeLayer);
    routeLayer = null;
  }
}

function routePoints(request, flights, locations) {
  const codes = [];
  if (request.trip_type === "multi_city" && request.multi_city_segments) {
    request.multi_city_segments.forEach((item) => {
      addCode(codes, item.origin);
      addCode(codes, item.destination);
    });
  } else {
    addCode(codes, request.origin);
    addCode(codes, request.destination);
  }
  if (flights[0]) {
    addCode(codes, flights[0].origin);
    addCode(codes, flights[0].destination);
  }
  const locationsByCode = new Map(locations.map((location) => [location.code, location]));
  return codes
    .map((code) => locationsByCode.get(code))
    .filter(Boolean)
    .map((location) => ({
      code: location.code,
      lon: location.longitude,
      lat: location.latitude,
    }));
}

function addCode(codes, code) {
  const normalized = String(code || "").toUpperCase();
  if (normalized && !codes.includes(normalized)) codes.push(normalized);
}

function segment(label, departAt, arriveAt) {
  return `
    <div class="route-segment">
      <span>${label}</span>
      <strong>${formatDateTime(departAt)} to ${formatDateTime(arriveAt)}</strong>
    </div>
  `;
}

function money(value) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(value);
}

function formatDuration(minutes) {
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return mins ? `${hours}h ${mins}m` : `${hours}h`;
}

function formatDateTime(value) {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
