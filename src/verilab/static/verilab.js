(() => {
  const language = document.body.dataset.language || 'en';
  const messages = {
    en: {
      executorWorking: 'Executor is working…',
      executorError: 'Executor error',
      auditNote: 'Append an audit note:',
      withdrawalReason: 'Withdrawal reason (history will be preserved):'
    },
    zh: {
      executorWorking: '执行者正在处理……',
      executorError: '执行者错误',
      auditNote: '请添加一条审计备注：',
      withdrawalReason: '请输入撤回原因（历史证据仍会保留）：'
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
  const chart = document.getElementById('metric-chart');
  if (chart) { try { ChartLite.line(chart, JSON.parse(chart.dataset.values)); } catch (_) {} }
})();
