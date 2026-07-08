from __future__ import annotations

import json
import socket
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .bootstrap import bootstrap_remote_config, check_remote_registration
from .config import load_config, save_config
from .db import Database
from .dev_seed import DevSeeder, apply_dev_defaults, result_to_text
from .logging_store import LogStore
from .outbox import Outbox
from .state import JsonState
from .sync_engine import SyncEngine


PIN = "210189"


HTML = """<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Revend Sync New</title>
  <style>
    :root {
      --bg: oklch(0.955 0.006 248);
      --chrome: oklch(0.205 0.020 250);
      --chrome-2: oklch(0.255 0.022 250);
      --surface: oklch(0.998 0.001 250);
      --surface-raised: oklch(0.976 0.004 248);
      --surface-soft: oklch(0.940 0.006 248);
      --ink: oklch(0.205 0.018 252);
      --muted: oklch(0.420 0.018 252);
      --line: oklch(0.865 0.010 252);
      --primary: oklch(0.620 0.145 78);
      --primary-strong: oklch(0.485 0.128 78);
      --primary-soft: oklch(0.915 0.055 78);
      --ok: oklch(0.470 0.130 153);
      --ok-soft: oklch(0.932 0.052 153);
      --bad: oklch(0.515 0.170 28);
      --bad-soft: oklch(0.930 0.050 28);
      --warn: oklch(0.590 0.145 64);
      --warn-soft: oklch(0.940 0.060 72);
      --shadow: 0 12px 28px oklch(0.210 0.020 252 / 0.08);
      --shadow-strong: 0 20px 54px oklch(0.120 0.018 252 / 0.18);
    }
    * { box-sizing: border-box; }
    html { min-width:100%; min-height:100%; }
    body {
      margin:0;
      min-width:100%;
      min-height:100vh;
      overflow:hidden;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }
    .shell { height:100vh; width:100%; display:grid; grid-template-columns:280px minmax(0, 1fr); overflow:hidden; }
    header {
      background: linear-gradient(135deg, var(--chrome), var(--chrome-2));
      color:#fff;
      padding:22px;
      display:flex;
      flex-direction:column;
      justify-content:flex-start;
      align-items:stretch;
      gap:16px;
      height:100vh;
      border-right:1px solid oklch(1 0 0 / 0.10);
      box-shadow: 16px 0 40px oklch(0.120 0.018 252 / 0.22);
    }
    h1 { font-size:21px; margin:0; letter-spacing:0; line-height:1.1; }
    h2 { margin:0 0 14px; font-size:15px; line-height:1.2; }
    .subhead { margin:5px 0 0; color:oklch(0.875 0.018 250); font-size:13px; }
    .state-pill { margin-top:auto; padding:12px 13px; border-radius:14px; background:oklch(1 0 0 / 0.10); border:1px solid oklch(1 0 0 / 0.16); font-weight:800; white-space:normal; box-shadow: inset 0 1px 0 oklch(1 0 0 / 0.08); line-height:1.35; }
    .state-pill.ok { background:oklch(0.520 0.120 153 / 0.20); border-color:oklch(0.720 0.130 153 / 0.32); }
    .state-pill.warn { background:oklch(0.620 0.140 64 / 0.22); border-color:oklch(0.780 0.130 72 / 0.36); }
    .state-pill.bad { background:oklch(0.560 0.160 28 / 0.22); border-color:oklch(0.740 0.150 28 / 0.34); }
    .workspace { min-width:0; height:100vh; overflow:auto; }
    main {
      margin:0;
      width:100%;
      max-width:none;
      padding:20px;
      flex:1;
    }
    section { width:100%; background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:18px; box-shadow:var(--shadow); }
    .hero {
      background: linear-gradient(135deg, oklch(0.240 0.024 250), oklch(0.295 0.026 250));
      color:#fff;
      border:1px solid oklch(1 0 0 / 0.10);
      border-radius:14px;
      padding:32px;
      box-shadow:var(--shadow-strong);
      display:grid;
      grid-template-columns: minmax(0, 1fr) minmax(260px, .42fr);
      gap:24px;
      align-items:center;
      min-height:clamp(220px, 30vh, 340px);
    }
    .hero .label { color:oklch(0.840 0.020 250); }
    .machine-title { font-size:44px; font-weight:850; margin:0 0 10px; overflow-wrap:anywhere; letter-spacing:0; line-height:1.03; }
    .machine-meta { color:oklch(0.875 0.016 250); font-size:15px; overflow-wrap:anywhere; max-width:72ch; line-height:1.45; }
    .hero-side { display:grid; gap:12px; }
    .heartbeat-badge { border:1px solid oklch(1 0 0 / 0.14); background:oklch(1 0 0 / 0.08); border-radius:12px; padding:20px; min-width:260px; min-height:138px; display:flex; flex-direction:column; justify-content:center; box-shadow: inset 0 1px 0 oklch(1 0 0 / 0.06); }
    .heartbeat-badge strong { display:block; margin-top:8px; font-size:16px; overflow-wrap:anywhere; line-height:1.35; }
    .mini-stack { display:grid; gap:8px; }
    .mini-row { display:flex; justify-content:space-between; align-items:center; gap:12px; padding:10px 12px; border:1px solid oklch(1 0 0 / 0.12); border-radius:12px; background:oklch(1 0 0 / 0.065); color:oklch(0.900 0.014 250); font-size:12px; }
    .mini-row strong { color:#fff; font-size:13px; text-align:right; overflow-wrap:anywhere; }
    .quick-signals { display:flex; flex-wrap:wrap; gap:10px; margin-top:20px; }
    .signal { display:inline-flex; align-items:center; gap:9px; max-width:min(100%, 460px); border:1px solid oklch(1 0 0 / 0.16); border-radius:999px; padding:9px 12px; background:oklch(1 0 0 / 0.09); color:#fff; font-size:13px; font-weight:800; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .dot { width:9px; height:9px; border-radius:50%; background:var(--warn); box-shadow:0 0 0 3px oklch(0.760 0.100 77 / 0.18); flex:none; }
    .signal.ok .dot { background:var(--ok); box-shadow:0 0 0 3px oklch(0.650 0.130 153 / 0.16); }
    .signal.bad .dot { background:var(--bad); box-shadow:0 0 0 3px oklch(0.650 0.170 28 / 0.16); }
    .status-panel { min-height:0; display:flex; flex-direction:column; }
    .status-grid { width:100%; flex:1; min-height:0; display:grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); grid-auto-rows:minmax(132px, auto); gap:14px; }
    .metric { position:relative; background:var(--surface-raised); border:1px solid var(--line); border-radius:12px; padding:18px 18px 18px 20px; min-height:132px; display:flex; flex-direction:column; justify-content:space-between; overflow:hidden; }
    .metric::before { content:""; position:absolute; inset:0 auto 0 0; width:5px; background:var(--warn); }
    .metric[data-state="ok"] { background:var(--ok-soft); border-color:oklch(0.820 0.050 153); }
    .metric[data-state="ok"]::before { background:var(--ok); }
    .metric[data-state="bad"] { background:var(--bad-soft); border-color:oklch(0.820 0.050 28); }
    .metric[data-state="bad"]::before { background:var(--bad); }
    .metric[data-state="warn"] { background:var(--warn-soft); border-color:oklch(0.820 0.060 72); }
    .metric[data-state="warn"]::before { background:var(--warn); }
    .label { color:var(--muted); font-size:12px; margin-bottom:10px; font-weight:750; }
    .value { font-weight:800; word-break:break-word; line-height:1.35; font-size:15px; }
    .ok { color:var(--ok); } .bad { color:var(--bad); } .warn { color:var(--warn); }
    .layout { width:100%; display:grid; grid-template-columns: minmax(480px, 1.35fr) repeat(2, minmax(320px, 1fr)); gap:16px; align-items:start; }
    .actions { display:grid; grid-template-columns:1fr; gap:9px; }
    button {
      width:100%;
      padding:11px 13px;
      border:1px solid var(--line);
      border-radius:11px;
      background:var(--surface);
      color:var(--ink);
      cursor:pointer;
      font-weight:800;
      text-align:left;
      transition: transform 160ms ease-out, border-color 160ms ease-out, background 160ms ease-out, box-shadow 160ms ease-out;
    }
    button:hover { transform:translateY(-1px); border-color:var(--primary); background:var(--primary-soft); box-shadow:0 10px 24px oklch(0.490 0.130 72 / 0.10); }
    button:focus-visible { outline:3px solid oklch(0.760 0.120 77 / 0.55); outline-offset:2px; }
    button.primary { background:var(--primary-strong); color:#fff; border-color:var(--primary-strong); text-align:center; }
    button.secondary { text-align:center; }
    button.danger { color:var(--bad); }
    pre { background:oklch(0.170 0.020 250); color:oklch(0.930 0.010 252); min-height:420px; max-height:calc(100vh - 220px); overflow:auto; padding:15px; border-radius:12px; white-space:pre-wrap; font-size:12px; line-height:1.55; border:1px solid oklch(1 0 0 / 0.08); }
    input { width:100%; padding:11px 12px; border:1px solid var(--line); border-radius:11px; font:inherit; color:var(--ink); background:var(--surface); }
    input:focus { outline:2px solid oklch(0.760 0.120 77 / 0.55); outline-offset:1px; }
    input[disabled] { background:var(--surface-raised); color:var(--muted); }
    label { color:var(--muted); font-size:13px; font-weight:750; }
    .form { display:grid; grid-template-columns: 190px 1fr; gap:11px 12px; align-items:center; }
    .hint { color:var(--muted); font-size:12px; grid-column:1 / -1; margin:2px 0 8px; }
    .section-gap { margin-top:18px; }
    .action-note { color:var(--muted); font-size:13px; margin:2px 0 12px; line-height:1.45; }
    .nav { display:grid; gap:8px; padding:0; width:100%; max-width:none; margin:8px 0 0; background:transparent; border:0; }
    .nav button { width:100%; color:#fff; border-color:oklch(1 0 0 / 0.12); background:oklch(1 0 0 / 0.07); padding:11px 12px; text-align:left; border-radius:12px; }
    .nav button.active { background:var(--primary-strong); border-color:var(--primary-strong); box-shadow:0 12px 28px oklch(0.120 0.040 80 / 0.22); }
    .screen { display:none; }
    .screen.active { display:block; }
    .readiness-grid { display:grid; grid-template-columns:repeat(4, minmax(180px, 1fr)); gap:14px; }
    .readiness-card { background:var(--surface); border:1px solid var(--line); border-radius:14px; padding:16px; min-height:118px; box-shadow:var(--shadow); display:flex; flex-direction:column; justify-content:space-between; }
    .readiness-card[data-state="ok"] { background:var(--ok-soft); border-color:oklch(0.820 0.050 153); }
    .readiness-card[data-state="bad"] { background:var(--bad-soft); border-color:oklch(0.820 0.050 28); }
    .readiness-card[data-state="warn"] { background:var(--warn-soft); border-color:oklch(0.820 0.060 72); }
    .readiness-top { display:flex; justify-content:space-between; align-items:center; gap:12px; color:var(--muted); font-size:12px; font-weight:800; }
    .readiness-value { font-size:17px; font-weight:850; line-height:1.25; overflow-wrap:anywhere; }
    #screen-status.active { min-height:calc(100vh - 40px); display:grid; grid-template-rows:auto auto minmax(300px, 1fr); gap:16px; }
    .secure { width:100%; min-height:calc(100vh - 40px); display:grid; place-items:center; text-align:center; padding:24px; }
    .secure-box { max-width:520px; width:min(520px, 100%); background:var(--surface); border:1px solid var(--line); border-radius:16px; padding:28px; box-shadow:var(--shadow); }
    .secure-box h2 { font-size:20px; margin-bottom:10px; }
    .secure-box p { color:var(--muted); line-height:1.45; margin:0; }
    .pin-row { display:grid; grid-template-columns:minmax(0, 1fr) 150px; gap:10px; margin-top:18px; max-width:none; }
    .pin-row input { min-width:0; height:46px; text-align:center; letter-spacing:.18em; font-weight:800; }
    .pin-row button { width:100%; text-align:center; }
    @media (prefers-reduced-motion: reduce) { button { transition:none; } button:hover { transform:none; } }
    @media (max-width: 1180px) { .layout { grid-template-columns:1fr 1fr; } .layout section:first-child { grid-column:1 / -1; } }
    @media (max-width: 1180px) { .readiness-grid { grid-template-columns:repeat(2, minmax(180px, 1fr)); } }
    @media (max-width: 980px) { body { overflow:auto; } .shell { height:auto; min-height:100vh; display:block; overflow:visible; } header { height:auto; padding:16px; box-shadow:none; border-right:0; border-bottom:1px solid oklch(1 0 0 / 0.10); } .workspace { height:auto; overflow:visible; } .state-pill { margin-top:4px; } main { padding:14px; } #screen-status.active { min-height:calc(100vh - 190px); grid-template-rows:auto auto minmax(420px, 1fr); gap:14px; } .hero { grid-template-columns:1fr; min-height:260px; padding:22px; } .machine-title { font-size:34px; } .status-grid { grid-auto-rows:minmax(128px, auto); } .layout { grid-template-columns:1fr; } .layout section:first-child { grid-column:auto; } .form { grid-template-columns:1fr; } .nav { grid-template-columns:repeat(3, minmax(0, 1fr)); margin-top:8px; } .nav button { text-align:center; } }
    @media (max-width: 640px) { .readiness-grid { grid-template-columns:1fr; } }
  </style>
</head>
<body>
<div class="shell">
<header>
  <div>
    <h1>Revend Sync New</h1>
    <p class="subhead">Lokalny panel kontroli synchronizacji maszyny</p>
  </div>
  <nav class="nav">
    <button id="nav-status" class="active" onclick="showScreen('status')">Stan</button>
    <button id="nav-logs" onclick="showProtected('logs')">Logi</button>
    <button id="nav-actions" onclick="showProtected('actions')">Akcje</button>
  </nav>
  <div class="state-pill" id="runState">Status: ...</div>
</header>
<div class="workspace">
<main id="screen-status" class="screen active">
  <div class="hero">
    <div>
      <p class="label">Stan rejestracji</p>
      <p class="machine-title" id="machineTitle">...</p>
      <p class="machine-meta" id="machineMeta">...</p>
      <div class="quick-signals" id="quickSignals"></div>
    </div>
    <div class="hero-side">
      <div class="heartbeat-badge">
        <span class="label">Ostatni heartbeat</span>
        <strong id="heartbeatHero">...</strong>
      </div>
      <div class="mini-stack">
        <div class="mini-row"><span>Baza</span><strong id="heroDb">...</strong></div>
        <div class="mini-row"><span>API</span><strong id="heroApi">...</strong></div>
        <div class="mini-row"><span>Internet</span><strong id="heroInternet">...</strong></div>
      </div>
    </div>
  </div>
  <div class="readiness-grid" id="readinessGrid"></div>
  <section class="status-panel">
    <h2>Stan maszyny i polaczen</h2>
    <div class="status-grid" id="metrics"></div>
  </section>
</main>
<main id="screen-logs" class="screen">
  <section class="secure" id="logs-lock">
    <div class="secure-box">
      <h2>Logi serwisowe</h2>
      <p>Logi zawieraja informacje techniczne o synchronizacji i sa zabezpieczone PIN-em serwisowym.</p>
      <div class="pin-row"><input id="logs-pin" type="password" placeholder="Wpisz PIN" onkeydown="pinEnter(event,'logs')"><button class="primary" onclick="unlock('logs')">Odblokuj</button></div>
    </div>
  </section>
  <section id="logs-content" style="display:none">
    <h2>Logi serwisowe</h2>
    <pre id="log"></pre>
  </section>
</main>
<main id="screen-actions" class="screen">
  <section class="secure" id="actions-lock">
    <div class="secure-box">
      <h2>Akcje serwisowe</h2>
      <p>Akcje zmieniaja stan lokalnego synchronizatora albo bazy. Wymagany PIN serwisowy.</p>
      <div class="pin-row"><input id="actions-pin" type="password" placeholder="Wpisz PIN" onkeydown="pinEnter(event,'actions')"><button class="primary" onclick="unlock('actions')">Odblokuj</button></div>
    </div>
  </section>
  <div id="actions-content" class="layout" style="display:none">
    <section>
      <h2>Konfiguracja</h2>
      <p class="action-note">Pierwszy start moze miec pusty numer maszyny. Rejestracja i pobranie konfiguracji korzystaja z osobnych URL-i.</p>
      <div class="form">
        <label>Machine ID</label><input id="machine_id">
        <label>URL rejestracji</label><input id="registration_url">
        <label>URL pobierania konfiguracji</label><input id="config_url">
        <label>API base URL</label><input id="base_url">
        <label>Token API</label><input id="token" type="password">
        <div class="hint">Baza danych maszyny jest statyczna i nie jest edytowana z panelu.</div>
        <label>DB host</label><input id="db_host" disabled>
        <label>DB port</label><input id="db_port" disabled>
        <label>DB nazwa</label><input id="db_database" disabled>
        <label>DB user</label><input id="db_username" disabled>
      </div>
    </section>
    <section>
      <h2>Akcje synchronizacji</h2>
      <p class="action-note">Najpierw sprawdz rejestracje, potem pobierz konfiguracje i uruchom watcher.</p>
      <div class="actions">
        <button class="primary" onclick="saveConfig()">Zapisz konfiguracje</button>
        <button class="secondary" onclick="act('refresh')">Odswiez status</button>
        <button onclick="act('check-registration')">Sprawdz rejestracje</button>
        <button onclick="act('fetch-config')">Pobierz konfiguracje</button>
        <button onclick="act('install-watcher')">Instaluj watcher</button>
        <button onclick="act('start-sync')">Start sync</button>
        <button class="danger" onclick="act('stop-sync')">Stop sync</button>
        <button onclick="act('heartbeat')">Heartbeat teraz</button>
      </div>
    </section>
    <section>
      <h2>Sztuczne dane</h2>
      <p class="action-note">Generator testowy do sprawdzania watcherow i wysylki bez prawdziwej transakcji.</p>
      <div class="actions">
        <button onclick="act('seed-transactions')">Dodaj transakcje</button>
        <button onclick="act('seed-bin')">Dodaj bin</button>
        <button onclick="act('seed-status')">Dodaj status</button>
        <button onclick="act('seed-all')">Dodaj wszystko</button>
      </div>
    </section>
  </div>
</main>
</div>
</div>
<script>
const PIN = '210189';
const unlocked = {
  logs: sessionStorage.getItem('logsUnlocked') === '1',
  actions: sessionStorage.getItem('actionsUnlocked') === '1'
};
const labels = {
  registration: "Rejestracja API", db: "Baza danych", internet: "Internet", machine_id: "Numer seryjny / Machine ID",
  machine_db: "Maszyna w lokalnej bazie", api: "API base URL", config_url: "URL konfiguracji", watcher: "Watcher / triggery",
  outbox: "Outbox", heartbeat: "Ostatni heartbeat"
};
const readiness = [
  ['registration', 'Rejestracja'],
  ['db', 'Baza danych'],
  ['internet', 'Internet'],
  ['heartbeat', 'Heartbeat']
];
function cls(v){
  v=String(v||"");
  if(v.includes("BLAD")||v.includes("OFFLINE")) return "bad";
  if(v.includes("NIE ZAREJESTROWANA")||v.includes("START")||v.includes("Pusty")||v.includes("Nie sprawdzono")||v.includes("Czeka")) return "warn";
  if(v.includes("OK")||v.includes("ONLINE")||v==="TAK"||v.includes("ZAREJESTROWANA")) return "ok";
  return "warn";
}
function signal(label, value){
  const state = cls(value);
  return `<span class="signal ${state}"><span class="dot"></span>${label}: ${value || 'brak'}</span>`;
}
async function load(){
  const r = await fetch('/api/status'); const data = await r.json();
  const runState = document.getElementById('runState');
  runState.textContent = 'Status: ' + data.run_state;
  runState.className = 'state-pill ' + cls(data.run_state);
  document.getElementById('machineTitle').textContent = data.config.machine_id ? data.config.machine_id : 'Nie zarejestrowana';
  document.getElementById('machineMeta').textContent = data.config.machine_id
    ? `${data.config.db_host}:${data.config.db_port}/${data.config.db_database} · ${data.metrics.internet || ''}`
    : 'Pierwszy start: numer maszyny jest pusty do czasu sprawdzenia rejestracji.';
  document.getElementById('heartbeatHero').textContent = data.metrics.heartbeat || 'Brak';
  document.getElementById('heroDb').textContent = data.metrics.db || 'Brak';
  document.getElementById('heroApi').textContent = data.metrics.api || 'Brak';
  document.getElementById('heroInternet').textContent = data.metrics.internet || 'Brak';
  document.getElementById('quickSignals').innerHTML =
    signal('DB', data.metrics.db) +
    signal('Rejestracja', data.metrics.registration) +
    signal('Internet', data.metrics.internet) +
    signal('Watcher', data.metrics.watcher);
  const ready = document.getElementById('readinessGrid'); ready.innerHTML = '';
  for (const [key,label] of readiness) {
    const state = cls(data.metrics[key]);
    const div = document.createElement('div');
    div.className = 'readiness-card';
    div.dataset.state = state;
    div.innerHTML = `<div class="readiness-top"><span>${label}</span><span class="${state}">${state.toUpperCase()}</span></div><div class="readiness-value ${state}">${data.metrics[key] || 'Brak'}</div>`;
    ready.appendChild(div);
  }
  const m = document.getElementById('metrics'); m.innerHTML = '';
  for (const [k,label] of Object.entries(labels)) {
    const div = document.createElement('div'); div.className='metric';
    const state = cls(data.metrics[k]);
    div.dataset.state = state;
    div.innerHTML = `<div class="label">${label}</div><div class="value ${state}">${data.metrics[k] || ''}</div>`;
    m.appendChild(div);
  }
  for (const [k,v] of Object.entries(data.config)) { const el=document.getElementById(k); if(el) el.value=v || ''; }
  if (unlocked.logs) await loadLogs();
}
function showScreen(name){
  for (const id of ['status','logs','actions']) {
    document.getElementById(`screen-${id}`).classList.toggle('active', id === name);
    document.getElementById(`nav-${id}`).classList.toggle('active', id === name);
  }
}
function showProtected(name){
  showScreen(name);
  renderLocks();
}
function renderLocks(){
  for (const name of ['logs','actions']) {
    const isOpen = unlocked[name];
    const lock = document.getElementById(`${name}-lock`);
    const content = document.getElementById(`${name}-content`);
    if (lock && content) {
      lock.style.display = isOpen ? 'none' : 'grid';
      content.style.display = isOpen ? (name === 'actions' ? 'grid' : 'block') : 'none';
    }
  }
}
async function unlock(name){
  const input = document.getElementById(`${name}-pin`);
  if (!input || input.value !== PIN) { alert('Nieprawidlowy PIN'); return; }
  unlocked[name] = true;
  sessionStorage.setItem(`${name}Unlocked`, '1');
  input.value = '';
  renderLocks();
  if (name === 'logs') await loadLogs();
}
function pinEnter(event, name){
  if (event.key === 'Enter') unlock(name);
}
async function loadLogs(){
  const r = await fetch('/api/logs', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({pin:PIN})});
  const data = await r.json();
  document.getElementById('log').textContent = (data.log || []).join('\\n');
}
function formConfig(){
  return {
    machine_id: document.getElementById('machine_id').value,
    registration_url: document.getElementById('registration_url').value,
    config_url: document.getElementById('config_url').value,
    base_url: document.getElementById('base_url').value,
    token: document.getElementById('token').value
  };
}
function servicePin(){ return unlocked.actions ? PIN : ''; }
async function post(payload){
  if (payload.action !== 'refresh') payload.pin = servicePin();
  const r = await fetch('/api/action', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  if (!r.ok) {
    const data = await r.json().catch(() => ({}));
    alert(data.error || 'Akcja wymaga PIN');
  }
  await load();
}
async function act(name){
  const payload = {action:name};
  if (name === 'check-registration' || name === 'fetch-config') payload.config = formConfig();
  await post(payload);
}
async function saveConfig(){ await post({action:'save-config', config: formConfig()}); }
setInterval(load, 3000); renderLocks(); load();
</script>
</body>
</html>"""


