'use strict';

const $ = (id) => document.getElementById(id);
let state = null;
let currentJob = null;
let resumeId = null;
let summarySeedId = null;
let cursor = 0;
let treePath = '';
let deliveryPath = null;
let pollBusy = false;
let pollPromise = null;
let currentTaskId = null;
let replaying = false;
let viewEpoch = 0;
let errorShown = false;
let toastTimer;
let pathTarget = null;
let historySignature = '';
let windowSettings = {};
let stateSyncBusy = false;
let submitting = false;
let exportingDelivery = false;
let workspaceBackend = 'local';
let sshWorkspace = {workspace: '', delivery_path: '', target_id: '', host: ''};
let remoteEditing = null;
let treeEpoch = 0;
let remoteExecutionState = null;
let remoteInstallBusy = false;
let remoteInstallError = '';
let remoteBrowserPurpose = 'workspace';
function isRemoteWorkspace() { return workspaceBackend === 'ssh'; }
const windowFields = ['workspace', 'model', 'base_url', 'delivery_path', 'developer_role', 'api_key_file',
  'max_output_tokens', 'model_context_window', 'model_auto_compact_token_limit',
  'max_parallel_runs', 'task_timeout_seconds', 'stream_idle_timeout_seconds', 'max_auto_continues',
  'max_context_images'];
try { windowSettings = JSON.parse(sessionStorage.getItem('codex.windowSettings') || '{}'); } catch {}

function saveWindowSettings(values) {
  for (const name of windowFields) {
    if (isRemoteWorkspace() && ['workspace', 'delivery_path'].includes(name)) continue;
    if (values[name] !== undefined) windowSettings[name] = values[name];
  }
  sessionStorage.setItem('codex.windowSettings', JSON.stringify(windowSettings));
}

function toast(message, error = false) {
  clearTimeout(toastTimer);
  $('toast-message').textContent = message;
  $('toast-copy').textContent = '复制';
  $('toast').className = 'toast' + (error ? ' error' : '');
  $('toast').hidden = false;
  if ($('toast').showPopover && !$('toast').matches(':popover-open')) $('toast').showPopover();
  if (!error) toastTimer = setTimeout(hideToast, 5500);
}

function hideToast() {
  clearTimeout(toastTimer);
  if ($('toast').hidePopover && $('toast').matches(':popover-open')) $('toast').hidePopover();
  $('toast').hidden = true;
}

function openCopyDialog(text, title = '完整信息', hint = '') {
  $('copy-title').textContent = title;
  $('copy-content').value = String(text);
  $('copy-hint').textContent = hint;
  if (!$('copy-dialog').open) $('copy-dialog').showModal();
  $('copy-content').focus();
  $('copy-content').select();
}

async function copyText(text, title = '完整信息') {
  const value = String(text);
  if (!value) return false;
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {}
  const field = document.createElement('textarea');
  field.value = value;
  field.readOnly = true;
  field.style.cssText = 'position:fixed;left:0;top:0;opacity:0;width:1px;height:1px';
  const active = document.activeElement;
  const parent = active?.closest('dialog[open], [popover]') || document.body;
  parent.append(field);
  let copied = false;
  try {
    field.focus();
    field.select();
    copied = document.execCommand('copy');
  } catch {}
  finally { field.remove(); active?.focus(); }
  if (!copied) openCopyDialog(value, title, '浏览器未允许自动复制，已选中全文，请按 ⌘C / Ctrl+C。');
  return copied;
}

function setRemoteStatus(text) {
  const field = $('remote-status');
  if (field.value !== text) field.value = text;
}

function remoteDiagnosticText() {
  const connection = remoteInfo.connection || {};
  return [
    `本机工作台：${window.location.origin}`,
    `SSH 配置：${connection.ssh_config || $('remote-config').value}`,
    `主机别名：${connection.host || $('remote-host').value}`,
    '连接方式：SSH 标准输入输出；无需远端网页端口',
    `服务器项目：${sshWorkspace.workspace || $('remote-workspace-path').value}`,
    `服务器 delivery：${sshWorkspace.delivery_path || $('remote-delivery-path').value}`,
    `状态：${connection.connected ? '已连接' : connection.connecting ? '连接中' : '未连接'}`,
    '', '完整状态 / 报错：', $('remote-status').value,
  ].join('\n');
}

function pathInfo(relative, directory) {
  const root = state.settings.workspace.replace(/[\\/]+$/, '');
  const separator = root.includes('\\') ? '\\' : '/';
  const child = relative.replace(/[\\/]/g, separator);
  const absolute = root + (child ? separator + child : '');
  return {absolute, relative: relative || '.', name: absolute.split(/[\\/]/).pop(), directory};
}

function closePathMenu() {
  const menu = $('path-menu');
  if (menu.matches(':popover-open')) menu.hidePopover();
}

function showPathMenu(event, info) {
  if (!info?.absolute) return;
  event.preventDefault();
  closePathMenu();
  pathTarget = info;
  const menu = $('path-menu');
  $('path-menu-name').textContent = info.name;
  menu.showPopover();
  const bounds = event.currentTarget.getBoundingClientRect();
  const x = event.clientX || bounds.left;
  const y = event.clientY || bounds.bottom;
  menu.style.left = Math.max(8, Math.min(x, window.innerWidth - menu.offsetWidth - 8)) + 'px';
  menu.style.top = Math.max(8, Math.min(y, window.innerHeight - menu.offsetHeight - 8)) + 'px';
  $('path-view').focus();
}

function showPathDetails() {
  closePathMenu();
  if (!pathTarget) return;
  $('path-kind').textContent = (pathTarget.directory ? '目录：' : '文件：') + pathTarget.name;
  $('path-absolute').value = pathTarget.absolute;
  $('path-relative').value = pathTarget.relative;
  if (!$('path-dialog').open) $('path-dialog').showModal();
}

async function copyPath(value, relative = false) {
  closePathMenu();
  try {
    await navigator.clipboard.writeText(value);
    toast('已复制：' + value);
  } catch {
    showPathDetails();
    const input = $(relative ? 'path-relative' : 'path-absolute');
    input.focus();
    input.select();
    toast('浏览器不允许自动复制，已选中路径，请按 ⌘C / Ctrl+C。', true);
  }
}

function setupPathMenu() {
  $('path-view').addEventListener('click', showPathDetails);
  for (const [id, field] of [['path-copy-absolute', 'absolute'], ['path-copy-relative', 'relative'], ['path-copy-name', 'name']]) {
    $(id).addEventListener('click', () => { if (pathTarget) copyPath(pathTarget[field], field === 'relative'); });
  }
  $('path-detail-copy-absolute').addEventListener('click', () => copyPath($('path-absolute').value));
  $('path-detail-copy-relative').addEventListener('click', () => copyPath($('path-relative').value, true));
  for (const id of ['workspace-path', 'workspace-name', 'tree-path']) {
    $(id).addEventListener('contextmenu', (event) => {
      if (state.settings.workspace) showPathMenu(event, pathInfo(id === 'tree-path' ? treePath : '', true));
    });
  }
  $('file-name').addEventListener('contextmenu', (event) => {
    if ($('file-name').dataset.path) showPathMenu(event, pathInfo($('file-name').dataset.path, false));
  });
  document.addEventListener('pointerdown', (event) => {
    if (!$('path-menu').contains(event.target)) closePathMenu();
  });
  window.addEventListener('resize', closePathMenu);
  document.addEventListener('scroll', closePathMenu, true);
  document.addEventListener('keydown', (event) => {
    const menu = $('path-menu');
    if (!menu.matches(':popover-open')) return;
    if (event.key === 'Escape') { event.preventDefault(); closePathMenu(); }
    if (event.key === 'Tab') closePathMenu();
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      const items = [...menu.querySelectorAll('[role=menuitem]')];
      const index = items.indexOf(document.activeElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
        : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items[next].focus();
    }
  });
}

