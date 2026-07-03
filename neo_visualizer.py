"""NASA NeoWs 3D visualizer.

Fetches near-Earth-object close-approach data from NASA's NeoWs API
(https://api.nasa.gov/) and renders an interactive dashboard in the browser:
a 3D scene with Earth at the center, the Moon's orbit for scale, and every
asteroid plotted at its real miss distance (log-scaled radius), plus a
searchable sidebar — click any asteroid in the list to highlight it in 3D.

Usage:
    python neo_visualizer.py                    # next 7 days, DEMO_KEY
    python neo_visualizer.py --start 2026-07-01 --days 3
    python neo_visualizer.py --api-key YOUR_KEY # or set NASA_API_KEY

The API reports how close each object passes, but not the direction it
approaches from — so each asteroid's direction here is a stable pseudo-random
placement (same asteroid always appears in the same spot); the distance,
size, speed, and hazard data are real.
"""

import argparse
import datetime as dt
import io
import json
import os
import sys
import webbrowser

import numpy as np
import plotly.graph_objects as go
import plotly.io as pio
import requests

FEED_URL = "https://api.nasa.gov/neo/rest/v1/feed"
TEXTURE_URL = "https://unpkg.com/three-globe/example/img/earth-blue-marble.jpg"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Windows "Controlled folder access" can block Python from writing to
# Documents (it shows up as a bogus FileNotFoundError). Fall back to
# LOCALAPPDATA, which is never a protected folder.
FALLBACK_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "neo-visualizer")


def writable_path(preferred):
    """Return `preferred` if its directory accepts writes, else the same
    filename in FALLBACK_DIR (created on demand)."""
    directory = os.path.dirname(preferred) or "."
    probe = os.path.join(directory, ".write_probe")
    try:
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return preferred
    except OSError:
        os.makedirs(FALLBACK_DIR, exist_ok=True)
        fallback = os.path.join(FALLBACK_DIR, os.path.basename(preferred))
        print(f"Note: can't write to {directory} (Controlled Folder Access?); "
              f"using {fallback}")
        return fallback


TEXTURE_CACHE = os.path.join(SCRIPT_DIR, "earth_texture.jpg")
TEXTURE_CACHE_FALLBACK = os.path.join(FALLBACK_DIR, "earth_texture.jpg")

LUNAR_DIST_KM = 384_400.0
EARTH_RADIUS_VIS = 1.0        # Earth's drawn radius in scene units
MOON_RADIUS_VIS = 0.27        # true Moon/Earth size ratio
RING_DISTANCES_LD = [1, 5, 10, 25, 50, 100]

HAZARD_COLOR = "#ff4d4d"
SAFE_COLOR = "#4dd8ff"
HIGHLIGHT_COLOR = "#ffd84d"


def radial(ld):
    """Map a distance in lunar distances to a scene radius (log scale)."""
    return EARTH_RADIUS_VIS + 4.5 * np.log10(1.0 + np.asarray(ld, dtype=float) * 2.0)


def fetch_feed(start, days, api_key):
    end = start + dt.timedelta(days=days - 1)
    params = {"start_date": start.isoformat(), "end_date": end.isoformat(), "api_key": api_key}
    print(f"Fetching NeoWs feed {params['start_date']} .. {params['end_date']} ...")
    resp = requests.get(FEED_URL, params=params, timeout=30)
    if resp.status_code == 403:
        sys.exit("API key rejected (403). Get a free key at https://api.nasa.gov/ "
                 "and pass --api-key or set NASA_API_KEY.")
    if resp.status_code == 429:
        sys.exit("Rate limit hit (429). DEMO_KEY allows ~30 requests/hour — "
                 "wait a bit or use your own key from https://api.nasa.gov/.")
    resp.raise_for_status()
    return resp.json()