class WebState:
    def __init__(self):
        self.cfg = load_config()
        self.log = LogStore()
        self.json_state = JsonState()
        self.engine: SyncEngine | None = None
        self.messages: list[str] = []
        self.run_state = "Zatrzymany"

    def msg(self, text: str) -> None:
        self.messages.append(f"{datetime.now().strftime('%H:%M:%S')} {text}")
        self.messages = self.messages[-80:]


STATE = WebState()


def internet_online() -> bool:
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=3):
            return True
    except OSError:
        return False


def diagnostics() -> dict[str, Any]:
    cfg = load_config()
    STATE.cfg = cfg
    state = STATE.json_state.load()
    registration_status = state.get("last_registration_status")
    if not registration_status:
        registration_status = "START - czeka na rejestracje" if not cfg.machine_id else "Nie sprawdzono"
    metrics: dict[str, str] = {
        "registration": str(registration_status),
        "machine_id": cfg.machine_id or "Pusty - pierwszy start",
        "api": cfg.api.base_url or "BRAK",
        "config_url": cfg.api.config_url or "BRAK",
        "internet": "ONLINE" if internet_online() else "OFFLINE",
        "heartbeat": str(state.get("last_heartbeat_sent_at") or "Brak wyslanego heartbeat"),
    }
    db = Database(cfg.database)
    try:
        db.ping()
        metrics["db"] = f"OK: {cfg.database.host}:{cfg.database.port}/{cfg.database.database}"
        with db.connect() as conn:
            machine = None
            if cfg.machine_id:
                machine = Database.fetch_one(conn, "SELECT id, mid FROM machineinformation WHERE mid=%s LIMIT 1", (cfg.machine_id,))
            triggers = Database.fetch_all(
                conn,
                """
                SELECT TRIGGER_NAME FROM information_schema.TRIGGERS
                WHERE TRIGGER_SCHEMA = DATABASE()
                  AND TRIGGER_NAME IN ('sync_user_transaction_ai','sync_user_transaction_au','sync_empty_record_ai','sync_command_ai','sync_command_au')
                """,
            )
            try:
                counts = Outbox(db).counts()
            except Exception:
                counts = {}
            conn.commit()
        if not cfg.machine_id:
            metrics["machine_db"] = "Czeka na numer maszyny"
        else:
            metrics["machine_db"] = "TAK" if machine else "NIE - brak wpisu w machineinformation"
        metrics["watcher"] = f"{len(triggers)}/5 triggerow"
        metrics["outbox"] = ", ".join(f"{k}: {v}" for k, v in counts.items()) if counts else "pusto / brak tabeli"
    except Exception as exc:
        metrics["db"] = f"BLAD: {exc}"
        metrics["machine_db"] = "Nie sprawdzono"
        metrics["watcher"] = "Nie sprawdzono"
        metrics["outbox"] = "Nie sprawdzono"
    return {
        "run_state": STATE.run_state,
        "metrics": metrics,
        "config": {
            "machine_id": cfg.machine_id,
            "registration_url": cfg.api.registration_url,
            "config_url": cfg.api.config_url,
            "base_url": cfg.api.base_url,
            "token": cfg.api.token,
            "db_host": cfg.database.host,
            "db_port": str(cfg.database.port),
            "db_database": cfg.database.database,
            "db_username": cfg.database.username,
            "db_password": cfg.database.password,
        },
    }


