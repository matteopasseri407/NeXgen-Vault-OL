#!/usr/bin/env node
/**
 * Launch Playwright MCP without stealing the user's native file chooser.
 *
 * Upstream @playwright/mcp globally intercepts every `filechooser` event.
 * That blocks a human sharing the visible browser from using the native
 * chooser. This exact, fail-closed patch keeps human clicks native while
 * browser_file_upload targets the HTML input directly with setInputFiles().
 *
 * Playwright also changes Chromium's download behavior while attaching over
 * CDP. That setting is global for Chrome's default profile, so its temporary
 * artifact directory would steal a human download and make it disappear when
 * the MCP session exits. Shared Chrome keeps its native download behavior.
 *
 * Upstream's TabsContext.newTab() never calls Page.bringToFront(), unlike
 * selectTab(). On a real (non-headless) shared Chrome attached over CDP,
 * Chromium throttles rendering for a tab that isn't frontmost, so every
 * pointer-based action (click, hover, drag, drop — including file-drop
 * uploads) on a freshly created tab hangs until the action timeout. This
 * patch makes newTab() bring the tab to front too, matching selectTab().
 *
 * An MCP client disposal also closes the BrowserContext it received from
 * Chromium. That is correct for an MCP-owned browser, but fatal for the
 * user's persistent Chrome attached with --cdp-endpoint. In that case this
 * wrapper must detach only, leaving the visible browser and its context alive.
 */
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const VERSION = '0.0.78';
const MARKER = 'agent-human-file-chooser-patch-v1';
const DOWNLOAD_MARKER = 'agent-preserve-shared-downloads-patch-v1';
const NEW_TAB_FOCUS_MARKER = 'agent-focus-new-tab-patch-v1';
const CDP_DISPOSAL_MARKER = 'agent-preserve-shared-cdp-context-patch-v1';
const BACKUP_SUFFIX = `.${MARKER}.original`;

const fileChooserListener = `          eventsHelper.addEventListener(p, "filechooser", (chooser) => {
            this.setModalState({
              type: "fileChooser",
              description: "File chooser",
              fileChooser: chooser,
              clearedBy: { tool: uploadFile.schema.name, skill: "upload" }
            });
          }),`;

const upstreamUploadTool = `    uploadFile = defineTabTool({
      capability: "core",
      schema: {
        name: "browser_file_upload",
        title: "Upload files",
        description: "Upload one or multiple files",
        inputSchema: z3.object({
          paths: z3.array(z3.string()).optional().describe("The absolute paths to the files to upload. Can be single file or multiple files. If omitted, file chooser is cancelled.")
        }),
        type: "action"
      },
      handle: async (tab2, params2, response2) => {
        response2.setIncludeSnapshot();
        const modalState = tab2.modalStates().find((state) => state.type === "fileChooser");
        if (!modalState)
          throw new Error("No file chooser visible");
        if (params2.paths)
          await Promise.all(params2.paths.map((filePath) => response2.resolveClientFilename(filePath)));
        response2.addCode(\`await fileChooser.setFiles(\${JSON.stringify(params2.paths)})\`);
        tab2.clearModalState(modalState);
        await tab2.waitForCompletion(async () => {
          if (params2.paths)
            await modalState.fileChooser.setFiles(params2.paths);
        });
      },
      clearsModalState: "fileChooser"
    });`;

const directUploadTool = `    uploadFile = defineTabTool({
      capability: "core",
      schema: {
        name: "browser_file_upload",
        title: "Upload files",
        description: "Set files on a file input without opening the native file chooser",
        inputSchema: elementSchema.extend({
          paths: z3.array(z3.string()).min(1).describe("Absolute paths to the files to upload.")
        }),
        type: "action"
      },
      handle: async (tab2, params2, response2) => {
        response2.setIncludeSnapshot();
        const { locator: locator2, resolved } = await tab2.targetLocator(params2);
        await Promise.all(params2.paths.map((filePath) => response2.resolveClientFilename(filePath)));
        await tab2.waitForCompletion(async () => {
          await locator2.setInputFiles(params2.paths, tab2.actionTimeoutOptions);
        });
        response2.addCode(\`await page.\${resolved}.setInputFiles(\${JSON.stringify(params2.paths)});\`);
      }
    });`;