def parse_neos(feed):
    """Flatten the feed into one record per close approach."""
    neos = []
    for date, objects in feed.get("near_earth_objects", {}).items():
        for obj in objects:
            approach = obj["close_approach_data"][0]
            d = obj["estimated_diameter"]["meters"]
            when = approach["close_approach_date_full"]
            try:
                epoch = dt.datetime.strptime(when, "%Y-%b-%d %H:%M").timestamp()
            except ValueError:
                epoch = 0
            neos.append({
                "id": obj["id"],
                "name": obj["name"].strip("()"),
                "url": obj.get("nasa_jpl_url", ""),
                "hazardous": obj["is_potentially_hazardous_asteroid"],
                "diam_min_m": d["estimated_diameter_min"],
                "diam_max_m": d["estimated_diameter_max"],
                "when": when,
                "epoch": epoch,
                "miss_km": float(approach["miss_distance"]["kilometers"]),
                "miss_ld": float(approach["miss_distance"]["lunar"]),
                "vel_kms": float(approach["relative_velocity"]["kilometers_per_second"]),
            })
    neos.sort(key=lambda n: n["miss_ld"])
    return neos


def neo_positions(neos):
    """Stable pseudo-random direction per asteroid, real (log-scaled) distance."""
    xs, ys, zs = [], [], []
    for n in neos:
        rng = np.random.default_rng(int(n["id"]))
        theta = rng.uniform(0.0, 2.0 * np.pi)
        z_dir = rng.uniform(-0.85, 0.85)          # keep off the exact poles
        r_xy = np.sqrt(1.0 - z_dir ** 2)
        r = radial(n["miss_ld"])
        xs.append(r * r_xy * np.cos(theta))
        ys.append(r * r_xy * np.sin(theta))
        zs.append(r * z_dir)
    return np.array(xs), np.array(ys), np.array(zs)


def load_earth_texture():
    """Return (surfacecolor grid, colorscale, cmax) for a textured Earth,
    or None if the texture can't be fetched — caller falls back to plain blue."""
    try:
        from PIL import Image
        cached = next((p for p in (TEXTURE_CACHE, TEXTURE_CACHE_FALLBACK)
                       if os.path.exists(p)), None)
        if cached:
            img = Image.open(cached)
        else:
            print("Downloading Earth texture (one-time, cached) ...")
            r = requests.get(TEXTURE_URL, timeout=30)
            r.raise_for_status()
            with open(writable_path(TEXTURE_CACHE), "wb") as f:
                f.write(r.content)
            img = Image.open(io.BytesIO(r.content))
        img = img.convert("RGB").resize((360, 180))
        quant = img.quantize(colors=96)
        palette = quant.getpalette()[: 96 * 3]
        colorscale = [
            [i / 95.0, f"rgb({palette[3*i]},{palette[3*i+1]},{palette[3*i+2]})"]
            for i in range(96)
        ]
        indices = np.asarray(quant, dtype=float)  # (180, 360) rows = +90..-90 lat
        return indices, colorscale, 95
    except Exception as e:  # noqa: BLE001 - cosmetic feature, never fatal
        print(f"Earth texture unavailable ({e}); using plain sphere.")
        return None


def earth_trace():
    n_lat, n_lon = 180, 360
    phi = np.linspace(0, np.pi, n_lat)            # 0 = north pole
    theta = np.linspace(-np.pi, np.pi, n_lon)
    phi_g, theta_g = np.meshgrid(phi, theta, indexing="ij")
    x = EARTH_RADIUS_VIS * np.sin(phi_g) * np.cos(theta_g)
    y = EARTH_RADIUS_VIS * np.sin(phi_g) * np.sin(theta_g)
    z = EARTH_RADIUS_VIS * np.cos(phi_g)

    tex = load_earth_texture()
    if tex is not None:
        surfacecolor, colorscale, cmax = tex
    else:
        surfacecolor = np.cos(phi_g)              # gentle pole-to-pole shading
        colorscale = [[0, "#0b3d91"], [1, "#1a6bd6"]]
        cmax = 1
    return go.Surface(
        x=x, y=y, z=z, surfacecolor=surfacecolor,
        colorscale=colorscale, cmin=0, cmax=cmax,
        showscale=False, name="Earth", hoverinfo="skip",
        lighting=dict(ambient=0.85, diffuse=0.4, specular=0.05),
    )


