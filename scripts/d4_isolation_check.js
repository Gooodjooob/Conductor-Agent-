'use strict';

const fs = require('node:fs/promises');
const {
  D4_GATEWAY_AGENT_ID,
  d4IsolationRequestError,
  effectiveD4IsolationPolicy,
} = require('../../../sales-dev/scripts/dispatch/backends/qclaw-gateway');

async function check(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) {
    return { safe: false, reason: 'request_invalid' };
  }
  const request = { agentId: D4_GATEWAY_AGENT_ID, permissions: input.permissions };
  const requestError = d4IsolationRequestError(request);
  if (requestError) {
    return { safe: false, reason: requestError.error.code };
  }
  const writeDir = input.permissions.writeScope[0];
  return effectiveD4IsolationPolicy(
    input.configResult, D4_GATEWAY_AGENT_ID, writeDir, fs.realpath,
  );
}

async function main() {
  let raw = '';
  for await (const chunk of process.stdin) raw += chunk;
  const result = await check(JSON.parse(raw));
  process.stdout.write(JSON.stringify({ safe: result.safe === true, reason: result.reason }) + '\n');
  if (result.safe !== true) process.exitCode = 1;
}

if (require.main === module) {
  main().catch(() => {
    process.stdout.write('{"safe":false,"reason":"checker_error"}\n');
    process.exitCode = 2;
  });
}

module.exports = { check };
