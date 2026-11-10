// ClearingHub 관리자 정산 화면. 빌드 도구 없이 브라우저가 그대로 읽는다.
// 토큰은 이 탭의 sessionStorage에만 둔다. 화면은 textContent로만 그린다 (은행이 보낸 문자열이 섞여 있다).
"use strict";

const TOKEN_KEY = "clearingHubAdminToken";
const TZ = "Asia/Seoul"; // 허브 SETTLEMENT_TIMEZONE 기본값. 화면의 모든 시각을 이 시간대로 보여 준다

const STATUS_LABEL = { DRAFT: "DRAFT (확정 대기)", SETTLED: "SETTLED (확정)", CANCELLED: "CANCELLED (취소)" };
const BY_LABEL = {
  auto: "auto (일일 배치)",
  admin: "admin (관리자)",
  "auto-ack": "auto-ack (전원 확인)",
  "auto-deadline": "auto-deadline (마감 자동)",
};
const RESULT_LABEL = { OK: "OK", OVERDRAWN: "OVERDRAWN (예치금 부족)", OVERDUE: "OVERDUE (한도 초과)" };

const state = { banks: {}, runs: [], selected: null, detail: null, transfers: null };

// ---- 작은 도구 ----

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
  if (n > 0) return el("span", { class: "plus" }, `+${won(n)}`);
  if (n < 0) return el("span", { class: "minus" }, `−${won(-n)}`);
  return el("span", { class: "zero" }, "0원");
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

function remaining(iso) {
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return "마감 시각이 지났습니다 (워커가 곧 처리하거나, 처리 중입니다)";
  const min = Math.floor(ms / 60000);
  const h = Math.floor(min / 60);
  if (h > 0) return `${h}시간 ${min % 60}분 남음`;
  return min > 0 ? `${min}분 남음` : "1분 미만 남음";
}

function bankLabel(code) {
  const name = state.banks[code];
  return name ? `${code} ${name}` : code;
}

function badge(text, kind) {
  return el("span", { class: `badge b-${kind}` }, text);
}