def moon_traces():
    r_moon = float(radial(1.0))
    t = np.linspace(0, 2 * np.pi, 200)
    orbit = go.Scatter3d(
        x=r_moon * np.cos(t), y=r_moon * np.sin(t), z=np.zeros_like(t),
        mode="lines", line=dict(color="#aaaaaa", width=2, dash="dot"),
        name="Moon orbit (1 LD)", hoverinfo="skip",
    )
    phi = np.linspace(0, np.pi, 20)
    theta = np.linspace(0, 2 * np.pi, 30)
    phi_g, theta_g = np.meshgrid(phi, theta, indexing="ij")
    mx = r_moon + MOON_RADIUS_VIS * np.sin(phi_g) * np.cos(theta_g)
    my = MOON_RADIUS_VIS * np.sin(phi_g) * np.sin(theta_g)
    mz = MOON_RADIUS_VIS * np.cos(phi_g)
    moon = go.Surface(
        x=mx, y=my, z=mz, surfacecolor=np.zeros_like(mz),
        colorscale=[[0, "#c8c8c8"], [1, "#c8c8c8"]], showscale=False,
        name="Moon", hoverinfo="skip",
    )
    return [orbit, moon]


def ring_traces():
    traces = []
    t = np.linspace(0, 2 * np.pi, 120)
    for ld in RING_DISTANCES_LD:
        r = float(radial(ld))
        traces.append(go.Scatter3d(
            x=r * np.cos(t), y=r * np.sin(t), z=np.zeros_like(t),
            mode="lines", line=dict(color="#3a3f55", width=1),
            hoverinfo="skip", showlegend=(ld == RING_DISTANCES_LD[0]),
            name="Distance rings", legendgroup="rings",
        ))
        traces.append(go.Scatter3d(
            x=[r], y=[0], z=[0.15], mode="text", text=[f"{ld} LD"],
            textfont=dict(color="#7a80a0", size=10),
            hoverinfo="skip", showlegend=False, legendgroup="rings",
        ))
    return traces


def starfield_trace(seed=7):
    """Faint stars on a far sphere for depth."""
    rng = np.random.default_rng(seed)
    n = 500
    z = rng.uniform(-1, 1, n)
    theta = rng.uniform(0, 2 * np.pi, n)
    r = float(radial(RING_DISTANCES_LD[-1])) * 1.35
    r_xy = np.sqrt(1 - z ** 2)
    return go.Scatter3d(
        x=r * r_xy * np.cos(theta), y=r * r_xy * np.sin(theta), z=r * z,
        mode="markers",
        marker=dict(size=rng.uniform(1.0, 2.2, n), color="#9aa3c0",
                    opacity=0.5, line=dict(width=0)),
        hoverinfo="skip", showlegend=False, name="Stars",
    )


def neo_trace(neos, xs, ys, zs, hazardous):
    idx = [i for i, n in enumerate(neos) if n["hazardous"] == hazardous]
    if not idx:
        return None, []
    sizes = [min(18, 4 + 10 * np.sqrt(neos[i]["diam_max_m"] / 1000.0)) for i in idx]
    customdata = [[
        neos[i]["name"], neos[i]["when"], neos[i]["miss_ld"], neos[i]["miss_km"],
        neos[i]["vel_kms"], neos[i]["diam_min_m"], neos[i]["diam_max_m"], i,
    ] for i in idx]
    label = "Potentially hazardous" if hazardous else "Non-hazardous"
    trace = go.Scatter3d(
        x=xs[idx], y=ys[idx], z=zs[idx], mode="markers",
        marker=dict(
            size=sizes, color=HAZARD_COLOR if hazardous else SAFE_COLOR,
            opacity=0.9, line=dict(width=0),
        ),
        name=f"{label} ({len(idx)})",
        customdata=customdata,
        hovertemplate=(
            "<b>%{customdata[0]}</b><br>"
            "Closest approach: %{customdata[1]}<br>"
            "Miss distance: %{customdata[2]:.2f} LD "
            "(%{customdata[3]:,.0f} km)<br>"
            "Speed: %{customdata[4]:.1f} km/s<br>"
            "Est. diameter: %{customdata[5]:.0f}–%{customdata[6]:.0f} m<br>"
            "<i>click to select</i>"
            "<extra></extra>"
        ),
    )
    return trace, idx


