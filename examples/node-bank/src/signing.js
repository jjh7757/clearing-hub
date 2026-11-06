// signing.js — SPEC 2.2·2.3·2.5의 서명·검증을 Node.js 내장 crypto(Ed25519)로 구현한다.
// 키와 서명은 raw 바이트의 표준 base64다 (키 32바이트 44자, 서명 64바이트 88자, SPEC 2.2).
// Node crypto는 Ed25519 raw 키를 바로 받지 않으므로, SPEC 빠른 시작 2단계의 안내대로
// 고정 DER 접두사를 붙여 PKCS#8(개인키)·SPKI(공개키) DER로 읽는다.
'use strict';

const crypto = require('node:crypto');

const PKCS8_PREFIX = Buffer.from('302e020100300506032b657004220420', 'hex'); // + 개인키 32바이트
const SPKI_PREFIX = Buffer.from('302a300506032b6570032100', 'hex'); // + 공개키 32바이트
const B64 = /^[A-Za-z0-9+/]+={0,2}$/; // 표준 base64만 (URL-safe·공백 거절)

function strictB64(value, bytes) {
  if (typeof value !== 'string' || !B64.test(value) || value.length % 4 !== 0) throw new Error('base64 형식 아님');
  const buf = Buffer.from(value, 'base64'); // Node는 틀린 문자를 조용히 버리므로 위에서 먼저 검사한다
  if (bytes && buf.length !== bytes) throw new Error(`길이 ${buf.length}바이트 (기대 ${bytes})`);
  return buf;
}

function privateKeyFromB64(privateKeyB64) {
  const raw = strictB64(privateKeyB64, 32);
  return crypto.createPrivateKey({ key: Buffer.concat([PKCS8_PREFIX, raw]), format: 'der', type: 'pkcs8' });
}

function publicKeyFromB64(publicKeyB64) {
  const raw = strictB64(publicKeyB64, 32);
  return crypto.createPublicKey({ key: Buffer.concat([SPKI_PREFIX, raw]), format: 'der', type: 'spki' });
}

/** 키 쌍 만들기 (SPEC 1단계·2.6). raw 32바이트를 DER 끝에서 잘라 base64로 돌려준다. */
function generateKeypair() {
  const { privateKey, publicKey } = crypto.generateKeyPairSync('ed25519');
  const priv = privateKey.export({ format: 'der', type: 'pkcs8' }).subarray(-32);
  const pub = publicKey.export({ format: 'der', type: 'spki' }).subarray(-32);
  return { privateKey: priv.toString('base64'), publicKey: pub.toString('base64') };
}

/** 지문 = sha256(raw 공개키 32바이트)의 16진수 앞 16자 (SPEC 2.6). */
function fingerprint(publicKeyB64) {
  return crypto.createHash('sha256').update(strictB64(publicKeyB64, 32)).digest('hex').slice(0, 16);
}

function toBuffer(body) {
  if (body === undefined || body === null) return Buffer.alloc(0);
  return Buffer.isBuffer(body) ? body : Buffer.from(body, 'utf8');
}

function sign(privateKeyB64, message) {
  return crypto.sign(null, message, privateKeyFromB64(privateKeyB64)).toString('base64');
}

/** 키·서명이 base64가 아니거나 길이가 틀려도 예외(500) 대신 false(401). */
function verify(publicKeyB64, message, signatureB64) {
  try {
    return crypto.verify(null, message, publicKeyFromB64(publicKeyB64), strictB64(signatureB64, 64));
  } catch {
    return false;
  }
}

function fresh(ts, skew = 300) {
  if (typeof ts !== 'string' || !/^\d+$/.test(ts)) return false;
  return Math.abs(Math.floor(Date.now() / 1000) - Number(ts)) <= skew;
}

function now() {
  return String(Math.floor(Date.now() / 1000));
}

function requestMessage(ts, method, path, body) {
  return Buffer.concat([Buffer.from(`${ts}\n${method.toUpperCase()}\n${path}\n`, 'utf8'), toBuffer(body)]);
}

function responseMessage(ts, status, requestSignature, body) {
  return Buffer.concat([Buffer.from(`${ts}\n${status}\n${requestSignature || ''}\n`, 'utf8'), toBuffer(body)]);
}

/** 요청 서명 (2.2). path: 은행→허브는 /v1부터, 허브→은행은 base_url 뒤 부분. body: 보내는 바이트 그대로. */
function signRequest(privateKeyB64, method, path, body, ts = now()) {
  return {
    'X-Timestamp': ts,
    'X-Signature': sign(privateKeyB64, requestMessage(ts, method, path, body)),
    'X-Signature-Alg': 'ed25519',
  };
}

function verifyRequest(publicKeyB64, ts, method, path, body, signature) {
  return fresh(ts) && verify(publicKeyB64, requestMessage(ts, method, path, body), signature);
}

/** 응답 서명 (2.5). requestSignature: 그 요청의 X-Signature 그대로(없었으면 ""). body: 보내는 바이트 그대로. */
function signResponse(privateKeyB64, status, requestSignature, body, ts = now()) {
  return {
    'X-Response-Timestamp': ts,
    'X-Response-Signature': sign(privateKeyB64, responseMessage(ts, status, requestSignature, body)),
    'X-Signature-Alg': 'ed25519',
  };
}

function verifyResponse(publicKeyB64, ts, status, requestSignature, body, signature) {
  return fresh(ts) && verify(publicKeyB64, responseMessage(ts, status, requestSignature, body), signature);
}

module.exports = {
  generateKeypair, fingerprint, sign, verify,
  signRequest, verifyRequest, signResponse, verifyResponse,
};

// `node src/signing.js keygen` → 키 쌍을 .env 형식으로 출력 (SPEC 1단계)
if (require.main === module && process.argv[2] === 'keygen') {
  const { privateKey, publicKey } = generateKeypair();
  console.log(`BANK_PRIVATE_KEY=${privateKey}`);
  console.log(`BANK_PUBLIC_KEY=${publicKey}`);
  console.error(`# 공개키 지문: ${fingerprint(publicKey)}`);
}
