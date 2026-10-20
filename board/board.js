/* D4 MVP 2.0 看板入口：五层导航（总体驾驶舱 / 项目 / 业务线 / 任务 / 成员）+ 面包屑。
   只读：所有数据来自只读页面接口；不派发任务、不写数据库、不改变任何执行状态。 */
const $ = id => document.getElementById(id);
const LAYERS = ['portfolio', 'projects', 'businessLines', 'tasks', 'workers'];
const LAYER_LABELS = {portfolio: '总体驾驶舱', projects: '项目', businessLines: '业务线', tasks: '任务', workers: '成员'};
const state = {
  projects: [], reviews: {items: []}, portfolio: null,
  workers: {available: false, workers: [], notes: [], counts: {}},
  view: 'portfolio', selectedProject: null, selectedTask: null,
  lineFilter: '', recordFilter: '', searchFilter: '',
  refreshing: false, selectionVersion: 0, dataSignature: null, detailSignature: null,
};
const labels = {created:'待准备', submitted_pending_host:'已发出，等待对方确认', accepted_by_host:'对方已收到', in_progress:'正在处理', awaiting_review:'等待负责人检查', changes_requested:'需要补充或修改', approved:'已检查通过', blocked:'遇到问题', delivered:'已交回', failed:'处理失败'};
const reviewLabels = {approved:'已检查通过', changes_requested:'需要补充或修改', blocked:'暂时无法继续', request_info:'需要补充信息'};
function node(tag, text, cls) { const e = document.createElement(tag); if (text !== undefined && text !== null) e.textContent = String(text); if (cls) e.className = cls; return e; }
function clear(e) { e.replaceChildren(); }
function label(value) { return labels[value] || '状态待确认'; }
function reviewLabel(value) { return reviewLabels[value] || '检查状态待确认'; }
function memberLabel(id, display) {
  if (display && /[\u4e00-\u9fff]/.test(display)) return display;
  if (!id) return '未登记执行成员';
  return '执行成员';
}
function sourceLabel(id, display) {
  const text = String(display || '').trim();
  if (text && /[\u4e00-\u9fff]/.test(text)) return text;
  if (/codebuddy/i.test(String(id || ''))) return 'CodeBuddy CLI';
  if (/qclaw/i.test(String(id || ''))) return 'QClaw';
  if (/workbuddy/i.test(String(id || ''))) return 'WorkBuddy';
  return id ? '已登记运行位置' : '尚未登记运行位置';
}
async function api(path) {
  const response = await fetch(path, { cache: 'no-store' });
  if (response.status === 404) { const error = new Error('这条链接没有对应记录，可能尚未登记或已经失效。'); error.status = 404; throw error; }
  if (!response.ok) { const error = new Error(response.status === 503 ? '数据库暂时无法读取，稍后重试。' : '读取失败（' + response.status + '）。'); error.status = response.status; throw error; }
  return response.json();
}
function projectById(projectId) { return state.projects.find(p => p.projectId === projectId) || null; }
function projectName(projectId) { const p = projectById(projectId); return (p && p.name) || projectId; }
function projectNames() { const map = {}; for (const p of state.projects) map[p.projectId] = p.name || p.projectId; return map; }
function liveProject() { return state.projects.find(p => p.recordType !== 'archive') || null; }
function filters() { return {line: state.lineFilter, record: state.recordFilter, search: state.searchFilter}; }
function projectTasks(project) { return ProjectView.filterTasks((project && project.tasks) || [], filters()); }
function allTasks() {
  const rows = [];
  for (const project of state.projects) for (const task of projectTasks(project)) rows.push({...task, projectName: project.name || project.projectId});
  return rows;
}
/* -- 导航与面包屑 -------------------------------------------------------- */
function renderNav() {
  const root = $('layer-nav'); if (!root) return;
  for (const button of root.querySelectorAll('.layer-link')) {
    const active = button.dataset.layer === state.view;
    button.classList.toggle('selected', active);
    button.setAttribute('aria-current', active ? 'true' : 'false');
    button.onclick = () => showLayer(button.dataset.layer);
  }
}
function showLayer(layer) {
  if (LAYERS.indexOf(layer) < 0) return;
  state.view = layer; state.selectedTask = null;
  renderNav(); render();
}
function crumbs() {
  const rows = [{layer: 'portfolio', label: '总体驾驶舱'}];
  if (state.selectedProject) rows.push({layer: 'projects', id: state.selectedProject, label: projectName(state.selectedProject)});
  if (state.view === 'businessLines' && state.lineFilter) rows.push({layer: 'businessLines', label: state.lineFilter});
  if (state.selectedTask) rows.push({layer: 'tasks', id: state.selectedTask, label: state.selectedTask});
  return rows;
}
function onCrumb(crumb) {
  if (crumb.layer === 'portfolio') { state.selectedProject = null; state.selectedTask = null; state.lineFilter = ''; showLayer('portfolio'); return; }
  if (crumb.layer === 'projects') { state.selectedTask = null; showLayer('projects'); }
}
/* -- 左栏：项目与任务 --------------------------------------------------- */
function renderRail() {
  const root = $('projects'); clear(root);
  let count = 0;
  const options = {line: state.lineFilter, record: state.recordFilter, search: state.searchFilter};
  for (const project of state.projects) {
    const button = node('button', null, 'project-button' + (state.selectedProject === project.projectId ? ' selected' : ''));
    button.type = 'button';
    const tasks = ProjectView.filterTasks(project.tasks || [], options);
    button.append(node('strong', project.name || project.projectId), node('small', (project.tasks || []).length + ' 项任务 · ' + (project.recordType === 'archive' ? '历史资料登记' : (project.statusLabel || '项目'))));
    button.onclick = () => selectProject(project.projectId);
    root.append(button);
    if (state.selectedProject !== project.projectId) continue;
    if (!tasks.length) root.append(node('div', (project.tasks || []).length ? '没有符合条件的任务' : '这个项目还没有任务。', 'empty'));
    for (const task of tasks) {
      count += 1;
      const row = node('button', null, 'task-button' + (state.selectedTask === task.taskId ? ' selected' : ''));
      row.type = 'button';
      row.append(node('strong', task.title || '未命名任务'), node('small', (task.stateLabel || label(task.taskDisplayState)) + ' · ' + memberLabel(task.agentId, task.workerLabel || task.agentLabel)));
      row.onclick = () => selectTask(task.taskId).catch(showError);
      root.append(row);
    }
  }
  if (!state.projects.length) root.append(node('div', '尚未登记任何项目。', 'empty'));
  $('task-count').textContent = String(count);
  renderFilterOptions();
}
function renderFilterOptions() {
  const projects = $('project-filter');
  if (projects) {
    clear(projects);
    const all = document.createElement('option'); all.value = ''; all.textContent = '全部项目';
    projects.append(all);
    for (const project of state.projects) {
      const option = document.createElement('option');
      option.value = project.projectId; option.textContent = project.name || project.projectId;
      projects.append(option);
    }
    projects.value = state.selectedProject || '';
    projects.onchange = () => { const value = projects.value; if (value) selectProject(value); else { state.selectedProject = null; state.selectedTask = null; state.lineFilter = ''; render(); } };
  }
  const lines = $('line-filter');
  if (lines) {
    clear(lines);
    const all = document.createElement('option'); all.value = ''; all.textContent = '全部业务线';
    lines.append(all);
    const project = projectById(state.selectedProject);
    for (const line of (project && project.businessLines) || []) {
      const option = document.createElement('option');
      option.value = line.businessLineId; option.textContent = line.name || line.businessLineId;
      lines.append(option);
    }
    for (const line of (project && project.businessLinesPlanned) || []) {
      const option = document.createElement('option');
      option.value = line.businessLineId; option.textContent = '计划：' + (line.name || line.businessLineId);
      lines.append(option);
    }
    lines.value = state.lineFilter || '';
    lines.disabled = !project;
    lines.onchange = () => { state.lineFilter = lines.value; state.view = 'businessLines'; renderNav(); render(); };
  }
  const records = $('record-filter');
  if (records) {
    records.value = state.recordFilter || '';
    records.onchange = () => { state.recordFilter = records.value; render(); };
  }
  const search = $('search-filter');
  if (search) {
    search.value = state.searchFilter || '';
    search.oninput = () => { state.searchFilter = search.value.trim(); renderRail(); renderDetailPane(); };
  }
}
/* -- 右侧待审核 --------------------------------------------------------- */
function renderReviews() {
  const project = projectById(state.selectedProject);
  const root = $('reviews'); clear(root);
  if (project && project.recordType === 'archive') {
    $('review-heading').textContent = '项目待解决事项';
    ProjectView.issues(root, project, id => selectTask(id).catch(showError));
    return;
  }
  $('review-heading').textContent = '待审核';
  const items = (state.reviews && state.reviews.items) || [];
  for (const row of items) {
    const wrap = node('div', null, 'item');
    const button = node('button', row.delivery && row.delivery.summary || row.title || '新的交付结果');
    button.onclick = () => selectTask(row.taskId).catch(showError);
    wrap.append(button, node('small', memberLabel(row.agentId, row.agentLabel) + ' · 等待负责人检查'));
    root.append(wrap);
  }
  if (!items.length) root.append(node('div', '暂无待审核交付。', 'empty'));
}
/* -- 详情面板 ----------------------------------------------------------- */
function renderDetailPane() {
  const project = projectById(state.selectedProject);
  const options = {crumbs: crumbs(), onNavigate: onCrumb, onTask: id => selectTask(id).catch(showError), onProject: selectProject, projectName: projectName(state.selectedProject), projectNames: projectNames(), filters: filters()};
  if (state.view === 'workers') { ProjectView.workerList($('workers'), state.workers, options); return; }
  if (state.view === 'businessLines') {
    const lines = [];
    for (const item of state.projects) {
      if (state.selectedProject && item.projectId !== state.selectedProject) continue;
      if (item.recordType === 'archive') continue;
      for (const line of item.businessLines || []) lines.push({...line, projectId: item.projectId});
      for (const line of item.businessLinesPlanned || []) lines.push({...line, projectId: item.projectId});
    }
    ProjectView.businessLines($('workers'), {businessLines: lines, notes: ['计划业务线与登记业务线分开显示，不按标题自动合并。']}, options);
    return;
  }
  if (state.view === 'tasks') { renderTaskList(options); return; }
  if (state.selectedTask) return;
  if (state.view === 'projects' && project) {
    if (project.recordType === 'archive') { ProjectView.overview($('workers'), project, options.onTask); return; }
    ProjectView.projectDetail($('workers'), project, options);
    return;
  }
  ProjectView.portfolio($('portfolio'), state.portfolio || fallbackPortfolio(), options);
}
function renderTaskList(options) {
  const root = $('workers'); clear(root);
  const wrap = node('div', null, 'task-list-view');
  ProjectView.breadcrumb(wrap, options.crumbs, options.onNavigate);
  const head = node('div', null, 'title-row');
  head.append(node('h1', '任务'), node('span', state.selectedProject ? projectName(state.selectedProject) : '全部项目', 'badge'));
  wrap.append(head);
  const rows = state.selectedProject ? projectTasks(projectById(state.selectedProject)) : allTasks();
  if (!rows.length) wrap.append(node('p', (state.selectedProject ? '这个项目' : '当前范围') + '没有符合条件的任务；筛选条件不会跨项目套用。', 'empty'));
  for (const task of rows) wrap.append(ProjectView.taskRow(task, options.onTask));
  wrap.append(node('p', '任务用稳定编号归属到项目与业务线；同名任务不会跨项目合并。', 'panel-note'));
  root.append(wrap);
}
function fallbackPortfolio() {
  const rows = state.projects.map(project => ({
    projectId: project.projectId, name: project.name || project.projectId, recordType: project.recordType,
    statusLabel: project.recordType === 'archive' ? '历史资料登记' : (project.statusLabel || '未登记状态'),
    gateSummary: {total: project.recordType === 'archive' ? 0 : (project.milestones || []).length, reviewPassed: 0, unverified: project.recordType === 'archive' ? 0 : (project.milestones || []).length},
    tasks: (project.tasks || []).length, workers: 0, keyIssueCount: (project.issues || []).length,
    nextStep: project.nextStep || '历史资料未登记下一步',
  }));
  return {
    recordTypeLabel: '总体驾驶舱（没有拿到项目登记数据）',
    conclusion: ['没有读到项目登记或执行记录，无法判断门禁、关键问题或独立审核；驾驶舱只显示已读取到的登记情况。'],
    conclusionBasis: ['milestone_gate', 'key_issue', 'independent_review'],
    headline: {gates: [], keyIssues: [], independentReview: {totals: {}}},
    auxiliary: {taskCompletionRatio: {role: 'auxiliary', note: '没有可用的任务数据。'}},
    projects: rows,
    unverified: [{item: '执行与审核', evidence: 'document_archive', note: '只读取到资料登记或没有数据。'}],
    notes: ['驾驶舱数据来自项目详情接口；没有可用项目登记时只显示已知情况，不补造结论。'],
  };
}
function render() {
  renderNav(); renderRail(); renderFilterOptions(); renderReviews();
  if (state.view === 'workers') { clear($('portfolio')); clear($('task-detail')); }
  else if (state.view === 'tasks' || state.view === 'businessLines') { clear($('portfolio')); clear($('task-detail')); }
  else { clear($('workers')); }
  clear($('portfolio')); clear($('workers')); clear($('task-detail'));
  renderDetailPane();
}
/* -- 选择与读取 --------------------------------------------------------- */
async function selectTask(taskId, quiet = false, supplied = null) {
  const version = ++state.selectionVersion;
  state.selectedTask = taskId; state.view = 'tasks';
  if (!quiet) { $('notice').textContent = '正在读取任务…'; $('notice').classList.remove('error'); renderNav(); renderRail(); }
  try {
    const data = supplied || await api('/api/tasks/' + encodeURIComponent(taskId));
    if (version !== state.selectionVersion) return;
    const signature = JSON.stringify(data);
    if (quiet && signature === state.detailSignature) return;
    state.detailSignature = signature;
    const projectId = (data.task && data.task.project_id) || data.projectId;
    if (projectId) { state.selectedProject = projectId; renderRail(); renderFilterOptions(); renderReviews(); }
    clear($('portfolio')); clear($('workers')); clear($('task-detail'));
    if (data.recordType === 'archive') {
      ProjectView.detail($('task-detail'), data, selectProject);
    } else {
    ProjectView.taskDetail($('task-detail'), data, {crumbs: crumbs(), onNavigate: onCrumb, projectName: projectName(projectId)});
    }
    $('notice').textContent = ''; $('notice').classList.remove('error');
  } catch (error) {
    if (version !== state.selectionVersion) return;
    state.selectedTask = null;
    showError(error, '这个任务不存在，或旧链接已经失效；可以回到项目继续查看其它任务。');
  }
}
async function selectProject(projectId) {
  ++state.selectionVersion;
  state.selectedProject = projectId; state.selectedTask = null; state.lineFilter = '';
  state.view = 'projects';
  $('notice').textContent = '正在读取项目…'; $('notice').classList.remove('error');
  renderNav(); renderRail(); renderFilterOptions();
  const project = projectById(projectId);
  try {
    const detail = await api('/api/projects/' + encodeURIComponent(projectId));
    if (detail && detail.portfolio) state.portfolio = detail.portfolio;
    if (detail && detail.workersRegistry) state.workers = detail.workersRegistry;
    if (detail && detail.projectId === state.selectedProject) {
      const index = state.projects.findIndex(item => item.projectId === state.selectedProject);
      if (index >= 0 && detail.recordType !== 'archive') state.projects[index] = {...state.projects[index], ...detail};
    }
    render(); $('notice').textContent = ''; $('notice').classList.remove('error');
  } catch (error) {
    showError(error, '这个项目没有可读的项目详情，或旧链接已经失效。');
  }
}
function showError(error, hint) {
  clear($('task-detail')); clear($('portfolio')); clear($('workers'));
  $('notice').textContent = (error && error.message ? error.message : '读取失败') + (hint ? ' ' + hint : '');
  $('notice').classList.add('error');
  $('updated').textContent = '读取失败';
}
function captureView() {
  const scroll = [document.scrollingElement, ...document.querySelectorAll('.rail, .detail, .review-rail')].filter(Boolean).map(el => ({el, top: el.scrollTop, left: el.scrollLeft}));
  const active = document.activeElement;
  const focus = active && active.id ? {id: active.id, start: active.selectionStart, end: active.selectionEnd} : null;
  const folds = [...document.querySelectorAll('details')].map(el => ({text: el.querySelector('summary')?.textContent, open: el.open}));
  return () => {
    for (const item of scroll) { item.el.scrollTop = item.top; item.el.scrollLeft = item.left; }
    for (const item of folds) { const el = [...document.querySelectorAll('details')].find(e => e.querySelector('summary')?.textContent === item.text); if (el) el.open = item.open; }
    if (focus) { const el = document.getElementById(focus.id); if (el) { el.focus({preventScroll: true}); if (typeof focus.start === 'number' && el.setSelectionRange) el.setSelectionRange(focus.start, focus.end); } }
  };
}
async function refresh(manual = false) {
  if (state.refreshing || document.getElementById('material-dialog')?.open) return;
  state.refreshing = true;
  const selectionVersion = state.selectionVersion;
  const selectedTask = state.selectedTask;
  const restore = captureView();
  if (!state.dataSignature) { $('notice').textContent = '正在读取看板…'; }
  try {
    const [projects, reviews, detail] = await Promise.all([api('/api/projects'), api('/api/reviews'), selectedTask ? api('/api/tasks/' + encodeURIComponent(selectedTask)) : Promise.resolve(null)]);
    if (selectionVersion !== state.selectionVersion) return;
    const signature = JSON.stringify([projects, reviews]);
    const changed = signature !== state.dataSignature;
    state.dataSignature = signature;
    state.projects = projects.projects || [];
    state.reviews = reviews || {items: []};
    if (state.selectedProject && !projectById(state.selectedProject)) { state.selectedProject = null; state.selectedTask = null; state.lineFilter = ''; }
    const cockpitSource = (state.selectedProject && (projectById(state.selectedProject) || {}).recordType !== 'archive') ? state.selectedProject : (liveProject() || {}).projectId;
    if (cockpitSource && changed) {
      try {
        const source = await api('/api/projects/' + encodeURIComponent(cockpitSource));
        if (selectionVersion !== state.selectionVersion) return;
        state.portfolio = source.portfolio || null;
        if (source.workersRegistry) state.workers = source.workersRegistry;
      } catch (error) { state.portfolio = null; }
    }
    if (state.selectedTask && detail) {
      if (changed) { renderRail(); renderReviews(); }
      await selectTask(state.selectedTask, true, detail);
    } else if (changed) { render(); }
    $('notice').textContent = state.projects.length ? '' : '当前还没有可显示的项目。';
    $('notice').classList.remove('error');
    $('updated').textContent = (changed ? '数据更新于 ' : '已检查，无变化 ') + new Date().toLocaleTimeString('zh-CN');
  } catch (error) {
    // Keep the last readable view while a background observation fails.
    $('updated').textContent = '检查失败，保留上次数据';
    if (!state.dataSignature || manual) { $('notice').textContent = error.message || '读取失败'; $('notice').classList.add('error'); }
  } finally { restore(); state.refreshing = false; }
}
$('refresh').onclick = () => refresh(true);
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 30000);
