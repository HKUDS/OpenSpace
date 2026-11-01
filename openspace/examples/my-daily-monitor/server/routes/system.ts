/**
 * System monitoring API — job tracker, process watcher, server probes.
 */
import type { IncomingHttpHeaders } from 'node:http';
import { exec } from 'node:child_process';
import { promisify } from 'node:util';
import { statSync, readdirSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const execAsync = promisify(exec);
const EXEC_TIMEOUT = 5000;

export async function handleSystemRequest(
  query: Record<string, string>,
  _body: string,
  _headers: IncomingHttpHeaders,
): Promise<unknown> {
  const action = query.action || 'jobs';

  try {
    // ---- Watch specific processes by name pattern ----
    if (action === 'jobs') {
      const patterns = (query.patterns || '').split('||').filter(Boolean);
      if (patterns.length === 0) {
        // Auto-detect: Cursor/VSCode instances + common dev processes
        const autoPatterns = ['Cursor', 'Code', 'node', 'python', 'npm', 'tsx', 'vite', 'docker', 'ssh'];
        return await findProcesses(autoPatterns, true);
      }
      return await findProcesses(patterns, false);
    }

    // ---- Check if a specific PID is still running ----
    if (action === 'pid-check') {
      const pids = (query.pids || '').split(',').filter(Boolean);
      const results = [];
      for (const pid of pids) {
        try {
          process.kill(parseInt(pid, 10), 0); // signal 0 = check if alive
          results.push({ pid, alive: true });
        } catch {
          results.push({ pid, alive: false });
        }
      }
      return { results };
    }

    // ---- Tail a log file (last N lines) ----
    if (action === 'tail') {
      const file = query.file || '';
      const lines = parseInt(query.lines || '20', 10);
      if (!file) return { error: 'No file path', lines: [] };
      try {
        statSync(file);
        const cmd = process.platform === 'win32'
          ? `powershell -Command "Get-Content -Tail ${lines} '${file}'"`
          : `tail -n ${lines} "${file}"`;
        const { stdout } = await execAsync(cmd, { timeout: EXEC_TIMEOUT });
        return { lines: stdout.split('\n'), file };
      } catch (err: any) {
        return { error: err.message, lines: [] };
      }
    }

    // ---- Server health probes ----
    if (action === 'probe') {
      const urls = (query.urls || '').split(',').map(s => s.trim()).filter(Boolean);
      if (urls.length === 0) return { probes: [] };
      const probes = await Promise.allSettled(
        urls.map(async (url) => {
          const start = Date.now();
          try {
            const resp = await fetch(url, { signal: AbortSignal.timeout(5000), method: 'HEAD' });
            return { url, status: resp.status, ok: resp.ok, latencyMs: Date.now() - start };
          } catch (err: any) {
            return { url, status: 0, ok: false, latencyMs: Date.now() - start, error: err.message };
          }
        })
      );
      return { probes: probes.map(r => r.status === 'fulfilled' ? r.value : { url: '', ok: false }) };
    }

    // ---- Docker containers ----
    if (action === 'docker') {
      try {
        const { stdout } = await execAsync(
          'docker ps --format "{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|{{.RunningFor}}" 2>/dev/null',
          { timeout: EXEC_TIMEOUT }
        );
        const containers = stdout.trim().split('\n').filter(Boolean).map(line => {
          const [id, name, image, status, running] = line.split('|');
          return { id, name, image, status, running };
        });
        return { containers, available: true };
      } catch {
        return { containers: [], available: false };
      }
    }

    // ---- Trigger GitHub Actions dispatch ----
    if (action === 'dispatch') {
      const token = (_headers['x-github-token'] as string) || '';
      const repo = query.repo || '';
      const workflow = query.workflow || '';
      const ref = query.ref || 'main';
      if (!token || !repo || !workflow) return { error: 'Missing params' };
      try {
        const resp = await fetch(
          `https://api.github.com/repos/${repo}/actions/workflows/${workflow}/dispatches`,
          { method: 'POST', headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'User-Agent': 'MDM/1.0' }, body: JSON.stringify({ ref }) },
        );
        return { triggered: resp.ok, status: resp.status };
      } catch (err: any) {
        return { triggered: false, error: err.message };
      }
    }

    // ---- Cursor terminal sessions ----
    if (action === 'terminals') {
      const tailLines = parseInt(query.lines || '30', 10);
      return scanCursorTerminals(tailLines);
    }

    // ---- Execute Python code (agent tasks) ----
    if (action === 'exec') {
      return await executePython(_body);
    }

    return { error: `Unknown action: ${action}` };
  } catch (err: any) {
    return { error: err.message };
  }
}