async function api(path, data) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), path === '/api/remote/fs' && data?.operation === 'takeover' ? 45000 : path === '/api/delivery' || (path === '/api/remote/fs' && data?.operation === 'delivery') ? 120000 : 20000);
  const options = {method: data === undefined ? 'GET' : 'POST', headers: {}, signal: controller.signal};
  if (data !== undefined) {
    const context = {};
    for (const name of windowFields) if (state?.settings[name] !== undefined) context[name] = state.settings[name];
    options.headers = {'Content-Type': 'application/json', 'X-Local-Token': state?.csrf_token || ''};
    if (isRemoteWorkspace()) {
      for (const name of ['workspace', 'delivery_path']) {
        if (windowSettings[name] !== undefined) context[name] = windowSettings[name]; else delete context[name];
      }
    }
    options.body = JSON.stringify({...context, ...data});
  } else if (path.startsWith('/api/tree') || path.startsWith('/api/file')) {
    path += (path.includes('?') ? '&' : '?') + 'workspace=' + encodeURIComponent(state?.settings.workspace || '');
  }
  try {
    const response = await fetch(path, options);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `请求失败 (${response.status})`);
    return result;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('本地请求等待超时，后台任务未被取消；请稍后查看任务状态。');
    throw error;
  } finally { clearTimeout(timeout); }
}

function action(id, callback) {
  $(id).addEventListener('click', async (event) => {
    event.preventDefault();
    const button = $(id);
    button.disabled = true;
    try { await callback(event); } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; updateRunControls(); }
  });
}

function currentJobs() { return isRemoteWorkspace() ? remoteExecutionState?.jobs || [] : state?.jobs || []; }
function active() { return currentTaskId ? currentJobs().find((job) => job.task_id === currentTaskId && ['running', 'queued', 'stopping'].includes(job.status)) : null; }
function activeInWorkspace() { return currentJobs().some((job) => job.workspace === state.settings.workspace && ['running', 'queued', 'stopping'].includes(job.status)); }

function taskStartBlockReason() {
  if (submitting) return '正在确认上次提交，请勿重复发送。';
  if (replaying) return '正在加载任务历史，请稍候。';
  if (active()) return '当前会话已有任务运行；可等待完成或点击停止。';
  if (isRemoteWorkspace()) {
    if (!remoteInfo.connection?.connected || remoteInfo.connection.target_id !== sshWorkspace.target_id) return 'SSH 未连接或服务器已切换，请重新连接并打开项目。';
    if (remoteInstallBusy || remoteExecutionState?.install?.running) return '正在安装服务器 Codex；安装完成前不能开始任务，请勿重复点击安装。';
    if (remoteInstallError) return '服务器 Codex 安装未确认成功：' + remoteInstallError;
    if (!remoteExecutionState) return '正在获取服务器执行状态，请稍候；若一直等待，请重新打开服务器项目。';
    if (!remoteExecutionState.codex_installed) return '服务器没有可用 Codex。请点击“安装远端 Codex”；仅本机安装不够。';
  }
  if (!state?.settings.workspace) return '请先选择并打开项目目录。';
  if (!state?.has_key) return '请在“连接与模型”配置 API Key。';
  if (!state?.settings.model) return '请在“连接与模型”选择模型。';
  return '';
}

function updateRunControls() {
  const running = Boolean(active());
  const busy = running || replaying || submitting;
  const hasWorkspace = Boolean(state?.settings.workspace);
  const remote = isRemoteWorkspace();
  $('workspace-target').value = workspaceBackend;
  $('target-badge').textContent = remote ? `SSH · ${sshWorkspace.host}` : '本机';
  $('remote-file-tools').hidden = !remote;
  const connected = !remote || (remoteInfo.connection?.connected && remoteInfo.connection.target_id === sshWorkspace.target_id);
  $('prompt').disabled = false;
  $('prompt').placeholder = remote ? `输入指令，Codex 将在 ${sshWorkspace.host} 的服务器项目执行；轨迹自动保存到服务器 delivery。` : '描述需求，让 Codex 在当前目录中工作…';
  const blocked = taskStartBlockReason();
  $('send').disabled = Boolean(blocked);
  $('send').title = blocked || '开始任务';
  $('send-block-reason').textContent = blocked;
  $('send-block-reason').hidden = !blocked;
  $('remote-install-inline').hidden = !remote || !connected || !remoteExecutionState || remoteExecutionState.codex_installed;
  $('remote-install-inline').disabled = remoteInstallBusy || busy;
  $('stop').hidden = !running;
  if (document.activeElement !== $('context-images')) {
    $('context-images').value = active()?.max_context_images ?? state?.settings.max_context_images ?? 6;
  }
  for (const id of ['open-workspace', 'fetch-models', 'probe']) $(id).disabled = replaying || submitting;
  $('save-delivery').disabled = exportingDelivery || replaying || submitting || activeInWorkspace() || !hasWorkspace;
  for (const id of ['delivery-nav', 'delivery-top', 'delivery-inspector']) $(id).disabled = replaying || submitting || !hasWorkspace;
  $('open-finder').disabled = !hasWorkspace && !remote;
  $('open-finder').textContent = remote ? '浏览服务器目录' : '打开目录 ↗';
  $('summarize').disabled = busy || !connected || !resumeId || Boolean(summarySeedId) || !state?.has_key;
  $('continue-task').hidden = busy || !connected || !resumeId || currentJob?.error_code !== 'max_output_tokens';
  if (running) {
    $('run-dot').className = 'dot busy';
    const job = active();
    $('run-label').textContent = job.status === 'queued' ? '等待并发名额或同目录任务结束'
      : job.status === 'stopping' ? '正在停止当前任务' : 'Codex 正在工作';
  } else {
    const failed = currentJob && ['failed', 'timeout', 'interrupted'].includes(currentJob.status);
    $('run-dot').className = failed ? 'dot error' : 'dot';
    $('run-label').textContent = failed ? '任务未完成 · 查看日志' : currentJob?.status === 'completed' ? '任务已完成' : '准备就绪';
  }
  $('session-note').textContent = summarySeedId ? '摘要已准备 · 下一轮精简续接' : resumeId ? '继续当前 Codex 会话' : '新会话';
  const delivery = currentJob?.delivery_error ? '交互保存失败：' + currentJob.delivery_error
    : currentJob?.delivery_records_path ? '交互已保存：' + currentJob.delivery_records_path
      : state?.settings.delivery_path ? '交互自动保存：' + state.settings.delivery_path : '未设置交互保存目录';
  $('delivery-status').textContent = delivery;
  $('delivery-status').title = delivery;
  if (remote && !remoteExecutionState?.codex_installed) $('run-label').textContent = remoteInstallBusy ? '正在安装服务器 Codex…' : remoteInstallError ? '服务器安装失败 · 查看输入框下方错误' : '服务器 Codex 未就绪 · 点击安装远端 Codex';
  $('remote-install-codex').hidden = !remote || remoteExecutionState?.codex_installed;
  $('remote-install-codex').disabled = !connected || busy || remoteInstallBusy;
}