const upstreamDownloadBehavior = `        if (this._browser.options.name !== "clank" && this._options.acceptDownloads !== "internal-browser-default") {
          promises2.push(this._browser._session.send("Browser.setDownloadBehavior", {
            behavior: this._options.acceptDownloads === "accept" ? "allowAndName" : "deny",
            browserContextId: this._browserContextId,
            downloadPath: this._browser.options.downloadsPath,
            eventsEnabled: true
          }));
        }`;

const nativeDownloadBehavior = `        /* ${DOWNLOAD_MARKER}: a CDP-attached shared Chrome keeps its native Downloads directory. */`;

const upstreamNewTab = `      async newTab() {
        const browserContext = await this.ensureBrowserContext();
        const page = await browserContext.newPage();
        this._currentTab = this._tabs.find((t) => t.page === page);
        return this._currentTab;
      }`;

const focusedNewTab = `      async newTab() {
        const browserContext = await this.ensureBrowserContext();
        const page = await browserContext.newPage();
        await page.bringToFront(); // ${NEW_TAB_FOCUS_MARKER}: keep pointer actions unblocked on a shared Chrome.
        this._currentTab = this._tabs.find((t) => t.page === page);
        return this._currentTab;
      }`;

const cdpDisposalBackendAnchor = 'const browserContext = backend.browserContext;';
const cdpDisposalReset = '        sharedBrowserPromise = void 0;';
const cdpDisposalBrowserClose = `        await browserContext.browser()?.close().catch(() => {
        });`;
const guardedCdpDisposal = `${cdpDisposalReset}
        if (config.browser.cdpEndpoint) {
          /* ${CDP_DISPOSAL_MARKER}: an attached personal Chrome owns this context. */
          return;
        }
        await browserContext.close().catch(() => {
        });
        await browserContext.browser()?.close().catch(() => {
        });`;

function withNodeOnPath() {
  const env = { ...process.env };
  const key = Object.keys(env).find((name) => name.toLowerCase() === 'path') || 'PATH';
  const nodeDir = path.dirname(process.execPath);
  const entries = [nodeDir, ...(env[key] || '').split(path.delimiter).filter(Boolean)];
  const seen = new Set();
  const deduped = entries.filter((entry) => {
    const normalized = entry.trim().replace(/[\\/]+$/, '').toLowerCase();
    if (!normalized || seen.has(normalized))
      return false;
    seen.add(normalized);
    return true;
  });
  let candidate = deduped.join(path.delimiter);
  if (process.platform === 'win32' && candidate.length > 8191) {
    const systemRoot = env.SystemRoot || env.SYSTEMROOT || 'C:\\Windows';
    candidate = [
      nodeDir,
      path.join(systemRoot, 'System32'),
      systemRoot,
      path.join(systemRoot, 'System32', 'WindowsPowerShell', 'v1.0'),
      path.join(systemRoot, 'System32', 'OpenSSH'),
    ].join(path.delimiter);
  }
  env[key] = candidate;
  return env;
}

function npmCliPath() {
  const candidates = [
    process.env.npm_execpath,
    path.join(path.dirname(process.execPath), 'node_modules', 'npm', 'bin', 'npm-cli.js'),
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (fs.existsSync(candidate))
      return candidate;
  }
  return null;
}

function runNpm(args) {
  const options = { encoding: 'utf8', env: withNodeOnPath() };
  if (process.platform === 'win32') {
    const npmCli = npmCliPath();
    if (!npmCli)
      return { status: null, error: new Error('npm-cli.js was not found beside node.exe') };
    return spawnSync(process.execPath, [npmCli, ...args], options);
  }
  return spawnSync('npm', args, options);
}

function failure(result) {
  return result.stderr?.trim()
    || result.stdout?.trim()
    || result.error?.message
    || `exit status ${result.status}`;
}

function npmCache() {
  const result = runNpm(['config', 'get', 'cache']);
  if (result.status !== 0)
    throw new Error(`Unable to read the npm cache: ${failure(result)}`);
  return result.stdout?.trim() || path.join(os.homedir(), '.npm');
}

function ensurePackageIsCached() {
  const result = runNpm([
    'exec', '--yes', `--package=@playwright/mcp@${VERSION}`, '--',
    'playwright-mcp', '--version',
  ]);
  if (result.status !== 0)
    throw new Error(`Unable to prepare @playwright/mcp@${VERSION}: ${failure(result)}`);
}

