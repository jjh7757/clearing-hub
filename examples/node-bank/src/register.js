// register.js — 셀프 등록 POST /v1/banks/register (SPEC 3.5). 서명하지 않는 유일한 요청이다 (초대 코드가 인증).
// 받는 쪽 서버(bank.js)를 먼저 띄워 둔다: 허브가 등록 직후 내 /inquiry를 불러 ping한다.
'use strict';

const { need, upsertEnv } = require('./config');
const { fingerprint, verifyResponse } = require('./signing');
const { HUB } = require('./hub');

async function main() {
  const inviteFingerprint = need('HUB_FINGERPRINT'); // 초대 문구(관리자 DM)의 허브 공개키 지문
  const hubPublicKey = need('HUB_PUBLIC_KEY'); // 초대 문구의 허브 공개키
  if (fingerprint(hubPublicKey) !== inviteFingerprint) {
    console.error('HUB_PUBLIC_KEY의 지문이 초대 문구의 지문과 다릅니다. 키를 다시 확인하세요 (2.6).');
    process.exit(1);
  }

  const payload = {
    invite_code: need('INVITE_CODE'),
    name: process.env.BANK_NAME || 'NodeBank',
    base_url: process.env.BASE_URL || 'http://127.0.0.1:3000/interbank', // 운영은 공개 HTTPS (3.5)
    public_key: need('BANK_PUBLIC_KEY'),
  };
  const body = Buffer.from(JSON.stringify(payload), 'utf8');
  const res = await fetch(`${HUB}/v1/banks/register`, {
    method: 'POST', headers: { 'Content-Type': 'application/json; charset=utf-8' }, body,
    signal: AbortSignal.timeout(15_000),
  });
  const raw = Buffer.from(await res.arrayBuffer());
  // 등록 응답도 허브가 서명한다. 요청에 서명이 없었으므로 request_signature는 "" (3.5, 2.5)
  const signatureOk = verifyResponse(hubPublicKey, res.headers.get('x-response-timestamp'), res.status, '',
    raw, res.headers.get('x-response-signature'));
  const data = JSON.parse(raw.toString('utf8'));
  console.log(`POST /v1/banks/register → ${res.status}  허브 응답 서명 ${signatureOk ? '맞음' : '틀림'}`);

  if (res.status !== 201) {
    console.log(JSON.stringify(data, null, 2));
    process.exit(1);
  }
  const fpOk = data.hub_fingerprint === inviteFingerprint && fingerprint(data.hub_public_key) === inviteFingerprint;
  console.log(`- bank_code: ${data.bank_code}  name: ${data.name}  net_debit_cap: ${data.net_debit_cap}`);
  console.log(`- api_key: ${data.api_key.slice(0, 6)}…(.env에만 저장, 이 응답에만 있다)`);
  console.log(`- hub_fingerprint: ${data.hub_fingerprint}  초대 문구 지문과 ${fpOk ? '같음' : '다름 → 이 허브를 믿지 않는다'}`);
  console.log(`- ping.ok: ${data.ping.ok}`, JSON.stringify(data.ping));
  if (!signatureOk || !fpOk) {
    console.error('허브 응답 서명이나 지문이 맞지 않아 저장하지 않습니다.');
    process.exit(1);
  }
  upsertEnv({ BANK_CODE: data.bank_code, BANK_API_KEY: data.api_key });
  console.log('BANK_CODE·BANK_API_KEY를 .env에 저장했습니다.');
  if (!data.ping.ok) console.log('ping.ok가 false입니다. SPEC 3.6의 ping 결과 표를 보고 고친 뒤 POST /v1/me/ping으로 다시 확인하세요.');
}

main().catch((e) => { console.error(e); process.exit(1); });
