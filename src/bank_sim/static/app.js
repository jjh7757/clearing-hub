// SimBank 시연 화면. 빌드 도구 없이 브라우저가 그대로 읽는다.
// 시연 비밀번호는 이 탭의 sessionStorage에만 둔다. 다른 은행이 보낸 이름·메모가 섞여 있어 textContent로만 그린다.
// 5초마다 잔액·거래내역을 다시 읽고(탭이 안 보이면 멈춘다, 요청은 겹치지 않는다), PENDING 보내기는 refresh를 부른다.
"use strict";

const PASSWORD_KEY = "simBankPassword";
const CUSTOMER_KEY = "simBankCustomer";
const ACCOUNT_KEY = "simBankAccount";
const POLL_MS = 5000;
const FRESH_MS = 8000; // 새 입금 강조 시간
const MAX_AMOUNT = 1000000; // 서버 MAX_SEND_AMOUNT와 같다
const TZ = "Asia/Seoul";
const STATUS_LABEL = { PENDING: "처리 중", COMPLETED: "완료", FAILED: "실패" };

const state = {
  customers: [], customer: null, account: null, bankCode: "902",
  txs: {}, // account_num → 거래 목록
  seen: {}, // account_num → Set(transfer_id) : 이미 본 입금 (처음 읽은 것은 강조하지 않는다)
  fresh: new Map(), // transfer_id → 강조 끝나는 시각
  banks: [], holder: null, loading: false, timer: null, sending: false,
};

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

const comma = (n) => Number(n).toLocaleString("ko-KR");
const won = (n) => `${comma(n)}원`;

const timeFormat = new Intl.DateTimeFormat("ko-KR", {
  timeZone: TZ, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});

function kst(iso) {
  if (!iso) return "";
  const utc = /(Z|[+-]\d\d:?\d\d)$/i.test(iso) ? iso : `${iso}Z`;
  return timeFormat.format(new Date(utc));
}

// ---- API ----

class AuthError extends Error {}
class DisabledError extends Error {}

class ApiError extends Error {
  constructor(status, body) {
    const message = body && body.message ? body.message : `HTTP ${status}`;
    super(body && body.error ? `${message} (${body.error})` : message);
    this.status = status;
    this.body = body || {};
  }
}

async function api(path, options = {}) {
  const password = sessionStorage.getItem(PASSWORD_KEY);
  if (!password) throw new AuthError("");
  const headers = { "X-Sim-Password": password };
  let body;
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.json);
  }
  const res = await fetch(path, { method: options.method || "GET", headers, body, cache: "no-store" });
  const data = await res.json().catch(() => null);
  // 이 화면의 401은 언제나 시연 비밀번호 오류다 (허브의 401·403은 서버가 502로 바꿔 준다)
  if (res.status === 401) throw new AuthError("시연 비밀번호가 맞지 않습니다");
  if (res.status === 403 && data && data.error === "FORBIDDEN") throw new DisabledError();
  if (!res.ok && res.status !== 202) throw new ApiError(res.status, data);
  return { status: res.status, data };
}

function handleError(err) {
  if (err instanceof AuthError) {
    stop();
    sessionStorage.removeItem(PASSWORD_KEY);
    showLogin(err.message);
    return true;
  }
  if (err instanceof DisabledError) {
    stop();
    showOnly("disabled");
    return true;
  }
  return false;
}

function flash(message, kind) {
  const node = $("flash");
  node.textContent = message || "";
  node.className = `flash ${kind || "bad"}`;
  node.hidden = !message;
}

// ---- 화면 전환 ----

function showOnly(id) {
  for (const name of ["login", "disabled", "app"]) $(name).hidden = name !== id;
  $("logout").hidden = id !== "app";
}

function showLogin(error) {
  showOnly("login");
  $("login-error").hidden = !error;
  $("login-error").textContent = error || "";
  $("password").value = "";
  $("password").focus();
}

// ---- 자동 갱신 ----

function stop() {
  clearTimeout(state.timer);
  state.timer = null;
}

function schedule() {
  stop();
  if (document.hidden || !sessionStorage.getItem(PASSWORD_KEY)) return;
  state.timer = setTimeout(refresh, POLL_MS);
}

async function refresh() {
  if (state.loading) return;
  state.loading = true;
  try {
    await loadCustomers();
    await loadTransactions();
    await refreshPending();
    flash("");
    $("updated").textContent = `5초마다 갱신 · ${kst(new Date().toISOString())}`;
  } catch (err) {
    if (handleError(err)) return;
    flash(`갱신 실패: ${err.message || String(err)} (다음 주기에 다시 시도합니다)`);
  } finally {
    state.loading = false;
  }
  schedule();
}