def stem_trace(xs, ys, zs):
    """Thin vertical guides from each asteroid to the ecliptic plane —
    helps judge 3D position. Toggle via legend."""
    sx, sy, sz = [], [], []
    for x, y, z in zip(xs, ys, zs):
        sx += [x, x, None]
        sy += [y, y, None]
        sz += [z, 0, None]
    return go.Scatter3d(
        x=sx, y=sy, z=sz, mode="lines",
        line=dict(color="#2e3350", width=1),
        name="Height guides", hoverinfo="skip", visible="legendonly",
    )


def highlight_trace():
    """Selection marker, repositioned from JS when a sidebar row is clicked."""
    return go.Scatter3d(
        x=[0], y=[0], z=[0], mode="markers", visible=False,
        marker=dict(size=24, color=HIGHLIGHT_COLOR, symbol="circle-open",
                    line=dict(color=HIGHLIGHT_COLOR, width=3)),
        hoverinfo="skip", showlegend=False, name="Selected",
    )


def build_figure(neos, xs, ys, zs):
    """Returns (figure, index of the highlight trace)."""
    traces = [starfield_trace(), earth_trace()] + moon_traces() + ring_traces()
    traces.append(stem_trace(xs, ys, zs))
    for hazardous in (True, False):
        tr, _ = neo_trace(neos, xs, ys, zs, hazardous)
        if tr is not None:
            traces.append(tr)
    traces.append(highlight_trace())
    highlight_idx = len(traces) - 1

    axis = dict(visible=False)
    fig = go.Figure(data=traces)
    fig.update_layout(
        scene=dict(
            xaxis=axis, yaxis=axis, zaxis=axis,
            aspectmode="data", bgcolor="#05060f",
            camera=dict(eye=dict(x=1.9, y=1.9, z=1.1)),
        ),
        paper_bgcolor="#05060f",
        legend=dict(font=dict(color="#c8cce0", size=11),
                    bgcolor="rgba(10,12,30,0.7)", x=0.01, y=0.02),
        margin=dict(l=0, r=0, t=0, b=0),
        autosize=True,
    )
    return fig, highlight_idx


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Near-Earth Objects — __DATE_RANGE__</title>
<style>
  :root {
    --bg: #05060f; --panel: #0b0e1e; --panel2: #11152b;
    --text: #e8eaf5; --dim: #8a90ad; --line: #23283f;
    --haz: #ff4d4d; --safe: #4dd8ff; --sel: #ffd84d;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; background: var(--bg); color: var(--text);
    font: 14px/1.45 "Segoe UI", system-ui, sans-serif; overflow: hidden; }
  #app { display: flex; flex-direction: column; height: 100%; }

  header { display: flex; align-items: center; gap: 16px; padding: 10px 16px;
    background: var(--panel); border-bottom: 1px solid var(--line); flex: 0 0 auto; }
  header h1 { font-size: 17px; margin: 0; font-weight: 600; }
  header .range { color: var(--dim); font-size: 13px; }
  header .stats { color: var(--dim); font-size: 13px; }
  header .stats b.haz { color: var(--haz); }
  header .spacer { flex: 1; }
  button.hbtn { background: var(--panel2); color: var(--text); border: 1px solid var(--line);
    border-radius: 6px; padding: 5px 12px; cursor: pointer; font-size: 13px; }
  button.hbtn:hover { border-color: #4a5178; }
  button.hbtn.on { border-color: var(--sel); color: var(--sel); }

  #main { display: flex; flex: 1; min-height: 0; }
  #sidebar { width: 320px; flex: 0 0 320px; background: var(--panel);
    border-right: 1px solid var(--line); display: flex; flex-direction: column; }
  #controls { padding: 10px 12px 6px; display: flex; gap: 8px; }
  #search { flex: 1; background: var(--panel2); border: 1px solid var(--line);
    border-radius: 6px; color: var(--text); padding: 6px 10px; outline: none; }
  #search:focus { border-color: #4a5178; }
  #sort { background: var(--panel2); border: 1px solid var(--line); border-radius: 6px;
    color: var(--text); padding: 6px 6px; outline: none; }
  #list { flex: 1; overflow-y: auto; padding: 4px 6px; }
  #list::-webkit-scrollbar { width: 8px; }
  #list::-webkit-scrollbar-thumb { background: #2a3050; border-radius: 4px; }
  .row { display: flex; align-items: center; gap: 10px; padding: 7px 8px;
    border-radius: 7px; cursor: pointer; }
  .row:hover { background: var(--panel2); }
  .row.active { background: #1c2140; outline: 1px solid var(--sel); }
  .dot { width: 10px; height: 10px; border-radius: 50%; flex: 0 0 10px; }
  .dot.haz { background: var(--haz); } .dot.safe { background: var(--safe); }
  .row .nm { font-size: 13px; font-weight: 600; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; }
  .row .sub { font-size: 12px; color: var(--dim); }
  .row .ld { margin-left: auto; font-size: 12px; color: var(--dim); white-space: nowrap; }

  #detail { flex: 0 0 auto; border-top: 1px solid var(--line); padding: 12px 14px;
    background: var(--panel2); display: none; }
  #detail.show { display: block; }
  #detail h2 { margin: 0 0 6px; font-size: 15px; color: var(--sel); }
  #detail table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
  #detail td { padding: 2px 0; color: var(--text); }
  #detail td:first-child { color: var(--dim); width: 46%; }
  #detail .badge { display: inline-block; font-size: 11px; font-weight: 700;
    padding: 1px 8px; border-radius: 10px; margin-left: 6px; vertical-align: middle; }
  #detail .badge.haz { background: #4d1515; color: var(--haz); }
  #detail .badge.safe { background: #123240; color: var(--safe); }
  #detail a { color: var(--safe); }
  #foot { flex: 0 0 auto; padding: 8px 14px; font-size: 11px; color: var(--dim);
    border-top: 1px solid var(--line); }

  #plot-wrap { flex: 1; min-width: 0; position: relative; }
  #plot-wrap .plotly-graph-div { width: 100% !important; height: 100% !important; }
