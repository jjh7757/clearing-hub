// config.js — 설정은 환경 변수로 받는다. 같은 폴더의 .env가 있으면 먼저 읽는다 (Node 20.12+ process.loadEnvFile).
// 개인키(BANK_PRIVATE_KEY)와 api_key(BANK_API_KEY)는 .env(=gitignore)나 서버 환경 변수에만 둔다 (SPEC 1단계, 3.5).
'use strict';

const fs = require('node:fs');
const path = require('node:path');

const ENV_FILE = path.join(__dirname, '..', '.env');

if (fs.existsSync(ENV_FILE)) process.loadEnvFile(ENV_FILE); // 이미 있는 환경 변수가 먼저다

function need(name) {
  const value = process.env[name];
  if (!value) {
    console.error(`환경 변수 ${name}이(가) 없습니다. README의 순서대로 .env를 채우세요.`);
    process.exit(1);
  }
  return value;
}

/** .env의 KEY=VALUE를 넣거나 바꾼다 (등록 응답의 bank_code·api_key 저장용). */
function upsertEnv(values) {
  let lines = fs.existsSync(ENV_FILE) ? fs.readFileSync(ENV_FILE, 'utf8').split(/\r?\n/) : [];
  for (const [key, value] of Object.entries(values)) {
    const i = lines.findIndex((l) => l.startsWith(`${key}=`));
    if (i >= 0) lines[i] = `${key}=${value}`;
    else lines.push(`${key}=${value}`);
    process.env[key] = value;
  }
  lines = lines.filter((l, i) => l !== '' || i < lines.length - 1);
  fs.writeFileSync(ENV_FILE, `${lines.join('\n')}\n`, { mode: 0o600 });
}

module.exports = { need, upsertEnv, ENV_FILE };