async function loadCustomers() {
  const { data } = await api("/sim/api/customers");
  state.customers = data.customers;
  state.bankCode = data.bank_code;
  $("bank-title").textContent = data.bank_code === "902" ? "SimBank" : `SimBank (${data.bank_code})`;
  const names = state.customers.map((c) => c.name);
  if (!names.includes(state.customer)) {
    const saved = sessionStorage.getItem(CUSTOMER_KEY);
    state.customer = names.includes(saved) ? saved : names[0];
  }
  const customer = currentCustomer();
  const nums = customer ? customer.accounts.map((a) => a.account_num) : [];
  if (!nums.includes(state.account)) {
    const saved = sessionStorage.getItem(ACCOUNT_KEY);
    state.account = nums.includes(saved) ? saved : nums[0] || null;
  }
  renderCustomers();
  renderAccounts();
  renderFromAccounts();
}

const currentCustomer = () => state.customers.find((c) => c.name === state.customer) || null;

async function loadTransactions() {
  const customer = currentCustomer();
  if (!customer) return;
  // 고른 고객의 계좌를 모두 읽는다: 고르지 않은 계좌에 들어온 입금도 카드에서 강조하려고
  for (const acc of customer.accounts) {
    const { data } = await api(`/sim/api/accounts/${encodeURIComponent(acc.account_num)}/transactions?limit=30`);
    noteFresh(acc.account_num, data.transactions);
    state.txs[acc.account_num] = data.transactions;
  }
  renderAccounts();
  renderTransactions();
}

function noteFresh(accountNum, txs) {
  const ids = txs.filter((t) => t.direction === "IN").map((t) => t.transfer_id);
  const seen = state.seen[accountNum];
  if (!seen) { // 처음 읽은 내역은 "방금 들어온" 것이 아니다
    state.seen[accountNum] = new Set(ids);
    return;
  }
  const now = Date.now();
  const arrivals = txs.filter((t) => t.direction === "IN" && !seen.has(t.transfer_id));
  for (const tx of arrivals) {
    seen.add(tx.transfer_id);
    state.fresh.set(tx.transfer_id, now + FRESH_MS);
  }
  if (arrivals.length) {
    const tx = arrivals[0];
    toast(`${tx.counterpart_bank_name || tx.counterpart_bank} ${tx.counterpart_name}님이 ${won(tx.amount)}을 보냈습니다`);
    setTimeout(() => { renderAccounts(); renderTransactions(); }, FRESH_MS + 50);
  }
}

async function refreshPending() {
  const pending = Object.values(state.txs).flat().filter((t) => t.direction === "OUT" && t.status === "PENDING");
  let changed = false;
  for (const tx of pending.slice(0, 5)) {
    try {
      const { data } = await api(`/sim/api/transfers/${encodeURIComponent(tx.transfer_id)}/refresh`, { method: "POST" });
      if (data.status !== "PENDING") changed = true;
    } catch (err) {
      if (err instanceof AuthError || err instanceof DisabledError) throw err;
      // 허브에 못 닿아도 다음 주기에 다시 본다
    }
  }
  if (changed) {
    await loadCustomers();
    await loadTransactions();
  }
}

// ---- 그리기 ----

function renderCustomers() {
  const tabs = $("customer-tabs");
  tabs.replaceChildren(...state.customers.map((c) => {
    const button = el("button", { type: "button", role: "tab", class: "tab", "aria-selected": String(c.name === state.customer) },
      `${c.name} 님`);
    button.addEventListener("click", () => selectCustomer(c.name));
    return button;
  }));
}

function isFresh(transferId) {
  const until = state.fresh.get(transferId);
  if (!until) return false;
  if (until < Date.now()) { state.fresh.delete(transferId); return false; }
  return true;
}

function renderAccounts() {
  const customer = currentCustomer();
  const box = $("accounts");
  if (!customer) { box.replaceChildren(); return; }
  box.replaceChildren(...customer.accounts.map((acc) => {
    const fresh = (state.txs[acc.account_num] || []).some((t) => t.direction === "IN" && isFresh(t.transfer_id));
    const card = el("button", {
      type: "button", class: `account${acc.account_num === state.account ? " selected" : ""}${fresh ? " fresh" : ""}`,
      "aria-pressed": String(acc.account_num === state.account),
    },
    el("span", { class: "product" }, acc.product_name),
    el("span", { class: "num" }, acc.account_num),
    el("span", { class: "balance" }, won(acc.balance)),
    fresh ? el("span", { class: "fresh-tag" }, "방금 입금") : null);
    card.addEventListener("click", () => selectAccount(acc.account_num));
    return card;
  }));
}

