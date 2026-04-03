import { useEffect, useMemo, useState, type KeyboardEvent } from 'react';
import { useTranslation } from 'react-i18next';
import type { DiffFile, DiffLine } from '../../utils/diffParser';

interface DiffViewerProps {
  files: DiffFile[];
}

interface SplitDiffRow {
  leftType: DiffLine['type'] | null;
  leftText: string;
  leftLineNumber: number | null;
  rightType: DiffLine['type'] | null;
  rightText: string;
  rightLineNumber: number | null;
}

function lineClassName(type: DiffLine['type'] | null): string {
  switch (type) {
    case 'add':
      return 'bg-[color:var(--color-diff-add)] text-[color:var(--color-ink)]';
    case 'del':
      return 'bg-[color:var(--color-diff-del)] text-[color:var(--color-ink)]';
    case 'ctx':
      return 'text-[color:var(--color-ink)]';
    default:
      return 'bg-[rgba(20,20,19,0.03)] text-[color:var(--color-muted)]';
  }
}

function linePrefix(type: DiffLine['type'] | null): string {
  switch (type) {
    case 'add':
      return '+';
    case 'del':
      return '-';
    case 'ctx':
      return ' ';
    default:
      return ' ';
  }
}

function parseHunkHeader(header: string): { oldLine: number; newLine: number } {
  const match = /^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/.exec(header);
  if (!match) {
    return { oldLine: 1, newLine: 1 };
  }
  return {
    oldLine: Number.parseInt(match[1], 10),
    newLine: Number.parseInt(match[2], 10),
  };
}

function pairChangedLines(
  deletions: DiffLine[],
  additions: DiffLine[],
  state: { oldLine: number; newLine: number },
): SplitDiffRow[] {
  const rows: SplitDiffRow[] = [];
  const maxLength = Math.max(deletions.length, additions.length);

  for (let index = 0; index < maxLength; index += 1) {
    const left = deletions[index] ?? null;
    const right = additions[index] ?? null;
    rows.push({
      leftType: left?.type ?? null,
      leftText: left?.text ?? '',
      leftLineNumber: left ? state.oldLine++ : null,
      rightType: right?.type ?? null,
      rightText: right?.text ?? '',
      rightLineNumber: right ? state.newLine++ : null,
    });
  }

  return rows;
}

function buildSplitRows(lines: DiffLine[], header: string): SplitDiffRow[] {
  const state = parseHunkHeader(header);
  const rows: SplitDiffRow[] = [];

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.type === 'ctx') {
      rows.push({
        leftType: 'ctx',
        leftText: line.text,
        leftLineNumber: state.oldLine++,
        rightType: 'ctx',
        rightText: line.text,
        rightLineNumber: state.newLine++,
      });
      continue;
    }

    if (line.type === 'del') {
      const deletions: DiffLine[] = [];
      while (index < lines.length && lines[index]?.type === 'del') {
        deletions.push(lines[index]);
        index += 1;
      }
      const additions: DiffLine[] = [];
      while (index < lines.length && lines[index]?.type === 'add') {
        additions.push(lines[index]);
        index += 1;
      }
      rows.push(...pairChangedLines(deletions, additions, state));
      index -= 1;
      continue;
    }

    if (line.type === 'add') {
      const additions: DiffLine[] = [];
      while (index < lines.length && lines[index]?.type === 'add') {
        additions.push(lines[index]);
        index += 1;
      }
      rows.push(...pairChangedLines([], additions, state));
      index -= 1;
    }
  }

  return rows;
}