function flash(message, bad = false) {
  const node = $("flash");
  node.textContent = message;
  node.className = bad ? "flash bad" : "flash";
  node.hidden = false;
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

// ---- 로그인 ----

function showLogin(error) {
  $("app").hidden = true;
  $("refresh").hidden = true;
  $("logout").hidden = true;
  $("login").hidden = false;
  $("login-error").hidden = !error;
  $("login-error").textContent = error || "";
  $("token").value = "";
  $("token").focus();
}

function showApp() {
  $("login").hidden = true;
  $("app").hidden = false;
  $("refresh").hidden = false;
  $("logout").hidden = false;
}

// ---- 목록 ----

async function loadAll() {
  const [banks, runs] = await Promise.all([api("/admin/banks"), api("/admin/settlements?limit=90")]);
  state.banks = Object.fromEntries(banks.map((b) => [b.bank_code, b.name]));
  state.runs = runs;
  tick();
  showApp();
  renderRuns();
  const fromHash = Number((location.hash.match(/run=(\d+)/) || [])[1]);
  const pick = fromHash || state.selected || (runs.find((r) => r.status === "DRAFT") || runs[0] || {}).run_id;
  if (pick) await openRun(pick);
  else $("detail").hidden = true;
}

function renderRuns() {
  const tbody = $("runs").querySelector("tbody");
  tbody.replaceChildren();
  if (!state.runs.length) {
    tbody.append(el("tr", {}, el("td", { colspan: 9, class: "muted" }, "정산 run이 아직 없습니다")));
    return;
  }
  for (const r of state.runs) {
    tbody.append(el("tr", { class: r.run_id === state.selected ? "selected" : "", onclick: () => openRun(r.run_id) },
      el("td", {}, `#${r.run_id}`),
      el("td", {}, r.settle_date),
      el("td", {}, badge(STATUS_LABEL[r.status] || r.status, r.status)),
      el("td", { class: "num" }, r.tx_count.toLocaleString("ko-KR")),
      el("td", { class: "num" }, won(r.total_amount)),
      el("td", {}, BY_LABEL[r.created_by] || r.created_by),
      el("td", {}, kst(r.created_at)),
      el("td", {}, kst(r.settled_at)),
      el("td", {}, r.settled_by ? BY_LABEL[r.settled_by] || r.settled_by : "—"),
    ));
  }
}

// ---- 상세 ----

async function openRun(runId) {
  state.selected = runId;
  state.transfers = null;
  history.replaceState(null, "", `#run=${runId}`);
  renderRuns();
  state.detail = await api(`/admin/settlements/${runId}`);
  renderDetail();
}

function deadlineNotice(d) {
  if (d.status !== "DRAFT") return null;
  const waiting = d.unanswered.map(bankLabel);
  const disputes = d.acks.filter((a) => a.result === "DISPUTE").map((a) => bankLabel(a.bank_code));
  const items = [];
  if (waiting.length) items.push(el("li", {}, `아직 확인하지 않은 은행: ${waiting.join(", ")}`));
  else if (!disputes.length) items.push(el("li", {}, "포지션이 있는 은행이 모두 ack했습니다"));
  items.push(el("li", {}, "포지션이 있는 은행이 모두 ack하면 그 즉시 auto-ack로 확정됩니다"));

  if (disputes.length) {
    return el("div", { class: "notice bad" },
      el("strong", {}, "관리자 판정 필요: dispute가 있어 자동 확정되지 않습니다"),
      `dispute를 보낸 은행: ${disputes.join(", ")}. 증거를 확인한 뒤 확정하거나 취소하세요. ` +
        "판정하기 전까지 다음 날 정산 배치도 건너뜁니다. 은행이 나중에 ack를 다시 보내면 dispute가 풀립니다.",
      d.confirm_deadline_at ? el("ul", {}, el("li", {}, `다음 확정 마감 ${kst(d.confirm_deadline_at)} (${remaining(d.confirm_deadline_at)}) — dispute가 남아 있으면 그때도 확정하지 않습니다`), items) : el("ul", {}, items),
    );
  }
  if (!d.confirm_deadline_at) {
    return el("div", { class: "notice warn" },
      el("strong", {}, "자동 마감 확정이 꺼져 있습니다 (SETTLEMENT_WORKER_ENABLED=false)"),
      "은행이 모두 ack하지 않으면 관리자가 직접 확정해야 합니다.", el("ul", {}, items));
  }
  return el("div", { class: "notice ok" },
    el("strong", {}, `${kst(d.confirm_deadline_at)}에 자동 확정 예정 (${remaining(d.confirm_deadline_at)})`),
    "그때까지 dispute가 없으면 확인하지 않은 은행이 있어도 auto-deadline으로 확정됩니다.",
    el("ul", {}, items));
}

function summary(d) {
  const field = (label, value) => el("div", {}, el("span", {}, label), value);
  return el("div", { class: "summary" },
    field("상태", badge(STATUS_LABEL[d.status] || d.status, d.status)),
    field("정산일", d.settle_date),
    field("거래", `${d.tx_count.toLocaleString("ko-KR")}건 · ${won(d.total_amount)}`),
    field("모은 거래 기준 (cutoff)", `${kst(d.cutoff_at)} 전`),
    field("만든 주체·시각", `${BY_LABEL[d.created_by] || d.created_by} · ${kst(d.created_at)}`),
    field("확정 주체·시각", d.settled_at ? `${BY_LABEL[d.settled_by] || d.settled_by} · ${kst(d.settled_at)}` : "—"),
  );
}

function positionsTable(d) {
  const settled = d.status === "SETTLED";
  const head = ["은행", "보낸 건수", "보낸 금액 (낼 돈)", "받은 건수", "받은 금액 (받을 돈)", "순액", "방향"];
  if (settled) head.push("반영 결과", "반영 뒤 예치금");
  const rows = d.positions.map((p) => el("tr", {},
    el("td", {}, bankLabel(p.bank_code)),
    el("td", { class: "num" }, p.sent_count),
    el("td", { class: "num" }, won(p.sent_total)),
    el("td", { class: "num" }, p.recv_count),
    el("td", { class: "num" }, won(p.recv_total)),
    el("td", { class: "num" }, signedWon(p.net_amount)),
    el("td", {}, p.net_amount > 0 ? "▲ 받음 (예치금 +)" : p.net_amount < 0 ? "▼ 냄 (예치금 −)" : "없음"),
    settled ? el("td", {}, RESULT_LABEL[p.result] || p.result || "—") : null,
    settled ? el("td", { class: "num" }, p.balance_after === null ? "—" : won(p.balance_after)) : null,
  ));
  const total = d.positions.reduce((s, p) => s + p.net_amount, 0);
  return [
    el("h3", {}, "은행별 포지션"),
    el("p", { class: "muted small" }, `순액 = 받은 금액 − 보낸 금액. + 는 받을 은행, − 는 낼 은행입니다. 순액의 합: ${won(total)}`),
    el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, head.map((h, i) => el("th", { class: i > 0 && i < 6 ? "num" : "" }, h)))),
      el("tbody", {}, rows.length ? rows : el("tr", {}, el("td", { colspan: head.length, class: "muted" }, "포지션 없음 (거래 0건)"))),
    )),
  ];
}

