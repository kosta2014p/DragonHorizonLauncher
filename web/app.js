// ---------- фон: тлеющие угольки (дёшево: спрайт вместо shadowBlur, лимит 30 fps) ----------
// Оформлено как модуль с start()/stop(), чтобы «экономный режим» и системная
// настройка «меньше анимаций» могли полностью остановить рендер-цикл, а не
// просто спрятать канвас (canvas продолжал бы рисовать в фоне и грузить CPU).

const embersControl = (function embers(){
  const canvas = document.getElementById('embers');
  const ctx = canvas.getContext('2d');
  let w, h, particles = [];
  const FRAME = 1000 / 30;
  let active = false;
  let rafId = null;
  let last = 0, prev = 0, slowTicks = 0, adapted = false;

  const sprite = document.createElement('canvas');
  sprite.width = sprite.height = 32;
  (function(){
    const s = sprite.getContext('2d');
    const g = s.createRadialGradient(16, 16, 1, 16, 16, 15);
    g.addColorStop(0, 'rgba(216,180,254,.95)');
    g.addColorStop(.4, 'rgba(167,139,250,.55)');
    g.addColorStop(1, 'rgba(167,139,250,0)');
    s.fillStyle = g;
    s.fillRect(0, 0, 32, 32);
  })();

  function resize(){
    w = canvas.width = window.innerWidth;
    h = canvas.height = window.innerHeight;
  }
  window.addEventListener('resize', resize);
  resize();

  function makeParticle(){
    return {
      x: Math.random() * w,
      y: h + Math.random() * 60,
      r: 2 + Math.random() * 3.5,
      speed: .25 + Math.random() * .5,
      drift: (Math.random() - .5) * .3,
      life: Math.random(),
    };
  }

  function tick(t){
    if(!active) return;
    rafId = requestAnimationFrame(tick);
    const dt = t - prev;
    prev = t;
    if(dt > 70){ slowTicks++; } else if(slowTicks > 0){ slowTicks--; }
    if(!adapted && slowTicks > 8 && particles.length > 6){
      particles = particles.slice(0, Math.ceil(particles.length / 2));
      adapted = true;
    }
    if(t - last < FRAME) return;
    last = t - ((t - last) % FRAME);

    ctx.clearRect(0, 0, w, h);
    for(const p of particles){
      p.y -= p.speed;
      p.x += p.drift;
      p.life += .01;
      if(p.y < -10){ Object.assign(p, makeParticle(), {y: h + 10}); }

      const flicker = .55 + Math.sin(p.life * 12) * .25;
      ctx.globalAlpha = Math.max(.1, flicker);
      ctx.drawImage(sprite, p.x - p.r, p.y - p.r, p.r * 2, p.r * 2);
    }
    ctx.globalAlpha = 1;
  }

  function start(){
    if(active) return;
    active = true;
    adapted = false;
    slowTicks = 0;
    prev = last = 0;
    particles = Array.from({length: 18}, makeParticle);
    canvas.hidden = false;
    rafId = requestAnimationFrame(tick);
  }

  function stop(){
    active = false;
    if(rafId) cancelAnimationFrame(rafId);
    rafId = null;
    ctx.clearRect(0, 0, w, h);
    canvas.hidden = true;
  }

  return { start, stop };
})();

// ---------- звуки (8) — WebAudio без файлов ----------
const sfx = (function(){
  let ctx = null;
  function ensure(){ try{ if(!ctx) ctx = new (window.AudioContext||window.webkitAudioContext)(); if(ctx.state==='suspended') ctx.resume().catch(()=>{}); }catch(e){} return ctx; }
  function beep(freq, dur, type, vol, slide){
    if(settings && settings.sounds===false) return;
    const c = ensure(); if(!c) return;
    const o = c.createOscillator(), g=c.createGain();
    o.type=type||'sine'; o.frequency.value=freq;
    if(slide) o.frequency.exponentialRampToValueAtTime(slide, c.currentTime+dur);
    g.gain.value=vol||0.18; g.gain.exponentialRampToValueAtTime(0.01, c.currentTime+dur);
    o.connect(g); g.connect(c.destination); o.start(); o.stop(c.currentTime+dur);
  }
  return {
    click(){ beep(900,0.07,'sine',0.12); },
    success(){ beep(600,0.12,'sine',0.18,900); setTimeout(()=>beep(900,0.15,'sine',0.16),90); },
    error(){ beep(180,0.22,'square',0.12,120); },
    whoosh(){ beep(400,0.08,'triangle',0.1,200); },
  };
})();

// ---------- i18n RU/EN (23) ----------
const i18nDict = {
  ru: {play:"Играть", ready:"готов к запуску", checking:"Проверяем обновления…", show:"Показать", use:"Использовать этот скин", imported:"импортировано", needNick:"введи ник для превью", music:"Фон-музыка", cursor:"Кастомный курсор"},
  en: {play:"Play", ready:"ready to launch", checking:"Checking updates…", show:"Show", use:"Use this skin", imported:"imported", needNick:"enter nick"},
};
function t(k){ try{ return (i18nDict[settings?.language||'ru']||i18nDict.ru)[k]||k; }catch(e){ return k; } }
function applyLanguage(lang){
  settings.language = lang==='en'?'en':'ru';
  document.documentElement.lang = settings.language;
  if(el.playBtn) el.playBtn.querySelector('.play-btn-label').textContent = t('play');
  if(el.meta) el.meta.textContent = t('ready');
  if(el.skinShowBtn) el.skinShowBtn.textContent = t('show');
  if(el.skinUseBtn) el.skinUseBtn.textContent = t('use');
  persistAll();
}

// ---------- фон-музыка (1) — громкая эмбиент-петля ----------
let musicCtx=null, musicOsc=[], musicGain=null, musicOn=false, musicPending=false, musicTimer=null;
function createMusicNodes(){
  if(musicOn) return;
  try{
    if(!musicCtx) musicCtx = new (window.AudioContext||window.webkitAudioContext)();
    try{ musicOsc.forEach(o=>{try{o.stop(); o.disconnect();}catch(e){}});}catch(e){}
    musicOsc=[]; if(musicGain) try{musicGain.disconnect();}catch(e){}
    if(musicTimer) { clearInterval(musicTimer); musicTimer=null; }
    musicGain = musicCtx.createGain(); musicGain.gain.value=0.0; musicGain.connect(musicCtx.destination);
    // мягкий fade-in 1.5с чтобы не хлопало
    musicGain.gain.linearRampToValueAtTime(0.14, musicCtx.currentTime+1.4);
    // 2 дрона + лёгкий фильтр
    const o1=musicCtx.createOscillator(), o2=musicCtx.createOscillator();
    o1.type='sine'; o1.frequency.value=110; // A2
    o2.type='triangle'; o2.frequency.value=55; // A1 октавой ниже
    o2.detune.value=7; // лёгкий детюн для объёма
    const f=musicCtx.createBiquadFilter(); f.type='lowpass'; f.frequency.value=900; f.Q.value=0.7;
    // лёгкое LFO на фильтр для живости
    const lfo=musicCtx.createOscillator(); lfo.type='sine'; lfo.frequency.value=0.18;
    const lfoGain=musicCtx.createGain(); lfoGain.gain.value=250;
    lfo.connect(lfoGain); lfoGain.connect(f.frequency); lfo.start();
    o1.connect(f); o2.connect(f); f.connect(musicGain);
    // простая мелодия арпеджио поверх дрона
    const lead=musicCtx.createOscillator(); lead.type='sine'; lead.frequency.value=220;
    const leadGain=musicCtx.createGain(); leadGain.gain.value=0.0; leadGain.connect(musicGain);
    lead.connect(leadGain); lead.start();
    o1.start(); o2.start();
    const notes=[220,246.9,277.18,329.63,277.18,246.9]; // A3 B C# E
    let idx=0;
    musicTimer=setInterval(()=>{
      if(!musicCtx || musicCtx.state!=='running') return;
      const freq=notes[idx%notes.length];
      try{
        lead.frequency.linearRampToValueAtTime(freq, musicCtx.currentTime+0.08);
        leadGain.gain.cancelScheduledValues(musicCtx.currentTime);
        leadGain.gain.setValueAtTime(0.0, musicCtx.currentTime);
        leadGain.gain.linearRampToValueAtTime(0.11, musicCtx.currentTime+0.12);
        leadGain.gain.linearRampToValueAtTime(0.0, musicCtx.currentTime+0.55);
      }catch(e){}
      idx++;
    }, 620);
    musicOsc=[o1,o2,lead,lfo]; musicOn=true; musicPending=false;
    console.log('[music] started');
  }catch(e){ console.warn('[music] fail', e); musicPending=false; }
}
function startMusic(){
  if(musicOn) return;
  try{
    if(!musicCtx) musicCtx = new (window.AudioContext||window.webkitAudioContext)();
    if(musicCtx.state==='suspended'){
      musicPending=true;
      // пробуем resume — если не жест, Chromium отклонит, ждём клика
      musicCtx.resume().then(()=>{
        if(musicPending) createMusicNodes();
      }).catch(()=>{});
      // fallback: первый клик/клавиша разбудит
      const onGesture=()=>{
        if(musicPending && musicCtx){
          musicCtx.resume().then(()=>{ if(musicPending) createMusicNodes(); }).catch(()=>{});
        }
        document.removeEventListener('click', onGesture);
        document.removeEventListener('keydown', onGesture);
        document.removeEventListener('pointerdown', onGesture);
      };
      document.addEventListener('click', onGesture, {once:true});
      document.addEventListener('keydown', onGesture, {once:true});
      document.addEventListener('pointerdown', onGesture, {once:true});
      return;
    }
    createMusicNodes();
  }catch(e){ musicPending=false; }
}
function stopMusic(){
  musicPending=false;
  if(musicTimer){ clearInterval(musicTimer); musicTimer=null; }
  try{
    if(musicGain){
      try{ musicGain.gain.linearRampToValueAtTime(0.0, musicCtx ? musicCtx.currentTime+0.3 : 0); }catch(e){}
    }
    setTimeout(()=>{
      try{ musicOsc.forEach(o=>{try{o.stop(); o.disconnect();}catch(e){}}); musicOsc=[]; if(musicGain) try{musicGain.disconnect();}catch(e){} musicGain=null; }catch(e){}
    }, 320);
    musicOn=false;
  }catch(e){ musicOn=false; }
  console.log('[music] stopped');
}
function applyMusic(on){
  if(on) startMusic(); else stopMusic();
}
// также будим музыку на любом первом взаимодействии если включена в настройках
document.addEventListener('click', ()=>{
  if(settings && settings.music && !musicOn && musicPending) startMusic();
}, {once:true});
function applyCustomCursor(on){
  document.documentElement.classList.toggle('custom-cursor', !!on);
}

// ---------- скорость график (25) ----------
let speedHistory=[];
function pushSpeed(mb){
  speedHistory.push(mb);
  if(speedHistory.length>60) speedHistory.shift();
  drawSpeedChart();
}
function drawSpeedChart(){
  const cv=document.getElementById('speedChart');
  if(!cv) return;
  // canvas прячут через style.display, а не .hidden — проверяем оба + нулевую ширину
  const hidden = cv.hidden || cv.style.display === 'none' || cv.clientWidth === 0;
  if(speedHistory.length <= 1 || hidden) return;
  const dpr=window.devicePixelRatio||1;
  const W=cv.clientWidth||260, H=40;
  cv.width=W*dpr; cv.height=H*dpr;
  const ctx=cv.getContext('2d');
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);
  const max=Math.max(1, ...speedHistory);
  ctx.beginPath();
  speedHistory.forEach((v,i)=>{
    const x=i/(speedHistory.length-1)*W;
    const y=H-4 - (v/max)*(H-8);
    if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  });
  ctx.strokeStyle='#a78bfa'; ctx.lineWidth=1.4; ctx.stroke();
  const grad=ctx.createLinearGradient(0,0,0,H);
  grad.addColorStop(0,'rgba(167,139,250,.35)'); grad.addColorStop(1,'rgba(167,139,250,.02)');
  ctx.lineTo(W,H); ctx.lineTo(0,H); ctx.closePath(); ctx.fillStyle=grad; ctx.fill();
}

// ---------- estado / мост к Python ----------

const el = {
  modpackName: document.getElementById('modpackName'),
  meta: document.getElementById('meta'),
  nickname: document.getElementById('nickname'),
  progressPct: document.getElementById('progressPct'),
  playBtn: document.getElementById('playBtn'),
  restartBtn: document.getElementById('restartBtn'),
  rowIdle: document.getElementById('rowIdle'),
  rowProgress: document.getElementById('rowProgress'),
  progressStatus: document.getElementById('progressStatus'),
  progressFill: document.getElementById('progressFill'),
  progressSpeed: document.getElementById('progressSpeed'),
  cancelBtn: document.getElementById('cancelBtn'),
  errorMsg: document.getElementById('errorMsg'),
  versionNotice: document.getElementById('versionNotice'),
  serverDot: document.getElementById('serverDot'),
  serverBadgeText: document.getElementById('serverBadgeText'),
  themeBtn: document.getElementById('themeBtn'),
  settingsRam: document.getElementById('settingsRam'),
  settingsRamValue: document.getElementById('settingsRamValue'),
  settingsRamHint: document.getElementById('settingsRamHint'),
  settingsRamWarn: document.getElementById('settingsRamWarn'),
  settingsRamRec: document.getElementById('settingsRamRec'),
  settingsRamRecRow: document.getElementById('settingsRamRecRow'),
  ramApplyBtn: document.getElementById('ramApplyBtn'),
  perfMode: document.getElementById('perfMode'),
  closeOnLaunch: document.getElementById('closeOnLaunch'),
  launcherOnly: document.getElementById('launcherOnly'),
  themeSelect: document.getElementById('themeSelect'),
  javaPath: document.getElementById('javaPath'),
  jvmArgs: document.getElementById('jvmArgs'),
  settingsReset: document.getElementById('settingsReset'),
  repairBtn: document.getElementById('repairBtn'),
  repairHint: document.getElementById('repairHint'),
  historyBlock: document.getElementById('historyBlock'),
  onlineChart: document.getElementById('onlineChart'),
  historyHint: document.getElementById('historyHint'),
  statsText: document.getElementById('statsText'),
  veteranBadge: document.getElementById('veteranBadge'),
  achGrid: document.getElementById('achGrid'),
  diagBtn: document.getElementById('diagBtn'),
  diagList: document.getElementById('diagList'),
  nickAvatar: document.getElementById('nickAvatar'),
  skinNick: document.getElementById('skinNick'),
  skinShowBtn: document.getElementById('skinShowBtn'),
  skinUseBtn: document.getElementById('skinUseBtn'),
  skinPreview: document.getElementById('skinPreview'),
  skinBodyImg: document.getElementById('skinBodyImg'),
  skinName: document.getElementById('skinName'),
  skinModel: document.getElementById('skinModel'),
  skinStatus: document.getElementById('skinStatus'),
  monitorBlock: document.getElementById('monitorBlock'),
  monitorStatus: document.getElementById('monitorStatus'),
  monitorCpu: document.getElementById('monitorCpu'),
  monitorMem: document.getElementById('monitorMem'),
  monitorTps: document.getElementById('monitorTps'),
  monitorPlayers: document.getElementById('monitorPlayers'),
  rpList: document.getElementById('rpList'),
  rpUrl: document.getElementById('rpUrl'),
  rpInstallBtn: document.getElementById('rpInstallBtn'),
  soundsEnabled: document.getElementById('soundsEnabled'),
  trayMinimize: document.getElementById('trayMinimize'),
  fullscreenBtn: document.getElementById('fullscreenBtn'),
  musicEnabled: document.getElementById('musicEnabled'),
  customCursor: document.getElementById('customCursor'),
  langSelect: document.getElementById('langSelect'),
  skinExportBtn: document.getElementById('skinExportBtn'),
  elytraCheck: document.getElementById('elytraCheck'),
  skinEditCanvas: document.getElementById('skinEditCanvas'),
  skinEditColor: document.getElementById('skinEditColor'),
  skinEditClear: document.getElementById('skinEditClear'),
  skinEditExport: document.getElementById('skinEditExport'),
  speedChart: document.getElementById('speedChart'),
};

