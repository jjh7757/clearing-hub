// ClearingHub 관리자 예치금 신청 화면 (SPEC 10.9). 빌드 도구 없이 브라우저가 그대로 읽는다.
// 토큰은 정산 화면과 같은 sessionStorage 키를 쓴다 (같은 탭에서 두 화면을 오가도 다시 넣지 않게).
// 은행이 보낸 메모·신청 번호가 섞여 있으므로 화면은 textContent로만 그린다.
"use strict";

const TOKEN_KEY = "clearingHubAdminToken";
const TZ = "Asia/Seoul";
const KIND_LABEL = { DEPOSIT: "입금", WITHDRAW: "인출" };
const STATUS_LABEL = { APPROVED: "승인", REJECTED: "거절", CANCELLED: "은행 취소", PENDING: "대기" };
const STATUS_BADGE = { APPROVED: "SETTLED", REJECTED: "DISPUTE", CANCELLED: "CANCELLED", PENDING: "DRAFT" };

const state = { banks: {}, reserves: {} };

const $ = (id) => document.getElementById(id);

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
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

function kindCell(kind) {
  return el("span", { class: kind === "DEPOSIT" ? "plus" : "minus" }, KIND_LABEL[kind] || kind);
}

function flash(message, bad = false) {
  const node = $("flash");
  node.textContent = message;
  node.className = bad ? "flash bad" : "flash";
  node.hidden = false;
}

// 인출 가능 = 잔액 − max(미정산 순채무, 0) (허브 requests.withdrawable과 같은 식)
function withdrawable(code) {
  const r = state.reserves[code];
  return r ? r.balance - Math.max(r.exposure, 0) : null;
}

// ---- API ----

class AuthError extends Error {}

async function api(path, options = {}) {
  const token = sessionStorage.getItem(TOKEN_KEY);
  if (!token) throw new AuthError("토큰이 없습니다");
  const headers = { "X-Admin-Token": token };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    cache: "no-store",
  });
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
    sessionStorage.removeItem(TOKEN_KEY);
    showLogin(err.message);
    return;
  }
  flash(err.message || String(err), true);
}

function showLogin(error) {
  for (const id of ["app", "refresh", "logout"]) $(id).hidden = true;
  $("login").hidden = false;
  $("login-error").hidden = !error;
  $("login-error").textContent = error || "";
  $("token").value = "";
  $("token").focus();
}

function showApp() {
  $("login").hidden = true;
  for (const id of ["app", "refresh", "logout"]) $(id).hidden = false;
}

// ---- 그리기 ----

async function loadAll() {
  const [banks, reserves, pending, all] = await Promise.all([
    api("/admin/banks"), api("/admin/reserve"),
    api("/admin/reserve/requests?status=PENDING&limit=500"), api("/admin/reserve/requests?limit=100"),
  ]);
  state.banks = Object.fromEntries(banks.map((b) => [b.bank_code, b.name]));
  state.reserves = Object.fromEntries(reserves.map((r) => [r.bank_code, r]));
  tick();
  showApp();
  renderPending(pending);
  renderReserves(reserves);
  renderHistory(all.filter((r) => r.status !== "PENDING"));
  document.title = pending.length ? `(${pending.length}) 예치금 신청 · ClearingHub` : "예치금 신청 · ClearingHub";
}

function emptyRow(tbody, cols, text) {
  tbody.append(el("tr", {}, el("td", { colspan: cols, class: "muted" }, text)));
}

function renderPending(rows) {
  const tbody = $("pending").querySelector("tbody");
  tbody.replaceChildren();
  if (!rows.length) return emptyRow(tbody, 10, "승인을 기다리는 신청이 없습니다");
  for (const r of rows) {
    const room = withdrawable(r.bank_code);
    const short = r.kind === "WITHDRAW" && room !== null && r.amount > room;
    tbody.append(el("tr", {},
      el("td", {}, `#${r.request_id}`),
      el("td", {}, bankLabel(r.bank_code)),
      el("td", {}, kindCell(r.kind)),
      el("td", { class: "num" }, won(r.amount)),
      el("td", { class: "wrap" }, r.memo || "—"),
      el("td", { class: "ids" }, r.request_ref),
      el("td", {}, kst(r.created_at)),
      el("td", { class: "num" }, state.reserves[r.bank_code] ? signedWon(state.reserves[r.bank_code].balance) : "—"),
      el("td", { class: "num" }, r.kind === "WITHDRAW" && room !== null
        ? el("span", { class: short ? "minus" : "" }, short ? `${won(Math.max(room, 0))} (부족)` : won(room)) : "—"),
      el("td", {}, el("div", { class: "actions" },
        el("button", { type: "button", disabled: short, title: short ? "인출 가능 금액보다 큽니다" : null,
          onclick: () => approve(r) }, "승인"),
        el("button", { type: "button", class: "warn", onclick: () => reject(r) }, "거절"))),
    ));
  }
}