function pairsTable(d) {
  const rows = d.pairs.map((p) => {
    let direction = "서로 같음";
    if (p.net_direction === "a_to_b") direction = `${bankLabel(p.bank_a)} → ${bankLabel(p.bank_b)} ${won(p.net_amount)}`;
    if (p.net_direction === "b_to_a") direction = `${bankLabel(p.bank_b)} → ${bankLabel(p.bank_a)} ${won(p.net_amount)}`;
    return el("tr", {},
      el("td", {}, bankLabel(p.bank_a)),
      el("td", {}, bankLabel(p.bank_b)),
      el("td", { class: "num" }, `${p.a_to_b_count}건 · ${won(p.a_to_b_total)}`),
      el("td", { class: "num" }, `${p.b_to_a_count}건 · ${won(p.b_to_a_total)}`),
      el("td", {}, direction),
    );
  });
  return [
    el("h3", {}, "은행 쌍별 표"),
    el("p", { class: "muted small" }, "화면 참고용입니다. 실제 예치금 반영은 위 은행별 순액으로 합니다."),
    el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, ["은행 A", "은행 B", "A → B", "B → A", "차액 (누가 누구에게)"].map((h, i) => el("th", { class: i === 2 || i === 3 ? "num" : "" }, h)))),
      el("tbody", {}, rows.length ? rows : el("tr", {}, el("td", { colspan: 5, class: "muted" }, "없음"))),
    )),
  ];
}

function acksTable(d) {
  const acks = Object.fromEntries(d.acks.map((a) => [a.bank_code, a]));
  const withPosition = new Set(d.positions.map((p) => p.bank_code));
  const codes = [...new Set([...withPosition, ...Object.keys(acks)])].sort();
  const rows = codes.map((code) => {
    const a = acks[code];
    let status;
    if (a) status = badge(a.result, a.result);
    else if (d.status === "DRAFT") status = badge("미응답", "WAIT");
    else status = badge("응답 없음", "NONE");
    const note = withPosition.has(code) ? "" : " (포지션 없음 · 기록만)";
    return el("tr", {},
      el("td", {}, bankLabel(code) + note),
      el("td", {}, status),
      el("td", {}, a ? kst(a.created_at) : "—"),
      el("td", {}, a ? kst(a.updated_at) : "—"),
      el("td", { class: "wrap" }, a && a.message ? a.message : "—"),
      el("td", { class: "wrap ids" }, a && a.transfer_ids.length ? a.transfer_ids.join("\n") : "—"),
    );
  });
  return [
    el("h3", {}, "은행 확인 (ack / dispute)"),
    el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, ["은행", "결과", "처음 받은 시각", "마지막 시각", "dispute 메시지", "차이 난다는 transfer_id"].map((h) => el("th", {}, h)))),
      el("tbody", {}, rows.length ? rows : el("tr", {}, el("td", { colspan: 6, class: "muted" }, "없음"))),
    )),
  ];
}

function transfersBlock(d) {
  if (!state.transfers) {
    return [el("h3", {}, "거래 목록"),
      el("button", { type: "button", class: "ghost", onclick: () => loadTransfers(d.run_id).catch(handleError) }, `거래 ${d.tx_count}건 보기`)];
  }
  const rows = state.transfers.map((t) => el("tr", {},
    el("td", { class: "ids" }, t.transfer_id),
    el("td", {}, bankLabel(t.from_bank)),
    el("td", {}, bankLabel(t.to_bank)),
    el("td", { class: "num" }, won(t.amount)),
    el("td", {}, kst(t.finished_at)),
  ));
  return [el("h3", {}, `거래 목록 (${state.transfers.length}건)`),
    el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, ["transfer_id", "보낸 은행", "받은 은행", "금액", "끝난 시각"].map((h, i) => el("th", { class: i === 3 ? "num" : "" }, h)))),
      el("tbody", {}, rows.length ? rows : el("tr", {}, el("td", { colspan: 5, class: "muted" }, "없음"))),
    ))];
}

async function loadTransfers(runId) {
  state.transfers = await api(`/admin/settlements/${runId}/transfers`);
  renderDetail();
}