export default function DiffViewer({ files }: DiffViewerProps) {
  const { t } = useTranslation();
  const [selectedIndex, setSelectedIndex] = useState(0);
  const renderableFiles = useMemo(
    () => files.filter((file) => file.hunks.some((hunk) => hunk.lines.length > 0)),
    [files],
  );

  useEffect(() => {
    setSelectedIndex(0);
  }, [renderableFiles]);

  if (renderableFiles.length === 0) {
    return <p className="text-[color:var(--color-muted)] text-sm">{t('diffViewer.noFiles')}</p>;
  }

  const activeIndex = selectedIndex < renderableFiles.length ? selectedIndex : 0;
  const activeFile = renderableFiles[activeIndex];
  const activeHunks = activeFile.hunks.filter((hunk) => hunk.lines.length > 0);
  const activeTabId = `diff-file-tab-${activeIndex}`;
  const activePanelId = 'diff-file-panel';

  function focusTab(index: number) {
    const tab = document.querySelector<HTMLButtonElement>(`button[data-diff-tab-index="${index}"]`);
    tab?.focus();
  }

  function handleTabKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    if (renderableFiles.length < 2) {
      return;
    }

    let nextIndex: number | null = null;
    switch (event.key) {
      case 'ArrowDown':
      case 'ArrowRight':
        nextIndex = (index + 1) % renderableFiles.length;
        break;
      case 'ArrowUp':
      case 'ArrowLeft':
        nextIndex = (index - 1 + renderableFiles.length) % renderableFiles.length;
        break;
      case 'Home':
        nextIndex = 0;
        break;
      case 'End':
        nextIndex = renderableFiles.length - 1;
        break;
      default:
        return;
    }

    event.preventDefault();
    setSelectedIndex(nextIndex);
    focusTab(nextIndex);
  }

  return (
    <div className="flex overflow-hidden rounded-[16px] border border-[color:var(--color-ink)] bg-[color:var(--color-surface)]" style={{ maxHeight: 460 }}>
      <nav className="w-[200px] min-w-[200px] border-r-2 border-[color:var(--color-border-dark)] overflow-y-auto bg-[color:var(--color-surface)] p-3">
        <ul className="flex flex-col gap-2 text-xs font-mono" role="tablist" aria-label="Diff files" aria-orientation="vertical">
          {renderableFiles.map((file, idx) => (
            <li key={file.path}>
              <button
                type="button"
                onClick={() => setSelectedIndex(idx)}
                onKeyDown={(event) => handleTabKeyDown(event, idx)}
                id={`diff-file-tab-${idx}`}
                data-diff-tab-index={idx}
                role="tab"
                aria-selected={idx === activeIndex}
                aria-controls={activePanelId}
                tabIndex={idx === activeIndex ? 0 : -1}
                className={`w-full truncate rounded-full border px-3 py-2 text-left transition-all duration-200 ${
                  idx === activeIndex
                    ? 'border-[color:var(--color-border-dark)] bg-[color:var(--color-border-dark)] font-bold text-[color:var(--color-ink)] shadow-[inset_0_0_0_1px_var(--color-ink)]'
                    : 'border-transparent bg-[color:var(--color-bg-page)] text-[color:var(--color-muted)] hover:border-[color:var(--color-border-dark)] hover:text-[color:var(--color-ink)]'
                }`}
                title={file.path}
              >
                {file.path}
              </button>
            </li>
          ))}
        </ul>
      </nav>

      <div className="flex-1 overflow-auto bg-[color:var(--color-bg-page)]" id={activePanelId} role="tabpanel" aria-labelledby={activeTabId}>
        <div className="text-xs font-mono leading-5 min-w-[720px]">
          {activeHunks.map((hunk, hunkIdx) => {
            const rows = buildSplitRows(hunk.lines, hunk.header);
            return (
              <div key={`${activeFile.path}-hunk-${hunkIdx}`}>
                <div className="sticky top-0 z-10 grid grid-cols-[1fr_1fr] border-y border-[color:var(--color-ink)] bg-[#CBCADB] px-3 py-1.5 text-[color:var(--color-muted)] select-none">
                  <div>{t('diffViewer.old')}</div>
                  <div>{t('diffViewer.new')}</div>
                </div>
                <div className="sticky top-[29px] z-10 border-b border-[color:var(--color-border-dark)] bg-[color:var(--color-surface)] px-3 py-1 text-[color:var(--color-muted)] select-none">
                  {hunk.header}
                </div>
                <div className="grid grid-cols-[1fr_1fr]">
                  {rows.map((row, rowIdx) => (
                    <div key={`${activeFile.path}-h${hunkIdx}-r${rowIdx}`} className="contents">
                      <div className={`grid grid-cols-[3rem_1.5rem_minmax(0,1fr)] border-r border-b border-[color:var(--color-border)] px-2 ${lineClassName(row.leftType)}`}>
                        <span className="select-none opacity-60 text-right pr-2">{row.leftLineNumber ?? ''}</span>
                        <span className="select-none opacity-60">{linePrefix(row.leftType)}</span>
                        <span className="whitespace-pre-wrap break-all py-0.5">{row.leftText || ' '}</span>
                      </div>
                      <div className={`grid grid-cols-[3rem_1.5rem_minmax(0,1fr)] border-b border-[color:var(--color-border)] px-2 ${lineClassName(row.rightType)}`}>
                        <span className="select-none opacity-60 text-right pr-2">{row.rightLineNumber ?? ''}</span>
                        <span className="select-none opacity-60">{linePrefix(row.rightType)}</span>
                        <span className="whitespace-pre-wrap break-all py-0.5">{row.rightText || ' '}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
