import assert from "node:assert/strict";
import test from "node:test";

import {
  GUARD_MESSAGE_BASH_STRICT,
  GUARD_MESSAGE_BASH_WRITE,
  GUARD_MESSAGE_WRITE,
  buildDocsMatchers,
  decideShell,
  isReadonlyChain,
  scanPathArguments,
  touchesDocs,
} from "../lib/guard.js";

const DOCS = "/opt/wenshu/docs";
const DOCS_UNC = "\\\\wsl.localhost\\Debian\\opt\\wenshu\\docs";

test("路径识别：WSL 原生路径与 UNC 路径都能命中", () => {
  const markers = buildDocsMatchers([]);
  assert.equal(touchesDocs(`${DOCS}/a.md`, markers), true);
  assert.equal(touchesDocs(DOCS_UNC, markers), true);
  assert.equal(touchesDocs(DOCS_UNC.replace(/\\/g, "/"), markers), true);
  assert.equal(touchesDocs("/opt/wenshu/data/db.sqlite", markers), false);
  assert.equal(touchesDocs("D:/work/a.md", markers), false);
});

test("路径识别：extraDocsPaths 追加后可命中", () => {
  const markers = buildDocsMatchers(["C:\\docs\\wenshu"]);
  assert.equal(touchesDocs("c:/docs/wenshu/x.md", markers), true);
  assert.equal(touchesDocs("/opt/wenshu/docs/x.md", markers), true);
});

test("写工具：只认路径类参数，正文里出现文档库路径不误判", () => {
  const markers = buildDocsMatchers([]);
  assert.equal(scanPathArguments({ file_path: `${DOCS}/a.md` }, markers), `${DOCS}/a.md`);
  assert.equal(scanPathArguments({ path: DOCS_UNC }, markers), DOCS_UNC);
  assert.equal(
    scanPathArguments({ file_path: "D:/work/a.md", content: `见 ${DOCS}/b.md` }, markers),
    ""
  );
});

test("写工具：patch 类工具扫描补丁正文", () => {
  const markers = buildDocsMatchers([]);
  const args = { patch: `*** Begin Patch\n*** Update File: ${DOCS}/a.md\n*** End Patch` };
  assert.equal(scanPathArguments(args, markers, false), "");
  assert.notEqual(scanPathArguments(args, markers, true), "");
});

test("shell：文档库写入命令被拒", () => {
  const markers = buildDocsMatchers([]);
  for (const command of [
    `rm -rf ${DOCS}/old`,
    `echo hi >> ${DOCS}/a.md`,
    `sed -i 's/a/b/' ${DOCS}/a.md`,
    `cp /tmp/x.md ${DOCS}/x.md`,
    `git -C ${DOCS} commit -am wip`,
  ]) {
    assert.equal(decideShell(command, markers), GUARD_MESSAGE_BASH_WRITE, command);
  }
});

test("shell：只读查看放行（含 cd 前缀与管道组合）", () => {
  const markers = buildDocsMatchers([]);
  for (const command of [
    `cat ${DOCS}/a.md`,
    `grep -n refund ${DOCS}/a.md`,
    `cd ${DOCS} && ls`,
    `ls ${DOCS} | head -20`,
    `git -C ${DOCS} log --oneline`,
  ]) {
    assert.equal(decideShell(command, markers), "", command);
  }
});

test("shell：无害 fd 重定向不算写入（2>&1 / >/dev/null）", () => {
  const markers = buildDocsMatchers([]);
  assert.equal(decideShell(`ls ${DOCS} 2>&1 | head -5`, markers), "");
  assert.equal(decideShell(`grep -n refund ${DOCS}/a.md 2>/dev/null`, markers), "");
  assert.equal(decideShell(`cat ${DOCS}/a.md > /tmp/out.txt`, markers), GUARD_MESSAGE_BASH_WRITE);
});

test("shell：命中文档库但不是只读命令按严格档拒绝", () => {  const markers = buildDocsMatchers([]);
  assert.equal(decideShell(`vim ${DOCS}/a.md`, markers), GUARD_MESSAGE_BASH_STRICT);
  assert.equal(decideShell(`cd ${DOCS} && vim a.md`, markers), GUARD_MESSAGE_BASH_STRICT);
});

test("shell：不涉及文档库的命令一律放行", () => {
  const markers = buildDocsMatchers([]);
  assert.equal(decideShell("rm -rf D:/tmp/build", markers), "");
  assert.equal(decideShell("cat D:/work/a.md", markers), "");
});

test("只读链判定", () => {
  assert.equal(isReadonlyChain("ls | head"), true);
  assert.equal(isReadonlyChain("ls && rm x"), false);
  assert.equal(isReadonlyChain(""), false);
});

test("写工具拒绝文案指向 wenshu_* 工具", () => {
  assert.match(GUARD_MESSAGE_WRITE, /wenshu_create/);
  assert.match(GUARD_MESSAGE_WRITE, /wenshu_patch/);
});