</style>
</head>
<body>
<div id="app">
  <header>
    <h1>Near-Earth Objects</h1>
    <span class="range">__DATE_RANGE__</span>
    <span class="stats">__N_TOTAL__ approaches · <b class="haz">__N_HAZ__ potentially hazardous</b></span>
    <span class="spacer"></span>
    <button class="hbtn" id="cam-tilt">Tilt</button>
    <button class="hbtn" id="cam-top">Top-down</button>
    <button class="hbtn" id="cam-side">Side</button>
    <button class="hbtn on" id="spin-btn">⟳ Auto-rotate</button>
  </header>
  <div id="main">
    <div id="sidebar">
      <div id="controls">
        <input id="search" type="text" placeholder="Search asteroids…">
        <select id="sort">
          <option value="ld">Closest</option>
          <option value="size">Largest</option>
          <option value="date">Soonest</option>
        </select>
      </div>
      <div id="list"></div>
      <div id="detail"></div>
      <div id="foot">Distances are real (log scale; LD = lunar distance ≈ 384,400 km).
        Approach direction is illustrative — the API doesn't provide it.
        Earth &amp; Moon drawn oversized. Data: NASA NeoWs.</div>
    </div>
    <div id="plot-wrap">__PLOT_DIV__</div>
  </div>
</div>
<script>
const NEOS = __NEO_DATA__;
const HIGHLIGHT_IDX = __HIGHLIGHT_IDX__;
const plot = document.querySelector('#plot-wrap .plotly-graph-div');
const listEl = document.getElementById('list');
const detailEl = document.getElementById('detail');
let selected = -1;

const fmt = {
  ld: v => v.toFixed(2) + ' LD',
  km: v => Math.round(v).toLocaleString() + ' km',
  m: v => Math.round(v).toLocaleString() + ' m',
  day: w => { const p = w.split(' ')[0].split('-'); return p[1] + ' ' + p[2]; },
};

function sortedIndices() {
  const mode = document.getElementById('sort').value;
  const idx = NEOS.map((_, i) => i);
  if (mode === 'size') idx.sort((a, b) => NEOS[b].dmax - NEOS[a].dmax);
  else if (mode === 'date') idx.sort((a, b) => NEOS[a].epoch - NEOS[b].epoch);
  else idx.sort((a, b) => NEOS[a].ld - NEOS[b].ld);
  return idx;
}

