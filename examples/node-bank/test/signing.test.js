// SPEC 2.3 예시 1·2, 2.5 예시 3의 고정 값이 그대로 나오는지 확인한다 (`node --test`).
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const s = require('../src/signing');

// 공개된 검증 전용 예시 키 (SPEC 2.3). 운영에서 쓰지 않는다
const BANK_KEY = 'D7frsUqSl9ZuC+7EpHfY1+w6+dAlWqg8vuHVtZfdpg4=';
const BANK_PUB = 'gxCDPKbN/rIXKDJgn4GZh5JY7I65Cwbk3A/mJjTtPzY=';
const HUB_KEY = 'cycR/h4ydSTPkBGuRFO340cDtdAdwrXQjKC4N1GPgtQ=';
const HUB_PUB = 'yZdBWoR7swp/3CQvh4c58oahhbjxVeaxWWtqmqwDXdo=';
const TID = '7c9e6679-7425-40de-944b-e07fc1f90ae7';

const body1 = Buffer.from(JSON.stringify({
  transfer_id: TID, to_bank: '902', to_account: '110-123-0001', from_account: '110-200-456789',
  amount: 50000, sender_name: '홍길동', memo: '생활비',
}), 'utf8');
const S1 = 'SY/3NCjTxAR1dC5UXHM/PkQZR9/SMa+wcDBSIrAit0jeHW5JKKgw4vZWKiN2Z7o5V+ywCY7EyWsTEfLf/F3mBw==';
const S2 = 'IMn6yCKkn/eZGPNY74b3+cNvA/4ax9knE97hh4wCQnPEHe6IEdKREh/Pit3+7bJJjx801OiRbJIvJD0HbKzPAA==';
const S3 = 'D84tooOZEJ6jehp/BN6dVPw1KZCB+eejxpZOqgggeTW7iJznoyNjQGEFwShbhnX7prHkg9uG9e7rP9IxZS91Dg==';

test('예시 1: 은행 → 허브 POST /v1/transfers (JSON.stringify가 SPEC의 190바이트와 같다)', () => {
  assert.equal(body1.length, 190);
  assert.equal(s.signRequest(BANK_KEY, 'POST', '/v1/transfers', body1, '1762300800')['X-Signature'], S1);
  assert.ok(s.verify(BANK_PUB, Buffer.from(`1762300800\nPOST\n/v1/transfers\n${body1}`), S1));
});

test('예시 2: 허브 → 은행 GET /receive/{id} (본문 없음)', () => {
  assert.equal(s.signRequest(HUB_KEY, 'GET', `/receive/${TID}`, '', '1762300800')['X-Signature'], S2);
  assert.ok(s.verify(HUB_PUB, Buffer.from(`1762300800\nGET\n/receive/${TID}\n`), S2));
});

test('예시 3: 예시 2에 대한 은행의 200 응답 서명', () => {
  const body3 = Buffer.from(JSON.stringify({
    transfer_id: TID, status: 'COMPLETED', holder_name: '김철수', received_at: '2026-11-05T01:20:02Z',
  }), 'utf8');
  assert.equal(body3.length, 138);
  assert.equal(s.signResponse(BANK_KEY, 200, S2, body3, '1762300801')['X-Response-Signature'], S3);
});

test('지문 (SPEC 2.3의 d61a340fe933017d, 239a9acfc7ecb8de)', () => {
  assert.equal(s.fingerprint(BANK_PUB), 'd61a340fe933017d');
  assert.equal(s.fingerprint(HUB_PUB), '239a9acfc7ecb8de');
});

test('지금 시각 서명은 검증을 통과하고, 틀린 값은 예외 없이 false', () => {
  const h = s.signRequest(BANK_KEY, 'GET', '/v1/banks', '');
  assert.ok(s.verifyRequest(BANK_PUB, h['X-Timestamp'], 'GET', '/v1/banks', '', h['X-Signature']));
  assert.equal(s.verifyRequest(BANK_PUB, h['X-Timestamp'], 'GET', '/banks', '', h['X-Signature']), false);
  assert.equal(s.verifyRequest(BANK_PUB, '1762300800', 'GET', '/v1/banks', '', h['X-Signature']), false); // 5분 초과
  assert.equal(s.verify('not-base64!', Buffer.from('x'), S1), false);
  assert.equal(s.verify(BANK_PUB, Buffer.from('x'), S1.replace(/=+$/, '')), false); // 패딩 없음
});

test('생성한 키 쌍: 44자 base64, 서명·검증 왕복', () => {
  const { privateKey, publicKey } = s.generateKeypair();
  assert.equal(privateKey.length, 44);
  assert.equal(publicKey.length, 44);
  const h = s.signResponse(privateKey, 404, '', '{}');
  assert.ok(s.verifyResponse(publicKey, h['X-Response-Timestamp'], 404, '', '{}', h['X-Response-Signature']));
});
