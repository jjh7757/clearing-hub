// ClearingHub 관리자 실시간 장부 화면. 빌드 도구 없이 브라우저가 그대로 읽는다.
// 5초마다 관리자 API를 다시 읽는다 (탭이 안 보이거나 일시정지면 멈춘다. 요청은 겹치지 않는다).
// 토큰은 다른 관리자 화면과 같은 sessionStorage 키를 쓴다. 바깥 문자열(메모 등)이 섞여 있어 textContent로만 그린다.
"use strict";

const TOKEN_KEY = "clearingHubAdminToken";
const TZ = "Asia/Seoul";
const POLL_MS = 5000;
const LOW_RATIO = 0.2; // 가용액이 순채무한도의 20% 미만이면 강조
const RESERVE_LABEL = { OK: "OK", OVERDRAWN: "OVERDRAWN (마이너스)", OVERDUE: "OVERDUE (송금 막힘)" };
const KIND_LABEL = {
  DEPOSIT: "입금", WITHDRAW: "인출", SETTLE_CREDIT: "정산 받음", SETTLE_DEBIT: "정산 냄", ADJUST: "정정",
};
const TRANSFER_BADGE = {
  COMPLETED: "ACK", FAILED: "DISPUTE", ACCEPTED: "WAIT", DELIVERING: "WAIT", PENDING: "WAIT", UNKNOWN: "DISPUTE",
};

const state = { banks: {}, paused: false, loading: false, timer: null };

const $ = (id) => document.getElementById(id);

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

const won = (n) => `${Number(n).toLocaleString("ko-KR")}원`;

function signedWon(n) {
  if (n < 0) return el("span", { class: "minus" }, `−${won(-n)}`);
  return won(n);
}

function netWon(n) {
  if (n > 0) return el("span", { class: "plus" }, `+${won(n)}`);
  if (n < 0) return el("span", { class: "minus" }, `−${won(-n)}`);
  return el("span", { class: "zero" }, won(0));
}

const dtFormat = new Intl.DateTimeFormat("en-CA", {
  timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});