function renderList() {
  const q = document.getElementById('search').value.trim().toLowerCase();
  listEl.innerHTML = '';
  for (const i of sortedIndices()) {
    const n = NEOS[i];
    if (q && !n.name.toLowerCase().includes(q)) continue;
    const row = document.createElement('div');
    row.className = 'row' + (i === selected ? ' active' : '');
    row.dataset.i = i;
    row.innerHTML =
      '<span class="dot ' + (n.haz ? 'haz' : 'safe') + '"></span>' +
      '<div style="min-width:0"><div class="nm">' + n.name + '</div>' +
      '<div class="sub">~' + fmt.m(n.dmax) + ' · ' + n.vel.toFixed(1) + ' km/s · ' +
      fmt.day(n.when) + '</div></div>' +
      '<span class="ld">' + fmt.ld(n.ld) + '</span>';
    row.onclick = () => select(i);
    listEl.appendChild(row);
  }
}

function select(i, scroll) {
  selected = i;
  const n = NEOS[i];
  Plotly.restyle(plot, { x: [[n.x]], y: [[n.y]], z: [[n.z]], visible: true }, [HIGHLIGHT_IDX]);
  detailEl.className = 'show';
  detailEl.innerHTML =
    '<h2>' + n.name +
    '<span class="badge ' + (n.haz ? 'haz' : 'safe') + '">' +
    (n.haz ? 'HAZARDOUS' : 'NON-HAZARDOUS') + '</span></h2>' +
    '<table>' +
    '<tr><td>Closest approach</td><td>' + n.when + ' UTC</td></tr>' +
    '<tr><td>Miss distance</td><td>' + fmt.ld(n.ld) + ' (' + fmt.km(n.km) + ')</td></tr>' +
    '<tr><td>Relative speed</td><td>' + n.vel.toFixed(1) + ' km/s</td></tr>' +
    '<tr><td>Est. diameter</td><td>' + fmt.m(n.dmin) + ' – ' + fmt.m(n.dmax) + '</td></tr>' +
    '</table>' +
    (n.url ? '<div style="margin-top:6px"><a href="' + n.url +
             '" target="_blank" rel="noopener">Open in JPL orbit viewer ↗</a></div>' : '');
  document.querySelectorAll('#list .row').forEach(r =>
    r.classList.toggle('active', +r.dataset.i === i));
  if (scroll) {
    const row = document.querySelector('#list .row[data-i="' + i + '"]');
    if (row) row.scrollIntoView({ block: 'nearest' });
  }
}

document.getElementById('search').addEventListener('input', renderList);
document.getElementById('sort').addEventListener('change', renderList);

// Clicking an asteroid in 3D selects it in the sidebar (customdata[7] = index).
plot.on('plotly_click', ev => {
  const p = ev.points && ev.points[0];
  if (p && p.customdata && p.customdata.length >= 8) select(p.customdata[7], true);
});

// --- Camera ---
const EYE_R = 2.69, EYE_Z = 1.1;
let angle = Math.PI / 4, spinning = true;
const spinBtn = document.getElementById('spin-btn');

function setSpin(on) {
  spinning = on;
  spinBtn.classList.toggle('on', on);
}
function setCamera(eye) {
  setSpin(false);
  Plotly.relayout(plot, { 'scene.camera.eye': eye, 'scene.camera.up': { x: 0, y: 0, z: 1 } });
}
document.getElementById('cam-tilt').onclick = () =>
  setCamera({ x: EYE_R * Math.cos(angle), y: EYE_R * Math.sin(angle), z: EYE_Z });
document.getElementById('cam-top').onclick = () => setCamera({ x: 0, y: 0.01, z: 2.9 });
document.getElementById('cam-side').onclick = () =>
  setCamera({ x: EYE_R * Math.cos(angle), y: EYE_R * Math.sin(angle), z: 0.12 });
spinBtn.onclick = () => setSpin(!spinning);

['mousedown', 'wheel', 'touchstart'].forEach(evt =>
  plot.addEventListener(evt, () => setSpin(false), { passive: true }));