function applyState(next, refreshForm = false) {
  next.settings = {...next.settings, ...windowSettings};
  if (isRemoteWorkspace()) {
    next.settings.workspace = sshWorkspace.workspace;
    next.settings.delivery_path = sshWorkspace.delivery_path;
  }
  state = next;
  if (!Object.keys(windowSettings).length) saveWindowSettings(state.settings);
  $('workspace-path').value = state.settings.workspace;
  $('workspace-name').textContent = state.settings.workspace.split(/[\\/]/).filter(Boolean).pop() || '请选择工作目录';
  $('model-label').textContent = state.settings.model || '配置模型';
  $('endpoint-label').textContent = state.settings.base_url;
  $('codex-version').textContent = state.codex_installed ? `v${state.codex_version} · 已安装` : '未安装';
  $('install-dot').className = state.codex_installed ? 'dot' : 'dot error';
  if (refreshForm) fillSettings();
  renderHistory();
  updateRunControls();
}

function fillSettings() {
  $('base-url').value = state.settings.base_url;
  $('model').value = state.settings.model;
  $('default-delivery-path').value = (isRemoteWorkspace() ? windowSettings.delivery_path : state.settings.delivery_path) || '';
  $('max-output-tokens').value = state.settings.max_output_tokens || 65536;
  $('context-window').value = state.settings.model_context_window || 400000;
  $('auto-compact-limit').value = state.settings.model_auto_compact_token_limit || 280000;
  $('max-parallel-runs').value = state.settings.max_parallel_runs || 3;
  $('task-timeout').value = state.settings.task_timeout_seconds ?? 0;
  $('stream-idle-timeout').value = state.settings.stream_idle_timeout_seconds || 900;
  $('max-auto-continues').value = state.settings.max_auto_continues ?? 30;
  $('max-context-images').value = state.settings.max_context_images ?? 6;
  $('developer-role').value = state.settings.developer_role || 'system';
  $('api-key-file').value = state.settings.api_key_file || '';
  $('remember-key').checked = state.settings.remember_key;
  $('api-key').value = '';
  $('api-key').placeholder = state.has_key ? '已设置，留空保持原 Key' : '输入你的 API Key';
  $('key-state').textContent = state.settings.api_key_file
    ? `Token 来源：${state.settings.api_key_file}。Key 仅在内存中，未复制到 setting.json。`
    : state.settings.remember_key
      ? `已选择本地保存：Key 将写入 ${state.settings_path}，不会进入 delivery；POSIX 使用权限 600，Windows 请确保用户目录 ACL 私有。`
      : `配置文件：${state.settings_path}。Key 默认仅在内存中，退出后需重新输入。`;
  showHttpWarning();
}

function showHttpWarning() { $('http-warning').hidden = !$('base-url').value.trim().startsWith('http://'); }

async function saveSettings() {
  const next = await api('/api/settings', {base_url: $('base-url').value, api_key: $('api-key').value,
    model: $('model').value, remember_key: $('remember-key').checked,
    delivery_path: $('default-delivery-path').value, developer_role: $('developer-role').value,
    api_key_file: $('api-key-file').value,
    max_output_tokens: Number($('max-output-tokens').value),
    model_context_window: Number($('context-window').value),
    model_auto_compact_token_limit: Number($('auto-compact-limit').value),
    max_parallel_runs: Number($('max-parallel-runs').value),
    task_timeout_seconds: Number($('task-timeout').value),
    stream_idle_timeout_seconds: Number($('stream-idle-timeout').value),
    max_auto_continues: Number($('max-auto-continues').value),
    max_context_images: Number($('max-context-images').value)});
  saveWindowSettings({...next.settings, workspace: state.settings.workspace});
  applyState(next);
  $('api-key').value = '';
  $('api-key').placeholder = state.has_key ? '已设置，留空保持原 Key' : '输入你的 API Key';
  return next;
}

function openSettings() { fillSettings(); $('settings-dialog').showModal(); }

let remoteInfo = {connection: {connected: false}, ssh_available: false};
let remoteBusy = false;
let remoteChallenge = null;
let remoteAnswerBusy = false;

function renderRemoteAuthentication() {
  const prompts = remoteInfo.connection?.prompts || [];
  const challenge = prompts[0];
  if (!challenge) {
    if (!remoteAnswerBusy) {
      remoteChallenge = null;
      $('remote-auth-answer').value = '';
      $('remote-auth-dialog').close();
    }
    return;
  }
  if (remoteChallenge?.id === challenge.id) return;
  remoteChallenge = challenge;
  const fingerprint = /continue connecting|authenticity of host/i.test(challenge.prompt);
  $('remote-auth-title').textContent = fingerprint ? '确认 SSH 主机指纹' : 'SSH 密码 / 验证码';
  $('remote-auth-prompt').textContent = challenge.prompt;
  $('remote-auth-answer').value = '';
  $('remote-auth-answer').type = fingerprint ? 'text' : 'password';
  $('remote-auth-answer').placeholder = fingerprint ? '核实指纹后输入 yes；不要盲目确认' : '输入当前提示对应的密码、密钥口令或验证码';
  $('remote-auth-note').textContent = fingerprint
    ? '请向服务器管理员核实指纹。输入 yes 才会信任并由 OpenSSH 写入 known_hosts；指纹变化不会被自动接受。'
    : '只用于当前提示，不保存到配置、delivery 或浏览器存储；请在 3 分钟内提交。';
  $('remote-auth-error').textContent = '';
  if (!$('remote-auth-dialog').open) $('remote-auth-dialog').showModal();
  $('remote-auth-answer').focus();
}

function renderRemoteConnection() {
  const connection = remoteInfo.connection || {connected: false};
  const connected = connection.connected;
  const connecting = connection.connecting;
  const pending = remoteBusy || connecting;
  $('remote-label').textContent = connected ? connection.host : connecting ? '连接中…' : '远程连接';
  $('remote-top').title = connected ? `SSH 已连接：${connection.host}` : '连接远端工作台';
  $('remote-dot').className = 'dot' + (pending ? ' busy' : connected ? ' connected' : '');
  $('remote-config').disabled = pending || connected;
  $('remote-host').disabled = pending || connected;
  $('remote-refresh').disabled = pending || connected;
  $('remote-connect').disabled = pending || connected || !remoteInfo.ssh_available || !$('remote-host').value;
  $('remote-connect').textContent = pending ? '正在连接…' : connected ? '已连接' : '建立 SSH 连接';
  $('remote-disconnect').disabled = remoteBusy || !(connected || connecting);
  $('remote-disconnect').textContent = connecting ? '取消连接' : '断开 SSH';
  $('remote-open').hidden = !connected;
  if (remoteBusy) return;
  const error = connection.error || remoteInfo.error;
  $('remote-status').className = 'remote-status' + (error ? ' error' : connected ? ' connected' : '');
  setRemoteStatus(error || (connected
    ? `已连接 ${connection.host}\n服务器 home：${connection.home}\nPython：${connection.python}\n点击“在本页打开服务器项目”，即可在当前页面浏览和读写远端文件。`
    : connecting ? '正在 SSH 登录并启动内存文件操作器；请完成跳板机 / 目标服务器对应认证。无需远端工作台或网页端口。'
    : '尚未连接。选择 SSH 主机连接，服务器只需 Python 3.9+，无需远端网页或端口。'));
  renderRemoteAuthentication();
}

async function remoteFs(operation, args = {}, targetId = sshWorkspace.target_id || remoteInfo.connection?.target_id) {
  if (!targetId) throw new Error('请先连接 SSH 服务器。');
  try {
    return await api('/api/remote/fs', {target_id: targetId, operation, args});
  } catch (error) {
    if (operation === 'takeover' || !String(error.message).includes('已有远端执行连接')) throw error;
    if (!window.confirm('服务器上有遗留的旧执行器（可能是上次中断的连接或安装进程）占用了执行锁。\n是否结束旧执行器并由当前连接接管？\n（只会结束本工具的执行器；它正在运行的任务会被停止。）')) throw error;
    await api('/api/remote/fs', {target_id: targetId, operation: 'takeover', args: {}});
    toast('已结束旧执行器并接管。');
    return api('/api/remote/fs', {target_id: targetId, operation, args});
  }
}