function renderReserves(rows) {
  const tbody = $("reserves").querySelector("tbody");
  tbody.replaceChildren();
  if (!rows.length) return emptyRow(tbody, 6, "등록된 은행이 없습니다");
  for (const r of rows) {
    tbody.append(el("tr", {},
      el("td", {}, bankLabel(r.bank_code)),
      el("td", { class: "num" }, signedWon(r.balance)),
      el("td", { class: "num" }, won(r.net_debit_cap)),
      el("td", { class: "num" }, signedWon(r.exposure)),
      el("td", { class: "num" }, signedWon(r.available)),
      el("td", {}, kst(r.updated_at)),
    ));
  }
}

function renderHistory(rows) {
  const tbody = $("history").querySelector("tbody");
  tbody.replaceChildren();
  if (!rows.length) return emptyRow(tbody, 8, "처리한 신청이 아직 없습니다");
  for (const r of rows) {
    tbody.append(el("tr", {},
      el("td", {}, `#${r.request_id}`),
      el("td", {}, bankLabel(r.bank_code)),
      el("td", {}, kindCell(r.kind)),
      el("td", { class: "num" }, won(r.amount)),
      el("td", {}, badge(STATUS_LABEL[r.status] || r.status, STATUS_BADGE[r.status] || "NONE")),
      el("td", { class: "wrap" }, r.reject_reason || r.memo || "—"),
      el("td", {}, kst(r.decided_at)),
      el("td", { class: "num" }, r.balance_after === null ? "—" : signedWon(r.balance_after)),
    ));
  }
}

// ---- 동작 (확인 대화상자를 거친다) ----

function ask({ title, body, reasonLabel, needReason, okLabel, danger }) {
  return new Promise((resolve) => {
    const dialog = $("confirm-dialog");
    $("confirm-title").textContent = title;
    $("confirm-body").textContent = body;
    $("confirm-reason-label").hidden = !reasonLabel;
    $("confirm-reason-text").textContent = reasonLabel || "";
    $("confirm-reason").value = "";
    $("confirm-error").hidden = true;
    $("confirm-ok").textContent = okLabel;
    $("confirm-ok").className = danger ? "danger" : "";
    const done = (value) => {
      $("confirm-form").onsubmit = null;
      $("confirm-cancel").onclick = null;
      dialog.onclose = null;
      dialog.close();
      resolve(value);
    };
    $("confirm-form").onsubmit = (event) => {
      event.preventDefault();
      const reason = $("confirm-reason").value.trim();
      if (needReason && !reason) {
        $("confirm-error").textContent = "사유를 입력하세요";
        $("confirm-error").hidden = false;
        return;
      }
      done({ reason });
    };
    $("confirm-cancel").onclick = () => done(null);
    dialog.onclose = () => resolve(null);
    dialog.showModal();
    if (reasonLabel) $("confirm-reason").focus();
  });
}

async function approve(r) {
  const label = KIND_LABEL[r.kind];
  const now = state.reserves[r.bank_code];
  const after = now ? now.balance + (r.kind === "DEPOSIT" ? r.amount : -r.amount) : null;
  const answer = await ask({
    title: `신청 #${r.request_id} ${label} 승인`,
    body: `${bankLabel(r.bank_code)}의 예치금 ${label} ${won(r.amount)}을 승인합니다.\n` +
      (r.kind === "DEPOSIT" ? "실제(모의) 입금을 확인했을 때만 승인하세요.\n" : "") +
      (after !== null ? `잔액 ${won(now.balance)} → ${won(after)}\n` : "") +
      "승인하면 예치금 원장에 분개가 남고 되돌릴 수 없습니다.",
    reasonLabel: "관리자 메모 (선택, 100자까지 — 분개 메모에 붙습니다)",
    needReason: false,
    okLabel: "승인",
  });
  if (!answer) return;
  try {
    const res = await api(`/admin/reserve/requests/${r.request_id}/approve`, {
      method: "POST", body: answer.reason ? { memo: answer.reason.slice(0, 100) } : {},
    });
    flash(`신청 #${r.request_id}을 승인했습니다 (잔액 ${won(res.balance_after)})`);
    await loadAll();
  } catch (err) { handleError(err); }
}

async function reject(r) {
  const answer = await ask({
    title: `신청 #${r.request_id} 거절`,
    body: `${bankLabel(r.bank_code)}의 ${KIND_LABEL[r.kind]} 신청 ${won(r.amount)}을 거절합니다. 잔액은 바뀌지 않습니다.`,
    reasonLabel: "거절 사유 (필수, 200자까지 — 은행에 그대로 보입니다)",
    needReason: true,
    okLabel: "거절",
    danger: true,
  });
  if (!answer) return;
  try {
    await api(`/admin/reserve/requests/${r.request_id}/reject`, { method: "POST", body: { reason: answer.reason } });
    flash(`신청 #${r.request_id}을 거절했습니다`);
    await loadAll();
  } catch (err) { handleError(err); }
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
    loadAll().catch(handleError);
  });
  $("logout").addEventListener("click", () => {
    sessionStorage.removeItem(TOKEN_KEY);
    showLogin();
  });
  $("refresh").addEventListener("click", () => { $("flash").hidden = true; loadAll().catch(handleError); });

  tick();
  setInterval(() => {
    tick();
    if (!$("app").hidden && !$("confirm-dialog").open) loadAll().catch(handleError); // 새 신청을 30초마다 확인
  }, 30000);

  if (sessionStorage.getItem(TOKEN_KEY)) loadAll().catch(handleError);
  else showLogin();
});
