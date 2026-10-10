'use strict';
(() => {
  // При изменении слов или кода — увеличить, чтобы Telegram не отдавал старое из кеша
  const VER = '1';
  const COLORS = ['#ff4fa3', '#ffb547', '#2aabee', '#3ddc97', '#b388ff', '#ff7a45'];
  const NAMES = ['Огненные лисы', 'Трезвые не будем', 'Третьи лишние', 'Ночные совы', 'Без тормозов', 'Шестое чувство'];
  const TIMES = [30, 60, 90, 120];
  const TARGETS = [10, 20, 30, 50, 75, 100];
  const DIFFS = [[0, 'Все'], [1, 'Лёгкие'], [2, 'Средние'], [3, 'Сложные']];
  const RING = 2 * Math.PI * 44;

  const tg = window.Telegram && window.Telegram.WebApp;
  const inTelegram = !!(tg && tg.initData);

  const store = {
    get(k, d) { try { const v = localStorage.getItem('kroko.' + k); return v ? JSON.parse(v) : d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem('kroko.' + k, JSON.stringify(v)); } catch { /* приватный режим */ } },
    del(k) { try { localStorage.removeItem('kroko.' + k); } catch { /* приватный режим */ } },
  };

  const app = document.getElementById('app');
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  let CATS = [];
  let settings = Object.assign({
    teams: [NAMES[0], NAMES[1]], time: 60, target: 30, cats: null, diff: 0,
    penalty: true, lastWord: true, sound: true,
  }, store.get('settings', {}));
  let custom = store.get('custom', []);
  let game = store.get('game', null);
  let screen = 'loading';
  let setupError = '';
  let timer = null, lastBeep = -1, lastSaved = -1, audio = null, wakeLock = null;

  // ---------- слова ----------
  async function loadWords() {
    const idx = await (await fetch(`words/index.json?v=${VER}`)).json();
    CATS = await Promise.all(idx.map(async c => ({
      id: c.id, name: c.name,
      words: await (await fetch(`words/${c.id}.json?v=${VER}`)).json(),
    })));
  }
  function allCats() {
    return custom.length ? CATS.concat({ id: 'custom', name: 'Свои слова', words: custom.map(t => ({ t, d: 2 })) }) : CATS;
  }
  function selectedCats() {
    const ids = allCats().map(c => c.id);
    return settings.cats ? settings.cats.filter(id => ids.includes(id)) : ids;
  }
  function pool(cats, diff, used) {
    const out = [];
    for (const c of allCats()) {
      if (!cats.includes(c.id)) continue;
      for (const w of c.words) {
        const k = c.id + '|' + w.t;
        if ((!diff || w.d === diff) && !(used && used.includes(k))) out.push({ t: w.t, c: c.name, k });
      }
    }
    return out;
  }
  function nextWord() {
    let p = pool(game.cats, game.diff, game.used);
    if (!p.length) { game.used = []; p = pool(game.cats, game.diff, null); }
    if (!p.length) { game.word = null; return; }
    const w = p[Math.floor(Math.random() * p.length)];
    game.used.push(w.k);
    game.word = { t: w.t, c: w.c };
    game.shownAt = Date.now();
  }

  // ---------- звук, вибрация, экран ----------
  function beep(freq, dur) {
    if (!settings.sound || !audio) return;
    try {
      const o = audio.createOscillator(), g = audio.createGain();
      o.frequency.value = freq; o.type = 'sine';
      g.gain.setValueAtTime(.18, audio.currentTime);
      g.gain.exponentialRampToValueAtTime(.001, audio.currentTime + dur);
      o.connect(g).connect(audio.destination);
      o.start(); o.stop(audio.currentTime + dur);
    } catch { /* без звука */ }
  }
  function haptic(kind) {
    try {
      if (inTelegram && tg.HapticFeedback) {
        if (['success', 'warning', 'error'].includes(kind)) tg.HapticFeedback.notificationOccurred(kind);
        else tg.HapticFeedback.impactOccurred(kind);
        return;
      }
    } catch { /* старый клиент */ }
    try { navigator.vibrate && navigator.vibrate(kind === 'success' ? 40 : kind === 'warning' ? [80, 60, 80] : 15); } catch { /* нет вибро */ }
  }
  async function keepAwake(on) {
    try {
      if (on && !wakeLock && navigator.wakeLock) wakeLock = await navigator.wakeLock.request('screen');
      if (!on && wakeLock) { await wakeLock.release(); wakeLock = null; }
    } catch { wakeLock = null; }
  }

  // ---------- состояние ----------
  function save() { store.set('game', game); }
  function saveSettings() { store.set('settings', settings); }
  function go(s) {
    screen = s;
    if (game && ['ready', 'turn', 'last', 'review', 'score', 'win'].includes(s)) { game.phase = s; save(); }
    render();
    window.scrollTo(0, 0);
  }

  function newGame() {
    const cats = selectedCats();
    if (!cats.length) { setupError = 'Выберите хотя бы одну категорию.'; return render(); }
    if (!pool(cats, settings.diff, null).length) { setupError = 'В выбранных категориях нет слов такой сложности.'; return render(); }
    setupError = '';
    game = {
      teams: settings.teams.map((n, i) => ({ name: n.trim() || `Команда ${i + 1}`, color: COLORS[i], score: 0 })),
      cats, diff: settings.diff, time: settings.time, target: settings.target,
      penalty: settings.penalty, lastWord: settings.lastWord,
      turn: 0, round: 1, turns: 0, used: [], log: [], word: null, left: 0,
      paused: false, lastTeam: null, best: null, winner: null, phase: 'ready',
    };
    closingGuard(true);
    go('ready');
  }

  function startTurn() {
    game.log = []; game.lastTeam = null; game.left = game.time * 1000;
    nextWord();
    if (!game.word) return go('review');
    game.paused = false;
    go('turn');
    run();
  }
  function run() {
    stop();
    game.paused = false;
    game.endsAt = Date.now() + game.left;
    lastBeep = -1;
    timer = setInterval(tick, 200);
    keepAwake(true);
    tick();
  }
  function stop() { if (timer) { clearInterval(timer); timer = null; } }
  function tick() {
    game.left = Math.max(0, game.endsAt - Date.now());
    const s = Math.ceil(game.left / 1000);
    paintTimer(s);
    if (s !== lastSaved) { lastSaved = s; save(); }
    if (s <= 10 && s > 0 && s !== lastBeep) { lastBeep = s; beep(s <= 3 ? 990 : 740, .07); haptic('light'); }
    if (game.left <= 0) timeUp();
  }
  function paintTimer(s) {
    const t = document.getElementById('sec'), r = document.getElementById('ring');
    if (!t || !r) return;
    t.textContent = Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
    r.setAttribute('stroke-dashoffset', String(RING * (1 - game.left / (game.time * 1000))));
    r.setAttribute('stroke', s <= 10 ? 'var(--bad)' : 'var(--pink)');
  }
  function pause() {
    if (screen !== 'turn' || game.paused) return;
    stop();
    game.left = Math.max(0, game.endsAt - Date.now());
    game.paused = true;
    keepAwake(false);
    save();
    render();
  }
  function timeUp() {
    stop(); keepAwake(false);
    beep(330, .6); haptic('warning');
    go(game.lastWord && game.word ? 'last' : 'review');
  }
  function mark(ok) {
    if (!game.word || game.paused) return;
    game.log.push({ t: game.word.t, ok, ms: Date.now() - game.shownAt });
    if (ok) { haptic('success'); beep(660, .09); } else haptic('soft');
    nextWord();
    save();
    if (!game.word) return timeUp();
    render();
  }

  function turnPoints() {
    const hits = game.log.filter(x => x.ok).length;
    const skips = game.log.length - hits;
    return hits - (game.penalty ? skips : 0) + (game.lastTeam === game.turn ? 1 : 0);
  }
  function applyTurn() {
    const team = game.teams[game.turn];
    team.score = Math.max(0, team.score + turnPoints());
    if (game.lastTeam !== null && game.lastTeam >= 0 && game.lastTeam !== game.turn) game.teams[game.lastTeam].score++;
    for (const x of game.log) if (x.ok && (!game.best || x.ms < game.best.ms)) game.best = { t: x.t, ms: x.ms, team: team.name };
    game.turns++;
    game.turn = (game.turn + 1) % game.teams.length;
    // победа проверяется только после полного круга, чтобы у всех было поровну ходов
    if (game.turn === 0) {
      const max = Math.max(...game.teams.map(t => t.score));
      const leaders = game.teams.filter(t => t.score === max);
      if (max >= game.target && leaders.length === 1) {
        game.winner = game.teams.indexOf(leaders[0]);
        closingGuard(false);
        return go('win');
      }
      game.round++;
    }
    go('score');
  }

  function closingGuard(on) {
    try { if (inTelegram) on ? tg.enableClosingConfirmation() : tg.disableClosingConfirmation(); } catch { /* старый клиент */ }
  }

  // ---------- экраны ----------
  const CROC = `<svg class="croc" viewBox="0 0 100 100" aria-hidden="true"><path d="M8 62c10-20 30-28 52-26l24-10c6-2 10 4 6 8l-8 6 6 2c4 1 4 7 0 8l-26 6c-12 14-34 18-54 6z" fill="#3ddc97"/><circle cx="58" cy="44" r="5" fill="#140f1f"/><circle cx="59" cy="43" r="1.6" fill="#fff"/><path d="M46 66l4 6 4-6 4 6 4-6" stroke="#140f1f" stroke-width="3" fill="none" stroke-linejoin="round"/></svg>`;

  const views = {
    loading: () => `<p class="loading">Загрузка…</p>`,
    failed: () => `<div class="hero"><h2>Не удалось загрузить слова</h2><p class="muted">Проверьте интернет и откройте игру ещё раз.</p><button class="btn" data-act="reload">Повторить</button></div>`,

    age: () => `
      <div class="hero">${CROC}<h1>Крокодил <span>18+</span></h1>
        <p class="muted">В игре есть пикантные слова, шутки про алкоголь и отношения. Без откровенного контента, но для взрослой компании.</p></div>
      <div class="stack spacer">
        <button class="btn" data-act="adult">Мне есть 18 лет</button>
        <button class="link" data-act="minor">Мне нет 18</button>
      </div>`,
    minor: () => `<div class="hero"><h2>Тогда в другой раз</h2><p class="muted">Эта версия игры только для взрослых.</p></div>`,

    home: () => {
      const canResume = game && game.phase && game.phase !== 'win';
      return `
      <div class="hero">${CROC}<h1>Крокодил <span>18+</span></h1>
        <p class="muted">Покажи слово без звуков и слов. Твоя команда угадывает, пока идёт таймер.</p></div>
      <div class="stack spacer">
        ${canResume ? `<button class="btn ok" data-act="resume">Продолжить игру · ход ${game.turns + 1}</button>` : ''}
        <button class="btn" data-act="setup">Новая игра</button>
        <button class="btn alt" data-act="rules">Правила</button>
        <button class="btn alt" data-act="custom">Свои слова${custom.length ? ` · ${custom.length}` : ''}</button>
      </div>`;
    },

    rules: () => `
      <h2>Правила</h2>
      <div class="rules card"><ol>
        <li>Разбейтесь на команды от 2 человек. Телефон один на всех.</li>
        <li>Команды ходят по очереди. Один игрок показывает слово, его команда угадывает.</li>
        <li>Показывать можно жестами и мимикой. Нельзя говорить, издавать звуки, показывать буквы и тыкать в предметы вокруг.</li>
        <li>Угадали — свайп вправо или «Угадано», +1. Не выходит — свайп влево или «Пропуск». Со штрафом пропуск стоит −1.</li>
        <li>«Последнее слово»: когда время вышло, текущее слово может угадать любая команда, очко получает она.</li>
        <li>После хода спорные слова можно исправить. Побеждает команда, набравшая нужные очки по итогам полного круга.</li>
        <li>Пить за пропуски необязательно. Но кто мы такие, чтобы вас останавливать.</li>
      </ol></div>
      <button class="btn alt spacer" data-act="home">Назад</button>`,

    custom: () => `
      <h2>Свои слова</h2>
      <p class="muted small">По одному слову или фразе на строку. Появятся как категория «Свои слова». Хранятся только на этом телефоне.</p>
      <textarea id="customText" placeholder="Директор на корпоративе&#10;Кот Васька в отпуске">${esc(custom.join('\n'))}</textarea>
      <div class="stack spacer">
        <button class="btn" data-act="saveCustom">Сохранить</button>
        <button class="btn alt" data-act="home">Назад</button>
      </div>`,

    setup: () => {
      const sel = selectedCats();
      return `
      <h2>Новая игра</h2>
      <div class="stack">
        <div class="eyebrow">Команды</div>
        ${settings.teams.map((n, i) => `
          <div class="team-row"><span class="dot" style="background:${COLORS[i]}"></span>
            <input id="team${i}" data-team="${i}" value="${esc(n)}" maxlength="24" aria-label="Название команды ${i + 1}">
            ${settings.teams.length > 2 ? `<button class="x" data-act="delTeam" data-i="${i}" aria-label="Удалить команду">×</button>` : ''}
          </div>`).join('')}
        ${settings.teams.length < 6 ? `<button class="btn alt" data-act="addTeam">+ Команда</button>` : ''}
      </div>
      <div class="stack"><div class="eyebrow">Время хода, сек</div>
        <div class="seg">${TIMES.map(t => `<button class="${t === settings.time ? 'on' : ''}" data-act="time" data-v="${t}">${t}</button>`).join('')}</div></div>
      <div class="stack"><div class="eyebrow">Очки до победы</div>
        <div class="seg">${TARGETS.map(t => `<button class="${t === settings.target ? 'on' : ''}" data-act="target" data-v="${t}">${t}</button>`).join('')}</div></div>
      <div class="stack"><div class="eyebrow">Категории</div>
        <div class="chips">${allCats().map(c => `<button class="chip ${sel.includes(c.id) ? 'on' : ''}" data-act="cat" data-id="${c.id}">${esc(c.name)}<small>${c.words.length}</small></button>`).join('')}</div></div>
      <div class="stack"><div class="eyebrow">Сложность слов</div>
        <div class="seg">${DIFFS.map(([v, n]) => `<button class="${v === settings.diff ? 'on' : ''}" data-act="diff" data-v="${v}">${n}</button>`).join('')}</div></div>
      <div class="stack">
        ${toggle('penalty', 'Штраф за пропуск', 'Пропущенное слово отнимает очко')}
        ${toggle('lastWord', 'Последнее слово', 'Когда время вышло, слово может угадать любая команда')}
        ${toggle('sound', 'Звук', 'Тиканье на последних 10 секундах')}
      </div>
      ${setupError ? `<p class="error">${setupError}</p>` : ''}
      <div class="stack spacer">
        <button class="btn" data-act="start">Начать игру</button>
        <button class="link" data-act="home">Назад</button>
      </div>`;
    },

    ready: () => {
      const t = game.teams[game.turn];
      return `
      <div class="hero center" style="margin-top:14vh">
        <div class="eyebrow">Раунд ${game.round} · передайте телефон</div>
        <div class="big-team" style="color:${t.color}">${esc(t.name)}</div>
        <p class="muted">Один игрок показывает, остальные из команды угадывают. Экран видит только показывающий.</p>
        <p class="muted small">${game.time} секунд${game.penalty ? ' · пропуск −1' : ''}</p>
      </div>
      <div class="stack spacer">
        <button class="btn" data-act="go">Я готов, поехали</button>
        <button class="link" data-act="home">В меню</button>
      </div>`;
    },

    turn: () => {
      const t = game.teams[game.turn];
      const hits = game.log.filter(x => x.ok).length;
      const s = Math.ceil(game.left / 1000);
      return `
      <div class="turn">
        <div class="turn-top">
          <span><span class="dot" style="background:${t.color};margin-right:8px"></span>${esc(t.name)}</span>
          <span style="display:flex;gap:8px;align-items:center"><span class="pill">Угадано: ${hits}</span><button class="pause-btn" data-act="pause" aria-label="Пауза">❚❚</button></span>
        </div>
        <div class="timer"><svg viewBox="0 0 100 100"><circle cx="50" cy="50" r="44" fill="none" stroke="var(--raise)" stroke-width="8"/>
          <circle id="ring" cx="50" cy="50" r="44" fill="none" stroke="var(--pink)" stroke-width="8" stroke-linecap="round" stroke-dasharray="${RING}" stroke-dashoffset="${RING * (1 - game.left / (game.time * 1000))}"/></svg>
          <div class="t" id="sec">${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}</div></div>
        <div class="word-card" id="card"><div><div class="word">${esc(game.word ? game.word.t : '')}</div><div class="cat">${esc(game.word ? game.word.c : '')}</div></div></div>
        <div class="actions"><button class="skip" data-act="skip">Пропуск</button><button class="hit" data-act="hit">Угадано</button></div>
        <div class="hint">или свайп по карточке: вправо угадано, влево пропуск</div>
      </div>
      ${game.paused ? `
      <div class="overlay"><div class="box">
        <h2>Пауза</h2><p class="muted">Осталось ${s} с</p>
        <button class="btn" data-act="unpause">Продолжить</button>
        <button class="btn alt" data-act="endTurn">Закончить ход</button>
        <button class="link" data-act="home">В меню (игра сохранится)</button>
      </div></div>` : ''}`;
    },

    last: () => `
      <div class="hero center" style="margin-top:8vh">
        <div class="eyebrow">Время вышло · последнее слово</div>
        <div class="big-team">${esc(game.word.t)}</div>
        <p class="muted">Кто угадал? Очко получит эта команда.</p>
      </div>
      <div class="stack spacer">
        ${game.teams.map((t, i) => `<button class="btn alt" data-act="lastBy" data-i="${i}"><span class="dot" style="background:${t.color};margin-right:8px"></span>${esc(t.name)}</button>`).join('')}
        <button class="btn" data-act="lastBy" data-i="-1">Никто не угадал</button>
      </div>`,

    review: () => {
      const pts = turnPoints();
      const other = game.lastTeam !== null && game.lastTeam >= 0 && game.lastTeam !== game.turn ? game.teams[game.lastTeam] : null;
      return `
      <div class="center stack"><div class="eyebrow">Итог хода · ${esc(game.teams[game.turn].name)}</div>
        <div class="total ${pts >= 0 ? 'plus' : 'minus'}">${pts > 0 ? '+' : ''}${pts}</div>
        <p class="muted small">Нажмите на слово, чтобы исправить спорное</p></div>
      <div class="list">
        ${game.log.map((x, i) => `<button class="row" data-act="toggle" data-i="${i}"><span class="w">${esc(x.t)}</span><span class="mark ${x.ok ? 'y' : 'n'}">${x.ok ? '✓' : '✕'}</span></button>`).join('') || '<p class="muted center">Ни одного слова за ход</p>'}
        ${game.lastTeam !== null && game.lastTeam >= 0 ? `<div class="row"><span class="w">Последнее слово: ${esc(game.word.t)}</span><span class="muted small">${esc(game.teams[game.lastTeam].name)} +1</span></div>` : ''}
      </div>
      ${other ? `<p class="muted small center">${esc(other.name)} получает +1 за последнее слово</p>` : ''}
      <div class="stack spacer"><button class="btn" data-act="apply">Записать очки</button></div>`;
    },

    score: () => {
      const max = Math.max(1, game.target, ...game.teams.map(t => t.score));
      const top = Math.max(...game.teams.map(t => t.score));
      const tie = game.turn === 0 && top >= game.target;
      return `
      <div class="stack"><div class="eyebrow">Раунд ${game.round} · играем до ${game.target}</div><h2>Счёт</h2></div>
      ${tie ? `<p class="card small">Ничья на первом месте. Играем ещё круг.</p>` : ''}
      <div class="bars">${game.teams.map((t, i) => `
        <div class="bar ${i === game.turn ? 'next' : ''}"><div class="lab"><span>${esc(t.name)}</span><b>${t.score}</b></div>
          <div class="track"><div class="fill" style="width:${Math.round(t.score / max * 100)}%;background:${t.color}"></div></div></div>`).join('')}</div>
      <div class="stack spacer">
        <button class="btn" data-act="next">Ходит ${esc(game.teams[game.turn].name)}</button>
        <button class="link" data-act="home">В меню</button>
      </div>`;
    },

    win: () => {
      const w = game.teams[game.winner];
      const sorted = game.teams.slice().sort((a, b) => b.score - a.score);
      const max = Math.max(1, ...game.teams.map(t => t.score));
      return `
      <div class="hero center" style="margin-top:6vh">
        <div class="eyebrow">Победа</div>
        <div class="big-team winner">${esc(w.name)}</div>
        <p class="muted">${w.score} очков · раундов: ${game.round}</p>
      </div>
      <div class="bars">${sorted.map(t => `
        <div class="bar"><div class="lab"><span>${esc(t.name)}</span><b>${t.score}</b></div>
          <div class="track"><div class="fill" style="width:${Math.round(t.score / max * 100)}%;background:${t.color}"></div></div></div>`).join('')}</div>
      ${game.best ? `<p class="card small">Самое быстрое слово: «${esc(game.best.t)}», ${esc(game.best.team)} угадали за ${(game.best.ms / 1000).toFixed(1)} с</p>` : ''}
      <div class="stack spacer">
        <button class="btn" data-act="again">Сыграть ещё</button>
        <button class="link" data-act="menuEnd">В меню</button>
      </div>`;
    },
  };

  function toggle(key, title, sub) {
    return `<button class="toggle ${settings[key] ? 'on' : ''}" data-act="flag" data-key="${key}"><span>${title}<small>${sub}</small></span><span class="sw"></span></button>`;
  }

  function render() {
    app.innerHTML = views[screen]();
    if (screen === 'turn' && !game.paused) bindSwipe();
    confetti(screen === 'win');
    backButton();
  }

  // ---------- действия ----------
  const actions = {
    reload: () => location.reload(),
    adult: () => { store.set('adult', true); go('home'); },
    minor: () => { try { if (inTelegram) return tg.close(); } catch { /* */ } go('minor'); },
    home: () => { if (screen === 'turn') pause(); stop(); keepAwake(false); go('home'); },
    rules: () => go('rules'),
    custom: () => go('custom'),
    setup: () => { setupError = ''; go('setup'); },
    saveCustom: () => {
      const seen = new Set();
      custom = document.getElementById('customText').value.split('\n').map(s => s.trim()).filter(s => s && !seen.has(s) && seen.add(s));
      store.set('custom', custom);
      go('home');
    },
    resume: () => {
      if (game.phase === 'turn') game.paused = true;
      closingGuard(true);
      go(game.phase);
    },
    addTeam: () => { settings.teams.push(NAMES[settings.teams.length]); saveSettings(); render(); },
    delTeam: el => { settings.teams.splice(+el.dataset.i, 1); saveSettings(); render(); },
    time: el => { settings.time = +el.dataset.v; saveSettings(); render(); },
    target: el => { settings.target = +el.dataset.v; saveSettings(); render(); },
    diff: el => { settings.diff = +el.dataset.v; saveSettings(); render(); },
    flag: el => { settings[el.dataset.key] = !settings[el.dataset.key]; saveSettings(); render(); },
    cat: el => {
      const sel = selectedCats(), id = el.dataset.id;
      settings.cats = sel.includes(id) ? sel.filter(x => x !== id) : sel.concat(id);
      saveSettings(); render();
    },
    start: newGame,
    go: startTurn,
    pause,
    unpause: () => { game.paused = false; render(); run(); },
    endTurn: () => { stop(); keepAwake(false); go('review'); },
    hit: () => mark(true),
    skip: () => mark(false),
    lastBy: el => { game.lastTeam = +el.dataset.i; go('review'); },
    toggle: el => { const x = game.log[+el.dataset.i]; x.ok = !x.ok; save(); render(); },
    apply: applyTurn,
    next: () => go('ready'),
    again: () => { settings.teams = game.teams.map(t => t.name); saveSettings(); newGame(); },
    menuEnd: () => { game = null; store.del('game'); go('home'); },
  };

  app.addEventListener('click', e => {
    if (!audio) { try { audio = new (window.AudioContext || window.webkitAudioContext)(); } catch { /* без звука */ } }
    if (audio && audio.state === 'suspended') audio.resume();
    const el = e.target.closest('[data-act]');
    if (el && actions[el.dataset.act]) actions[el.dataset.act](el);
  });
  app.addEventListener('input', e => {
    const i = e.target.dataset.team;
    if (i !== undefined) { settings.teams[+i] = e.target.value; saveSettings(); }
  });

  function bindSwipe() {
    const card = document.getElementById('card');
    if (!card) return;
    let x0 = null, id = null;
    card.addEventListener('pointerdown', e => { x0 = e.clientX; id = e.pointerId; card.setPointerCapture(id); card.style.transition = 'none'; });
    card.addEventListener('pointermove', e => {
      if (x0 === null) return;
      const dx = e.clientX - x0;
      card.style.transform = `translateX(${dx}px) rotate(${dx / 25}deg)`;
      card.style.borderColor = dx > 40 ? 'var(--ok)' : dx < -40 ? 'var(--bad)' : '';
    });
    const end = e => {
      if (x0 === null) return;
      const dx = e.clientX - x0; x0 = null;
      card.style.transition = 'transform .15s'; card.style.transform = ''; card.style.borderColor = '';
      if (dx > 70) mark(true); else if (dx < -70) mark(false);
    };
    card.addEventListener('pointerup', end);
    card.addEventListener('pointercancel', end);
  }

  // ---------- Telegram ----------
  function backButton() {
    if (!inTelegram || !tg.BackButton) return;
    ['rules', 'custom', 'setup'].includes(screen) ? tg.BackButton.show() : tg.BackButton.hide();
  }
  if (tg) {
    try {
      tg.ready(); tg.expand();
      if (tg.disableVerticalSwipes) tg.disableVerticalSwipes();
      if (tg.setHeaderColor) tg.setHeaderColor('#140f1f');
      if (tg.setBackgroundColor) tg.setBackgroundColor('#140f1f');
      if (tg.BackButton) tg.BackButton.onClick(() => go('home'));
    } catch { /* старый клиент Telegram */ }
  }
  document.addEventListener('visibilitychange', () => { if (document.hidden) pause(); });

  // ---------- конфетти ----------
  let confettiOn = false;
  function confetti(on) {
    const cv = document.getElementById('confetti');
    if (on === confettiOn) return;
    confettiOn = on; cv.hidden = !on;
    if (!on) return;
    const ctx = cv.getContext('2d');
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const bits = Array.from({ length: 90 }, (_, k) => ({ x: Math.random(), y: Math.random() - 1, v: .002 + Math.random() * .004, r: Math.random() * 6, c: COLORS[k % COLORS.length] }));
    const frame = () => {
      if (!confettiOn) return;
      cv.width = cv.clientWidth; cv.height = cv.clientHeight;
      for (const b of bits) {
        if (!reduce) { b.y += b.v; b.r += .06; if (b.y > 1.05) b.y = -.05; }
        ctx.save(); ctx.translate(b.x * cv.width, b.y * cv.height); ctx.rotate(b.r);
        ctx.fillStyle = b.c; ctx.fillRect(-4, -2, 8, 4); ctx.restore();
      }
      if (!reduce) requestAnimationFrame(frame);
    };
    frame();
  }

  // ---------- старт ----------
  render();
  loadWords().then(() => {
    screen = store.get('adult', false) ? 'home' : 'age';
    render();
  }).catch(() => { screen = 'failed'; render(); });
})();