async function refreshRemoteExecution(targetId = sshWorkspace.target_id) {
  const result = await remoteFs('execution-state', {}, targetId);
  if (targetId === sshWorkspace.target_id) {
    remoteExecutionState = result;
    if (result.codex_installed) remoteInstallError = '';
  }
  return result;
}

async function jobResult(id, offset) {
  return isRemoteWorkspace() ? remoteFs('job', {id, cursor: offset}) : api(`/api/jobs/${id}?cursor=${offset}`);
}

async function setRemoteDeliveryPath(destination) {
  const result = await remoteFs('delivery-path', {workspace: sshWorkspace.workspace, destination});
  sshWorkspace.delivery_path = result.delivery_path;
  state.settings.delivery_path = result.delivery_path;
  $('delivery-path').value = result.delivery_path;
  $('remote-delivery-path').value = result.delivery_path;
  $('delivery-result').textContent = `trajectory 自动保存目录已设置：\n${result.trajectory_path}\n已有运行记录已补存；源码快照可在任务结束后点击创建。`;
  $('delivery-result').hidden = false;
  updateRunControls();
  return result;
}

async function switchWorkspaceTarget(remote) {
  if (submitting || replaying) throw new Error('请等待当前提交或历史加载完成。');
  if (remote) {
    const connection = remoteInfo.connection;
    if (!connection?.connected) { await openRemoteConnection(); return; }
    if (sshWorkspace.target_id !== connection.target_id) {
      sshWorkspace = {workspace: '', delivery_path: '', host: connection.host, target_id: connection.target_id};
      remoteExecutionState = null;
    }
    await refreshRemoteExecution(connection.target_id);
    workspaceBackend = 'ssh';
  } else workspaceBackend = 'local';
  ++treeEpoch;
  pathTarget = null;
  remoteEditing = null;
  $('file-dialog').close();
  $('file-tree').replaceChildren();
  applyState(await api('/api/state'));
  resetChat();
  if (remote && !sshWorkspace.workspace) await browseRemoteDirectory(remoteInfo.connection.home, 'workspace');
  else await loadTree();
}

async function openRemoteProject(path) {
  const connection = remoteInfo.connection;
  const result = await remoteFs('workspace', {path}, connection?.target_id);
  if (connection?.target_id !== remoteInfo.connection?.target_id) throw new Error('服务器已切换，请重新打开项目。');
  remoteEditing = null;
  pathTarget = null;
  $('file-dialog').close();
  workspaceBackend = 'ssh';
  const preferred = $('remote-delivery-path').value.trim() || (sshWorkspace.workspace === result.workspace ? sshWorkspace.delivery_path : '');
  sshWorkspace = {workspace: result.workspace, delivery_path: preferred || result.workspace + '/delivery',
    target_id: connection.target_id, host: connection.host};
  const saved = await remoteFs('delivery-path', {workspace: result.workspace, destination: sshWorkspace.delivery_path});
  sshWorkspace.delivery_path = saved.delivery_path;
  await refreshRemoteExecution();
  ++treeEpoch;
  applyState(await api('/api/state'));
  resetChat();
  await loadTree();
}

async function browseRemoteDirectory(path, purpose = remoteBrowserPurpose) {
  remoteBrowserPurpose = purpose;
  $('remote-browser-select').textContent = purpose === 'delivery' ? '使用此目录保存 delivery 与 trajectory' : '使用此服务器项目目录';
  const result = await remoteFs('browse', {path}, remoteInfo.connection?.target_id);
  $('remote-browser-path').value = result.path;
  $('remote-browser-up').dataset.parent = result.parent;
  $('remote-browser-list').replaceChildren();
  for (const directory of result.children) {
    const button = document.createElement('button');
    button.className = 'file-row';
    button.textContent = '▸ ' + directory.name;
    button.addEventListener('click', () => browseRemoteDirectory(directory.path).catch((error) => toast(error.message, true)));
    $('remote-browser-list').append(button);
  }
  if (!$('remote-browser-dialog').open) $('remote-browser-dialog').showModal();
}

async function remoteOperation(callback) {
  if (remoteBusy) return;
  remoteBusy = true;
  renderRemoteConnection();
  try { await callback(); }
  catch (error) {
    remoteInfo.error = error.message;
    if (remoteInfo.connection) remoteInfo.connection.error = '';
    toast(error.message, true);
  } finally { remoteBusy = false; renderRemoteConnection(); }
}

async function loadRemoteHosts(initial = false) {
  const selected = $('remote-host').value;
  remoteInfo = await api('/api/remote/hosts', initial ? undefined : {ssh_config: $('remote-config').value});
  $('remote-config').value = remoteInfo.ssh_config;
  $('remote-host').replaceChildren(new Option('请选择 SSH 主机', ''));
  for (const host of remoteInfo.hosts) $('remote-host').append(new Option(host, host));
  const host = remoteInfo.connection?.host || selected;
  if (remoteInfo.hosts.includes(host)) $('remote-host').value = host;
  $('remote-host-note').textContent = remoteInfo.error || (remoteInfo.hosts.length
    ? `读取到 ${remoteInfo.hosts.length} 个具体主机别名；继承 User、Port 和 ProxyJump，不复制密钥。`
    : '未找到具体 Host 别名。请检查配置文件；通配符 Host 不作为连接目标。');
}

async function openRemoteConnection() {
  if (!$('remote-dialog').open) $('remote-dialog').showModal();
  setRemoteStatus('正在读取本机 SSH 主机与连接状态…');
  await remoteOperation(() => loadRemoteHosts(!$('remote-config').value));
}

