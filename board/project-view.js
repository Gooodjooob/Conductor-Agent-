/* D4 MVP 2.0 只读视图组件：总体驾驶舱、项目/业务线、任务、成员、历史资料。
   只渲染已上报事实；空态、未知、旧链接都显示明确文案，不猜、不补造、不派发。 */
const ProjectView = (() => {
  const statusLabels = {verified:'已核验', validated:'限定流程通过', partial:'部分完成', in_progress:'正在开发 · 待验收', blocked:'暂未接通', planned:'计划项 · 未启动', awaiting_review:'待检查'};
  const kindLabels = {implementation:'开发任务', validation:'验证任务', research:'能力核验', planned:'后续计划'};
  const issueLabels = {open:'待解决', deferred:'后续处理', closed:'已解决'};
  const layerLabels = {portfolio:'总体驾驶舱', projects:'项目', businessLines:'业务线', tasks:'任务', workers:'成员'};
  const recordTypeLabels = {
    real_execution:'真实执行记录', system_ledger:'系统入账', historical_archive:'历史资料登记',
    planning_snapshot:'规划快照（计划中，未执行）', project_status:'项目/业务线登记状态',
    self_reported_delivery:'执行成员自报交付', independent_review:'独立审核结论',
    worker_registry:'成员登记', business_line:'业务线', project:'项目', portfolio:'总体驾驶舱',
    project_unregistered:'未登记项目', milestone:'里程碑', task:'任务',
  };
  const evidenceLabels = {
    plan_only:'只有规划资料，没有执行证据',
    document_archive:'历史资料登记',
    host_event:'已在真实环境运行（宿主证据）',
    synthetic_demo:'演示 / 本地核对数据',
    worker_report:'执行成员自报，未经独立审核',
    review_record:'独立审核记录',
    unverified:'尚未验证（没有找到证据）',
    unknown:'无法判定（资料或表缺失）',
  };
  const gateStateLabels = {
    review_passed:'独立审核已通过', delivered_pending_review:'已自报交付，待独立审核',
    in_progress:'执行中（有宿主接收证据）', submitted_pending_host:'已发出，等待对方确认',
    document_only:'只有历史资料', planned:'计划中 · 未启动', blocked:'暂时受阻',
    unverified:'尚未验证（没有找到证据）',
  };
  function n(tag, text, cls) { const e = document.createElement(tag); if (text !== undefined && text !== null) e.textContent = String(text); if (cls) e.className = cls; return e; }
  function sec(root, title, cls) { const s = n('section', null, cls || 'section'); s.append(n('h2', title)); root.append(s); return s; }
  function fact(root, title, text) { const f = n('div', null, 'fact'); f.append(n('label', title), n('div', text || '未记录')); root.append(f); }
  function button(text, action, cls) { const b = n('button', text, cls || 'text-button'); b.type = 'button'; b.onclick = action; return b; }
  function value(text, fallback) { return (text === undefined || text === null || text === '') ? (fallback || '未记录') : text; }
  function listText(items, fallback, limit) {
    const rows = (items || []).filter(item => item !== undefined && item !== null && String(item).trim() !== '');
    if (!rows.length) return fallback || '未记录';
    return (limit && rows.length > limit ? rows.slice(0, limit).concat('…') : rows).join('；');
  }
  function badge(text, cls) { return n('span', text, 'badge ' + (cls || '')); }
  function recordBadge(recordType) { return n('span', recordTypeLabels[recordType] || recordType || '类型未知', 'badge record ' + (recordType || 'unknown')); }
  function evidenceBadge(evidence) { return n('span', evidenceLabels[evidence] || '证据等级未知', 'badge evidence ' + (evidence || 'unknown')); }
  function plainSummary(root, plain) {
    const box = n('div', null, 'plain-summary');
    if (!plain) { box.append(n('p', '这份记录没有普通摘要。', 'empty')); root.append(box); return box; }
    const grid = n('div', null, 'facts');
    fact(grid, '已经做了什么', plain.done);
    fact(grid, '现在遇到什么问题', plain.problem);
    fact(grid, '下一步做什么', plain.nextStep);
    box.append(grid, n('p', '摘要来源：' + (plain.sourceLabel || '未标注'), 'plain-source'));
    root.append(box);
    return box;
  }
  function technicalDetails(root, technical, extra) {
    const wrap = sec(root, '技术详情');
    if (!technical) { wrap.append(n('p', '没有可展开的技术详情。', 'empty')); return wrap; }
    const fold = n('details', null, 'tech-details technical');
    fold.append(n('summary', '技术详情：编号、路径、摘要、命令与原始报错（默认折叠）'));
    const ids = technical.recordIds || {};
    const facts = n('div', null, 'facts');
    fact(facts, '任务编号', ids.taskId);
    fact(facts, '执行轮次', listText(ids.attemptIds, '未记录'));
    fact(facts, '交付编号', listText(ids.deliveryIds, '未记录'));
    fact(facts, '报告编号', value(ids.reportId, '未记录'));
    fact(facts, '宿主运行编号', value(ids.hostRunId, '未记录'));
    fact(facts, '会话编号', value(ids.sessionId, '未记录'));
    fold.append(facts);
    fold.append(n('h3', '路径'), n('p', listText(technical.paths, '未记录路径')));
    fold.append(n('h3', '改动文件'), n('p', listText(technical.changedFiles, '未记录文件')));
    fold.append(n('h3', '摘要 / SHA'), n('p', listText((technical.digests || []).map(item => item.kind + '：' + item.value), '未记录摘要')));
    fold.append(n('h3', '命令'), n('p', listText(technical.commands, '未记录命令')));
    fold.append(n('h3', '执行成员自报的检查'), n('p', listText(technical.claimedVerification, '未记录检查')));
    fold.append(n('h3', '原始报错'), n('p', listText(technical.rawErrors, '没有原始报错')));
    if (extra) fold.append(n('p', extra, 'panel-note'));
    for (const note of technical.notes || []) fold.append(n('p', note, 'panel-note'));
    wrap.append(fold);
    return wrap;
  }
  function unverifiedBox(root, items) {
    const rows = (items || []).filter(item => item && item.item);
    if (!rows.length) return null;
    const box = sec(root, '尚未验证的事项');
    const list = n('div', null, 'unverified-list');
    for (const item of rows) {
      const row = n('div', null, 'unverified-row');
      row.append(n('strong', item.item), evidenceBadge(item.evidence), n('p', item.note || '没有找到证据'));
      list.append(row);
    }
    box.append(list, n('p', '这些事项只表示“没有找到证据”，不表示已经失败，也不表示已经通过。', 'panel-note'));
    root.append(box);
    return box;
  }
  function breadcrumb(root, crumbs, onNavigate) {
    const nav = n('nav', null, 'breadcrumb');
    (crumbs || []).forEach((crumb, index) => {
      if (index) nav.append(n('span', '›', 'crumb-sep'));
      if (crumb.id && onNavigate && index < crumbs.length - 1) {
        nav.append(button(crumb.label || crumb.id, () => onNavigate(crumb), 'crumb-link'));
      } else {
        nav.append(n('span', crumb.label || crumb.id || '未命名', 'crumb-current'));
      }
    });
    root.append(nav);
    return nav;
  }
  function gateRow(gate, onTask) {
    const row = n('article', null, 'gate-row');
    const head = n('div', null, 'item-head');
    head.append(n('strong', value(gate.name, gate.gateId)), badge(gateStateLabels[gate.state] || '状态待确认', gate.state), recordBadge(gate.recordType), evidenceBadge(gate.evidence));
    row.append(head, n('p', '门禁条件：' + value(gate.gateCriteria, '未登记门禁条件')));
    row.append(n('small', '登记状态：' + value(gate.declaredStatusLabel, '未登记') + ' · 业务线：' + value(gate.businessLineName, '未登记业务线') + (gate.sectionRef ? ' · 来源章节：' + gate.sectionRef : '')));
    row.append(n('p', gate.note || '', 'panel-note'));
    if ((gate.taskIds || []).length && onTask) {
      const links = n('div', null, 'inline-links');
      for (const taskId of gate.taskIds) links.append(button(taskId, () => onTask(taskId), 'task-title-link'));
      row.append(links);
    }
    return row;
  }
  function issueRow(issue, onTask) {
    const box = n('article', null, 'issue-card');
    const head = n('div', null, 'item-head');
    head.append(n('strong', value(issue.title, '未命名问题')), recordBadge(issue.recordType), evidenceBadge(issue.evidence));
    box.append(head, n('p', value(issue.problem, '未登记问题描述')));
    box.append(n('small', '处理对象：' + value(issue.owner) + ' · 状态：' + value(issue.state, '待确认')));
    box.append(n('p', '下一步：' + value(issue.nextStep, '待补充')));
    if (issue.taskId && onTask) box.append(button('查看任务 ' + issue.taskId, () => onTask(issue.taskId), 'task-title-link'));
    return box;
  }
  function portfolio(root, data, opts) {
    const options = opts || {};
    root.replaceChildren();
    const wrap = n('div', null, 'portfolio-view');
    breadcrumb(wrap, options.crumbs || [{layer:'portfolio', label:'总体驾驶舱'}], options.onNavigate);
    const head = n('div', null, 'title-row');
    head.append(n('h1', '总体驾驶舱'), n('span', data.recordTypeLabel || '多个项目的汇总视图', 'badge'));
    wrap.append(head);
    if (data.emptyState) wrap.append(n('p', data.emptyState.text, 'empty'));
    const conclusion = sec(wrap, '总体结论（先看门禁、关键问题和独立审核）');
    for (const line of data.conclusion || []) conclusion.append(n('p', line));
    const auxiliary = (data.auxiliary || {}).taskCompletionRatio || {};
    conclusion.append(n('p', '辅助信息：任务完成比例只作参考，' + value(auxiliary.note, '不代表项目完成。'), 'panel-note'));
    const gates = sec(wrap, '里程碑门禁');
    const gateRows = (data.headline || {}).gates || [];
    if (!gateRows.length) gates.append(n('p', '还没有登记或计划中的门禁。', 'empty'));
    for (const gate of gateRows) gates.append(gateRow(gate, options.onTask));
    const issues = sec(wrap, '关键问题');
    const issueRows = (data.headline || {}).keyIssues || [];
    if (!issueRows.length) issues.append(n('p', '当前没有登记中的关键问题。', 'empty'));
    for (const issue of issueRows) issues.append(issueRow(issue, options.onTask));
    const review = ((data.headline || {}).independentReview || {}).totals || {};
    const reviewBox = sec(wrap, '独立审核');
    reviewBox.append(n('p', '已通过 ' + value(review.approved, 0) + ' 项 · 尚未审核 ' + value(review.notReviewed, 0) + ' 项 · 要求返工 ' + value(review.changesRequested, 0) + ' 项'));
    reviewBox.append(n('p', '执行成员自报完成不等于独立审核通过；审核结论单独保存和显示。', 'panel-note'));
    const projects = sec(wrap, '项目一览');
    const projectRows = data.projects || [];
    if (!projectRows.length) projects.append(n('p', '尚未登记任何项目。', 'empty'));
    for (const row of projectRows) {
      const card = n('article', null, 'portfolio-project');
      const cardHead = n('div', null, 'item-head');
      cardHead.append(n('strong', value(row.name, row.projectId)));
      if (row.statusLabel) cardHead.append(badge(row.statusLabel, row.status || 'unknown'));
      cardHead.append(n('span', '记录类型：' + (recordTypeLabels[row.recordType] || '未标注'), 'badge record'));
      card.append(cardHead);
      card.append(n('p', '门禁：共 ' + value((row.gateSummary || {}).total, 0) + ' 项，独立审核通过 ' + value((row.gateSummary || {}).reviewPassed, 0) + ' 项，尚未验证 ' + value((row.gateSummary || {}).unverified, 0) + ' 项'));
      card.append(n('small', '任务 ' + value(row.tasks, 0) + ' 项 · 参与成员 ' + value(row.workers, 0) + ' 人 · 关键问题 ' + value(row.keyIssueCount, 0) + ' 项'));
      card.append(n('p', '下一步：' + value(row.nextStep, '尚未上报下一步'), 'panel-note'));
      if (row.projectId && options.onProject) card.append(button('查看项目 ' + row.projectId, () => options.onProject(row.projectId), 'task-title-link'));
      projects.append(card);
    }
    unverifiedBox(wrap, data.unverified);
    const notes = ((data.notes || []).length ? data.notes : []);
    if (notes.length) wrap.append(n('p', notes.join(' '), 'panel-note'));
    root.append(wrap);
  }
  function projectDetail(root, project, opts) {
    const options = opts || {};
    root.replaceChildren();
    const wrap = n('div', null, 'project-view');
    breadcrumb(wrap, options.crumbs || [{layer:'projects', id: project.projectId, label: value(project.name, project.projectId)}], options.onNavigate);
    const head = n('div', null, 'title-row');
    head.append(n('h1', value(project.name, project.projectId)), badge(project.statusLabel || '未登记状态', project.status || 'unknown'), recordBadge(project.recordType));
    wrap.append(head, n('p', project.goalLabel || '尚未登记项目目标', 'project-goal'));
    if (project.registrationNote) wrap.append(n('div', project.registrationNote, 'registration-note'));
    if (project.emptyState) wrap.append(n('p', project.emptyState.text, 'empty'));
    const facts = n('div', null, 'facts project-facts');
    fact(facts, '当前阶段', project.currentStageLabel);
    fact(facts, '项目负责人', value(project.ownerId, '未登记负责人'));
    fact(facts, '下一步目标', project.nextStepLabel);
    fact(facts, '规划来源', project.planningSnapshot ? ('已导入规划快照（版本 ' + value(project.planningSnapshot.documentVersion, '未标注') + '）') : '尚未导入规划快照');
    wrap.append(facts);
    const gates = sec(wrap, '里程碑门禁');
    if (!(project.gates || []).length) gates.append(n('p', '这个项目还没有登记或计划中的门禁。', 'empty'));
    for (const gate of project.gates || []) gates.append(gateRow(gate, options.onTask));
    const lines = sec(wrap, '业务线');
    const registered = project.businessLines || [];
    const planned = project.businessLinesPlanned || [];
    if (!registered.length && !planned.length) lines.append(n('p', '这个项目还没有业务线。', 'empty'));
    for (const line of registered) lines.append(lineRow(line, options));
    for (const line of planned) lines.append(lineRow(line, options));
    const issues = sec(wrap, '问题与待解决事项');
    if (!(project.keyIssues || []).length) issues.append(n('p', '当前没有登记中的问题；这不代表项目已完成。', 'empty'));
    for (const issue of project.keyIssues || []) issues.append(issueRow(issue, options.onTask));
    const review = sec(wrap, '独立审核');
    const totals = (project.independentReview || {}).totals || {};
    review.append(n('p', '已通过 ' + value(totals.approved, 0) + ' 项 · 尚未审核 ' + value(totals.notReviewed, 0) + ' 项 · 要求返工 ' + value(totals.changesRequested, 0) + ' 项'));
    const ratio = (project.auxiliary || {}).taskCompletionRatio || {};
    review.append(n('p', '辅助字段：已完成任务 ' + value(ratio.agentReportedDone, 0) + '/' + value(ratio.tasksTotal, 0) + '（不代表项目完成，人工确认状态：' + value((ratio.userConfirmed || {}).state, '未知') + '）', 'panel-note'));
    const workers = sec(wrap, '参与成员');
    if (!(project.workers || []).length) workers.append(n('p', '这个项目还没有登记的执行成员。', 'empty'));
    for (const worker of project.workers || []) workers.append(workerCard(worker));
    const tasks = sec(wrap, '项目任务');
    const projectTasks = filterTasks(project.tasks || [], options.filters);
    if (!(project.tasks || []).length) tasks.append(n('p', project.emptyState ? project.emptyState.text : '这个项目没有任务。', 'empty'));
    else if (!projectTasks.length) tasks.append(n('p', '没有符合条件的任务。', 'empty'));
    for (const task of projectTasks) tasks.append(taskRow(task, options.onTask));
    unverifiedBox(wrap, project.unverified);
    technicalDetails(wrap, {
      recordIds: { taskId: (project.technical || {}).projectId },
      paths: (project.technical || {}).planSourcePaths,
      digests: ((project.technical || {}).planSourceDigests || []).map(item => ({kind:'规划来源摘要', value: item})),
      commands: [], changedFiles: [], rawErrors: [],
      notes: ['这里显示项目登记与规划来源的技术信息；规划资料不代表执行事实。'],
    });
    root.append(wrap);
  }
  function lineRow(line, options) {
    const row = n('article', null, 'business-line');
    const head = n('div', null, 'item-head');
    head.append(n('strong', value(line.name, line.businessLineId)), badge(line.statusLabel || line.stateLabel || '未登记状态', line.state || 'unknown'), recordBadge(line.recordType), evidenceBadge(line.evidence));
    row.append(head);
    row.append(n('p', value(line.goal, '未登记业务线目标')));
    row.append(n('small', '任务 ' + (line.taskIds || []).length + ' 项 · 成员 ' + (line.workerIds || []).length + ' 人' + (line.sourceSection ? ' · 来源章节：' + line.sourceSection : '')));
    if (line.note) row.append(n('p', line.note, 'panel-note'));
    for (const gate of line.gates || []) row.append(gateRow(gate, options.onTask));
    if ((line.taskIds || []).length && options.onLine) row.append(button('只看这条业务线的任务', () => options.onLine(line.businessLineId), 'text-button'));
    return row;
  }
  function workerCard(worker) {
    const card = n('article', null, 'worker-card');
    const head = n('div', null, 'item-head');
    head.append(n('strong', value(worker.displayName, worker.workerId || '未命名成员')), badge(worker.roleLabel || '未登记角色'), badge(worker.acceptingLabel || '接单状态未知', worker.acceptingTasks ? 'open' : 'planned'));
    card.append(head);
    const facts = n('div', null, 'facts');
    fact(facts, '稳定编号', value(worker.workerId));
    fact(facts, '宿主 / 环境', value(worker.host) + ' / ' + value(worker.environment, '未登记环境'));
    fact(facts, '登记状态', value(worker.registrationLabel, '未知'));
    fact(facts, '运行验证', value(worker.runtimeLabel, '未验证'));
    fact(facts, '默认模型', value(worker.defaultModel, '未登记'));
    fact(facts, '绑定版本', value(worker.bindingRevision, '未登记'));
    fact(facts, '可参与项目', listText(worker.allowedProjects, '未登记项目范围'));
    fact(facts, '目录范围', '读：' + listText(worker.readScope, '未登记') + ' / 写：' + listText(worker.writeScope, '未登记'));
    card.append(facts);
    if (worker.description) card.append(n('p', worker.description, 'panel-note'));
    if (worker.modelNote) card.append(n('p', worker.modelNote, 'panel-note'));
    for (const note of worker.notes || []) card.append(n('p', note, 'panel-note'));
    card.append(n('p', '证据：' + (evidenceLabels[worker.evidence] || '未知'), 'panel-note'));
    return card;
  }
  function taskRow(task, onTask) {
    const row = n('article', null, 'task-card');
    const head = n('div', null, 'item-head');
    head.append(button(value(task.title, task.taskId), () => onTask && onTask(task.taskId), 'task-title-link'));
    head.append(badge(task.stateLabel || '状态待确认', task.taskDisplayState || 'unknown'), recordBadge(task.recordType), evidenceBadge(task.evidence));
    row.append(head);
    row.append(n('p', value((task.plainSummary || {}).done, '尚未上报已完成内容')));
    row.append(n('small', '问题：' + value((task.plainSummary || {}).problem, '尚未上报') + ' · 下一步：' + value((task.plainSummary || {}).nextStep, '尚未上报')));
    row.append(n('small', '执行成员：' + value(task.workerLabel) + ' · 业务线：' + value(task.businessLineName, task.businessLineId || '未登记业务线') + ' · 独立审核：' + value((task.reviewRecord || {}).label, '尚未独立审核')));
    if (task.taskId) row.append(button('查看任务详情', () => onTask && onTask(task.taskId), 'text-button'));
    return row;
  }
  function filterTasks(tasks, filters) {
    const box = filters || {};
    return (tasks || []).filter(task => {
      if (box.line && task.businessLineId !== box.line) return false;
      if (box.record && task.recordType !== box.record) return false;
      if (box.search) {
        const text = String(task.title || '') + String(task.taskId || '');
        if (text.toLowerCase().indexOf(String(box.search).toLowerCase()) < 0) return false;
      }
      return true;
    });
  }
  function businessLines(root, data, opts) {
    const options = opts || {};
    root.replaceChildren();
    const wrap = n('div', null, 'business-lines-view');
    breadcrumb(wrap, options.crumbs || [{layer:'businessLines', label:'业务线'}], options.onNavigate);
    const head = n('div', null, 'title-row');
    head.append(n('h1', '业务线'), badge('按项目分组；计划业务线与登记业务线分开显示'));
    wrap.append(head);
    const names = options.projectNames || {};
    const groups = [];
    for (const line of data.businessLines || []) {
      const key = line.projectId || '未登记项目';
      let group = groups.find(item => item.key === key);
      if (!group) { group = {key: key, lines: []}; groups.push(group); }
      group.lines.push(line);
    }
    for (const group of groups) {
      const box = sec(wrap, names[group.key] || group.key);
      for (const line of group.lines) box.append(lineRow(line, options));
    }
    if (!groups.length) wrap.append(n('p', '还没有业务线或计划业务线。', 'empty'));
    for (const note of data.notes || []) wrap.append(n('p', note, 'panel-note'));
    root.append(wrap);
  }
  function workerList(root, data, opts) {
    const options = opts || {};
    root.replaceChildren();
    const wrap = n('div', null, 'worker-view');
    breadcrumb(wrap, options.crumbs || [{layer:'workers', label:'成员管理'}], options.onNavigate);
    const head = n('div', null, 'title-row');
    head.append(n('h1', '成员管理'), badge('独立登记对象，不与项目或任务平级'));
    wrap.append(head);
    if (!data.available) {
      wrap.append(n('p', '这个数据库没有成员登记表，成员信息一律显示为未知；不按任务里的名字推测成员。', 'empty'));
    }
    const facts = n('div', null, 'facts');
    fact(facts, '登记成员', value((data.counts || {}).total, 0));
    fact(facts, '可接任务', value((data.counts || {}).acceptingTasks, 0));
    fact(facts, '运行验证通过', value((data.counts || {}).verified, 0));
    wrap.append(facts);
    const list = n('div', null, 'worker-list');
    if (!(data.workers || []).length) list.append(n('p', '没有已登记的成员。', 'empty'));
    for (const worker of data.workers || []) list.append(workerCard(worker));
    wrap.append(list);
    if ((data.unknownWorkers || []).length) {
      wrap.append(n('p', '有任务指向未登记成员编号：' + data.unknownWorkers.join('、') + '。这里只如实显示，不做身份推断。', 'panel-note'));
    }
    for (const note of data.notes || []) wrap.append(n('p', note, 'panel-note'));
    root.append(wrap);
  }
  function taskDetail(root, data, opts) {
    const options = opts || {};
    root.replaceChildren();
    const wrap = n('div', null, 'task-view');
    breadcrumb(wrap, options.crumbs || [], options.onNavigate);
    const task = data.task || {};
    const head = n('div', null, 'title-row');
    head.append(n('h1', value(task.title, task.task_id)), badge(data.stateLabel || '状态待确认', data.taskDisplayState || 'unknown'), recordBadge(data.recordType));
    wrap.append(head);
    wrap.append(n('p', '证据等级：' + (data.evidenceLabel || evidenceLabels[data.evidence] || '未知') + ' · 记录类型：' + (data.recordTypeLabel || recordTypeLabels[data.recordType] || '未标注'), 'subtitle'));
    plainSummary(wrap, data.plainSummary);
    const layer = sec(wrap, '归属与执行成员');
    const layerFacts = n('div', null, 'facts');
    fact(layerFacts, '所属项目', value(options.projectName, task.project_id));
    fact(layerFacts, '所属业务线', value((data.businessLine || {}).name, (data.businessLine || {}).businessLineId || '未登记业务线'));
    fact(layerFacts, '执行成员', value((data.worker || {}).label, '未登记执行成员'));
    fact(layerFacts, '宿主', value((data.worker || {}).host, '未登记宿主'));
    fact(layerFacts, '登记绑定版本', value((data.worker || {}).bindingRevision, '未登记'));
    fact(layerFacts, '任务目标', task.goal);
    layer.append(layerFacts);
    if ((data.businessLine || {}).note) layer.append(n('p', data.businessLine.note, 'panel-note'));
    if ((data.worker || {}).note) layer.append(n('p', data.worker.note, 'panel-note'));
    const records = sec(wrap, '交付与审核记录');
    const delivered = (data.workerReport || {}).state === 'delivered';
    records.append(n('p', '执行成员自报交付：' + (delivered ? '已收到交付' : '尚未收到交付') + ' · ' + value((data.workerReport || {}).note, '')));
    records.append(n('p', '独立审核：' + value((data.independentReview || {}).label, '尚未独立审核') + '（记录类型：' + recordTypeLabels[(data.independentReview || {}).recordType] + '）'));
    for (const delivery of data.deliveries || []) {
      const row = n('div', null, 'item');
      row.append(n('strong', value(delivery.summary, '本次交付')), badge(delivery.isCurrent ? '当前交付' : '历史交付', delivery.isCurrent ? 'open' : 'planned'));
      row.append(n('small', '记录类型：' + (recordTypeLabels.self_reported_delivery)));
      if ((delivery.risks_json || []).length) row.append(n('p', '风险：' + delivery.risks_json.join('；')));
      if ((delivery.unfinished_json || []).length) row.append(n('p', '未完成项：' + delivery.unfinished_json.join('；')));
      records.append(row);
    }
    if (!(data.deliveries || []).length) records.append(n('p', '这个任务还没有交付记录。', 'empty'));
    for (const review of data.reviews || []) {
      const row = n('div', null, 'item');
      row.append(n('strong', '独立审核：' + value(review.decision, '待核对')), badge(recordTypeLabels.independent_review, 'record'));
      row.append(n('p', value(review.note, '未写审核意见')));
      records.append(row);
    }
    const process = sec(wrap, '处理过程');
    if (!(data.attempts || []).length) process.append(n('p', '还没有执行轮次。', 'empty'));
    for (const attempt of data.attempts || []) {
      const row = n('div', null, 'item');
      row.append(n('strong', '第 ' + value(attempt.ordinal, '?') + ' 次处理'), badge(attempt.displayState || '状态待确认', attempt.displayState || 'unknown'));
      row.append(n('small', '发出时间：' + value(attempt.submitted_at, '未记录') + ' · 对方确认时间：' + value(attempt.accepted_by_host_at, '尚未确认')));
      process.append(row);
    }
    unverifiedBox(wrap, data.unverified);
    technicalDetails(wrap, data.technical);
    root.append(wrap);
  }
  function sourcePaths(root, rows, title) {
    if (!rows || !rows.length) return;
    const box = sec(root, title || '存储位置与主要资料');
    for (const row of rows) { const item = n('div', null, 'source-location'); item.append(n('strong', row.label || '来源文件'), n('code', row.path || row.sourcePath || '未登记')); box.append(item); }
  }
  function executionFacts(root, facts) {
    if (!facts) return;
    const box = sec(root, facts.historical ? '旧候选任务与角色（不继承到新项目候选）' : '自动读取的任务与角色');
    if (facts.scopeNote) box.append(n('p', facts.scopeNote, 'panel-note'));
    if (!facts.available) { box.append(n('p', facts.error || '来源暂不可读，保留登记快照。', 'empty')); return; }
    const grid = n('div', null, 'facts');
    fact(grid, '来源当前阶段', facts.phase);
    fact(grid, '任务状态', facts.ledgerState || facts.declaredState);
    fact(grid, '原任务 / attempt', [facts.taskId, facts.attemptId].filter(Boolean).join(' / ') || '当前身份尚未齐备');
    fact(grid, '指定开发者', facts.assignedAgent || '未登记');
    fact(grid, '开发回传身份', facts.developer ? facts.developer + '（task/attempt账本匹配）' : '未取得当前执行账本证据');
    fact(grid, '运行身份', facts.observedAgent && facts.observedAgent !== 'UNVERIFIED' ? facts.observedAgent : 'UNVERIFIED，不能仅凭派发成员认定');
    fact(grid, 'QA', facts.qa || '当前来源未登记独立 QA 身份');
    fact(grid, '独立 review', (facts.reviews || []).map(r => r.reviewer_id + '：' + r.decision).join('；') || '未找到当前task/attempt的review记录');
    box.append(grid, n('p', facts.ledgerNote || '执行状态、开发自报和独立review分开读取；单个任务审核通过不代表整个项目完成。', 'panel-note'));
    sourcePaths(box, [{label:'状态来源', path:facts.sourcePath}, ...(facts.cardPath ? [{label:'当前任务卡',path:facts.cardPath}] : []), ...(facts.workspace ? [{label:'当前执行workspace',path:facts.workspace}] : []), ...(facts.deliveries || []).map(d => ({label:'开发报告', path:d.reportPath || d.report_path}))], '执行证据位置');
  }
  function sourceCards(root, cards) {
    if (!cards || !cards.length) return;
    const box = sec(root, '任务卡角色与来源');
    const fold = n('details', null, 'source-card-list');
    fold.append(n('summary', cards.length + ' 份卡：查看开发者、QA、reviewer与文件位置'));
    fold.append(n('p', '卡内声明是计划分工。实际执行者与QA以匹配的task/attempt和回传为准；不按文件名猜测。', 'panel-note'));
    const table = n('table');
    const head = n('tr'); for (const text of ['任务卡 / 文件位置', '卡内执行者', '卡内QA', '卡内reviewer', '实际身份证据']) head.append(n('th', text));
    table.append(head);
    for (const card of cards) {
      const row = n('tr'); const title = n('td'); title.append(n('strong', card.title), n('code', card.path));
      const roles = card.roles || {};
      row.append(title, n('td', roles.executor || '未注明'), n('td', roles.qa || '未注明独立QA'), n('td', roles.reviewer || '未注明'), n('td', card.actualIdentity || card.evidence));
      table.append(row);
    }
    fold.append(table); box.append(fold);
  }
  function overview(root, project, onTask) {
    root.replaceChildren();
    const wrap = n('div', null, 'archive-view');
    breadcrumb(wrap, [{layer:'projects', id: project.projectId, label: value(project.name, project.projectId)}]);
    const heading = n('div', null, 'title-row'); heading.append(n('h1', value(project.name, project.projectId)), n('span', project.recordType === 'archive' ? '项目资料已登记' : '执行记录', 'badge'));
    wrap.append(heading, n('p', project.goal || '查看已登记任务的执行与交付。', 'project-goal'));
    if (project.emptyState) wrap.append(n('p', project.emptyState.text, 'empty'));
    if (project.registrationNote) wrap.append(n('div', project.registrationNote, 'registration-note'));
    sourcePaths(wrap, project.storageLocations || (project.sourceRoot ? [{label:'项目工作空间',path:project.sourceRoot}] : []));
    if (project.sourceUpdatePolicy) wrap.append(n('p', project.sourceUpdatePolicy, 'panel-note'));
    if (project.currentExecutionStages) wrap.append(n('p', '来源当前阶段：' + project.currentExecutionStages.map(x => x.phase + '（' + (x.state || '未核状态') + '）').join('；'), 'current-source-stage'));
    if (project.recordType === 'archive') {
      wrap.append(n('div', '依据已有任务卡、回执与审核整理；查看材料不代表重新执行任务。', 'registration-note'));
      const facts = n('div', null, 'facts project-facts');
      fact(facts, '当前阶段', project.stage); fact(facts, '项目负责人', project.owner);
      fact(facts, '当前工作', project.currentWork); fact(facts, '下一步目标', project.nextStep);
      wrap.append(facts);
      const stats = n('div', null, 'project-stats');
      const tasks = project.tasks;
      for (const [title, count] of [
        ['已核验事项', tasks.filter(t=>['verified','validated'].includes(t.status)).length],
        ['开发或待核验', tasks.filter(t=>['in_progress','partial','awaiting_review'].includes(t.status)).length],
        ['暂未接通', tasks.filter(t=>t.status==='blocked').length],
        ['后续计划', tasks.filter(t=>t.status==='planned').length],
      ]) { const box = n('div', null, 'stat'); box.append(n('strong', count), n('span', title)); stats.append(box); }
      wrap.append(stats, n('p', '这些数字统计登记事项，不是项目完成百分比。返工和复验归在原任务下，问题单独关联。', 'count-note'));
      const roadmap = sec(wrap, '项目解决路径');
      const path = n('div', null, 'milestone-path');
      project.milestones.forEach((m, i)=>{ const step=n('div',null,'milestone-step'); step.append(n('span', String(i+1).padStart(2,'0'),'step-number'),n('strong',m.title),n('small',m.summary));path.append(step); });
      roadmap.append(path);
      const all = sec(wrap, '项目任务');
      for (const stage of project.milestones) {
        const group = n('div', null, 'task-group');
        group.append(n('h3', stage.title));
        project.tasks.filter(t=>t.milestoneId===stage.id).forEach(t=>group.append(archiveTaskRow(t,onTask)));
        all.append(group);
      }
    } else {
      const box = sec(wrap, '项目任务');
      if (!(project.tasks || []).length) box.append(n('p', project.emptyState ? project.emptyState.text : '这个项目没有任务。', 'empty'));
      (project.tasks || []).forEach(t => box.append(taskRow({...t, member: t.agentLabel || '执行成员', verification: '以宿主回执和审核记录为准', result: t.summary?.fields?.stage || '查看任务详情'}, onTask)));
    }
    sourceCards(wrap, project.sourceCards);
    root.append(wrap);
  }
  function archiveTaskRow(task, onTask) {
    const row = n('article', null, 'archive-task-row');
    const title = n('div', null, 'item-head');
    title.append(button(task.title, () => onTask(task.taskId), 'task-title-link'), badge(task.statusLabel || statusLabels[task.status] || '待核对', task.status));
    if (task.sourceUpdateNote) row.append(n('p', task.sourceUpdateNote, 'source-update-note'));
    if (task.executionFacts) row.append(n('p', '当前执行来源：' + (task.executionFacts.ledgerState || task.executionFacts.declaredState || '身份未齐') + '；开发者：' + (task.executionFacts.developer || task.executionFacts.assignedAgent || '未登记') + '；QA / review 见详情', 'panel-note'));
    row.append(title, n('p', task.result || task.goal), n('small', (kindLabels[task.category] || '任务') + ' · ' + task.member + ' · ' + task.verification));
    return row;
  }
  function materialButton(task, index, text) { return button(text || task.documents[index].name, () => openMaterial(task.taskId, index), 'material-link'); }
  async function openMaterial(taskId, index) {
    const dialog = document.getElementById('material-dialog');
    document.getElementById('material-title').textContent = '正在读取来源材料…';
    document.getElementById('material-source').textContent = '';
    document.getElementById('material-content').textContent = '';
    dialog.showModal();
    try {
      const response = await fetch('/api/materials/' + encodeURIComponent(taskId) + '/' + index, {cache:'no-store'});
      if (!response.ok) throw new Error('材料暂时无法读取，请核对登记文件。');
      const doc = await response.json();
      document.getElementById('material-title').textContent = doc.name;
      document.getElementById('material-source').textContent = '登记时的文件快照 · 来源：' + doc.sourcePath;
      document.getElementById('material-content').textContent = doc.content;
    } catch (error) { document.getElementById('material-content').textContent = error.message; }
  }
  function detail(root, data, onProject) {
    const t = data.task; root.replaceChildren();
    const wrap = n('div', null, 'archive-task');
    breadcrumb(wrap, [{layer:'projects', id: data.projectId, label: value(data.projectName, data.projectId)}, {layer:'tasks', id: t.taskId, label: t.title}], crumb => { if (crumb.layer === 'projects') onProject(crumb.id); });
    const title = n('div', null, 'title-row'); title.append(n('h1', t.title), badge(t.statusLabel || statusLabels[t.status] || '待核对', t.status), recordBadge('historical_archive'));
    wrap.append(title);
    wrap.append(n('div', value(data.milestone.title, '未登记里程碑') + ' · ' + (kindLabels[t.category]||'任务') + ' · 历史资料登记', 'subtitle'));
    plainSummary(wrap, t.plainSummary);
    const facts = n('div', null, 'facts');
    fact(facts, '任务目标', t.goal); fact(facts, '执行成员', t.member);
    fact(facts, '已完成的事', t.result); fact(facts, '检查范围', t.verification);
    fact(facts, '当前问题', t.problem || '该任务未登记未解决问题'); fact(facts, '下一步', t.nextStep);
    wrap.append(facts);
    if (t.sourceUpdateNote) wrap.append(n('p', t.sourceUpdateNote, 'source-update-note'));
    executionFacts(wrap, t.executionFacts);
    sourcePaths(wrap, (t.sourceReferences || []).map(r => ({label:'主要资料 / 工程文档',path:r.sourcePath})), '原项目资料位置');
    const path = sec(wrap, '任务解决路径');
    const list = n('ol', null, 'solution-path'); (t.path||[]).forEach(s=>list.append(n('li',s))); path.append(list);
    const history = sec(wrap, '执行、返工与检查历史');
    if (!(t.history||[]).length) history.append(n('p','尚未派发；这里只登记后续目标。'));
    (t.history||[]).forEach(h=>{const row=n('article',null,'history-entry');row.append(n('strong',h.title),n('p',h.note));
      (h.documentIndexes||[]).forEach(i=>row.append(materialButton(t,i)));history.append(row);});
    const issues = sec(wrap, '问题与待解决事项');
    if (!(data.issues||[]).length) issues.append(n('p','该任务未登记未解决问题；不表示整个项目已完成。'));
    (data.issues||[]).forEach(i=>{const row=n('div',null,'item');row.append(n('strong',i.title),n('p',i.impact),n('small',(issueLabels[i.status]||'待确认')+' · 处理人：'+i.owner),n('p','处理方法：'+i.nextStep));issues.append(row);});
    const docs = sec(wrap, '任务卡与回执材料');
    if (!(t.documents||[]).length) docs.append(n('p','计划项，尚无正式执行卡和交付回执。'));
    const documentList = n('div', null, 'document-list');
    (t.documents||[]).forEach((d,i)=>{const row=n('div',null,'document-row');row.append(n('span',d.kind,'document-kind'),materialButton(t,i));documentList.append(row);});docs.append(documentList);
    technicalDetails(wrap, t.technical, '这里显示登记时的历史快照编号与文件来源；旧卡面状态可能与后续审核不同，当前结论以本任务列出的最新证据为准。未建立新的执行轮次或宿主接收事实。');
    root.append(wrap);
  }
  function issues(root, project, onTask) {
    root.replaceChildren();
    root.append(n('p','问题关联任务，不另算一张开发任务卡。','panel-note'));
    for (const issue of project.keyIssues || []) root.append(issueRow(issue, onTask));
    (project.issues||[]).forEach(i=>{const box=n('article',null,'issue-card');box.append(badge(issueLabels[i.status]||'待确认', i.status),button(i.title,()=>onTask(i.taskId),'task-title-link'),n('p',i.impact),n('small','处理人：'+i.owner),n('p','下一步：'+i.nextStep));root.append(box);});
    if (!(project.issues||[]).length && !(project.keyIssues||[]).length) root.append(n('p','暂无登记的问题。'));
  }
  return {statusLabels, kindLabels, issueLabels, layerLabels, recordTypeLabels, evidenceLabels, gateStateLabels,
          breadcrumb, technicalDetails, plainSummary, unverifiedBox, portfolio, projectDetail, businessLines, workerList, taskDetail, taskRow, filterTasks, overview, detail, issues};
})();
const materialClose = document.getElementById('material-close');
if (materialClose) materialClose.onclick = () => document.getElementById('material-dialog').close();