function cachedBundles() {
  const root = path.join(npmCache(), '_npx');
  if (!fs.existsSync(root))
    return [];
  return fs.readdirSync(root, { withFileTypes: true }).flatMap((entry) => {
    if (!entry.isDirectory())
      return [];
    const packageJson = path.join(root, entry.name, 'node_modules', '@playwright', 'mcp', 'package.json');
    if (!fs.existsSync(packageJson))
      return [];
    try {
      if (JSON.parse(fs.readFileSync(packageJson, 'utf8')).version !== VERSION)
        return [];
    } catch {
      return [];
    }
    const bundle = path.join(root, entry.name, 'node_modules', 'playwright-core', 'lib', 'coreBundle.js');
    const cli = path.join(root, entry.name, 'node_modules', '@playwright', 'mcp', 'cli.js');
    return fs.existsSync(bundle) && fs.existsSync(cli) ? [{ bundle, cli }] : [];
  });
}

function writeAtomically(file, content) {
  const stat = fs.statSync(file);
  const temporary = `${file}.${process.pid}.${Date.now()}.tmp`;
  fs.writeFileSync(temporary, content, { encoding: 'utf8', mode: stat.mode });
  fs.renameSync(temporary, file);
}

function occurrences(source, needle) {
  return source.split(needle).length - 1;
}

function patchCdpDisposal(source, bundle) {
  const hasCdpDisposalPatch = source.includes(CDP_DISPOSAL_MARKER);
  if (hasCdpDisposalPatch) {
    const markerAt = source.indexOf(CDP_DISPOSAL_MARKER);
    const guard = source.slice(Math.max(0, markerAt - 160), markerAt + 220);
    if (!guard.includes('config.browser.cdpEndpoint') || !guard.includes('return;'))
      throw new Error(`Invalid existing shared-CDP disposal patch at ${bundle}.`);
    return source;
  }

  const backendAt = source.indexOf(cdpDisposalBackendAnchor);
  if (backendAt < 0 || source.indexOf(cdpDisposalBackendAnchor, backendAt + 1) >= 0)
    throw new Error(`Unsupported Playwright disposal bundle at ${bundle}. Refusing an unsafe partial patch.`);
  const resetAt = source.indexOf(cdpDisposalReset, backendAt);
  const browserCloseAt = source.indexOf(cdpDisposalBrowserClose, resetAt);
  if (resetAt < 0 || browserCloseAt < resetAt)
    throw new Error(`Unsupported Playwright CDP disposal block at ${bundle}. Refusing an unsafe partial patch.`);

  const upstreamDisposal = source.slice(resetAt, browserCloseAt + cdpDisposalBrowserClose.length);
  if (occurrences(upstreamDisposal, 'await browserContext.close().catch(() => {') !== 1
      || occurrences(upstreamDisposal, 'await browserContext.browser()?.close().catch(() => {') !== 1)
    throw new Error(`Unexpected Playwright CDP disposal block at ${bundle}. Refusing an unsafe partial patch.`);

  return source.slice(0, resetAt) + guardedCdpDisposal
    + source.slice(browserCloseAt + cdpDisposalBrowserClose.length);
}