const SETTINGS_KEY = 'launcherSettings';
const NICK_KEY = 'nickname';

// Сколько памяти в системе (заполняется initSystemMemory, нужно для предупреждения о RAM)
let sysMem = null;
// Были ли сохранённые настройки до первого запуска (для автовыбора RAM
// на fresh-установке). Считаем до initPersistedState, который сам пишет кэш.
let hadLocalSettings = (() => {
  try{ return !!localStorage.getItem(SETTINGS_KEY); } catch(e){ return false; }
})();
// Трогал ли пользователь слайдер вручную — тогда автовыбор не перезаписываем
let ramTouched = false;

// Сколько МБ отдать игре при таком объёме системы: половина RAM с потолком
// ~6 ГБ (больше модпаку обычно не нужно, лишь дольше GC-паузы), минимум 2 ГБ
// системе оставляем всегда. Шаг слайдера — 512, все значения ему кратны.
function recommendedRam(totalMb){
  let rec;
  if(totalMb <= 4096) rec = totalMb - 2048;
  else if(totalMb <= 8192) rec = 4096;
  else rec = 6144;
  rec = Math.floor(rec / 512) * 512;
  return Math.max(2048, rec);
}

function updateRamRec(){
  const row = el.settingsRamRecRow;
  if(!row) return;
  if(!sysMem || !sysMem.total_mb){
    row.hidden = true;
    return;
  }
  const max = +el.settingsRam.max || 16384;
  const rec = Math.min(recommendedRam(sysMem.total_mb), max);
  const cur = +el.settingsRam.value;
  row.hidden = false;
  el.settingsRamRec.textContent = fmtGb(rec);
  // если уже выбрано рекомендованное — кнопку прячем, нечего применять
  el.ramApplyBtn.hidden = (cur === rec);
  row.dataset.rec = rec;
}

// восстановить сохранённый ник
try{
  const savedNick = localStorage.getItem('nickname');
  if(savedNick) el.nickname.value = savedNick;
} catch(e){}

// ---------- настройки (файл launcher_ui.json через Python + localStorage как кэш) ----------
// localStorage в pywebview/WebView2 не всегда переживает перезапуск,
// поэтому источник правды — файл на диске, localStorage лишь для мгновенной отрисовки.

function detectWeakPc(){
  // грубая эвристика: мало логических ядер — почти всегда старое/слабое железо.
  // Не идеально, но лучше, чем всегда включать тяжёлые эффекты по умолчанию.
  const cores = navigator.hardwareConcurrency || 4;
  return cores > 0 && cores <= 2;
}

function loadLocalSettings(){
  let saved = {};
  try{ saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}'); } catch(e){ saved = {}; }
  return {
    ram: Number.isFinite(saved.ram) ? saved.ram : 4096,
    javaPath: typeof saved.javaPath === 'string' ? saved.javaPath : '',
    jvmArgs: typeof saved.jvmArgs === 'string' ? saved.jvmArgs : '',
    skinNick: typeof saved.skinNick === 'string' ? saved.skinNick : '',
    closeOnLaunch: !!saved.closeOnLaunch,
    perfMode: typeof saved.perfMode === 'boolean' ? saved.perfMode : detectWeakPc(),
    launcherOnly: !!saved.launcherOnly,
    theme: saved.theme || 'dark',
    sounds: typeof saved.sounds === 'boolean' ? saved.sounds : true,
    trayMinimize: !!saved.trayMinimize,
    music: !!saved.music,
    customCursor: !!saved.customCursor,
    language: (saved.language==='en'?'en':'ru'),
    compactMode: !!saved.compactMode,
    startMinimized: !!saved.startMinimized,
    autoCloseDelay: Number.isFinite(saved.autoCloseDelay) ? saved.autoCloseDelay : 0,
    downloadSpeedLimit: Number.isFinite(saved.downloadSpeedLimit) ? saved.downloadSpeedLimit : 0,
  };
}

// Совместимость: старый код вызывал loadSettings()/saveSettings()
function loadSettings(){ return loadLocalSettings(); }

function saveSettings(next){
  if(next) settings = next;
  persistAll();
}

function saveLocal(){
  try{
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
    localStorage.setItem(NICK_KEY, el.nickname.value || '');
  } catch(e){}
}

function syncToPython(){
  try{
    const api = window.pywebview && window.pywebview.api;
    if(api && api.save_ui_settings){
      api.save_ui_settings({
        nickname: el.nickname.value || '',
        ram: settings.ram,
        javaPath: settings.javaPath,
        jvmArgs: settings.jvmArgs,
        skinNick: settings.skinNick || '',
        closeOnLaunch: settings.closeOnLaunch,
        perfMode: settings.perfMode,
        launcherOnly: settings.launcherOnly || false,
        theme: settings.theme || 'dark',
        sounds: settings.sounds !== false,
        trayMinimize: !!settings.trayMinimize,
        music: !!settings.music,
        customCursor: !!settings.customCursor,
        language: settings.language||'ru',
        compactMode: !!settings.compactMode,
        startMinimized: !!settings.startMinimized,
        autoCloseDelay: settings.autoCloseDelay||0,
        downloadSpeedLimit: settings.downloadSpeedLimit||0,
      }).catch(() => {});
    }
  } catch(e){}
}

function persistAll(){
  saveLocal();
  syncToPython();
}

function applyLoadedState(data){
  if(!data || typeof data !== 'object') return false;
  let changed = false;
  if(typeof data.nickname === 'string' && data.nickname && !el.nickname.value){
    el.nickname.value = data.nickname;
    changed = true;
  } else if(typeof data.nickname === 'string' && data.nickname && el.nickname.value !== data.nickname){
    // файл новее кэша — доверяем файлу
    el.nickname.value = data.nickname;
    changed = true;
  }
  const next = { ...settings };
  if(Number.isFinite(data.ram)) next.ram = data.ram;
  if(typeof data.javaPath === 'string') next.javaPath = data.javaPath;
  if(typeof data.jvmArgs === 'string') next.jvmArgs = data.jvmArgs;
  if(typeof data.skinNick === 'string') next.skinNick = data.skinNick;
  if(typeof data.closeOnLaunch === 'boolean') next.closeOnLaunch = data.closeOnLaunch;
  if(typeof data.launcherOnly === 'boolean') next.launcherOnly = data.launcherOnly;
  // perfMode:null из Python = "авто", не затираем уже определённое значение
  if(typeof data.perfMode === 'boolean') next.perfMode = data.perfMode;
  if(typeof data.sounds === 'boolean') next.sounds = data.sounds;
  if(typeof data.trayMinimize === 'boolean') next.trayMinimize = data.trayMinimize;
  if(typeof data.music === 'boolean') next.music = data.music;
  if(typeof data.customCursor === 'boolean') next.customCursor = data.customCursor;
  if(typeof data.compactMode === 'boolean') next.compactMode = data.compactMode;
  if(typeof data.startMinimized === 'boolean') next.startMinimized = data.startMinimized;
  if(Number.isFinite(data.autoCloseDelay)) next.autoCloseDelay = data.autoCloseDelay;
  if(Number.isFinite(data.downloadSpeedLimit)) next.downloadSpeedLimit = data.downloadSpeedLimit;
  if(typeof data.language === 'string') next.language = data.language==='en'?'en':'ru';
  settings = next;
  renderSettingsForm();
  applyMotionPreference(settings);
  applyMusic(next.music);
  applyCustomCursor(next.customCursor);
  applyLanguage(next.language);
  if(data.theme){
    applyTheme(data.theme);
    if(el.themeSelect) el.themeSelect.value = data.theme;
  }
  saveLocal();
  return changed;
}

let _persistPromise = null;
async function initPersistedState(){
  const api = window.pywebview && window.pywebview.api;
  // мост ещё не готов — НЕ помечаем загруженным, повторим на pywebviewready,
  // иначе настройки из файла никогда не прочитаются и старт сохранит дефолты
  if(!api || !api.get_ui_settings) return;
  if(_persistPromise) return _persistPromise;
  _persistPromise = (async () => {
    try{
      const data = await api.get_ui_settings();
      if(data) applyLoadedState(data);
    } catch(e){}
  })();
  return _persistPromise;
}

function applyMotionPreference(settings){
  const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const reduce = settings.perfMode || prefersReduced;
  document.body.classList.toggle('reduce-motion', reduce);
  document.body.classList.toggle('perf-mode', settings.perfMode);
  if(reduce){ embersControl.stop(); } else { embersControl.start(); }
}

function applyTheme(theme){
  if(theme === 'light'){
    document.documentElement.classList.add('light-theme');
  } else {
    document.documentElement.classList.remove('light-theme');
  }
}

function initTheme(){
  if(window.pywebview && window.pywebview.api && window.pywebview.api.get_theme){
    window.pywebview.api.get_theme().then(t => {
      // бэкенд возвращает {ok, theme}; старые сборки — голую строку
      const theme = (t && typeof t === 'object' ? t.theme : t);
      if(theme){
        applyTheme(theme);
        if(el.themeSelect) el.themeSelect.value = theme;
      }
    }).catch(() => {});
  }
}
function applyCompactMode(on){
  document.documentElement.classList.toggle('compact-mode', !!on);
  document.body.classList.toggle('compact-mode', !!on);
}

let settings = loadLocalSettings();

// слайдер RAM: заливка трека + подпись в гигабайтах
function paintRam(){
  const v = +el.settingsRam.value, min = +el.settingsRam.min, max = +el.settingsRam.max;
  el.settingsRam.style.setProperty('--ram-pct', ((v - min) / (max - min) * 100).toFixed(2) + '%');
  const gb = v / 1024;
  el.settingsRamValue.textContent = (Number.isInteger(gb) ? gb : gb.toFixed(1)) + ' ГБ';
  updateRamWarning(v);
  updateRamRec();
}

function updateRamWarning(ramMb){
  const warn = el.settingsRamWarn;
  if(!warn) return;
  const v = ramMb != null ? ramMb : +el.settingsRam.value;
  let msg = '', level = '';
  if(sysMem && sysMem.total_mb){
    const total = sysMem.total_mb;
    if(v > total - 1024){
      msg = `слишком много — системе останется меньше 1 ГБ, игра и Windows будут тормозить`;
      level = 'bad';
    } else if(v > total * 0.75){
      msg = `многовато — больше 75% всей памяти, браузер и систему может поджать`;
      level = 'warn';
    }
  } else if(v > 8192){
    msg = `больше 8 ГБ нужно только тяжёлым сборкам — обычно хватает 4–6 ГБ`;
    level = 'warn';
  }
  if(!msg){
    warn.hidden = true;
    warn.textContent = '';
    warn.dataset.level = '';
    return;
  }
  warn.hidden = false;
  warn.textContent = msg;
  warn.dataset.level = level;
}

function renderSettingsForm(){
  el.settingsRam.value = settings.ram;
  paintRam();
  el.javaPath.value = settings.javaPath;
  el.jvmArgs.value = settings.jvmArgs;
  if(el.skinNick && document.activeElement !== el.skinNick) el.skinNick.value = settings.skinNick || '';
  el.closeOnLaunch.checked = settings.closeOnLaunch;
  el.perfMode.checked = settings.perfMode;
  if(el.launcherOnly) el.launcherOnly.checked = settings.launcherOnly || false;
  if(el.themeSelect) el.themeSelect.value = settings.theme || 'dark';
  if(el.soundsEnabled) el.soundsEnabled.checked = settings.sounds !== false;
  if(el.trayMinimize) el.trayMinimize.checked = !!settings.trayMinimize;
  if(el.musicEnabled) el.musicEnabled.checked = !!settings.music;
  if(el.customCursor) el.customCursor.checked = !!settings.customCursor;
  if(el.langSelect) el.langSelect.value = settings.language||'ru';
  const acd=document.getElementById('autoCloseDelay'); if(acd) acd.value=String(settings.autoCloseDelay||0);
  const dl=document.getElementById('downloadLimit'); if(dl) dl.value=String(settings.downloadSpeedLimit||0);
  const cm=document.getElementById('compactMode'); if(cm) cm.checked=!!settings.compactMode;
  const sm=document.getElementById('startMinimized'); if(sm) sm.checked=!!settings.startMinimized;
  applyCompactMode(!!settings.compactMode);
}

