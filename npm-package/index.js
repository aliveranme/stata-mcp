#!/usr/bin/env node
'use strict';

// Stata MCP Server — NPM 薄启动器
// MCP 走 stdio：本进程的 stdio 直接桥接给内嵌的 Python 服务器（server.py）。
// 服务器依赖真实 Stata 的 pystata（从 STATA_HOME/utilities 加载），以及 fastmcp。
// 优先用 `uv run`（自动装 fastmcp）；无 uv 时退回系统 python（需已装 fastmcp）。

const { spawn, spawnSync } = require('node:child_process');
const path = require('node:path');

const serverPy = path.join(__dirname, 'python', 'server.py');

function findCommand(candidates) {
  for (const c of candidates) {
    const r = spawnSync(process.platform === 'win32' ? 'where' : 'which', [c], {
      stdio: 'ignore',
    });
    if (r.status === 0) return c;
  }
  return null;
}

let command;
let args;
const uv = findCommand(['uv']);
const python = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');

// 探测 Python 主次版本，供直接使用解释器的路径做前置检查。
function pythonMajorMinor(py) {
  const r = spawnSync(py, ['-c', 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")'], {
    encoding: 'utf8',
  });
  return r.status === 0 && r.stdout ? r.stdout.trim() : null;
}

if (python !== (process.platform === 'win32' ? 'python' : 'python3') || !uv) {
  // 显式指定了 PYTHON（或环境无 uv）：直接用该 python（需已装 fastmcp）。
  // macOS 出厂 /usr/bin/python3=3.9 不满足 server.py 的 PEP 604 语法与 fastmcp 的
  // Requires-Python>=3.10，先探测并给出可操作提示，而非裸 traceback。
  const ver = pythonMajorMinor(python);
  if (ver) {
    const [ma, mi] = ver.split('.').map(Number);
    if (ma < 3 || (ma === 3 && mi < 10)) {
      process.stderr.write(`[stata-mcp-server] 当前 python 是 ${ver}，需要 Python 3.10+。\n`);
      process.stderr.write('  建议：安装 uv（https://docs.astral.sh/uv/）后重试，或装 Python 3.10+ 并设置 PYTHON 指向它。\n');
    }
  }
  command = python;
  args = [serverPy];
} else if (uv) {
  // uv run 自动装 fastmcp；--python '>=3.10' 让 uv 挑选/下载满足要求的解释器，
  // 而不是拿 PATH 上的 python3（macOS 出厂 3.9 装不上 fastmcp 的 Requires-Python）。
  command = uv;
  args = ['run', '--no-project', '--with', 'fastmcp>=3.2.0', '--python', '>=3.10', serverPy];
} else {
  command = python;
  args = [serverPy];
}

const child = spawn(command, args, { stdio: 'inherit', env: process.env });

child.on('error', (err) => {
  process.stderr.write(`[stata-mcp-server] 启动失败: ${err.message}\n`);
  process.stderr.write('需要：Python 3.10+ 与真实 Stata（设 STATA_HOME 指向含 utilities/pystata 的目录）。\n');
  process.exit(1);
});

child.on('exit', (code, signal) => {
  process.exit(code ?? 1);
});

['SIGINT', 'SIGTERM'].forEach((sig) => process.on(sig, () => child.kill(sig)));