// ---- Find processes matching patterns ----
async function findProcesses(patterns: string[], autoMode: boolean): Promise<unknown> {
  const isMac = process.platform === 'darwin';
  const isLinux = process.platform === 'linux';

  // Get all processes with detailed info
  const cmd = isMac || isLinux
    ? `ps axo pid,pcpu,pmem,etime,command`
    : `tasklist /V /FO CSV`;

  const { stdout } = await execAsync(cmd, { timeout: EXEC_TIMEOUT });
  const lines = stdout.trim().split('\n').slice(1); // skip header

  const jobs: any[] = [];
  const seenCommands = new Set<string>();

  for (const line of lines) {
    const parts = line.trim().split(/\s+/);
    if (parts.length < 5) continue;

    const pid = parts[0];
    const cpu = parseFloat(parts[1] || '0');
    const mem = parseFloat(parts[2] || '0');
    const elapsed = parts[3] || ''; // format: [[dd-]hh:]mm:ss
    const command = parts.slice(4).join(' ');

    // Check if command matches any pattern
    const matchedPattern = patterns.find(p => command.toLowerCase().includes(p.toLowerCase()));
    if (!matchedPattern) continue;

    // In auto mode, skip boring system processes
    if (autoMode) {
      if (command.includes('/System/') || command.includes('/usr/libexec/') || command.includes('WindowServer')) continue;
      if (cpu < 0.1 && mem < 0.1) continue;
    }

    // Deduplicate by short command name
    const shortCmd = command.slice(0, 80);
    if (seenCommands.has(shortCmd)) continue;
    seenCommands.add(shortCmd);

    // Parse elapsed time to human readable
    const duration = parseElapsed(elapsed);

    // Determine a friendly label
    const label = inferLabel(command, matchedPattern);

    jobs.push({
      pid,
      label,
      command: command.slice(0, 120),
      pattern: matchedPattern,
      cpu,
      mem,
      elapsed,
      duration,
      status: 'running',
    });
  }

  // Sort: highest CPU first
  jobs.sort((a, b) => b.cpu - a.cpu);

  return { jobs: jobs.slice(0, 30) };
}

function parseElapsed(elapsed: string): string {
  // Format from ps: [[dd-]hh:]mm:ss
  if (!elapsed) return '';
  const parts = elapsed.split(/[-:]/);
  if (parts.length === 2) return `${parts[0]}m ${parts[1]}s`;
  if (parts.length === 3) return `${parts[0]}h ${parts[1]}m`;
  if (parts.length === 4) return `${parts[0]}d ${parts[1]}h`;
  return elapsed;
}

// ---- Scan Cursor terminal sessions ----
function scanCursorTerminals(tailLines: number): unknown {
  const cursorProjectsDir = join(homedir(), '.cursor', 'projects');
  try {
    statSync(cursorProjectsDir);
  } catch {
    return { terminals: [], error: 'Cursor projects dir not found' };
  }

  // Find all workspace folders that have a terminals/ subdirectory
  const terminals: any[] = [];
  try {
    const workspaces = readdirSync(cursorProjectsDir, { withFileTypes: true })
      .filter(d => d.isDirectory());

    for (const ws of workspaces) {
      const termDir = join(cursorProjectsDir, ws.name, 'terminals');
      try {
        const files = readdirSync(termDir).filter(f => f.endsWith('.txt'));
        for (const file of files) {
          try {
            const content = readFileSync(join(termDir, file), 'utf-8');
            const parsed = parseTerminalFile(content, file, ws.name, tailLines);
            if (parsed) terminals.push(parsed);
          } catch { /* skip unreadable files */ }
        }
      } catch { /* no terminals dir — skip */ }
    }
  } catch { /* can't read projects dir */ }

  // Sort: active terminals first, then by terminal ID descending
  terminals.sort((a, b) => {
    if (a.isActive !== b.isActive) return a.isActive ? -1 : 1;
    return b.termId - a.termId;
  });

  return { terminals };
}