function setupRemoteConnection() {
  $('toast-close').addEventListener('click', hideToast);
  $('toast-copy').addEventListener('click', async () => {
    if (await copyText($('toast-message').textContent, '完整提示')) $('toast-copy').textContent = '已复制';
  });
  $('toast-details').addEventListener('click', () => openCopyDialog($('toast-message').textContent));
  $('copy-retry').addEventListener('click', async () => {
    if (await copyText($('copy-content').value, $('copy-title').textContent)) $('copy-hint').textContent = '已复制全文';
  });
  action('remote-nav', openRemoteConnection);
  action('remote-top', openRemoteConnection);
  $('remote-refresh').addEventListener('click', () => remoteOperation(() => loadRemoteHosts()));
  $('remote-host').addEventListener('change', renderRemoteConnection);
  action('remote-open', async () => {
    $('remote-dialog').close();
    const path = $('remote-workspace-path').value.trim();
    if (path) await openRemoteProject(path); else await switchWorkspaceTarget(true);
  });
  $('workspace-target').addEventListener('change', async () => {
    try { await switchWorkspaceTarget($('workspace-target').value === 'ssh'); }
    catch (error) { toast(error.message, true); }
    finally { updateRunControls(); }
  });
  action('remote-browser-go', () => browseRemoteDirectory($('remote-browser-path').value));
  action('remote-browser-up', () => browseRemoteDirectory($('remote-browser-up').dataset.parent));
  action('remote-browser-select', async () => {
    if (remoteBrowserPurpose === 'delivery') await setRemoteDeliveryPath($('remote-browser-path').value);
    else await openRemoteProject($('remote-browser-path').value);
    $('remote-browser-dialog').close();
  });
  action('delivery-browse', () => browseRemoteDirectory(remoteInfo.connection.home, 'delivery'));
  action('delivery-set-path', async () => {
    await setRemoteDeliveryPath($('delivery-path').value);
    toast('服务器 trajectory 保存目录已设置。');
  });
  const installRemoteCodex = async () => {
    if (remoteInstallBusy) return;
    if (!window.confirm('在服务器用户私有目录安装官方 Codex（下载并校验 npm 官方包），是否继续？')) return;
    const targetId = sshWorkspace.target_id;
    remoteInstallBusy = true;
    remoteInstallError = '';
    updateRunControls();
    try {
      let result = await remoteFs('install-codex', {}, targetId);
      const deadline = Date.now() + 15 * 60 * 1000;
      while (result.install?.running && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        if (targetId !== sshWorkspace.target_id) throw new Error('服务器已切换，请重新打开项目确认安装结果。');
        result = await remoteFs('execution-state', {}, targetId);
      }
      if (targetId !== sshWorkspace.target_id) throw new Error('服务器已切换，请重新打开项目确认安装结果。');
      if (result.install?.running) throw new Error('安装仍在服务器后台进行，请稍后点击刷新或重新打开项目查看。');
      if (!result.codex_installed) throw new Error(result.install?.error || '服务器 Codex 安装未完成。');
      remoteExecutionState = result;
      toast('服务器 Codex 已准备好，可以开始任务。');
    } catch (error) {
      remoteInstallError = error.message;
      throw error;
    } finally { remoteInstallBusy = false; updateRunControls(); }
  };
  action('remote-install-codex', installRemoteCodex);
  action('remote-install-inline', installRemoteCodex);
  action('remote-file-save', async () => {
    const editing = remoteEditing;
    if (!editing?.editable || !isRemoteWorkspace() || editing.target_id !== sshWorkspace.target_id || editing.workspace !== sshWorkspace.workspace) throw new Error('编辑文件的服务器或项目已变化，请重新打开文件。');
    const saved = await remoteFs('save', {workspace: editing.workspace, path: editing.path,
      revision: editing.revision, text: $('remote-file-editor').value}, editing.target_id);
    if (remoteEditing === editing) editing.revision = saved.revision;
    toast('已保存到服务器：' + editing.path);
  });
  action('remote-mkdir', async () => {
    const name = window.prompt('在当前服务器目录新建文件夹（不能包含 / 或 ..）');
    if (!name) return;
    if (/[\\/]/.test(name) || ['.', '..'].includes(name)) throw new Error('请输入单个文件夹名称。');
    await remoteFs('mkdir', {workspace: sshWorkspace.workspace, path: [treePath, name].filter(Boolean).join('/')});
    await loadTree(treePath);
  });
  action('remote-upload', () => $('remote-upload-input').click());
  $('remote-upload-input').addEventListener('change', async () => {
    const file = $('remote-upload-input').files[0];
    $('remote-upload-input').value = '';
    if (!file) return;
    try {
      if (file.size > 2 * 1024 * 1024) throw new Error('单文件上传上限 2MiB。');
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = '';
      for (let offset = 0; offset < bytes.length; offset += 8192) binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
      await remoteFs('upload', {workspace: sshWorkspace.workspace, path: [treePath, file.name].filter(Boolean).join('/'), data: btoa(binary), revision: null});
      await loadTree(treePath);
      toast('文件已上传到服务器；已有文件不会自动覆盖。');
    } catch (error) { toast(error.message, true); }
  });
  action('remote-download', async () => {
    const path = remoteEditing?.path || (pathTarget && !pathTarget.directory ? pathTarget.relative : null);
    if (!path) throw new Error('请先打开文件，或右键选择一个文件。');
    const file = await remoteFs('download', {workspace: sshWorkspace.workspace, path});
    const bytes = Uint8Array.from(atob(file.data), (char) => char.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes]));
    const link = document.createElement('a');
    link.href = url; link.download = file.name; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  });
  $('remote-copy-error').addEventListener('click', async () => {
    const text = remoteInfo.connection?.error || remoteInfo.error || $('remote-status').value;
    if (await copyText(text, '完整连接报错')) $('remote-copy-error').textContent = '报错已复制';
  });
  $('remote-copy-diagnostics').addEventListener('click', async () => {
    if (await copyText(remoteDiagnosticText(), '远程连接诊断')) $('remote-copy-diagnostics').textContent = '诊断已复制';
  });
  $('remote-form').addEventListener('submit', (event) => {
    event.preventDefault();
    remoteOperation(async () => {
      $('remote-status').className = 'remote-status';
      setRemoteStatus('正在 SSH 登录并启动远端文件通道…');
      const connection = await api('/api/remote/connect', {ssh_config: $('remote-config').value, host: $('remote-host').value});
      remoteInfo = {...remoteInfo, connection, error: ''};
      toast(connection.connected ? 'SSH 文件通道已连接。' : '连接已开始，请完成认证提示。');
    });
  });
  $('remote-disconnect').addEventListener('click', () => remoteOperation(async () => {
    remoteInfo.connection = await api('/api/remote/disconnect', {});
    remoteInfo.error = '';
    toast('SSH 已断开。远端文件不会回退为本机操作，请重新连接或手动切换本机项目。');
  }));
  const cancelAuthentication = () => remoteOperation(async () => {
    $('remote-auth-answer').value = '';
    remoteInfo.connection = await api('/api/remote/disconnect', {});
    remoteInfo.error = '';
  });
  $('remote-auth-cancel').addEventListener('click', cancelAuthentication);
  $('remote-auth-dialog').addEventListener('cancel', (event) => { event.preventDefault(); cancelAuthentication(); });
  $('remote-auth-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!remoteChallenge || remoteAnswerBusy) return;
    let answer = $('remote-auth-answer').value;
    const challengeId = remoteChallenge.id;
    $('remote-auth-answer').value = '';
    remoteAnswerBusy = true;
    $('remote-auth-submit').disabled = true;
    try {
      await api('/api/remote/answer', {challenge_id: challengeId, answer});
      remoteChallenge = null;
      $('remote-auth-dialog').close();
    } catch (error) { $('remote-auth-error').textContent = error.message; }
    finally { answer = ''; remoteAnswerBusy = false; $('remote-auth-submit').disabled = false; }
  });
  renderRemoteConnection();
  setInterval(async () => {
    if (remoteBusy || remoteAnswerBusy || !(remoteInfo.connection?.connected || remoteInfo.connection?.connecting || $('remote-dialog').open)) return;
    try { remoteInfo.connection = await api('/api/remote/status'); renderRemoteConnection(); } catch {}
  }, 700);
}

function openDelivery() {
  if (!state.settings.workspace) throw new Error('请先在页面顶部输入路径并打开项目。');
  $('delivery-path').value = state.settings.delivery_path || state.settings.workspace + '/delivery';
  $('remember-delivery-path').checked = true;
  $('delivery-browse').hidden = !isRemoteWorkspace();
  $('delivery-set-path').hidden = !isRemoteWorkspace();
  $('delivery-result').hidden = true;
  $('open-delivery').hidden = true;
  deliveryPath = null;
  $('delivery-dialog').showModal();
}

function taskGroups() {
  const groups = new Map();
  const jobs = currentJobs().slice()
    .sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id));
  for (const job of jobs) {
    const id = job.task_id || job.id;
    if (!groups.has(id)) groups.set(id, {id, jobs: []});
    groups.get(id).jobs.push(job);
  }
  return [...groups.values()].sort((a, b) => {
    const latestA = a.jobs[a.jobs.length - 1];
    const latestB = b.jobs[b.jobs.length - 1];
    return (latestB.finished_at || latestB.created_at).localeCompare(latestA.finished_at || latestA.created_at);
  });
}

