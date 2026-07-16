#!/usr/bin/env node
/**
 * Launch a pinned mcp-remote bridge without writing bearer tokens to config.
 *
 * Antigravity on Windows can mangle spaces inside stdio arguments. The
 * generated config therefore passes only a URL and the NAME of the bearer
 * environment variable. This wrapper derives an environment-only header and
 * gives mcp-remote a no-space placeholder argument that it expands itself.
 */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const EXACT_PACKAGE = /^mcp-remote@\d+\.\d+\.\d+$/;
const HEADER_ENV = 'NEXGEN_MCP_AUTH_HEADER';

function npmCliPath() {
  const candidates = [
    process.env.npm_execpath,
    path.join(path.dirname(process.execPath), 'node_modules', 'npm', 'bin', 'npm-cli.js'),
  ].filter(Boolean);
  return candidates.find((candidate) => fs.existsSync(candidate)) || null;
}

function withNodeOnPath(extra = {}) {
  const env = { ...process.env, ...extra };
  const key = Object.keys(env).find((name) => name.toLowerCase() === 'path') || 'PATH';
  const nodeDir = path.dirname(process.execPath);
  const entries = [nodeDir, ...(env[key] || '').split(path.delimiter).filter(Boolean)];
  const seen = new Set();
  env[key] = entries.filter((entry) => {
    const normalized = entry.trim().replace(/[\\/]+$/, '').toLowerCase();
    if (!normalized || seen.has(normalized))
      return false;
    seen.add(normalized);
    return true;
  }).join(path.delimiter);
  return env;
}

function fail(message) {
  console.error(`mcp-http-bridge: ${message}`);
  process.exitCode = 1;
}

function main() {
  if (process.argv[2] === '--self-test') {
    const packagePin = process.argv[3];
    if (!EXACT_PACKAGE.test(packagePin || ''))
      throw new Error('self-test requires an exact mcp-remote package pin');
    if (process.platform === 'win32' && !npmCliPath())
      throw new Error('npm-cli.js was not found beside node.exe');
    return;
  }

  const [, , url, tokenEnvName, packagePin] = process.argv;
  if (!/^https?:\/\//.test(url || ''))
    throw new Error('a valid HTTP(S) MCP URL is required');
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(tokenEnvName || ''))
    throw new Error('a valid bearer environment-variable name is required');
  if (!EXACT_PACKAGE.test(packagePin || ''))
    throw new Error('mcp-remote must be pinned to an exact version');
  const token = process.env[tokenEnvName];
  if (!token)
    throw new Error(`required environment variable ${tokenEnvName} is missing`);

  const env = withNodeOnPath({ [HEADER_ENV]: `Bearer ${token}` });
  const args = [
    'exec', '--yes', `--package=${packagePin}`, '--', 'mcp-remote', url,
    '--header', `Authorization:\${${HEADER_ENV}}`,
  ];
  const npmCli = npmCliPath();
  const command = npmCli ? process.execPath : 'npm';
  const commandArgs = npmCli ? [npmCli, ...args] : args;
  const child = spawn(command, commandArgs, { stdio: 'inherit', env });

  for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP'])
    process.on(signal, () => child.kill(signal));
  child.on('error', (error) => fail(`unable to launch ${packagePin}: ${error.message}`));
  child.on('exit', (code, signal) => {
    if (signal)
      process.kill(process.pid, signal);
    else
      process.exitCode = code ?? 1;
  });
}

try {
  main();
} catch (error) {
  fail(error.message);
}