function parseTerminalFile(content: string, filename: string, workspace: string, tailLines: number): any | null {
  // Split frontmatter and body
  const fmMatch = content.match(/^---\n([\s\S]*?)\n---\n?([\s\S]*)$/);
  if (!fmMatch) return null;

  const meta = fmMatch[1];
  const body = fmMatch[2];

  // Parse YAML-like frontmatter (simple key: value)
  const get = (key: string): string => {
    // Handle multiline values (| prefix)
    const re = new RegExp(`^${key}:\\s*(.*)$`, 'm');
    const m = meta.match(re);
    return m ? m[1].trim() : '';
  };

  const pid = get('pid');
  const cwd = get('cwd');
  const activeCommand = get('active_command');
  const lastCommand = get('last_command');
  const lastExitCode = get('last_exit_code');

  // Skip terminals with no useful info
  if (pid === '-1' && !activeCommand && !body.trim()) return null;

  // Get tail of output
  const allLines = body.split('\n');
  const outputLines = allLines.slice(-tailLines).filter(l => l.trim() !== '');

  const termId = parseInt(filename.replace('.txt', ''), 10);
  const isActive = !!activeCommand || (pid !== '-1' && pid !== '');

  // Derive a friendly label
  let label = activeCommand || lastCommand || '';
  // Truncate very long commands
  if (label.length > 80) label = label.slice(0, 77) + '...';

  // Extract short cwd
  const shortCwd = cwd.split('/').slice(-2).join('/');

  return {
    termId,
    pid: pid === '-1' ? null : pid,
    cwd,
    shortCwd,
    activeCommand: activeCommand || null,
    lastCommand: lastCommand || null,
    lastExitCode: lastExitCode ? parseInt(lastExitCode, 10) : null,
    isActive,
    label,
    workspace,
    outputTail: outputLines.join('\n'),
    outputLineCount: allLines.length,
  };
}

function inferLabel(command: string, pattern: string): string {
  // Extract meaningful label from command
  if (command.includes('Cursor.app') || command.includes('Cursor Helper')) {
    const wsMatch = command.match(/--folder-uri=file:\/\/(.+?)(?:\s|$)/);
    return wsMatch ? `Cursor: ${wsMatch[1].split('/').pop()}` : 'Cursor';
  }
  if (command.includes('Code.app') || command.includes('Code Helper')) return 'VS Code';
  if (command.includes('vite')) return 'Vite Dev Server';
  if (command.includes('tsx watch')) return 'TSX Watch';
  if (command.includes('npm run')) {
    const m = command.match(/npm run (\S+)/);
    return m ? `npm run ${m[1]}` : 'npm script';
  }
  if (command.includes('python') || command.includes('python3')) {
    const m = command.match(/python3?\s+(\S+)/);
    return m ? `Python: ${m[1].split('/').pop()}` : 'Python';
  }
  if (command.includes('node ')) {
    const m = command.match(/node\s+(\S+)/);
    return m ? `Node: ${m[1].split('/').pop()}` : 'Node.js';
  }
  if (command.includes('docker')) return 'Docker';
  if (command.includes('ssh ')) {
    const m = command.match(/ssh\s+(\S+)/);
    return m ? `SSH: ${m[1]}` : 'SSH session';
  }
  // Fallback: first meaningful part of command
  const base = command.split('/').pop()?.split(' ')[0] || pattern;
  return base.slice(0, 30);
}

// ---- Execute Python code in a sandboxed process ----
async function executePython(code: string): Promise<unknown> {
  if (!code || !code.trim()) return { success: false, error: 'No code provided' };

  // Security: basic safety checks
  const dangerous = ['import subprocess', 'import shutil', '__import__', 'eval(', 'exec(', 'os.system', 'os.remove', 'rmtree'];
  for (const d of dangerous) {
    if (code.includes(d)) return { success: false, error: `Blocked: "${d}" is not allowed for safety` };
  }

  // Ensure output directory exists
  const outputDir = '/tmp/agent-output';
  try {
    await execAsync(`mkdir -p ${outputDir}`, { timeout: 2000 });
  } catch {}

  try {
    const { stdout, stderr } = await execAsync(
      `python3 -c ${JSON.stringify(code)}`,
      {
        timeout: 30000, // 30 second timeout
        env: { ...process.env, PYTHONPATH: '' },
        cwd: outputDir,
      }
    );
    return { success: true, stdout: stdout.slice(0, 5000), stderr: stderr.slice(0, 2000) };
  } catch (err: any) {
    return {
      success: false,
      error: err.message?.slice(0, 500),
      stderr: err.stderr?.slice(0, 2000),
      stdout: err.stdout?.slice(0, 2000),
    };
  }
}
