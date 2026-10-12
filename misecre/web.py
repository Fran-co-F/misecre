"""Web de control de misecre: sin dependencias ni build. Servida por el propio core en `/`.

Todo el contenido dinámico se inserta con textContent (nunca innerHTML) y el script lleva nonce:
una nota con `<script>` en el título no puede ejecutar nada.
"""

PAGE = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>misecre</title>
<style>
  :root { --bg:#fafaf9; --fg:#1c1917; --mut:#78716c; --card:#fff; --bd:#e7e5e4; --acc:#0f766e; --accfg:#fff;
          --trabajo:#2563eb; --personal:#c2410c; --ocio:#7c3aed; --warn:#b45309; --bad:#b91c1c; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0c0a09; --fg:#f5f5f4; --mut:#a8a29e; --card:#1c1917; --bd:#292524; --acc:#2dd4bf; --accfg:#042f2e;
            --trabajo:#60a5fa; --personal:#fb923c; --ocio:#a78bfa; --warn:#fbbf24; --bad:#f87171; } }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.45 system-ui,sans-serif; }
  main { max-width: 820px; margin: 0 auto; padding: 16px 16px 80px; }
  header { display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; }
  h1 { font-size: 21px; margin: 0; }
  h2 { font-size: 12px; text-transform: uppercase; letter-spacing:.06em; color:var(--mut); margin: 22px 0 8px; }
  .meta { color:var(--mut); font-size:13px; }
  .card { background:var(--card); border:1px solid var(--bd); border-radius:12px; overflow:hidden; }
  .row { display:flex; gap:10px; padding:10px 12px; border-top:1px solid var(--bd); align-items:flex-start; }
  .row:first-child { border-top:0; }
  .time { width:64px; flex:none; color:var(--mut); font-variant-numeric: tabular-nums; font-size:13px; padding-top:2px; }
  .dot { width:9px; height:9px; border-radius:50%; flex:none; margin-top:7px; background:var(--mut); }
  .trabajo .dot { background:var(--trabajo); } .personal .dot { background:var(--personal); }
  .ocio .dot { background:var(--ocio); }
  .body { flex:1; min-width:0; }
  .title { word-break: break-word; } .done .title { text-decoration: line-through; color: var(--mut); }
  .seq { color:var(--mut); font-size:12px; }
  .flag { color:var(--warn); font-size:12px; margin-top:2px; }
  .ok { color:var(--acc); font-size:12px; }
  .actions { display:flex; gap:4px; flex:none; }
  .chips { display:flex; gap:6px; flex-wrap:wrap; margin-top:6px; }
  .empty { padding:14px; color:var(--mut); }
  button, select, input[type=text], input[type=password] { font:inherit; color:var(--fg); background:var(--card);
      border:1px solid var(--bd); border-radius:8px; padding:6px 10px; }
  button { cursor:pointer; } button:hover { border-color:var(--acc); }
  button.primary { background:var(--acc); color:var(--accfg); border-color:var(--acc); font-weight:600; }
  button.icon { padding:4px 8px; } button.chip { padding:2px 10px; border-radius:99px; font-size:13px; }
  button:disabled { opacity:.5; cursor:default; }
  .tabs { display:flex; gap:4px; flex-wrap:wrap; margin:14px 0 4px; }
  .tabs button { border-radius:99px; } .tabs button.on { background:var(--acc); color:var(--accfg); border-color:var(--acc); }
  .bar { display:flex; gap:8px; margin-top:12px; }
  .bar input { flex:1; min-width:0; }
  #toast { position:fixed; left:50%; bottom:20px; transform:translateX(-50%); background:var(--fg); color:var(--bg);
           padding:8px 14px; border-radius:10px; opacity:0; transition:opacity .2s; pointer-events:none; max-width:90vw; }
  #toast.on { opacity:1; } #toast.err { background:var(--bad); color:#fff; }
  dialog { border:1px solid var(--bd); border-radius:14px; background:var(--card); color:var(--fg); width:min(440px,92vw); }
  dialog::backdrop { background:rgba(0,0,0,.45); }
  label { display:block; margin:10px 0 4px; font-size:13px; color:var(--mut); }
  dialog input, dialog select { width:100%; }
  .gridform { display:grid; gap:8px; grid-template-columns: 1fr auto auto; align-items:center; }
  .pill { font-size:12px; padding:2px 8px; border-radius:99px; border:1px solid var(--bd); color:var(--mut); }
  #login { display:none; margin-top:20px; padding:16px; }
  .rec { background:var(--bad); color:#fff; border-color:var(--bad); }
</style>
</head>
<body>
<main>
  <header>
    <h1 id="title">misecre</h1>
    <div>
      <button class="icon" id="undo" title="Deshacer la última acción">↩ Deshacer</button>
      <button class="icon" id="sync" title="Sincronizar ahora">🔄 Sync</button>
      <button class="icon" id="logout" title="Cerrar sesión en este dispositivo" hidden>Salir</button>
    </div>
  </header>
  <div class="meta" id="status"></div>

  <div id="login" class="card">
    <p><b>Entrá con el token de este dispositivo.</b><br>
       <span class="meta">Se crea en el servidor con <code>misecre token create &lt;nombre&gt;</code>.</span></p>
    <div class="bar"><input id="tok" type="password" placeholder="msc_…" autocomplete="off">
      <button class="primary" id="enter">Entrar</button></div>
    <label><input type="checkbox" id="remember" checked> Recordar en este dispositivo</label>
  </div>

  <div id="app" hidden>
    <div class="bar">
      <input type="text" id="quick" placeholder="Anotá algo: “reunión con Marcos el jueves a las 10”" maxlength="500">
      <button class="primary" id="add">Añadir</button>
      <button id="mic" title="Dictar por voz">🎙</button>
    </div>
    <div class="tabs" id="tabs"></div>
    <div id="content"></div>
  </div>
</main>

<dialog id="edit">
  <form method="dialog" id="editform">
    <h3 style="margin:0 0 4px">Editar <span id="e_seq" class="seq"></span></h3>
    <label>Título</label><input type="text" id="e_title" maxlength="200">
    <label>Cuándo (lenguaje natural: “el viernes a las 10”, “mañana”, “sin hora”)</label>
    <input type="text" id="e_when" placeholder="dejar vacío para no cambiar">
    <label>Duración (minutos)</label><input type="text" id="e_dur" inputmode="numeric" placeholder="sin cambios">
    <label>Categoría</label><select id="e_cat"></select>
    <label>Tipo</label><select id="e_type"><option>evento</option><option>tarea</option><option>nota</option></select>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
      <button type="button" id="e_cancel">Cancelar</button>
      <button type="submit" class="primary">Guardar</button>
    </div>
  </form>
</dialog>
<div id="toast"></div>

<script nonce="__NONCE__">
"use strict";
const NEEDS_TOKEN = __NEEDS_TOKEN__;
const DAYS = ["domingo","lunes","martes","miércoles","jueves","viernes","sábado"];
const ICON = {trabajo:"💼", personal:"🏠", ocio:"🎮"};
const $ = id => document.getElementById(id);
let token = "", cats = ["trabajo","personal","ocio"], tab = "hoy", editing = null, timer = null;
let agentOn = false, chatLog = [], chatBusy = false;

// ---------- almacenamiento del token (puede fallar en modo privado)
function loadTok(){ try { return localStorage.getItem("misecre_token") || sessionStorage.getItem("misecre_token") || ""; } catch(e){ return ""; } }
function saveTok(t, remember){ try { (remember ? localStorage : sessionStorage).setItem("misecre_token", t); } catch(e){} }
function clearTok(){ try { localStorage.removeItem("misecre_token"); sessionStorage.removeItem("misecre_token"); } catch(e){} }

// ---------- utilidades
function el(tag, props, ...kids){
  const n = document.createElement(tag);
  for (const [k,v] of Object.entries(props || {})) {
    if (k === "class") n.className = v; else if (k === "on") for (const [ev,fn] of Object.entries(v)) n.addEventListener(ev, fn);
    else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null) n.append(c.nodeType ? c : document.createTextNode(String(c)));
  return n;
}
function toast(msg, err){ const t=$("toast"); t.textContent=msg; t.className="on"+(err?" err":""); clearTimeout(toast._t);
  toast._t=setTimeout(()=>t.className="", err?4500:2200); }
async function api(path, opts={}){
  const headers = {...(opts.headers||{})};
  if (token) headers.Authorization = "Bearer " + token;
  if (opts.json !== undefined) { headers["Content-Type"]="application/json"; opts.body = JSON.stringify(opts.json); }
  const r = await fetch(path, {method: opts.method||"GET", headers, body: opts.body});
  if (r.status === 401) { showLogin("Token inválido o revocado."); throw new Error("auth"); }
  if (r.status === 429) { throw new Error("Demasiados intentos fallidos: esperá unos minutos."); }
  let data = null; try { data = await r.json(); } catch(e) {}
  if (!r.ok) throw new Error((data && data.detail) ? (typeof data.detail==="string"?data.detail:JSON.stringify(data.detail)) : "Error " + r.status);
  return data;
}
function hhmm(iso){ return iso.slice(11,16); }
function dayLabel(iso){ const d=new Date(iso.slice(0,10)+"T12:00"); return DAYS[d.getDay()].slice(0,3)+" "+d.getDate()+"/"+(d.getMonth()+1); }
function whenText(c, withDay){
  if (c.start) return (withDay ? dayLabel(c.start)+" " : "") + hhmm(c.start);
  if (c.day) return (withDay ? dayLabel(c.day)+" " : "") + (withDay ? "" : "todo el día");
  return "";
}
async function act(fn, okMsg){
  try { await fn(); if (okMsg) toast(okMsg); await refresh(); }
  catch(e){ if (e.message !== "auth") toast(e.message, true); }
}

// ---------- filas
function amPm(c){ const s=new Date(c.start); const pm=new Date(s); if (pm.getHours()<12) pm.setHours(pm.getHours()+12);
  const am=new Date(pm); am.setHours(am.getHours()-12); const f=d=>String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0");
  return [f(am), f(pm)]; }
function row(c, opts={}){
  const p = c.pending || [], chips = [];
  if (c.status !== "borrada" && p.includes("category")) {
    for (const k of cats) chips.push(el("button",{class:"chip", on:{click:()=>act(()=>api(`/capture/${c.id}/category`,{method:"POST",json:{category:k}}), k+" ✓")}},
      (ICON[k]||"•")+" "+k+(k===c.category?" ✓":"")));
  }
  if (c.status !== "borrada" && p.includes("hour") && c.start) {
    const [am,pm]=amPm(c);
    chips.push(el("button",{class:"chip",on:{click:()=>act(()=>api(`/capture/${c.id}/hour`,{method:"POST",json:{part:"am"}}))}},"🌅 "+am));
    chips.push(el("button",{class:"chip",on:{click:()=>act(()=>api(`/capture/${c.id}/hour`,{method:"POST",json:{part:"pm"}}))}},"🌆 "+pm));
  }
  if (c.status !== "borrada" && p.includes("time")) {
    chips.push(el("button",{class:"chip",on:{click:()=>{ const t=prompt("¿A qué hora? (ej: 18:30)"); if(t) act(()=>api(`/capture/${c.id}/time`,{method:"POST",json:{text:t}})); }}},"🕘 Ponerle hora"));
    chips.push(el("button",{class:"chip",on:{click:()=>act(()=>api(`/capture/${c.id}/all-day`,{method:"POST"}))}},"📅 Todo el día"));
  }
  let flag = null;
  if ((c.start||c.day) && c.status!=="borrada") {
    flag = c.in_calendar ? el("div",{class:"ok"},"📆 en el calendario")
         : c.calendar_off ? el("div",{class:"meta"},"🔔 solo avisos (esta categoría no va al calendario)")
         : el("div",{class:"flag"}, c.category ? "⚠️ no está en el calendario" : "❓ falta categoría para ir al calendario");
  }
  const actions = el("div",{class:"actions"});
  if (c.status === "borrada") {
    actions.append(el("button",{class:"icon",title:"Restaurar",on:{click:()=>act(()=>api(`/capture/${c.id}/restore`,{method:"POST"}),"Restaurada")}},"♻️"));
  } else {
    actions.append(
      el("button",{class:"icon",title:c.status==="hecha"?"Reabrir":"Marcar hecha",on:{click:()=>act(()=>api(`/capture/${c.id}`,{method:"PATCH",json:{status:c.status==="hecha"?"pendiente":"hecha"}}))}}, c.status==="hecha"?"↩":"✓"),
      el("button",{class:"icon",title:"Editar",on:{click:()=>openEdit(c)}},"✎"),
      el("button",{class:"icon",title:"Borrar",on:{click:()=>act(()=>api(`/capture/${c.id}`,{method:"DELETE"}),"A la papelera (↩ Deshacer)")}},"🗑"));
  }
  return el("div",{class:`row ${c.category||""} ${c.status==="hecha"?"done":""}`},
    el("span",{class:"time"}, whenText(c, opts.withDay)), el("span",{class:"dot"}),
    el("div",{class:"body"},
      el("div",{}, el("span",{class:"title"}, c.title), " ", el("span",{class:"seq"}, "#"+c.seq)),
      flag, chips.length ? el("div",{class:"chips"}, chips) : null),
    actions);
}
function section(title, items, opts){
  const box = el("div",{class:"card"});
  if (!items.length) box.append(el("div",{class:"empty"},"Nada."));
  for (const c of items) box.append(row(c, opts));
  return [el("h2",{},title), box];
}

// ---------- vistas
const TABS = [["hoy","Hoy"],["chat","💬 Chat"],["semana","Semana"],["tareas","Tareas"],["inbox","Sin categoría"],["papelera","Papelera"],["ajustes","Ajustes"]];
function drawTabs(counts){
  const t = $("tabs"); t.replaceChildren();
  for (const [k,label] of TABS) {
    const n = counts && counts[k] ? ` (${counts[k]})` : "";
    t.append(el("button",{class:tab===k?"on":"", on:{click:()=>{tab=k; refresh();}}}, label+n));
  }
}
async function refresh(){
  if (!token && NEEDS_TOKEN) return showLogin();
  let st;
  try { st = await api("/status"); } catch(e){ if (e.message!=="auth") toast(e.message,true); return; }
  $("login").style.display="none"; $("app").hidden=false; $("logout").hidden = !NEEDS_TOKEN;
  stream();                                                    // solo con un token ya validado
  cats = st.categories || cats;
  agentOn = !!st.agent;
  const d = new Date();
  $("title").textContent = `${DAYS[d.getDay()]} ${d.getDate()}/${d.getMonth()+1}`;
  $("status").textContent = `${st.pendientes} pendientes · ${st.en_calendario}/${st.con_fecha} con fecha están en el calendario`
    + (st.silenced ? ` · avisos en ${st.silenced}` : "") + (st.sin_categoria ? ` · ${st.sin_categoria} sin categoría` : "")
    + (st.last_sync ? ` · sync ${st.last_sync.slice(11,16)}` : "");
  const out = $("content");
  const dn = new Date();   // fecha LOCAL (toISOString() devuelve UTC y de 21 a 24 h en Argentina ya es mañana)
  const today = dn.getFullYear() + "-" + String(dn.getMonth()+1).padStart(2,"0") + "-" + String(dn.getDate()).padStart(2,"0");
  const isToday = c => (c.start||c.day||"").slice(0,10) === today;
  let nodes = [];
  if (tab === "hoy") {
    const week = await api("/agenda?days=1");
    nodes = [...section("Hoy", week), ...section("Tareas pendientes", (await api("/tasks")).filter(t=>!isToday(t)), {withDay:true})];
  } else if (tab === "chat") {
    nodes = chatView();
  } else if (tab === "semana") {
    const week = await api("/agenda?days=7");
    nodes = section("Próximos 7 días", week, {withDay:true});
  } else if (tab === "tareas") {
    nodes = section("Tareas pendientes", await api("/tasks"), {withDay:true});
  } else if (tab === "inbox") {
    nodes = section("Sin categoría", await api("/inbox"), {withDay:true});
  } else if (tab === "papelera") {
    nodes = section("Papelera", await api("/trash"), {withDay:true});
  } else {
    nodes = await settingsView();
  }
  drawTabs({inbox: st.sin_categoria});
  out.replaceChildren(...nodes);
}

function chatView(){
  const box = el("div",{class:"card",style:"padding:12px;display:flex;flex-direction:column;gap:8px;min-height:220px"});
  if (!chatLog.length) box.append(el("div",{class:"empty"},
    "Decime qué anotar o qué hiciste: “mete un recordatorio de comprar pan”, “ya fui a comprar pan”, “¿qué tengo mañana?”. Resuelvo con reglas y uso la IA solo si hace falta."));
  for (const m of chatLog) {
    const mine = m.who === "yo";
    box.append(el("div",{style:`align-self:${mine?"flex-end":"flex-start"};max-width:88%;padding:8px 12px;border-radius:12px;`
      + `border:1px solid var(--bd);white-space:pre-wrap;background:${mine?"var(--acc)":"var(--bg)"};color:${mine?"var(--accfg)":"var(--fg)"}`}, m.text));
  }
  if (chatBusy) box.append(el("div",{class:"meta"},"escribiendo…"));
  const inp = el("input",{type:"text",id:"chatin",placeholder:"Escribile al operador…",maxlength:"500",style:"flex:1;min-width:0"});
  const send = async()=>{
    const text = inp.value.trim(); if (!text || chatBusy) return;
    chatLog.push({who:"yo", text}); chatBusy = true; await refresh();
    try { const r = await api("/agent/chat",{method:"POST",json:{text}});
          chatLog.push({who:"bot", text:r.reply + (r.source==="ia" ? " 🤖" : "") + (r.learned||[]).map(x=>"\n"+x).join("")}); }
    catch(e){ if (e.message!=="auth") chatLog.push({who:"bot", text:"⚠️ "+e.message}); }
    chatBusy = false; await refresh();
    const n = $("chatin"); if (n) n.focus();
  };
  inp.addEventListener("keydown", e=>{ if (e.key==="Enter") send(); });
  const bar = el("div",{class:"bar"}, inp, el("button",{class:"primary",on:{click:send}},"Enviar"),
    el("button",{title:"Borrar la memoria del chat",on:{click:()=>act(async()=>{ await api("/agent/chat",{method:"DELETE"}); chatLog=[]; },"Memoria borrada")}},"🧹"));
  return [box, bar];
}

async function settingsView(){
  const s = await api("/settings");
  const nodes = [];
  // quién responde primero
  const modeSel = el("select",{});
  for (const [k,label] of Object.entries(s.modes || {})) modeSel.append(el("option",{value:k,selected:k===s.mode}, label));
  modeSel.addEventListener("change", ()=>act(()=>api("/settings/mode",{method:"POST",json:{mode:modeSel.value}}),"Guardado"));
  nodes.push(el("h2",{},"Quién responde primero"), el("div",{class:"card"}, el("div",{class:"row"},
    el("div",{class:"body"}, modeSel, el("div",{class:"meta"},"Las reglas son gratis e instantáneas; la IA entiende mejor lo libre y cuesta centavos.")))));
  // avisos por categoría
  const pol = el("div",{class:"card"});
  for (const c of cats) {
    const p = s.policies[c] || {before:[], digest_only:false};
    const inp = el("input",{type:"text", value:p.before.join(", "), placeholder:"minutos antes: 15, 5", style:"width:150px"});
    const dig = el("input",{type:"checkbox"}); dig.checked = p.digest_only;
    const save = el("button",{class:"icon",on:{click:()=>act(()=>api("/settings/policy",{method:"POST",json:{category:c,
        before:inp.value.split(/[ ,]+/).filter(Boolean).map(Number), digest_only:dig.checked}}),"Guardado")}},"Guardar");
    const calOn = el("input",{type:"checkbox"}); calOn.checked = (s.calendar||{})[c] !== false;
    calOn.addEventListener("change", ()=>act(()=>api("/settings/calendar",{method:"POST",json:{category:c,on:calOn.checked}}),
      calOn.checked ? "Va al calendario" : "Solo avisos (sin calendario)"));
    pol.append(el("div",{class:"row"}, el("span",{style:"width:110px"}, (ICON[c]||"•")+" "+c),
      el("div",{class:"body"}, inp, " ", el("label",{style:"display:inline;margin-left:8px"}, dig, " solo en el resumen"),
        el("div",{style:"margin-top:6px"}, el("label",{style:"display:inline"}, calOn, " va al calendario de Google"))), save));
  }
  nodes.push(el("h2",{},"Avisos"), pol);
  // silencio y pausa
  const q = el("input",{type:"text", value:s.quiet, style:"width:140px"});
  const quiet = el("div",{class:"card"}, el("div",{class:"row"},
    el("span",{style:"width:110px"},"🤫 Silencio"),
    el("div",{class:"body"}, q, " ", el("button",{class:"icon",on:{click:()=>act(()=>api("/settings/quiet",{method:"POST",json:{spec:q.value}}),"Guardado")}},"Guardar"),
      el("div",{class:"meta"},"Formato 23:00-07:00, o “off”."))),
    el("div",{class:"row"}, el("span",{style:"width:110px"},"⏸ Pausa"),
      el("div",{class:"body"},
        s.paused_until ? el("div",{class:"flag"},"En pausa hasta "+s.paused_until.slice(11,16)) : null,
        el("div",{class:"chips"},
          ...[30,120,480].map(m=>el("button",{class:"chip",on:{click:()=>act(()=>api("/settings/pause",{method:"POST",json:{minutes:m}}),"Pausado")}}, m<60?m+" min":(m/60)+" h")),
          el("button",{class:"chip",on:{click:()=>act(()=>api("/settings/pause",{method:"POST",json:{minutes:null}}),"Reanudado")}},"Reanudar")))));
  nodes.push(el("h2",{},"Silencio y pausa"), quiet);
  // sugerencias
  if (s.suggestions.length) {
    const box = el("div",{class:"card"});
    for (const g of s.suggestions) box.append(el("div",{class:"row"},
      el("div",{class:"body"}, "“"+g.word+"” → "+g.category+" ", el("span",{class:"seq"},`(${g.count} veces)`)),
      el("div",{class:"actions"},
        el("button",{class:"icon",on:{click:()=>act(()=>api("/suggestions",{method:"POST",json:{category:g.category,word:g.word,action:"accept"}}),"Regla agregada")}},"➕"),
        el("button",{class:"icon",on:{click:()=>act(()=>api("/suggestions",{method:"POST",json:{category:g.category,word:g.word,action:"reject"}}))}},"✖"))));
    nodes.push(el("h2",{},"Reglas sugeridas por tus correcciones"), box);
  }
  // reglas
  const rules = el("div",{class:"card"});
  for (const c of cats) {
    const add = el("input",{type:"text", placeholder:"nueva palabra", style:"width:150px"});
    const chips = el("div",{class:"chips"}, ...(s.rules[c]||[]).map(w =>
      el("button",{class:"chip",title:"Quitar",on:{click:()=>act(()=>api("/rules",{method:"POST",json:{category:c,word:w,action:"remove"}}))}}, w+" ✕")));
    rules.append(el("div",{class:"row"}, el("span",{style:"width:110px"},(ICON[c]||"•")+" "+c),
      el("div",{class:"body"}, chips, el("div",{style:"margin-top:8px"}, add, " ",
        el("button",{class:"icon",on:{click:()=>{ if(add.value.trim()) act(()=>api("/rules",{method:"POST",json:{category:c,word:add.value,action:"add"}}),"Regla agregada"); }}},"Agregar")))));
  }
  nodes.push(el("h2",{},"Reglas de clasificación"), rules);
  // notificaciones del navegador
  const perm = ("Notification" in window) ? Notification.permission : "no soportado";
  nodes.push(el("h2",{},"Notificaciones de este navegador"), el("div",{class:"card"}, el("div",{class:"row"},
    el("div",{class:"body"}, "Estado: "+perm, el("div",{class:"meta"},"Funcionan mientras esta pestaña esté abierta. Para avisos con el navegador cerrado usá Telegram o `misecre listen`.")),
    el("button",{on:{click:async()=>{ if("Notification" in window){ await Notification.requestPermission(); refresh(); } }}},"Activar"))));
  return nodes;
}

// ---------- edición
function openEdit(c){
  editing = c;
  $("e_seq").textContent = "#"+c.seq;
  $("e_title").value = c.title; $("e_when").value = ""; $("e_dur").value = "";
  const sel = $("e_cat"); sel.replaceChildren(...cats.map(k=>el("option",{value:k,selected:k===c.category},k)));
  if (!c.category) sel.prepend(el("option",{value:"",selected:true},"(sin categoría)"));
  $("e_type").value = c.type;
  $("edit").showModal();
}
$("e_cancel").addEventListener("click", ()=>$("edit").close());
$("editform").addEventListener("submit", e=>{
  e.preventDefault();
  const c = editing, body = {};
  if ($("e_title").value.trim() && $("e_title").value.trim() !== c.title) body.title = $("e_title").value.trim();
  if ($("e_when").value.trim()) body.when = $("e_when").value.trim();
  if ($("e_dur").value.trim()) body.duration_min = parseInt($("e_dur").value, 10);
  if ($("e_cat").value && $("e_cat").value !== c.category) body.category = $("e_cat").value;
  if ($("e_type").value !== c.type) body.type = $("e_type").value;
  $("edit").close();
  if (Object.keys(body).length) act(()=>api(`/capture/${c.id}`,{method:"PATCH",json:body}),"Guardado");
});

// ---------- captura (texto y voz)
async function addText(){
  const t = $("quick").value.trim(); if (!t) return;
  try { const r = await api("/capture/text",{method:"POST",json:{text:t,source:"web"}}); $("quick").value="";
        toast(`#${r.seq} ${r.title}` + (r.category ? ` → ${r.category}` : " (falta categoría)")); await refresh(); }
  catch(e){ if (e.message!=="auth") toast(e.message,true); }
}
$("add").addEventListener("click", addText);
$("quick").addEventListener("keydown", e=>{ if (e.key==="Enter") addText(); });

let rec = null, chunks = [];
$("mic").addEventListener("click", async ()=>{
  if (rec) { rec.stop(); return; }
  if (!navigator.mediaDevices || !window.MediaRecorder) return toast("Este navegador no puede grabar (¿falta HTTPS?)", true);
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio:true});
    rec = new MediaRecorder(stream); chunks = [];
    rec.ondataavailable = e => chunks.push(e.data);
    rec.onstop = async ()=>{
      stream.getTracks().forEach(t=>t.stop()); $("mic").classList.remove("rec"); $("mic").textContent="🎙";
      const blob = new Blob(chunks, {type: rec.mimeType || "audio/webm"}); rec = null;
      toast("Transcribiendo…");
      const fd = new FormData(); fd.append("file", blob, "voz.webm");
      try { const r = await api("/capture/audio",{method:"POST",body:fd});
            toast(`#${r.seq} ${r.title}`); await refresh(); } catch(e){ if (e.message!=="auth") toast(e.message,true); }
    };
    rec.start(); $("mic").classList.add("rec"); $("mic").textContent="⏹";
  } catch(e){ toast("No pude acceder al micrófono", true); rec = null; }
});