def do_action(action: str) -> None:
    cfg = load_config()
    if action == "save-config":
        # This branch is handled by do_POST because it needs the submitted form payload.
        raise RuntimeError("Missing config payload")
    elif action == "install-watcher":
        Outbox(Database(cfg.database)).install(True)
        STATE.msg("Watcher zainstalowany.")
    elif action == "start-sync":
        if not STATE.engine:
            STATE.engine = SyncEngine(cfg, STATE.log, lambda event: STATE.msg(str(event)))
        STATE.engine.start()
        STATE.run_state = "Uruchomiony"
        STATE.msg("Synchronizacja wystartowala.")
    elif action == "stop-sync":
        if STATE.engine:
            STATE.engine.stop()
        STATE.run_state = "Zatrzymany"
        STATE.msg("Synchronizacja zatrzymana.")
    elif action == "heartbeat":
        engine = STATE.engine or SyncEngine(cfg, STATE.log, lambda event: STATE.msg(str(event)))
        engine.send_heartbeat()
        STATE.msg("Heartbeat wyslany.")
    elif action == "check-registration":
        attrs = check_remote_registration(cfg)
        status = "ZAREJESTROWANA" if attrs.get("registered") else "NIE ZAREJESTROWANA"
        if attrs.get("registered") and attrs.get("machineId"):
            cfg.machine_id = str(attrs["machineId"])
            save_config(cfg)
            STATE.cfg = cfg
        STATE.json_state.data["last_registration_status"] = status
        STATE.json_state.data["last_registration_checked_at"] = datetime.now(timezone.utc).isoformat()
        STATE.json_state.save()
        STATE.msg("Rejestracja sprawdzona: " + status)
    elif action == "fetch-config":
        if not cfg.machine_id:
            raise RuntimeError("Najpierw sprawdz rejestracje albo wpisz Machine ID.")
        new_cfg = bootstrap_remote_config(cfg, cfg.machine_id)
        save_config(new_cfg)
        STATE.cfg = new_cfg
        if STATE.engine:
            STATE.engine.cfg = new_cfg
            STATE.engine.db = Database(new_cfg.database)
            STATE.engine.http = STATE.engine.http.__class__(new_cfg.api)
            STATE.engine.outbox = Outbox(STATE.engine.db)
        STATE.json_state.data["last_registration_status"] = "ZAREJESTROWANA"
        STATE.json_state.save()
        STATE.msg("Konfiguracja pobrana z API.")
    elif action.startswith("seed"):
        seeder = DevSeeder(cfg)
        if action == "seed-transactions":
            result = seeder.seed_transactions(1)
        elif action == "seed-bin":
            result = seeder.seed_bins(1)
        elif action == "seed-status":
            result = seeder.seed_status()
        else:
            result = seeder.seed_all(3, 2, True)
        STATE.msg("Dodano sztuczne dane: " + result_to_text(result))


