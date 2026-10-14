import html
import io
import json
import mimetypes
import os
import re
import sys
import threading
import time
import uuid
import webbrowser
import zipfile
from collections import OrderedDict

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory
from google.genai import errors, types

load_dotenv()

import agent  # noqa: E402

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

# ============================================================
#  PERSISTENT CHAT STORAGE — per user
# ============================================================
CHATS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chats.json")

chats = {}
lock = threading.Lock()
CHAT_TTL_SECONDS = 60 * 60 * 24 * 7
MAX_CHATS_PER_USER = 100


def _load_chats_from_disk():
    try:
        with open(CHATS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    migrated = {}
    if data and all(isinstance(v, dict) and "history" in v for v in data.values()):
        first_key = next(iter(data))
        first_val = data[first_key]
        if isinstance(first_val, dict) and not ("chat" in first_val or "history" in first_val):
            migrated = data
        else:
            migrated = {"_legacy": data}
    else:
        migrated = data
    for user_id, sessions in migrated.items():
        for sid, info in sessions.items():
            info["chat"] = None
    return migrated


def _save_chats_to_disk():
    serializable = {}
    for user_id, sessions in chats.items():
        serializable[user_id] = {}
        for sid, info in sessions.items():
            serializable[user_id][sid] = {
                "history": info.get("history", []),
                "title": info.get("title", "New Chat"),
                "created": info.get("created", time.time()),
                "updated": info.get("updated", time.time()),
            }
    try:
        with open(CHATS_PATH, "w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[could not save chats.json: {e}]")


def _cleanup_chats():
    now = time.time()
    changed = False
    for user_id in list(chats.keys()):
        sessions = chats[user_id]
        stale = [sid for sid, info in sessions.items() if now - info.get("updated", 0) > CHAT_TTL_SECONDS]
        for sid in stale:
            sessions.pop(sid, None)
            changed = True
        if len(sessions) > MAX_CHATS_PER_USER:
            ordered = sorted(sessions.items(), key=lambda kv: kv[1].get("updated", 0))
            for sid, _ in ordered[: len(sessions) - MAX_CHATS_PER_USER]:
                sessions.pop(sid, None)
                changed = True
        if not sessions:
            chats.pop(user_id, None)
    if changed:
        _save_chats_to_disk()


def _make_title(question, files):
    if question:
        title = re.sub(r"\s+", " ", question).strip()
        return (title[:48] + "…") if len(title) > 48 else title
    if files:
        return "📎 " + (files[0].filename or "attachment")
    return "New Chat"


chats = _load_chats_from_disk()
_total = sum(len(s) for s in chats.values())
print(f"[loaded {_total} saved chat(s) for {len(chats)} user(s) from chats.json]")


# ---------- Attachment rules ----------
MAX_FILES = 4
MAX_FILE_MB = 10
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".py", ".js", ".html",
    ".css", ".xml", ".yaml", ".yml", ".log", ".sql",
}
MAX_TEXT_CHARS = 100_000


def build_contents(question, files):
    if len(files) > MAX_FILES:
        raise ValueError(f"Please attach at most {MAX_FILES} files at a time.")
    contents = []
    for upload in files:
        name = upload.filename or "file"
        data = upload.read()
        if len(data) > MAX_FILE_MB * 1024 * 1024:
            raise ValueError(f"'{name}' is bigger than {MAX_FILE_MB} MB.")
        extension = os.path.splitext(name)[1].lower()
        mime = (upload.mimetype or "").lower()
        if mime in ("", "application/octet-stream"):
            mime = mimetypes.guess_type(name)[0] or ""
        if mime in IMAGE_TYPES or mime == "application/pdf":
            contents.append(types.Part.from_bytes(data=data, mime_type=mime))
        elif extension in TEXT_EXTENSIONS or mime.startswith("text/"):
            text = data.decode("utf-8", errors="replace")[:MAX_TEXT_CHARS]
            contents.append(f"--- File: {name} ---\n{text}\n--- End of {name} ---")
        else:
            raise ValueError(f"'{name}' is not supported yet.")
    contents.append(question or "Please look at the attached file(s) and tell me what they contain.")
    return contents


# ---------- Reading documents aloud ----------
READABLE_EXTENSIONS = TEXT_EXTENSIONS | {".pdf", ".docx"}
MAX_READ_CHARS = 300_000


class SetupError(Exception):
    pass


def unwrap_lines(text):
    units, current = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if current:
                units.append(current); current = ""
            continue
        if current.endswith("-") and line[:1].islower():
            current = current[:-1] + line
        elif current:
            current += " " + line
        else:
            current = line
        if re.search(r"[.!?:।]$", line):
            units.append(current); current = ""
    if current:
        units.append(current)
    return "\n".join(units)


def extract_pdf(data):
    try:
        from pypdf import PdfReader
    except ImportError:
        raise SetupError("Reading PDFs needs pypdf. Run: python -m pip install pypdf")
    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return unwrap_lines("\n".join(pages))


