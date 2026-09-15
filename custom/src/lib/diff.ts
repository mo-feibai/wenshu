export type DiffOp = { type: 'eq' | 'del' | 'add'; text: string };
export type WordSeg = { text: string; changed: boolean };
export type DiffLine = { type: 'eq' | 'del' | 'add'; text: string; words?: WordSeg[] };
export type DiffHunk = {
  oldStart: number;
  oldCount: number;
  newStart: number;
  newCount: number;
  lines: DiffLine[];
};
export type DiffResult = { hunks: DiffHunk[]; added: number; removed: number };

const CONTEXT = 3;

function myers(a: string[], b: string[]): DiffOp[] {
  const n = a.length;
  const m = b.length;
  if (n === 0 && m === 0) return [];
  const max = n + m;
  const size = 2 * max + 1;
  const v = new Int32Array(size);
  const trace: Int32Array[] = [];
  let done = 0;
  outer: for (let d = 0; d <= max; d++) {
    trace.push(v.slice());
    for (let k = -d; k <= d; k += 2) {
      let x: number;
      if (k === -d || (k !== d && v[k - 1 + max] < v[k + 1 + max])) x = v[k + 1 + max];
      else x = v[k - 1 + max] + 1;
      let y = x - k;
      while (x < n && y < m && a[x] === b[y]) {
        x++;
        y++;
      }
      v[k + max] = x;
      if (x >= n && y >= m) {
        done = d;
        break outer;
      }
    }
  }
  const ops: DiffOp[] = [];
  let x = n;
  let y = m;
  for (let d = done; d > 0; d--) {
    const prev = trace[d];
    const k = x - y;
    let prevK: number;
    if (k === -d || (k !== d && prev[k - 1 + max] < prev[k + 1 + max])) prevK = k + 1;
    else prevK = k - 1;
    const prevX = prev[prevK + max];
    const prevY = prevX - prevK;
    while (x > prevX && y > prevY) {
      ops.push({ type: 'eq', text: a[x - 1] });
      x--;
      y--;
    }
    if (x > prevX) {
      ops.push({ type: 'del', text: a[x - 1] });
      x--;
    } else if (y > prevY) {
      ops.push({ type: 'add', text: b[y - 1] });
      y--;
    }
  }
  while (x > 0 && y > 0) {
    ops.push({ type: 'eq', text: a[--x] });
    y--;
  }
  while (x > 0) ops.push({ type: 'del', text: a[--x] });
  while (y > 0) ops.push({ type: 'add', text: b[--y] });
  ops.reverse();
  return ops;
}

function tokenize(text: string): string[] {
  return text.match(/[A-Za-z0-9_]+|\s+|[^A-Za-z0-9_\s]/gu) ?? [];
}

function wordSegments(ops: DiffOp[], side: 'del' | 'add'): WordSeg[] {
  const segs: WordSeg[] = [];
  for (const op of ops) {
    if (op.type !== 'eq' && op.type !== side) continue;
    const changed = op.type === side;
    const last = segs[segs.length - 1];
    if (last && last.changed === changed) last.text += op.text;
    else segs.push({ text: op.text, changed });
  }
  return segs;
}

export function buildDiff(oldText: string, newText: string): DiffResult {
  const a = oldText.replace(/\r\n?/g, '\n').split('\n');
  const b = newText.replace(/\r\n?/g, '\n').split('\n');
  const ops = myers(a, b);

  const added = ops.reduce((n, op) => n + (op.type === 'add' ? 1 : 0), 0);
  const removed = ops.reduce((n, op) => n + (op.type === 'del' ? 1 : 0), 0);
  if (added === 0 && removed === 0) return { hunks: [], added: 0, removed: 0 };

  const startsOld: number[] = [];
  const startsNew: number[] = [];
  let o = 1;
  let n = 1;
  for (const op of ops) {
    startsOld.push(o);
    startsNew.push(n);
    if (op.type !== 'add') o++;
    if (op.type !== 'del') n++;
  }

  const changeIdx: number[] = [];
  ops.forEach((op, i) => {
    if (op.type !== 'eq') changeIdx.push(i);
  });

  const groups: Array<[number, number]> = [];
  let s = changeIdx[0];
  let last = changeIdx[0];
  for (let i = 1; i < changeIdx.length; i++) {
    if (changeIdx[i] - last - 1 <= CONTEXT * 2) last = changeIdx[i];
    else {
      groups.push([s, last]);
      s = changeIdx[i];
      last = changeIdx[i];
    }
  }
  groups.push([s, last]);

  const hunks: DiffHunk[] = [];
  for (const [gi, gj] of groups) {
    const from = Math.max(0, gi - CONTEXT);
    const to = Math.min(ops.length, gj + CONTEXT + 1);
    const slice = ops.slice(from, to);
    const lines: DiffLine[] = [];
    let i = 0;
    while (i < slice.length) {
      if (slice[i].type === 'eq') {
        lines.push({ type: 'eq', text: slice[i].text });
        i++;
        continue;
      }
      const dels: string[] = [];
      const adds: string[] = [];
      while (i < slice.length && slice[i].type !== 'eq') {
        if (slice[i].type === 'del') dels.push(slice[i].text);
        else adds.push(slice[i].text);
        i++;
      }
      const pairs = Math.min(dels.length, adds.length);
      for (let j = 0; j < pairs; j++) {
        const tops = myers(tokenize(dels[j]), tokenize(adds[j]));
        lines.push({ type: 'del', text: dels[j], words: wordSegments(tops, 'del') });
        lines.push({ type: 'add', text: adds[j], words: wordSegments(tops, 'add') });
      }
      for (let j = pairs; j < dels.length; j++) lines.push({ type: 'del', text: dels[j] });
      for (let j = pairs; j < adds.length; j++) lines.push({ type: 'add', text: adds[j] });
    }
    hunks.push({
      oldStart: startsOld[from],
      oldCount: slice.reduce((c, op) => c + (op.type !== 'add' ? 1 : 0), 0),
      newStart: startsNew[from],
      newCount: slice.reduce((c, op) => c + (op.type !== 'del' ? 1 : 0), 0),
      lines,
    });
  }
  return { hunks, added, removed };
}