def save_config_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise RuntimeError("Invalid config payload")
    cfg = load_config()
    cfg.machine_id = str(payload.get("machine_id", cfg.machine_id)).strip()
    cfg.api.registration_url = str(payload.get("registration_url", cfg.api.registration_url)).strip()
    cfg.api.config_url = str(payload.get("config_url", cfg.api.config_url)).strip()
    cfg.api.base_url = str(payload.get("base_url", cfg.api.base_url)).strip().rstrip("/")
    cfg.api.token = str(payload.get("token", cfg.api.token))
    cfg = apply_dev_defaults(cfg)
    save_config(cfg)
    STATE.cfg = cfg
    if STATE.engine:
        STATE.engine.cfg = cfg
        STATE.engine.db = Database(cfg.database)
        STATE.engine.http = STATE.engine.http.__class__(cfg.api)
        STATE.engine.outbox = Outbox(STATE.engine.db)
    STATE.msg("Konfiguracja zapisana.")


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            self._send(200, json.dumps(diagnostics(), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/api/action", "/api/logs"}:
            self._send(404, b"{}", "application/json")
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        data = json.loads(self.rfile.read(length) or b"{}")
        if path == "/api/logs":
            if str(data.get("pin", "")) != PIN:
                self._send(403, json.dumps({"ok": False, "error": "Nieprawidlowy PIN"}, ensure_ascii=False).encode("utf-8"), "application/json")
                return
            self._send(200, json.dumps({"ok": True, "log": STATE.messages}, ensure_ascii=False).encode("utf-8"), "application/json")
            return
        try:
            action = str(data.get("action", ""))
            if action != "refresh" and str(data.get("pin", "")) != PIN:
                self._send(403, json.dumps({"ok": False, "error": "Akcja wymaga PIN"}, ensure_ascii=False).encode("utf-8"), "application/json")
                return
            if action == "save-config":
                save_config_payload(data.get("config", {}))
            else:
                if action in {"check-registration", "fetch-config"} and isinstance(data.get("config"), dict):
                    save_config_payload(data.get("config", {}))
                do_action(action)
            self._send(200, b'{"ok":true}', "application/json")
        except Exception as exc:
            STATE.msg("BLAD: " + str(exc))
            self._send(500, json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"), "application/json")

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    host = "127.0.0.1"
    port = 8787
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    STATE.msg("Panel uruchomiony: " + url)
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    print(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
