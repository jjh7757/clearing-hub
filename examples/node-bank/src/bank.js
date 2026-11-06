// bank.js — 받는 쪽: 허브가 부르는 API 3개 (SPEC 4절)와 응답 서명 (2.5). 저장은 메모리라 연습용이다.
// 실제 서버로 옮길 때: transfer_id에 유니크 제약을 건 테이블, 입금과 기록을 한 트랜잭션으로 (4.2 규칙 1·2). README 참고.
'use strict';

const express = require('express');
const { need } = require('./config');
const { signResponse, verifyRequest } = require('./signing');

const BANK_PRIVATE_KEY = need('BANK_PRIVATE_KEY'); // 응답 서명용 내 개인키
const HUB_PUBLIC_KEY = need('HUB_PUBLIC_KEY'); // 허브 요청 검증용 (초대 문구의 지문과 맞춰 본 키)
const PORT = Number(process.env.PORT || 3000);
const PREFIX = '/interbank'; // base_url의 경로 부분

// 계좌번호 → 예금주·잔액 (실제로는 DB). 시험용 계좌를 따로 둔다 (3.7)
const accounts = new Map([
  ['110-123-000001', { holder: '김민수', balance: 0 }], // 셀프 시험용
  ['110-123-000002', { holder: '이영희', balance: 0 }],
  ['110-123-000009', { holder: '박지연', balance: 0, slow: true }], // 입금 후 6초 늦게 답함 (7절 7의 지연 시험)
]);
// transfer_id → 입금 기록. 실제로는 transfer_id 유니크 제약 + 입금과 한 트랜잭션 (4.2)
const received = new Map();

const app = express();
app.set('etag', false); // 본문·상태를 바꾸는 것은 서명 뒤에 두지 않는다 (2.5)
app.set('x-powered-by', false);

// ① 응답 서명 미들웨어: 가장 바깥에 둔다. 서명 검증 실패 401, 없는 경로 404, 본문 오류 422에도 붙는다 (2.5).
//    Express는 res.json → res.send → res.end(본문 바이트) 순서라, 실제로 나가는 바이트가 정해지는 res.end에서 서명한다.
app.use((req, res, next) => {
  const requestSignature = req.get('x-signature') || ''; // 요청에 서명이 없었으면 ""
  const end = res.end;
  res.end = function signedEnd(chunk, encoding, cb) {
    if (typeof chunk === 'function') { cb = chunk; chunk = undefined; encoding = undefined; }
    if (typeof encoding === 'function') { cb = encoding; encoding = undefined; }
    const body = chunk === undefined || chunk === null ? Buffer.alloc(0)
      : Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk, encoding || 'utf8');
    if (res.statusCode < 500 && !res.headersSent) { // 5xx는 서명하지 않아도 된다
      for (const [k, v] of Object.entries(signResponse(BANK_PRIVATE_KEY, res.statusCode, requestSignature, body))) {
        res.setHeader(k, v);
      }
    }
    return end.call(this, body, cb);
  };
  next();
});

// ② 원본 바이트로 받는다. JSON으로 먼저 풀면 서명을 검증할 수 없다 (2.2).
//    type을 함수로 줘서 Content-Type과 상관없이 받는다. 본문이 없는 GET은 req.body가 Buffer가 아니라 {}가 된다.
app.use(express.raw({ type: () => true, limit: '64kb' }));

function error(res, status, code, message, extra = {}) {
  return res.status(status).json({ error: code, message, ...extra });
}

function log(req, note) {
  console.log(`[bank] ${req.method} ${req.originalUrl} rid=${req.get('x-request-id') || '-'} ${note}`);
}

