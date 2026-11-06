// hub.js — 은행 → 허브 호출 (SPEC 3절). 요청에 서명하고, 허브 응답 서명을 검증한다 (2.2, 2.5).
'use strict';

const crypto = require('node:crypto');
const { need } = require('./config');
const { signRequest, verifyResponse } = require('./signing');

const HUB = process.env.HUB || 'http://127.0.0.1:8000'; // 운영은 https://clearing-hub.ai-agent-develop.cloud

/**
 * 허브를 부른다.
 * @param {string} method  GET·POST
 * @param {string} path    /v1부터의 경로. 서명의 PATH도 이 값이다 (2.2)
 * @param {object} [payload] JSON 본문. 없으면 본문 없이 보내고 서명의 body는 ""
 * @param {object} [opts]  signPath: 서명에만 다른 PATH를 쓴다 (일부러 틀리기 시험용)
 * @returns {{status:number, body:object, signatureOk:boolean, raw:Buffer, headers:Headers}}
 */
async function call(method, path, payload, opts = {}) {
  // JSON.stringify는 공백 없이, 한글을 그대로 UTF-8로 낸다 → SPEC 예시 1과 같은 바이트. 이 바이트를 서명하고 그대로 보낸다
  const body = payload === undefined ? Buffer.alloc(0) : Buffer.from(JSON.stringify(payload), 'utf8');
  const signed = signRequest(need('BANK_PRIVATE_KEY'), method, opts.signPath || path, body);
  const headers = {
    'X-Bank-Code': need('BANK_CODE'),
    'X-Api-Key': need('BANK_API_KEY'),
    'X-Request-ID': crypto.randomUUID(), // 선택 (2.4)
    ...signed,
  };
  if (body.length) headers['Content-Type'] = 'application/json; charset=utf-8';

  const res = await fetch(HUB + path, {
    method, headers, body: body.length ? body : undefined, signal: AbortSignal.timeout(10_000),
  });
  const raw = Buffer.from(await res.arrayBuffer()); // 검증은 받은 바이트 그대로 (다시 직렬화하지 않는다)
  const signatureOk = verifyResponse(
    need('HUB_PUBLIC_KEY'), res.headers.get('x-response-timestamp'), res.status,
    signed['X-Signature'], raw, res.headers.get('x-response-signature'),
  );
  let parsed = {};
  try { parsed = raw.length ? JSON.parse(raw.toString('utf8')) : {}; } catch { parsed = { _raw: raw.toString('utf8') }; }
  console.log(`${method} ${path} → ${res.status}  허브 응답 서명 ${signatureOk ? '맞음' : '틀림 → 본문을 믿지 않는다 (2.5)'}`);
  return { status: res.status, body: parsed, signatureOk, raw, headers: res.headers };
}

/** 서명 없이 X-Bank-Code·X-Api-Key만 (3.8 연동 진단). 응답 서명의 request_signature는 "". */
async function callUnsigned(path) {
  const res = await fetch(HUB + path, {
    headers: { 'X-Bank-Code': need('BANK_CODE'), 'X-Api-Key': need('BANK_API_KEY') },
    signal: AbortSignal.timeout(10_000),
  });
  const raw = Buffer.from(await res.arrayBuffer());
  const signatureOk = verifyResponse(
    need('HUB_PUBLIC_KEY'), res.headers.get('x-response-timestamp'), res.status, '', raw,
    res.headers.get('x-response-signature'),
  );
  console.log(`GET ${path} → ${res.status}  허브 응답 서명 ${signatureOk ? '맞음' : '틀림'}`);
  return { status: res.status, body: JSON.parse(raw.toString('utf8') || '{}'), signatureOk };
}

module.exports = { call, callUnsigned, HUB };