function renderHistory() {
  const tasks = taskGroups();
  const signature = JSON.stringify([currentTaskId, state.settings.workspace, tasks.map((task) => {
    const latest = task.jobs[task.jobs.length - 1];
    return [task.id, task.jobs[0].prompt, latest.id, latest.status, task.jobs.length];
  })]);
  if (signature === historySignature) return;
  historySignature = signature;
  $('history-count').textContent = String(tasks.length);
  $('history').replaceChildren();
  if (!tasks.length) {
    const empty = document.createElement('p');
    empty.className = 'muted small';
    empty.textContent = '新建任务后，多轮交互保留在同一个窗口。';
    $('history').append(empty);
  }
  const statuses = {completed: '已完成', failed: '失败', cancelled: '已停止', interrupted: '已中断', running: '运行中', queued: '排队中', stopping: '停止中', timeout: '超时'};
  for (const task of tasks.slice(0, 40)) {
    const first = task.jobs.find((job) => job.kind !== 'summary') || task.jobs[0];
    const latest = task.jobs[task.jobs.length - 1];
    const button = document.createElement('button');
    button.className = 'history-item' + (task.id === currentTaskId ? ' selected' : '');
    button.dataset.taskId = task.id;
    button.title = first.prompt;
    button.append(document.createTextNode(first.prompt.slice(0, 70)));
    const meta = document.createElement('span');
    meta.textContent = `${new Date(latest.created_at).toLocaleString('zh-CN', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'})} · ${task.jobs.length} 轮 · ${statuses[latest.status] || latest.status}`;
    button.append(meta);
    button.addEventListener('click', () => viewTask(task).catch((error) => toast(error.message, true)));
    $('history').append(button);
  }
}

function resetChat() {
  if (submitting) { toast('正在确认提交结果，请稍候。', true); return; }
  viewEpoch += 1;
  replaying = false;
  currentTaskId = null;
  sessionStorage.setItem('codex.selectedTask', '');
  currentJob = null;
  resumeId = null;
  summarySeedId = null;
  cursor = 0;
  errorShown = false;
  for (const child of [...$('messages').children]) if (child.id !== 'welcome') child.remove();
  $('welcome').hidden = false;
  updateRunControls();
  renderHistory();
  $('prompt').focus();
}

function message(role, text) {
  $('welcome').hidden = true;
  const section = document.createElement('section');
  section.className = `message ${role}`;
  const label = document.createElement('div');
  label.className = 'message-label';
  label.textContent = role === 'user' ? 'YOU · 编码需求' : 'CODEX · 回复';
  const body = document.createElement('div');
  body.className = 'message-body';
  body.textContent = text;
  section.append(label, body);
  $('messages').append(section);
}

function eventCard(title, text) {
  const details = document.createElement('details');
  details.className = 'event-card';
  const summary = document.createElement('summary');
  summary.textContent = title;
  const pre = document.createElement('pre');
  pre.textContent = text;
  details.append(summary, pre);
  $('messages').append(details);
}

function renderEvent(event) {
  const item = event.item || {};
  if (event.type === 'item.completed') {
    if (item.type === 'agent_message') message('agent', item.text || '');
    else if (item.type === 'command_execution') eventCard(`${item.exit_code === 0 ? '命令完成' : '命令未成功'} · ${item.command || ''}`, item.aggregated_output || '(无输出)');
    else if (item.type === 'file_change') eventCard('文件修改 · ' + (item.status || ''), JSON.stringify(item.changes || item, null, 2));
    else if (item.type === 'error') eventCard('Codex 提示', item.message || JSON.stringify(item, null, 2));
    else if (item.type !== 'reasoning') eventCard('工具执行 · ' + item.type, JSON.stringify(item, null, 2));
  } else if (event.type === 'log') eventCard('运行日志', event.text || '');
  else if (event.type === 'provider.request') eventCard('模型请求 · 输出预算 ' + event.max_output_tokens, JSON.stringify(event, null, 2));
  else if (event.type === 'provider.completed' && event.status && event.status !== 'completed') eventCard('模型响应未正常完成 · ' + (event.incomplete_reason || event.status), JSON.stringify(event, null, 2));
  else if (event.type === 'provider.warning') eventCard('连接重试或断流提示', event.message || JSON.stringify(event, null, 2));
  else if (event.type === 'turn.completed' && event.usage) eventCard('本轮完成 · Token 用量', JSON.stringify(event.usage, null, 2));
  else if (['turn.failed', 'error'].includes(event.type)) eventCard('接口或任务错误', JSON.stringify(event.error || event.message || event, null, 2));
}

function rememberJob(job) {
  const jobs = currentJobs();
  const index = jobs.findIndex((item) => item.id === job.id);
  if (index >= 0) jobs[index] = job;
  else jobs.unshift(job);
}

function setContinuation(job) {
  if (job.kind === 'summary' && job.status === 'completed') {
    summarySeedId = job.id;
    resumeId = null;
  } else {
    summarySeedId = null;
    resumeId = job.thread_id || job.resume_id || null;
  }
}

function showRunError(job) {
  if (['running', 'queued', 'stopping'].includes(job.status) || !job.finished_at) return false;
  if (!job.error && !job.completion_note && !job.delivery_error) return false;
  if (job.delivery_error) {
    const warning = document.createElement('div');
    warning.className = 'run-error';
    warning.textContent = 'trajectory 写入所选目录失败：' + job.delivery_error + '。运行记录保留在服务器私有状态目录，请修复权限后重新设置保存路径补存。';
    $('messages').append(warning);
  }
  if (job.error) {
    const error = document.createElement('div');
    error.className = 'run-error';
    error.textContent = job.error;
    $('messages').append(error);
  }
  if (job.completion_note) {
    const note = document.createElement('div');
    note.className = 'run-note' + (job.completion_state === 'complete' ? ' done' : '');
    note.textContent = job.completion_note;
    $('messages').append(note);
  }
  return true;
}

function pollJob() {
  if (pollPromise) return pollPromise;
  if (!currentJob || replaying) return Promise.resolve();
  pollBusy = true;
  const id = currentJob.id;
  const epoch = viewEpoch;
  pollPromise = (async () => {
    try {
      let result;
      const nearBottom = $('messages').scrollHeight - $('messages').scrollTop - $('messages').clientHeight < 160;
      do {
        result = await jobResult(id, cursor);
        if (epoch !== viewEpoch || currentJob?.id !== id) return;
        for (const event of result.events) renderEvent(event);
        cursor = result.cursor;
        currentJob = result.job;
        rememberJob(result.job);
      } while (cursor < result.total);
      setContinuation(currentJob);
      if (!errorShown) errorShown = showRunError(currentJob);
      if (nearBottom) $('messages').scrollTop = $('messages').scrollHeight;
      updateRunControls();
      renderHistory();
      if (!active() && result.events.length) loadTree(treePath).catch(() => {});
    } catch (error) { if (epoch === viewEpoch) toast(error.message, true); }
  })().finally(() => { pollBusy = false; pollPromise = null; });
  return pollPromise;
}

