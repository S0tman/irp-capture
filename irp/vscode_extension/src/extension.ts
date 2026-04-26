import * as vscode from 'vscode';
import { exec, spawn } from 'child_process';
import { promisify } from 'util';

const execAsync = promisify(exec);

// ── Helpers ───────────────────────────────────────────────────────────────────

function getConfig() {
  const cfg = vscode.workspace.getConfiguration('irp');
  return {
    executable: cfg.get<string>('executable') || 'irp',
    projectRoot: cfg.get<string>('projectRoot') ||
      vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || '',
  };
}

async function runIrp(args: string): Promise<{ stdout: string; stderr: string }> {
  const { executable, projectRoot } = getConfig();
  const cmd = `${executable} ${args}`;
  try {
    return await execAsync(cmd, { cwd: projectRoot || undefined });
  } catch (err: any) {
    return { stdout: err.stdout || '', stderr: err.stderr || String(err) };
  }
}

/** Pipe a JSON payload to `irp capture --stdin` to avoid shell escaping issues. */
async function runIrpCaptureStdin(payload: object): Promise<{ stdout: string; stderr: string }> {
  return new Promise((resolve) => {
    const { executable, projectRoot } = getConfig();
    const child = spawn(executable, ['capture', '--stdin'], {
      cwd: projectRoot || undefined,
      env: { ...process.env },
    });

    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d: Buffer) => { stdout += d.toString(); });
    child.stderr.on('data', (d: Buffer) => { stderr += d.toString(); });
    child.on('close', () => resolve({ stdout, stderr }));

    child.stdin.write(JSON.stringify(payload));
    child.stdin.end();
  });
}

function getOutputChannel(): vscode.OutputChannel {
  if (!outputChannel) {
    outputChannel = vscode.window.createOutputChannel('IRP');
  }
  return outputChannel;
}

// ── State ─────────────────────────────────────────────────────────────────────

let statusBarItem: vscode.StatusBarItem;
let outputChannel: vscode.OutputChannel;

// ── Status bar ────────────────────────────────────────────────────────────────

async function refreshStatusBar() {
  const { stdout } = await runIrp('why --json');
  try {
    const decisions = JSON.parse(stdout);
    const count = Array.isArray(decisions?.decisions)
      ? decisions.decisions.length
      : Array.isArray(decisions)
      ? decisions.length
      : null;
    statusBarItem.text = count !== null
      ? `⬡ IRP · ${count} decision${count === 1 ? '' : 's'}`
      : '⬡ IRP';
  } catch {
    statusBarItem.text = '⬡ IRP';
  }
  statusBarItem.show();
}

// ── Commands ──────────────────────────────────────────────────────────────────

async function captureDecision() {
  const what = await vscode.window.showInputBox({
    title: 'IRP: Capture Decision (1/2)',
    prompt: 'What was decided?',
    placeHolder: 'e.g. Use Postgres for the reporting service',
    ignoreFocusOut: true,
  });
  if (!what) return;

  const why = await vscode.window.showInputBox({
    title: 'IRP: Capture Decision (2/2)',
    prompt: 'Why? What was rejected or accepted?',
    placeHolder: 'e.g. Redis rejected — query patterns require joins',
    ignoreFocusOut: true,
  });
  if (!why) return;

  const { stdout, stderr } = await runIrpCaptureStdin({
    type: 'decision',
    what: `Decision: ${what}`,
    why,
    confidence: 'high',
    source: 'vscode',
    tags: [],
  });

  if (stderr && !stdout) {
    vscode.window.showErrorMessage(`IRP error: ${stderr}`);
    return;
  }

  // Extract ID from output for the notification
  const idMatch = stdout.match(/IRP-[\d-]+/);
  const id = idMatch ? idMatch[0] : 'captured';

  vscode.window.showInformationMessage(
    `⬡ IRP: Decision recorded — ${id}`,
    'Show Decisions'
  ).then(selection => {
    if (selection === 'Show Decisions') showRecentDecisions();
  });

  await refreshStatusBar();
}

async function showRecentDecisions() {
  const channel = getOutputChannel();
  channel.clear();
  channel.appendLine('IRP — Recent Decisions\n');

  const { stdout, stderr } = await runIrp('why');
  if (stderr && !stdout) {
    channel.appendLine(`Error: ${stderr}`);
  } else {
    channel.appendLine(stdout || 'No decisions captured yet.');
  }
  channel.show(true);
}

async function runDoctor() {
  const channel = getOutputChannel();
  channel.clear();

  const { stdout, stderr } = await runIrp('doctor');
  channel.appendLine(stdout || stderr || 'No output.');
  channel.show(true);
}

// ── Activation / deactivation ─────────────────────────────────────────────────

export function activate(context: vscode.ExtensionContext) {
  // Status bar
  statusBarItem = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Right,
    100
  );
  statusBarItem.command = 'irp.why';
  statusBarItem.tooltip = 'IRP: Click to show recent decisions';
  context.subscriptions.push(statusBarItem);
  refreshStatusBar();

  // Commands
  context.subscriptions.push(
    vscode.commands.registerCommand('irp.capture', captureDecision),
    vscode.commands.registerCommand('irp.why', showRecentDecisions),
    vscode.commands.registerCommand('irp.doctor', runDoctor),
  );

  // Refresh status bar on workspace folder change
  context.subscriptions.push(
    vscode.workspace.onDidChangeWorkspaceFolders(() => refreshStatusBar())
  );
}

export function deactivate() {
  outputChannel?.dispose();
}
