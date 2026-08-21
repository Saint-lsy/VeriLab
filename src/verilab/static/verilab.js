(() => {
  const language = document.body.dataset.language || 'en';
  const messages = {
    en: {
      executorWorking: 'Executor is working…',
      executorError: 'Executor error',
      auditNote: 'Append an audit note:',
      withdrawalReason: 'Withdrawal reason (history will be preserved):',
      lineageRoot: 'Root experiment',
      lineageDerived: 'Derived experiment',
      lineageRootDescription: 'This experiment has no declared parent.',
      lineageParent: 'Parent',
      lineageCommit: 'Commit change',
      lineageScore: 'Primary metric',
      lineageNoScore: 'No trusted score yet',
      lineageSpecChanges: 'Experiment specification changes',
      lineageGitChanges: 'Versioned file changes',
      lineageNoSpecChanges: 'No scientific spec fields changed.',
      lineageNoGitChanges: 'No versioned file changes between commits.',
      lineageNarrative: 'Human-readable conclusion',
      lineageNarrativePending: 'No reviewer narrative has been recorded yet.',
      lineageKeyChanges: 'What changed',
      lineageExpected: 'Expected effect',
      lineageObserved: 'Observed result',
      lineageTechnical: 'View detailed specification and code evidence',
      lineageWithdrawn: 'Withdrawn',
      lineageWithdrawalReason: 'Withdrawal note',
      lineageOpen: 'Open experiment details →',
      lineageBefore: 'Parent',
      lineageAfter: 'Current',
      lineageFields: 'fields',
      lineageFiles: 'files',
      statusLabels: {},
      fieldLabels: {}
    },
    zh: {
      executorWorking: '执行者正在处理……',
      executorError: '执行者错误',
      auditNote: '请添加一条审计备注：',
      withdrawalReason: '请输入撤回原因（历史证据仍会保留）：',
      lineageRoot: '根实验',
      lineageDerived: '衍生实验',
      lineageRootDescription: '该实验没有声明父实验，是当前谱系的起点。',
      lineageParent: '父实验',
      lineageCommit: '提交变化',
      lineageScore: '主要指标',
      lineageNoScore: '尚无可信分数',
      lineageSpecChanges: '实验规格变化',
      lineageGitChanges: '版本文件变化',
      lineageNoSpecChanges: '科学实验规格没有变化。',
      lineageNoGitChanges: '两个提交之间没有版本文件变化。',
      lineageNarrative: '人类可读的实验结论',
      lineageNarrativePending: '尚未记录审核者生成的自然语言说明。',
      lineageKeyChanges: '主要变化',
      lineageExpected: '预期作用',
      lineageObserved: '实际结果',
      lineageTechnical: '查看详细规格和代码证据',
      lineageWithdrawn: '已撤回',
      lineageWithdrawalReason: '撤回备注',
      lineageOpen: '打开完整实验详情 →',
      lineageBefore: '父实验',
      lineageAfter: '当前实验',
      lineageFields: '个字段',
      lineageFiles: '个文件',
      statusLabels: {
        DRAFT: '草稿', QUEUED: '排队中', RUNNING: '运行中', GRADING: '评分中',
        REVIEW_PENDING: '待审核', ACCEPTED: '已通过', REJECTED: '已拒绝',
        FAILED: '失败', CANCELLED: '已取消', VERIFICATION_FAILED: '验证失败',
        REVIEW_BLOCKED: '审核受阻', NEEDS_HUMAN: '需人工处理', ORPHANED: '进程失联'
      },
      fieldLabels: {
        hypothesis: '实验假设', command: '执行命令', cwd: '工作目录', env: '环境变量',
        protocol_id: '实验协议', expected_artifacts: '预期产物',
        resource_claim: '资源声明', metadata: '实验元数据'
      }
    }
  }[language] || {};
  const csrf = () => document.cookie.split('; ').find(v => v.startsWith('verilab_csrf='))?.split('=')[1] || '';
  document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => {
    const name = button.dataset.tab;
    document.querySelectorAll('[data-tab]').forEach(item => item.classList.toggle('active', item === button));
    document.querySelectorAll('[data-tab-panel]').forEach(panel => { panel.hidden = panel.dataset.tabPanel !== name; });
  }));
  const form = document.getElementById('chat-form');
  if (form) form.addEventListener('submit', async event => {
    event.preventDefault();
    const textarea = form.querySelector('textarea');
    const log = document.getElementById('chat-log');
    const text = textarea.value.trim(); if (!text) return;
    const user = document.createElement('div'); user.className = 'chat user'; user.textContent = text; log.append(user);
    textarea.value = ''; textarea.disabled = true;
    const pending = document.createElement('div'); pending.className = 'chat system'; pending.textContent = messages.executorWorking; log.append(pending); log.scrollTop = log.scrollHeight;
    try {
      const response = await fetch('/api/chat/messages', {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(csrf())}, body:JSON.stringify({message:text})});
      const data = await response.json(); if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      pending.className = 'chat assistant'; pending.textContent = data.message;
    } catch (error) { pending.className = 'chat assistant'; pending.textContent = `${messages.executorError}: ${error.message}`; }
    finally { textarea.disabled = false; textarea.focus(); log.scrollTop = log.scrollHeight; }
  });
  if (window.EventSource) {
    const stream = new EventSource('/api/events');
    stream.onmessage = () => {};
  }
  const post = async (url, body = {}) => {
    const response = await fetch(url, {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(csrf())},body:JSON.stringify(body)});
    const data = await response.json(); if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`); return data;
  };
  document.querySelectorAll('[data-api-action]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true; try { await post(button.dataset.apiAction); location.reload(); } catch (error) { alert(error.message); button.disabled = false; }
  }));
  document.querySelectorAll('[data-note-experiment]').forEach(button => button.addEventListener('click', async () => {
    const note = prompt(messages.auditNote); if (!note) return;
    try { await post(`/api/experiments/${button.dataset.noteExperiment}/notes`, {note}); location.reload(); } catch (error) { alert(error.message); }
  }));
  document.querySelectorAll('[data-withdraw-experiment]').forEach(button => button.addEventListener('click', async () => {
    const reason = prompt(messages.withdrawalReason); if (!reason) return;
    try { await post(`/api/experiments/${button.dataset.withdrawExperiment}/withdraw`, {reason}); location.reload(); } catch (error) { alert(error.message); }
  }));
  const lineageRoot = document.getElementById('experiment-lineage');
  if (lineageRoot) {
    const lineageFallback = document.getElementById('lineage-fallback');
    const payload = JSON.parse(lineageRoot.dataset.lineage || '{"nodes":[]}');
    const nodes = payload.nodes || [];
    const byId = new Map(nodes.map(node => [node.id, node]));
    const children = new Map(nodes.map(node => [node.id, []]));
    nodes.forEach(node => {
      if (node.parent_experiment_id && children.has(node.parent_experiment_id)) {
        children.get(node.parent_experiment_id).push(node);
      }
    });
    children.forEach(items => items.sort((a, b) => a.created_at.localeCompare(b.created_at)));

    const nodeWidth = 220;
    const nodeHeight = 82;
    const columnGap = 88;
    const rowStep = 100;
    const padding = 24;
    const positions = new Map();
    let leafIndex = 0;
    let maxDepth = 0;
    const place = (node, depth, trail = new Set()) => {
      if (positions.has(node.id)) return positions.get(node.id).centerY;
      const nextTrail = new Set(trail); nextTrail.add(node.id);
      const descendants = (children.get(node.id) || []).filter(item => !nextTrail.has(item.id));
      let centerY;
      if (descendants.length) {
        const childCenters = descendants.map(item => place(item, depth + 1, nextTrail));
        centerY = (childCenters[0] + childCenters[childCenters.length - 1]) / 2;
      } else {
        centerY = padding + nodeHeight / 2 + leafIndex * rowStep;
        leafIndex += 1;
      }
      maxDepth = Math.max(maxDepth, depth);
      positions.set(node.id, {
        x: padding + depth * (nodeWidth + columnGap),
        y: centerY - nodeHeight / 2,
        centerY
      });
      return centerY;
    };
    const roots = nodes.filter(node => !node.parent_experiment_id || !byId.has(node.parent_experiment_id));
    roots.forEach((node, index) => { if (index) leafIndex += 0.5; place(node, 0); });
    nodes.filter(node => !positions.has(node.id)).forEach(node => place(node, 0));

    const canvasWidth = Math.max(520, padding * 2 + (maxDepth + 1) * nodeWidth + maxDepth * columnGap);
    const canvasHeight = Math.max(180, padding * 2 + nodeHeight + Math.max(0, leafIndex - 1) * rowStep);
    lineageRoot.style.display = 'block';
    lineageRoot.style.width = `${canvasWidth}px`;
    lineageRoot.style.height = `${canvasHeight}px`;
    const svg = lineageRoot.querySelector('.lineage-edges');
    svg.setAttribute('viewBox', `0 0 ${canvasWidth} ${canvasHeight}`);
    const edgeLayer = svg.querySelector('g');
    const nodeLayer = lineageRoot.querySelector('.lineage-node-layer');
    const inspector = document.getElementById('lineage-inspector');
    const make = (tag, className, text) => {
      const element = document.createElement(tag);
      if (className) element.className = className;
      if (text !== undefined) element.textContent = text;
      return element;
    };
    const statusLabel = status => messages.statusLabels[status] || status;
    const nodeStatusLabel = node => node.withdrawn ? messages.lineageWithdrawn : statusLabel(node.status);
    const fieldLabel = field => messages.fieldLabels[field] || field.replaceAll('_', ' ');
    const formatValue = value => {
      const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
      return text.length > 900 ? `${text.slice(0, 900)}…` : text;
    };
    const formatDelta = value => `${value >= 0 ? '+' : ''}${value.toFixed(6)}`;

    nodes.forEach(node => {
      if (!node.parent_experiment_id || !positions.has(node.parent_experiment_id)) return;
      const parent = positions.get(node.parent_experiment_id);
      const current = positions.get(node.id);
      const startX = parent.x + nodeWidth;
      const endX = current.x;
      const bend = (startX + endX) / 2;
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', `M ${startX} ${parent.centerY} C ${bend} ${parent.centerY}, ${bend} ${current.centerY}, ${endX} ${current.centerY}`);
      path.setAttribute('marker-end', 'url(#lineage-arrow)');
      edgeLayer.append(path);
    });

    const renderInspector = node => {
      inspector.replaceChildren();
      const parent = node.parent_experiment_id ? byId.get(node.parent_experiment_id) : null;
      inspector.append(make('p', 'eyebrow', parent ? messages.lineageDerived : messages.lineageRoot));
      inspector.append(make('h3', '', node.title));
      const status = make('span', `status ${node.withdrawn ? 'withdrawn' : node.status.toLowerCase()}`, nodeStatusLabel(node));
      inspector.append(status);
      if (node.withdrawn && node.note) {
        inspector.append(make('p', 'lineage-withdrawal', `${messages.lineageWithdrawalReason}: ${node.note}`));
      }
      if (parent) {
        const parentLine = make('p', 'lineage-parent');
        parentLine.append(`${messages.lineageParent}: `);
        const parentLink = make('a', '', parent.title);
        parentLink.href = `/experiments/${parent.id}`;
        parentLine.append(parentLink);
        inspector.append(parentLine);
      } else {
        inspector.append(make('p', 'lineage-muted', messages.lineageRootDescription));
      }
      const score = make('div', 'lineage-score');
      score.append(make('span', '', messages.lineageScore));
      score.append(make('strong', '', node.score === null ? messages.lineageNoScore : Number(node.score).toFixed(6)));
      if (node.parent_delta !== null) score.append(make('small', node.parent_delta >= 0 ? 'positive' : 'negative', formatDelta(Number(node.parent_delta))));
      inspector.append(score);
      if (parent) {
        const commit = make('p', 'lineage-commit');
        commit.append(`${messages.lineageCommit}: `);
        commit.append(make('code', '', `${parent.git_commit.slice(0, 9)} → ${node.git_commit.slice(0, 9)}`));
        inspector.append(commit);
      }

      inspector.append(make('h4', '', messages.lineageNarrative));
      if (node.change_summary) {
        inspector.append(make('h3', 'lineage-narrative-headline', node.change_summary.headline));
        inspector.append(make('p', 'lineage-narrative-summary', node.change_summary.summary));
        const keyChanges = make('div', 'lineage-narrative-block');
        keyChanges.append(make('b', '', messages.lineageKeyChanges));
        const keyChangeList = make('ul');
        node.change_summary.key_changes.forEach(change => keyChangeList.append(make('li', '', change)));
        keyChanges.append(keyChangeList);
        inspector.append(keyChanges);
        const effects = make('div', 'lineage-effects');
        const expected = make('div');
        expected.append(make('b', '', messages.lineageExpected));
        expected.append(make('p', '', node.change_summary.expected_effect));
        const observed = make('div');
        observed.append(make('b', '', messages.lineageObserved));
        observed.append(make('p', '', node.change_summary.observed_effect));
        effects.append(expected, observed);
        inspector.append(effects);
      } else {
        inspector.append(make('p', 'lineage-muted', messages.lineageNarrativePending));
      }

      if (parent) {
        const technical = make('details', 'lineage-technical');
        technical.append(make('summary', '', messages.lineageTechnical));
        const technicalBody = make('div', 'lineage-technical-body');
        technicalBody.append(make('h4', '', messages.lineageSpecChanges));
        if (!node.spec_changes.length) technicalBody.append(make('p', 'lineage-muted', messages.lineageNoSpecChanges));
        node.spec_changes.forEach(change => {
          const details = make('details', 'lineage-change');
          details.append(make('summary', '', fieldLabel(change.field)));
          const values = make('div', 'lineage-change-values');
          const before = make('div'); before.append(make('b', '', messages.lineageBefore)); before.append(make('code', '', formatValue(change.parent)));
          const after = make('div'); after.append(make('b', '', messages.lineageAfter)); after.append(make('code', '', formatValue(change.current)));
          values.append(before, after); details.append(values); technicalBody.append(details);
        });
        technicalBody.append(make('h4', '', `${messages.lineageGitChanges} (${node.git_change_count})`));
        if (!node.git_changes.length) technicalBody.append(make('p', 'lineage-muted', messages.lineageNoGitChanges));
        if (node.git_changes.length) {
          const files = make('ul', 'lineage-files');
          node.git_changes.forEach(change => {
            const item = make('li');
            item.append(make('b', '', change.status));
            item.append(make('code', '', change.old_path ? `${change.old_path} → ${change.path}` : change.path));
            files.append(item);
          });
          technicalBody.append(files);
        }
        technical.append(technicalBody);
        inspector.append(technical);
      }
      const link = make('a', 'button secondary lineage-open', messages.lineageOpen);
      link.href = `/experiments/${node.id}`;
      inspector.append(link);
    };

    const buttons = new Map();
    nodes.forEach(node => {
      const position = positions.get(node.id);
      const button = make('button', `lineage-node ${node.status.toLowerCase()}${node.withdrawn ? ' withdrawn' : ''}`);
      button.type = 'button';
      button.style.left = `${position.x}px`; button.style.top = `${position.y}px`;
      button.setAttribute('aria-label', `${node.title}, ${nodeStatusLabel(node)}`);
      button.append(make('b', '', node.title));
      const metric = node.score === null ? messages.lineageNoScore : `${payload.primary_metric} ${Number(node.score).toFixed(6)}`;
      button.append(make('span', '', metric));
      if (node.parent_experiment_id) {
        const changeText = `${node.spec_changes.length} ${messages.lineageFields} · ${node.git_change_count} ${messages.lineageFiles}`;
        button.append(make('small', '', `${nodeStatusLabel(node)} · ${changeText}`));
      } else {
        button.append(make('small', '', `${nodeStatusLabel(node)} · ${messages.lineageRoot}`));
      }
      button.addEventListener('click', () => {
        buttons.forEach(item => item.classList.remove('selected'));
        button.classList.add('selected');
        renderInspector(node);
      });
      buttons.set(node.id, button); nodeLayer.append(button);
    });
    if (nodes.length) buttons.get(nodes[nodes.length - 1].id).click();
    if (lineageFallback && buttons.size === nodes.length) lineageFallback.hidden = true;
  }
  const chart = document.getElementById('metric-chart');
  if (chart) { try { ChartLite.line(chart, JSON.parse(chart.dataset.values)); } catch (_) {} }
})();