function actions(d) {
  if (d.status !== "DRAFT") return null;
  return el("div", { class: "actions" },
    el("button", { type: "button", class: "danger", onclick: () => confirmRun(d) }, "확정 (예치금 반영)"),
    el("button", { type: "button", class: "warn", onclick: () => cancelRun(d) }, "취소 (거래를 다음 정산으로)"),
  );
}

function renderDetail() {
  const d = state.detail;
  const box = $("detail");
  box.hidden = false;
  const parts = [
    el("div", { class: "card-head" }, el("h2", {}, `run #${d.run_id} 상세`), actions(d)),
    summary(d),
    deadlineNotice(d),
    positionsTable(d),
    acksTable(d),
    pairsTable(d),
    transfersBlock(d),
  ];
  box.replaceChildren(...parts.flat().filter(Boolean));
}

// ---- 관리 동작 (확인 대화상자를 거친다) ----

function ask({ title, body, needReason, okLabel }) {
  return new Promise((resolve) => {
    const dialog = $("confirm-dialog");
    $("confirm-title").textContent = title;
    $("confirm-body").textContent = body;
    $("confirm-reason-label").hidden = !needReason;
    $("confirm-reason").value = "";
    $("confirm-error").hidden = true;
    $("confirm-ok").textContent = okLabel;
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
    if (needReason) $("confirm-reason").focus();
  });
}

async function confirmRun(d) {
  const answer = await ask({
    title: `run #${d.run_id} 확정`,
    body: `정산일 ${d.settle_date}, ${d.tx_count}건 ${won(d.total_amount)}을 확정합니다.\n` +
      "은행별 순액이 예치금 원장에 반영되고 되돌릴 수 없습니다." +
      (d.disputed ? "\n\n⚠ dispute가 있는 run입니다. 관리자 판정으로 강제 확정합니다." : "") +
      (d.unanswered.length ? `\n\n아직 확인하지 않은 은행: ${d.unanswered.map(bankLabel).join(", ")}` : ""),
    needReason: true,
    okLabel: "확정",
  });
  if (!answer) return;
  try {
    await api(`/admin/settlements/${d.run_id}/confirm`, { method: "POST", body: { reason: answer.reason } });
    flash(`run #${d.run_id}을 확정했습니다`);
    await loadAll();
  } catch (err) { handleError(err); }
}

async function cancelRun(d) {
  const answer = await ask({
    title: `run #${d.run_id} 취소`,
    body: `정산일 ${d.settle_date}, ${d.tx_count}건 ${won(d.total_amount)}의 DRAFT를 취소합니다.\n` +
      "거래는 미정산으로 돌아가 다음 정산 run에 다시 들어갑니다. 은행의 ack/dispute 기록은 남습니다.\n" +
      "(취소 API는 사유를 받지 않습니다)",
    needReason: false,
    okLabel: "취소 실행",
  });
  if (!answer) return;
  try {
    await api(`/admin/settlements/${d.run_id}/cancel`, { method: "POST" });
    flash(`run #${d.run_id}을 취소했습니다`);
    await loadAll();
  } catch (err) { handleError(err); }
}

async function createRun(event) {
  event.preventDefault();
  const date = $("run-date").value;
  const answer = await ask({
    title: "수동 정산 run 만들기",
    body: (date ? `정산일 ${date}` : "정산일을 비워 두면 어제") + "로 DRAFT를 만듭니다.\n" +
      "오늘 날짜면 지금 시각으로 즉시 마감합니다. 확정하지 않은 DRAFT가 있으면 만들 수 없습니다 (409).",
    needReason: false,
    okLabel: "만들기",
  });
  if (!answer) return;
  try {
    const run = await api("/admin/settlements/run", { method: "POST", body: date ? { settle_date: date } : {} });
    flash(`run #${run.run_id}을 만들었습니다 (${run.tx_count}건 ${won(run.total_amount)})`);
    state.selected = run.run_id;
    history.replaceState(null, "", `#run=${run.run_id}`);
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
    state.detail = null;
    $("detail").replaceChildren();
    showLogin();
  });
  $("refresh").addEventListener("click", () => { $("flash").hidden = true; loadAll().catch(handleError); });
  $("run-form").addEventListener("submit", createRun);

  tick();
  setInterval(() => {
    tick();
    if (state.detail && state.detail.status === "DRAFT" && !$("detail").hidden) renderDetail(); // 남은 시간 갱신
  }, 30000);

  if (sessionStorage.getItem(TOKEN_KEY)) loadAll().catch(handleError);
  else showLogin();
});
