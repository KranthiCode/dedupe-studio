import os
import re
import json
import hashlib
import argparse
import mimetypes
import subprocess
import shutil
import threading
import sys
import socketserver
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from collections import defaultdict

try:
    import tkinter as tk
    from tkinter import filedialog
except ImportError:
    tk = None
    filedialog = None

# --- CONFIGURATION & CONSTANTS ---
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.heic', '.webp', '.tiff', '.bmp', '.arw', '.tga'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.flv', '.wmv', '.m4v'}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
CHUNK_SIZE = 64 * 1024  # 64KB for partial hashing

# --- ENGINE LOGIC ---

def get_file_hash(filepath, full=False):
    sha256 = hashlib.sha256()
    try:
        with open(filepath, 'rb') as f:
            if full:
                while chunk := f.read(8192):
                    sha256.update(chunk)
            else:
                sha256.update(f.read(CHUNK_SIZE))
        return sha256.hexdigest()
    except: return None

def get_creation_date(filepath):
    try:
        cmd = ['mdls', '-name', 'kMDItemContentCreationDate', '-raw', filepath]
        output = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode('utf-8')
        if output and "(null)" not in output:
            return datetime.strptime(output[:19], '%Y-%m-%d %H:%M:%S')
    except: pass
    try: return datetime.fromtimestamp(os.path.getmtime(filepath))
    except: return None

def format_eta(seconds):
    if seconds < 0: return ""
    if seconds < 60: return f"{int(seconds)}s remaining"
    return f"{int(seconds // 60)}m {int(seconds % 60)}s remaining"

