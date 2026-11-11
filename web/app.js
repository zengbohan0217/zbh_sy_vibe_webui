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
const windowFields = ['workspace', 'model', 'base_url', 'delivery_path', 'developer_role', 'api_key_file',
  'max_output_tokens', 'model_context_window', 'model_auto_compact_token_limit',
  'max_parallel_runs', 'task_timeout_seconds', 'stream_idle_timeout_seconds', 'max_auto_continues',
  'max_context_images'];
try { windowSettings = JSON.parse(sessionStorage.getItem('codex.windowSettings') || '{}'); } catch {}

function saveWindowSettings(values) {
  for (const name of windowFields) if (values[name] !== undefined) windowSettings[name] = values[name];
  sessionStorage.setItem('codex.windowSettings', JSON.stringify(windowSettings));
}

function toast(message, error = false) {
  clearTimeout(toastTimer);
  $('toast').textContent = message;
  $('toast').className = 'toast' + (error ? ' error' : '');
  $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, error ? 12000 : 5500);
}

function pathInfo(relative, directory) {
  const root = state.settings.workspace.replace(/\/+$/, '');
  const absolute = root + (relative ? '/' + relative : '');
  return {absolute, relative: relative || '.', name: absolute.split('/').pop(), directory};
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
  const timeout = setTimeout(() => controller.abort(), path === '/api/delivery' ? 120000 : 20000);
  const options = {method: data === undefined ? 'GET' : 'POST', headers: {}, signal: controller.signal};
  if (data !== undefined) {
    const context = {};
    for (const name of windowFields) if (state?.settings[name] !== undefined) context[name] = state.settings[name];
    options.headers = {'Content-Type': 'application/json', 'X-Local-Token': state?.csrf_token || ''};
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

function active() { return currentTaskId ? state?.jobs.find((job) => job.task_id === currentTaskId && ['running', 'queued', 'stopping'].includes(job.status)) : null; }
function activeInWorkspace() { return state?.jobs.some((job) => job.workspace === state.settings.workspace && ['running', 'queued', 'stopping'].includes(job.status)); }

function updateRunControls() {
  const running = Boolean(active());
  const busy = running || replaying || submitting;
  const hasWorkspace = Boolean(state?.settings.workspace);
  $('send').disabled = busy || !state?.has_key || !state?.settings.model || !hasWorkspace;
  $('stop').hidden = !running;
  if (document.activeElement !== $('context-images')) {
    $('context-images').value = active()?.max_context_images ?? state?.settings.max_context_images ?? 6;
  }
  for (const id of ['open-workspace', 'fetch-models', 'probe']) $(id).disabled = replaying || submitting;
  $('save-delivery').disabled = replaying || submitting || activeInWorkspace() || !hasWorkspace;
  $('open-finder').disabled = !hasWorkspace;
  $('summarize').disabled = busy || !resumeId || Boolean(summarySeedId) || !state?.has_key;
  $('continue-task').hidden = busy || !resumeId || currentJob?.error_code !== 'max_output_tokens';
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
}

function applyState(next, refreshForm = false) {
  next.settings = {...next.settings, ...windowSettings};
  state = next;
  if (!Object.keys(windowSettings).length) saveWindowSettings(state.settings);
  $('workspace-path').value = state.settings.workspace;
  $('workspace-name').textContent = state.settings.workspace.split('/').filter(Boolean).pop() || '请选择工作目录';
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
  $('default-delivery-path').value = state.settings.delivery_path || '';
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
      ? `已选择本地保存：Key 将写入 ${state.settings_path}（权限 600），不会进入 delivery。`
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

function openDelivery() {
  if (!state.settings.workspace) throw new Error('请先在页面顶部输入路径并打开项目。');
  $('delivery-path').value = state.settings.delivery_path || state.settings.workspace + '/delivery';
  $('remember-delivery-path').checked = true;
  $('delivery-result').hidden = true;
  $('open-delivery').hidden = true;
  deliveryPath = null;
  $('delivery-dialog').showModal();
}

function taskGroups() {
  const groups = new Map();
  const jobs = state.jobs.slice()
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
  const index = state.jobs.findIndex((item) => item.id === job.id);
  if (index >= 0) state.jobs[index] = job;
  else state.jobs.unshift(job);
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
  if (!job.error && !job.completion_note) return false;
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
        result = await api(`/api/jobs/${id}?cursor=${cursor}`);
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
        result = await api(`/api/jobs/${job.id}?cursor=${offset}`);
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
  const result = await api('/api/tree?path=' + encodeURIComponent(path));
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
          const result = await api('/api/file?path=' + encodeURIComponent(file.path));
          $('file-name').textContent = file.path;
          $('file-name').dataset.path = file.path;
          $('file-content').textContent = result.text;
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
  if (!state.has_key || !state.settings.model) { openSettings(); return; }
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
  const signature = JSON.stringify([body, windowSettings]);
  const requestId = pending?.signature === signature ? pending.id : crypto.randomUUID();
  sessionStorage.setItem('codex.pendingRun', JSON.stringify({id: requestId, signature}));
  submitting = true;
  updateRunControls();
  let job;
  try {
    job = await api('/api/run', {...body, client_request_id: requestId});
  } catch (error) {
    const recovered = await api('/api/state').catch(() => null);
    if (recovered) {
      applyState(recovered);
      job = state.jobs.find((item) => item.client_request_id === requestId);
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
  state.jobs.unshift(job);
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
    const next = await api('/api/workspace', {path: $('workspace-path').value, window_context: true});
    saveWindowSettings({workspace: next.settings.workspace});
    applyState(next);
    resetChat();
    await loadTree();
    toast('已原地打开项目。Codex 将直接在该目录工作。');
  });
  action('open-finder', async () => { await api('/api/open', {path: state.settings.workspace}); });
  action('refresh-files', () => loadTree(treePath));
  action('tree-up', () => loadTree(treePath.split('/').slice(0, -1).join('/')));
  action('send', () => startTask());
  action('summarize', () => startTask('summary'));
  action('continue-task', async () => {
    $('prompt').value = '继续上一轮未完成的任务。先检查当前文件和已执行结果，不要盲目重复已完成或有副作用的操作；将剩余工作分成较小步骤完成并验证。';
    await startTask();
  });
  action('stop', async () => { if (active()) await api('/api/stop', {id: active().id}); });
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
  action('open-delivery', async () => { if (deliveryPath) await api('/api/open', {path: deliveryPath}); });
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
    $('save-delivery').disabled = true;
    $('save-delivery').textContent = '正在扫描与保存…';
    try {
      const result = await api('/api/delivery', {path: $('delivery-path').value,
        remember_path: $('remember-delivery-path').checked});
      if ($('remember-delivery-path').checked) saveWindowSettings({delivery_path: $('delivery-path').value});
      applyState(await api('/api/state'));
      deliveryPath = result.path;
      const skipped = result.skipped_files ? `\n注意：${result.skipped_files} 个超过 25MiB 的文件未打包，详见 file_manifest.json。` : '';
      $('delivery-result').textContent = `已保存 ${result.files} 个源码文件。\n${result.path}\n验收状态：${result.acceptance_status}（尚未验收）${skipped}`;
      $('delivery-result').hidden = false;
      $('open-delivery').hidden = false;
      toast('交付快照已保存，旧交付未被覆盖。');
    } catch (error) { toast(error.message, true); }
    finally { $('save-delivery').textContent = '创建交付快照 ↗'; updateRunControls(); }
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
      rememberJob(await api('/api/job-images', {id: job.id, max_context_images: value}));
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