function statusBadge(tx) {
  return el("span", { class: `badge s-${tx.status.toLowerCase()}` }, STATUS_LABEL[tx.status] || tx.status);
}

function txRow(tx) {
  const isIn = tx.direction === "IN";
  const bank = tx.counterpart_bank_name || tx.counterpart_bank || "";
  const who = tx.counterpart_name || tx.counterpart_account || "";
  const title = isIn ? `${who}` : `${who} 님께`;
  const sub = [isIn ? `${bank}에서 받음` : `${bank} ${tx.counterpart_account}`, kst(tx.created_at)];
  const fresh = isIn && isFresh(tx.transfer_id);
  const failed = tx.status === "FAILED";
  return el("li", { class: `tx ${isIn ? "in" : "out"}${fresh ? " fresh" : ""}${failed ? " failed" : ""}` },
    el("div", { class: "tx-main" },
      el("div", { class: "tx-title" }, title, fresh ? el("span", { class: "fresh-tag" }, "방금 들어옴") : null),
      el("div", { class: "tx-sub muted small" }, sub.filter(Boolean).join(" · ")),
      tx.memo ? el("div", { class: "tx-memo small" }, tx.memo) : null,
      failed && (tx.fail_message || tx.fail_code)
        ? el("div", { class: "tx-fail small" }, `실패 사유: ${tx.fail_message || ""}${tx.fail_code ? ` (${tx.fail_code})` : ""} · 환불됨`)
        : null),
    el("div", { class: "tx-side" },
      el("div", { class: `tx-amount ${isIn ? "plus" : "minus"}` }, `${isIn ? "+" : "−"}${won(tx.amount)}`),
      isIn ? null : statusBadge(tx)));
}

function renderTransactions() {
  const txs = state.txs[state.account] || [];
  const customer = currentCustomer();
  const acc = customer && customer.accounts.find((a) => a.account_num === state.account);
  $("tx-account").textContent = acc ? `${acc.product_name} ${acc.account_num}` : "";
  $("transactions").replaceChildren(...txs.map(txRow));
  $("tx-empty").hidden = txs.length > 0;
}

function renderFromAccounts() {
  const customer = currentCustomer();
  const select = $("from-account");
  const options = customer ? customer.accounts.map((a) => el("option", { value: a.account_num },
    `${a.product_name} ${a.account_num} (잔액 ${won(a.balance)})`)) : [];
  select.replaceChildren(...options);
  select.value = state.account || "";
}

function renderBanks() {
  const select = $("to-bank");
  const previous = select.value;
  select.replaceChildren(el("option", { value: "" }, state.banks.length ? "은행을 고르세요" : "받을 은행이 없습니다"),
    ...state.banks.map((b) => el("option", { value: b.bank_code }, `${b.name} (${b.bank_code})`)));
  if (state.banks.some((b) => b.bank_code === previous)) select.value = previous;
}

let toastTimer = null;
function toast(message) {
  let node = document.querySelector(".toast");
  if (!node) {
    node = el("div", { class: "toast", role: "status", "aria-live": "polite" });
    document.body.append(node);
  }
  node.textContent = `💸 ${message}`;
  node.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("show"), FRESH_MS);
}

// ---- 고르기 ----

function selectCustomer(name) {
  state.customer = name;
  sessionStorage.setItem(CUSTOMER_KEY, name);
  const customer = currentCustomer();
  state.account = customer && customer.accounts[0] ? customer.accounts[0].account_num : null;
  sessionStorage.setItem(ACCOUNT_KEY, state.account || "");
  renderCustomers();
  renderAccounts();
  renderFromAccounts();
  renderTransactions();
  resetHolder();
  refreshNow();
}

function selectAccount(num) {
  state.account = num;
  sessionStorage.setItem(ACCOUNT_KEY, num);
  renderAccounts();
  renderFromAccounts();
  renderTransactions();
}

function refreshNow() {
  stop();
  if (state.loading) { schedule(); return; }
  refresh();
}

// ---- 보내기 폼 ----

function formatAccount(value) {
  const digits = value.replace(/\D/g, "").slice(0, 12);
  if (digits.length <= 3) return digits;
  if (digits.length <= 6) return `${digits.slice(0, 3)}-${digits.slice(3)}`;
  return `${digits.slice(0, 3)}-${digits.slice(3, 6)}-${digits.slice(6)}`;
}

function amountValue() {
  return Number($("amount").value.replace(/\D/g, "")) || 0;
}