// ---------- header
$("undo").addEventListener("click", ()=>act(async()=>{ const r = await api("/undo",{method:"POST"}); toast("Deshice: "+r.undone); }));
$("sync").addEventListener("click", ()=>act(async()=>{ const r = await api("/sync",{method:"POST"});
  toast(r.errors.length ? r.errors[0] : (r.changes.length ? r.changes.length+" cambio(s)" : "Todo sincronizado"), r.errors.length>0); }));
$("logout").addEventListener("click", ()=>{ clearTok(); token=""; showLogin(); });

// ---------- login
function showLogin(msg){
  $("app").hidden = true; $("login").style.display = "block";
  if (msg) toast(msg, true);
}
$("enter").addEventListener("click", async ()=>{
  token = $("tok").value.trim(); if (!token) return;
  saveTok(token, $("remember").checked); $("tok").value = ""; await refresh();
});
$("tok").addEventListener("keydown", e=>{ if (e.key==="Enter") $("enter").click(); });

// ---------- avisos en vivo (SSE por fetch, para poder mandar el header Authorization)
let streaming = false;
async function stream(){
  if (streaming) return; streaming = true;
  for (;;) {
    try {
      const r = await fetch("/events", {headers: token ? {Authorization:"Bearer "+token} : {}});
      // Token inválido/revocado: NO reintentar (cada intento fallido cuenta para el bloqueo de la IP)
      if (r.status === 401 || r.status === 403) { streaming = false; showLogin("Token inválido o revocado."); return; }
      if (r.status === 429) { await new Promise(r=>setTimeout(r, 60000)); continue; }
      if (!r.ok || !r.body) throw new Error("sin stream");
      const reader = r.body.getReader(), dec = new TextDecoder(); let buf = "";
      for (;;) {
        const {value, done} = await reader.read(); if (done) break;
        buf += dec.decode(value, {stream:true});
        let i; while ((i = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, i); buf = buf.slice(i+2);
          const line = chunk.split("\n").find(l=>l.startsWith("data:"));
          if (line) { try { notify(JSON.parse(line.slice(5))); } catch(e){} }
        }
      }
    } catch(e) {}
    await new Promise(r=>setTimeout(r, 10000));
  }
}
function notify(ev){
  toast(ev.title + ": " + ev.text.split("\n")[0]);
  if ("Notification" in window && Notification.permission === "granted") new Notification(ev.title, {body: ev.text});
  if (ev.kind === "reminder" || ev.kind === "sync") refresh();
}

// ---------- arranque
token = loadTok();
if (NEEDS_TOKEN && !token) showLogin(); else { refresh(); }
timer = setInterval(()=>{
  const a = document.activeElement, typing = a && ["INPUT","SELECT","TEXTAREA"].includes(a.tagName);
  if (!document.hidden && !$("edit").open && !typing && tab !== "ajustes" && tab !== "chat") refresh();
}, 30000);
</script>
</body>
</html>
"""