// ?eye=x,y,z in the URL sets a fixed camera (and stops rotation) — used for
// scripted screenshots.
const eyeParam = new URLSearchParams(location.search).get('eye');
if (eyeParam) {
  const [ex, ey, ez] = eyeParam.split(',').map(Number);
  setSpin(false);
  Plotly.relayout(plot, { 'scene.camera.eye': { x: ex, y: ey, z: ez } });
}

(function spin() {
  if (spinning) {
    angle += 0.0012;
    Plotly.relayout(plot, {
      'scene.camera.eye': { x: EYE_R * Math.cos(angle), y: EYE_R * Math.sin(angle), z: EYE_Z },
    });
  }
  requestAnimationFrame(spin);
})();

renderList();
</script>
</body>
</html>
"""


def build_html(fig, highlight_idx, neos, xs, ys, zs, start, days):
    plot_div = pio.to_html(
        fig, full_html=False, include_plotlyjs=True,
        config={"responsive": True, "displaylogo": False,
                "modeBarButtonsToRemove": ["toImage"]},
    )
    neo_data = [{
        "name": n["name"], "when": n["when"], "epoch": n["epoch"],
        "ld": n["miss_ld"], "km": n["miss_km"], "vel": n["vel_kms"],
        "dmin": n["diam_min_m"], "dmax": n["diam_max_m"],
        "haz": n["hazardous"], "url": n["url"],
        "x": float(xs[i]), "y": float(ys[i]), "z": float(zs[i]),
    } for i, n in enumerate(neos)]
    end = start + dt.timedelta(days=days - 1)
    html = PAGE_TEMPLATE
    for token, value in [
        ("__PLOT_DIV__", plot_div),
        ("__NEO_DATA__", json.dumps(neo_data)),
        ("__HIGHLIGHT_IDX__", str(highlight_idx)),
        ("__DATE_RANGE__", f"{start} → {end}"),
        ("__N_TOTAL__", str(len(neos))),
        ("__N_HAZ__", str(sum(n["hazardous"] for n in neos))),
    ]:
        html = html.replace(token, value)
    return html


def print_summary(neos):
    print(f"\n{len(neos)} close approaches in window "
          f"({sum(n['hazardous'] for n in neos)} flagged potentially hazardous).")
    print("\nFive closest:")
    for n in neos[:5]:
        flag = "  [HAZARDOUS]" if n["hazardous"] else ""
        print(f"  {n['name']:<22} {n['miss_ld']:6.2f} LD  "
              f"({n['miss_km']:>12,.0f} km)  {n['vel_kms']:5.1f} km/s  "
              f"~{n['diam_max_m']:,.0f} m  on {n['when']}{flag}")


def main():
    ap = argparse.ArgumentParser(description="3D visualizer for NASA NeoWs near-Earth objects.")
    ap.add_argument("--start", type=dt.date.fromisoformat, default=dt.date.today(),
                    help="start date YYYY-MM-DD (default: today)")
    ap.add_argument("--days", type=int, default=7, choices=range(1, 8), metavar="1-7",
                    help="window length in days (API max 7, default 7)")
    ap.add_argument("--api-key", default=os.environ.get("NASA_API_KEY", "DEMO_KEY"),
                    help="api.nasa.gov key (default: NASA_API_KEY env var or DEMO_KEY)")
    ap.add_argument("--output", default=os.path.join(SCRIPT_DIR, "neo_map.html"),
                    help="output HTML file")
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    args = ap.parse_args()

    feed = fetch_feed(args.start, args.days, args.api_key)
    neos = parse_neos(feed)
    if not neos:
        sys.exit("No near-Earth objects returned for that window.")
    print_summary(neos)

    xs, ys, zs = neo_positions(neos)
    fig, highlight_idx = build_figure(neos, xs, ys, zs)
    html = build_html(fig, highlight_idx, neos, xs, ys, zs, args.start, args.days)

    output = writable_path(os.path.abspath(args.output))
    with open(output, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nSaved {output}")
    if not args.no_open:
        webbrowser.open("file:///" + output.replace("\\", "/"))


if __name__ == "__main__":
    main()