async function viewTask(task) {
  if (submitting) throw new Error('请等待提交结果确认后再切换窗口。');
  const epoch = ++viewEpoch;
  replaying = true;
  const latest = task.jobs[task.jobs.length - 1];
  const restored = {};
  for (const name of windowFields) if (latest[name] !== undefined && latest[name] !== null) restored[name] = latest[name];
  saveWindowSettings(restored);
  if (isRemoteWorkspace()) {
    sshWorkspace.workspace = restored.workspace;
    sshWorkspace.delivery_path = restored.delivery_path;
  }
  state.settings = {...state.settings, ...restored};
  currentTaskId = task.id;
  sessionStorage.setItem('codex.selectedTask', task.id);
  currentJob = null;
  resumeId = null;
  summarySeedId = null;
  updateRunControls();
  renderHistory();
  try {
    if (pollPromise) await pollPromise;
    if (epoch !== viewEpoch) return;
    for (const child of [...$('messages').children]) if (child.id !== 'welcome') child.remove();
    let round = 0;
    for (const job of task.jobs) {
      if (epoch !== viewEpoch) return;
      round += 1;
      eventCard(`第 ${round} 轮 · ${job.kind === 'summary' ? '上下文总结' : '任务交互'}`, new Date(job.created_at).toLocaleString('zh-CN'));
      message('user', job.prompt);
      let offset = 0;
      let result;
      do {
        result = await jobResult(job.id, offset);
        if (epoch !== viewEpoch) return;
        for (const event of result.events) renderEvent(event);
        offset = result.cursor;
        rememberJob(result.job);
      } while (offset < result.total);
      errorShown = showRunError(result.job);
      currentJob = result.job;
      cursor = offset;
      setContinuation(result.job);
    }
    $('messages').scrollTop = $('messages').scrollHeight;
  } finally {
    if (epoch === viewEpoch) {
      replaying = false;
      applyState(state);
      await loadTree('').catch((error) => toast(error.message, true));
    }
  }
}

async function loadTree(path = '') {
  const epoch = ++treeEpoch;
  const remote = isRemoteWorkspace();
  if (!state.settings.workspace) { $('file-tree').replaceChildren(); return; }
  const result = remote ? await remoteFs('tree', {workspace: sshWorkspace.workspace, path})
    : await api('/api/tree?path=' + encodeURIComponent(path));
  if (epoch !== treeEpoch || remote !== isRemoteWorkspace()) return;
  treePath = result.path === '.' ? '' : result.path;
  $('tree-path').textContent = '/' + treePath;
  $('tree-up').disabled = !treePath;
  $('file-tree').replaceChildren();
  for (const file of result.children) {
    const button = document.createElement('button');
    button.className = 'file-row';
    button.title = pathInfo(file.path, file.directory).absolute;
    button.addEventListener('contextmenu', (event) => showPathMenu(event, pathInfo(file.path, file.directory)));
    const icon = document.createElement('span');
    icon.textContent = file.directory ? '▸' : '·';
    const name = document.createElement('span');
    name.textContent = file.name;
    button.append(icon, name);
    button.addEventListener('click', async () => {
      try {
        if (file.directory) await loadTree(file.path);
        else {
          const result = remote ? await remoteFs('file', {workspace: sshWorkspace.workspace, path: file.path})
            : await api('/api/file?path=' + encodeURIComponent(file.path));
          if (epoch !== treeEpoch || remote !== isRemoteWorkspace()) return;
          $('file-name').textContent = file.path;
          $('file-name').dataset.path = file.path;
          $('file-content').textContent = result.text;
          $('file-content').hidden = remote && result.editable;
          $('remote-file-editor').hidden = !remote || !result.editable;
          $('remote-file-editor').value = result.text;
          $('remote-file-actions').hidden = !remote;
          $('remote-file-save').disabled = !result.editable;
          remoteEditing = remote ? {...result, path: file.path, target_id: sshWorkspace.target_id, workspace: sshWorkspace.workspace} : null;
          $('file-dialog').showModal();
        }
      } catch (error) { toast(error.message, true); }
    });
    $('file-tree').append(button);
  }
  if (!result.children.length) {
    const empty = document.createElement('p');
    empty.className = 'small muted';
    empty.textContent = state.settings.workspace ? '暂无可展示文件' : '请先在顶部输入项目路径并点击“打开项目”。';
    $('file-tree').append(empty);
  }
}

async function startTask(kind = 'task') {
  const remote = isRemoteWorkspace();
  const targetId = sshWorkspace.target_id;
  if (remote && (!remoteInfo.connection?.connected || targetId !== remoteInfo.connection.target_id)) throw new Error('服务器已断开或切换，请重新连接。');
  if (!state.has_key || !state.settings.model) { openSettings(); return; }
  const blocked = taskStartBlockReason();
  if (blocked) throw new Error(blocked);
  if (replaying || active()) throw new Error('请等待当前任务运行或历史加载完成。');
  const epoch = viewEpoch;
  if (currentJob) await pollJob();
  if (epoch !== viewEpoch) return;
  const prompt = kind === 'summary' ? '' : $('prompt').value.trim();
  if (!prompt && kind !== 'summary') throw new Error('请先输入编码需求。');
  let pending;
  try { pending = JSON.parse(sessionStorage.getItem('codex.pendingRun') || 'null'); } catch {}
  const body = {prompt, kind, resume_id: resumeId, task_id: currentTaskId,
    parent_job_id: currentJob?.id || null, summary_id: summarySeedId, allow_network: $('allow-network').checked};
  if (remote) for (const name of windowFields) body[name] = state.settings[name];
  const signature = JSON.stringify([body, windowSettings, remote ? [sshWorkspace.host, targetId] : 'local']);
  const requestId = pending?.signature === signature ? pending.id : crypto.randomUUID();
  sessionStorage.setItem('codex.pendingRun', JSON.stringify({id: requestId, signature}));
  submitting = true;
  updateRunControls();
  let job;
  try {
    job = remote ? await remoteFs('run', {data: {...body, client_request_id: requestId}}, targetId)
      : await api('/api/run', {...body, client_request_id: requestId});
  } catch (error) {
    const recovered = remote ? await refreshRemoteExecution(targetId).catch(() => null) : await api('/api/state').catch(() => null);
    if (recovered) {
      if (!remote) applyState(recovered);
      job = recovered.jobs.find((item) => item.client_request_id === requestId);
    }
    if (!job) throw error;
    toast('提交响应曾中断，已恢复后台任务，没有重复执行。');
  } finally {
    submitting = false;
    updateRunControls();
  }
  sessionStorage.removeItem('codex.pendingRun');
  if (epoch !== viewEpoch) {
    applyState(await api('/api/state'));
    return;
  }
  summarySeedId = null;
  resumeId = job.resume_id || null;
  currentTaskId = job.task_id;
  sessionStorage.setItem('codex.selectedTask', job.task_id);
  currentJob = job;
  cursor = 0;
  errorShown = false;
  rememberJob(job);
  message('user', job.prompt);
  if (kind === 'task') $('prompt').value = '';
  $('messages').scrollTop = $('messages').scrollHeight;
  updateRunControls();
  await pollJob();
}

async function syncState() {
  if (stateSyncBusy) return;
  stateSyncBusy = true;
  try {
    const next = await api('/api/state');
    state.jobs = next.jobs;
    state.tasks = next.tasks;
    state.csrf_token = next.csrf_token;
    state.has_key = next.has_key || Boolean(state.settings.api_key_file);
    state.codex_installed = next.codex_installed;
    if (!remoteInstallBusy && isRemoteWorkspace() && remoteInfo.connection?.connected && sshWorkspace.target_id === remoteInfo.connection.target_id) await refreshRemoteExecution();
    renderHistory();
    updateRunControls();
    if (!replaying && !submitting && currentTaskId) {
      const task = taskGroups().find((item) => item.id === currentTaskId);
      if (task && task.jobs[task.jobs.length - 1].id !== currentJob?.id) await viewTask(task);
    }
  } catch {
    $('run-label').textContent = '连接暂时不可用，后台任务状态将在恢复后同步';
  } finally { stateSyncBusy = false; }
}