function persistFromForm(){
  const acd=document.getElementById('autoCloseDelay');
  const dl=document.getElementById('downloadLimit');
  const cm=document.getElementById('compactMode');
  const sm=document.getElementById('startMinimized');
  settings = {
    ram: parseInt(el.settingsRam.value, 10) || 4096,
    javaPath: el.javaPath.value.trim(),
    jvmArgs: el.jvmArgs.value.trim(),
    skinNick: el.skinNick ? el.skinNick.value.trim() : (settings.skinNick || ''),
    closeOnLaunch: el.closeOnLaunch.checked,
    perfMode: el.perfMode.checked,
    launcherOnly: el.launcherOnly ? el.launcherOnly.checked : false,
    theme: el.themeSelect ? el.themeSelect.value : 'dark',
    sounds: el.soundsEnabled ? el.soundsEnabled.checked : true,
    trayMinimize: el.trayMinimize ? el.trayMinimize.checked : false,
    music: el.musicEnabled ? el.musicEnabled.checked : false,
    customCursor: el.customCursor ? el.customCursor.checked : false,
    language: el.langSelect ? el.langSelect.value : 'ru',
    compactMode: cm ? cm.checked : !!settings.compactMode,
    startMinimized: sm ? sm.checked : !!settings.startMinimized,
    autoCloseDelay: acd ? parseInt(acd.value,10)||0 : 0,
    downloadSpeedLimit: dl ? parseInt(dl.value,10)||0 : 0,
  };
  persistAll();
  applyMotionPreference(settings);
  applyCompactMode(!!settings.compactMode);
  refreshNickAvatar();
}

renderSettingsForm();
applyMotionPreference(settings);
applyMusic(settings.music);
applyCustomCursor(settings.customCursor);
applyLanguage(settings.language);
initPersistedState();

el.settingsRam.addEventListener('input', () => { ramTouched = true; paintRam(); persistFromForm(); });
if(el.ramApplyBtn){
  el.ramApplyBtn.addEventListener('click', () => {
    const rec = +(el.settingsRamRecRow.dataset.rec || 0);
    if(!rec) return;
    ramTouched = true;
    el.settingsRam.value = Math.min(rec, +el.settingsRam.max);
    persistFromForm();
    paintRam();
  });
}
el.javaPath.addEventListener('change', persistFromForm);
el.jvmArgs.addEventListener('change', persistFromForm);
if(el.skinNick) el.skinNick.addEventListener('change', persistFromForm);
el.closeOnLaunch.addEventListener('change', persistFromForm);
el.perfMode.addEventListener('change', persistFromForm);
if(el.launcherOnly) el.launcherOnly.addEventListener('change', persistFromForm);
if(el.soundsEnabled) el.soundsEnabled.addEventListener('change', ()=>{ persistFromForm(); sfx.click(); });
if(el.trayMinimize) el.trayMinimize.addEventListener('change', persistFromForm);
if(el.themeSelect) el.themeSelect.addEventListener('change', () => {
  const theme = el.themeSelect.value;
  applyTheme(theme);
  settings.theme = theme;
  try{ saveSettings(settings); }catch(e){}
  if(window.pywebview && window.pywebview.api && window.pywebview.api.set_theme){
    window.pywebview.api.set_theme(theme).catch(() => {});
  }
});
if(el.themeBtn){
  el.themeBtn.addEventListener('click', () => {
    const current = document.documentElement.classList.contains('light-theme') ? 'light' : 'dark';
    const next = current === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    if(el.themeSelect) el.themeSelect.value = next;
    settings.theme = next;
    try{ saveSettings(settings); }catch(e){}
    if(window.pywebview && window.pywebview.api && window.pywebview.api.set_theme){
      window.pywebview.api.set_theme(next).catch(() => {});
    }
  });
}
if(el.musicEnabled) el.musicEnabled.addEventListener('change', ()=>{ persistFromForm(); applyMusic(el.musicEnabled.checked); sfx.click(); });
if(el.customCursor) el.customCursor.addEventListener('change', ()=>{ persistFromForm(); applyCustomCursor(el.customCursor.checked); sfx.click(); });
if(el.langSelect) el.langSelect.addEventListener('change', ()=>{ applyLanguage(el.langSelect.value); sfx.click(); });
['autoCloseDelay','downloadLimit','compactMode','startMinimized'].forEach(id=>{
  const e=document.getElementById(id);
  if(!e) return;
  const ev = e.tagName==='SELECT' ? 'change' : 'change';
  e.addEventListener(ev, ()=>{ persistFromForm(); if(id==='compactMode') applyCompactMode(e.checked); sfx.click(); });
});

el.settingsReset.addEventListener('click', () => {
  try{
    localStorage.removeItem(SETTINGS_KEY); localStorage.removeItem(NICK_KEY);
    localStorage.removeItem('recentSkins'); localStorage.removeItem('skinAutoRotate');
    localStorage.removeItem('mainTab'); localStorage.removeItem('onboardingDone');
    localStorage.setItem('forceOnboarding','1');
  }catch(e){}
  try{ hadLocalSettings = false; }catch(e){}
  el.nickname.value = '';
  settings = loadLocalSettings();
  renderSettingsForm();
  applyMotionPreference(settings);
  persistAll();
  sfx.click();
  try{ if(window.maybeShowOnboarding) maybeShowOnboarding(); }catch(e){}
});
const factoryBtn=document.getElementById('factoryReset');
if(factoryBtn) factoryBtn.addEventListener('click', async ()=>{
  if(!confirm('Сбросить всё как в начале? Удалит настройки, статистику, кеш скинов. Сборку придётся качать заново.')) return;
  sfx.click();
  try{ localStorage.clear(); localStorage.setItem('forceOnboarding','1'); }catch(e){}
  try{ localStorage.removeItem('onboardingDone'); }catch(e){}
  try{
    const api=window.pywebview?.api;
    if(api && api.factory_reset) await api.factory_reset();
  }catch(e){}
  try{ hadLocalSettings = false; }catch(e){}
  // сразу перезагружаем — онбординг покажется на свежей странице (forceOnboarding)
  setTimeout(()=>{ try{ location.reload(); }catch(e){} }, 300);
});

// 33 мини-кнопка
const compactBtn=document.getElementById('compactBtn');
if(compactBtn) compactBtn.addEventListener('click', ()=>{
  const cur=document.documentElement.classList.contains('compact-mode');
  const next=!cur;
  applyCompactMode(next);
  // сохраняем
  try{
    settings.compactMode=next;
    persistAll();
    if(document.getElementById('compactMode')) document.getElementById('compactMode').checked=next;
  }catch(e){}
  sfx.click();
});

// вкладки на главном экране (перенесено из настроек)
function switchMainTab(name, silent){
  document.querySelectorAll('.main-tab-btn').forEach(b=>{
    const on = b.dataset.mtab===name;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', on?'true':'false');
  });
  document.querySelectorAll('.main-tabpane').forEach(p=>{
    const on = p.id==='mtab-'+name;
    p.hidden = !on;
    p.classList.toggle('active', on);
  });
  try{ localStorage.setItem('mainTab', name); }catch(e){}
  if(!silent){ try{ sfx.click(); }catch(e){} }
}
document.querySelectorAll('.main-tab-btn').forEach(b=>{
  b.addEventListener('click', ()=> switchMainTab(b.dataset.mtab));
});
(function(){
  try{
    const saved = localStorage.getItem('mainTab');
    if(saved && document.getElementById('mtab-'+saved)) switchMainTab(saved, true);
  }catch(e){}
})();
// совместимость: старый код мог вызывать openSettings/closeSettings
function openSettings(){ switchMainTab('main'); document.getElementById('mainTabs')?.scrollIntoView({behavior:'smooth', block:'start'}); }
function closeSettings(){}

// реагируем на смену системной настройки «меньше анимаций» на лету
window.matchMedia('(prefers-reduced-motion: reduce)').addEventListener('change', () => {
  applyMotionPreference(settings);
});

function showProgress(){
  el.rowIdle.hidden = true;
  el.rowProgress.hidden = false;
  if(el.cancelBtn){ el.cancelBtn.disabled = false; el.cancelBtn.textContent = '✕ Отмена'; }
}
window.showIdle = showIdle;
function showIdle(){
  el.rowProgress.hidden = true;
  el.rowIdle.hidden = false;
  el.playBtn.disabled = false;
  el.progressFill.style.width = '0%';
  el.progressPct.textContent = '0%';
  speedHistory.length = 0;
  window.clearDownloadStats();
  try{ window.setProgressStages([],0); }catch(e){}
  if(el.cancelBtn){ el.cancelBtn.disabled = false; el.cancelBtn.textContent = '✕ Отмена'; }
}

function showVersionNotice(text){
  if(!el.versionNotice) return;
  el.versionNotice.textContent = text;
  el.versionNotice.hidden = false;
}

function hideVersionNotice(){
  if(!el.versionNotice) return;
  el.versionNotice.hidden = true;
  el.versionNotice.textContent = '';
}

function showToast(msg, kind){
  try{
    const cont=document.getElementById('toastContainer');
    if(!cont) return;
    const d=document.createElement('div');
    d.className='toast '+(kind==='error'?'toast--error':kind==='ok'?'toast--ok':'');
    d.textContent=msg;
    cont.appendChild(d);
    setTimeout(()=>{ d.style.opacity='0'; d.style.transform='translateY(8px)'; setTimeout(()=>d.remove(), 250); }, 3000);
  }catch(e){}
}
function setError(msg){
  if(!msg){
    el.errorMsg.hidden = true;
    el.errorMsg.textContent = '';
    return;
  }
  el.errorMsg.hidden = false;
  el.errorMsg.textContent = msg + '  (клик — скопировать)';
  showToast(msg, 'error');
}
function setOkToast(msg){ showToast(msg, 'ok'); }

// вызывается из Python: window.pywebview.api не нужен для входящих вызовов —
// бэкенд сам дергает эти функции через evaluate_js

window.setModpackInfo = function(name, meta){
  if(name) el.modpackName.textContent = name;
  if(meta) el.meta.textContent = meta;
};

window.updateStatus = function(text){
  el.progressStatus.textContent = text;
  // 37 этапы: маппим текст статуса на 4 стадии
  try{
    const stages=["Проверка","Скачивание","Установка","Запуск"];
    let active=0;
    const t=(text||'').toLowerCase();
    if(t.includes('скачива')||t.includes('download')) active=1;
    else if(t.includes('minecraft')||t.includes('модлоадер')||t.includes('проверяем minecraft')||t.includes('сверяем')) active=2;
    else if(t.includes('запускаем')||t.includes('запуск')) active=3;
    if(window.setProgressStages) window.setProgressStages(stages, active);
  }catch(e){}
};

window.updateProgress = function(fraction){
  const pct = Math.max(0, Math.min(1, fraction)) * 100;
  el.progressFill.style.width = pct + '%';
  el.progressPct.textContent = Math.round(pct) + '%';
};

window.updateDownloadStats = function(mbPerSec, etaText){
  if(!el.progressSpeed) return;
  if(!mbPerSec){
    el.progressSpeed.hidden = true;
    el.progressSpeed.textContent = '';
    const sc=document.getElementById('speedChart'); if(sc) sc.style.display='none';
    return;
  }
  el.progressSpeed.hidden = false;
  el.progressSpeed.textContent = etaText
    ? `${mbPerSec.toFixed(1)} МБ/с \u00b7 осталось ${etaText}`
    : `${mbPerSec.toFixed(1)} МБ/с`;
  // 25 график
  const sc=document.getElementById('speedChart');
  if(sc){ sc.style.display='block'; pushSpeed(mbPerSec); }
};

window.clearDownloadStats = function(){
  if(!el.progressSpeed) return;
  el.progressSpeed.hidden = true;
  el.progressSpeed.textContent = '';
};

window.launchDone = function(){
  el.progressStatus.textContent = 'Игра запущена';
  el.progressPct.textContent = '100%';
  sfx.success();
  showToast('Игра запущена ✓','ok');
  try{ if(navigator.vibrate) navigator.vibrate([80,40,80]); }catch(e){}
  try{ if(window.pywebview && window.pywebview.api && window.pywebview.api.notify) window.pywebview.api.notify('Игра запущена','Minecraft запущен'); }catch(e){}
  const sc=document.getElementById('speedChart'); if(sc){ sc.style.display='none'; speedHistory=[]; }
  setTimeout(showIdle, 1200);
};

window.launchFailed = function(message){
  sfx.error();
  setError(message);
  showIdle();
  if(el.restartBtn && !settings.closeOnLaunch) el.restartBtn.hidden = false;
};

el.nickname.addEventListener('input', () => { persistAll(); queueAvatarRefresh(); });

// копирование ошибки в буфер (4)
if(el.errorMsg){
  el.errorMsg.style.cursor = 'pointer';
  el.errorMsg.addEventListener('click', async () => {
    const txt = el.errorMsg.textContent.replace('  (клик — скопировать)','').trim();
    if(!txt) return;
    try{
      if(navigator.clipboard && navigator.clipboard.writeText) await navigator.clipboard.writeText(txt);
      else {
        const ta = document.createElement('textarea'); ta.value = txt; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
      }
      const orig = el.errorMsg.textContent;
      el.errorMsg.textContent = 'Скопировано ✓';
      setTimeout(()=>{ el.errorMsg.textContent = orig; }, 1200);
      if(window.pywebview && window.pywebview.api && window.pywebview.api.copy_clipboard) window.pywebview.api.copy_clipboard(txt).catch(()=>{});
    }catch(e){}
  });
}
// отмена скачивания (6)
if(el.cancelBtn){
  el.cancelBtn.addEventListener('click', async () => {
    el.cancelBtn.disabled = true;
    el.cancelBtn.textContent = 'Отменяем…';
    try{
      if(window.pywebview && window.pywebview.api && window.pywebview.api.cancel_download) await window.pywebview.api.cancel_download();
    }catch(e){}
    // бэкенд кинет DownloadCancelled -> launchFailed
  });
}

el.playBtn.addEventListener('click', () => {
  sfx.click();
  setError(null);
  hideVersionNotice();
  const nickname = (el.nickname.value || 'Player').trim();
  el.nickname.value = nickname;
  persistAll();

  el.playBtn.disabled = true;
  el.restartBtn.hidden = true;
  el.progressFill.style.width = '0%';
  el.progressPct.textContent = '0%';
  el.progressStatus.textContent = 'Проверяем обновления\u2026';
  showProgress();

  const settingsData = {
    java_path: settings.javaPath || null,
    jvm_args: settings.jvmArgs || null,
    close_on_launch: !!settings.closeOnLaunch,
    launcher_only: !!settings.launcherOnly,
  };

  const _api = window.pywebview?.api;
  if(!_api || !_api.play){ window.launchFailed('Мост pywebview не готов — перезапусти лаунчер'); return; }
  _api.play(nickname, settings.ram, settingsData).catch(err => {
    window.launchFailed(String(err));
  });
});