// ③ 허브 서명 검증 (2.2): 원본 바이트, 허브 공개키, PATH는 base_url 뒤 부분. 입금 처리보다 먼저 한다 (4.2 규칙 3).
const interbank = express.Router();
interbank.use((req, res, next) => {
  const body = Buffer.isBuffer(req.body) ? req.body : Buffer.alloc(0);
  const ts = req.get('x-timestamp');
  const signature = req.get('x-signature');
  const alg = req.get('x-signature-alg') || 'ed25519'; // 생략하면 ed25519 (2.1)
  // 라우터를 /interbank에 붙였으므로 req.path는 이미 base_url 뒤 부분이다 (/interbank/receive → /receive)
  const path = req.path;
  if (req.get('x-bank-code') !== '000' || !ts || !signature) {
    log(req, '401 UNAUTHORIZED');
    return error(res, 401, 'UNAUTHORIZED', '허브 요청이 아니거나 서명 헤더가 없습니다');
  }
  if (alg !== 'ed25519') return error(res, 401, 'SIGNATURE_INVALID', 'X-Signature-Alg는 ed25519만 받습니다');
  if (!/^\d+$/.test(ts) || Math.abs(Date.now() / 1000 - Number(ts)) > 300) {
    log(req, '401 SIGNATURE_EXPIRED');
    return error(res, 401, 'SIGNATURE_EXPIRED', '요청 시각이 5분 넘게 차이 납니다');
  }
  if (!verifyRequest(HUB_PUBLIC_KEY, ts, req.method, path, body, signature)) {
    log(req, `401 SIGNATURE_INVALID (path=${path})`);
    return error(res, 401, 'SIGNATURE_INVALID', '서명이 맞지 않습니다');
  }
  if (body.length) {
    try {
      req.json = JSON.parse(body.toString('utf8'));
    } catch {
      return error(res, 422, 'VALIDATION_ERROR', '본문이 JSON이 아닙니다');
    }
    if (typeof req.json !== 'object' || req.json === null || Array.isArray(req.json)) {
      return error(res, 422, 'VALIDATION_ERROR', '본문은 JSON 객체여야 합니다');
    }
  } else {
    req.json = {};
  }
  next();
});

const isStr = (v, max) => typeof v === 'string' && v.length >= 1 && v.length <= max;

// 4.1 수취 조회
interbank.post('/inquiry', (req, res) => {
  const account = req.json.account_num;
  if (!isStr(account, 50)) return error(res, 422, 'VALIDATION_ERROR', 'account_num이 없거나 형식이 틀립니다');
  const acct = accounts.get(account);
  log(req, `inquiry ${account} → ${acct ? 200 : 404}`);
  if (!acct) return error(res, 404, 'ACCOUNT_NOT_FOUND', '받는 계좌를 찾을 수 없습니다');
  return res.json({ holder_name: acct.holder });
});