class StudioEngine:
    def __init__(self):
        self.progress = {"status": "Idle", "percent": 0, "details": "", "eta": ""}
        self.results = {"groups": [], "stats": {}}
        self.analysis = {"stats": {}, "no_exif": {}, "total": 0}
        self.stop_requested = False

    def _update_progress(self, status, processed, total, start_time, detail_prefix=""):
        elapsed = time.time() - start_time
        percent = int((processed / total) * 100) if total > 0 else 100
        eta_str = ""
        if processed > 5 and elapsed > 1:
            per_second = processed / elapsed
            remaining = total - processed
            eta_str = format_eta(remaining / per_second)
        
        self.progress = {
            "status": status,
            "percent": percent,
            "details": f"{detail_prefix} {processed}/{total} files...",
            "eta": eta_str
        }

    def analyze(self, folders):
        self.stop_requested = False
        start_t = time.time()
        self.progress = {"status": "Analyzing", "percent": 0, "details": "Indexing...", "eta": ""}
        
        all_paths = []
        for folder in folders:
            for root, _, files in os.walk(folder):
                for f in files:
                    if os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS:
                        all_paths.append(os.path.join(root, f))
        
        total = len(all_paths)
        if total == 0:
            self.progress = {"status": "Complete", "percent": 100, "details": "No files found."}
            return

        stats = defaultdict(lambda: {"img": 0, "img_sz": 0, "vid": 0, "vid_sz": 0})
        no_exif = {"img": 0, "img_sz": 0, "vid": 0, "vid_sz": 0}
        
        processed = 0
        with ProcessPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(get_creation_date, p): p for p in all_paths}
            for future in as_completed(futures):
                path = futures[future]
                dt = future.result()
                sz = os.path.getsize(path)
                is_vid = os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS
                
                if dt:
                    month = dt.strftime('%Y-%m')
                    if is_vid: stats[month]["vid"] += 1; stats[month]["vid_sz"] += sz
                    else: stats[month]["img"] += 1; stats[month]["img_sz"] += sz
                else:
                    if is_vid: no_exif["vid"] += 1; no_exif["vid_sz"] += sz
                    else: no_exif["img"] += 1; no_exif["img_sz"] += sz
                
                processed += 1
                if processed % 50 == 0 or processed == total:
                    self._update_progress("Analyzing", processed, total, start_t, "Auditing metadata:")

        self.analysis = {"stats": stats, "no_exif": no_exif, "total": total}
        self.progress = {"status": "Complete", "percent": 100, "details": "Analysis finished.", "eta": ""}

    def scan(self, folders):
        self.stop_requested = False
        start_t = time.time()
        self.progress = {"status": "Scanning", "percent": 0, "details": "Indexing...", "eta": ""}
        by_size = defaultdict(list)
        total_files = 0
        for folder in folders:
            for root, _, files in os.walk(folder):
                for f in files:
                    if os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS:
                        path = os.path.join(root, f)
                        try:
                            by_size[os.path.getsize(path)].append(path)
                            total_files += 1
                        except: continue
        
        candidates = {s: p for s, p in by_size.items() if len(p) > 1}
        cand_list = [p for ps in candidates.values() for p in ps]
        total_cand = len(cand_list)
        
        by_partial = defaultdict(list)
        processed = 0
        start_t_h = time.time()
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(get_file_hash, p, False): p for p in cand_list}
            for future in as_completed(futures):
                p = futures[future]
                h = future.result()
                if h: by_partial[(os.path.getsize(p), h)].append(p)
                processed += 1
                if processed % 50 == 0 or processed == total_cand:
                    self._update_progress("Scanning", processed, total_cand, start_t_h, "Hashing candidates:")

        final_groups = []
        potentials = [p for ps in by_partial.values() if len(ps) > 1 for p in ps]
        total_final = len(potentials)
        processed = 0
        start_t_full = time.time()
        with ThreadPoolExecutor(max_workers=4) as executor: # Fewer workers for full hash to avoid disk thrashing
            futures = {}
            # Group by partial+size to process each group efficiently
            for (size, ph), paths in by_partial.items():
                if len(paths) < 2: continue
                for p in paths:
                    futures[executor.submit(get_file_hash, p, True)] = (p, size)
            
            by_full = defaultdict(list)
            for future in as_completed(futures):
                p, size = futures[future]
                fh = future.result()
                if fh: by_full[(size, fh)].append(p)
                processed += 1
                if processed % 20 == 0 or processed == total_final:
                    self._update_progress("Scanning", processed, total_final, start_t_full, "Verifying duplicates:")

            for (size, fh), dupe_p in by_full.items():
                if len(dupe_p) > 1:
                    final_groups.append({
                        "size": size, "hash": fh, "files": dupe_p, 
                        "type": "video" if os.path.splitext(dupe_p[0])[1].lower() in VIDEO_EXTENSIONS else "image"
                    })

        final_groups.sort(key=lambda x: x['size'], reverse=True)
        reclaimable = sum(g['size'] * (len(g['files']) - 1) for g in final_groups)
        self.results = {"groups": final_groups, "stats": {"total": total_files, "found": len(final_groups), "reclaimable": reclaimable}}
        self.progress = {"status": "Complete", "percent": 100, "details": f"Found {len(final_groups)} groups.", "eta": ""}

    def _organize_worker(self, src, target_base, move):
        dt = get_creation_date(src)
        y, m = (dt.strftime('%Y'), dt.strftime('%m')) if dt else ("Unknown", "Unknown")
        tdir = os.path.join(target_base, y, m)
        os.makedirs(tdir, exist_ok=True)
        f = os.path.basename(src)
        dest = os.path.join(tdir, f)

        # Safety Check: If src and dest are the same, skip!
        if os.path.abspath(src) == os.path.abspath(dest):
            return True

        if os.path.exists(dest):
            base, ext = os.path.splitext(f)
            dest = os.path.join(tdir, f"{base}_{int(time.time())}{ext}")
        try:
            if move: shutil.move(src, dest)
            else: shutil.copy2(src, dest)
            return True
        except: return False

    def organize(self, source_dirs, target_base, move=True):
        start_t = time.time()
        self.progress = {"status": "Organizing", "percent": 0, "details": "Indexing...", "eta": ""}
        
        all_to_org = []
        for sdir in source_dirs:
            for root, _, files in os.walk(sdir):
                for f in files:
                    if os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS:
                        all_to_org.append(os.path.join(root, f))
        
        total = len(all_to_org)
        processed = 0
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(self._organize_worker, src, target_base, move): src for src in all_to_org}
            for future in as_completed(futures):
                processed += 1
                if processed % 50 == 0 or processed == total:
                    self._update_progress("Organizing", processed, total, start_t, "Moving library:")
        
        self.progress = {"status": "Complete", "percent": 100, "details": f"Finished {processed} files.", "eta": ""}