el.restartBtn.addEventListener('click', async () => {
  el.restartBtn.disabled = true;
  el.restartBtn.textContent = '↻ Перезапуск…';
  try{
    const api = window.pywebview && window.pywebview.api;
    if(api && api.restart_game){
      const res = await api.restart_game();
      if(res.ok){
        el.restartBtn.textContent = '↻ Перезапустить';
        el.restartBtn.disabled = false;
      } else {
        setError('Не удалось перезапустить: ' + (res.error || 'неизвестно'));
        el.restartBtn.disabled = false;
      }
    } else {
      el.restartBtn.disabled = false;
    }
  } catch(e){
    setError('Ошибка перезапуска: ' + e);
    el.restartBtn.disabled = false;
  } finally {
    if(el.restartBtn.textContent.includes('…'))
      el.restartBtn.textContent = '↻ Перезапустить';
  }
});

el.nickname.addEventListener('keydown', (e) => {
  if(e.key === 'Enter') el.playBtn.click();
});

// ---------- счётчик игроков онлайн (38 анимация) ----------
let _prevPlayers = null;
let _prevOnline = null;
function animateOnlineDelta(cur, prev){
  const badge=document.getElementById('serverBadge');
  const deltaEl=document.getElementById('serverDelta');
  if(!badge || !deltaEl || prev==null || cur===prev) return;
  const diff = cur - prev;
  if(diff===0) return;
  // pop
  badge.classList.remove('badge--pop');
  void badge.offsetWidth;
  badge.classList.add('badge--pop');
  setTimeout(()=>badge.classList.remove('badge--pop'), 400);
  // delta
  deltaEl.textContent = (diff>0?'+':'')+diff;
  deltaEl.className='badge-delta '+(diff>0?'badge-delta--up':'badge-delta--down');
  deltaEl.hidden=false;
  deltaEl.style.animation='none';
  void deltaEl.offsetWidth;
  deltaEl.style.animation='';
  setTimeout(()=>{ deltaEl.hidden=true; }, 950);
  // звук/вибро легко
  try{ if(diff>0 && settings && settings.sounds!==false) sfx.click(); }catch(e){}
}

