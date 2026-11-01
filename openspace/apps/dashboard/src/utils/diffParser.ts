export interface DiffLine {
  type: 'add' | 'del' | 'ctx';
  text: string;
}

export interface DiffHunk {
  header: string;
  lines: DiffLine[];
}

export interface DiffFile {
  path: string;
  hunks: DiffHunk[];
}

function hasRenderableHunks(file: DiffFile): boolean {
  return file.hunks.some((hunk) => hunk.lines.length > 0);
}

export function parseDiff(raw: string | null | undefined): DiffFile[] {
  if (!raw || typeof raw !== 'string' || raw.length === 0) {
    return [];
  }

  const files: DiffFile[] = [];
  let currentFile: DiffFile | null = null;
  let awaitingNewFilePath = false;

  const finalizeCurrentFile = () => {
    if (currentFile && hasRenderableHunks(currentFile)) {
      files.push(currentFile);
    }
    currentFile = null;
    awaitingNewFilePath = false;
  };

  for (const line of raw.split('\n')) {
    if (line.startsWith('--- a/')) {
      finalizeCurrentFile();
      currentFile = { path: line.slice(6), hunks: [] };
      continue;
    }

    if (line === '--- /dev/null') {
      finalizeCurrentFile();
      awaitingNewFilePath = true;
      continue;
    }

    if (line.startsWith('+++ b/')) {
      if (awaitingNewFilePath || !currentFile) {
        currentFile = { path: line.slice(6), hunks: [] };
      }
      awaitingNewFilePath = false;
      continue;
    }

    if (line === '+++ /dev/null') {
      awaitingNewFilePath = false;
      continue;
    }

    if (line.startsWith('@@') && currentFile) {
      currentFile.hunks.push({ header: line, lines: [] });
      continue;
    }

    if (!currentFile || currentFile.hunks.length === 0) {
      continue;
    }

    const hunk = currentFile.hunks[currentFile.hunks.length - 1];

    if (line.startsWith('+')) {
      hunk.lines.push({ type: 'add', text: line.slice(1) });
    } else if (line.startsWith('-')) {
      hunk.lines.push({ type: 'del', text: line.slice(1) });
    } else if (line.startsWith(' ')) {
      hunk.lines.push({ type: 'ctx', text: line.slice(1) });
    }
  }

  finalizeCurrentFile();
  return files;
}