INDEX_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Deduplication Studio</title>
    <style>
        :root { --bg: #0f172a; --card: #1e293b; --accent: #38bdf8; --text: #f8fafc; --text-dim: #94a3b8; --danger: #ef4444; }
        * { box-sizing: border-box; font-family: 'Inter', system-ui, sans-serif; }
        body { background: var(--bg); color: var(--text); margin: 0; display: flex; height: 100vh; }
        .sidebar { width: 280px; background: rgba(30, 41, 59, 0.5); border-right: 1px solid rgba(255,255,255,0.1); padding: 2rem; }
        .logo { font-size: 1.5rem; font-weight: 800; margin-bottom: 3rem; background: linear-gradient(to right, #38bdf8, #818cf8); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .step { display: flex; align-items: center; gap: 1rem; margin-bottom: 1.5rem; color: var(--text-dim); cursor: pointer; }
        .step.active { color: var(--accent); font-weight: bold; }
        .step .icon { width: 32px; height: 32px; border-radius: 50%; border: 2px solid currentColor; display: flex; align-items: center; justify-content: center; }
        .main { flex: 1; padding: 2rem; overflow-y: auto; }
        .view { display: none; animation: fadeIn 0.3s ease; }
        .view.active { display: block; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
        .card { background: var(--card); border-radius: 1rem; padding: 2rem; border: 1px solid rgba(255,255,255,0.05); margin-bottom: 2rem; }
        .folder-item { background: rgba(15, 23, 42, 0.5); padding: 1rem; border-radius: 0.5rem; margin-bottom: 0.5rem; display: flex; justify-content: space-between; border: 1px solid rgba(255,255,255,0.05); }
        .btn { background: var(--accent); color: white; border: none; padding: 0.75rem 1.5rem; border-radius: 0.5rem; cursor: pointer; font-weight: 600; }
        .btn-outline { background: transparent; border: 1px solid var(--accent); color: var(--accent); }
        .progress-bar { width: 100%; height: 12px; background: rgba(255,255,255,0.1); border-radius: 6px; overflow: hidden; margin: 1rem 0; }
        .progress-fill { height: 100%; background: var(--accent); width: 0%; transition: 0.3s; }
        .eta-text { font-size: 0.85rem; color: var(--accent); font-weight: 600; }
        .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1.5rem; margin-bottom: 2rem; }
        .stat-card { background: var(--card); padding: 1.5rem; border-radius: 1rem; text-align: center; border: 1px solid rgba(255,255,255,0.05); }
        .stat-val { font-size: 1.8rem; font-weight: 800; color: var(--accent); }
        .dupe-group { background: var(--card); border-radius: 1rem; margin-bottom: 1.5rem; border: 1px solid rgba(255,255,255,0.05); }
        .files-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 1rem; padding: 1rem; }
        .file-card { background: rgba(0,0,0,0.2); border-radius: 0.5rem; overflow: hidden; border: 1px solid transparent; cursor: pointer; position: relative; }
        .file-card.marked { border-color: var(--danger); box-shadow: 0 0 10px rgba(239, 68, 68, 0.3); }
        .media-prev { height: 150px; background: black; display: flex; align-items: center; justify-content: center; }
        .media-prev img, .media-prev video { max-width: 100%; max-height: 100%; }
        table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
        th, td { text-align: left; padding: 1rem; border-bottom: 1px solid rgba(255,255,255,0.05); }
        th { color: var(--text-dim); text-transform: uppercase; font-size: 0.8rem; }
    </style>
</head>
<body>
    <div class="sidebar">
        <div class="logo">Dedupe Studio</div>
        <div class="step active" id="btn-setup" onclick="switchView('setup')"><div class="icon">1</div> Setup</div>
        <div class="step" id="btn-analyze" onclick="switchView('analyze')"><div class="icon">2</div> Analyze</div>
        <div class="step" id="btn-scan" onclick="switchView('scan')"><div class="icon">3</div> Scan</div>
        <div class="step" id="btn-review" onclick="switchView('review')"><div class="icon">4</div> Review</div>
        <div class="step" id="btn-organize" onclick="switchView('organize')"><div class="icon">5</div> Organize</div>
    </div>
    <div class="main">
        <div class="view active" id="view-setup">
            <h1>Workspace Setup</h1>
            <div class="card"><h3>Scan Folders</h3><div id="scan-list"></div><button class="btn btn-outline" onclick="pickFolder('scan')">+ Add Folder</button></div>
            <div class="card"><h3>Source of Truth</h3><select id="pref-select" class="folder-item" style="width:100%; color:white" onchange="state.pref=this.value"></select></div>
            <div class="card"><h3>Destination</h3><div id="target-path" class="folder-item">None</div><button class="btn btn-outline" onclick="pickFolder('target')">Select Target</button></div>
            <div style="display:flex; gap:1rem">
                <button class="btn" style="flex:2; padding:1.2rem" onclick="startWorkflow('scan')">Skip to Duplicate Scan</button>
                <button class="btn btn-outline" style="flex:1" onclick="startWorkflow('analyze')">Run Metadata Audit (Optional)</button>
            </div>
        </div>
        <div class="view" id="view-analyze">
            <h1>Library Analysis</h1>
            <div class="progress-bar"><div class="progress-fill" id="analyze-fill"></div></div>
            <div style="display:flex; justify-content:space-between">
                <span id="analyze-status" style="color:var(--text-dim)">Ready</span>
                <span id="analyze-eta" class="eta-text"></span>
            </div>
            <div class="stat-grid" id="analyze-stats-grid" style="display:none; margin-top:2rem">
                <div class="stat-card"><div class="stat-val" id="ax-total">0</div><div>Total Files</div></div>
                <div class="stat-card"><div class="stat-val" id="ax-exif">0</div><div>With EXIF</div></div>
                <div class="stat-card"><div class="stat-val" id="ax-noexif" style="color:var(--danger)">0</div><div>No EXIF</div></div>
            </div>
            <div class="card" id="analyze-table-card" style="display:none">
                <div style="display:flex; justify-content:space-between"><h3>Monthly Distribution</h3><button class="btn btn-outline" onclick="saveReport('analysis')">Save Report</button></div>
                <table id="analyze-table"><thead><tr><th>Month</th><th>Images</th><th>Videos</th></tr></thead><tbody></tbody></table>
            </div>
            <button class="btn" id="btn-go-scan" style="display:none; width:100%; margin-top:2rem" onclick="startWorkflow('scan')">Continue to Duplicate Scan</button>
        </div>
        <div class="view" id="view-scan">
            <h1>Scanning Duplicates...</h1>
            <div class="progress-bar"><div class="progress-fill" id="scan-fill"></div></div>
            <div style="display:flex; justify-content:space-between">
                <span id="scan-status" style="color:var(--text-dim)">Initializing...</span>
                <span id="scan-eta" class="eta-text"></span>
            </div>
            <div id="scan-done" style="display:none; text-align:right; margin-top:2rem"><button class="btn" onclick="switchView('review')">Review Results</button></div>
        </div>
        <div class="view" id="view-review">
            <h1>Duplicate Review</h1>
            <div class="stat-grid">
                <div class="stat-card"><div class="stat-val" id="st-groups">0</div><div>Groups</div></div>
                <div class="stat-card"><div class="stat-val" id="st-space">0MB</div><div>Reclaimable</div></div>
                <div class="stat-card"><div class="stat-val" id="st-marked">0</div><div>Marked</div></div>
            </div>
            <div style="display:flex; gap:1rem; margin-bottom:1rem">
                <button class="btn btn-outline" onclick="autoMark()">Apply Auto-Preference</button>
                <div style="flex:1"></div>
                <div id="pagination"></div>
            </div>
            <div id="groups-container"></div>
            <div style="display:flex; justify-content:space-between; margin-top:2rem">
                <button class="btn btn-outline" style="color:var(--danger); border-color:var(--danger)" onclick="deleteSelected()">Delete Marked</button>
                <button class="btn" onclick="switchView('organize')">Next: Organize</button>
            </div>
        </div>
        <div class="view" id="view-organize">
            <h1>Final Organization</h1>
            <div class="card">
                <h3>Structured Move</h3>
                <p>Remaining unique files will be sorted into Year/Month folders.</p>
                <button class="btn" style="width:100%" onclick="startWorkflow('organize')">Begin Organization</button>
                <div id="org-ui" style="display:none; margin-top:2rem">
                    <div class="progress-bar"><div class="progress-fill" id="org-fill"></div></div>
                    <div style="display:flex; justify-content:space-between">
                        <span id="org-status">Processing...</span>
                        <span id="org-eta" class="eta-text"></span>
                    </div>
                </div>
            </div>
        </div>
    </div>
    <script>
        let state = { folders: [], pref: '', target: '', groups: [], marked: new Set(), page: 0 };
        async function pickFolder(type) {
            const r = await fetch('/api/pick-folder');
            const d = await r.json();
            if(!d.path) return;
            if(type==='scan') { state.folders.push(d.path); renderSetup(); }
            else { state.target=d.path; document.getElementById('target-path').innerText=d.path; }
        }
        function renderSetup() {
            document.getElementById('scan-list').innerHTML = state.folders.map(f => `<div class="folder-item">${f}</div>`).join('');
            document.getElementById('pref-select').innerHTML = state.folders.map(f => `<option value="${f}">${f}</option>`).join('');
            if(state.folders.length && !state.pref) state.pref = state.folders[0];
        }
        function switchView(id) {
            document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
            document.querySelectorAll('.step').forEach(s => s.classList.remove('active'));
            document.getElementById('view-'+id).classList.add('active');
            document.getElementById('btn-'+id).classList.add('active');
            if(id==='review') renderReview();
        }
        async function startWorkflow(type) {
            if(type==='org-ui') document.getElementById('org-ui').style.display='block';
            switchView(type==='org-ui'?'organize':type);
            if(type==='organize') { document.getElementById('org-ui').style.display='block'; type='org'; }
            const apiType = type==='org'?'organize':type;
            const url = apiType==='analyze' ? '/api/start-analysis' : (apiType==='scan' ? '/api/start-scan' : '/api/start-organize');
            const body = apiType==='organize' ? {source: state.folders, target: state.target, move: true} : {folders: state.folders};
            fetch(url, {method:'POST', body: JSON.stringify(body)});
            const p = setInterval(async ()=>{
                const r = await fetch('/api/status');
                const d = await r.json();
                const fillId = type==='org'?'org-fill':type+'-fill';
                const statusId = type==='org'?'org-status':type+'-status';
                const etaId = type==='org'?'org-eta':type+'-eta';
                document.getElementById(fillId).style.width = d.percent+'%';
                document.getElementById(statusId).innerText = d.details;
                document.getElementById(etaId).innerText = d.eta || '';
                if(d.status==='Complete') {
                    clearInterval(p);
                    if(type==='analyze') showAnalysis();
                    if(type==='scan') document.getElementById('scan-done').style.display='block';
                }
            }, 500);
        }
        async function showAnalysis() {
            const r = await fetch('/api/analysis');
            const d = await r.json();
            document.getElementById('analyze-stats-grid').style.display='grid';
            document.getElementById('analyze-table-card').style.display='block';
            document.getElementById('btn-go-scan').style.display='block';
            document.getElementById('ax-total').innerText = d.total;
            const hasExif = d.total - (d.no_exif.img+d.no_exif.vid);
            document.getElementById('ax-exif').innerText = hasExif;
            document.getElementById('ax-noexif').innerText = (d.no_exif.img+d.no_exif.vid);
            document.querySelector('#analyze-table tbody').innerHTML = Object.keys(d.stats).sort().reverse().map(m => {
                const s = d.stats[m]; return `<tr><td>${m}</td><td>${s.img}</td><td>${s.vid}</td></tr>`;
            }).join('');
        }
        async function renderReview() {
            const r = await fetch('/api/results');
            const d = await r.json(); state.groups = d.groups;
            document.getElementById('st-groups').innerText=d.stats.found;
            document.getElementById('st-space').innerText=formatSz(d.stats.reclaimable);
            const perPage = 10; const pageData = state.groups.slice(state.page*perPage, (state.page+1)*perPage);
            document.getElementById('groups-container').innerHTML = pageData.map((g,i)=>`
                <div class="dupe-group">
                    <div style="padding:1rem; background:rgba(255,255,255,0.05)">Group ${state.page*perPage+i+1}</div>
                    <div class="files-grid">${g.files.map(p => `<div class="file-card ${state.marked.has(p)?'marked':''}" onclick="toggleMark('${p}')"><div class="media-prev">${g.type==='image'?`<img src="/media${encodeURIComponent(p)}">`:`<video src="/media${encodeURIComponent(p)}">`}</div><div style="padding:0.5rem; font-size:0.7rem; color:var(--text-dim); overflow:hidden">${p}</div></div>`).join('')}</div>
                </div>`).join('');
            document.getElementById('pagination').innerHTML = `<button class="btn btn-outline" onclick="state.page=Math.max(0,state.page-1);renderReview()">Prev</button><span>${state.page+1}</span><button class="btn btn-outline" onclick="state.page++;renderReview()">Next</button>`;
            document.getElementById('st-marked').innerText = state.marked.size;
        }
        function toggleMark(p){ if(state.marked.has(p)) state.marked.delete(p); else state.marked.add(p); renderReview(); }
        function formatSz(b){ if(b<1024) return b+' B'; let k=1024, s=['B','KB','MB','GB','TB'], i=Math.floor(Math.log(b)/Math.log(k)); return (b/pow(k,i)).toFixed(2)+' '+s[i]; }
        function autoMark() {
            state.groups.forEach(g=>{ const inP=g.files.filter(f=>f.startsWith(state.pref)), outP=g.files.filter(f=>!f.startsWith(state.pref)); if(inP.length) outP.forEach(f=>state.marked.add(f)); }); renderReview();
        }
        async function deleteSelected() { if(confirm(`Delete ${state.marked.size} files?`)){ await fetch('/api/delete',{method:'POST',body:JSON.stringify({paths:Array.from(state.marked)})}); state.marked.clear(); renderReview(); } }
        async function saveReport() { await fetch('/api/save-analysis', {method:'POST'}); alert("Report saved to analysis_report.txt"); }
    </script>
</body>
</html>
"""

class StudioRequestHandler(BaseHTTPRequestHandler):
    engine = StudioEngine()
    def do_GET(self):
        p = urlparse(self.path).path
        if p == '/':
            self.send_response(200); self.send_header('Content-Type','text/html'); self.end_headers()
            self.wfile.write(INDEX_HTML.encode('utf-8'))
        elif p == '/api/pick-folder':
            path = ""
            try:
                root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
                path = filedialog.askdirectory(parent=root, title="Select Folder"); root.destroy()
            except:
                try: path = subprocess.check_output("osascript -e 'POSIX path of (choose folder with prompt \"Select Folder\")'", shell=True).decode('utf-8').strip()
                except: path = ""
            self.send_json({"path": path})
        elif p == '/api/status': self.send_json(self.engine.progress)
        elif p == '/api/results': self.send_json(self.engine.results)
        elif p == '/api/analysis': self.send_json(self.engine.analysis)
        elif p.startswith('/media'):
            fp = unquote(p[6:])
            if os.path.exists(fp):
                try:
                    self.send_response(200); mt, _ = mimetypes.guess_type(fp)
                    self.send_header('Content-Type', mt or 'application/octet-stream'); self.send_header('Content-Length', os.path.getsize(fp)); self.end_headers()
                    with open(fp, 'rb') as f: self.wfile.write(f.read())
                except (ConnectionResetError, BrokenPipeError): pass
            else: self.send_error(404)
    def do_POST(self):
        cl = int(self.headers['Content-Length']); jd = json.loads(self.rfile.read(cl).decode('utf-8')); p = urlparse(self.path).path
        if p == '/api/start-analysis': threading.Thread(target=self.engine.analyze, args=(jd['folders'],)).start()
        elif p == '/api/start-scan': threading.Thread(target=self.engine.scan, args=(jd['folders'],)).start()
        elif p == '/api/delete':
            for f in jd['paths']:
                try: os.remove(f)
                except: pass
            self.send_json({"done":True})
        elif p == '/api/start-organize': threading.Thread(target=self.engine.organize, args=(jd['source'], jd['target'], jd['move'])).start()
        elif p == '/api/save-analysis':
            with open("analysis_report.txt", "w") as f:
                f.write("LIBRARY ANALYSIS REPORT\n" + "="*30 + "\n")
                res = self.engine.analysis
                for m in sorted(res['stats'].keys(), reverse=True):
                    s = res['stats'][m]
                    f.write(f"{m}: {s['img']} images, {s['vid']} videos\n")
                f.write(f"No EXIF: {res['no_exif']['img']} images, {res['no_exif']['vid']} videos\n")
            self.send_json({"done":True})
        self.send_json({"started": True})
    def send_json(self, data):
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))
    def log_message(self, format, *args): return

class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer): daemon_threads = True

if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("--port", type=int, default=8000); args = p.parse_args()
    print(f"[*] Studio ready at http://localhost:{args.port}"); ThreadingHTTPServer(('localhost', args.port), StudioRequestHandler).serve_forever()