function escHtml(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function renderOnlineSample(names){
  const elS=document.getElementById('onlineSample');
  if(!elS) return;
  if(!names||!names.length){ elS.hidden=true; elS.innerHTML=''; return; }
  elS.hidden=false;
  elS.innerHTML='<span style="letter-spacing:.08em;text-transform:uppercase;font-size:10px;">игроки:</span> ' + names.map(n=>`<span style="background:rgba(167,139,250,.14);border:1px solid rgba(167,139,250,.25);padding:2px 6px;border-radius:99px;font-size:11px;color:var(--ink)">${escHtml(n)}</span>`).join('');
}

async function pollServerStatus(){
  if(!el.serverDot || !el.serverBadgeText) return;
  if(document.hidden) return; // окно свёрнуто — мост и сокет не дёргаем
  if(pollServerStatus._inflight) return;
  pollServerStatus._inflight = true;
  try{
    const api = window.pywebview?.api;
    if(!api || !api.server_status) return; // file://-фолбэк: не рисуем ложный офлайн
    const status = await api.server_status();
    if(status && status.online){
      el.serverDot.classList.remove('badge-dot--off');
      el.serverDot.classList.add('badge-dot--on');
      el.serverBadgeText.textContent = `${status.players}/${status.max} онлайн`;
      renderOnlineSample(status.sample||[]);
      if(_prevOnline!==false && _prevPlayers!==null) animateOnlineDelta(status.players, _prevPlayers);
      _prevPlayers = status.players;
      _prevOnline = true;
    } else {
      el.serverDot.classList.remove('badge-dot--on');
      el.serverDot.classList.add('badge-dot--off');
      el.serverBadgeText.textContent = 'сервер недоступен';
      renderOnlineSample([]);
      if(_prevOnline===true) animateOnlineDelta(0, _prevPlayers);
      _prevOnline = false;
      _prevPlayers = null; // сброс — иначе дельта после переподключения преувеличена
    }
  } catch(e){
    el.serverDot.classList.remove('badge-dot--on');
    el.serverDot.classList.add('badge-dot--off');
    el.serverBadgeText.textContent = 'сервер недоступен';
    renderOnlineSample([]);
    _prevOnline = false;
    _prevPlayers = null;
  } finally {
    pollServerStatus._inflight = false;
  }
}

// ---------- починка сборки ----------
if(el.repairBtn){
  el.repairBtn.addEventListener('click', async () => {
    const btn = el.repairBtn, hint = el.repairHint;
    btn.disabled = true;
    if(hint) hint.textContent = 'сбрасываем маркеры установки…';
    try{
      const api = window.pywebview && window.pywebview.api;
      if(api && api.repair) await api.repair();
      if(hint) hint.textContent = 'готово — нажми «Играть», сборка перекачается';
      showVersionNotice('Сборка помечена для переустановки. Нажми «Играть», чтобы перекачать.');
    } catch(e){
      if(hint) hint.textContent = 'не получилось: ' + e;
    } finally{
      btn.disabled = false;
    }
  });
}

// ---------- системная память ----------
function fmtGb(mb){
  const gb = mb / 1024;
  return (Number.isInteger(gb) ? gb : gb.toFixed(1)) + ' ГБ';
}

async function initSystemMemory(){
  if(!el.settingsRamHint) return;
  let mem = null;
  try{
    const api = window.pywebview?.api;
    mem = api && api.system_memory ? await api.system_memory() : null;
  } catch(e){ mem = null; }
  // fallback для открытия в обычном браузере без Python-бэкенда
  if((!mem || !mem.total_mb) && navigator.deviceMemory){
    mem = { total_mb: navigator.deviceMemory * 1024, available_mb: 0 };
  }
  if(!mem || !mem.total_mb){
    el.settingsRamHint.textContent = 'не удалось определить память системы';
    updateRamWarning();
    return;
  }
  sysMem = mem;
  // Оставляем ~1 ГБ системе, округляем вниз до шага 512, минимум 2048
  const safeMax = Math.max(2048, Math.min(16384, Math.floor((mem.total_mb - 1024) / 512) * 512));
  el.settingsRam.max = safeMax;
  // Первая установка (ни файла, ни кэша) + слайдер не трогали —
  // сразу ставим рекомендованное вместо дефолтных 4 ГБ
  let fresh = !hadLocalSettings && !ramTouched;
  if(fresh){
    try{
      const api = window.pywebview && window.pywebview.api;
      if(api && api.has_ui_settings) fresh = !(await api.has_ui_settings());
    } catch(e){}
  }
  if(fresh && !ramTouched){
    settings.ram = Math.min(recommendedRam(mem.total_mb), safeMax);
    persistAll();
    renderSettingsForm();
  }
  // Клампим сохранённое значение, если оно больше реального железа
  else if(settings.ram > safeMax){
    settings.ram = safeMax;
    saveSettings(settings);
    renderSettingsForm();
  } else {
    paintRam();
  }
  el.settingsRamHint.textContent = mem.available_mb
    ? `всего в системе: ${fmtGb(mem.total_mb)} · свободно: ${fmtGb(mem.available_mb)}`
    : `всего в системе: ${fmtGb(mem.total_mb)}`;
}

// ---------- график онлайна за сутки + статистика ----------

function drawOnlineChart(points){
  const cv = el.onlineChart;
  if(!cv || !cv.clientWidth) return; // скрытый блок не рисуем
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth;
  const H = 44;
  cv.width = W * dpr;
  cv.height = H * dpr;
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  if(!points || !points.length){
    return;
  }
  const max = Math.max(1, ...points.map(p => p.players || 0));
  const step = W / Math.max(1, points.length - 1);
  // заливка
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, 'rgba(167,139,250,.45)');
  grad.addColorStop(1, 'rgba(167,139,250,.02)');
  ctx.beginPath();
  points.forEach((p, i) => {
    const x = i * step;
    const y = H - 4 - (p.players / max) * (H - 10);
    if(i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = '#a78bfa';
  ctx.lineWidth = 1.6;
  ctx.stroke();
  // заливка под линией
  ctx.lineTo(W, H);
  ctx.lineTo(0, H);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();
  // точки офлайна — разрывы не рисуем, просто пропускаем
}

async function refreshOnlineHistory(){
  if(document.hidden) return;
  try{
    const api = window.pywebview?.api;
    if(!api || !api.online_history) return;
    const data = await api.online_history();
    const points = (data && data.points) || [];
    if(points.length >= 2 && el.historyBlock){
      el.historyBlock.hidden = false;
      drawOnlineChart(points);
      const last = points[points.length - 1];
      const peak = Math.max(...points.map(p => p.players || 0));
      if(el.historyHint){
        el.historyHint.textContent = `пик ${peak} · сейчас ${last.players}`;
      }
    }
  } catch(e){}
}

async function refreshPlayStats(){
  if(document.hidden) return;
  try{
    const api = window.pywebview?.api;
    if(!api || !api.play_stats) return;
    const s = await api.play_stats();
    if(el.statsText){
      const hours = s.play_hours != null ? s.play_hours : Math.round((s.play_seconds || 0) / 360) / 10;
      el.statsText.textContent = `запусков: ${s.launches || 0} · в игре: ${hours} ч`;
      if(el.historyBlock && (s.launches > 0 || (s.play_seconds || 0) > 0)){
        el.historyBlock.hidden = false;
      }
    }
    if(el.veteranBadge){
      el.veteranBadge.hidden = !s.veteran;
      if(s.veteran){
        const vh = (s.play_hours != null ? s.play_hours : Math.round((s.play_seconds || 0) / 360) / 10);
        el.veteranBadge.title = `Ветеран: ${s.launches || 0} запусков · ${vh} ч в игре`;
      }
    }
    renderAchievements(s.achievements || []);
  } catch(e){}
}

function renderAchievements(list){
  if(!el.achGrid) return;
  if(!Array.isArray(list) || !list.length) return;
  el.achGrid.innerHTML = '';
  let seen = null;
  try{ seen = JSON.parse(localStorage.getItem('achSeen') || 'null'); }catch(e){ seen = null; }
  const firstRun = !Array.isArray(seen);
  if(firstRun) seen = [];
  const seenSet = new Set(seen);
  let changed = false;
  list.forEach(a => {
    const pill = document.createElement('span');
    pill.className = 'ach' + (a.unlocked ? ' ach--got' : '');
    const ico = document.createElement('span');
    ico.className = 'ach-ico';
    ico.textContent = a.icon || '🏅';
    const nm = document.createElement('span');
    nm.textContent = a.name || a.id;
    pill.appendChild(ico);
    pill.appendChild(nm);
    let title = a.desc || '';
    if(a.unlocked && a.date) title += ` — ${a.date}`;
    if(!a.unlocked && a.desc) title = '🔒 ' + title;
    pill.title = title;
    el.achGrid.appendChild(pill);
    if(a.unlocked && !seenSet.has(a.id)){
      seenSet.add(a.id);
      changed = true;
      if(!firstRun){
        try{ showToast(`🏆 Ачивка: ${a.name}`, 'ok'); }catch(e){}
        try{ sfx.success(); }catch(e){}
      }
    }
  });
  if(changed || firstRun){
    try{ localStorage.setItem('achSeen', JSON.stringify([...seenSet])); }catch(e){}
  }
}

// ---------- самодиагностика ----------
if(el.diagBtn){
  el.diagBtn.addEventListener('click', async () => {
    const btn = el.diagBtn, list = el.diagList;
    btn.disabled = true;
    btn.textContent = 'Проверяем…';
    try{
      const api = window.pywebview && window.pywebview.api;
      const res = api && api.run_diagnostics ? await api.run_diagnostics() : null;
      if(list){
        list.hidden = false;
        list.innerHTML = '';
        (res && res.checks || []).forEach(c => {
          const row = document.createElement('div');
          row.className = 'diag-row' + (c.ok ? ' ok' : ' bad');
          row.innerHTML = `<span class="diag-dot"></span><span class="diag-label"></span><small class="diag-detail"></small>`;
          row.querySelector('.diag-label').textContent = c.label;
          row.querySelector('.diag-detail').textContent = c.detail || (c.ok ? 'OK' : 'ошибка');
          list.appendChild(row);
        });
        if(!res || !res.checks || !res.checks.length){
          list.textContent = 'не удалось выполнить диагностику';
        }
      }
      btn.textContent = (res && res.ok) ? 'Всё хорошо ✓' : 'Проверка завершена';
    } catch(e){
      btn.textContent = 'Ошибка проверки';
    } finally{
      btn.disabled = false;
      setTimeout(() => { btn.textContent = 'Проверить систему'; }, 3000);
    }
  });
}

// ---------- скин по нику: аватар + превью + выбор ----------
// Картинки — Minotar по нику (без UUID), проверка существования ника —
// через Python (Mojang API). Выбранный скин хранится в settings.skinNick.

let pendingSkin = null;
let avatarTimer = null;

function skinAvatarNick(){
  if(settings.skinNick) return settings.skinNick;
  const typed = (el.nickname.value || '').trim();
  return typed || null;
}

function refreshNickAvatar(){
  if(!el.nickAvatar) return;
  const n = skinAvatarNick();
  if(!n){
    el.nickAvatar.hidden = true;
    el.nickAvatar.removeAttribute('src');
    return;
  }
  el.nickAvatar.hidden = false;
  el.nickAvatar.src = `https://minotar.net/helm/${encodeURIComponent(n)}/32.png`;
}

function queueAvatarRefresh(){
  clearTimeout(avatarTimer);
  avatarTimer = setTimeout(refreshNickAvatar, 400);
}

if(el.nickAvatar){
  el.nickAvatar.addEventListener('error', () => { el.nickAvatar.hidden = true; });
}

function setSkinStatus(text){
  if(el.skinStatus) el.skinStatus.textContent = text;
}

if(el.skinShowBtn){
  el.skinShowBtn.addEventListener('click', async () => {
    const nick = ((el.skinNick && el.skinNick.value) || '').trim() || (el.nickname.value || '').trim();
    if(!nick){
      setSkinStatus('введи ник для превью');
      return;
    }
    el.skinShowBtn.disabled = true;
    setSkinStatus('ищем скин…');
    if(el.skinPreview) el.skinPreview.hidden = true;
    if(el.skinUseBtn) el.skinUseBtn.hidden = true;
    pendingSkin = null;
    try{
      const api = window.pywebview && window.pywebview.api;
      const res = api && api.skin_lookup ? await api.skin_lookup(nick) : null;
      if(res && res.ok){
        pendingSkin = res;
        if(el.skinBodyImg){
          el.skinBodyImg.hidden=false;
          el.skinBodyImg.style.imageRendering='auto';
          el.skinBodyImg.style.width='';
          // офлайн: local_png file:// приоритет
          if(res.local_png){
            el.skinBodyImg.src = res.local_png;
            el.skinBodyImg.onerror = () => {
              el.skinBodyImg.src = `https://minotar.net/armor/body/${encodeURIComponent(res.nickname)}/120.png`;
              el.skinBodyImg.onerror = () => {
                const uuid = res.uuid;
                if(uuid) el.skinBodyImg.src = `https://crafatar.com/renders/body/${uuid}?overlay`;
                else el.skinBodyImg.hidden=true;
              };
            };
          } else {
            el.skinBodyImg.src = `https://minotar.net/armor/body/${encodeURIComponent(res.nickname)}/120.png`;
            el.skinBodyImg.onerror = () => {
              const uuid = res.uuid;
              if(uuid && el.skinBodyImg.src.includes('minotar.net')){
                el.skinBodyImg.src = `https://crafatar.com/renders/body/${uuid}?overlay`;
                el.skinBodyImg.onerror = () => {
                  if(res.skin_url){
                    el.skinBodyImg.src = res.skin_url;
                    el.skinBodyImg.style.imageRendering='pixelated';
                    el.skinBodyImg.style.width='64px';
                  } else if(res.local_png) el.skinBodyImg.src = res.local_png;
                  else el.skinBodyImg.hidden=true;
                };
              }
            };
          }
        }
        if(el.skinName) el.skinName.textContent = res.nickname + (res.source ? ' · ' + res.source : '');
        if(el.skinModel) el.skinModel.textContent = (res.slim ? 'slim (Алекс)' : 'classic (Стив)') + (res.cached ? ' · из кэша' : '');
        // плащ (33)
        const capeEl = document.getElementById('capeImg');
        if(capeEl){
          let capeUrl = res.cape_url || (res.cape_urls && res.cape_urls.minotar) || `https://minotar.net/cape/${encodeURIComponent(res.nickname)}/64.png`;
          capeEl.hidden = false;
          capeEl.src = capeUrl;
          capeEl.onerror = () => {
            const fallback = res.cape_urls && res.cape_urls.ely;
            if(fallback && capeEl.src !== fallback){ capeEl.src = fallback; } else { capeEl.hidden = true; }
          };
        }
        if(el.skinPreview) el.skinPreview.hidden = false;
        if(el.skinUseBtn) el.skinUseBtn.hidden = false;
        const expBtn=document.getElementById('skinExportBtn'); if(expBtn) expBtn.hidden=false;
        const btn3d=document.getElementById('skin3dBtn'); if(btn3d) btn3d.hidden=false;
        hideSkin3d();
        setSkinStatus(res.cached ? 'найдено в кэше' : 'скин найден' + (res.source==='ely.by'?' (Ely.by)':'')); refreshGallery();
      } else {
        setSkinStatus((res && res.error) || 'не получилось получить скин');
      }
    } catch(e){
      setSkinStatus('ошибка: ' + e);
    } finally{
      el.skinShowBtn.disabled = false;
    }
  });
}

// 3D превью (34) skinview3d
let skinViewer = null, skin3dShown = false;
function ensureSkin3d(){
  return new Promise((res, rej)=>{
    if(window.skinview3d) return res();
    const s=document.createElement('script');
    s.src='vendor/skinview3d.bundle.js';
    s.onload=()=>res();
    s.onerror=()=>{
      // fallback CDN если локальный bundle не загрузился
      const c=document.createElement('script');
      c.src='https://unpkg.com/skinview3d@3.4.1/bundles/skinview3d.bundle.js';
      c.onload=()=>res(); c.onerror=()=>rej('не удалось загрузить skinview3d');
      document.head.appendChild(c);
    };
    document.head.appendChild(s);
  });
}
async function showSkin3d(){
  const canvas=document.getElementById('skin3dCanvas');
  const img=document.getElementById('skinBodyImg');
  if(!canvas || !pendingSkin) return;
  // проверка WebGL — в софтварном Qt может быть недоступен
  try{
    const test = document.createElement('canvas').getContext('webgl') || document.createElement('canvas').getContext('experimental-webgl');
    if(!test) throw new Error('WebGL недоступен');
  }catch(e){ setSkinStatus('WebGL недоступен — 2D режим (включи аппаратное ускорение)'); return; }
  try{
    await ensureSkin3d();
    const url = pendingSkin.skin_url || (pendingSkin.nickname ? `https://minotar.net/skin/${encodeURIComponent(pendingSkin.nickname)}` : null);
    if(!url){ setSkinStatus('нет скина для 3D — выбери ник и нажми «Показать»'); return; }
    // если bundle загрузился, пробуем локальный png из кэша через API
    let skinUrl = pendingSkin.local_png || url;
    if(skinViewer) skinViewer.dispose();
    skinViewer = new window.skinview3d.SkinViewer({canvas, width:80, height:120, skin: skinUrl});
    // 20 автоповорот + zoom колёсиком
    try{
      skinViewer.controls.enableZoom = true;
      skinViewer.controls.enableRotate = true;
      const auto = document.getElementById('skinAutoRotate');
      skinViewer.autoRotate = !auto || auto.checked;
      skinViewer.autoRotateSpeed = 1.2;
    }catch(e){}
    skinViewer.animation = new window.skinview3d.WalkingAnimation();
    skinViewer.animation.speed=0.6;
    if(pendingSkin.cape_url){
      try{ skinViewer.loadCape(pendingSkin.cape_url); }catch(e){}
    } else if(pendingSkin.cape_urls){
      try{ skinViewer.loadCape(pendingSkin.cape_urls.minotar); }catch(e){}
    }
    canvas.hidden=false; if(img) img.hidden=true; skin3dShown=true;
    const btn=document.getElementById('skin3dBtn'); if(btn) btn.textContent='2D';
  }catch(e){
    const msg = String(e).includes('WebGL') ? 'WebGL недоступен — используется 2D. Перезапусти без --disable-gpu' : '3D: '+e;
    setSkinStatus(msg);
    hideSkin3d();
  }
}
function hideSkin3d(){
  const canvas=document.getElementById('skin3dCanvas');
  const img=document.getElementById('skinBodyImg');
  if(canvas) canvas.hidden=true; if(img) img.hidden=false;
  if(skinViewer){ try{skinViewer.animation=null;}catch(e){} }
  skin3dShown=false;
  const btn=document.getElementById('skin3dBtn'); if(btn) btn.textContent='3D';
}
const skin3dBtn=document.getElementById('skin3dBtn');
if(skin3dBtn) skin3dBtn.addEventListener('click', ()=>{ sfx.click(); if(skin3dShown) hideSkin3d(); else showSkin3d(); });

// 18 импорт из файла .png
(function(){
  const fileInput = document.getElementById('skinFile');
  const fileBtn = document.getElementById('skinFileBtn');
  const fileName = document.getElementById('skinFileName');
  if(fileBtn && fileInput){
    fileBtn.addEventListener('click', ()=>{ sfx.click(); fileInput.click(); });
    fileInput.addEventListener('change', async ()=>{
      const f = fileInput.files && fileInput.files[0];
      if(!f){ if(fileName) fileName.textContent=''; return; }
      if(fileName) fileName.textContent = f.name;
      if(f.size > 2*1024*1024){ setSkinStatus('файл >2 МБ'); return; }
      const reader = new FileReader();
      reader.onerror = () => setSkinStatus('не удалось прочитать файл');
      reader.onload = async () => {
        try{
          const b64 = String(reader.result);
          if(!b64.startsWith('data:image/png')){ setSkinStatus('нужен PNG 64×64'); return; }
          setSkinStatus('загружаем…');
          const api = window.pywebview?.api;
          const res = api && api.import_skin_file ? await api.import_skin_file(b64, f.name) : null;
          if(res && res.ok){
            // импортированный PNG — локальный файл, у него нет ника для lookup.
            // Не подсовываем имя файла в skin_use: храним как imported, а ник
            // для привязки берётся из поля скина при нажатии «Использовать».
            pendingSkin = {nickname: null, skin_url: res.uri, local_png: res.uri, slim:false, imported:true, fileName: f.name};
            if(el.skinBodyImg){ el.skinBodyImg.src = res.uri; el.skinBodyImg.hidden=false; }
            const cvs=document.getElementById('skin3dCanvas'); if(cvs) cvs.hidden=true;
            if(el.skinPreview) el.skinPreview.hidden=false;
            const btn3d=document.getElementById('skin3dBtn'); if(btn3d) btn3d.hidden=false;
            const exp2=document.getElementById('skinExportBtn'); if(exp2) exp2.hidden=false;
            setSkinStatus('импортировано: '+f.name+'. Введи ник выше и нажми «Использовать», чтобы привязать скин ника.');
            refreshGallery(); sfx.success();
          } else setSkinStatus((res&&res.error)||'не удалось импортировать');
        }catch(e){ setSkinStatus('ошибка: '+e); }
      };
      reader.readAsDataURL(f);
    });
  }
})();
// 19 галерея последних 5 скинов
async function refreshGallery(){
  const gallery=document.getElementById('skinGallery');
  const group=document.getElementById('skinGalleryGroup');
  if(!gallery) return;
  try{
    const api=window.pywebview&&window.pywebview.api;
    const res= api && api.get_recent_skins ? await api.get_recent_skins() : null;
    const skins=(res&&res.skins)||[];
    const combined=[...skins];
    gallery.innerHTML='';
    if(!combined.length){ if(group) group.hidden=true; return; }
    if(group) group.hidden=false;
    combined.slice(0,5).forEach(s=>{
      const div=document.createElement('div');
      div.className='skin-gallery-item';
      div.title=s.name||s.nickname||'скин';
      const img=document.createElement('img');
      if(s.type==='png' && s.uri) img.src=s.uri;
      else if(s.nickname) {
        img.src=`https://minotar.net/helm/${encodeURIComponent(s.nickname)}/32.png`;
        img.onerror = () => {
          if(s.uri) img.src=s.uri;
          else if(s.uuid) img.src=`https://crafatar.com/avatars/${s.uuid}?overlay`;
          else img.style.display='none';
        };
      }
      else if(s.uri) img.src=s.uri;
      else img.style.display='none';
      img.alt=s.name||'';
      div.appendChild(img);
      div.addEventListener('click', async ()=>{
        sfx.click();
        const nick=(s.nickname||'').trim();
        if(nick && /^[A-Za-z0-9_]{3,16}$/.test(nick)){
          // пробуем lookup
          setSkinStatus('загружаем '+nick);
          try{
            const api2=window.pywebview&&window.pywebview.api;
            const r= api2 && api2.skin_lookup ? await api2.skin_lookup(nick) : null;
            if(r&&r.ok){ pendingSkin=r; if(el.skinBodyImg) el.skinBodyImg.src=`https://minotar.net/armor/body/${encodeURIComponent(r.nickname)}/120.png`; if(el.skinName) el.skinName.textContent=r.nickname; if(el.skinPreview) el.skinPreview.hidden=false; setSkinStatus('выбран из галереи'); }
            else if(s.uri){ // импортированный
              pendingSkin={nickname:null, skin_url:s.uri, local_png:s.uri, imported:true, fileName:s.name}; if(el.skinBodyImg) el.skinBodyImg.src=s.uri; if(el.skinPreview) el.skinPreview.hidden=false; setSkinStatus('выбран импортированный');
            }
          }catch(e){}
        } else if(s.uri){
          pendingSkin={nickname:null, skin_url:s.uri, local_png:s.uri, imported:true, fileName:(s.name||'skin.png')};
          if(el.skinBodyImg) el.skinBodyImg.src=s.uri;
          if(el.skinPreview) el.skinPreview.hidden=false;
          setSkinStatus('выбран импортированный: введи ник выше и нажми «Использовать»');
        }
      });
      gallery.appendChild(div);
    });
    // сохраняем в localStorage для быстрого доступа
    try{
      const names=combined.slice(0,5).map(s=>s.nickname||s.name);
      localStorage.setItem('recentSkins', JSON.stringify(names));
    }catch(e){}
  }catch(e){}
}

// 20 автоповорот переключатель
(function(){
  const auto=document.getElementById('skinAutoRotate');
  if(auto) auto.addEventListener('change', ()=>{
    sfx.click();
    try{ if(skinViewer) skinViewer.autoRotate = auto.checked; }catch(e){}
    try{ localStorage.setItem('skinAutoRotate', auto.checked?'1':'0'); }catch(e){}
  });
  try{
    const v=localStorage.getItem('skinAutoRotate');
    if(v!==null && auto) auto.checked = v==='1';
  }catch(e){}
})();

// 14 экспорт .png
if(el.skinExportBtn){
  el.skinExportBtn.addEventListener('click', ()=>{
    sfx.click();
    if(!pendingSkin || !pendingSkin.skin_url){
      // пробуем взять с превью
      const img=document.getElementById('skinBodyImg');
      if(img && img.src) window.open(img.src, '_blank');
      return;
    }
    const url = pendingSkin.local_png || pendingSkin.skin_url;
    const a=document.createElement('a');
    a.href=url; a.download=(pendingSkin.nickname||'skin')+'.png';
    document.body.appendChild(a); a.click(); a.remove();
    // для file:// урлов — fallback через pywebview
    if(url.startsWith('file://')){
      try{ const api=window.pywebview?.api; if(api&&api.copy_clipboard) api.copy_clipboard(url).catch(()=>{}); }catch(e){}
    }
  });
}
// 13 элитра (в бандле skinview3d нет loadElytra — только loadCape; гардим оба)
(function(){
  const chk=document.getElementById('elytraCheck');
  if(chk) chk.addEventListener('change', ()=>{
    sfx.click();
    if(!pendingSkin) return;
    try{
      if(chk.checked){
        if(skinViewer && typeof skinViewer.loadElytra === 'function')
          skinViewer.loadElytra(pendingSkin.cape_url||pendingSkin.cape_urls?.minotar||pendingSkin.skin_url);
        else if(skinViewer && typeof skinViewer.loadCape === 'function' && pendingSkin.cape_url)
          skinViewer.loadCape(pendingSkin.cape_url);
      } else {
        if(skinViewer && typeof skinViewer.loadElytra === 'function')
          skinViewer.loadElytra(null);
        if(skinViewer && typeof skinViewer.loadCape === 'function' && pendingSkin.cape_url)
          skinViewer.loadCape(pendingSkin.cape_url);
      }
    }catch(e){}
  });
})();

// 12 редактор 64×64
(function(){
  const cv=document.getElementById('skinEditCanvas');
  const col=document.getElementById('skinEditColor');
  const clr=document.getElementById('skinEditClear');
  const exp=document.getElementById('skinEditExport');
  if(!cv) return;
  const ctx=cv.getContext('2d');
  ctx.imageSmoothingEnabled=false;
  // шахматка уже в CSS, заливка прозрачным
  ctx.clearRect(0,0,64,64);
  let down=false, color='#ff0000';
  if(col) col.addEventListener('input', ()=>{ color=col.value; });
  function pos(e){
    const r=cv.getBoundingClientRect();
    const x=Math.floor((e.clientX - r.left) / (r.width/64));
    const y=Math.floor((e.clientY - r.top) / (r.height/64));
    return {x:Math.max(0,Math.min(63,x)), y:Math.max(0,Math.min(63,y))};
  }
  function draw(e){
    const {x,y}=pos(e);
    ctx.fillStyle=color;
    ctx.fillRect(x,y,1,1);
  }
  cv.addEventListener('pointerdown', e=>{ down=true; cv.setPointerCapture(e.pointerId); draw(e); sfx.click(); });
  cv.addEventListener('pointermove', e=>{ if(down) draw(e); });
  cv.addEventListener('pointerup', e=>{ down=false; try{cv.releasePointerCapture(e.pointerId);}catch(e){} });
  if(clr) clr.addEventListener('click', ()=>{ sfx.click(); ctx.clearRect(0,0,64,64); });
  if(exp) exp.addEventListener('click', ()=>{
    sfx.click();
    const data=cv.toDataURL('image/png');
    const a=document.createElement('a');
    a.href=data; a.download='edited_skin.png';
    document.body.appendChild(a); a.click(); a.remove();
    // сразу в импорт
    const img=document.getElementById('skinBodyImg');
    if(img){ img.src=data; document.getElementById('skinPreview').hidden=false; }
    pendingSkin={nickname:null, skin_url:data, local_png:data, slim:false, imported:true, fileName:'edited_skin.png'};
    const b3=document.getElementById('skin3dBtn'); if(b3) b3.hidden=false;
    setSkinStatus('экспортировано из редактора');
  });
  // загрузка текущего скина в редактор при показе
  // клик по превью загружает в редактор
  const prev=document.getElementById('skinPreview');
  if(prev) prev.addEventListener('dblclick', ()=>{
    const img=document.getElementById('skinBodyImg');
    if(!img || !img.src) return;
    const im=new Image(); im.crossOrigin='anonymous';
    im.onload=()=>{ ctx.clearRect(0,0,64,64); ctx.drawImage(im,0,0,64,64); sfx.whoosh(); };
    im.onerror=()=>{};
    im.src=img.src;
  });
})();

if(el.skinUseBtn){
  el.skinUseBtn.addEventListener('click', async () => {
    if(!pendingSkin) return;
    // импортированный/отредактированный PNG нельзя привязать через lookup —
    // нужен реальный ник из поля скина
    if(pendingSkin.imported && !pendingSkin.nickname){
      const typed = ((el.skinNick && el.skinNick.value) || '').trim();
      if(!/^[A-Za-z0-9_]{3,16}$/.test(typed)){
        setSkinStatus('введи ник лицензионного скина выше и нажми «Показать», затем «Использовать»');
        if(el.skinNick) el.skinNick.focus();
        return;
      }
      setSkinStatus('ищем скин ника «'+typed+'»…');
      try{
        const api0 = window.pywebview && window.pywebview.api;
        const lk = api0 && api0.skin_lookup ? await api0.skin_lookup(typed) : null;
        if(!lk || !lk.ok){ setSkinStatus((lk && lk.error) || 'не получилось найти скин'); return; }
        pendingSkin = lk;
      } catch(e){ setSkinStatus('ошибка: ' + e); return; }
    }
    el.skinUseBtn.disabled = true;
    setSkinStatus('сохраняем…');
    try{
      const api = window.pywebview && window.pywebview.api;
      const res = api && api.skin_use ? await api.skin_use(pendingSkin.nickname) : pendingSkin;
      if(res && res.ok){
        settings.skinNick = res.nickname;
        persistAll();
        if(el.skinNick) el.skinNick.value = res.nickname;
        refreshNickAvatar();
        setSkinStatus(`скин «${res.nickname}» выбран. В игре введи /skin set ${res.nickname} (если сервер поддерживает)`);
        if(el.skinUseBtn) el.skinUseBtn.hidden = true; refreshGallery();
      } else {
        setSkinStatus((res && res.error) || 'не получилось сохранить скин');
      }
    } catch(e){
      setSkinStatus('ошибка: ' + e);
    } finally{
      el.skinUseBtn.disabled = false;
    }
  });
}

// ---------- ресурспаки ----------
async function loadResourcePacks(){
  if(!el.rpList) return;
  try{
    const api = window.pywebview && window.pywebview.api;
    if(!api || !api.resource_packs) return;
    const res = await api.resource_packs();
    if(!res || !res.ok) return;
    const packs = res.packs || [];
    el.rpList.innerHTML = '';
    if(!packs.length){
      el.rpList.innerHTML = '<small style="color:var(--ink-faint);padding:4px 8px;">Пусто — загрузите .zip ниже</small>';
      return;
    }
    packs.forEach(p => {
      const item = document.createElement('div');
      item.className = 'rp-item';
      const nameEl = document.createElement('span');
      nameEl.className = 'rp-item-name';
      nameEl.textContent = p.name;
      nameEl.title = p.name;
      const sizeEl = document.createElement('span');
      sizeEl.className = 'rp-item-size';
      sizeEl.textContent = fmtMb(p.size);
      const removeBtn = document.createElement('button');
      removeBtn.className = 'rp-item-remove';
      removeBtn.textContent = '✕';
      removeBtn.title = 'Удалить';
      removeBtn.addEventListener('click', async () => {
        try{
          await api.remove_resource_pack(p.name);
          loadResourcePacks();
        } catch(e){}
      });
      item.appendChild(nameEl);
      item.appendChild(sizeEl);
      item.appendChild(removeBtn);
      el.rpList.appendChild(item);
    });
  } catch(e){}
}

function fmtMb(bytes){
  bytes = Number(bytes) || 0;
  if(bytes < 1024) return bytes + ' Б';
  const kb = bytes / 1024;
  if(kb < 1024) return kb.toFixed(1) + ' КБ';
  const mb = kb / 1024;
  return mb >= 1024 ? (mb / 1024).toFixed(1) + ' ГБ' : mb.toFixed(1) + ' МБ';
}

if(el.rpInstallBtn){
  el.rpInstallBtn.addEventListener('click', async () => {
    const url = el.rpUrl ? el.rpUrl.value.trim() : '';
    if(!url){
      if(el.rpUrl) el.rpUrl.focus();
      return;
    }
    el.rpInstallBtn.disabled = true;
    el.rpInstallBtn.textContent = 'загрузка\u2026';
    try{
      const api = window.pywebview && window.pywebview.api;
      if(api && api.install_resource_pack){
        const name = url.split('/').pop().split('?')[0] || 'pack.zip';
        const res = await api.install_resource_pack(url, name);
        if(res && res.ok){
          el.rpUrl.value = '';
          loadResourcePacks();
        } else {
          setError('Ошибка установки ресурспака: ' + (res && res.error ? res.error : 'неизвестно'));
        }
      }
    } catch(e){
      setError('Ошибка: ' + e);
    } finally {
      el.rpInstallBtn.disabled = false;
      el.rpInstallBtn.textContent = 'Установить';
    }
  });
}

// 21 скриншоты карусель на Вид
let scrShots=[], scrIdx=0;
async function refreshScreenshots(){
  const img=document.getElementById('scrImg');
  const hint=document.getElementById('scrHint');
  if(!img) return;
  try{
    const api=window.pywebview&&window.pywebview.api;
    const res= api && api.get_screenshots ? await api.get_screenshots() : null;
    scrShots=(res&&res.shots)||[];
    scrIdx=0;
    if(!scrShots.length){ img.removeAttribute('src'); img.alt='пусто'; if(hint) hint.textContent='папка screenshots/ — пусто (сделай F2 в игре)'; return; }
    if(hint) hint.textContent=`${scrShots.length} скриншотов · ${scrShots[0].name}`;
    showShot(0);
  }catch(e){ if(hint) hint.textContent='ошибка: '+e; }
}
function showShot(i){
  if(!scrShots.length) return;
  scrIdx=(i+scrShots.length)%scrShots.length;
  const s=scrShots[scrIdx];
  const img=document.getElementById('scrImg');
  const hint=document.getElementById('scrHint');
  if(img) img.src=s.uri;
  if(hint) hint.textContent=`${scrIdx+1}/${scrShots.length} · ${s.name} · ${s.size ? (s.size/1024).toFixed(0)+' КБ' : ''}`;
}
(function(){
  const prev=document.getElementById('scrPrev');
  const next=document.getElementById('scrNext');
  const refresh=document.getElementById('scrRefresh');
  const openBtn=document.getElementById('scrOpenFolder');
  const img=document.getElementById('scrImg');
  if(prev) prev.addEventListener('click', ()=>{ sfx.click(); showShot(scrIdx-1); });
  if(next) next.addEventListener('click', ()=>{ sfx.click(); showShot(scrIdx+1); });
  if(refresh) refresh.addEventListener('click', ()=>{ sfx.click(); refreshScreenshots(); });
  if(openBtn) openBtn.addEventListener('click', async ()=>{ sfx.click(); try{ const api=window.pywebview&&window.pywebview.api; if(api&&api.open_screenshots_folder) await api.open_screenshots_folder(); }catch(e){} });
  if(img) img.addEventListener('click', ()=>{ if(scrShots[scrIdx]) window.open(scrShots[scrIdx].uri, '_blank'); });
})();

// ---------- мониторинг игры ----------
let monitorInterval = null;

async function pollGameStats(){
  if(document.hidden) return;
  if(pollGameStats._inflight) return; // ответы не должны обгонять друг друга
  pollGameStats._inflight = true;
  try{
    const api = window.pywebview?.api;
    if(!api || !api.game_stats) return;
    const res = await api.game_stats();
    try{ if(window.__hangCheck) window.__hangCheck(res); }catch(e){}
    if(!res || !res.running){
      if(el.monitorBlock) el.monitorBlock.hidden = true;
      if(el.monitorStatus) el.monitorStatus.textContent = 'не запущена';
      return;
    }
    if(el.monitorBlock) el.monitorBlock.hidden = false;
    el.monitorCpu.textContent = res.cpu != null ? res.cpu + '%' : '—';
    el.monitorMem.textContent = res.total_memory_mb != null ? res.total_memory_mb + ' МБ' : '—';
    // TPS данных нет у ванильного сервера — показываем прочерк, чтобы не дублировать игроков
    el.monitorTps.textContent = '—';
    el.monitorPlayers.textContent = (res.server_online != null && res.server_online_max)
      ? res.server_online + ' / ' + res.server_online_max
      : (res.server_online != null ? res.server_online + ' онлайн' : '—');
    if(el.monitorStatus) el.monitorStatus.textContent = 'работает';
  } catch(e){} finally {
    pollGameStats._inflight = false;
  }
}

// ---------- консоль ----------
window.appendConsole = function(line){
  const el2 = document.getElementById('consoleLog');
  if(!el2) return;
  // первая заглушка
  if(el2.textContent.startsWith('[00:00:00]')) el2.textContent = '';
  el2.textContent += (el2.textContent ? '\n' : '') + line;
  // лимит 2000 строк в DOM тоже
  const parts = el2.textContent.split('\n');
  if(parts.length > 2000) el2.textContent = parts.slice(-2000).join('\n');
  const auto = document.getElementById('consoleAutoScroll');
  if(!auto || auto.checked) el2.scrollTop = el2.scrollHeight;
};
window.setConsoleLog = function(lines){
  const el2 = document.getElementById('consoleLog');
  if(!el2) return;
  // уважаем чекбокс автопрокрутки и не дёргаем скролл если юзер читает выше
  const auto = document.getElementById('consoleAutoScroll');
  const nearBottom = (el2.scrollHeight - el2.scrollTop - el2.clientHeight) < 60;
  if(Array.isArray(lines)) el2.textContent = lines.join('\n') || '[пусто]';
  else el2.textContent = String(lines||'');
  if(!auto || auto.checked || nearBottom) el2.scrollTop = el2.scrollHeight;
};
async function refreshConsole(){
  try{
    const api=window.pywebview&&window.pywebview.api;
    if(api&&api.get_console_log){ const r=await api.get_console_log(); if(r&&r.lines) window.setConsoleLog(r.lines); }
  }catch(e){}
}
async function refreshLatest(){
  try{
    const api=window.pywebview&&window.pywebview.api;
    const elL=document.getElementById('latestLog');
    const hint=document.getElementById('crashHint');
    if(api&&api.get_latest_log){
      const r=await api.get_latest_log(200);
      if(elL) elL.textContent = r.text || '— нет файла logs/latest.log —';
      if(elL) elL.scrollTop = elL.scrollHeight;
    }
    if(api&&api.get_crash_reports && hint){
      const c=await api.get_crash_reports();
      if(c.reports && c.reports.length) hint.textContent = `краши: ${c.reports.length} · ${c.reports[0].name}`;
      else hint.textContent = '';
    }
  }catch(e){}
}
(function(){
  const copyBtn=document.getElementById('consoleCopy');
  const clearBtn=document.getElementById('consoleClear');
  const folderBtn=document.getElementById('consoleFolder');
  const crashBtn=document.getElementById('consoleCrashBtn');
  const latestBtn=document.getElementById('latestRefresh');
  const latestOpen=document.getElementById('latestOpen');
  if(copyBtn) copyBtn.addEventListener('click', async ()=>{
    sfx.click();
    const el2=document.getElementById('consoleLog');
    const txt=(el2&&el2.textContent)||'';
    try{
      if(navigator.clipboard) await navigator.clipboard.writeText(txt);
      else { const ta=document.createElement('textarea'); ta.value=txt; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); }
      const orig=copyBtn.textContent; copyBtn.textContent='Скопировано ✓'; setTimeout(()=>copyBtn.textContent=orig,1200);
      const api=window.pywebview&&window.pywebview.api; if(api&&api.copy_clipboard) api.copy_clipboard(txt).catch(()=>{});
    }catch(e){}
  });
  if(clearBtn) clearBtn.addEventListener('click', async ()=>{
    sfx.click();
    try{ const api=window.pywebview&&window.pywebview.api; if(api&&api.clear_console_log) await api.clear_console_log(); }catch(e){}
    const el2=document.getElementById('consoleLog'); if(el2) el2.textContent='[очищено]';
  });
  if(folderBtn) folderBtn.addEventListener('click', async ()=>{ sfx.click(); try{ const api=window.pywebview&&window.pywebview.api; if(api&&api.open_logs_folder) await api.open_logs_folder(); }catch(e){} });
  if(crashBtn) crashBtn.addEventListener('click', async ()=>{ sfx.click(); try{ const api=window.pywebview&&window.pywebview.api; if(api&&api.open_crash_folder) await api.open_crash_folder(); }catch(e){} });
  const gameDirBtn=document.getElementById('consoleGameDir');
  if(gameDirBtn) gameDirBtn.addEventListener('click', async ()=>{ sfx.click(); try{ const api=window.pywebview&&window.pywebview.api; if(api&&api.open_game_folder) await api.open_game_folder(); }catch(e){} });
  if(latestBtn) latestBtn.addEventListener('click', ()=>{ sfx.click(); refreshLatest(); });
  if(latestOpen) latestOpen.addEventListener('click', async ()=>{ sfx.click(); try{ const api=window.pywebview&&window.pywebview.api; if(api&&api.open_logs_folder) await api.open_logs_folder(); }catch(e){} });
  // копирование — только двойной клик (одиночный клик мешал выделять текст:
  // mouseup затирал буфер обмена пользователя всем логом)
  const cl=document.getElementById('consoleLog');
  if(cl) cl.addEventListener('dblclick', async ()=>{
    const txt=cl.textContent||'';
    if(!txt) return;
    try{ if(navigator.clipboard) await navigator.clipboard.writeText(txt); }catch(e){}
    showToast('Лог скопирован','ok');
  });
  cl&&cl.setAttribute('title','Двойной клик — скопировать весь лог');
  const expBtn=document.getElementById('consoleExport');
  if(expBtn) expBtn.addEventListener('click', async ()=>{
    sfx.click();
    try{
      const api=window.pywebview&&window.pywebview.api;
      const r=api&&api.export_logs ? await api.export_logs() : null;
      if(r&&r.ok){
        expBtn.textContent='Готово ✓';
        showToast('Логи: '+(r.path||'готово'),'ok');
        setTimeout(()=>expBtn.textContent='Экспорт логов (39)',1500);
        try{
          if(api&&api.open_exported_logs){
            const o=await api.open_exported_logs();
            if(o&&o.ok&&o.path) showToast('Папка: '+o.path,'ok');
          }
        }catch(e){}
      }
      else setError(r&&r.error||'не удалось экспортировать');
    }catch(e){ setError(String(e)); }
  });
})();

// 37 прогресс по этапам
window.setProgressStages = function(stages, active){
  const c=document.getElementById('progressStages');
  if(!c) return;
  if(!stages||!stages.length){ c.innerHTML=''; c.style.display='none'; return; }
  c.style.display='flex';
  c.innerHTML='';
  stages.forEach((s,i)=>{
    const d=document.createElement('span');
    d.textContent=s;
    d.style.padding='2px 8px';
    d.style.borderRadius='99px';
    d.style.border='1px solid '+(i===active?'rgba(167,139,250,.5)':'var(--line-soft)');
    d.style.background=i===active?'rgba(167,139,250,.18)':i<active?'rgba(167,139,250,.08)':'transparent';
    d.style.color=i===active?'var(--amber-bright)':'var(--ink-faint)';
    if(i<active) d.textContent='✓ '+s;
    c.appendChild(d);
  });
};
// 34 палитра Ctrl+K
(function(){
  const overlay=document.getElementById('palette');
  const input=document.getElementById('paletteInput');
  const list=document.getElementById('paletteList');
  if(!overlay||!input||!list) return;
  const cmds=[
    {id:'play', name:'Играть', hint:'Enter', act:()=>document.getElementById('playBtn')?.click()},
    {id:'repair', name:'Починить сборку', hint:'Прочее', act:()=>document.getElementById('repairBtn')?.click()},
    {id:'console', name:'Открыть Консоль', hint:'Консоль', act:()=>switchMainTab('console')},
    {id:'clear', name:'Очистить консоль', hint:'Ctrl+L', act:()=>document.getElementById('consoleClear')?.click()},
    {id:'export', name:'Экспорт логов', hint:'39', act:()=>document.getElementById('consoleExport')?.click()},
    {id:'logs', name:'Папка logs', hint:'', act:()=>{try{window.pywebview?.api?.open_logs_folder?.()}catch(e){}}},
    {id:'crash', name:'Папка крашей', hint:'', act:()=>{try{window.pywebview?.api?.open_crash_folder?.()}catch(e){}}},
    {id:'gamedir', name:'Папка игры', hint:'', act:()=>{try{window.pywebview?.api?.open_game_folder?.()}catch(e){}}},
    {id:'diag', name:'Проверить систему', hint:'', act:()=>{switchMainTab('extra'); document.getElementById('diagBtn')?.click()}},
    {id:'compact', name:'Компакт режим', hint:'Ctrl+M', act:()=>document.getElementById('compactBtn')?.click()},
    {id:'theme', name:'Сменить тему', hint:'', act:()=>document.getElementById('themeBtn')?.click()},
    {id:'main', name:'Вкладка Основное', hint:'', act:()=>switchMainTab('main')},
    {id:'launch', name:'Вкладка Запуск', hint:'', act:()=>switchMainTab('launch')},
    {id:'look', name:'Вкладка Вид', hint:'', act:()=>switchMainTab('look')},
    {id:'skin', name:'Вкладка Скин', hint:'', act:()=>switchMainTab('skin')},
    {id:'extra', name:'Вкладка Прочее', hint:'', act:()=>switchMainTab('extra')},
  ];
  let filtered=[...cmds], selIdx=0;
  function render(){
    const q=(input.value||'').toLowerCase();
    filtered = cmds.filter(c=>!q||c.name.toLowerCase().includes(q)||c.id.includes(q));
    list.innerHTML='';
    filtered.forEach((c,i)=>{
      const d=document.createElement('div');
      d.className='palette-item'+(i===selIdx?' active':'');
      d.innerHTML=`<span>${c.name}</span><small>${c.hint}</small>`;
      d.addEventListener('click', ()=>{ close(); c.act(); sfx.click(); });
      list.appendChild(d);
    });
    if(!filtered.length){
      const d=document.createElement('div'); d.className='palette-item'; d.textContent='ничего не найдено'; list.appendChild(d);
    }
  }
  function open(){
    overlay.hidden=false; input.value=''; selIdx=0; render(); input.focus(); sfx.click();
  }
  function close(){ overlay.hidden=true; }
  input.addEventListener('input', ()=>{ selIdx=0; render(); });
  input.addEventListener('keydown', (e)=>{
    if(e.key==='ArrowDown'){ e.preventDefault(); selIdx=Math.min(filtered.length-1, selIdx+1); render(); }
    else if(e.key==='ArrowUp'){ e.preventDefault(); selIdx=Math.max(0, selIdx-1); render(); }
    else if(e.key==='Enter'){ e.preventDefault(); const c=filtered[selIdx]; if(c){ close(); c.act(); sfx.click(); } }
    else if(e.key==='Escape'){ e.preventDefault(); close(); }
  });
  overlay.addEventListener('click', (e)=>{ if(e.target===overlay) close(); });
  document.addEventListener('keydown', (e)=>{
    if(e.ctrlKey && e.key.toLowerCase()==='k'){ e.preventDefault(); if(overlay.hidden) open(); else close(); }
    else if(e.key==='Escape' && !overlay.hidden){ e.preventDefault(); close(); }
  });
})();
// 35 горячие клавиши (остальные) — не срабатывают при вводе в полях
document.addEventListener('keydown', (e)=>{
  const t = e.target;
  const typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
  if(typing) return;
  if(e.key==='F5'){ e.preventDefault(); document.getElementById('playBtn')?.click(); }
  if(e.ctrlKey && e.key.toLowerCase()==='l'){ e.preventDefault(); document.getElementById('consoleClear')?.click(); }
  if(e.ctrlKey && e.key.toLowerCase()==='m'){ e.preventDefault(); document.getElementById('compactBtn')?.click(); }
});

// 13 мульти-ник
(function(){
  const inp=document.getElementById('nickname');
  const btn=document.getElementById('nickDropBtn');
  const hist=document.getElementById('nickHistory');
  if(!inp||!hist) return;
  async function getRecents(){
    let arr=[];
    try{
      if(settings && Array.isArray(settings.recentNicks)) arr=[...settings.recentNicks];
      // свежие из python
      try{
        const api=window.pywebview&&window.pywebview.api;
        if(api&&api.get_ui_settings){
          const d=await api.get_ui_settings();
          if(d&&Array.isArray(d.recentNicks)) arr=[...d.recentNicks, ...arr];
        }
      }catch(e){}
      try{
        const loc=JSON.parse(localStorage.getItem('recentNicks')||'[]');
        if(Array.isArray(loc)) arr=[...arr,...loc];
      }catch(e){}
    }catch(e){}
    const seen=new Set(); const out=[];
    for(const n of arr){ if(!seen.has(n) && /^[A-Za-z0-9_]{3,16}$/.test(n)){ seen.add(n); out.push(n);} if(out.length>=5) break; }
    return out;
  }
  async function render(){
    const rec=await getRecents();
    hist.innerHTML='';
    if(!rec.length){ hist.hidden=true; return; }
    rec.forEach(n=>{
      const d=document.createElement('div');
      d.className='nick-history-item';
      d.textContent=n;
      d.addEventListener('click', ()=>{ inp.value=n; hist.hidden=true; persistAll(); refreshNickAvatar(); sfx.click(); });
      hist.appendChild(d);
    });
    hist.hidden=false;
  }
  if(btn) btn.addEventListener('click', async (e)=>{
    e.stopPropagation();
    if(!hist.hidden){ hist.hidden=true; return; }
    await render();
    sfx.click();
  });
  document.addEventListener('click', (e)=>{
    if(!hist.contains(e.target) && e.target!==btn && e.target!==inp) hist.hidden=true;
  });
})();

// 14 детект зависания CPU 0% 30с — едет на общем опросе game_stats (см. ниже _sharedGameStats),
// чтобы не дёргать блокирующий socket-пинг дважды каждые 2 секунды
(function(){
  let zeroTicks=0;
  window.__hangCheck = function(s){
    try{
      if(!s||!s.running){ zeroTicks=0; const h=document.getElementById('hangHint'); if(h) h.hidden=true; return; }
      const cpu=Number(s.cpu||0);
      if(cpu===0){
        zeroTicks++;
        if(zeroTicks>=15){ // 15*2с =30с
          const hint=document.getElementById('hangHint');
          if(hint) hint.hidden=false;
        }
      } else {
        zeroTicks=0;
        const hint=document.getElementById('hangHint');
        if(hint) hint.hidden=true;
      }
    }catch(e){}
  };
  const killBtn=document.getElementById('hangKill');
  if(killBtn) killBtn.addEventListener('click', async ()=>{
    sfx.click();
    try{
      const api=window.pywebview&&window.pywebview.api;
      if(api&&api.kill_game){
        const r=await api.kill_game();
        if(r&&r.ok) showToast('Процесс убит','ok');
        else setError(r&&r.error||'не удалось убить');
      } else if(api&&api.restart_game){
        await api.restart_game();
        showToast('Процесс завершён','ok');
      }
    }catch(e){ setError(String(e)); }
  });
})();

// 15 sample уже в pollServerStatus — дополняем рендер ниже (патч внутри pollServerStatus)

// 21 шейдеры
async function loadShaderPacks(){
  const list=document.getElementById('shaderList');
  if(!list) return;
  try{
    const api=window.pywebview&&window.pywebview.api;
    const r=api&&api.shader_packs?await api.shader_packs():null;
    if(!r||!r.ok) return;
    list.innerHTML='';
    const packs=r.packs||[];
    if(!packs.length){ list.innerHTML='<small style="color:var(--ink-faint);padding:4px 8px;">Пусто — загрузите .zip ниже</small>'; return; }
    packs.forEach(p=>{
      const item=document.createElement('div'); item.className='rp-item';
      const n=document.createElement('span'); n.className='rp-item-name'; n.textContent=p.name; n.title=p.name;
      const sz=document.createElement('span'); sz.className='rp-item-size'; sz.textContent=p.size? (p.size/1024).toFixed(0)+' КБ' : '';
      const rm=document.createElement('button'); rm.className='rp-item-remove'; rm.textContent='✕';
      rm.addEventListener('click', async ()=>{ try{ await api.remove_shader_pack(p.name); loadShaderPacks(); }catch(e){}});
      item.appendChild(n); item.appendChild(sz); item.appendChild(rm); list.appendChild(item);
    });
  }catch(e){}
}
(function(){
  const btn=document.getElementById('shaderInstallBtn');
  if(!btn) return;
  btn.addEventListener('click', async ()=>{
    const urlEl=document.getElementById('shaderUrl');
    const url=urlEl&&urlEl.value.trim();
    if(!url) return;
    btn.disabled=true; btn.textContent='загрузка…';
    try{
      const api=window.pywebview&&window.pywebview.api;
      const name=url.split('/').pop().split('?')[0]||'pack.zip';
      const r=api&&api.install_shader_pack?await api.install_shader_pack(url,name):null;
      if(r&&r.ok){ if(urlEl) urlEl.value=''; loadShaderPacks(); } else setError(r&&r.error||'ошибка');
    }catch(e){ setError(String(e)); }
    btn.disabled=false; btn.textContent='Установить';
  });
})();

// 40 антивирус
(function(){
  const btn=document.getElementById('avCheckBtn');
  const hint=document.getElementById('avHint');
  if(!btn) return;
  btn.addEventListener('click', async ()=>{
    sfx.click();
    btn.disabled=true; btn.textContent='Проверяем…';
    try{
      const api=window.pywebview&&window.pywebview.api;
      const r=api&&api.check_antivirus?await api.check_antivirus():null;
      if(r){
        const txt=`Defender: ${r.defender_running?'активен':'не найден'} · исключения: ${r.excluded===true?'есть':r.excluded===false?'нет':'?'} — ${r.advice||''}`;
        if(hint) hint.textContent=txt;
      }
    }catch(e){ if(hint) hint.textContent=String(e); }
    btn.disabled=false; btn.textContent='Проверить Защитник';
  });
})();
// BlackList анти-чит
(function(){
  const btn=document.getElementById('cheatCheckBtn');
  const hint=document.getElementById('cheatHint');
  if(!btn) return;
  btn.addEventListener('click', async ()=>{
    sfx.click();
    btn.disabled=true; btn.textContent='Сканируем…';
    try{
      const api=window.pywebview&&window.pywebview.api;
      const jvm=document.getElementById('jvmArgs')?.value||'';
      const r=api&&api.check_cheats?await api.check_cheats(jvm):null;
      if(r){
        if(r.clean){
          if(hint) hint.textContent='Чисто ✓ — читов не найдено';
          hint.style.color='var(--amber-bright)';
          showToast('Чисто — читов нет','ok');
        } else {
          const txt=r.cheats.slice(0,3).join('; ');
          if(hint) { hint.textContent='Найдены читы: '+txt; hint.style.color='#ff6b5e'; }
          showToast('Читерство запрещено!','error');
        }
      }
    }catch(e){ if(hint) hint.textContent=String(e); }
    btn.disabled=false; btn.textContent='Проверить на читы';
  });
})();

// ---------- онбординг (7) ----------
(function(){
  const steps = [
    {t:'Добро пожаловать!', d:'Это DragonHorizon Launcher. Давай настроим за 30 секунд.'},
    {t:'Ник игрока', d:'Введи ник латиницей (3–16 символов). Скин подтянется автоматически.', action:()=>{ document.getElementById('nickname')?.focus(); }},
    {t:'Оперативная память', d:'Мы уже подобрали RAM под твой ПК. Можно оставить как есть или открыть настройки.', action:()=>{ openSettings(); }},
    {t:'Готово к полёту!', d:'Нажми «Играть» — сборка скачается автоматически. Удачной игры!'},
  ];
  let idx=0;
  const overlay = document.getElementById('onboarding');
  const title = document.getElementById('onbTitle');
  const text = document.getElementById('onbText');
  const prog = document.getElementById('onbProgress');
  const next = document.getElementById('onbNext');
  const skip = document.getElementById('onbSkip');
  function render(){ title.textContent=steps[idx].t; text.textContent=steps[idx].d; prog.style.width=((idx+1)/steps.length*100)+'%'; if(steps[idx].action) try{steps[idx].action()}catch(e){} if(idx===steps.length-1) next.textContent='Играть ✓'; else next.textContent='Далее →'; sfx.whoosh(); }
  function hide(){ if(overlay) overlay.hidden=true; try{localStorage.setItem('onboardingDone','1')}catch(e){} closeSettings(); }
  if(next) next.addEventListener('click', ()=>{ sfx.click(); if(idx<steps.length-1){ idx++; render(); } else hide(); });
  if(skip) skip.addEventListener('click', ()=>{ sfx.click(); hide(); });
  window.maybeShowOnboarding = async function(){
    let force=false;
    try{ force = !!localStorage.getItem('forceOnboarding'); if(force) localStorage.removeItem('forceOnboarding'); }catch(e){}
    try{ if(!force && localStorage.getItem('onboardingDone')) return; }catch(e){}
    let fresh=force;
    if(!fresh){
      try{ const api=window.pywebview&&window.pywebview.api; if(api&&api.has_ui_settings) fresh=!(await api.has_ui_settings()); else fresh=!hadLocalSettings; }catch(e){ fresh=!hadLocalSettings; }
    }
    if(!fresh) return;
    if(!overlay) return;
    overlay.hidden=false; render();
  };
})();

// ---------- вход через сайт ----------
// Лаунчер закрыт без учётки — та же, что на dragonhorizon.ru.
// Состояние и логин идут через Python (Api.auth_state / Api.login),
// токен хранится в .minecraft/launcher_auth.json.
const authOverlay = document.getElementById('authOverlay');
let _authDone = null; // колбэк, вызываемый после успешного входа

function setAuthError(msg){
  const box = document.getElementById('authError');
  if(!box) return;
  box.hidden = !msg;
  box.textContent = msg || '';
}

function applyAccount(st){
  const grp = document.getElementById('authGroup');
  if(grp) grp.hidden = !(st && st.logged_in);
  const nickEl = document.getElementById('authNick');
  const roleEl = document.getElementById('authRole');
  if(nickEl) nickEl.textContent = (st && st.username) || '—';
  if(roleEl){
    let t = (st && st.roleLabel) ? st.roleLabel : '';
    if(st && st.offline) t += (t ? ' · ' : '') + 'нет сети';
    roleEl.textContent = t;
  }
  // ник игры = ник аккаунта: поле пустое только при первом запуске
  if(st && st.username && el.nickname && !String(el.nickname.value||'').trim()){
    el.nickname.value = st.username;
    try{ localStorage.setItem(NICK_KEY, st.username); }catch(e){}
    refreshNickAvatar();
  }
}

function showAuth(err){
  if(!authOverlay) return;
  authOverlay.hidden = false;
  setAuthError(err || '');
  const u = document.getElementById('authUser');
  if(u) setTimeout(()=>{ try{ u.focus(); }catch(e){} }, 150);
}

function submitAuth(){
  const api = window.pywebview && window.pywebview.api;
  const u = document.getElementById('authUser');
  const p = document.getElementById('authPass');
  const btn = document.getElementById('authBtn');
  if(!api || !api.login || !u || !p) return;
  const name = u.value.trim();
  const pass = p.value;
  if(!name || !pass){ setAuthError('Введи ник и пароль'); return; }
  if(btn){ btn.disabled = true; btn.textContent = 'Проверяем…'; }
  setAuthError('');
  const done = ()=>{ if(btn){ btn.disabled = false; btn.textContent = 'Войти'; } };
  Promise.resolve(api.login(name, pass)).then(res=>{
    done();
    if(res && res.ok){
      p.value = '';
      showToast('Вошли как ' + (res.username || name), 'ok');
      if(authOverlay) authOverlay.hidden = true;
      applyAccount(res);
      if(_authDone){ const cb = _authDone; _authDone = null; cb(res); }
      else maybeShowOnboarding();
    } else {
      setAuthError((res && res.error) || 'Не получилось войти');
    }
  }).catch(()=>{
    done();
    setAuthError('Лаунчер не смог связаться с сайтом — проверь интернет');
  });
}

// Показывает оверлей входа, если сессии нет; резолвится, когда игрок вошёл
function ensureAuth(){
  return new Promise(resolve=>{
    const api = window.pywebview && window.pywebview.api;
    if(!api || !api.auth_state){ resolve(null); return; } // открыт в браузере
    Promise.resolve(api.auth_state()).then(st=>{
      if(st && st.logged_in){
        if(authOverlay) authOverlay.hidden = true;
        applyAccount(st);
        resolve(st);
        return;
      }
      _authDone = resolve;
      showAuth(st && st.error);
    }).catch(()=>{ resolve(null); });
  });
}

if(document.getElementById('authBtn')){
  document.getElementById('authBtn').addEventListener('click', submitAuth);
}
if(document.getElementById('authPass')){
  document.getElementById('authPass').addEventListener('keydown', e=>{
    if(e.key === 'Enter'){ e.preventDefault(); submitAuth(); }
  });
}
if(document.getElementById('authUser')){
  document.getElementById('authUser').addEventListener('keydown', e=>{
    if(e.key === 'Enter'){ e.preventDefault(); document.getElementById('authPass').focus(); }
  });
}
if(document.getElementById('authForgot')){
  document.getElementById('authForgot').addEventListener('click', ()=>{
    const api = window.pywebview && window.pywebview.api;
    if(api && api.open_site) api.open_site('/cabinet').catch(()=>{});
  });
}
if(document.getElementById('logoutBtn')){
  document.getElementById('logoutBtn').addEventListener('click', ()=>{
    const api = window.pywebview && window.pywebview.api;
    const go = ()=>{ try{ location.reload(); }catch(e){} };
    if(api && api.logout) Promise.resolve(api.logout()).then(go).catch(go);
    else go();
  });
}

let _pyReadyFired = false;
window.addEventListener('error', e=>{ console.warn('JS error', e.message); });
window.addEventListener('unhandledrejection', e=>{ console.warn('promise', e.reason); try{e.preventDefault();}catch(ex){} });
window.addEventListener('pywebviewready', () => {
  if(_pyReadyFired) return;
  _pyReadyFired = true;
  // Настройки нужны для RAM-слайдера — ждём их, остальное летит параллельно
  // одним пакетом вместо 6 последовательных roundtrip через мост.
  initPersistedState().finally(async () => {
    // вход — раньше всего: без учётки онбординг и запуск не показываем
    await ensureAuth();
    initSystemMemory();
    refreshNickAvatar();
    maybeShowOnboarding();
    const api = window.pywebview?.api;
    pollServerStatus();
    refreshOnlineHistory();
    refreshPlayStats();
    // версии в футере: какая лаунчер и какая сборка стоят (для саппорта)
    try{
      if(api?.versions) api.versions().then(v => {
        const foot = document.getElementById('footVer');
        if(!foot || !v) return;
        let t = 'лаунчер ' + (v.launcher || '?');
        if(v.build) t += ' · сборка ' + v.build;
        foot.textContent = t;
      }).catch(()=>{});
    }catch(e){}
    // тихая диагностика Java — ничего не показываем игроку, просто лог
    try{
      if(api?.check_java) api.check_java().then(res => {
        if(res && !res.ok) console.warn('Java:', res.message);
      }).catch(() => {});
    }catch(e){}
    // мягкая проверка версии Minecraft: не блокирует запуск, просто предупреждает,
    // что сервер уже обновился, а локально всё ещё стоит старая версия
    try{
      if(api?.check_for_update) api.check_for_update().then(res => {
        if(res && res.outdated){
          showVersionNotice(
            `Доступна новая версия Minecraft: ${res.available} (сейчас ${res.current}). ` +
            `Нажми «Играть», чтобы обновиться.`
          );
        }
      }).catch(() => {});
    }catch(e){}
    // автообновление самого лаунчера: тег релиза = версия, ассет = Setup exe.
    // Скачивание идёт в фоне, потом лаунчер сам закроется, установится молча
    // и откроется заново уже новой версии.
    try{
      if(api?.check_launcher_update) api.check_launcher_update().then(res => {
        if(!(res && res.needed)) return;
        if(res.game_running || res.busy){
          showVersionNotice(`Доступна версия лаунчера ${res.version} — он обновится при следующем запуске.`);
          return;
        }
        showToast(`Обновление лаунчера ${res.version} — качаю, окно перезапустится…`, 'ok');
        return api.update_launcher().then(r2 => {
          if(r2 && r2.ok){
            // поллинг прогресса: обновляем тост, чтобы не казалось что зависло
            let n = 0;
            const poll = setInterval(()=>{
              try{
                if(api.update_progress) api.update_progress().then(p=>{
                  if(p && p.active){
                    const pct = res.size ? Math.min(99, Math.round(p.size / res.size * 100)) : 0;
                    showToast(`Обновление лаунчера ${res.version} — скачано ${pct}%`, 'ok');
                  } else { clearInterval(poll); }
                }).catch(()=>{});
              }catch(e){}
              if(++n > 600) clearInterval(poll); // макс 10 мин
            }, 1000);
            return;
          }
          if(r2 && (r2.game_running || r2.busy)){
            showVersionNotice(`Лаунчер обновится до ${res.version} при следующем запуске.`);
          } else if(r2 && r2.error && !/нет обновления|уже/i.test(r2.error)){
            showToast('Обновление лаунчера: ' + r2.error, 'error');
          }
        });
      }).catch(() => {});
    }catch(e){}
    // загрузка темы
    if(api && api.get_theme){
      api.get_theme().then(t => {
        const theme = (t && typeof t === 'object' ? t.theme : t);
        if(theme){
          applyTheme(theme);
          if(el.themeSelect) el.themeSelect.value = theme;
        }
      }).catch(() => {});
    }
    // загрузка ресурспаков
    loadResourcePacks();
    loadShaderPacks();
    refreshGallery();
    refreshScreenshots();
    refreshConsole();
    refreshLatest();
  });
  setInterval(pollServerStatus, 15000);
  setInterval(refreshOnlineHistory, 60000);
  setInterval(refreshPlayStats, 60000);
  // мониторинг игры — каждые 2 секунды когда она запущена
  monitorInterval = setInterval(() => {
    pollGameStats();
  }, 2000);
  // консоль — подтягиваем новые строки раз в 2с когда игра бежит или вкладка открыта
  // (дешёвое сравнение: длина + последняя строка, а не join всего буфера)
  let _lastConsoleLen = -1, _lastConsoleTail = '';
  setInterval(()=>{
    const pane=document.getElementById('mtab-console');
    const isVisible=pane && !pane.hidden;
    const api=window.pywebview&&window.pywebview.api;
    if((isVisible || (el.monitorBlock && !el.monitorBlock.hidden)) && api && api.get_console_log){
      api.get_console_log().then(r=>{
        if(r&&r.lines&&r.lines.length){
          const tail = r.lines[r.lines.length-1] || '';
          if(r.lines.length !== _lastConsoleLen || tail !== _lastConsoleTail){
            _lastConsoleLen = r.lines.length;
            _lastConsoleTail = tail;
            window.setConsoleLog(r.lines);
          }
        }
      }).catch(()=>{});
    }
  }, 2000);
});
// fallback для открытия в браузере без pywebview (file://)
setTimeout(()=>{
  if(!_pyReadyFired && !window.pywebview){
    window.dispatchEvent(new Event('pywebviewready'));
  }
}, 900);

// ---------- полноэкранный режим / разворачивание ----------
async function toggleFullscreen(){
  sfx.click();
  // сначала пробуем нативный browser fullscreen (работает и в браузере)
  try{
    if(!document.fullscreenElement){
      await document.documentElement.requestFullscreen().catch(()=>{});
      document.documentElement.classList.add('is-fullscreen');
    } else {
      await document.exitFullscreen().catch(()=>{});
      document.documentElement.classList.remove('is-fullscreen');
    }
  }catch(e){}
  // затем просим pywebview развернуть окно (maximize / fullscreen)
  try{
    const api=window.pywebview&&window.pywebview.api;
    if(api&&api.toggle_fullscreen) await api.toggle_fullscreen();
  }catch(e){}
  // пересчитать canvas угольков под новый размер
  setTimeout(()=>{ try{ window.dispatchEvent(new Event('resize')); }catch(e){} }, 300);
}
if(el.fullscreenBtn) el.fullscreenBtn.addEventListener('click', toggleFullscreen);
document.addEventListener('keydown', (e)=>{
  if(e.key==='F11'){
    e.preventDefault();
    toggleFullscreen();
  }
  if(e.key==='Escape' && document.fullscreenElement){
    document.documentElement.classList.remove('is-fullscreen');
  }
});
document.addEventListener('fullscreenchange', ()=>{
  const on = !!document.fullscreenElement;
  document.documentElement.classList.toggle('is-fullscreen', on);
});

// Canvas в свернутом окне не рисуем — rAF в фоне всё равно жрёт CPU,
// а угольки никто не видит. При возврате — по текущим настройкам.
document.addEventListener('visibilitychange', () => {
  if(document.hidden){ embersControl.stop(); }
  else { try{ applyMotionPreference(settings); } catch(e){} }
});