function resetHolder() {
  state.holder = null;
  $("holder").hidden = true;
  $("holder").textContent = "";
  updateSendButton();
}

function updateSendButton() {
  const amount = amountValue();
  $("send").disabled = state.sending || !state.holder || amount < 1 || amount > MAX_AMOUNT || !$("from-account").value;
}

function showHolder(text, kind) {
  const node = $("holder");
  node.textContent = text;
  node.className = `holder ${kind}`;
  node.hidden = false;
}

async function loadBanks() {
  try {
    const { data } = await api("/sim/api/banks");
    state.banks = data.banks;
    renderBanks();
  } catch (err) {
    if (handleError(err)) return;
    state.banks = [];
    renderBanks();
    $("to-bank").firstChild.textContent = `은행 목록을 못 받았습니다: ${err.message}`;
    setTimeout(loadBanks, POLL_MS * 2);
  }
}

async function inquiry() {
  const toBank = $("to-bank").value;
  const toAccount = $("to-account").value.trim();
  resetHolder();
  if (!toBank || !toAccount) { showHolder("받는 은행과 계좌번호를 넣어 주세요", "bad"); return; }
  $("inquiry").disabled = true;
  try {
    const { data } = await api("/sim/api/inquiry", { method: "POST", json: { to_bank: toBank, to_account: toAccount } });
    state.holder = { bank: toBank, account: toAccount, name: data.holder_name };
    const bank = state.banks.find((b) => b.bank_code === toBank);
    showHolder(`받는 분: ${data.holder_name} (${bank ? bank.name : toBank} ${toAccount})`, "ok");
  } catch (err) {
    if (handleError(err)) return;
    showHolder(`확인 실패: ${err.message}`, "bad");
  } finally {
    $("inquiry").disabled = false;
    updateSendButton();
  }
}

function showResult(text, kind) {
  const node = $("send-result");
  node.textContent = text;
  node.className = `result ${kind}`;
  node.hidden = false;
}

async function send(event) {
  event.preventDefault();
  if (!state.holder) return;
  const amount = amountValue();
  const payload = {
    from_account: $("from-account").value, to_bank: state.holder.bank, to_account: state.holder.account,
    amount, memo: $("memo").value.trim(),
  };
  state.sending = true;
  updateSendButton();
  $("send").textContent = "보내는 중…";
  try {
    const { status, data } = await api("/sim/api/transfers", { method: "POST", json: payload });
    if (data.status === "COMPLETED") {
      showResult(`${data.holder_name} 님께 ${won(amount)}을 보냈습니다. 남은 잔액 ${won(data.balance)}`, "ok");
    } else if (data.status === "FAILED") {
      showResult(`보내지 못했습니다: ${data.fail_message || data.fail_code || "실패"} (돈은 돌려받았습니다)`, "bad");
    } else if (status === 202) {
      showResult(`처리 중입니다. 결과는 거래내역에서 자동으로 바뀝니다 (${data.holder_name} 님, ${won(amount)})`, "wait");
    }
    if (data.status !== "FAILED") {
      $("amount").value = "";
      $("memo").value = "";
    }
    selectAccount(payload.from_account);
  } catch (err) {
    if (handleError(err)) return;
    showResult(`보내지 못했습니다: ${err.message}`, "bad");
  } finally {
    state.sending = false;
    $("send").textContent = "보내기";
    updateSendButton();
    refreshNow();
  }
}

// ---- 시작 ----

function start() {
  showOnly("app");
  loadBanks();
  refreshNow();
}

document.addEventListener("DOMContentLoaded", () => {
  $("login-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const value = $("password").value;
    if (!value) return;
    sessionStorage.setItem(PASSWORD_KEY, value);
    start();
  });
  $("logout").addEventListener("click", () => {
    stop();
    sessionStorage.removeItem(PASSWORD_KEY);
    showLogin("");
  });
  $("to-account").addEventListener("input", (event) => {
    event.target.value = formatAccount(event.target.value);
    resetHolder();
  });
  $("to-bank").addEventListener("change", resetHolder);
  $("from-account").addEventListener("change", (event) => selectAccount(event.target.value));
  $("amount").addEventListener("input", (event) => {
    const n = event.target.value.replace(/\D/g, "").slice(0, 7);
    event.target.value = n ? comma(Number(n)) : "";
    updateSendButton();
  });
  $("inquiry").addEventListener("click", inquiry);
  $("send-form").addEventListener("submit", send);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && sessionStorage.getItem(PASSWORD_KEY) && !$("app").hidden) refreshNow();
  });

  if (sessionStorage.getItem(PASSWORD_KEY)) start();
  else showLogin("");
});