// 4.2 입금
interbank.post('/receive', async (req, res) => {
  const d = req.json;
  const tid = d.transfer_id;
  if (!isStr(tid, 36)) return error(res, 422, 'VALIDATION_ERROR', 'transfer_id가 없거나 형식이 틀립니다');

  const done = received.get(tid);
  if (done) { // 같은 ID가 다시 오면 입금하지 않고 같은 답 (4.2 규칙 1). 본문은 비교하지 않는다
    log(req, `receive ${tid} 재전송 → 200 (입금 안 함)`);
    return res.json({ transfer_id: tid, status: 'COMPLETED', holder_name: done.holder_name });
  }

  // 필드 검사 (4.2 필드 = 3.3 필드 표). 거절(4xx)한 거래는 기록하지 않는다 (4.2 규칙 5)
  const bad = [];
  if (!/^\d{3}$/.test(d.from_bank || '')) bad.push('from_bank');
  if (!isStr(d.from_bank_name, 50)) bad.push('from_bank_name');
  if (!isStr(d.from_account, 50)) bad.push('from_account');
  if (!isStr(d.to_account, 50)) bad.push('to_account');
  if (!Number.isSafeInteger(d.amount) || d.amount < 1) bad.push('amount');
  if (!isStr(d.sender_name, 50)) bad.push('sender_name');
  if (d.memo !== undefined && (typeof d.memo !== 'string' || d.memo.length > 100)) bad.push('memo');
  if (bad.length) return error(res, 422, 'VALIDATION_ERROR', `필드 형식 오류: ${bad.join(', ')}`, { transfer_id: tid });

  const acct = accounts.get(d.to_account);
  if (!acct) {
    log(req, `receive ${tid} → 404 ACCOUNT_NOT_FOUND (기록 안 함)`);
    return error(res, 404, 'ACCOUNT_NOT_FOUND', '받는 계좌를 찾을 수 없습니다', { transfer_id: tid });
  }

  // [실제로는 한 트랜잭션] 고객 계좌 입금 + transfer_id 기록. 유니크 제약에 걸리면 4xx가 아니라
  // 저장된 결과로 200, 아직 못 읽으면 5xx (4.2 규칙 2). 여기서는 Node가 한 스레드라 이 두 줄 사이에 끼어들 요청이 없다.
  acct.balance += d.amount;
  received.set(tid, {
    ...d, holder_name: acct.holder,
    history: `${d.from_bank_name} ${d.sender_name}`, // 거래내역 표시 (4.2 규칙 6)
    received_at: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'),
  });
  log(req, `receive ${tid} → 200 입금 ${d.amount}원 (${d.from_bank_name} ${d.sender_name})`);

  if (acct.slow) await new Promise((r) => setTimeout(r, 6000)); // 입금은 끝났는데 허브 타임아웃(5초)보다 늦게 답한다
  return res.json({ transfer_id: tid, status: 'COMPLETED', holder_name: acct.holder });
});

// 4.3 입금 확인: 입금한 거래는 200 COMPLETED, 그 밖은 모두 404 TRANSFER_NOT_FOUND
interbank.get('/receive/:transferId', (req, res) => {
  const tid = req.params.transferId;
  const record = received.get(tid);
  log(req, `receive-status ${tid} → ${record ? 200 : 404}`);
  if (!record) return error(res, 404, 'TRANSFER_NOT_FOUND', '그 거래 ID로 입금한 적이 없습니다', { transfer_id: tid });
  return res.json({
    transfer_id: tid, status: 'COMPLETED', holder_name: record.holder_name, received_at: record.received_at,
  });
});

app.use(PREFIX, interbank);

// 연습용 확인 창구: 내 Map에 몇 건 들어 있는지 (selftest.js가 멱등을 확인할 때 쓴다). 실제 서버에는 두지 않는다.
// 허브 서명 검증 라우터 밖이며, 127.0.0.1로만 띄우므로 이 컴퓨터에서만 보인다.
app.get('/_local/received', (req, res) => {
  res.json({
    count: received.size,
    transfers: [...received.values()].map((r) => ({
      transfer_id: r.transfer_id, to_account: r.to_account, amount: r.amount, history: r.history,
    })),
    balances: Object.fromEntries([...accounts].map(([k, v]) => [k, v.balance])),
  });
});

// 없는 경로·메서드는 404 NOT_FOUND (5절 형식, 서명 붙음)
app.use((req, res) => error(res, 404, 'NOT_FOUND', '없는 경로입니다'));

// 처리하지 못한 오류: 500 INTERNAL_ERROR, 내부 내용은 본문에 넣지 않는다 (5절)
// eslint-disable-next-line no-unused-vars
app.use((err, req, res, next) => {
  if (err.type === 'entity.too.large') return error(res, 422, 'VALIDATION_ERROR', '본문이 너무 큽니다');
  console.error('[bank] 처리 못 한 오류', err);
  return error(res, 500, 'INTERNAL_ERROR', '은행 내부 오류');
});

app.listen(PORT, '127.0.0.1', () => {
  console.log(`[bank] 받는 쪽 서버 http://127.0.0.1:${PORT}${PREFIX}  (base_url로 등록할 주소)`);
});