async function boot() {
  applyState(await api('/api/state'), true);
  setupPathMenu();
  setupRemoteConnection();
  await loadTree().catch((error) => toast('无法读取原工作目录，请重新打开项目：' + error.message, true));
  const tasks = taskGroups();
  let saved = sessionStorage.getItem('codex.selectedTask');
  try {
    const pending = JSON.parse(sessionStorage.getItem('codex.pendingRun') || 'null');
    const recovered = pending && state.jobs.find((job) => job.client_request_id === pending.id);
    if (recovered) { saved = recovered.task_id; sessionStorage.removeItem('codex.pendingRun'); }
  } catch {}
  const selected = saved === null ? tasks.find((task) => task.jobs[0].workspace === state.settings.workspace)
    : tasks.find((task) => task.id === saved);
  if (selected) await viewTask(selected);
  action('settings-nav', openSettings);
  action('model-button', openSettings);
  for (const id of ['delivery-nav', 'delivery-top', 'delivery-inspector']) action(id, openDelivery);
  action('workspace-nav', () => $('prompt').focus());
  action('new-chat', resetChat);
  action('clear-view', resetChat);
  action('open-workspace', async () => {
    if (isRemoteWorkspace()) { await openRemoteProject($('workspace-path').value); return; }
    const next = await api('/api/workspace', {path: $('workspace-path').value, window_context: true});
    saveWindowSettings({workspace: next.settings.workspace});
    applyState(next);
    resetChat();
    await loadTree();
    toast('已原地打开项目。Codex 将直接在该目录工作。');
  });
  action('open-finder', async () => {
    if (isRemoteWorkspace()) await browseRemoteDirectory(sshWorkspace.workspace || remoteInfo.connection?.home, 'workspace');
    else await api('/api/open', {path: state.settings.workspace});
  });
  action('refresh-files', () => loadTree(treePath));
  action('tree-up', () => loadTree(treePath.split(/[\\/]/).slice(0, -1).join('/')));
  action('send', () => startTask());
  action('summarize', () => startTask('summary'));
  action('continue-task', async () => {
    $('prompt').value = '继续上一轮未完成的任务。先检查当前文件和已执行结果，不要盲目重复已完成或有副作用的操作；将剩余工作分成较小步骤完成并验证。';
    await startTask();
  });
  action('stop', async () => {
    const job = active();
    if (!job) return;
    if (isRemoteWorkspace()) rememberJob(await remoteFs('stop', {id: job.id}));
    else await api('/api/stop', {id: job.id});
  });
  action('fetch-models', async () => {
    await saveSettings();
    const {models} = await api('/api/models', {});
    $('model-options').replaceChildren();
    for (const model of models) { const option = document.createElement('option'); option.value = model; $('model-options').append(option); }
    $('models-note').textContent = `已获取 ${models.length} 个模型。点击输入框选择，也可以手动输入。`;
    $('model').focus();
    toast(`已获取 ${models.length} 个模型`);
  });
  action('probe', async () => { await saveSettings(); const result = await api('/api/probe', {}); toast(result.message); });
  action('forget-key', async () => {
    const next = await api('/api/settings', {clear_key: true, remember_key: false});
    saveWindowSettings({api_key_file: ''});
    applyState(next);
    fillSettings();
    toast('已清除本地保存与内存中的 Key。');
  });
  action('open-delivery', async () => {
    if (!deliveryPath) return;
    if (isRemoteWorkspace()) await copyText(deliveryPath, '服务器 delivery 路径');
    else await api('/api/open', {path: deliveryPath});
  });
  $('settings-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = $('settings-form').querySelector('[type=submit]');
    button.disabled = true;
    try { await saveSettings(); $('settings-dialog').close(); toast('连接设置已保存'); }
    catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  });
  $('delivery-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (exportingDelivery) return;
    exportingDelivery = true;
    $('save-delivery').disabled = true;
    $('save-delivery').textContent = '正在扫描与保存…';
    try {
      const remote = isRemoteWorkspace();
      const result = remote ? await remoteFs('delivery', {workspace: sshWorkspace.workspace, destination: $('delivery-path').value})
        : await api('/api/delivery', {path: $('delivery-path').value, remember_path: $('remember-delivery-path').checked});
      if ($('remember-delivery-path').checked) {
        if (remote) sshWorkspace.delivery_path = $('delivery-path').value;
        else saveWindowSettings({delivery_path: $('delivery-path').value});
      }
      $('open-delivery').textContent = remote ? '复制服务器交付路径' : '打开交付目录';
      applyState(await api('/api/state'));
      deliveryPath = result.path;
      const skipped = result.skipped_files ? `\n注意：${result.skipped_files} 个超过 25MiB 的文件未打包，详见 file_manifest.json。` : '';
      const trajectory = remote ? `\n真实服务器运行：${result.runs} 条\ntrajectory：${result.trajectory_path}` : '';
      $('delivery-result').textContent = `已保存 ${result.files} 个源码文件。\n${result.path}${trajectory}\n验收状态：${result.acceptance_status}（尚未验收）${skipped}`;
      $('delivery-result').hidden = false;
      $('open-delivery').hidden = false;
      toast('交付快照已保存，旧交付未被覆盖。');
    } catch (error) { toast(error.message, true); }
    finally { exportingDelivery = false; $('save-delivery').textContent = '创建交付快照 ↗'; updateRunControls(); }
  });
  document.querySelectorAll('[data-close]').forEach((button) => button.addEventListener('click', (event) => {
    event.preventDefault(); $(button.dataset.close).close();
  }));
  document.querySelectorAll('[data-prompt]').forEach((button) => button.addEventListener('click', () => {
    $('prompt').value = button.dataset.prompt; $('prompt').focus();
  }));
  $('base-url').addEventListener('input', showHttpWarning);
  $('context-images').addEventListener('change', async () => {
    const value = Number($('context-images').value);
    if (!Number.isInteger(value) || value < 0 || value > 50) {
      toast('截图数量需为 0–50 的整数。', true);
      updateRunControls();
      return;
    }
    state.settings.max_context_images = value;
    saveWindowSettings({max_context_images: value});
    const job = active();
    if (!job) { toast(`本窗口之后的任务每次发送最近 ${value} 张截图。`); return; }
    try {
      rememberJob(isRemoteWorkspace() ? await remoteFs('job-images', {id: job.id, max_context_images: value})
        : await api('/api/job-images', {id: job.id, max_context_images: value}));
      toast(`已调整：当前任务下一次模型请求起发送最近 ${value} 张截图。`);
    } catch (error) { toast(error.message, true); }
  });
  $('remember-key').addEventListener('change', () => {
    $('key-state').textContent = $('remember-key').checked
      ? `Key 将保存在 ${state.settings_path}（权限 600）。不要共享此文件。`
      : '仅保留在服务内存中；保存后删除磁盘配置中的 Key。';
  });
  document.addEventListener('keydown', (event) => {
    if (!(event.metaKey || event.ctrlKey)) return;
    if (event.key === 'Enter' && document.activeElement === $('prompt')) {
      event.preventDefault(); if (!$('send').disabled) $('send').click();
    }
    if (event.key.toLowerCase() === 'n' && !document.querySelector('dialog[open]')) { event.preventDefault(); resetChat(); }
  });
  setInterval(() => { if (currentJob) pollJob(); }, 1000);
  setInterval(syncState, 2000);
}

boot().catch((error) => toast('工作台加载失败：' + error.message, true));