function patchedSource(source, bundle) {
  const hasFileChooserPatch = source.includes(MARKER);
  if (hasFileChooserPatch) {
    if (source.includes(fileChooserListener) || source.includes(upstreamUploadTool) || !source.includes(directUploadTool))
      throw new Error(`Invalid existing human-safe patch at ${bundle}.`);
  } else if (occurrences(source, fileChooserListener) !== 1 || occurrences(source, upstreamUploadTool) !== 1) {
    throw new Error(`Unsupported Playwright file-chooser bundle at ${bundle}. Refusing an unsafe partial patch.`);
  }

  const fileChooserPatched = hasFileChooserPatch ? source : source
    .replace(fileChooserListener, `          /* ${MARKER}: native chooser remains available to the human. */`)
    .replace(upstreamUploadTool, directUploadTool);

  const hasDownloadPatch = fileChooserPatched.includes(DOWNLOAD_MARKER);
  if (hasDownloadPatch) {
    if (fileChooserPatched.includes(upstreamDownloadBehavior))
      throw new Error(`Invalid existing shared-download patch at ${bundle}.`);
  } else if (occurrences(fileChooserPatched, upstreamDownloadBehavior) !== 1) {
    throw new Error(`Unsupported Playwright download bundle at ${bundle}. Refusing an unsafe partial patch.`);
  }

  const downloadPatched = hasDownloadPatch ? fileChooserPatched : fileChooserPatched
    .replace(upstreamDownloadBehavior, nativeDownloadBehavior);

  const cdpDisposalPatched = patchCdpDisposal(downloadPatched, bundle);

  const hasNewTabFocusPatch = cdpDisposalPatched.includes(NEW_TAB_FOCUS_MARKER);
  if (hasNewTabFocusPatch) {
    if (cdpDisposalPatched.includes(upstreamNewTab))
      throw new Error(`Invalid existing new-tab-focus patch at ${bundle}.`);
  } else if (occurrences(cdpDisposalPatched, upstreamNewTab) !== 1) {
    throw new Error(`Unsupported Playwright tabs bundle at ${bundle}. Refusing an unsafe partial patch.`);
  }

  const patched = hasNewTabFocusPatch ? cdpDisposalPatched : cdpDisposalPatched
    .replace(upstreamNewTab, focusedNewTab);
  if (!patched.includes(MARKER) || !patched.includes(directUploadTool)
      || !patched.includes(DOWNLOAD_MARKER)
      || !patched.includes(NEW_TAB_FOCUS_MARKER) || !patched.includes(focusedNewTab)
      || !patched.includes(CDP_DISPOSAL_MARKER) || !patched.includes('if (config.browser.cdpEndpoint)')
      || patched.includes(fileChooserListener) || patched.includes(upstreamUploadTool)
      || patched.includes(upstreamDownloadBehavior) || patched.includes(upstreamNewTab))
    throw new Error(`Human-safe patch validation failed in memory for ${bundle}.`);
  return patched;
}

function patchBundle(bundle) {
  const source = fs.readFileSync(bundle, 'utf8');
  const patched = patchedSource(source, bundle);
  if (patched === source)
    return false;
  const backup = `${bundle}${BACKUP_SUFFIX}`;
  if (!fs.existsSync(backup)) {
    try {
      fs.copyFileSync(bundle, backup, fs.constants.COPYFILE_EXCL);
    } catch (error) {
      if (error.code !== 'EEXIST')
        throw error;
    }
  }
  writeAtomically(bundle, patched);
  return true;
}

function restoreBundles(bundles) {
  let restored = 0;
  for (const { bundle } of bundles) {
    const backup = `${bundle}${BACKUP_SUFFIX}`;
    if (fs.existsSync(backup)) {
      writeAtomically(bundle, fs.readFileSync(backup, 'utf8'));
      restored++;
    }
  }
  console.log(`Restored ${restored} Playwright bundle${restored === 1 ? '' : 's'}.`);
}

function main() {
  if (process.argv[2] === '--self-test') {
    npmCache();
    let bundles = cachedBundles();
    if (!bundles.length) {
      ensurePackageIsCached();
      bundles = cachedBundles();
    }
    if (!bundles.length)
      throw new Error(`No cached @playwright/mcp@${VERSION} bundle is available for validation.`);
    for (const { bundle } of bundles)
      patchedSource(fs.readFileSync(bundle, 'utf8'), bundle);
    return;
  }
  let bundles = cachedBundles();
  if (!bundles.length) {
    ensurePackageIsCached();
    bundles = cachedBundles();
  }
  if (!bundles.length)
    throw new Error(`@playwright/mcp@${VERSION} was prepared but no usable npm cache bundle was found.`);
  if (process.argv[2] === '--restore') {
    restoreBundles(bundles);
    return;
  }
  const patched = bundles.filter(({ bundle }) => patchBundle(bundle));
  const launch = bundles.find(({ bundle }) => fs.readFileSync(bundle, 'utf8').includes(MARKER));
  if (!launch)
    throw new Error('No patched Playwright MCP bundle is available.');
  if (patched.length)
    console.error(`playwright-human-safe: patched ${patched.length} cached @playwright/mcp bundle(s).`);
  const child = spawn(process.execPath, [launch.cli, ...process.argv.slice(2)], {
    stdio: 'inherit',
    env: withNodeOnPath(),
  });
  for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP'])
    process.on(signal, () => child.kill(signal));
  child.on('error', (error) => {
    console.error(`Unable to launch Playwright MCP: ${error.message}`);
    process.exitCode = 1;
  });
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
  console.error(`playwright-human-safe: ${error.message}`);
  process.exitCode = 1;
}