function kst(iso) {
  if (!iso) return "—";
  // 허브는 UTC로 저장한다. SQLite(로컬 개발 서버)는 시간대 표시를 빼고 돌려주므로 없으면 UTC로 읽는다
  const utc = /(Z|[+-]\d\d:?\d\d)$/i.test(iso) ? iso : `${iso}Z`;
  const parts = Object.fromEntries(dtFormat.formatToParts(new Date(utc)).map((p) => [p.type, p.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}

const bankLabel = (code) => (state.banks[code] ? `${code} ${state.banks[code]}` : code);
const badge = (text, kind) => el("span", { class: `badge b-${kind}` }, text);

// ---- API ----

class AuthError extends Error {}

async function api(path) {
  const token = sessionStorage.getItem(TOKEN_KEY);
  if (!token) throw new AuthError("토큰이 없습니다");
  const res = await fetch(path, { headers: { "X-Admin-Token": token }, cache: "no-store" });
  if (res.status === 401) throw new AuthError("관리자 토큰이 맞지 않습니다");
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const message = body && body.message ? body.message : `HTTP ${res.status}`;
    throw new Error(body && body.error ? `${message} (${body.error})` : message);
  }
  return body;
}

function handleError(err) {
  if (err instanceof AuthError) {
    stop();
    sessionStorage.removeItem(TOKEN_KEY);
    showLogin(err.message);
    return;
  }
  $("flash").textContent = `갱신 실패: ${err.message || String(err)} (다음 주기에 다시 시도합니다)`;
  $("flash").hidden = false;
}

function showLogin(error) {
  for (const id of ["app", "refresh", "logout", "pause"]) $(id).hidden = true;
  $("login").hidden = false;
  $("login-error").hidden = !error;
  $("login-error").textContent = error || "";
  $("token").value = "";
  $("token").focus();
}

function showApp() {
  $("login").hidden = true;
  for (const id of ["app", "refresh", "logout", "pause"]) $(id).hidden = false;
}

// ---- 자동 갱신 ----

function stop() {
  clearTimeout(state.timer);
  state.timer = null;
}

function schedule() {
  stop();
  if (state.paused || document.hidden || !sessionStorage.getItem(TOKEN_KEY)) return;
  state.timer = setTimeout(refresh, POLL_MS);
}

function renderLiveState() {
  let text = `${POLL_MS / 1000}초마다 자동 갱신`;
  if (state.paused) text = "일시정지됨";
  else if (document.hidden) text = "탭이 보이지 않아 멈춤";
  $("live-state").textContent = text;
  $("pause").textContent = state.paused ? "재개" : "일시정지";
}

// 요청이 겹치지 않게: 진행 중이면 건너뛴다. 끝나면(성공·실패 모두) 다음 주기를 잡는다
async function refresh() {
  if (state.loading) return;
  state.loading = true;
  stop();
  try {
    await loadAll();
  } catch (err) {
    handleError(err);
  } finally {
    state.loading = false;
    renderLiveState();
    if (!$("app").hidden) schedule();
  }
}

// ---- 그리기 ----

async function loadAll() {
  const [banks, reserves, preview, transfers, ledger] = await Promise.all([
    api("/admin/banks"), api("/admin/reserve"), api("/admin/settlements/preview"),
    api("/admin/transfers?limit=20"), api("/admin/reserve/ledger?limit=20"),
  ]);
  state.banks = Object.fromEntries(banks.map((b) => [b.bank_code, b.name]));
  showApp();
  $("flash").hidden = true;
  renderReserves(reserves);
  renderPreview(preview);
  renderTransfers(transfers);
  renderLedger(ledger);
  $("updated").textContent = `${kst(new Date().toISOString())} KST`;
  tick();
}

function emptyRow(tbody, cols, text) {
  tbody.append(el("tr", {}, el("td", { colspan: cols, class: "muted" }, text)));
}

function reserveLevel(r) {
  if (r.status === "OVERDUE") return "bad";
  if (r.status === "OVERDRAWN" || r.available <= 0 || r.available < r.net_debit_cap * LOW_RATIO) return "warn";
  return "";
}

function renderReserves(rows) {
  const tbody = $("reserves").querySelector("tbody");
  tbody.replaceChildren();
  if (!rows.length) return emptyRow(tbody, 7, "등록된 은행이 없습니다");
  for (const r of rows) {
    const level = reserveLevel(r);
    const low = r.available <= 0 || r.available < r.net_debit_cap * LOW_RATIO;
    tbody.append(el("tr", { class: level ? `row-${level}` : null },
      el("td", {}, bankLabel(r.bank_code)),
      el("td", { class: "num" }, signedWon(r.balance)),
      el("td", { class: "num" }, won(r.net_debit_cap)),
      // 음수 순채무 = 받은 돈이 더 많다 (더 보낼 수 있다). 위험이 아니므로 빨간색으로 칠하지 않는다
      el("td", { class: "num" }, r.exposure < 0 ? el("span", { class: "zero" }, `−${won(-r.exposure)}`) : won(r.exposure)),
      el("td", { class: "num" }, el("span", { class: low ? "minus" : "" }, r.available < 0 ? `−${won(-r.available)}` : won(r.available))),
      el("td", {}, badge(RESERVE_LABEL[r.status] || r.status, r.status)),
      el("td", {}, kst(r.updated_at)),
    ));
  }
}

function renderPreview(p) {
  $("draft-notice").hidden = p.draft_run_id === null;
  $("draft-title").textContent = p.draft_run_id === null ? "" : `확정하지 않은 DRAFT run #${p.draft_run_id}이 있습니다.`;
  $("pv-date").textContent = p.settle_date;
  $("pv-cutoff").textContent = kst(p.cutoff_at);
  $("pv-count").textContent = `${p.tx_count.toLocaleString("ko-KR")}건`;
  $("pv-total").textContent = won(p.total_amount);
  $("pv-after").textContent = `${p.after_cutoff.count.toLocaleString("ko-KR")}건 · ${won(p.after_cutoff.total)}`;
  $("pv-inflight").textContent = `${p.in_flight.count.toLocaleString("ko-KR")}건 · ${won(p.in_flight.total)}`;
  $("pv-inflight-detail").textContent = "진행 중 상태별: " +
    Object.entries(p.in_flight.by_status).map(([s, n]) => `${s} ${n}건`).join(" · ") +
    ". 진행 중 거래는 COMPLETED가 된 뒤 다음 정산에 들어갑니다 (보낸 쪽 순채무에는 이미 잡혀 있습니다).";

  const pos = $("positions").querySelector("tbody");
  pos.replaceChildren();
  if (!p.positions.length) emptyRow(pos, 6, "지금 정산할 거래가 없습니다");
  for (const x of p.positions) {
    pos.append(el("tr", {},
      el("td", {}, bankLabel(x.bank_code)),
      el("td", { class: "num" }, x.sent_count),
      el("td", { class: "num" }, won(x.sent_total)),
      el("td", { class: "num" }, x.recv_count),
      el("td", { class: "num" }, won(x.recv_total)),
      el("td", { class: "num" }, netWon(x.net_amount)),
    ));
  }

  const pairs = $("pairs").querySelector("tbody");
  pairs.replaceChildren();
  if (!p.pairs.length) emptyRow(pairs, 6, "—");
  for (const x of p.pairs) {
    let direction = "상계 0 (주고받을 것 없음)";
    if (x.net_direction === "a_to_b") direction = `${bankLabel(x.bank_a)} → ${bankLabel(x.bank_b)}`;
    else if (x.net_direction === "b_to_a") direction = `${bankLabel(x.bank_b)} → ${bankLabel(x.bank_a)}`;
    pairs.append(el("tr", {},
      el("td", {}, bankLabel(x.bank_a)),
      el("td", {}, bankLabel(x.bank_b)),
      el("td", { class: "num" }, `${x.a_to_b_count}건 ${won(x.a_to_b_total)}`),
      el("td", { class: "num" }, `${x.b_to_a_count}건 ${won(x.b_to_a_total)}`),
      el("td", { class: "num" }, won(x.net_amount)),
      el("td", {}, direction),
    ));
  }
}

function renderTransfers(rows) {
  const tbody = $("transfers").querySelector("tbody");
  tbody.replaceChildren();
  if (!rows.length) return emptyRow(tbody, 6, "거래가 아직 없습니다");
  for (const t of rows) {
    tbody.append(el("tr", {},
      el("td", {}, kst(t.requested_at)),
      el("td", {}, `${bankLabel(t.from_bank)} → ${bankLabel(t.to_bank)}`),
      el("td", { class: "num" }, won(t.amount)),
      el("td", { title: t.fail_message || null }, badge(t.status, TRANSFER_BADGE[t.status] || "NONE")),
      el("td", {}, kst(t.finished_at)),
      el("td", { class: "ids" }, t.transfer_id),
    ));
  }
}

function renderLedger(rows) {
  const tbody = $("ledger").querySelector("tbody");
  tbody.replaceChildren();
  if (!rows.length) return emptyRow(tbody, 9, "분개가 아직 없습니다");
  for (const e of rows) {
    tbody.append(el("tr", {},
      el("td", {}, `#${e.entry_id}`),
      el("td", {}, kst(e.created_at)),
      el("td", {}, bankLabel(e.bank_code)),
      el("td", {}, KIND_LABEL[e.kind] || e.kind),
      el("td", { class: "num" }, netWon(e.amount)),
      el("td", { class: "num" }, signedWon(e.balance_after)),
      el("td", {}, e.run_id === null ? "—" : `#${e.run_id}`),
      el("td", { class: "wrap" }, e.memo || "—"),
      el("td", {}, e.created_by),
    ));
  }
}

// ---- 시작 ----

function tick() {
  $("clock").textContent = `지금 ${kst(new Date().toISOString())} KST`;
}

document.addEventListener("DOMContentLoaded", () => {
  $("login-form").addEventListener("submit", (event) => {
    event.preventDefault();
    sessionStorage.setItem(TOKEN_KEY, $("token").value.trim());
    $("token").value = "";
    refresh();
  });
  $("logout").addEventListener("click", () => {
    stop();
    sessionStorage.removeItem(TOKEN_KEY);
    showLogin();
  });
  $("refresh").addEventListener("click", () => refresh());
  $("pause").addEventListener("click", () => {
    state.paused = !state.paused;
    renderLiveState();
    if (state.paused) stop();
    else refresh();
  });
  document.addEventListener("visibilitychange", () => {
    renderLiveState();
    if (document.hidden) stop();
    else if (!state.paused && !$("app").hidden) refresh(); // 다시 보이면 바로 갱신
  });

  tick();
  setInterval(tick, 1000);
  renderLiveState();
  if (sessionStorage.getItem(TOKEN_KEY)) refresh();
  else showLogin();
});
