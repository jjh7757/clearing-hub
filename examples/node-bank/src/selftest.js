// selftest.js — 받는 쪽 시험 (SPEC 빠른 시작 6단계, 3.7, 7절 7). bank.js가 떠 있어야 한다.
//   node src/selftest.js         정상 이체 → 재전송 → 입금 확인 → 없는 계좌
//   node src/selftest.js --slow  입금 후 응답 지연(202 PENDING) 시험을 더한다. 1분쯤 뒤 hub_status를 다시 본다
// 하루(KST) 20번까지 (429 TOO_MANY_TESTS).
'use strict';

const { call } = require('./hub');

const BANK_LOCAL = `http://127.0.0.1:${process.env.PORT || 3000}/_local/received`; // bank.js의 연습용 확인 창구

async function myCount(tid) {
  const r = await (await fetch(BANK_LOCAL)).json();
  return { total: r.count, thisOne: r.transfers.filter((t) => t.transfer_id === tid).length, r };
}

function show(r) {
  const { receiver_reply: reply, ...rest } = r.body;
  console.log('   hub:', JSON.stringify(rest));
  if (reply) console.log('   receiver_reply:', JSON.stringify(reply));
}

async function main() {
  // 1. 정상 계좌로 소액 이체
  const r1 = await call('POST', '/v1/test-receive', { to_account: '110-123-000001', amount: 1000 });
  show(r1);
  const tid = r1.body.transfer_id;
  const c1 = await myCount(tid);
  console.log(`   내 Map: 이 거래 ${c1.thisOne}건, 거래내역 "${c1.r.transfers.find((t) => t.transfer_id === tid)?.history}"`);

  // 2. 같은 transfer_id 재전송 (멱등, 4.2 규칙 1)
  const r2 = await call('POST', `/v1/test-receive/${tid}/resend`);
  show(r2);
  const c2 = await myCount(tid);
  console.log(`   재전송 뒤 내 Map: 이 거래 ${c2.thisOne}건 (1이어야 함), 계좌 잔액 ${c2.r.balances['110-123-000001']}원`);

  // 3. 입금 확인 (4.3)
  const r3 = await call('GET', `/v1/test-receive/${tid}/status`);
  show(r3);

  // 4. 없는 계좌 → 404 ACCOUNT_NOT_FOUND, status FAILED, 저장 안 함 → /status도 404
  const r4 = await call('POST', '/v1/test-receive', { to_account: '999-999-999999', amount: 1000 });
  show(r4);
  if (r4.body.transfer_id) {
    const r5 = await call('GET', `/v1/test-receive/${r4.body.transfer_id}/status`);
    show(r5);
  }

  if (process.argv.includes('--slow')) {
    // 5. 입금을 끝낸 뒤 6초 늦게 답함 → 허브는 202 PENDING, 1분쯤 뒤 4.3으로 확인해 COMPLETED
    const r6 = await call('POST', '/v1/test-receive', { to_account: '110-123-000009', amount: 1000 });
    show(r6);
    console.log('   70초 기다렸다가 hub_status를 다시 본다…');
    await new Promise((r) => setTimeout(r, 70_000));
    show(await call('GET', `/v1/test-receive/${r6.body.transfer_id}/status`));
  }
}

main().catch((e) => { console.error(e); process.exit(1); });