def extract_docx(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    lines = []
    for paragraph in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
        pieces = re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", paragraph, flags=re.S)
        lines.append(html.unescape("".join(pieces)))
    return "\n".join(lines)


# =====================================================================
#  HTML — NeuraxisAI  (universal per-browser memory + fresh chat per device)
# =====================================================================
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NeuraxisAI</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 200 200%22%3E%3Cdefs%3E%3ClinearGradient id=%22g%22 x1=%220%22 y1=%22200%22 x2=%22200%22 y2=%220%22 gradientUnits=%22userSpaceOnUse%22%3E%3Cstop offset=%220%22 stop-color=%22%233b6bff%22/%3E%3Cstop offset=%22.55%22 stop-color=%22%234f8dff%22/%3E%3Cstop offset=%221%22 stop-color=%22%239b6bff%22/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width=%22200%22 height=%22200%22 rx=%2244%22 fill=%22%23eaf2ff%22/%3E%3Cpath d=%22M40 150 V60 L100 140 L160 55%22 fill=%22none%22 stroke=%22url(%23g)%22 stroke-width=%2230%22 stroke-linecap=%22round%22 stroke-linejoin=%22round%22/%3E%3C/svg%3E">
<meta name="color-scheme" content="light">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Michroma&family=Orbitron:wght@600;800&display=swap" rel="stylesheet">
<style>
:root {
  color-scheme: light;
  --ink:   #0b1730;
  --muted: #5b6f92;
  --line:  rgba(59,107,255,0.18);
  --card:  rgba(255,255,255,0.72);
  --card-strong: rgba(255,255,255,0.94);
  --blue:  #3b6bff;
  --violet:#9b6bff;
  --pink:  #ff5ea8;
  --mint:  #19e3a8;
  --grad:  linear-gradient(135deg, #3b6bff 0%, #4f8dff 45%, #9b6bff 100%);
  --shadow: 0 10px 30px rgba(31,74,161,0.10);
  --brand-font: 'Michroma', 'Inter', system-ui, sans-serif;
  --hero-font: 'Orbitron', 'Michroma', system-ui, sans-serif;
  --sidebar-w: 280px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  font-family: 'Inter', 'Segoe UI', system-ui, -apple-system, Roboto, sans-serif;
  color: var(--ink);
  background: #eef3ff;
  overflow: hidden;
  -webkit-font-smoothing: antialiased;
}

.bg { position: fixed; inset: 0; overflow: hidden; pointer-events: none; z-index: 0; }
.bg .base {
  position: absolute; inset: 0;
  background:
    radial-gradient(1200px 800px at 15% 0%, #c7d8ff 0%, transparent 55%),
    radial-gradient(1000px 700px at 85% 15%, #e7d4ff 0%, transparent 55%),
    radial-gradient(900px 700px at 50% 100%, #ffd9e9 0%, transparent 60%),
    linear-gradient(180deg, #eef3ff 0%, #f7f4ff 45%, #eaf6ff 100%);
}
.bg .blob { position: absolute; border-radius: 50%; filter: blur(70px); opacity: .55; mix-blend-mode: multiply; }
.bg .blob.b1 { width: 46vmin; height: 46vmin; left: 6%;  top: 12%;  background: radial-gradient(circle, #7ea6ff, transparent 70%); animation: blob1 22s ease-in-out infinite; }
.bg .blob.b2 { width: 52vmin; height: 52vmin; right: 4%; top: 8%;   background: radial-gradient(circle, #c39bff, transparent 70%); animation: blob2 26s ease-in-out infinite; }
.bg .blob.b3 { width: 40vmin; height: 40vmin; left: 40%; bottom: 8%; background: radial-gradient(circle, #ff9ec4, transparent 70%); animation: blob3 24s ease-in-out infinite; }
.bg .blob.b4 { width: 44vmin; height: 44vmin; right: 15%; bottom: 12%; background: radial-gradient(circle, #7ff0d3, transparent 70%); animation: blob4 30s ease-in-out infinite; }
@keyframes blob1 { 0%,100% { transform: translate(0,0) scale(1); } 50% { transform: translate(60px,-40px) scale(1.15); } }
@keyframes blob2 { 0%,100% { transform: translate(0,0) scale(1); } 50% { transform: translate(-70px,50px) scale(1.1); } }
@keyframes blob3 { 0%,100% { transform: translate(0,0) scale(1); } 50% { transform: translate(-40px,-30px) scale(1.2); } }
@keyframes blob4 { 0%,100% { transform: translate(0,0) scale(1); } 50% { transform: translate(50px,60px) scale(1.08); } }
.bg .grain {
  position: absolute; inset: 0; opacity: .035;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='120' height='120'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/></filter><rect width='100%25' height='100%25' filter='url(%23n)'/></svg>");
  mix-blend-mode: overlay;
}
.bg .sparkles { position: absolute; inset: 0; }
.bg .spark { position: absolute; width: 5px; height: 5px; border-radius: 50%; background: white; opacity: .8;
  box-shadow: 0 0 12px 2px rgba(255,255,255,.9); }
.bg .spark:nth-child(1) { left: 12%; top: 22%; animation: float1 8s ease-in-out infinite; }
.bg .spark:nth-child(2) { left: 68%; top: 14%; animation: float2 10s ease-in-out infinite; animation-delay: .8s; }
.bg .spark:nth-child(3) { left: 82%; top: 62%; animation: float3 12s ease-in-out infinite; animation-delay: 1.6s; }
.bg .spark:nth-child(4) { left: 30%; top: 78%; animation: float1 9s ease-in-out infinite; animation-delay: .4s; }
.bg .spark:nth-child(5) { left: 52%; top: 40%; animation: float2 11s ease-in-out infinite; animation-delay: 2.2s; }
.bg .spark:nth-child(6) { left: 90%; top: 30%; animation: float3 10s ease-in-out infinite; animation-delay: 1.2s; }
@keyframes float1 { 0%,100% { transform: translateY(0) scale(1); opacity:.8; } 50% { transform: translateY(-24px) scale(1.4); opacity: 1; } }
@keyframes float2 { 0%,100% { transform: translateY(0) scale(1); opacity:.7; } 50% { transform: translateY(-30px) scale(1.5); opacity: 1; } }
@keyframes float3 { 0%,100% { transform: translateY(0) scale(1); opacity:.9; } 50% { transform: translateY(-20px) scale(1.3); opacity: .6; } }

.shell {
  display: grid;
  grid-template-columns: var(--sidebar-w) 1fr;
  height: 100vh; height: 100dvh;
  position: relative; z-index: 2;
  transition: grid-template-columns .32s cubic-bezier(.22,1,.36,1);
}
.shell.collapsed { grid-template-columns: 72px 1fr; }

@media (max-width: 860px) {
  .shell { grid-template-columns: 0 1fr; }
  .sidebar { position: fixed; left: 0; top: 0; bottom: 0; width: 280px; z-index: 40;
    transform: translateX(-100%); transition: transform .32s cubic-bezier(.22,1,.36,1); }
  .shell.sidebar-open .sidebar { transform: translateX(0); box-shadow: 0 20px 60px rgba(31,74,161,0.28); }
  .shell.sidebar-open::after { content:''; position:fixed; inset:0; background:rgba(15,34,68,0.35); z-index:30; backdrop-filter: blur(4px); }
}

.sidebar {
  display: flex; flex-direction: column; gap: 14px;
  padding: 18px 14px 16px;
  background: linear-gradient(180deg, rgba(255,255,255,0.82), rgba(255,255,255,0.58));
  border-right: 1px solid rgba(255,255,255,0.7);
  box-shadow: 4px 0 32px rgba(31,74,161,0.06);
  backdrop-filter: blur(24px) saturate(140%);
  -webkit-backdrop-filter: blur(24px) saturate(140%);
  height: 100%; overflow: hidden;
  transition: padding .32s ease;
  position: relative;
}
.sidebar::after {
  content:''; position:absolute; top:0; right:0; bottom:0; width: 1px;
  background: linear-gradient(180deg, transparent, rgba(155,107,255,0.6), rgba(79,141,255,0.6), transparent);
}
.side-brand { display: flex; align-items: center; gap: 10px; padding: 6px 8px 12px; position: relative; }
.side-brand .logo-tile {
  width: 44px; height: 44px; flex: none;
  border-radius: 12px;
  background: linear-gradient(135deg, #3b6bff 0%, #5b7bff 45%, #9b6bff 100%);
  display: grid; place-items: center;
  box-shadow: 0 8px 20px rgba(59,107,255,0.45), 0 0 0 1.5px rgba(255,255,255,0.5) inset;
  animation: logoPulse 3.5s ease-in-out infinite;
  position: relative; overflow: hidden;
}
.side-brand .logo-tile::before {
  content: ''; position: absolute; inset: 0;
  background: linear-gradient(120deg, transparent 30%, rgba(255,255,255,0.35), transparent 70%);
  transform: translateX(-130%);
  animation: logoShine 4s ease-in-out infinite;
}
@keyframes logoShine {
  0%, 60% { transform: translateX(-130%); }
  80%      { transform: translateX(130%); }
  100%     { transform: translateX(130%); }
}
.side-brand .logo-tile svg { width: 34px; height: 34px; position: relative; z-index: 2; }
@keyframes logoPulse {
  0%,100% { transform: scale(1); box-shadow: 0 8px 20px rgba(59,107,255,0.45), 0 0 0 1.5px rgba(255,255,255,0.5) inset; }
  50%     { transform: scale(1.05); box-shadow: 0 12px 30px rgba(59,107,255,0.65), 0 0 0 1.5px rgba(255,255,255,0.7) inset; }
}
.side-brand .txt { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.side-brand .name {
  font-family: var(--brand-font); font-size: 1.05rem; letter-spacing:.02em; white-space: nowrap;
  background: linear-gradient(90deg, #0b1730 0%, #3b6bff 40%, #9b6bff 70%, #ff5ea8 100%);
  background-size: 200% auto;
  -webkit-background-clip: text; background-clip: text; color: transparent;
  animation: gradientShift 8s ease infinite;
}
@keyframes gradientShift { 0%,100% { background-position: 0% center; } 50% { background-position: 100% center; } }
.side-brand .tagline {
  font-family: var(--hero-font); font-size: .58rem; font-weight: 600;
  letter-spacing: .18em; text-transform: uppercase; white-space: nowrap;
  background: linear-gradient(90deg, #3b6bff, #9b6bff 45%, #ff5ea8 70%, #22c8ff 100%);
  background-size: 250% auto;
  -webkit-background-clip: text; background-clip: text; color: transparent;
  animation: gradientShift 5s ease infinite, taglineGlow 3s ease-in-out infinite;
}
@keyframes taglineGlow {
  0%,100% { filter: drop-shadow(0 0 4px rgba(79,141,255,0.55)); }
  50%     { filter: drop-shadow(0 0 10px rgba(155,107,255,0.9)); }
}
.collapse-btn {
  margin-left: auto; width: 32px; height: 32px; flex: none;
  border: 1px solid var(--line); background: rgba(255,255,255,0.9);
  color: var(--muted); border-radius: 10px;
  cursor: pointer; display: grid; place-items: center; font-size: 15px;
  transition: all .2s ease;
}
.collapse-btn:hover { color: var(--blue); border-color: var(--blue); transform: rotate(180deg) scale(1.08); box-shadow: 0 6px 16px rgba(59,107,255,0.25); }
.newchat {
  position: relative;
  display:flex; align-items:center; justify-content:center; gap:8px;
  padding: 13px 16px; border-radius: 14px;
  border: none; cursor: pointer; overflow: hidden;
  background: linear-gradient(135deg, #3b6bff, #4f8dff 45%, #9b6bff 100%);
  background-size: 200% 200%;
  color: #fff; font-weight: 600; font-size: .92rem;
  box-shadow: 0 10px 26px rgba(79,141,255,0.45);
  transition: transform .2s cubic-bezier(.22,1,.36,1), box-shadow .2s;
  white-space: nowrap;
  animation: gradMove 6s ease infinite;
}
@keyframes gradMove { 0%,100% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } }
.newchat:hover { transform: translateY(-2px) scale(1.02); box-shadow: 0 16px 34px rgba(79,141,255,0.55); }
.newchat:active { transform: translateY(0) scale(.98); }
.newchat::before {
  content:''; position:absolute; inset:0;
  background: linear-gradient(120deg, transparent 30%, rgba(255,255,255,.35), transparent 70%);
  transform: translateX(-120%);
}
.newchat:hover::before { animation: shine .9s ease; }
@keyframes shine { to { transform: translateX(120%); } }
.hist-head { display:flex; align-items:center; justify-content: space-between; padding: 4px 8px; }
.hist-head h3 { font-size: .72rem; letter-spacing: .14em; text-transform: uppercase; color: var(--muted); font-weight: 700; }
.hist-head .count {
  font-size: .7rem; color: #fff; background: var(--grad);
  padding: 2px 9px; border-radius: 999px; box-shadow: 0 4px 10px rgba(79,141,255,0.35);
}
.hist { flex: 1; overflow-y: auto; padding: 4px 2px 4px 0; }
.hist::-webkit-scrollbar { width: 6px; }
.hist::-webkit-scrollbar-thumb { background: rgba(59,107,255,0.22); border-radius: 6px; }
.hist-empty { color: var(--muted); font-size: .82rem; padding: 20px 12px; text-align: center; line-height:1.55; }
.hist-item {
  position: relative;
  display: flex; align-items: center; gap: 8px;
  padding: 11px 12px; margin: 4px 4px;
  border-radius: 12px; cursor: pointer;
  background: transparent; border: 1px solid transparent;
  transition: all .22s cubic-bezier(.22,1,.36,1);
  overflow: hidden;
}
.hist-item::before {
  content:''; position:absolute; left:0; top:0; bottom:0; width: 3px;
  background: var(--grad); border-radius: 4px;
  transform: scaleY(0); transform-origin: center;
  transition: transform .3s ease;
}
.hist-item:hover { background: rgba(255,255,255,0.7); border-color: rgba(79,141,255,0.2); transform: translateX(3px); }
.hist-item.active {
  background: linear-gradient(135deg, rgba(59,107,255,0.14), rgba(155,107,255,0.12));
  border-color: rgba(79,141,255,0.35);
  box-shadow: 0 6px 18px rgba(79,141,255,0.18);
}
.hist-item.active::before { transform: scaleY(1); }
.hist-item .dot2 { width: 8px; height: 8px; border-radius: 50%; background: #c7d6f2; flex: none; transition: all .25s ease; }
.hist-item.active .dot2 { background: var(--grad); box-shadow: 0 0 12px rgba(79,141,255,0.65); animation: dotPulse 2s ease-in-out infinite; }
@keyframes dotPulse { 0%,100% { transform: scale(1); } 50% { transform: scale(1.3); } }
.hist-item .txt { flex: 1; min-width: 0; }
.hist-item .ttl { font-size: .86rem; font-weight: 500; color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hist-item .meta { font-size: .68rem; color: var(--muted); margin-top: 2px; }
.hist-item .del {
  border: none; background: transparent; color: #b7c3da; cursor: pointer;
  padding: 4px 6px; border-radius: 8px; line-height: 1;
  opacity: 0; transition: all .18s ease;
  display: grid; place-items: center;
}
.hist-item .del svg { width: 16px; height: 16px; }
.hist-item:hover .del { opacity: 1; }
.hist-item .del:hover { transform: scale(1.15); }
.side-foot { border-top: 1px solid var(--line); padding-top: 10px; display:flex; flex-direction: column; gap: 6px; }
.side-foot button {
  text-align: left; padding: 10px 12px; border-radius: 10px; border: none; background: transparent;
  color: var(--muted); font-size: .82rem; cursor: pointer;
  display: flex; align-items: center; gap: 8px;
  transition: all .2s ease;
  white-space: nowrap;
}
.side-foot button:hover { background: rgba(79,141,255,0.1); color: var(--blue); transform: translateX(3px); }
.side-foot button svg { width: 20px; height: 20px; flex: none; }
.shell.collapsed .sidebar { padding: 18px 8px 16px; align-items: center; gap: 12px; }
.shell.collapsed .side-brand { justify-content: center; padding: 6px 0 10px; }
.shell.collapsed .side-brand .logo-tile { width: 40px; height: 40px; border-radius: 11px; }
.shell.collapsed .side-brand .txt { display: none; }
.shell.collapsed .collapse-btn { margin: 0 auto; }
.shell.collapsed .newchat { width: 46px; height: 46px; padding: 0; border-radius: 13px; }
.shell.collapsed .newchat .nc-label { display: none; }
.shell.collapsed .hist-head,
.shell.collapsed .hist,
.shell.collapsed .side-foot button span { display: none; }
.shell.collapsed .side-foot { align-items: center; width: 100%; }
.shell.collapsed .side-foot button { width: 46px; height: 46px; padding: 0; justify-content: center; }
.shell.collapsed .newchat,
.shell.collapsed .side-foot button,
.shell.collapsed .collapse-btn { position: relative; }
.shell.collapsed .newchat::after,
.shell.collapsed .side-foot button::after {
  content: attr(data-tip);
  position: absolute; left: calc(100% + 10px); top: 50%; transform: translateY(-50%) scale(.9);
  background: #0b1730; color: #fff; font-size: .74rem; padding: 6px 10px;
  border-radius: 8px; white-space: nowrap; opacity: 0; pointer-events: none;
  transition: opacity .18s ease, transform .18s ease; z-index: 60;
  box-shadow: 0 8px 24px rgba(0,0,0,0.18);
}
.shell.collapsed .newchat:hover::after,
.shell.collapsed .side-foot button:hover::after { opacity: 1; transform: translateY(-50%) scale(1); }

.main {
  position: relative;
  display: flex; flex-direction: column;
  height: 100%; min-width: 0; min-height: 0;
}
#chat {
  flex: 1 1 auto; min-height: 0;
  overflow-y: auto; overflow-x: hidden;
  padding: 24px 22px 20px;
  scroll-behavior: smooth;
  -webkit-overflow-scrolling: touch;
  display: flex; flex-direction: column;
}
#chat::-webkit-scrollbar { width: 8px; }
#chat::-webkit-scrollbar-thumb { background: linear-gradient(180deg, rgba(59,107,255,0.4), rgba(155,107,255,0.4)); border-radius: 8px; }
#chat::-webkit-scrollbar-track { background: transparent; }

.welcome {
  text-align: center; max-width: 780px; margin: auto;
  padding: 24px 10px 40px;
  display: flex; flex-direction: column; justify-content: center; align-items: center;
  width: 100%;
  animation: fadeUp .8s cubic-bezier(.22,1,.36,1);
}
.hero-wrap {
  position: relative; width: 190px; height: 190px; margin: 0 auto 6px;
  display: grid; place-items: center;
}
.hero-wrap::before {
  content: ''; position: absolute; inset: 0;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(59,107,255,0.35) 0%, rgba(155,107,255,0.20) 40%, transparent 70%);
  filter: blur(20px);
  animation: pulseGlow 4s ease-in-out infinite;
}
@keyframes pulseGlow {
  0%,100% { transform: scale(1); opacity: .8; }
  50% { transform: scale(1.15); opacity: 1; }
}
.hero-mark {
  width: 150px; height: 150px; display: block;
  filter: drop-shadow(0 22px 44px rgba(59,107,255,0.55));
  animation: heroFloat 5s ease-in-out infinite;
  position: relative; z-index: 2;
}
@keyframes heroFloat {
  0%,100% { transform: translateY(0) rotate(-2deg); }
  50% { transform: translateY(-14px) rotate(2deg); }
}
.hero-orbit {
  position: absolute; inset: 0; width: 100%; height: 100%;
  animation: orbitSpin 12s linear infinite;
  z-index: 1;
}
@keyframes orbitSpin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
.hero-title {
  font-family: var(--brand-font); font-weight: 400;
  font-size: clamp(1.8rem, 4.4vw, 2.5rem); letter-spacing: .02em;
  margin-bottom: 6px;
  background: linear-gradient(90deg, #0b1730 0%, #3b6bff 30%, #9b6bff 55%, #ff5ea8 80%, #0b1730 100%);
  background-size: 200% auto;
  -webkit-background-clip: text; background-clip: text; color: transparent;
  animation: gradientShift 10s ease infinite;
}
.hero-tagline {
  font-family: var(--hero-font);
  font-size: clamp(.62rem, 1.4vw, .78rem);
  font-weight: 600; letter-spacing: .38em; text-transform: uppercase;
  margin-bottom: 30px;
  background: linear-gradient(90deg, #3b6bff 0%, #9b6bff 45%, #ff5ea8 70%, #22c8ff 100%);
  background-size: 250% auto;
  -webkit-background-clip: text; background-clip: text; color: transparent;
  animation: gradientShift 5s ease infinite, heroTaglineGlow 3s ease-in-out infinite;
}
@keyframes heroTaglineGlow {
  0%,100% { filter: drop-shadow(0 0 6px rgba(79,141,255,0.5)); }
  50%     { filter: drop-shadow(0 0 14px rgba(155,107,255,0.9)); }
}
.help-question {
  font-family: var(--hero-font);
  font-weight: 800;
  font-size: clamp(1.6rem, 3.8vw, 2.4rem);
  letter-spacing: 0.02em;
  color: #9b6bff;
  margin: 0 auto 44px;
  line-height: 1.2;
  text-align: center;
}
.welcome .composer-slot { width: 100%; max-width: 780px; margin: 0 auto; }

.msg { display: flex; gap: 12px; margin: 18px auto; max-width: 860px; animation: rise .42s cubic-bezier(.22,1,.36,1); width: 100%; }
.msg.user { flex-direction: row-reverse; }
.avatar {
  width: 42px; height: 42px; flex: none; border-radius: 50%;
  display: grid; place-items: center; font-size: 19px;
  background: var(--card-strong); border: 1px solid var(--line);
  box-shadow: 0 6px 18px rgba(31,74,161,0.14);
  transition: transform .25s ease;
  overflow: hidden;
}
.msg:hover .avatar { transform: scale(1.08) rotate(-6deg); }
.msg.bot .avatar {
  background: linear-gradient(135deg, #eaf2ff, #f5ecff);
  border-color: rgba(79,141,255,0.4);
  box-shadow: 0 6px 18px rgba(79,141,255,0.28);
}
.msg.user .avatar {
  background: linear-gradient(135deg, #fff0f7, #f3ecff);
  border-color: rgba(255,94,168,0.35);
}
.bubble {
  max-width: 78%; padding: 14px 18px; border-radius: 18px;
  line-height: 1.65; font-size: .97rem; overflow-wrap: anywhere;
  transition: transform .25s ease, box-shadow .25s ease;
  position: relative;
}
.msg:hover .bubble { transform: translateY(-1px); }
.msg.bot .bubble {
  background: var(--card-strong);
  border: 1px solid rgba(255,255,255,0.9);
  border-top-left-radius: 6px;
  box-shadow: 0 10px 30px rgba(31,74,161,0.10), 0 2px 0 rgba(255,255,255,0.6) inset;
}
.msg.user .bubble {
  background: linear-gradient(135deg, #3b6bff 0%, #6b7dff 45%, #9b6bff 100%);
  background-size: 180% 180%;
  animation: gradMove 8s ease infinite;
  color: #fff;
  border-top-right-radius: 6px;
  box-shadow: 0 12px 28px rgba(79,141,255,0.42);
}
.msg.user .bubble .utext { white-space: pre-wrap; }
.msg.error .bubble {
  border-color: rgba(229,72,77,0.5);
  background: linear-gradient(135deg, rgba(229,72,77,0.1), rgba(255,143,143,0.08));
  color: #7a1620;
}
.bubble p { margin: 0 0 8px; } .bubble p:last-child { margin-bottom: 0; }
.bubble ul { margin: 4px 0 8px 20px; } .bubble li { margin: 3px 0; }
.bubble h4 { margin: 8px 0 6px; font-size: 1rem; }

.code-wrap { position: relative; margin: 10px 0; }
.code-wrap pre {
  background: linear-gradient(180deg, #0b1730, #131b3b);
  color: #e8f4ff;
  padding: 16px 18px 14px;
  border-radius: 14px;
  overflow-x: auto;
  margin: 0;
  border: 1px solid rgba(155,107,255,0.25);
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: .88rem;
  line-height: 1.55;
}
.code-wrap pre code { background: none; padding: 0; color: inherit; }
.code-copy {
  position: absolute;
  top: 10px; right: 10px;
  display: flex; align-items: center; gap: 6px;
  padding: 6px 12px;
  border: 1px solid rgba(155,107,255,0.4);
  background: rgba(255,255,255,0.08);
  color: #e8f4ff;
  border-radius: 10px;
  font-size: .74rem; font-weight: 600;
  cursor: pointer;
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  transition: all .2s ease;
  z-index: 2;
  opacity: .6;
}
.code-wrap:hover .code-copy { opacity: 1; }
.code-copy:hover {
  background: rgba(155,107,255,0.25);
  border-color: rgba(255,94,168,0.6);
  transform: translateY(-1px);
}
.code-copy svg { width: 13px; height: 13px; }
.code-copy.copied {
  background: linear-gradient(135deg, #19e3a8, #22c8ff);
  border-color: transparent;
  color: #0b1730;
}
.code-copy.copied svg { color: #0b1730; }
.bubble code {
  background: rgba(59,107,255,0.10);
  padding: 2px 6px;
  border-radius: 6px;
  font-family: Consolas, monospace;
  font-size: .88em;
  color: #274b9c;
}
.att-wrap { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
.att-wrap:last-child { margin-bottom: 0; }
.att-img { max-width: 210px; max-height: 170px; border-radius: 12px; display: block; object-fit: cover; box-shadow: 0 8px 20px rgba(0,0,0,0.15); }
.att-file { display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px; border-radius: 10px; background: rgba(255,255,255,0.28); font-size: .85rem; }

.typing { display: flex; gap: 6px; padding: 8px 4px; }
.typing i {
  width: 9px; height: 9px; border-radius: 50%;
  background: linear-gradient(135deg, #3b6bff, #9b6bff);
  animation: bounce 1.3s infinite;
  box-shadow: 0 0 10px rgba(79,141,255,0.5);
}
.typing i:nth-child(2) { animation-delay: .18s; }
.typing i:nth-child(3) { animation-delay: .36s; }
@keyframes bounce {
  0%,60%,100% { transform: translateY(0) scale(1); opacity:.5; }
  30% { transform: translateY(-7px) scale(1.15); opacity:1; }
}
.composer-previews { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 4px; }
.composer-previews:empty { display: none; }
.preview {
  display: flex; align-items: center; gap: 8px;
  padding: 5px 7px 5px 5px; border-radius: 12px;
  background: linear-gradient(135deg, rgba(59,107,255,0.1), rgba(155,107,255,0.08));
  border: 1px solid rgba(79,141,255,0.25);
  max-width: 230px;
  animation: popIn .3s cubic-bezier(.22,1,.36,1);
  transition: transform .2s ease, box-shadow .2s ease;
}
.preview:hover { transform: translateY(-2px); box-shadow: 0 8px 20px rgba(79,141,255,0.2); }
@keyframes popIn { from { opacity: 0; transform: scale(.85); } to { opacity: 1; transform: scale(1); } }
.preview img { width: 32px; height: 32px; border-radius: 8px; object-fit: cover; }
.preview .ico { width: 32px; height: 32px; display: grid; place-items: center; font-size: 17px; border-radius: 8px;
  background: linear-gradient(135deg, rgba(59,107,255,0.18), rgba(155,107,255,0.15)); }
.preview .nm { font-size: .76rem; max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ink); }
.preview .x {
  border: none; background: transparent; color: var(--muted); cursor: pointer;
  font-size: 16px; line-height: 1; padding: 3px 5px; border-radius: 6px;
  transition: all .15s ease;
}
.preview .x:hover { color: #e5484d; background: rgba(229,72,77,0.12); transform: rotate(90deg); }

.composer-wrap { flex: none; padding: 8px 22px 10px; }
.composer {
  position: relative;
  width: 100%; max-width: 860px; margin: 0 auto;
  background: rgba(255,255,255,0.95);
  border: 1px solid rgba(255,255,255,0.9);
  border-radius: 24px;
  box-shadow: 0 18px 40px rgba(31,74,161,0.12), 0 2px 0 rgba(255,255,255,0.6) inset;
  padding: 14px 16px 10px;
  display: flex; flex-direction: column; gap: 8px;
  transition: box-shadow .3s ease;
  text-align: left;
  backdrop-filter: blur(20px) saturate(160%);
  -webkit-backdrop-filter: blur(20px) saturate(160%);
}
.composer::before {
  content:''; position: absolute; inset: -1px; border-radius: inherit; pointer-events: none;
  background: linear-gradient(135deg, #3b6bff, #9b6bff, #ff5ea8, #19e3a8, #3b6bff);
  background-size: 300% 300%;
  padding: 1.5px;
  -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
  -webkit-mask-composite: xor;
  mask-composite: exclude;
  opacity: 0; transition: opacity .3s ease;
  animation: gradMove 6s linear infinite;
}
.composer:focus-within::before { opacity: 1; }
.composer:focus-within { box-shadow: 0 22px 50px rgba(79,141,255,0.22); }
.welcome .composer { max-width: 780px; }
.composer textarea {
  width: 100%; background: transparent; border: none; outline: none; resize: none;
  color: var(--ink); font: inherit; font-size: 1rem; line-height: 1.5;
  min-height: 28px; max-height: 180px; padding: 4px 2px;
}
.composer textarea::placeholder { color: #9aa8c2; }
.composer-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px; flex-wrap: nowrap;
}
.composer-actions-left { display: flex; align-items: center; gap: 6px; flex: none; }
.composer-actions-right { display: flex; align-items: center; gap: 6px; flex: none; }
.plus-btn {
  width: 40px; height: 40px; border-radius: 50%;
  border: none; background: transparent;
  color: var(--muted); cursor: pointer;
  display: grid; place-items: center; font-size: 22px; font-weight: 300;
  transition: all .22s cubic-bezier(.22,1,.36,1);
}
.plus-btn:hover {
  background: linear-gradient(135deg, rgba(59,107,255,0.14), rgba(155,107,255,0.12));
  color: var(--blue); transform: translateY(-2px) scale(1.08);
}
.plus-btn:active { transform: translateY(0) scale(.95); }
.plus-btn svg { width: 22px; height: 22px; }
.action-menu {
  position: absolute;
  bottom: calc(100% + 10px);
  left: 8px;
  min-width: 230px;
  background: rgba(255,255,255,0.98);
  border: 1px solid rgba(255,255,255,0.9);
  border-radius: 16px;
  box-shadow: 0 22px 50px rgba(31,74,161,0.20), 0 0 0 1px rgba(155,107,255,0.18);
  padding: 8px;
  backdrop-filter: blur(20px) saturate(160%);
  -webkit-backdrop-filter: blur(20px) saturate(160%);
  opacity: 0; transform: translateY(8px) scale(.96);
  pointer-events: none;
  transition: opacity .2s ease, transform .25s cubic-bezier(.22,1,.36,1);
  z-index: 25;
}
.action-menu.open { opacity: 1; transform: translateY(0) scale(1); pointer-events: auto; }
.action-menu button {
  display: flex; align-items: center; gap: 12px;
  width: 100%; padding: 10px 12px;
  border: none; background: transparent;
  border-radius: 12px;
  font-size: .88rem; font-weight: 500; color: var(--ink);
  text-align: left; cursor: pointer;
  transition: all .18s ease;
}
.action-menu button:hover {
  background: linear-gradient(135deg, rgba(59,107,255,0.10), rgba(155,107,255,0.08));
  transform: translateX(2px);
}
.action-menu .menu-ico {
  width: 34px; height: 34px; border-radius: 10px;
  display: grid; place-items: center; flex: none;
  background: linear-gradient(135deg, rgba(59,107,255,0.14), rgba(155,107,255,0.12));
}
.action-menu .menu-ico svg { width: 20px; height: 20px; }
.action-menu .menu-txt { display: flex; flex-direction: column; gap: 1px; }
.action-menu .menu-txt small { font-size: .7rem; color: var(--muted); font-weight: 400; }

.icon-btn-2 {
  width: 40px; height: 40px; border-radius: 50%;
  border: none; background: transparent;
  cursor: pointer; display: grid; place-items: center;
  transition: all .22s cubic-bezier(.22,1,.36,1);
  position: relative;
  padding: 0;
}
.icon-btn-2:hover { transform: translateY(-2px) scale(1.08); }
.icon-btn-2:active { transform: translateY(0) scale(.95); }
.icon-btn-2.recording { animation: recPulse 1.3s infinite; }
@keyframes recPulse {
  0% { box-shadow: 0 0 0 0 rgba(255,59,48,0.6); }
  70% { box-shadow: 0 0 0 18px rgba(255,59,48,0); }
  100% { box-shadow: 0 0 0 0 rgba(255,59,48,0); }
}
.ai-sparkle {
  width: 40px; height: 40px; border-radius: 12px;
  display: block; flex: none;
  box-shadow: 0 8px 18px rgba(155,107,255,0.4);
  cursor: pointer;
  transition: all .22s cubic-bezier(.22,1,.36,1);
}
.ai-sparkle:hover { transform: translateY(-2px) scale(1.06); }
.send-btn {
  width: 42px; height: 42px; border-radius: 50%;
  border: none; cursor: pointer;
  display: grid; place-items: center;
  background: linear-gradient(135deg, #c7d2fe, #c4b5fd);
  color: #ffffff; font-size: 18px; font-weight: 700;
  box-shadow: 0 6px 16px rgba(79,141,255,0.28);
  transition: all .25s cubic-bezier(.22,1,.36,1);
  position: relative; overflow: hidden;
}
.send-btn::after {
  content:''; position:absolute; inset:0; border-radius:50%;
  background: radial-gradient(circle at 50% 50%, rgba(255,255,255,.5), transparent 60%);
  opacity: 0; transition: opacity .2s;
}
.send-btn:hover::after { opacity: 1; }
.send-btn.ready {
  background: linear-gradient(135deg, #3b6bff, #4f8dff 45%, #9b6bff 100%);
  background-size: 200% 200%;
  animation: gradMove 4s ease infinite;
  box-shadow: 0 12px 30px rgba(79,141,255,0.55), 0 0 0 4px rgba(79,141,255,0.15);
}
.send-btn:hover { transform: translateY(-2px) scale(1.08); }
.send-btn:active { transform: translateY(0) scale(.92); }
.send-btn:disabled { opacity: .55; cursor: not-allowed; transform: none; animation: none; }
.send-btn svg { width: 20px; height: 20px; }
.foot { flex: none; text-align: center; font-size: .74rem; color: var(--muted); padding: 0 10px 12px; opacity: .85; }

.dropzone {
  position: fixed; inset: 12px;
  border: 3px dashed transparent;
  background:
    linear-gradient(rgba(255,255,255,0.85), rgba(255,255,255,0.85)) padding-box,
    linear-gradient(135deg, #3b6bff, #9b6bff, #ff5ea8, #19e3a8) border-box;
  border-radius: 26px;
  display: none; place-items: center;
  font-size: 1.3rem; font-weight: 700; color: var(--blue);
  z-index: 50; pointer-events: none;
  backdrop-filter: blur(6px);
  letter-spacing: .02em;
}
body.dragging .dropzone { display: grid; animation: popIn .3s ease; }

#toast {
  position: fixed; top: 22px; left: 50%; transform: translate(-50%, -120px);
  background: linear-gradient(135deg, #0b1730, #1c2547);
  color: #fff; padding: 12px 22px; border-radius: 14px;
  font-size: .9rem; font-weight: 500;
  z-index: 60; transition: transform .4s cubic-bezier(.22,1,.36,1);
  box-shadow: 0 20px 44px rgba(15,34,68,0.42), 0 0 0 1px rgba(155,107,255,0.3);
  max-width: 90%;
}
#toast.show { transform: translate(-50%, 0); }

.reader {
  position: fixed; inset: 0;
  background: rgba(15,34,68,0.45);
  backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
  display: none; place-items: center; z-index: 70; padding: 16px;
}
.reader.open { display: grid; animation: popIn .3s ease; }
.reader-card {
  width: min(760px, 100%); height: min(86vh, 800px);
  display: flex; flex-direction: column;
  background: rgba(255,255,255,0.98);
  border: 1px solid rgba(255,255,255,0.9);
  border-radius: 26px; padding: 22px;
  box-shadow: 0 30px 80px rgba(15,34,68,0.4), 0 0 0 1px rgba(155,107,255,0.25);
  animation: rise .35s cubic-bezier(.22,1,.36,1);
}
.reader-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 12px; }
.reader-head h3 { font-size: 1.05rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  display: flex; align-items: center; gap: 10px; }
.reader-head h3 svg { width: 24px; height: 24px; flex: none; }
.reader-src { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 10px; }
.reader-info { font-size: .8rem; color: var(--muted); }
.paste-box { margin-bottom: 10px; display: flex; flex-direction: column; gap: 8px; align-items: flex-start; }
.paste-box textarea { width: 100%; border: 1px solid var(--line); border-radius: 12px; padding: 10px;
  background: rgba(79,141,255,0.05); outline: none; transition: border-color .2s; }
.paste-box textarea:focus { border-color: var(--blue); }
.lines { flex: 1; overflow-y: auto; padding: 4px; border-radius: 14px; background: rgba(79,141,255,0.05); }
.line { padding: 10px 14px; margin: 4px 0; border-radius: 12px; cursor: pointer; line-height: 1.65;
  border: 1px solid transparent; opacity: .7; transition: all .22s cubic-bezier(.22,1,.36,1);
  overflow-wrap: anywhere; }
.line:hover { opacity: 1; transform: translateX(3px); background: rgba(255,255,255,0.7); }
.line.done { opacity: .4; }
.line.active {
  opacity: 1; font-weight: 500;
  background: linear-gradient(135deg, rgba(59,107,255,0.14), rgba(155,107,255,0.12));
  border-color: rgba(79,141,255,0.5);
  box-shadow: 0 6px 18px rgba(79,141,255,0.2);
}
.empty { padding: 40px 20px; text-align: center; color: var(--muted); line-height: 1.7; }
.reader-ctrl { margin-top: 12px; display: flex; flex-direction: column; gap: 10px; }
.transport { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.play {
  width: 54px; height: 54px; border: none; border-radius: 50%;
  background: linear-gradient(135deg, #3b6bff, #4f8dff 45%, #9b6bff 100%);
  background-size: 200% 200%;
  animation: gradMove 5s ease infinite;
  color: #fff; font-size: 22px; cursor: pointer;
  box-shadow: 0 14px 32px rgba(79,141,255,0.5);
  transition: transform .22s cubic-bezier(.22,1,.36,1);
}
.play:hover { transform: scale(1.1) rotate(-6deg); }
.pos { font-size: .85rem; color: var(--muted); margin-left: 6px; }
.settings { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; font-size: .85rem; }
.settings label { display: flex; align-items: center; gap: 8px; }
.settings input[type=range] { accent-color: var(--blue); width: 120px; }
.settings select { max-width: 100%; flex: 1; min-width: 150px; padding: 6px 10px;
  border: 1px solid var(--line); border-radius: 10px; background: #fff; outline: none; }

.ghost {
  background: rgba(255,255,255,0.9); color: var(--ink);
  border: 1px solid var(--line); padding: 9px 14px; border-radius: 12px;
  cursor: pointer; font-size: .85rem; font-weight: 500;
  transition: all .2s ease;
  display: inline-flex; align-items: center; gap: 6px;
}
.ghost:hover {
  border-color: var(--blue); color: var(--blue);
  box-shadow: 0 8px 20px rgba(79,141,255,0.2);
  transform: translateY(-1px);
}
.imgcard { margin-top: 12px; border-radius: 16px; overflow: hidden;
  border: 1px solid rgba(255,255,255,0.9);
  max-width: 420px; background: #fff;
  box-shadow: 0 14px 36px rgba(31,74,161,0.15);
  animation: popIn .4s cubic-bezier(.22,1,.36,1);
}
.imgcard img { display: block; width: 100%; height: auto; cursor: zoom-in; transition: transform .4s ease; }
.imgcard img:hover { transform: scale(1.03); }
.imgcard.drawing { max-width: 520px; }
.imgcard.drawing img { background: #ffffff; cursor: default; }
.imgbtns { display: flex; gap: 8px; }
.imgbar { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 10px 14px;
  font-size: .8rem; color: var(--muted);
  background: linear-gradient(135deg, rgba(59,107,255,0.06), rgba(155,107,255,0.05)); }
.filecard {
  margin-top: 12px; display: flex; align-items: center; gap: 14px;
  padding: 16px 18px; border-radius: 16px;
  border: 1px solid rgba(79,141,255,0.4);
  background: linear-gradient(135deg, rgba(59,107,255,0.1), rgba(155,107,255,0.08));
  max-width: 480px;
  box-shadow: 0 14px 32px rgba(79,141,255,0.18);
  animation: popIn .4s cubic-bezier(.22,1,.36,1);
  transition: transform .25s ease;
}
.filecard:hover { transform: translateY(-3px); }
.fileico { width: 50px; height: 50px; flex: none; border-radius: 14px;
  background: linear-gradient(135deg, #3b6bff, #9b6bff);
  display: grid; place-items: center; font-size: 24px; color:#fff;
  box-shadow: 0 10px 24px rgba(79,141,255,0.45);
}
.fileinfo { flex: 1; min-width: 0; }
.filename { font-weight: 600; font-size: .95rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.filemeta { font-size: .78rem; color: var(--muted); margin-top: 2px; }
.dl {
  display: inline-block; flex: none; padding: 8px 16px; border-radius: 12px;
  background: linear-gradient(135deg, #3b6bff, #9b6bff);
  color: #fff !important; text-decoration: none;
  font-size: .82rem; font-weight: 600;
  box-shadow: 0 10px 22px rgba(79,141,255,0.45);
  transition: transform .2s cubic-bezier(.22,1,.36,1);
}
.dl:hover { transform: translateY(-2px) scale(1.04); }
.imgbar .dl { padding: 6px 14px; box-shadow: none; }

@keyframes rise { from { opacity: 0; transform: translateY(14px) scale(.98); } to { opacity: 1; transform: translateY(0) scale(1); } }
@keyframes fadeUp { from { opacity:0; transform: translateY(14px); } to { opacity:1; transform: none; } }

.confirm { position: fixed; inset: 0; background: rgba(15,34,68,0.45); backdrop-filter: blur(8px);
  display: none; place-items: center; z-index: 80; padding: 16px; }
.confirm.open { display: grid; animation: popIn .25s ease; }
.confirm-card { background: #fff; border-radius: 22px; padding: 24px; max-width: 400px; width: 100%;
  box-shadow: 0 30px 80px rgba(15,34,68,0.35); animation: rise .35s cubic-bezier(.22,1,.36,1); }
.confirm-card h3 { font-size: 1.1rem; margin-bottom: 8px; }
.confirm-card p { font-size: .88rem; color: var(--muted); line-height: 1.55; margin-bottom: 20px; }
.confirm-card .row { display: flex; gap: 8px; justify-content: flex-end; }
.btn-danger {
  background: linear-gradient(135deg, #ef4444, #e5484d); color: #fff; border: none;
  padding: 11px 20px; border-radius: 12px; cursor: pointer; font-weight: 600; font-size: .88rem;
  box-shadow: 0 10px 22px rgba(229,72,77,0.35);
  transition: transform .2s cubic-bezier(.22,1,.36,1);
}
.btn-danger:hover { transform: translateY(-2px) scale(1.03); }
.btn-cancel {
  background: transparent; border: 1px solid var(--line); padding: 11px 20px; border-radius: 12px;
  cursor: pointer; font-weight: 500; font-size: .88rem; color: var(--ink);
  transition: all .2s ease;
}
.btn-cancel:hover { background: rgba(79,141,255,0.08); border-color: var(--blue); color: var(--blue); }

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .001ms !important; animation-iteration-count: 1 !important; transition-duration: .001ms !important; }
}
@media (max-width: 640px) {
  .action-menu { left: 0; right: 0; min-width: 0; }
  .action-menu button { padding: 9px 10px; }
}
</style>
</head>
<body>

<div class="bg" aria-hidden="true">
  <div class="base"></div>
  <div class="blob b1"></div>
  <div class="blob b2"></div>
  <div class="blob b3"></div>
  <div class="blob b4"></div>
  <div class="sparkles">
    <span class="spark"></span><span class="spark"></span><span class="spark"></span>
    <span class="spark"></span><span class="spark"></span><span class="spark"></span>
  </div>
  <div class="grain"></div>
</div>

<div class="dropzone">✨ Drop files here to attach them</div>
<div id="toast"></div>

<div class="confirm" id="confirmBox">
  <div class="confirm-card">
    <h3 id="confirmTitle">Delete this chat?</h3>
    <p id="confirmText">This will remove the conversation from your history. You cannot undo this.</p>
    <div class="row">
      <button class="btn-cancel" id="confirmCancel">Cancel</button>
      <button class="btn-danger" id="confirmOk">Delete</button>
    </div>
  </div>
</div>

<div class="reader" id="reader">
  <div class="reader-card">
    <div class="reader-head">
      <h3>
        <svg viewBox="0 0 64 64" aria-hidden="true"><use href="#i-readaloud"/></svg>
        <span id="readerTitleText">Read aloud</span>
      </h3>
      <button class="ghost" id="readerClose" title="Close">✕</button>
    </div>
    <div class="reader-src">
      <button class="ghost" id="readerFile">📂 Open document</button>
      <input type="file" id="readerFileInput" hidden accept=".pdf,.docx,.txt,.md,.csv,.tsv,.json,.py,.js,.html,.css,.xml,.yaml,.yml,.log,.sql">
      <button class="ghost" id="readerPaste">📋 Paste text</button>
      <span id="readerInfo" class="reader-info"></span>
    </div>
    <div id="readerPasteBox" class="paste-box" style="display:none">
      <textarea id="readerPasteText" rows="5" placeholder="Paste the text you want me to read aloud..."></textarea>
      <button class="ghost" id="readerLoad">▶ Load text</button>
    </div>
    <div id="readerLines" class="lines"></div>
    <div class="reader-ctrl">
      <div class="transport">
        <button class="ghost" id="rPrev" title="Previous line">⏮</button>
        <button class="play" id="rPlay" title="Play / pause">▶</button>
        <button class="ghost" id="rNext" title="Next line">⏭</button>
        <button class="ghost" id="rStop" title="Stop">⏹</button>
        <span id="rPos" class="pos">No text loaded</span>
      </div>
      <div class="settings">
        <label>Speed <input type="range" id="rRate" min="0.5" max="2" step="0.1" value="1"> <span id="rRateVal">1.0×</span></label>
        <select id="rVoice" class="lang"></select>
      </div>
    </div>
  </div>
</div>

<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">
  <defs>
    <linearGradient id="nx-gl" gradientUnits="userSpaceOnUse" x1="0" y1="200" x2="200" y2="0">
      <stop offset="0" stop-color="#3b6bff"/><stop offset=".55" stop-color="#4f8dff"/><stop offset="1" stop-color="#9b6bff"/>
    </linearGradient>
    <linearGradient id="nx-spark" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#22c8ff"/>
      <stop offset="55%" stop-color="#7a6bff"/>
      <stop offset="100%" stop-color="#ff5ea8"/>
    </linearGradient>

    <symbol id="nx-flow" viewBox="0 0 200 200">
      <path d="M60 165 V60 L140 155 V55"
            fill="none" stroke="#ffffff" stroke-width="26"
            stroke-linejoin="round" stroke-linecap="round"/>
    </symbol>

    <symbol id="nx-robot" viewBox="0 0 200 200">
      <line x1="100" y1="30" x2="100" y2="42" stroke="#6b7dff" stroke-width="3" stroke-linecap="round"/>
      <circle cx="100" cy="26" r="6" fill="url(#nx-gl)">
        <animate attributeName="r" values="6;8;6" dur="2s" repeatCount="indefinite"/>
      </circle>
      <rect x="55" y="42" width="90" height="66" rx="30" fill="#eaf0ff" stroke="#a3b6ff" stroke-width="2"/>
      <rect x="55" y="42" width="90" height="66" rx="30" fill="url(#nx-gl)" opacity="0.12"/>
      <rect x="66" y="58" width="68" height="34" rx="17" fill="#0b1730"/>
      <path d="M78 76 Q84 68 90 76" fill="none" stroke="#22c8ff" stroke-width="3.2" stroke-linecap="round">
        <animate attributeName="stroke" values="#22c8ff;#9b6bff;#ff5ea8;#22c8ff" dur="4s" repeatCount="indefinite"/>
      </path>
      <path d="M110 76 Q116 68 122 76" fill="none" stroke="#22c8ff" stroke-width="3.2" stroke-linecap="round">
        <animate attributeName="stroke" values="#22c8ff;#9b6bff;#ff5ea8;#22c8ff" dur="4s" repeatCount="indefinite"/>
      </path>
      <circle cx="52" cy="76" r="8" fill="url(#nx-gl)"/>
      <circle cx="148" cy="76" r="8" fill="url(#nx-gl)"/>
      <rect x="72" y="112" width="56" height="58" rx="22" fill="#eaf0ff" stroke="#a3b6ff" stroke-width="2"/>
      <rect x="72" y="112" width="56" height="58" rx="22" fill="url(#nx-gl)" opacity="0.1"/>
      <text x="100" y="148" text-anchor="middle" font-family="Michroma, sans-serif" font-weight="700" font-size="26" fill="url(#nx-gl)">N</text>
      <g transform="translate(132,110)">
        <rect width="48" height="44" rx="8" fill="rgba(255,255,255,0.85)" stroke="#9b6bff" stroke-width="2">
          <animate attributeName="opacity" values="0.8;1;0.8" dur="3s" repeatCount="indefinite"/>
        </rect>
        <line x1="8" y1="12" x2="40" y2="12" stroke="#7a6bff" stroke-width="2" stroke-linecap="round"/>
        <line x1="8" y1="20" x2="34" y2="20" stroke="#7a6bff" stroke-width="2" stroke-linecap="round" opacity="0.8"/>
        <line x1="8" y1="28" x2="40" y2="28" stroke="#7a6bff" stroke-width="2" stroke-linecap="round" opacity="0.6"/>
        <line x1="8" y1="36" x2="26" y2="36" stroke="#7a6bff" stroke-width="2" stroke-linecap="round" opacity="0.4"/>
      </g>
      <g transform="translate(62,140)">
        <ellipse cx="0" cy="0" rx="8" ry="10" fill="url(#nx-gl)">
          <animateTransform attributeName="transform" type="rotate"
            values="-15 0 0; 15 0 0; -15 0 0" dur="2s" repeatCount="indefinite"/>
        </ellipse>
      </g>
      <ellipse cx="100" cy="188" rx="52" ry="6" fill="url(#nx-gl)" opacity="0.35">
        <animate attributeName="opacity" values="0.25;0.5;0.25" dur="2.5s" repeatCount="indefinite"/>
      </ellipse>
    </symbol>

    <symbol id="nx-mark" viewBox="0 0 200 200">
      <path d="M40 160 V55 L100 145 L160 50" fill="none" stroke="url(#nx-gl)" stroke-width="30" stroke-linejoin="round" stroke-linecap="round"/>
      <circle cx="168" cy="42" r="12" fill="url(#nx-gl)"/>
    </symbol>

    <symbol id="i-paperclip" viewBox="0 0 24 24">
      <path d="M16.5 6.5 L8.7 14.3 a2.5 2.5 0 0 0 3.5 3.5 L19 11 a5 5 0 0 0 -7 -7 L5.5 10.5 a7 7 0 0 0 10 10 L22 14"
            fill="none" stroke="#4a9ce8" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
    </symbol>
    <symbol id="i-mic" viewBox="0 0 48 48">
      <circle cx="24" cy="24" r="24" fill="#ff3b30"/>
      <path d="M48 24 A24 24 0 0 1 0 24 A24 24 0 0 0 48 24 Z" fill="#e0281f" opacity="0.35"/>
      <rect x="17" y="12" width="14" height="20" rx="7" fill="#ffffff"/>
      <line x1="19" y1="17" x2="22.5" y2="17" stroke="#ff3b30" stroke-width="1.6" stroke-linecap="round"/>
      <line x1="19" y1="20" x2="22.5" y2="20" stroke="#ff3b30" stroke-width="1.6" stroke-linecap="round"/>
      <line x1="19" y1="23" x2="22.5" y2="23" stroke="#ff3b30" stroke-width="1.6" stroke-linecap="round"/>
      <path d="M12 22 v2 a12 12 0 0 0 24 0 v-2" fill="none" stroke="#ffffff" stroke-width="2.6" stroke-linecap="round"/>
      <line x1="24" y1="36" x2="24" y2="42" stroke="#ffffff" stroke-width="2.6" stroke-linecap="round"/>
      <line x1="20" y1="42" x2="28" y2="42" stroke="#ffffff" stroke-width="2.6" stroke-linecap="round"/>
    </symbol>
    <symbol id="i-readaloud" viewBox="0 0 64 64">
      <path d="M32 16 C24 10 14 10 8 13 V46 C14 43 24 43 32 49 Z" fill="none" stroke="#4a9ce8" stroke-width="3.4" stroke-linejoin="round"/>
      <path d="M32 16 C40 10 50 10 56 13 V46 C50 43 40 43 32 49 Z" fill="none" stroke="#4a9ce8" stroke-width="3.4" stroke-linejoin="round"/>
      <line x1="14" y1="20" x2="26" y2="20" stroke="#4a9ce8" stroke-width="2.4" stroke-linecap="round"/>
      <line x1="14" y1="27" x2="26" y2="27" stroke="#4a9ce8" stroke-width="2.4" stroke-linecap="round"/>
      <line x1="14" y1="34" x2="26" y2="34" stroke="#4a9ce8" stroke-width="2.4" stroke-linecap="round"/>
      <line x1="38" y1="20" x2="50" y2="20" stroke="#4a9ce8" stroke-width="2.4" stroke-linecap="round"/>
      <line x1="38" y1="27" x2="50" y2="27" stroke="#4a9ce8" stroke-width="2.4" stroke-linecap="round"/>
      <line x1="38" y1="34" x2="50" y2="34" stroke="#4a9ce8" stroke-width="2.4" stroke-linecap="round"/>
      <path d="M46 34 v10 h5 l8 6 v-22 l-8 6 h-5 z" fill="none" stroke="#4a9ce8" stroke-width="3.2" stroke-linejoin="round"/>
      <path d="M62 38 a4 4 0 0 1 0 6" fill="none" stroke="#4a9ce8" stroke-width="2.6" stroke-linecap="round"/>
      <path d="M62 34 a8 8 0 0 1 0 14" fill="none" stroke="#4a9ce8" stroke-width="2.6" stroke-linecap="round" opacity="0.7"/>
    </symbol>
    <symbol id="i-trash" viewBox="0 0 64 64">
      <path d="M14 20 H50 L47 60 a4 4 0 0 1 -4 3 H21 a4 4 0 0 1 -4 -3 Z" fill="#ff5e1f"/>
      <rect x="10" y="14" width="44" height="8" rx="3" fill="#ff5e1f"/>
      <rect x="24" y="8" width="16" height="6" rx="3" fill="#ff5e1f"/>
      <path d="M24 6 h16 a4 4 0 0 1 4 4 v2 h-24 v-2 a4 4 0 0 1 4 -4 z" fill="none" stroke="#ff5e1f" stroke-width="3" stroke-linejoin="round"/>
      <rect x="24" y="26" width="3.2" height="30" rx="1.6" fill="#ffffff"/>
      <rect x="30.4" y="26" width="3.2" height="30" rx="1.6" fill="#ffffff"/>
      <rect x="36.8" y="26" width="3.2" height="30" rx="1.6" fill="#ffffff"/>
    </symbol>
    <symbol id="i-sparkle" viewBox="0 0 64 64">
      <rect width="64" height="64" rx="16" fill="url(#nx-spark)"/>
      <rect x="14" y="40" width="6" height="24" rx="3" transform="rotate(-45 17 52)" fill="#ffffff"/>
      <rect x="34" y="20" width="8" height="8" rx="2" transform="rotate(-45 38 24)" fill="#ffffff"/>
      <path d="M22 14 C22.6 17.6 24.4 19.4 28 20 C24.4 20.6 22.6 22.4 22 26 C21.4 22.4 19.6 20.6 16 20 C19.6 19.4 21.4 17.6 22 14 Z" fill="#ffffff"/>
      <path d="M48 18 C48.4 20.4 49.6 21.6 52 22 C49.6 22.4 48.4 23.6 48 26 C47.6 23.6 46.4 22.4 44 22 C46.4 21.6 47.6 20.4 48 18 Z" fill="#ffffff"/>
      <path d="M42 34 C42.3 35.8 43.2 36.7 45 37 C43.2 37.3 42.3 38.2 42 40 C41.7 38.2 40.8 37.3 39 37 C40.8 36.7 41.7 35.8 42 34 Z" fill="#ffffff"/>
      <circle cx="34" cy="42" r="1.4" fill="#ffffff"/>
      <circle cx="50" cy="44" r="1.4" fill="#ffffff"/>
      <circle cx="30" cy="18" r="1.4" fill="#ffffff"/>
    </symbol>
    <symbol id="i-copy" viewBox="0 0 24 24">
      <rect x="9" y="9" width="13" height="13" rx="2.5" ry="2.5" fill="none" stroke="currentColor" stroke-width="2"/>
      <path d="M5 15 H4 a2 2 0 0 1 -2 -2 V4 a2 2 0 0 1 2 -2 h9 a2 2 0 0 1 2 2 v1" fill="none" stroke="currentColor" stroke-width="2"/>
    </symbol>
    <symbol id="i-download" viewBox="0 0 24 24">
      <path d="M21 15 v4 a2 2 0 0 1 -2 2 H5 a2 2 0 0 1 -2 -2 v-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
      <polyline points="7 10 12 15 17 10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      <line x1="12" y1="15" x2="12" y2="3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
    </symbol>
  </defs>
</svg>

<div class="shell" id="shell">
  <aside class="sidebar">
    <div class="side-brand">
      <div class="logo-tile">
        <svg viewBox="0 0 200 200"><use href="#nx-flow"/></svg>
      </div>
      <div class="txt">
        <div class="name">NeuraxisAI</div>
        <div class="tagline">Ideas to Intelligence</div>
      </div>
      <button class="collapse-btn" id="collapseBtn" title="Hide sidebar" aria-label="Hide sidebar">‹</button>
    </div>

    <button class="newchat" id="newChatBtn" data-tip="New Chat">
      <span class="nc-plus" style="font-size:1.1rem">＋</span>
      <span class="nc-label">New Chat</span>
    </button>

    <div class="hist-head">
      <h3>History</h3>
      <span class="count" id="histCount">0</span>
    </div>

    <div class="hist" id="histList">
      <div class="hist-empty">No conversations yet.<br>Start chatting and your history will appear here.</div>
    </div>

    <div class="side-foot">
      <button id="saveHistoryBtn" data-tip="Save chat history">
        <svg viewBox="0 0 24 24"><use href="#i-download"/></svg>
        <span>Save Chat History</span>
      </button>
      <button id="clearAllBtn" data-tip="Clear all history">
        <svg viewBox="0 0 64 64"><use href="#i-trash"/></svg>
        <span>Clear all history</span>
      </button>
      <button id="readerOpen" data-tip="Read aloud">
        <svg viewBox="0 0 64 64"><use href="#i-readaloud"/></svg>
        <span>Read aloud</span>
      </button>
    </div>
  </aside>

  <div class="main">
    <div id="chat">
      <div class="welcome" id="welcome">
        <div class="hero-wrap">
          <svg class="hero-orbit" viewBox="0 0 200 200">
            <ellipse cx="100" cy="100" rx="92" ry="30" transform="rotate(-24 100 100)"
                     fill="none" stroke="url(#nx-gl)" stroke-width="2" opacity="0.55"/>
            <circle cx="192" cy="100" r="4" fill="#22c8ff"/>
          </svg>
          <svg class="hero-mark" viewBox="0 0 200 200"><use href="#nx-robot"/></svg>
        </div>
        <h2 class="hero-title">NeuraxisAI</h2>
        <div class="hero-tagline">Ideas to Intelligence</div>
        <h1 class="help-question">How May I Assist You Today?</h1>

        <div class="composer-slot" id="welcomeComposerSlot">
          <form class="composer" id="composer" autocomplete="off">
            <div class="composer-previews" id="previews"></div>
            <textarea id="input" rows="1" placeholder="Ask me anything..." aria-label="Message"></textarea>

            <div class="composer-row">
              <div class="composer-actions-left">
                <button type="button" class="plus-btn" id="plusBtn" title="More actions" aria-label="More actions">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">
                    <line x1="12" y1="5" x2="12" y2="19"></line>
                    <line x1="5" y1="12" x2="19" y2="12"></line>
                  </svg>
                </button>
              </div>

              <div class="composer-actions-right">
                <button type="button" class="icon-btn-2" id="attach" title="Attach files" aria-label="Attach files">
                  <svg style="width:30px;height:30px" viewBox="0 0 24 24" aria-hidden="true"><use href="#i-paperclip"/></svg>
                </button>
                <button type="button" class="icon-btn-2" id="mic" title="Speak instead of typing" aria-label="Speak">
                  <svg style="width:40px;height:40px" viewBox="0 0 48 48" aria-hidden="true"><use href="#i-mic"/></svg>
                </button>
                <button type="button" class="ai-sparkle" id="sparkleBtn" title="AI generation tools" aria-label="AI tools">
                  <svg viewBox="0 0 64 64" width="40" height="40" aria-hidden="true"><use href="#i-sparkle"/></svg>
                </button>
                <button type="submit" class="send-btn" id="send" title="Send" disabled aria-label="Send">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
                    <line x1="12" y1="19" x2="12" y2="5"></line>
                    <polyline points="5 12 12 5 19 12"></polyline>
                  </svg>
                </button>
              </div>
            </div>

            <div class="action-menu" id="plusMenu" role="menu">
              <button type="button" data-action="attach">
                <span class="menu-ico"><svg viewBox="0 0 24 24"><use href="#i-paperclip"/></svg></span>
                <span class="menu-txt"><span>Attach Files</span><small>Upload images, PDFs or text</small></span>
              </button>
              <button type="button" data-action="speak">
                <span class="menu-ico"><svg viewBox="0 0 48 48"><use href="#i-mic"/></svg></span>
                <span class="menu-txt"><span>Speak</span><small>Use your microphone</small></span>
              </button>
              <button type="button" data-action="readaloud">
                <span class="menu-ico"><svg viewBox="0 0 64 64"><use href="#i-readaloud"/></svg></span>
                <span class="menu-txt"><span>Read Aloud</span><small>Open the TTS reader</small></span>
              </button>
              <button type="button" data-action="image">
                <span class="menu-ico"><svg viewBox="0 0 64 64"><use href="#i-sparkle"/></svg></span>
                <span class="menu-txt"><span>Generate Image</span><small>Draw a picture from a prompt</small></span>
              </button>
              <button type="button" data-action="presentation">
                <span class="menu-ico"><svg viewBox="0 0 64 64"><use href="#i-sparkle"/></svg></span>
                <span class="menu-txt"><span>Generate Presentation</span><small>Build a PowerPoint deck</small></span>
              </button>
            </div>

            <div class="action-menu" id="sparkleMenu" role="menu">
              <button type="button" data-action="image">
                <span class="menu-ico"><svg viewBox="0 0 64 64"><use href="#i-sparkle"/></svg></span>
                <span class="menu-txt"><span>Generate Image</span><small>Draw a picture from a prompt</small></span>
              </button>
              <button type="button" data-action="presentation">
                <span class="menu-ico"><svg viewBox="0 0 64 64"><use href="#i-sparkle"/></svg></span>
                <span class="menu-txt"><span>Generate Presentation</span><small>Build a PowerPoint deck</small></span>
              </button>
            </div>

            <input type="file" id="file" multiple hidden
                   accept=".png,.jpg,.jpeg,.webp,.gif,.pdf,.txt,.md,.csv,.tsv,.json,.py,.js,.html,.css,.xml,.yaml,.yml,.log,.sql">
          </form>
        </div>
      </div>
    </div>

    <div class="composer-wrap" id="composerSlot" style="display:none"></div>

    <div class="foot">Enter to send · Shift + Enter for a new line · 📎 or drag and drop files · 🎤 speak your question</div>
  </div>
</div>

<script>
/* ============================================================
   NeuraxisAI — front-end controller
   - Each browser gets its own persistent user ID (localStorage)
   - The server keeps chats + memory per user ID
   - Fresh chat on every page load
   ============================================================ */

const TYPEWRITER = true;
const TYPEWRITER_MAX_WORDS = 120;
const TYPEWRITER_STEP_MS   = 14;

const chat             = document.getElementById('chat');
const input            = document.getElementById('input');
const sendBtn          = document.getElementById('send');
const welcome          = document.getElementById('welcome');
const welcomeComposerSlot = document.getElementById('welcomeComposerSlot');
const composerSlot     = document.getElementById('composerSlot');
const previews         = document.getElementById('previews');
const fileInput        = document.getElementById('file');
const toastEl          = document.getElementById('toast');
const histList         = document.getElementById('histList');
const histCount        = document.getElementById('histCount');
const shell            = document.getElementById('shell');
const composer         = document.getElementById('composer');
const collapseBtn      = document.getElementById('collapseBtn');
const plusBtn          = document.getElementById('plusBtn');
const plusMenu         = document.getElementById('plusMenu');
const sparkleBtn       = document.getElementById('sparkleBtn');
const sparkleMenu      = document.getElementById('sparkleMenu');

/* ---------- User ID (per browser) ---------- */
const USER_KEY = 'nx_user_id';
let userId = localStorage.getItem(USER_KEY);
if (!userId) {
  userId = 'u-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
  localStorage.setItem(USER_KEY, userId);
}

/* ---------- Chat storage (local UI mirror) ---------- */
const STORAGE_KEY = 'nx_history_v2';
function newSid() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 8); }
let sessionId = newSid();

let history = loadHistory();
function loadHistory() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr : [];
  } catch (e) { return []; }
}
function saveHistory() { try { localStorage.setItem(STORAGE_KEY, JSON.stringify(history)); } catch (e) {} }

function findCurrentChat() { return history.find(function (x) { return x.id === sessionId; }); }
function ensureCurrentChat(title) {
  let c = findCurrentChat();
  if (!c) {
    c = { id: sessionId, title: title || 'New Chat', created: Date.now(), updated: Date.now(), messages: [] };
    history.unshift(c);
  }
  c.updated = Date.now();
  if (title && (c.title === 'New Chat' || !c.title)) c.title = title;
  history.sort(function (a, b) { return b.updated - a.updated; });
  saveHistory();
  return c;
}

const SIDEBAR_KEY = 'nx_sidebar';
function applySidebarState(state) {
  if (state === 'collapsed') {
    shell.classList.add('collapsed');
    collapseBtn.textContent = '›';
    collapseBtn.title = 'Show sidebar';
  } else {
    shell.classList.remove('collapsed');
    collapseBtn.textContent = '‹';
    collapseBtn.title = 'Hide sidebar';
  }
}
function isMobile() { return window.matchMedia('(max-width: 860px)').matches; }
function toggleSidebar() {
  if (isMobile()) { shell.classList.toggle('sidebar-open'); return; }
  const next = shell.classList.contains('collapsed') ? 'open' : 'collapsed';
  applySidebarState(next);
  try { localStorage.setItem(SIDEBAR_KEY, next); } catch (e) {}
}
(function restoreSidebar() {
  const saved = localStorage.getItem(SIDEBAR_KEY) || 'open';
  applySidebarState(saved);
})();
collapseBtn.addEventListener('click', toggleSidebar);

let wasMobile = isMobile();
window.addEventListener('resize', function () {
  const nowMobile = isMobile();
  if (nowMobile !== wasMobile) {
    shell.classList.remove('sidebar-open');
    wasMobile = nowMobile;
  }
});

function pinComposerToBottom() {
  if (composer.parentElement !== composerSlot) composerSlot.appendChild(composer);
  composerSlot.style.display = '';
}
function putComposerInWelcome() {
  if (composer.parentElement !== welcomeComposerSlot) welcomeComposerSlot.appendChild(composer);
  welcomeComposerSlot.style.display = '';
}

function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

function md(text) {
  let t = esc(text);
  const blocks = [];
  t = t.replace(/```[a-zA-Z0-9_-]*\n?([\s\S]*?)```/g, function (m, code) {
    blocks.push(code.replace(/\s+$/, ''));
    return '@@CODE' + (blocks.length - 1) + '@@';
  });
  t = t.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  t = t.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  t = t.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  let out = '', inList = false;
  t.split('\n').forEach(function (line) {
    const li = line.match(/^\s*[-*]\s+(.*)/) || line.match(/^\s*\d+\.\s+(.*)/);
    if (li) { if (!inList) { out += '<ul>'; inList = true; } out += '<li>' + li[1] + '</li>'; return; }
    if (inList) { out += '</ul>'; inList = false; }
    const h = line.match(/^#{1,6}\s+(.*)/);
    if (h) out += '<h4>' + h[1] + '</h4>';
    else if (line.trim() !== '') out += '<p>' + line + '</p>';
  });
  if (inList) out += '</ul>';
  out = out.replace(/<p>@@CODE(\d+)@@<\/p>/g, '@@CODE$1@@');
  out = out.replace(/@@CODE(\d+)@@/g, function (m, i) {
    const code = blocks[Number(i)];
    return '<div class="code-wrap">' +
             '<button type="button" class="code-copy">' +
               '<svg viewBox="0 0 24 24"><use href="#i-copy"/></svg>' +
               '<span>Copy</span>' +
             '</button>' +
             '<pre><code>' + code + '</code></pre>' +
           '</div>';
  });
  return out;
}

function attachCopyButtons(container) {
  container.querySelectorAll('.code-copy').forEach(function (btn) {
    if (btn.dataset.bound === '1') return;
    btn.dataset.bound = '1';
    btn.addEventListener('click', function () {
      const wrap = btn.closest('.code-wrap');
      if (!wrap) return;
      const codeEl = wrap.querySelector('pre code');
      if (!codeEl) return;
      const text = codeEl.innerText;
      const done = function () {
        btn.classList.add('copied');
        btn.querySelector('span').textContent = 'Copied!';
        setTimeout(function () {
          btn.classList.remove('copied');
          btn.querySelector('span').textContent = 'Copy';
        }, 1600);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(function () { fallbackCopy(text, done); });
      } else {
        fallbackCopy(text, done);
      }
    });
  });
}
function fallbackCopy(text, done) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); done(); } catch (e) { toast('Could not copy.'); }
  document.body.removeChild(ta);
}

function scrollDown() {
  requestAnimationFrame(function () { chat.scrollTop = chat.scrollHeight; });
}
let toastTimer = null;
function toast(msg) {
  toastEl.textContent = msg;
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { toastEl.classList.remove('show'); }, 3500);
}

const MAX_FILES = 4, MAX_FILE_MB = 10, MAX_TOTAL_MB = 15;
const OK_EXT = ['png','jpg','jpeg','webp','gif','pdf','txt','md','csv','tsv','json','py','js','html','css','xml','yaml','yml','log','sql'];
let attachments = [];
let busy = false;

function extOf(name) { const i = name.lastIndexOf('.'); return i < 0 ? '' : name.slice(i + 1).toLowerCase(); }
function isImage(f) { return ['png','jpg','jpeg','webp','gif'].indexOf(extOf(f.name)) !== -1; }
function iconFor(f) {
  const e = extOf(f.name);
  if (e === 'pdf') return '📕';
  if (e === 'csv' || e === 'tsv') return '📊';
  if (['py','js','html','css','json','xml','yaml','yml','sql'].indexOf(e) !== -1) return '🧩';
  return '📄';
}
function addFiles(list) {
  Array.from(list).forEach(function (file) {
    if (attachments.length >= MAX_FILES) { toast('You can attach up to ' + MAX_FILES + ' files.'); return; }
    if (OK_EXT.indexOf(extOf(file.name)) === -1) { toast('"' + file.name + '" is not supported.'); return; }
    if (file.size > MAX_FILE_MB * 1024 * 1024) { toast('"' + file.name + '" is bigger than ' + MAX_FILE_MB + ' MB.'); return; }
    const total = attachments.reduce(function (s, f) { return s + f.size; }, 0) + file.size;
    if (total > MAX_TOTAL_MB * 1024 * 1024) { toast('Total size is limited to ' + MAX_TOTAL_MB + ' MB.'); return; }
    attachments.push(file);
  });
  renderPreviews();
  updateSendState();
}
function renderPreviews() {
  previews.innerHTML = '';
  attachments.forEach(function (file, index) {
    const box = document.createElement('div');
    box.className = 'preview';
    if (isImage(file)) {
      const img = document.createElement('img');
      img.src = URL.createObjectURL(file);
      box.appendChild(img);
    } else {
      const ico = document.createElement('div');
      ico.className = 'ico'; ico.textContent = iconFor(file);
      box.appendChild(ico);
    }
    const name = document.createElement('span');
    name.className = 'nm'; name.textContent = file.name; name.title = file.name;
    box.appendChild(name);
    if (TTS && !isImage(file)) {
      const speak = document.createElement('button');
      speak.className = 'x'; speak.type = 'button'; speak.title = 'Read this file aloud'; speak.textContent = '🔊';
      speak.addEventListener('click', function () { loadFileIntoReader(file); });
      box.appendChild(speak);
    }
    const remove = document.createElement('button');
    remove.className = 'x'; remove.type = 'button'; remove.title = 'Remove'; remove.textContent = '×';
    remove.addEventListener('click', function () { attachments.splice(index, 1); renderPreviews(); updateSendState(); });
    box.appendChild(remove);
    previews.appendChild(box);
  });
}

function updateSendState() {
  const hasText = input.value.trim().length > 0;
  const hasFiles = attachments.length > 0;
  const ready = (hasText || hasFiles) && !busy;
  sendBtn.disabled = !ready;
  sendBtn.classList.toggle('ready', ready);
}

function addMessage(role, extraClass) {
  const row = document.createElement('div');
  row.className = 'msg ' + role + (extraClass ? ' ' + extraClass : '');
  row.innerHTML = '<div class="avatar">' + (role === 'user' ? '🧑‍💻' : '<svg viewBox="0 0 200 200" width="26" height="26"><use href="#nx-mark"/></svg>') + '</div><div class="bubble"></div>';
  chat.appendChild(row);
  scrollDown();
  return row;
}
function addUser(text, files, store) {
  const row = addMessage('user');
  const bubble = row.querySelector('.bubble');
  if (files && files.length) {
    const wrap = document.createElement('div');
    wrap.className = 'att-wrap';
    files.forEach(function (file) {
      if (isImage(file)) {
        const img = document.createElement('img');
        img.className = 'att-img'; img.src = URL.createObjectURL(file);
        img.addEventListener('load', scrollDown);
        wrap.appendChild(img);
      } else {
        const chip = document.createElement('span');
        chip.className = 'att-file';
        chip.textContent = iconFor(file) + ' ' + file.name;
        if (TTS) {
          chip.style.cursor = 'pointer';
          chip.title = 'Click to read this file aloud';
          chip.addEventListener('click', function () { loadFileIntoReader(file); });
        }
        wrap.appendChild(chip);
      }
    });
    bubble.appendChild(wrap);
  }
  if (text) {
    const t = document.createElement('div');
    t.className = 'utext'; t.textContent = text;
    bubble.appendChild(t);
  }
  if (store !== false) {
    const c = ensureCurrentChat(text ? text : (files && files.length ? '📎 ' + files[0].name : ''));
    c.messages.push({ role: 'user', text: text || '', fileNames: (files || []).map(function (f) { return f.name; }) });
    saveHistory();
    renderHistory();
  }
}
function addTyping() {
  const row = addMessage('bot');
  row.querySelector('.bubble').innerHTML = '<div class="typing"><i></i><i></i><i></i></div>';
  return row;
}
function addError(text) {
  const row = addMessage('bot', 'error');
  row.querySelector('.bubble').textContent = text;
}
function downloadPng(file) {
  const img = new Image();
  img.onload = function () {
    const w = img.naturalWidth || 800, h = img.naturalHeight || 600;
    const scale = Math.max(1, 1600 / w);
    const canvas = document.createElement('canvas');
    canvas.width = Math.round(w * scale); canvas.height = Math.round(h * scale);
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    canvas.toBlob(function (blob) {
      if (!blob) { toast('Could not make a PNG.'); return; }
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = file.name.replace(/\.svg$/i, '') + '.png';
      document.body.appendChild(a); a.click(); a.remove();
    }, 'image/png');
  };
  img.onerror = function () { toast('Could not make a PNG.'); };
  img.src = file.url;
}
function fileCard(file) {
  const card = document.createElement('div');
  if (file.kind === 'drawing') {
    card.className = 'imgcard drawing';
    const pic = document.createElement('img');
    pic.src = file.url; pic.alt = file.title || 'Drawing';
    pic.addEventListener('load', scrollDown);
    card.appendChild(pic);
    const bar = document.createElement('div');
    bar.className = 'imgbar';
    const label = document.createElement('span'); label.textContent = '🎨 Vector drawing';
    const buttons = document.createElement('div'); buttons.className = 'imgbtns';
    const png = document.createElement('a'); png.className = 'dl'; png.href = '#'; png.textContent = '⬇ PNG';
    png.addEventListener('click', function (e) { e.preventDefault(); downloadPng(file); });
    const svg = document.createElement('a'); svg.className = 'dl';
    svg.href = file.url + '?download=1'; svg.download = file.name; svg.textContent = '⬇ SVG';
    buttons.appendChild(png); buttons.appendChild(svg);
    bar.appendChild(label); bar.appendChild(buttons);
    card.appendChild(bar);
    return card;
  }
  if (file.kind === 'image') {
    card.className = 'imgcard';
    const img = document.createElement('img');
    img.src = file.url; img.alt = file.title || 'Generated image';
    img.title = 'Click to open full size';
    img.addEventListener('load', scrollDown);
    img.addEventListener('click', function () { window.open(file.url, '_blank'); });
    card.appendChild(img);
    const bar = document.createElement('div');
    bar.className = 'imgbar';
    const label = document.createElement('span'); label.textContent = '🎨 Generated image';
    const link = document.createElement('a'); link.className = 'dl';
    link.href = file.url + '?download=1'; link.download = file.name; link.textContent = '⬇ Download';
    bar.appendChild(label); bar.appendChild(link);
    card.appendChild(bar);
    return card;
  }
  card.className = 'filecard';
  const ico = document.createElement('div');
  ico.className = 'fileico';
  ico.textContent = file.kind === 'slides' ? '📊' : '📄';
  const info = document.createElement('div'); info.className = 'fileinfo';
  const name = document.createElement('div'); name.className = 'filename';
  name.textContent = file.title || file.name; name.title = file.title || file.name;
  const meta = document.createElement('div'); meta.className = 'filemeta';
  meta.textContent = file.kind === 'slides'
    ? 'PowerPoint · ' + (file.count || '') + ' slides (.pptx)'
    : 'Word document (.docx)';
  info.appendChild(name); info.appendChild(meta);
  const link = document.createElement('a'); link.className = 'dl';
  link.href = file.url; link.download = file.name; link.textContent = '⬇ Download';
  card.appendChild(ico); card.appendChild(info); card.appendChild(link);
  return card;
}

async function addBot(answer, tools, store) {
  const row = addMessage('bot');
  const bubble = row.querySelector('.bubble');

  const words = answer.split(/\s+/).filter(Boolean);
  const useTypewriter = TYPEWRITER && words.length > 0 && words.length <= TYPEWRITER_MAX_WORDS;

  if (!useTypewriter) {
    bubble.innerHTML = md(answer);
    attachCopyButtons(bubble);
    (tools || []).forEach(function (t) { if (t.file) bubble.appendChild(fileCard(t.file)); });
    scrollDown();
  } else {
    bubble.innerHTML = '';
    const textEl = document.createElement('div');
    textEl.style.whiteSpace = 'pre-wrap';
    textEl.style.wordBreak = 'break-word';
    bubble.appendChild(textEl);
    const CHUNK = 3;
    for (let i = 0; i < words.length; i += CHUNK) {
      textEl.append(words.slice(i, i + CHUNK).join(' ') + (i + CHUNK < words.length ? ' ' : ''));
      if ((i / CHUNK) % 4 === 0) scrollDown();
      await new Promise(function (r) { setTimeout(r, TYPEWRITER_STEP_MS); });
    }
    bubble.innerHTML = md(answer);
    attachCopyButtons(bubble);
    (tools || []).forEach(function (t) { if (t.file) bubble.appendChild(fileCard(t.file)); });
    scrollDown();
  }

  if (store !== false) {
    const c = findCurrentChat();
    if (c) {
      c.messages.push({ role: 'bot', text: answer, tools: tools || [] });
      c.updated = Date.now();
      saveHistory();
      renderHistory();
    }
  }
}

async function send(text) {
  text = (text || '').trim();
  if ((!text && attachments.length === 0) || busy) return;
  if (typeof listening !== 'undefined' && listening) stopListening();
  busy = true;
  updateSendState();

  pinComposerToBottom();
  welcome.style.display = 'none';

  input.value = '';
  input.style.height = 'auto';

  const files = attachments.slice();
  attachments = [];
  renderPreviews();
  addUser(text, files);
  const typing = addTyping();

  const form = new FormData();
  form.append('message', text);
  form.append('session', sessionId);
  form.append('user_id', userId);
  files.forEach(function (f) { form.append('files', f); });

  try {
    const res = await fetch('/api/chat', { method: 'POST', body: form });
    const data = await res.json();
    typing.remove();
    if (!res.ok) addError(data.error || 'Something went wrong.');
    else await addBot(data.answer, data.tools);
  } catch (e) {
    typing.remove();
    addError('Cannot reach the server. Is web.py still running in the terminal?');
  }
  busy = false;
  updateSendState();
  input.focus();
  scrollDown();
}

function relTime(ts) {
  const d = Math.floor((Date.now() - ts) / 1000);
  if (d < 60) return 'Just Now';
  if (d < 3600) return Math.floor(d / 60) + 'm Ago';
  if (d < 86400) return Math.floor(d / 3600) + 'h Ago';
  return Math.floor(d / 86400) + 'd Ago';
}
function renderHistory() {
  histList.innerHTML = '';
  const shown = history.filter(function (c) { return c.messages && c.messages.length > 0; });
  histCount.textContent = shown.length;
  if (!shown.length) {
    const empty = document.createElement('div');
    empty.className = 'hist-empty';
    empty.innerHTML = 'No conversations yet.<br>Start chatting and your history will appear here.';
    histList.appendChild(empty);
    return;
  }
  shown.forEach(function (c) {
    const item = document.createElement('div');
    item.className = 'hist-item' + (c.id === sessionId ? ' active' : '');
    item.innerHTML =
      '<span class="dot2"></span>' +
      '<div class="txt"><div class="ttl"></div><div class="meta"></div></div>' +
      '<button class="del" title="Delete chat">' +
        '<svg viewBox="0 0 64 64"><use href="#i-trash"/></svg>' +
      '</button>';
    item.querySelector('.ttl').textContent = c.title || 'New Chat';
    item.querySelector('.meta').textContent = relTime(c.updated) + ' · ' + c.messages.length + ' msg';
    item.addEventListener('click', function (e) {
      if (e.target.closest('.del')) return;
      openChat(c.id);
      if (isMobile()) shell.classList.remove('sidebar-open');
    });
    item.querySelector('.del').addEventListener('click', function (e) {
      e.stopPropagation();
      askDelete(c.id, c.title);
    });
    histList.appendChild(item);
  });
}
function openChat(id) {
  if (busy) return;
  sessionId = id;
  chat.querySelectorAll('.msg').forEach(function (m) { m.remove(); });
  const c = history.find(function (x) { return x.id === id; });
  if (!c || !c.messages.length) {
    welcome.style.display = '';
    putComposerInWelcome();
    composerSlot.style.display = 'none';
    renderHistory();
    return;
  }
  welcome.style.display = 'none';
  pinComposerToBottom();
  c.messages.forEach(function (m) {
    if (m.role === 'user') {
      const row = addMessage('user');
      const bubble = row.querySelector('.bubble');
      const t = document.createElement('div');
      t.className = 'utext'; t.textContent = m.text || '';
      bubble.appendChild(t);
    } else {
      addBotFast(m.text || '', m.tools || []);
    }
  });
  renderHistory();
}
function addBotFast(answer, tools) {
  const row = addMessage('bot');
  const bubble = row.querySelector('.bubble');
  bubble.innerHTML = md(answer);
  attachCopyButtons(bubble);
  (tools || []).forEach(function (t) { if (t.file) bubble.appendChild(fileCard(t.file)); });
  scrollDown();
}

let pendingDelete = null;
const confirmBox = document.getElementById('confirmBox');
const confirmTitle = document.getElementById('confirmTitle');
const confirmText = document.getElementById('confirmText');
function askDelete(id, title) {
  pendingDelete = id;
  confirmTitle.textContent = 'Delete "' + (title || 'this chat') + '"?';
  confirmText.textContent = 'This removes the conversation from your history. You cannot undo this.';
  confirmBox.classList.add('open');
}
document.getElementById('confirmCancel').addEventListener('click', function () {
  pendingDelete = null; confirmBox.classList.remove('open');
});
document.getElementById('confirmOk').addEventListener('click', function () {
  if (pendingDelete === '__ALL__') {
    history = [];
    saveHistory();
    fetch('/api/reset', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session: '*', user_id: userId })
    }).catch(function () {});
    sessionId = newSid();
    chat.querySelectorAll('.msg').forEach(function (m) { m.remove(); });
    welcome.style.display = '';
    putComposerInWelcome();
    composerSlot.style.display = 'none';
    renderHistory();
    pendingDelete = null;
    confirmBox.classList.remove('open');
    toast('History cleared.');
    return;
  }
  if (!pendingDelete) return;
  history = history.filter(function (c) { return c.id !== pendingDelete; });
  saveHistory();
  fetch('/api/reset', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session: pendingDelete, user_id: userId })
  }).catch(function () {});
  if (pendingDelete === sessionId) {
    sessionId = newSid();
    chat.querySelectorAll('.msg').forEach(function (m) { m.remove(); });
    welcome.style.display = '';
    putComposerInWelcome();
    composerSlot.style.display = 'none';
  }
  pendingDelete = null;
  confirmBox.classList.remove('open');
  renderHistory();
  toast('Chat deleted.');
});
confirmBox.addEventListener('click', function (e) {
  if (e.target === confirmBox) { pendingDelete = null; confirmBox.classList.remove('open'); }
});

document.getElementById('newChatBtn').addEventListener('click', function () {
  if (busy) return;
  sessionId = newSid();
  chat.querySelectorAll('.msg').forEach(function (m) { m.remove(); });
  welcome.style.display = '';
  putComposerInWelcome();
  composerSlot.style.display = 'none';
  attachments = [];
  renderPreviews();
  updateSendState();
  renderHistory();
  if (isMobile()) shell.classList.remove('sidebar-open');
  input.focus();
});

document.getElementById('clearAllBtn').addEventListener('click', function () {
  if (busy) return;
  const shown = history.filter(function (c) { return c.messages && c.messages.length > 0; });
  if (!shown.length) { toast('History is already empty.'); return; }
  confirmTitle.textContent = 'Clear all history?';
  confirmText.textContent = 'This will delete every conversation from your history. You cannot undo this.';
  pendingDelete = '__ALL__';
  confirmBox.classList.add('open');
});

document.getElementById('saveHistoryBtn').addEventListener('click', function () {
  const shown = history.filter(function (c) { return c.messages && c.messages.length > 0; });
  if (!shown.length) { toast('No conversations to save.'); return; }
  const current = findCurrentChat();
  const target = (current && current.messages && current.messages.length) ? current : shown[0];

  let out = '';
  out += 'NeuraxisAI — Chat History\n';
  out += 'Title: ' + (target.title || 'New Chat') + '\n';
  out += 'Saved: ' + new Date().toISOString().replace('T', ' ').slice(0, 19) + '\n';
  out += '============================================\n\n';

  target.messages.forEach(function (m) {
    if (m.role === 'user') out += 'You:\n' + (m.text || '(attachment)') + '\n\n';
    else out += 'NeuraxisAI:\n' + (m.text || '') + '\n\n';
  });

  const blob = new Blob([out], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'neuraxis-chat-' + new Date().toISOString().slice(0, 10) + '.txt';
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
  toast('Chat history saved.');
});

function closeAllMenus() { plusMenu.classList.remove('open'); sparkleMenu.classList.remove('open'); }
plusBtn.addEventListener('click', function (e) {
  e.stopPropagation();
  const isOpen = plusMenu.classList.contains('open');
  closeAllMenus();
  if (!isOpen) plusMenu.classList.add('open');
});
sparkleBtn.addEventListener('click', function (e) {
  e.stopPropagation();
  const isOpen = sparkleMenu.classList.contains('open');
  closeAllMenus();
  if (!isOpen) sparkleMenu.classList.add('open');
});
document.addEventListener('click', function (e) {
  if (!plusMenu.contains(e.target) && !sparkleMenu.contains(e.target) &&
      e.target !== plusBtn && e.target !== sparkleBtn &&
      !plusBtn.contains(e.target) && !sparkleBtn.contains(e.target)) {
    closeAllMenus();
  }
});
document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeAllMenus(); });

function handleMenuAction(action) {
  closeAllMenus();
  if (action === 'attach') fileInput.click();
  else if (action === 'speak') { if (typeof listening !== 'undefined' && listening) stopListening(); else startListening(); }
  else if (action === 'readaloud') document.getElementById('readerOpen').click();
  else if (action === 'image') {
    input.value = 'Draw me an image of ';
    input.dispatchEvent(new Event('input'));
    input.focus();
    try { input.setSelectionRange(input.value.length, input.value.length); } catch (e) {}
  } else if (action === 'presentation') {
    input.value = 'Create a presentation about ';
    input.dispatchEvent(new Event('input'));
    input.focus();
    try { input.setSelectionRange(input.value.length, input.value.length); } catch (e) {}
  }
}
plusMenu.querySelectorAll('button').forEach(function (btn) {
  btn.addEventListener('click', function () { handleMenuAction(btn.dataset.action); });
});
sparkleMenu.querySelectorAll('button').forEach(function (btn) {
  btn.addEventListener('click', function () { handleMenuAction(btn.dataset.action); });
});

composer.addEventListener('submit', function (e) { e.preventDefault(); send(input.value); });
input.addEventListener('keydown', function (e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(input.value); }
});
input.addEventListener('input', function () {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 180) + 'px';
  updateSendState();
});
document.getElementById('attach').addEventListener('click', function () { fileInput.click(); });
fileInput.addEventListener('change', function () { addFiles(fileInput.files); fileInput.value = ''; });
input.addEventListener('paste', function (e) {
  const files = e.clipboardData && e.clipboardData.files;
  if (files && files.length) { e.preventDefault(); addFiles(files); }
});

let dragDepth = 0;
function hasFiles(e) { return e.dataTransfer && Array.from(e.dataTransfer.types || []).indexOf('Files') !== -1; }
window.addEventListener('dragenter', function (e) { if (!hasFiles(e)) return; e.preventDefault(); dragDepth++; document.body.classList.add('dragging'); });
window.addEventListener('dragover', function (e) { if (hasFiles(e)) e.preventDefault(); });
window.addEventListener('dragleave', function (e) { if (!hasFiles(e)) return; dragDepth = Math.max(0, dragDepth - 1); if (dragDepth === 0) document.body.classList.remove('dragging'); });
window.addEventListener('drop', function (e) {
  if (!hasFiles(e)) return;
  e.preventDefault(); dragDepth = 0; document.body.classList.remove('dragging');
  addFiles(e.dataTransfer.files);
});

const AUTO_SEND = false;
const micBtn = document.getElementById('mic');
const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
const normalPlaceholder = input.placeholder;
const savedVoiceLang = localStorage.getItem('lang') || 'en-US';
let recog = null, listening = false;

function setListening(on) {
  listening = on;
  micBtn.classList.toggle('recording', on);
  micBtn.title = on ? 'Stop listening' : 'Speak instead of typing';
  input.placeholder = on ? 'Listening... speak now' : normalPlaceholder;
  const svg = micBtn.querySelector('svg');
  if (!svg) return;
  if (on) {
    svg.setAttribute('viewBox', '0 0 48 48');
    svg.innerHTML = '<circle cx="24" cy="24" r="24" fill="#ff3b30"/><rect x="16" y="16" width="16" height="16" rx="3" fill="#fff"/>';
  } else {
    svg.setAttribute('viewBox', '0 0 48 48');
    svg.innerHTML = '<use href="#i-mic"/>';
  }
}
function startListening() {
  if (busy || listening) return;
  if (!SpeechRec) { toast('Voice input is not supported in this browser.'); return; }
  recog = new SpeechRec();
  recog.lang = savedVoiceLang;
  recog.interimResults = true;
  recog.continuous = true;
  const base = input.value ? input.value.trimEnd() + ' ' : '';
  let finalText = '';
  recog.onstart = function () { setListening(true); };
  recog.onresult = function (event) {
    let interim = '';
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const r = event.results[i];
      if (r.isFinal) finalText += r[0].transcript;
      else interim += r[0].transcript;
    }
    input.value = base + finalText + interim;
    input.dispatchEvent(new Event('input'));
  };
  recog.onerror = function (event) {
    const messages = {
      'not-allowed': 'Microphone is blocked. Click the lock icon next to the address bar and allow it.',
      'service-not-allowed': 'Microphone is blocked. Click the lock icon next to the address bar and allow it.',
      'no-speech': "I didn't hear anything. Try again.",
      'audio-capture': 'No microphone found.',
      'network': 'Voice recognition needs an internet connection.',
      'language-not-supported': 'This language is not supported by your browser.'
    };
    if (event.error !== 'aborted') toast(messages[event.error] || ('Voice error: ' + event.error));
  };
  recog.onend = function () {
    setListening(false);
    input.focus();
    if (AUTO_SEND && input.value.trim() && !busy) send(input.value);
  };
  try { recog.start(); } catch (e) { setListening(false); }
}
function stopListening() { if (recog) recog.stop(); }
if (!SpeechRec) {
  micBtn.style.display = 'none';
} else {
  micBtn.addEventListener('click', function () { if (listening) stopListening(); else startListening(); });
}

const TTS = 'speechSynthesis' in window;
const reader = document.getElementById('reader');
const rTitle = document.getElementById('readerTitleText');
const rInfo = document.getElementById('readerInfo');
const rLines = document.getElementById('readerLines');
const rPasteBox = document.getElementById('readerPasteBox');
const rPasteText = document.getElementById('readerPasteText');
const rFileInput = document.getElementById('readerFileInput');
const rPlay = document.getElementById('rPlay');
const rPos = document.getElementById('rPos');
const rRate = document.getElementById('rRate');
const rRateVal = document.getElementById('rRateVal');
const rVoice = document.getElementById('rVoice');
const R = { lines: [], i: 0, playing: false, token: 0 };
const warned = {};
let voices = [];

function splitLines(text) {
  const out = [];
  function addLong(piece) {
    while (piece.length > 300) {
      let cut = piece.lastIndexOf(' ', 300);
      if (cut < 100) cut = 300;
      out.push(piece.slice(0, cut).trim());
      piece = piece.slice(cut).trim();
    }
    if (piece) out.push(piece);
  }
  text.replace(/\r/g, '').split('\n').forEach(function (raw) {
    const line = raw.trim();
    if (!line) return;
    if (line.length <= 240) { out.push(line); return; }
    line.split(/(?<=[.!?।؟])\s+/).forEach(function (s) { s = s.trim(); if (s) addLong(s); });
  });
  return out;
}
function guessLang(text) {
  if (/[\u0980-\u09FF]/.test(text)) return 'bn';
  if (/[\u0900-\u097F]/.test(text)) return 'hi';
  if (/[\u0600-\u06FF]/.test(text)) return 'ar';
  return 'en';
}
function loadVoices() {
  voices = window.speechSynthesis.getVoices();
  const saved = localStorage.getItem('voiceName') || '';
  rVoice.innerHTML = '';
  const auto = document.createElement('option');
  auto.value = ''; auto.textContent = '🌐 Auto (detect language)';
  rVoice.appendChild(auto);
  voices.slice().sort(function (a, b) { return a.lang.localeCompare(b.lang) || a.name.localeCompare(b.name); }).forEach(function (v) {
    const o = document.createElement('option');
    o.value = v.name; o.textContent = v.name + ' (' + v.lang + ')';
    rVoice.appendChild(o);
  });
  if (Array.from(rVoice.options).some(function (o) { return o.value === saved; })) rVoice.value = saved;
}
function pickVoice(prefix) {
  if (rVoice.value) return voices.find(function (v) { return v.name === rVoice.value; }) || null;
  const matches = voices.filter(function (v) { return v.lang.toLowerCase().replace('_', '-').indexOf(prefix) === 0; });
  if (!matches.length) return null;
  return matches.find(function (v) { return /natural|neural|online/i.test(v.name); })
      || matches.find(function (v) { return v.default; })
      || matches[0];
}
function updateButtons() { rPlay.textContent = R.playing ? '⏸' : '▶'; }
function highlight() {
  const nodes = rLines.children;
  for (let k = 0; k < nodes.length; k++) nodes[k].className = 'line' + (k === R.i ? ' active' : (k < R.i ? ' done' : ''));
  const cur = nodes[R.i];
  if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: 'center', behavior: 'smooth' });
  rPos.textContent = R.lines.length ? 'Line ' + Math.min(R.i + 1, R.lines.length) + ' / ' + R.lines.length : 'No text loaded';
}
function haltSpeech() { R.playing = false; R.token++; window.speechSynthesis.cancel(); updateButtons(); }
function finishReader() { haltSpeech(); R.i = 0; highlight(); rInfo.textContent = 'Finished 🎉'; }
function speakCurrent() {
  window.speechSynthesis.cancel();
  if (R.i >= R.lines.length) { finishReader(); return; }
  const my = ++R.token;
  highlight();
  setTimeout(function () {
    if (my !== R.token || !R.playing) return;
    const line = R.lines[R.i];
    const lang = guessLang(line);
    const voice = pickVoice(lang);
    const u = new SpeechSynthesisUtterance(line);
    if (voice) { u.voice = voice; u.lang = voice.lang; }
    else {
      u.lang = lang;
      if (lang !== 'en' && !rVoice.value && !warned[lang]) {
        warned[lang] = true;
        toast('No ' + lang.toUpperCase() + ' voice found on this device.');
      }
    }
    u.rate = parseFloat(rRate.value);
    u.onend = function () { if (my !== R.token || !R.playing) return; R.i++; speakCurrent(); };
    u.onerror = function (e) {
      if (my !== R.token) return;
      if (e.error === 'canceled' || e.error === 'interrupted') return;
      R.playing = false; updateButtons(); toast('Speech error: ' + e.error);
    };
    window.currentUtterance = u;
    window.speechSynthesis.speak(u);
  }, 60);
}
function playFrom(index) { R.i = index; R.playing = true; updateButtons(); rInfo.textContent = ''; speakCurrent(); }
function setLines(text, autoplay) {
  R.lines = splitLines(text);
  R.i = 0;
  rLines.innerHTML = '';
  rPasteBox.style.display = 'none';
  if (!R.lines.length) {
    const hint = document.createElement('div');
    hint.className = 'empty';
    hint.textContent = '📂 Open a PDF, Word or text file, or 📋 paste some text. Then press ▶.';
    rLines.appendChild(hint);
    highlight(); updateButtons();
    return;
  }
  R.lines.forEach(function (line, index) {
    const div = document.createElement('div');
    div.className = 'line'; div.textContent = line;
    div.title = 'Click to start reading from here';
    div.addEventListener('click', function () { playFrom(index); });
    rLines.appendChild(div);
  });
  rInfo.textContent = R.lines.length + ' lines';
  highlight(); updateButtons();
  if (autoplay) playFrom(0);
}
function openReader(text, title, autoplay) {
  if (!TTS) { toast('Your browser cannot read text aloud. Try Chrome, Edge or Safari.'); return; }
  haltSpeech();
  rTitle.textContent = title || 'Read aloud';
  reader.classList.add('open');
  setLines(text || '', autoplay);
}
function closeReader() { haltSpeech(); reader.classList.remove('open'); }
async function loadFileIntoReader(file) {
  openReader('', 'Read aloud — ' + file.name, false);
  rInfo.textContent = 'Reading ' + file.name + '...';
  const form = new FormData();
  form.append('file', file);
  try {
    const res = await fetch('/api/extract', { method: 'POST', body: form });
    const data = await res.json();
    if (!res.ok) { rInfo.textContent = ''; toast(data.error || 'Could not read that file.'); return; }
    setLines(data.text, false);
    if (data.truncated) rInfo.textContent += ' (very long file: only the first part is loaded)';
  } catch (e) {
    rInfo.textContent = '';
    toast('Cannot reach the server.');
  }
}
if (!TTS) {
  document.getElementById('readerOpen').style.display = 'none';
} else {
  loadVoices();
  window.speechSynthesis.onvoiceschanged = loadVoices;
  rRate.value = localStorage.getItem('rate') || '1';
  rRateVal.textContent = parseFloat(rRate.value).toFixed(1) + '×';

  document.getElementById('readerOpen').addEventListener('click', function () { openReader('', 'Read aloud', false); });
  document.getElementById('readerClose').addEventListener('click', closeReader);
  reader.addEventListener('click', function (e) { if (e.target === reader) closeReader(); });
  document.getElementById('readerFile').addEventListener('click', function () { rFileInput.click(); });
  rFileInput.addEventListener('change', function () { if (rFileInput.files.length) loadFileIntoReader(rFileInput.files[0]); rFileInput.value = ''; });
  document.getElementById('readerPaste').addEventListener('click', function () {
    rPasteBox.style.display = rPasteBox.style.display === 'none' ? 'flex' : 'none';
    rPasteText.focus();
  });
  document.getElementById('readerLoad').addEventListener('click', function () {
    haltSpeech();
    rTitle.textContent = 'Pasted text';
    setLines(rPasteText.value, false);
  });
  rPlay.addEventListener('click', function () {
    if (!R.lines.length) return;
    if (R.playing) haltSpeech(); else playFrom(R.i >= R.lines.length ? 0 : R.i);
  });
  document.getElementById('rNext').addEventListener('click', function () {
    if (!R.lines.length) return;
    R.i = Math.min(R.lines.length - 1, R.i + 1);
    if (R.playing) speakCurrent(); else highlight();
  });
  document.getElementById('rPrev').addEventListener('click', function () {
    if (!R.lines.length) return;
    R.i = Math.max(0, R.i - 1);
    if (R.playing) speakCurrent(); else highlight();
  });
  document.getElementById('rStop').addEventListener('click', function () { haltSpeech(); R.i = 0; highlight(); });
  rRate.addEventListener('input', function () {
    rRateVal.textContent = parseFloat(rRate.value).toFixed(1) + '×';
    localStorage.setItem('rate', rRate.value);
    if (R.playing) speakCurrent();
  });
  rVoice.addEventListener('change', function () {
    localStorage.setItem('voiceName', rVoice.value);
    if (R.playing) speakCurrent();
  });
  document.addEventListener('keydown', function (e) {
    if (!reader.classList.contains('open')) return;
    if (e.key === 'Escape') { closeReader(); return; }
    const tag = e.target && e.target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'BUTTON') return;
    if (e.key === ' ') { e.preventDefault(); rPlay.click(); }
    if (e.key === 'ArrowRight') document.getElementById('rNext').click();
    if (e.key === 'ArrowLeft') document.getElementById('rPrev').click();
  });
  window.addEventListener('beforeunload', function () { window.speechSynthesis.cancel(); });
}

(function boot() {
  welcome.style.display = '';
  putComposerInWelcome();
  composerSlot.style.display = 'none';
  renderHistory();
  updateSendState();
  input.focus();
})();
</script>
</body>
</html>
"""


def friendly_error(error):
    message = str(error)
    if error.code in (429, 503):
        return "⏳ The AI service is busy, or the free limit was reached. Please wait a minute and try again."
    if error.code == 404:
        return "⚠️ The model name is outdated. Change GEMINI_MODEL in your .env file to a current model from aistudio.google.com, then restart."
    if "API key" in message or error.code in (401, 403):
        return "🔑 There is a problem with the API key. Please check it in your .env file."
    if error.code == 400:
        return "⚠️ The AI could not process that request. If you attached a file, it may be unreadable or not supported. Try a different file."
    return f"⚠️ Something went wrong: {error}"


@app.errorhandler(413)
def too_large(_error):
    return jsonify(error="Your files are too large. Keep the total under 15 MB."), 413


@app.get("/")
def home():
    return Response(HTML.replace("__MODEL__", agent.MODEL), mimetype="text/html")


@app.post("/api/chat")
def chat():
    question = (request.form.get("message") or "").strip()
    session_id = request.form.get("session") or "default"
    user_id = (request.form.get("user_id") or "anonymous").strip() or "anonymous"
    files = [f for f in request.files.getlist("files") if f and f.filename]

    if not question and not files:
        return jsonify(error="Please type a question or attach a file."), 400

    try:
        contents = build_contents(question, files) if files else question
    except ValueError as error:
        return jsonify(error=str(error)), 400

    with lock:
        _cleanup_chats()

        user_chats = chats.setdefault(user_id, {})
        if session_id not in user_chats:
            user_chats[session_id] = {
                "chat": None,
                "history": [],
                "title": _make_title(question, files),
                "created": time.time(),
                "updated": time.time(),
            }
        info = user_chats[session_id]
        info["updated"] = time.time()
        if question and info.get("title") in ("New Chat", "📎 attachment", "", None):
            info["title"] = _make_title(question, files)

        if info.get("chat") is None:
            try:
                # ⬇️ This is the ONLY line that changed.
                info["chat"] = agent.create_chat(user_id=user_id, history=info.get("history", []))
            except Exception as e:
                return jsonify(error=f"Could not rebuild the chat memory: {e}"), 500

        try:
            if hasattr(agent, "ask_with_tools"):
                answer, tools = agent.ask_with_tools(info["chat"], contents)
            else:
                answer, tools = agent.ask(info["chat"], contents), []
        except errors.APIError as error:
            return jsonify(error=friendly_error(error)), 502
        except Exception as error:
            return jsonify(error=f"Unexpected error: {error}"), 500

        user_text = question or "Please look at the attached file(s)."
        info["history"].append({"role": "user",  "text": user_text})
        info["history"].append({"role": "model", "text": answer})
        if len(info["history"]) > 60:
            info["history"] = info["history"][-60:]
        _save_chats_to_disk()

    return jsonify(answer=answer, tools=tools)


@app.post("/api/extract")
def extract():
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify(error="No file received."), 400

    name = upload.filename
    extension = os.path.splitext(name)[1].lower()
    data = upload.read()
    if len(data) > MAX_FILE_MB * 1024 * 1024:
        return jsonify(error=f"'{name}' is bigger than {MAX_FILE_MB} MB."), 400

    try:
        if extension == ".pdf":
            text = extract_pdf(data)
        elif extension == ".docx":
            text = extract_docx(data)
        elif extension in TEXT_EXTENSIONS:
            text = data.decode("utf-8-sig", errors="replace")
        else:
            return jsonify(error=f"'{name}' cannot be read aloud. Try PDF, Word (.docx) or a text file."), 400
    except SetupError as error:
        return jsonify(error=str(error)), 400
    except Exception as error:
        return jsonify(error=f"Could not read '{name}': {error}"), 400

    text = text.strip()
    if not text:
        return jsonify(
            error="No readable text found. If this is a scanned PDF or an image, attach it to the chat, "
                  "ask me to transcribe the text, then press 🔊 Listen on my answer."
        ), 400
    return jsonify(text=text[:MAX_READ_CHARS], name=name, truncated=len(text) > MAX_READ_CHARS)


@app.get("/files/<path:name>")
def files(name):
    is_image = name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".svg"))
    download = bool(request.args.get("download")) or not is_image
    response = send_from_directory(agent.OUTPUT_DIR, name, as_attachment=download)
    if name.lower().endswith(".svg"):
        response.mimetype = "image/svg+xml"
        response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.post("/api/reset")
def reset():
    data = request.get_json(silent=True) or {}
    sid = data.get("session") or "default"
    user_id = (data.get("user_id") or "anonymous").strip() or "anonymous"
    with lock:
        if sid == "*":
            chats.pop(user_id, None)
        else:
            user_chats = chats.get(user_id)
            if user_chats:
                user_chats.pop(sid, None)
                if not user_chats:
                    chats.pop(user_id, None)
        _save_chats_to_disk()
    return jsonify(ok=True)


if __name__ == "__main__":
    if not os.getenv("GEMINI_API_KEY"):
        print("No API key found. Create a .env file with: GEMINI_API_KEY=your_key_here")
        sys.exit(1)
    url = "http://127.0.0.1:5000"
    print(f"\n  NeuraxisAI is running at {url}")
    print("  Press Ctrl + C in this terminal to stop it.\n")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=5000, debug=False)
