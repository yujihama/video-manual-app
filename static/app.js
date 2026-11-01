'use strict';
const $ = s => document.querySelector(s);
const video = $('#video');
let token = '', job = null, editing = false, dirty = false;
let pollTimer = null, statusInfo = null, pendingAction = null;
const initial = new URLSearchParams(location.search);
let deepTime = initial.has('time') ? Number(initial.get('time')) : null;

function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
function playIcon() {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 12 12'); svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(svg.namespaceURI, 'path');
  path.setAttribute('d', 'M4 2.5 9 6 4 9.5Z'); path.setAttribute('fill', 'currentColor');
  svg.append(path); return svg;
}
function stamp(t) { return `${Math.floor(t / 60).toString().padStart(2, '0')}:${(t % 60).toFixed(1).padStart(4, '0')}`; }
function showError(text) { $('#error').textContent = text; $('#error').classList.toggle('hidden', !text); }
async function api(path, data) {
  const options = data === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json', 'X-App-Token':token}, body:JSON.stringify(data)};
  const r = await fetch(path, options);
  let obj;
  try { obj = await r.json(); } catch { throw new Error('アプリから応答を読み取れませんでした。'); }
  if (!r.ok) throw new Error(obj.error || '処理に失敗しました。');
  return obj;
}
async function refreshStatus() {
  statusInfo = await api('/api/status'); token = statusInfo.token;
  $('#api-status').textContent = statusInfo.api_available ? 'OpenAI 設定済み' : 'OpenAI 未設定';
  $('#budget-summary').textContent = `累計 USD ${statusInfo.budget.spent_usd.toFixed(4)}`;
  return statusInfo;
}
function guardDirty() { return !dirty || confirm('保存していない編集を破棄して移動しますか？'); }
function setDirty() {
  dirty = true; $('#save-state').textContent = '未保存';
  $('#zip-button').disabled = true; $('#word-button').disabled = true;
}
function consent(title, message, action, button) {
  $('#consent-title').textContent = title; $('#consent-message').textContent = message;
  $('#confirm-consent').textContent = button; pendingAction = action; $('#consent').showModal();
}
$('#cancel-consent').onclick = () => { $('#consent').close(); pendingAction = null; };
$('#confirm-consent').onclick = async () => {
  const action = pendingAction; pendingAction = null; $('#consent').close();
  if (!action) return;
  try { await action(); } catch (e) { showError(e.message); }
};
function openUpload() { if (guardDirty()) $('#upload-dialog').showModal(); }
$('#new-button').onclick = openUpload; $('#empty-upload').onclick = openUpload;
$('#close-upload').onclick = $('#cancel-upload').onclick = () => $('#upload-dialog').close();
$('#file').onchange = () => {
  const f = $('#file').files[0]; $('#filename').textContent = f ? f.name : '録画ファイルを選ぶ';
  $('#upload-button').disabled = !f;
};
$('#upload-button').onclick = async () => {
  if (!guardDirty()) return;
  const f = $('#file').files[0]; if (!f) return;
  if (f.size > 300 * 1024 * 1024) { showError('動画は300MB以下で選択してください。'); return; }
  showError(''); $('#upload-dialog').close(); $('#upload-button').disabled = true;
  $('#status-heading').textContent = '動画を読み込んでいます';
  $('#status-message').textContent = 'このPCに動画を保存しています。';
  $('#task-status').classList.remove('hidden'); $('#status-actions').replaceChildren();
  try {
    const r = await fetch('/api/upload?name=' + encodeURIComponent(f.name) + '&interval=' + $('#interval').value, {method:'POST', headers:{'X-App-Token':token, 'Content-Type':'application/octet-stream'}, body:f});
    const j = await r.json(); if (!r.ok) throw new Error(j.error);
    editing = false; dirty = false; await openJob(j.id);
  } catch (e) { showError(e.message); $('#task-status').classList.add('hidden'); }
  finally { $('#upload-button').disabled = false; }
};
$('#sample-button').onclick = async () => {
  if (!guardDirty()) return;
  try { showError(''); const r = await api('/api/sample', {}); editing = false; dirty = false; await openJob(r.id); await refreshStatus(); }
  catch (e) { showError(e.message); }
};
async function openJob(id) {
  if (pollTimer) clearTimeout(pollTimer); pollTimer = null;
  const changed = job?.id !== id; job = await api('/api/jobs/' + id);
  history.replaceState(null, '', '/?job=' + id);
  if (changed) { video.pause(); video.removeAttribute('src'); video.load(); $('#now-step').textContent = '手順の時刻から再生できます'; }
  renderStatus();
  if (['ready','error','blocked'].includes(job.phase)) refreshStatus().catch(() => {});
  if (job.video_file && (!video.getAttribute('src') || changed)) {
    video.src = '/api/jobs/' + job.id + '/video';
    const poster = job.manual?.steps?.[0]?.images?.at(-1)?.id || job.frames?.[Math.floor(job.frames.length / 2)]?.id;
    if (poster) video.poster = `/api/jobs/${job.id}/frame?id=${poster}`;
    $('#video-name').textContent = job.name; $('#video-name').title = job.name; video.load();
  }
  if (job.phase === 'ready') renderManual();
  else if (job.video_file) renderPreview();
  else { $('#workspace').classList.add('hidden'); $('#empty').classList.remove('hidden'); $('#export-bar').classList.add('hidden'); }
  if (['queued','extracting','estimating','generating'].includes(job.phase)) pollTimer = setTimeout(() => openJob(id).catch(e => showError(e.message)), 800);
}
function renderPreview() {
  $('#empty').classList.add('hidden'); $('#workspace').classList.remove('hidden');
  $('#sample-badge').classList.add('hidden'); $('#sample-explanation').classList.add('hidden');
  $('#edit-button').disabled = true; $('#export-bar').classList.add('hidden');
  document.querySelector('.document-meta').classList.add('hidden'); document.querySelector('.document-footnote').classList.add('hidden');
  $('#manual-heading').replaceChildren(node('h2', '手順書を作成', 'manual-title'));
  $('#steps').replaceChildren(node('p', '動画を確認し、上の「生成の準備」から進めてください。画像の送信後に最大費用を表示します。', 'preview-text'));
  $('#questions').classList.add('hidden'); $('#step-nav').replaceChildren();
  $('#step-count').textContent = ''; $('#manual-count').textContent = '';
}
function renderStatus() {
  const phases = {uploaded:'動画を読み込みました', queued:'準備しています', extracting:'画面を抽出しています', prepared:'動画の準備ができました', estimating:'生成費用を確認しています', awaiting_generation:'生成費用を確認してください', generating:'手順書を作成しています', ready:'手順書を作成しました', blocked:'根拠の確認が必要です', error:'処理を完了できませんでした'};
  $('#task-status').classList.toggle('hidden', job.phase === 'ready');
  $('#status-heading').textContent = phases[job.phase] || job.phase;
  $('#status-message').textContent = job.message;
  $('#progress-dot').classList.toggle('busy', ['queued','extracting','estimating','generating'].includes(job.phase));
  const actions = $('#status-actions'); actions.replaceChildren();
  if (job.phase === 'prepared') {
    const b = node('button', '生成の準備', 'button primary');
    b.onclick = () => consent('画面画像をOpenAIへ送信します', `${job.frames_count}枚の画像を送り、生成の最大費用を確認します。金額を確認した後、手順書の生成を開始できます。`, async () => { await api('/api/jobs/' + job.id + '/estimate', {}); await openJob(job.id); }, '送信して費用を確認');
    actions.append(b);
  }
  if (job.phase === 'awaiting_generation') {
    const b = node('button', `最大 USD ${job.estimate_usd.toFixed(3)} で生成`, 'button primary');
    b.onclick = () => consent('手順書を生成します', `最大費用：USD ${job.estimate_usd.toFixed(3)}\n${job.frames_count}枚の画面画像から下書きを作成します。`, async () => { await api('/api/jobs/' + job.id + '/generate', {}); await openJob(job.id); }, '生成する');
    actions.append(b);
  }
  if (job.phase === 'error' || job.phase === 'blocked') showError(job.message);
}
function jump(t, step, reveal = false) {
  if (!Number.isFinite(t)) return;
  const go = () => { video.currentTime = Math.max(0, Math.min(t, Number.isFinite(video.duration) ? video.duration : t)); video.play().catch(() => {}); };
  if (video.readyState >= 1) go(); else video.addEventListener('loadedmetadata', go, {once:true});
  $('#now-step').textContent = step ? step.title : `参照位置 ${stamp(t)}`;
  if (window.innerWidth < 801) video.scrollIntoView({behavior:'auto', block:'start'});
  else if (reveal && step) {
    const el = [...document.querySelectorAll('.step')].find(x => x.dataset.stepId === step.id);
    el?.scrollIntoView({behavior:'auto', block:'nearest'});
  }
  history.replaceState(null, '', `/?job=${job.id}&time=${t}` + (step ? '&step=' + encodeURIComponent(step.id) : ''));
}
function editable(key, value, step, title) {
  const label = node('label', title, 'edit-field');
  const input = node(key === 'text' || key === 'uncertainty' ? 'textarea' : 'input');
  input.value = value; input.dataset.step = step.id; input.dataset.key = key;
  input.addEventListener('input', setDirty); label.append(input); return label;
}
function renderNavigation(m) {
  const nav = $('#step-nav'); nav.replaceChildren(); $('#step-count').textContent = `${m.steps.length}件`;
  m.steps.forEach((s, n) => {
    const b = node('button', undefined, 'chapter'); b.dataset.stepId = s.id;
    b.setAttribute('aria-label', `手順${n + 1} ${s.title}を動画で確認`);
    b.append(node('span', stamp(s.start), 'chapter-time'), node('span', s.title, 'chapter-title'));
    b.onclick = () => jump(s.start, s, true); nav.append(b);
  });
}
function renderEvidence(s) {
  const details = node('details', undefined, 'evidence');
  details.append(node('summary', `参照画像 ${s.images.length}枚`));
  const images = node('div', undefined, 'images');
  s.images.forEach(im => {
    const figure = node('figure'); const b = node('button', undefined, 'image-button');
    b.setAttribute('aria-label', `${stamp(im.time)}の参照画像から動画を再生`);
    const image = node('img'); image.src = `/api/jobs/${job.id}/frame?id=${im.id}`;
    image.alt = `${s.title}・${stamp(im.time)}の画面`; image.loading = 'lazy';
    b.append(image); b.onclick = () => jump(im.time, s);
    const caption = node('figcaption'); const t = node('button', stamp(im.time), 'image-time');
    t.onclick = () => jump(im.time, s); caption.append(t); figure.append(b, caption); images.append(figure);
  });
  details.append(images); return details;
}
function renderManual() {
  const m = job.manual;
  document.querySelector('.document-meta').classList.remove('hidden'); document.querySelector('.document-footnote').classList.remove('hidden');
  $('#empty').classList.add('hidden'); $('#workspace').classList.remove('hidden');
  $('#edit-button').disabled = false; $('#export-bar').classList.remove('hidden'); $('#questions').classList.remove('hidden');
  $('#sample-badge').classList.toggle('hidden', !job.sample); $('#sample-explanation').classList.toggle('hidden', !job.sample);
  $('#edit-button').textContent = editing ? '編集を閉じる' : '内容を編集';
  $('#save-button').classList.toggle('hidden', !editing);
  $('#save-state').textContent = dirty ? '未保存' : m.edited ? '保存済み' : '';
  $('#manual-count').textContent = `${m.steps.length}手順`;
  $('#zip-button').disabled = dirty; $('#word-button').disabled = dirty;
  const heading = $('#manual-heading'); heading.replaceChildren();
  if (editing) {
    const title = node('input'); title.className = 'title-input'; title.id = 'title-edit'; title.value = m.title;
    title.setAttribute('aria-label', '手順書の題名'); title.oninput = setDirty; heading.append(title);
  } else heading.append(node('h2', m.title, 'manual-title'));
  const container = $('#steps'); container.replaceChildren();
  m.steps.forEach((s, n) => {
    const section = node('section', undefined, 'step'); section.dataset.stepId = s.id;
    const head = node('div', undefined, 'step-head');
    head.append(node('span', `${n + 1}.`, 'step-number'), node('h3', s.title)); section.append(head);
    const b = node('button', undefined, 'jump'); b.dataset.jump = String(s.start);
    b.append(playIcon(), node('span', `${stamp(s.start)}–${stamp(s.end)}`));
    b.setAttribute('aria-label', `${s.title}の動画を${stamp(s.start)}から確認`); b.onclick = () => jump(s.start, s); section.append(b);
    if (editing) {
      for (const [key, title] of [['title','見出し'],['text','手順の説明'],['target','操作対象'],['value','値'],['uncertainty','要確認事項']]) section.append(editable(key, s[key], s, title));
    } else {
      section.append(node('p', s.text, 'step-body'));
      const dl = node('dl');
      for (const [label, value] of [['対象',s.target],['値',s.value]]) { dl.append(node('dt', label), node('dd', value || '—')); }
      section.append(dl);
      if (s.inferred) section.append(node('p', '画面の変化から推定', 'inference'));
      if (s.uncertainty) {
        const notes = node('details', undefined, 'notes');
        notes.append(node('summary', '操作の確認事項'), node('p', s.uncertainty)); section.append(notes);
      }
    }
    section.append(renderEvidence(s)); container.append(section);
  });
  const q = $('#questions'); q.replaceChildren(node('h3', '未確認事項'));
  if (m.questions.length) { const ul = node('ul'); m.questions.forEach(x => ul.append(node('li', x))); q.append(ul); }
  else q.append(node('p', '抽出された未確認事項はありません。内容の確認と承認は別途必要です。'));
  if (m.held_events.length) q.append(node('p', `${m.held_events.length}件の不明なイベントは本文から保留しています。`));
  renderNavigation(m);
  if (deepTime !== null) { const t = deepTime; deepTime = null; jump(t, m.steps.find(x => x.id === initial.get('step'))); }
  updateTime();
}
function collectEdits() {
  const m = JSON.parse(JSON.stringify(job.manual)); m.title = $('#title-edit').value;
  document.querySelectorAll('[data-key]').forEach(el => { m.steps.find(s => s.id === el.dataset.step)[el.dataset.key] = el.value; });
  return m;
}
$('#edit-button').onclick = () => {
  if (editing && dirty && !confirm('保存していない編集を破棄しますか？')) return;
  editing = !editing; dirty = false; renderManual();
};
$('#save-button').onclick = async () => {
  try { job = await api('/api/jobs/' + job.id + '/save', collectEdits()); dirty = false; renderManual(); }
  catch (e) { showError(e.message); }
};
async function download(format) {
  if (!job || dirty) return;
  const b = format === 'docx' ? $('#word-button') : $('#zip-button');
  const old = b.textContent; b.disabled = true; b.textContent = '準備中…';
  try {
    const r = await fetch(`/api/jobs/${job.id}/export?format=${format}`);
    if (!r.ok) { const x = await r.json(); throw new Error(x.error); }
    const blob = await r.blob(); const url = URL.createObjectURL(blob); const a = node('a');
    a.href = url; a.download = format === 'docx' ? '手順書.docx' : '手順書と動画.zip';
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000);
  } catch (e) { showError(e.message); }
  finally { b.textContent = old; b.disabled = false; }
}
$('#zip-button').onclick = () => download('zip'); $('#word-button').onclick = () => download('docx');
function updateTime() {
  const t = video.currentTime || 0; $('#video-position').textContent = stamp(t);
  $('#video-duration').textContent = Number.isFinite(video.duration) ? ' / ' + stamp(video.duration) : '';
  if (!job?.manual) return;
  const matches = job.manual.steps.filter(s => t >= s.start - .1 && t <= s.end + .25);
  const current = matches[matches.length - 1];
  document.querySelectorAll('.step,.chapter').forEach(el => {
    const active = el.dataset.stepId === current?.id; el.classList.toggle('active', active);
    if (el.classList.contains('chapter')) active ? el.setAttribute('aria-current','step') : el.removeAttribute('aria-current');
  });
  if (current) $('#now-step').textContent = current.title;
}
video.addEventListener('timeupdate', updateTime); video.addEventListener('loadedmetadata', updateTime);
$('#history-button').onclick = async () => {
  try {
    const status = await refreshStatus(); const items = $('#history-items'); items.replaceChildren();
    if (!status.jobs.length) items.append(node('p', '作成履歴はまだありません。'));
    for (const j of status.jobs) {
      const b = node('button', j.name, 'history-item');
      const date = new Intl.DateTimeFormat('ja-JP', {timeZone:'Asia/Tokyo', month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit'}).format(new Date(j.updated));
      b.append(node('small', `${date} · ${j.sample ? 'サンプル' : j.phase === 'ready' ? '下書き作成済み' : j.phase === 'error' ? '処理エラー' : '準備・処理中'}`));
      b.onclick = async () => { if (!guardDirty()) return; $('#history').close(); editing = false; dirty = false; showError(''); await openJob(j.id); };
      items.append(b);
    }
    $('#history').showModal();
  } catch (e) { showError(e.message); }
};
$('#close-history').onclick = () => $('#history').close();
window.addEventListener('beforeunload', e => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });
(async () => { try { await refreshStatus(); if (initial.get('job')) await openJob(initial.get('job')); } catch (e) { showError(e.message); } })();
