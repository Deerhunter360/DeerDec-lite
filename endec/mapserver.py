"""Local web server for the alert map (http://127.0.0.1:<port>/). The Map tab shows this page, and
you can also open it in any browser (e.g. on the projector machine)."""
import json
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COUNTIES_URL = "https://raw.githubusercontent.com/plotly/datasets/master/geojson-counties-fips.json"
DATA_DIR = "data"

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeerDec Alert Map</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css">
<style>
  :root { --bg:#14171c; --panel:#1b1f26; --line:#2c323c; --text:#e6e9ee; --mute:#8d95a3; }
  * { box-sizing:border-box; }
  html,body { height:100%; margin:0; background:var(--bg); color:var(--text);
              font:14px/1.45 "Segoe UI",system-ui,-apple-system,sans-serif; }
  #wrap { display:flex; height:100%; }
  #map { flex:1; min-width:0; background:#0d0f13; }
  #side { width:380px; max-width:42vw; background:var(--panel); border-left:1px solid var(--line);
          display:flex; flex-direction:column; }
  #top { padding:12px 14px; border-bottom:1px solid var(--line); }
  #top h1 { margin:0; font-size:16px; font-weight:650; }
  #top .sub { color:var(--mute); font-size:12px; margin-top:2px; }
  #scroll { flex:1; overflow:auto; }
  .sec { padding:10px 14px 4px; color:var(--mute); font-size:11px; letter-spacing:.08em; text-transform:uppercase; }
  .card { margin:8px 12px; padding:10px 12px; background:#222833; border:1px solid var(--line);
          border-left:5px solid var(--c,#888); border-radius:6px; cursor:pointer; }
  .card:hover { background:#272e3b; }
  .card.sel { outline:2px solid var(--c); }
  .card h3 { margin:0 0 2px; font-size:14px; }
  .card .meta { color:var(--mute); font-size:12px; }
  .detail { margin:8px 12px; padding:12px 14px; background:#222833; border:1px solid var(--line);
            border-top:5px solid var(--c,#888); border-radius:6px; }
  .detail h2 { margin:0 0 4px; font-size:17px; }
  .badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:11px; font-weight:600;
           background:var(--c); color:#111; margin-bottom:6px; }
  .detail dl { margin:8px 0 0; display:grid; grid-template-columns:84px 1fr; gap:3px 10px; font-size:13px; }
  .detail dt { color:var(--mute); } .detail dd { margin:0; }
  .detail pre { white-space:pre-wrap; font:inherit; margin:6px 0 0; padding:8px 10px; background:#181c23;
                border-radius:5px; font-size:13px; }
  .empty { padding:18px 16px; color:var(--mute); }
  .note { margin:8px 12px; padding:8px 10px; border:1px dashed #6b5b2a; color:#e0c36a; border-radius:6px; font-size:12px; }
  .legend { position:absolute; left:12px; bottom:22px; z-index:500; background:rgba(27,31,38,.92);
            border:1px solid var(--line); border-radius:6px; padding:8px 10px; font-size:12px; }
  .legend div { display:flex; align-items:center; gap:7px; margin:2px 0; }
  .legend i { width:14px; height:14px; border-radius:3px; display:inline-block; }
  .leaflet-container { background:#0d0f13; font:inherit; }
</style></head>
<body>
<div id="wrap">
  <div id="map"></div>
  <div id="side">
    <div id="top"><h1>Active alerts <span id="count"></span></h1><div class="sub" id="sub">Loading…</div></div>
    <div id="scroll"><div id="notes"></div><div id="detail"></div><div class="sec" id="listhead"></div><div id="list"></div></div>
  </div>
</div>
<div class="legend" id="legend">
  <div><i style="background:#e5322d"></i>Warning / emergency</div>
  <div><i style="background:#f5a300"></i>Watch</div>
  <div><i style="background:#2fb34a"></i>Advisory / statement</div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
<script>
const LEVEL = {
  alert:    {c:'#e5322d', rank:3, label:'Warning / emergency'},
  watch:    {c:'#f5a300', rank:2, label:'Watch'},
  advisory: {c:'#2fb34a', rank:1, label:'Advisory / statement'}
};
const lv = a => LEVEL[a.level] || LEVEL.alert;
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const map = L.map('map', {preferCanvas:true, zoomSnap:0.25}).setView([39.5, -98.5], 4);
const ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/';
L.tileLayer(ESRI + 'World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}',
  {maxZoom:16, maxNativeZoom:16, attribution:'Tiles &copy; Esri, HERE, Garmin, OpenStreetMap contributors'}).addTo(map);
L.tileLayer(ESRI + 'World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}',
  {maxZoom:16, maxNativeZoom:16, pane:'shadowPane'}).addTo(map);
const group = L.layerGroup().addTo(map);

let counties = null, byState = {}, countyError = '';
let alerts = [], layersOf = {}, lastJson = '', fitted = false, selected = null;

fetch('/counties.json').then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
  .then(g => {
    counties = new Map();
    for (const f of g.features) {
      const id = String(f.id).padStart(5, '0');
      counties.set(id, f);
      (byState[id.slice(0, 2)] = byState[id.slice(0, 2)] || []).push(id);
    }
    render();
  }).catch(e => { countyError = String(e.message || e); render(); });

function countyIds(a) {
  const ids = new Set();
  for (const code of a.fips || []) {
    const c = String(code).padStart(6, '0');
    if (c === '000000') continue;                       // whole nation: nothing sensible to shade
    const ss = c.slice(1, 3), ccc = c.slice(3);
    if (ccc === '000') (byState[ss] || []).forEach(i => ids.add(i)); else ids.add(ss + ccc);
  }
  return ids;
}

function render() {
  group.clearLayers(); layersOf = {};
  const perCounty = new Map(), polyAlerts = [];
  for (const a of alerts) {
    layersOf[a.id] = [];
    if (a.polygons && a.polygons.length) polyAlerts.push(a);
    else if (counties) for (const id of countyIds(a)) { if (!perCounty.has(id)) perCounty.set(id, []); perCounty.get(id).push(a); }
  }
  for (const [id, list] of perCounty) {
    const f = counties.get(id); if (!f) continue;
    list.sort((x, y) => lv(y).rank - lv(x).rank);
    const top = lv(list[0]);
    const layer = L.geoJSON(f, {style: {color: top.c, weight: 1, fillColor: top.c, fillOpacity: .42}});
    layer.on('click', () => show(list));
    layer.addTo(group);
    list.forEach(a => layersOf[a.id].push(layer));
  }
  for (const a of polyAlerts) {
    const c = lv(a).c;
    for (const ring of a.polygons) {
      const p = L.polygon(ring, {color: c, weight: 2, fillColor: c, fillOpacity: .38});
      p.on('click', () => show([a]));
      p.addTo(group); layersOf[a.id].push(p);
    }
  }
  for (const a of alerts) {                                   // marine zones (waters)
    const c = lv(a).c;
    for (const ring of a.zones || []) {
      if (!ring.length) continue;
      const p = L.polygon(ring, {color: c, weight: 1.5, dashArray: '5 4', fillColor: c, fillOpacity: .30});
      p.on('click', () => show([a]));
      p.addTo(group); (layersOf[a.id] = layersOf[a.id] || []).push(p);
    }
  }
  if (!fitted) {
    const all = [].concat(...Object.values(layersOf));
    if (all.length) { map.fitBounds(L.featureGroup(all).getBounds().pad(0.3), {maxZoom: 8}); fitted = true; }
  }
  renderSide();
}

function card(a) {
  return `<div class="card${selected === a.id ? ' sel' : ''}" style="--c:${lv(a).c}" data-id="${esc(a.id)}">
    <h3>${esc(a.title)}</h3><div class="meta">${esc((a.area || '').slice(0, 120))}</div>
    <div class="meta">${a.expired ? 'EXPIRED ' : 'Until '}${esc(a.expires || '—')} · ${esc(a.origin || '')}</div></div>`;
}

function renderSide() {
  document.getElementById('count').textContent = alerts.length ? '(' + alerts.length + ')' : '';
  document.getElementById('listhead').textContent = alerts.length ? 'All active alerts' : '';
  const list = document.getElementById('list');
  list.innerHTML = alerts.length ? alerts.map(card).join('')
    : '<div class="empty">No active alerts right now.<br>Test, demo and weekly-test messages are never shown here.</div>';
  list.querySelectorAll('.card').forEach(el => el.onclick = () => {
    const a = alerts.find(x => x.id === el.dataset.id); if (!a) return;
    show([a]);
    const ls = layersOf[a.id] || [];
    if (ls.length) map.fitBounds(L.featureGroup(ls).getBounds().pad(0.4), {maxZoom: 9});
  });
  const notes = [];
  if (countyError) notes.push('County shapes could not be loaded (' + esc(countyError) + '). They need an internet connection once; polygon alerts still show.');
  if (alerts.some(a => !(a.polygons && a.polygons.length) && !(a.zones && a.zones.length) && !(a.fips || []).some(f => String(f).padStart(6,'0') !== '000000')))
    notes.push('Some alerts have no map area (nationwide, or waters DeerDec has no outline for) - see the list.');
  document.getElementById('notes').innerHTML = notes.map(n => '<div class="note">' + n + '</div>').join('');
}

function detail(a) {
  const row = (k, v) => v ? `<dt>${k}</dt><dd>${esc(v)}</dd>` : '';
  const block = (k, v) => v ? `<div style="margin-top:8px"><b>${k}</b><pre>${esc(v)}</pre></div>` : '';
  return `<div class="detail" style="--c:${lv(a).c}">
    <span class="badge">${esc(lv(a).label)}</span><h2>${esc(a.title)}</h2>
    ${a.headline ? '<div>' + esc(a.headline) + '</div>' : ''}
    <dl>${row('Origin', a.origin)}${a.expired ? row('State', 'Expired') : ''}${row('Status', a.status)}${row('Type', a.msg_type)}${row('Issued', a.sent)}${row('Expires', a.expires)}${row('Sender', a.sender)}${row('Event code', a.eee)}${row('Severity', a.severity)}${row('Areas', a.area)}</dl>
    ${block('Description', a.description)}${block('Instructions', a.instruction)}</div>`;
}

function show(list) {
  selected = list[0].id;
  document.getElementById('detail').innerHTML =
    '<div class="sec">' + (list.length > 1 ? list.length + ' alerts cover this area' : 'Selected alert') + '</div>' + list.map(detail).join('');
  document.querySelectorAll('#list .card').forEach(el => el.classList.toggle('sel', el.dataset.id === selected));
  document.getElementById('scroll').scrollTop = 0;
}

async function poll() {
  try {
    const r = await fetch('/alerts.json?t=' + Date.now(), {cache: 'no-store'});
    const text = await r.text();
    const data = JSON.parse(text);
    document.getElementById('sub').textContent =
      'Updated ' + data.updated + (data.hidden ? ' · ' + data.hidden + ' test/cancelled hidden' : '');
    if (text !== lastJson) {
      lastJson = text; alerts = data.alerts;
      if (selected && !alerts.some(a => a.id === selected)) { selected = null; document.getElementById('detail').innerHTML = ''; }
      render();
    }
  } catch (e) { document.getElementById('sub').textContent = 'DeerDec is not reachable - retrying…'; }
}
function sweep() {
  const now = Date.now() / 1000, keep = alerts.filter(a => !a.expires_ts || a.expires_ts > now || a.expired);
  if (keep.length !== alerts.length) {
    alerts = keep; lastJson = '';
    if (selected && !alerts.some(a => a.id === selected)) { selected = null; document.getElementById('detail').innerHTML = ''; }
    render();
  }
}
poll(); setInterval(poll, 10000); setInterval(sweep, 5000);
</script></body></html>
"""


class MapServer:
    def __init__(self, engine, port=8765, web=None):
        """web = optional extra routes (the web dashboard); without it only the map is served."""
        self.engine, self.port, self.httpd, self.url, self.web = engine, int(port), None, "", web
        os.makedirs(DATA_DIR, exist_ok=True)
        self.counties_path = os.path.join(DATA_DIR, "counties.json")

    def start(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype, cache="no-store"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", cache)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code, obj):
                self._send(200 if code is None else code, json.dumps(obj).encode("utf-8"), "application/json")

            def _ok(self):
                """Map routes: allowed unless the extra routes (login) say no."""
                if outer.web is None or outer.web.allowed(self):
                    return True
                self._json(401, {"error": "login required"})
                return False

            def do_GET(self):
                from urllib.parse import parse_qs, urlsplit
                u = urlsplit(self.path)
                path, q = u.path, parse_qs(u.query)
                try:
                    if outer.web is not None and outer.web.handle_get(self, path, q):
                        return
                    if path in ("/", "/index.html", "/map", "/map/"):
                        if self._ok():
                            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                    elif path == "/alerts.json":
                        if self._ok():
                            self._json(None, outer.engine.map_payload())
                    elif path == "/counties.json":
                        if not self._ok():
                            return
                        body = outer._counties()
                        if body:
                            self._send(200, body, "application/json", "max-age=86400")
                        else:
                            self._send(503, b'{"error":"could not download county shapes"}', "application/json")
                    else:
                        self._send(404, b"not found", "text/plain")
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self):
                try:
                    if outer.web is not None and outer.web.handle_post(self, self.path.split("?")[0]):
                        return
                    self._send(404, b"not found", "text/plain")
                except (BrokenPipeError, ConnectionResetError):
                    pass

        bind = self.web.bind_host() if self.web is not None else "127.0.0.1"
        for port in (self.port, 0):                     # fall back to any free port
            try:
                self.httpd = ThreadingHTTPServer((bind, port), Handler)
                break
            except OSError:
                continue
        if not self.httpd:
            raise OSError("could not start the map server")
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        if bind != "127.0.0.1":
            self.engine.log(f"Remote access is ON: port {self.httpd.server_address[1]} (password protected, no HTTPS - "
                            "use Tailscale or a tunnel, not a bare port-forward)")
        return self.url

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    def _counties(self):
        """County outlines (US Census, by FIPS) - downloaded once, then kept in data/."""
        if os.path.exists(self.counties_path) and os.path.getsize(self.counties_path) > 100000:
            return open(self.counties_path, "rb").read()
        try:
            req = urllib.request.Request(COUNTIES_URL, headers={"User-Agent": "DeerDec/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            json.loads(body[:2000].decode("utf-8", "ignore").rsplit(",", 1)[0] + "]}") if False else None
            tmp = self.counties_path + ".part"
            with open(tmp, "wb") as f:
                f.write(body)
            os.replace(tmp, self.counties_path)
            return body
        except Exception as e:
            self.engine.log(f"Map: could not download county shapes: {e}")
            return None
