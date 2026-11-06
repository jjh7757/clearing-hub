// send.js — 보내는 쪽: 가짜 은행 902로 이체 (SPEC 빠른 시작 5단계, 3.1~3.4, 6절)
//   node src/send.js              은행 목록 → 수취 조회 → 이체 → 없는 계좌 → 같은 transfer_id 재전송
//   node src/send.js --wrong-path 일부러 서명 PATH에서 /v1을 빼고 보내 401의 hint와 연동 진단을 본다 (3.8)
'use strict';

const crypto = require('node:crypto');
const { call, callUnsigned } = require('./hub');

/**
 * 허브 응답에 따라 출금을 마무리한다 (6절). 되돌리는 것은 본문의 status가 "FAILED"일 때만이다 (3.3).
 * 이 예제는 메모리 장부가 없으므로 할 일을 콘솔에 적기만 한다.
 */
function settle(transferId, r) {
  const status = r.body && r.body.status;
  let action;
  if (!r.signatureOk) action = '허브 응답 서명이 틀림 → 본문을 믿지 않고 "모르는 상태" 유지, GET /v1/transfers/{id}로 확인 (2.5)';
  else if (r.status === 200 && status === 'COMPLETED') action = '완료: 중간 계정 차감, 내 거래 COMPLETED';
  else if (status === 'FAILED') action = `되돌림: 중간 계정 → 고객 계좌, 내 거래 FAILED (사유 ${r.body.error})`;
  else if (r.status === 202) action = '되돌리지 않음: PENDING, 30초마다 GET /v1/transfers/{id} (6절 4-1)';
  else if (r.status >= 500) action = '되돌리지 않음: 5xx는 결과 모름, 같은 transfer_id로 재전송';
  else action = `되돌리지 않음: status 없는 ${r.status}은 이체 결과가 아님. 원인(${r.body.error})을 고치고 같은 transfer_id로 재전송`;
  console.log(`  [장부] ${transferId}: ${action}`);
}

function newTransfer(toAccount, amount) {
  return {
    transfer_id: crypto.randomUUID(), // 소문자 UUID v4 (Node randomUUID는 소문자)
    to_bank: '902', to_account: toAccount, from_account: '110-200-456789',
    amount, sender_name: '홍길동', memo: 'Node 예제',
  };
}

async function normal() {
  const me = process.env.BANK_CODE;
  const banks = await call('GET', '/v1/banks');
  const others = banks.body.banks.filter((b) => b.bank_code !== me); // 자기 은행은 받는 은행 목록에서 뺀다 (3.1)
  console.log('  받는 은행 선택지:', others.map((b) => `${b.bank_code} ${b.name}`).join(', '));
  if (!others.some((b) => b.bank_code === '902')) throw new Error('902가 목록에 없음');

  const inq = await call('POST', '/v1/inquiry', { to_bank: '902', to_account: '110-123-0001' });
  console.log('  예금주:', inq.body.holder_name);

  // 실제로는 이 요청 전에 내 DB에서 출금(중간 계정으로)을 한 트랜잭션으로 끝내 둔다 (6절 3)
  const t1 = newTransfer('110-123-0001', 1000);
  const r1 = await call('POST', '/v1/transfers', t1);
  console.log('  ', JSON.stringify(r1.body));
  settle(t1.transfer_id, r1);

  const t2 = newTransfer('110-123-0002', 1000); // 902의 0002 = 계좌 없음
  const r2 = await call('POST', '/v1/transfers', t2);
  console.log('  ', JSON.stringify(r2.body));
  settle(t2.transfer_id, r2);

  // 같은 transfer_id로 다시 보내기: 서명은 새로 만들고 본문은 같게 (2.2). 허브는 처음 결과를 준다 (3.3)
  const r1b = await call('POST', '/v1/transfers', t1);
  const same = r1b.status === r1.status && JSON.stringify(r1b.body) === JSON.stringify(r1.body);
  console.log('  재전송:', JSON.stringify(r1b.body), same ? '→ 처음과 같은 결과' : '→ 처음과 다름!');

  const q = await call('GET', `/v1/transfers/${t1.transfer_id}`); // 3.4 결과 조회
  console.log('  결과 조회:', JSON.stringify(q.body));
}

async function wrongPath() {
  // 서명 PATH에서 /v1을 뺀다: 실제 주소는 /v1/banks, 서명은 /banks
  const r = await call('GET', '/v1/banks', undefined, { signPath: '/banks' });
  console.log('  401 본문:', JSON.stringify(r.body, null, 2));
  const d = await callUnsigned('/v1/diagnose/failures');
  const first = d.body.findings && d.body.findings[0];
  console.log('  진단 1건째:', JSON.stringify(first, null, 2));
}

(process.argv.includes('--wrong-path') ? wrongPath() : normal()).catch((e) => { console.error(e); process.exit(1); });
