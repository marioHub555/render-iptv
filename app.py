import os
import subprocess
import requests
import re
import time
import threading
import shutil
import uuid
import json
from flask import Flask, Response, request, render_template_string, jsonify, send_from_directory
from urllib.parse import unquote, quote

app = Flask(__name__)

CACHE_DIR = "/tmp/iptv_hls"
os.makedirs(CACHE_DIR, exist_ok=True)

class HLSStreamManager:
    def __init__(self):
        self.active_streams = {}
        self.lock = threading.Lock()
        self.cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self.cleanup_thread.start()
    
    def _cleanup_loop(self):
        while True:
            time.sleep(300)
            self._cleanup_old_streams()
    
    def _cleanup_old_streams(self):
        current_time = time.time()
        with self.lock:
            to_remove = []
            for stream_id, info in self.active_streams.items():
                if current_time - info.get('last_access', 0) > 600:
                    to_remove.append(stream_id)
            
            for stream_id in to_remove:
                self._stop_stream(stream_id)
                stream_dir = os.path.join(CACHE_DIR, stream_id)
                if os.path.exists(stream_dir):
                    shutil.rmtree(stream_dir, ignore_errors=True)
                if stream_id in self.active_streams:
                    del self.active_streams[stream_id]
                print(f"[CLEANUP] Removed stream: {stream_id}")
    
    def _stop_stream(self, stream_id):
        info = self.active_streams.get(stream_id, {})
        processes = info.get('processes', [])
        for proc_info in processes:
            process = proc_info.get('process')
            if process and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=3)
                except:
                    try:
                        process.kill()
                        process.wait(timeout=2)
                    except:
                        pass
    
    def create_stream(self, url, stream_id, base_quality="auto", custom_settings=None):
        stream_dir = os.path.join(CACHE_DIR, stream_id)
        os.makedirs(stream_dir, exist_ok=True)
        
        if custom_settings:
            qualities = self._build_qualities_from_settings(custom_settings)
        elif base_quality == "auto":
            qualities = [
                {"name": "360p", "resolution": "640x360", "video_bitrate": "800k", "audio_bitrate": "96k"},
                {"name": "480p", "resolution": "854x480", "video_bitrate": "1200k", "audio_bitrate": "128k"},
                {"name": "720p", "resolution": "1280x720", "video_bitrate": "2500k", "audio_bitrate": "128k"},
                {"name": "1080p", "resolution": "1920x1080", "video_bitrate": "4000k", "audio_bitrate": "192k"}
            ]
        elif base_quality == "hd":
            qualities = [
                {"name": "480p", "resolution": "854x480", "video_bitrate": "1000k", "audio_bitrate": "96k"},
                {"name": "720p", "resolution": "1280x720", "video_bitrate": "2000k", "audio_bitrate": "128k"}
            ]
        elif base_quality == "low":
            qualities = [
                {"name": "240p", "resolution": "426x240", "video_bitrate": "400k", "audio_bitrate": "64k"},
                {"name": "360p", "resolution": "640x360", "video_bitrate": "600k", "audio_bitrate": "80k"}
            ]
        elif base_quality == "worldcup":
            qualities = [
                {"name": "480p", "resolution": "854x480", "video_bitrate": "1500k", "audio_bitrate": "128k"},
                {"name": "720p", "resolution": "1280x720", "video_bitrate": "3000k", "audio_bitrate": "192k"}
            ]
        else:
            qualities = [
                {"name": "360p", "resolution": "640x360", "video_bitrate": "800k", "audio_bitrate": "96k"},
                {"name": "720p", "resolution": "1280x720", "video_bitrate": "2000k", "audio_bitrate": "128k"}
            ]
        
        master_playlist = self._create_master_playlist(qualities)
        master_path = os.path.join(stream_dir, "master.m3u8")
        with open(master_path, 'w') as f:
            f.write(master_playlist)
        
        processes = []
        for quality in qualities:
            process = self._start_quality_stream(url, stream_dir, quality, custom_settings)
            if process:
                processes.append({"process": process, "quality": quality})
        
        with self.lock:
            self.active_streams[stream_id] = {
                'processes': processes,
                'qualities': qualities,
                'last_access': time.time(),
                'url': url,
                'start_time': time.time()
            }
        
        return stream_id
    
    def _build_qualities_from_settings(self, settings):
        v_bitrate = settings.get('video_bitrate', 2000)
        a_bitrate = settings.get('audio_bitrate', 128)
        resolution = settings.get('resolution', '1280x720')
        fps = settings.get('fps', 25)
        
        qualities = [{
            "name": "custom",
            "resolution": resolution,
            "video_bitrate": f"{v_bitrate}k",
            "audio_bitrate": f"{a_bitrate}k",
            "fps": str(fps)
        }]
        
        if resolution == '1920x1080':
            qualities.insert(0, {
                "name": "720p_backup",
                "resolution": "1280x720",
                "video_bitrate": f"{int(v_bitrate*0.5)}k",
                "audio_bitrate": f"{a_bitrate}k"
            })
        elif resolution == '1280x720':
            qualities.insert(0, {
                "name": "480p_backup",
                "resolution": "854x480",
                "video_bitrate": f"{int(v_bitrate*0.4)}k",
                "audio_bitrate": f"{a_bitrate}k"
            })
        
        return qualities
    
    def _create_master_playlist(self, qualities):
        playlist = "#EXTM3U\n"
        playlist += "#EXT-X-VERSION:3\n\n"
        
        for quality in qualities:
            v_bw = int(quality['video_bitrate'].replace('k', '')) * 1000
            a_bw = int(quality['audio_bitrate'].replace('k', '')) * 1000
            bandwidth = v_bw + a_bw
            playlist += f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={quality['resolution']}\n"
            playlist += f"{quality['name']}/playlist.m3u8\n\n"
        
        return playlist
    
    def _start_quality_stream(self, url, stream_dir, quality, custom_settings=None):
        quality_dir = os.path.join(stream_dir, quality['name'])
        os.makedirs(quality_dir, exist_ok=True)
        
        segment_pattern = os.path.join(quality_dir, "segment_%03d.ts")
        playlist_path = os.path.join(quality_dir, "playlist.m3u8")
        
        preset = 'veryfast'
        tune = 'zerolatency'
        gop = '50'
        bufsize_mult = 2
        
        if custom_settings:
            preset = custom_settings.get('preset', 'veryfast')
            tune = custom_settings.get('tune', 'film')
            gop = str(custom_settings.get('gop', 50))
            bufsize_mult = custom_settings.get('bufsize_mult', 2)
        
        ffmpeg_cmd = [
            'ffmpeg',
            '-hide_banner',
            '-loglevel', 'warning',
            '-fflags', '+genpts+discardcorrupt+igndts',
            '-flags', 'low_delay',
            '-reconnect', '1',
            '-reconnect_streamed', '1',
            '-reconnect_delay_max', '5',
            '-reconnect_on_network_error', '1',
            '-reconnect_on_http_error', '4xx,5xx',
            '-timeout', '10000000',
            '-thread_queue_size', '4096',
            '-i', url,
            '-c:v', 'libx264',
            '-b:v', quality['video_bitrate'],
            '-maxrate', quality['video_bitrate'],
            '-bufsize', str(int(quality['video_bitrate'].replace('k', '')) * bufsize_mult) + 'k',
            '-s', quality['resolution'],
            '-r', quality.get('fps', '25'),
            '-preset', preset,
            '-tune', tune,
            '-g', gop,
            '-keyint_min', gop,
            '-sc_threshold', '0',
            '-refs', '2',
            '-bf', '0',
            '-c:a', 'aac',
            '-b:a', quality['audio_bitrate'],
            '-ar', '48000',
            '-ac', '2',
            '-async', '1',
            '-vsync', 'cfr',
            '-f', 'hls',
            '-hls_time', '4',
            '-hls_list_size', '6',
            '-hls_flags', 'delete_segments+independent_segments',
            '-hls_segment_type', 'mpegts',
            '-hls_segment_filename', segment_pattern,
            '-start_number', '0',
            playlist_path
        ]
        
        try:
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            print(f"[HLS] Started {quality['name']}: {quality['resolution']} @ {quality['video_bitrate']}")
            return process
        except Exception as e:
            print(f"[HLS] Error starting {quality['name']}: {e}")
            return None
    
    def get_stream_status(self, stream_id):
        with self.lock:
            info = self.active_streams.get(stream_id, {})
            if not info:
                return None
            
            status = {
                'active': True,
                'running_since': time.time() - info.get('start_time', 0),
                'qualities': [q['name'] for q in info.get('qualities', [])],
                'last_access': time.time() - info.get('last_access', 0)
            }
            
            for proc_info in info.get('processes', []):
                process = proc_info['process']
                if process.poll() is not None:
                    status['active'] = False
                    status['error'] = f"Process died for {proc_info['quality']['name']}"
            
            return status
    
    def update_access(self, stream_id):
        with self.lock:
            if stream_id in self.active_streams:
                self.active_streams[stream_id]['last_access'] = time.time()

stream_manager = HLSStreamManager()

def parse_m3u_content(content):
    channels = []
    lines = content.split('\n')
    current_name = None
    current_attrs = {}
    
    for line in lines:
        line = line.strip()
        if line.startswith('#EXTINF:'):
            name_match = re.search(r',([^,]+)$', line)
            if name_match:
                current_name = name_match.group(1).strip()
            else:
                current_name = "قناة بدون اسم"
            
            tvg_id = re.search(r'tvg-id="([^"]*)"', line)
            tvg_logo = re.search(r'tvg-logo="([^"]*)"', line)
            group = re.search(r'group-title="([^"]*)"', line)
            
            current_attrs = {
                'tvg_id': tvg_id.group(1) if tvg_id else '',
                'tvg_logo': tvg_logo.group(1) if tvg_logo else '',
                'group': group.group(1) if group else 'General'
            }
            
        elif line.startswith('http://') or line.startswith('https://'):
            if current_name:
                channels.append({
                    "name": current_name,
                    "url": line,
                    **current_attrs
                })
                current_name = None
                current_attrs = {}
            else:
                channels.append({
                    "name": line.split('/')[-1].split('.')[0] or "قناة",
                    "url": line,
                    "tvg_id": "",
                    "tvg_logo": "",
                    "group": "General"
                })
    
    return channels

HTML_INTERFACE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>📺 IPTV Pro - بث متكيف بدون انقطاع</title>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@1.4.12"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        
        :root {
            --primary: #58a6ff;
            --secondary: #238636;
            --danger: #da3633;
            --warning: #d29922;
            --gold: #ffd700;
            --bg-dark: #0d1117;
            --bg-card: #161b22;
            --bg-hover: #21262d;
            --border: #30363d;
            --text: #c9d1d9;
            --text-muted: #8b949e;
        }
        
        body { 
            background-color: var(--bg-dark); 
            color: var(--text); 
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            min-height: 100vh;
            line-height: 1.6;
        }
        
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        
        .header {
            text-align: center;
            padding: 30px 20px;
            background: var(--bg-card);
            border-radius: 16px;
            margin-bottom: 25px;
            border: 1px solid var(--border);
            position: relative;
            overflow: hidden;
        }
        
        .header::before {
            content: '';
            position: absolute;
            top: 0; left: 0; right: 0;
            height: 3px;
            background: var(--primary);
        }
        
        .header h1 { 
            color: var(--primary); 
            font-size: 32px; 
            margin-bottom: 10px;
            text-shadow: 0 2px 10px rgba(88,166,255,0.3);
        }
        
        .header p { color: var(--text-muted); font-size: 15px; }
        
        .tech-badges {
            display: flex;
            gap: 10px;
            justify-content: center;
            margin-top: 15px;
            flex-wrap: wrap;
        }
        
        .tech-badge {
            background: rgba(88,166,255,0.1);
            border: 1px solid var(--primary);
            color: var(--primary);
            padding: 5px 14px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
        }
        
        .card {
            background: var(--bg-card);
            border-radius: 12px;
            padding: 25px;
            margin-bottom: 20px;
            border: 1px solid var(--border);
            transition: all 0.3s;
        }
        
        .card:hover { border-color: var(--primary); }
        
        .card-title {
            color: var(--primary);
            font-size: 18px;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .form-group { margin-bottom: 20px; }
        
        label {
            display: block;
            font-weight: 600;
            margin-bottom: 8px;
            color: var(--text);
            font-size: 14px;
        }
        
        input[type="text"],
        input[type="file"],
        select {
            width: 100%;
            padding: 14px;
            background: var(--bg-dark);
            border: 1px solid var(--border);
            border-radius: 10px;
            color: #fff;
            font-size: 14px;
            transition: all 0.3s;
        }
        
        input:focus, select:focus {
            outline: none;
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(88,166,255,0.1);
        }
        
        .btn {
            padding: 14px 24px;
            border: none;
            border-radius: 10px;
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }
        
        .btn-primary {
            background: var(--primary);
            color: #fff;
        }
        
        .btn-primary:hover {
            background: #79b8ff;
            transform: translateY(-2px);
            box-shadow: 0 4px 15px rgba(88,166,255,0.3);
        }
        
        .btn-success {
            background: var(--secondary);
            color: #fff;
        }
        
        .btn-success:hover {
            background: #2ea043;
            transform: translateY(-2px);
        }
        
        .btn-warning {
            background: var(--warning);
            color: #000;
        }
        
        .btn-danger {
            background: var(--danger);
            color: #fff;
        }
        
        .btn-gold {
            background: var(--gold);
            color: #000;
            font-weight: 700;
        }
        
        .btn-gold:hover {
            background: #ffed4a;
            transform: translateY(-2px);
            box-shadow: 0 4px 15px rgba(255,215,0,0.3);
        }
        
        .btn-group {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
        }
        
        .quality-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-top: 15px;
        }
        
        .quality-card {
            background: var(--bg-dark);
            border: 2px solid var(--border);
            border-radius: 12px;
            padding: 20px;
            cursor: pointer;
            transition: all 0.3s;
            text-align: center;
        }
        
        .quality-card:hover {
            border-color: var(--primary);
            transform: translateY(-3px);
        }
        
        .quality-card.active {
            border-color: var(--secondary);
            background: rgba(35,134,54,0.1);
        }
        
        .quality-card .quality-icon {
            font-size: 28px;
            margin-bottom: 10px;
        }
        
        .quality-card h3 {
            font-size: 16px;
            margin-bottom: 5px;
        }
        
        .quality-card p {
            font-size: 12px;
            color: var(--text-muted);
        }
        
        .quality-card .badge {
            display: inline-block;
            padding: 3px 10px;
            border-radius: 12px;
            font-size: 11px;
            margin-top: 8px;
            font-weight: 600;
        }
        
        .badge-green { background: var(--secondary); color: #fff; }
        .badge-yellow { background: var(--warning); color: #000; }
        .badge-red { background: var(--danger); color: #fff; }
        .badge-blue { background: var(--primary); color: #fff; }
        .badge-gold { background: var(--gold); color: #000; }
        
        .video-container {
            position: relative;
            background: #000;
            border-radius: 12px;
            overflow: hidden;
            border: 2px solid var(--primary);
            display: none;
            margin-top: 25px;
        }
        
        .video-container.active { display: block; }
        
        video {
            width: 100%;
            height: auto;
            max-height: 70vh;
            display: block;
        }
        
        .player-overlay {
            position: absolute;
            top: 10px; right: 10px;
            display: flex;
            gap: 10px;
            z-index: 10;
        }
        
        .player-badge {
            background: rgba(0,0,0,0.8);
            color: #fff;
            padding: 5px 12px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 600;
        }
        
        .player-badge.live {
            background: var(--danger);
            animation: pulse 2s infinite;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.7; }
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 12px;
            margin-top: 15px;
        }
        
        .stat-item {
            background: var(--bg-dark);
            padding: 15px;
            border-radius: 10px;
            border: 1px solid var(--border);
            text-align: center;
        }
        
        .stat-value {
            color: var(--primary);
            font-size: 20px;
            font-weight: bold;
        }
        
        .stat-label {
            color: var(--text-muted);
            font-size: 12px;
            margin-top: 5px;
        }
        
        .progress-container {
            margin-top: 20px;
            display: none;
        }
        
        .progress-bar {
            height: 8px;
            background: var(--bg-dark);
            border-radius: 4px;
            overflow: hidden;
        }
        
        .progress-fill {
            height: 100%;
            background: var(--secondary);
            width: 0%;
            transition: width 0.5s;
            border-radius: 4px;
        }
        
        .progress-info {
            display: flex;
            justify-content: space-between;
            margin-top: 8px;
            font-size: 13px;
            color: var(--text-muted);
        }
        
        .channels-list {
            max-height: 400px;
            overflow-y: auto;
            background: var(--bg-dark);
            border-radius: 10px;
            border: 1px solid var(--border);
            padding: 10px;
        }
        
        .channel-item {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 12px;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.2s;
            border-bottom: 1px solid var(--border);
        }
        
        .channel-item:last-child { border-bottom: none; }
        
        .channel-item:hover {
            background: rgba(88,166,255,0.1);
        }
        
        .channel-item.selected {
            background: rgba(35,134,54,0.2);
            border: 1px solid var(--secondary);
        }
        
        .channel-logo {
            width: 40px;
            height: 40px;
            border-radius: 8px;
            object-fit: cover;
            background: var(--bg-hover);
        }
        
        .channel-info { flex: 1; }
        
        .channel-name { font-weight: 600; font-size: 14px; }
        
        .channel-group { font-size: 12px; color: var(--text-muted); }
        
        .channel-status {
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: 600;
        }
        
        .status-online { background: rgba(35,134,54,0.2); color: #3fb950; }
        .status-offline { background: rgba(218,54,51,0.2); color: var(--danger); }
        
        .network-status {
            position: fixed;
            bottom: 20px;
            left: 20px;
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 15px;
            display: none;
            z-index: 1000;
            max-width: 300px;
        }
        
        .network-status.active { display: block; }
        
        .network-status h4 {
            color: var(--primary);
            margin-bottom: 10px;
            font-size: 14px;
        }
        
        .network-bar {
            height: 6px;
            background: var(--bg-dark);
            border-radius: 3px;
            margin: 5px 0;
            overflow: hidden;
        }
        
        .network-bar-fill {
            height: 100%;
            border-radius: 3px;
            transition: width 0.5s;
        }
        
        .network-good { background: var(--secondary); }
        .network-medium { background: var(--warning); }
        .network-bad { background: var(--danger); }
        
        .custom-settings {
            background: var(--bg-dark);
            border-radius: 10px;
            padding: 20px;
            margin-top: 15px;
            border: 1px solid var(--border);
        }
        
        .setting-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 12px 0;
            border-bottom: 1px solid var(--border);
        }
        
        .setting-row:last-child { border-bottom: none; }
        
        .setting-label { font-size: 14px; }
        
        .setting-control {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .setting-btn {
            width: 36px;
            height: 36px;
            border-radius: 8px;
            border: 1px solid var(--border);
            background: var(--bg-card);
            color: var(--primary);
            font-size: 18px;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        
        .setting-btn:hover {
            background: var(--primary);
            color: #fff;
        }
        
        .setting-value {
            min-width: 80px;
            text-align: center;
            font-weight: 600;
            color: var(--primary);
        }
        
        .apply-btn {
            width: 100%;
            margin-top: 15px;
            padding: 12px;
            font-size: 16px;
        }
        
        @media (max-width: 768px) {
            .header h1 { font-size: 24px; }
            .quality-grid { grid-template-columns: 1fr; }
            .stats-grid { grid-template-columns: repeat(2, 1fr); }
        }
        
        ::-webkit-scrollbar { width: 8px; }
        ::-webkit-scrollbar-track { background: var(--bg-dark); }
        ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: var(--primary); }
        
        .hidden { display: none !important; }
    </style>
</head>
<body>

<div class="container">
    <div class="header">
        <h1>📺 IPTV Pro</h1>
        <p>🏆 وضع كأس العالم - جودة عالية + استقرار تام</p>
        <div class="tech-badges">
            <span class="tech-badge">🎯 Adaptive Bitrate</span>
            <span class="tech-badge">⚡ HLS Streaming</span>
            <span class="tech-badge">🔄 Auto Recovery</span>
            <span class="tech-badge">📊 Network Monitor</span>
        </div>
    </div>

    <div class="card">
        <div class="card-title">📡 مصدر البث</div>
        
        <div class="form-group">
            <label>نوع المصدر:</label>
            <select id="sourceType" onchange="handleSourceChange()">
                <option value="single">🔗 رابط مباشر (قناة واحدة)</option>
                <option value="m3u">📁 ملف أو رابط M3U</option>
            </select>
        </div>
        
        <div id="singleArea">
            <div class="form-group">
                <label>رابط البث المباشر:</label>
                <input type="text" id="singleUrl" placeholder="http://example.com/stream" value="http://ugeen.live:8080/Ugeen_VIPmS3NcQ/qQPQWj/4540">
            </div>
        </div>
        
        <div id="m3uArea" class="hidden">
            <div class="btn-group" style="margin-bottom: 15px;">
                <button class="btn btn-primary" id="btnLink" onclick="switchM3uMethod('link')">🔗 رابط M3U</button>
                <button class="btn btn-warning" id="btnFile" onclick="switchM3uMethod('file')">📁 رفع ملف</button>
            </div>
            
            <div id="m3uUrlInput">
                <div class="form-group">
                    <label>رابط ملف M3U:</label>
                    <input type="text" id="m3uUrl" placeholder="https://example.com/playlist.m3u">
                </div>
            </div>
            
            <div id="m3uFileInput" class="hidden">
                <div class="form-group">
                    <label>اختر ملف M3U:</label>
                    <input type="file" id="m3uFile" accept=".m3u,.m3u8">
                </div>
            </div>
            
            <button class="btn btn-primary" onclick="processM3U()" style="width: 100%;">🔄 تحليل واستخراج القنوات</button>
        </div>
    </div>

    <div class="card hidden" id="channelsCard">
        <div class="card-title">📋 القنوات المتاحة</div>
        <div class="form-group">
            <input type="text" id="channelSearch" placeholder="🔍 ابحث عن قناة..." oninput="filterChannels()" style="margin-bottom: 10px;">
        </div>
        <div class="channels-list" id="channelsList"></div>
    </div>

    <div class="card">
        <div class="card-title">🎯 وضع البث</div>
        
        <div class="quality-grid">
            <div class="quality-card" onclick="selectQuality('worldcup')" id="q-worldcup">
                <div class="quality-icon">🏆</div>
                <h3>كأس العالم</h3>
                <p>720p + 480p | استقرار عالي</p>
                <span class="badge badge-gold">مخصص للمباريات</span>
            </div>
            
            <div class="quality-card active" onclick="selectQuality('auto')" id="q-auto">
                <div class="quality-icon">🤖</div>
                <h3>تلقائي</h3>
                <p>يتكيف مع سرعة الإنترنت</p>
                <span class="badge badge-green">موصى به</span>
            </div>
            
            <div class="quality-card" onclick="selectQuality('hd')" id="q-hd">
                <div class="quality-icon">🎬</div>
                <h3>HD Plus</h3>
                <p>720p + 480p</p>
                <span class="badge badge-blue">جودة عالية</span>
            </div>
            
            <div class="quality-card" onclick="selectQuality('low')" id="q-low">
                <div class="quality-icon">📉</div>
                <h3>إنترنت ضعيف</h3>
                <p>360p + 240p</p>
                <span class="badge badge-yellow">توفير بيانات</span>
            </div>
            
            <div class="quality-card" onclick="selectQuality('custom')" id="q-custom">
                <div class="quality-icon">⚙️</div>
                <h3>مخصص يدوي</h3>
                <p>تحكم كامل في الإعدادات</p>
                <span class="badge badge-red">للمستخدمين المتقدمين</span>
            </div>
        </div>
        
        <div class="stats-grid" style="margin-top: 20px;">
            <div class="stat-item">
                <div class="stat-value" id="statRes">Auto</div>
                <div class="stat-label">الدقة</div>
            </div>
            <div class="stat-item">
                <div class="stat-value" id="statBitrate">~2.5 Mbps</div>
                <div class="stat-label">معدل البت</div>
            </div>
            <div class="stat-item">
                <div class="stat-value" id="statBuffer">4s</div>
                <div class="stat-label">المخزن المؤقت</div>
            </div>
            <div class="stat-item">
                <div class="stat-value" id="statLatency">2-5s</div>
                <div class="stat-label">التأخير</div>
            </div>
        </div>
    </div>

    <div class="card" id="customSettingsCard" style="display: none;">
        <div class="card-title">⚙️ إعدادات مخصصة</div>
        
        <div class="custom-settings">
            <div class="setting-row">
                <span class="setting-label">🎬 معدل بت الفيديو:</span>
                <div class="setting-control">
                    <button class="setting-btn" onclick="adjustSetting('vBitrate', -500)">−</button>
                    <span class="setting-value" id="vBitrateVal">2000 Kbps</span>
                    <button class="setting-btn" onclick="adjustSetting('vBitrate', 500)">+</button>
                </div>
            </div>
            
            <div class="setting-row">
                <span class="setting-label">🔊 معدل بت الصوت:</span>
                <div class="setting-control">
                    <button class="setting-btn" onclick="adjustSetting('aBitrate', -32)">−</button>
                    <span class="setting-value" id="aBitrateVal">128 Kbps</span>
                    <button class="setting-btn" onclick="adjustSetting('aBitrate', 32)">+</button>
                </div>
            </div>
            
            <div class="setting-row">
                <span class="setting-label">📐 الدقة:</span>
                <div class="setting-control">
                    <select id="customResolution" style="width: 120px; padding: 8px;">
                        <option value="426x240">240p</option>
                        <option value="640x360">360p</option>
                        <option value="854x480">480p</option>
                        <option value="1280x720" selected>720p</option>
                        <option value="1920x1080">1080p</option>
                    </select>
                </div>
            </div>
            
            <div class="setting-row">
                <span class="setting-label">🎞️ معدل الإطارات:</span>
                <div class="setting-control">
                    <button class="setting-btn" onclick="adjustSetting('fps', -5)">−</button>
                    <span class="setting-value" id="fpsVal">25 fps</span>
                    <button class="setting-btn" onclick="adjustSetting('fps', 5)">+</button>
                </div>
            </div>
            
            <div class="setting-row">
                <span class="setting-label">📦 حجم المخزن:</span>
                <div class="setting-control">
                    <button class="setting-btn" onclick="adjustSetting('buffer', -2)">−</button>
                    <span class="setting-value" id="bufferVal">4s</span>
                    <button class="setting-btn" onclick="adjustSetting('buffer', 2)">+</button>
                </div>
            </div>
        </div>
    </div>

    <div class="card">
        <div class="card-title">🔧 إعدادات سريعة</div>
        <div class="btn-group" style="justify-content: center;">
            <button class="btn btn-primary" onclick="applySettings()">✅ تطبيق الإعدادات</button>
            <button class="btn btn-warning" onclick="restartStream()">🔄 إعادة تشغيل البث</button>
            <button class="btn btn-danger" onclick="stopStream()">⏹️ إيقاف البث</button>
        </div>
    </div>

    <button class="btn btn-gold" onclick="startStream()" id="startBtn" style="width: 100%; font-size: 18px; padding: 20px;">
        🚀 ابدأ البث المباشر
    </button>

    <div class="progress-container" id="progressContainer">
        <div class="progress-bar">
            <div class="progress-fill" id="progressFill"></div>
        </div>
        <div class="progress-info">
            <span id="progressStatus">جاري إعداد البث...</span>
            <span id="progressPercent">0%</span>
        </div>
    </div>

    <div class="video-container" id="videoContainer">
        <div class="player-overlay">
            <span class="player-badge live" id="liveBadge">🔴 LIVE</span>
            <span class="player-badge" id="qualityBadge">Auto</span>
            <span class="player-badge" id="networkBadge">🟢 جيد</span>
        </div>
        <video id="player" controls playsinline></video>
    </div>

    <div class="network-status" id="networkStatus">
        <h4>📊 حالة الشبكة</h4>
        <div style="margin-bottom: 8px;">
            <span>سرعة التحميل:</span>
            <div class="network-bar"><div class="network-bar-fill network-good" id="speedBar" style="width: 80%"></div></div>
            <span id="speedValue">2.5 Mbps</span>
        </div>
        <div style="margin-bottom: 8px;">
            <span>جودة البث:</span>
            <div class="network-bar"><div class="network-bar-fill network-good" id="qualityBar" style="width: 90%"></div></div>
            <span id="qualityValue">720p</span>
        </div>
        <div>
            <span>المخزن المؤقت:</span>
            <div class="network-bar"><div class="network-bar-fill network-medium" id="bufferBar" style="width: 60%"></div></div>
            <span id="bufferValue">4.2s</span>
        </div>
    </div>
</div>

<script>
    let hls = null;
    let currentStreamId = null;
    let currentQuality = 'auto';
    let currentM3uMethod = 'link';
    let allChannels = [];
    let selectedChannelUrl = "";
    let networkMonitorInterval = null;
    let isPlaying = false;
    
    let customSettings = {
        vBitrate: 2000,
        aBitrate: 128,
        resolution: '1280x720',
        fps: 25,
        buffer: 4
    };
    
    function handleSourceChange() {
        const type = document.getElementById('sourceType').value;
        document.getElementById('singleArea').classList.toggle('hidden', type !== 'single');
        document.getElementById('m3uArea').classList.toggle('hidden', type !== 'm3u');
        if (type === 'single') {
            document.getElementById('channelsCard').classList.add('hidden');
        }
    }
    
    function switchM3uMethod(method) {
        currentM3uMethod = method;
        document.getElementById('btnLink').classList.toggle('btn-primary', method === 'link');
        document.getElementById('btnLink').classList.toggle('btn-warning', method !== 'link');
        document.getElementById('btnFile').classList.toggle('btn-primary', method === 'file');
        document.getElementById('btnFile').classList.toggle('btn-warning', method !== 'file');
        document.getElementById('m3uUrlInput').classList.toggle('hidden', method !== 'link');
        document.getElementById('m3uFileInput').classList.toggle('hidden', method !== 'file');
    }
    
    function processM3U() {
        if (currentM3uMethod === 'link') {
            const url = document.getElementById('m3uUrl').value.trim();
            if (!url) { alert('⚠️ أدخل رابط M3U'); return; }
            
            fetch('/parse_m3u_url?url=' + encodeURIComponent(url))
                .then(r => r.json())
                .then(data => showChannels(data))
                .catch(err => alert('❌ خطأ: ' + err));
        } else {
            const file = document.getElementById('m3uFile').files[0];
            if (!file) { alert('⚠️ اختر ملف'); return; }
            
            const formData = new FormData();
            formData.append('file', file);
            
            fetch('/parse_m3u_file', { method: 'POST', body: formData })
                .then(r => r.json())
                .then(data => showChannels(data))
                .catch(err => alert('❌ خطأ: ' + err));
        }
    }
    
    function showChannels(data) {
        allChannels = data || [];
        if (allChannels.length === 0) { alert('⚠️ لم يتم العثور على قنوات'); return; }
        
        document.getElementById('channelsCard').classList.remove('hidden');
        filterChannels();
    }
    
    function filterChannels() {
        const query = document.getElementById('channelSearch').value.toLowerCase();
        const container = document.getElementById('channelsList');
        container.innerHTML = '';
        
        allChannels.forEach((ch, i) => {
            if (ch.name.toLowerCase().includes(query)) {
                const item = document.createElement('div');
                item.className = 'channel-item' + (ch.url === selectedChannelUrl ? ' selected' : '');
                item.innerHTML = `
                    <img src="${ch.tvg_logo || 'https://via.placeholder.com/40'}" class="channel-logo" onerror="this.src='https://via.placeholder.com/40'">
                    <div class="channel-info">
                        <div class="channel-name">${ch.name}</div>
                        <div class="channel-group">${ch.group || 'عام'}</div>
                    </div>
                    <span class="channel-status status-online">متاح</span>
                `;
                item.onclick = () => selectChannel(ch.url, item);
                container.appendChild(item);
            }
        });
    }
    
    function selectChannel(url, element) {
        selectedChannelUrl = url;
        document.querySelectorAll('.channel-item').forEach(el => el.classList.remove('selected'));
        element.classList.add('selected');
    }
    
    function selectQuality(q) {
        currentQuality = q;
        document.querySelectorAll('.quality-card').forEach(c => c.classList.remove('active'));
        document.getElementById('q-' + q).classList.add('active');
        
        const customCard = document.getElementById('customSettingsCard');
        if (q === 'custom') {
            customCard.style.display = 'block';
        } else {
            customCard.style.display = 'none';
        }
        
        const stats = {
            'worldcup': { res: '720p/480p', bitrate: '~3.0 Mbps', buffer: '6s', latency: '3-6s' },
            'auto': { res: 'Auto', bitrate: '~2.5 Mbps', buffer: '4s', latency: '2-5s' },
            'hd': { res: '720p/480p', bitrate: '~2.0 Mbps', buffer: '4s', latency: '3-6s' },
            'low': { res: '360p/240p', bitrate: '~600 Kbps', buffer: '4s', latency: '2-4s' },
            'custom': { res: 'مخصص', bitrate: 'حسب الإعدادات', buffer: '4s', latency: '2-5s' }
        };
        
        const s = stats[q];
        document.getElementById('statRes').textContent = s.res;
        document.getElementById('statBitrate').textContent = s.bitrate;
        document.getElementById('statBuffer').textContent = s.buffer;
        document.getElementById('statLatency').textContent = s.latency;
        
        if (isPlaying && currentStreamId) {
            applySettings();
        }
    }
    
    function adjustSetting(key, delta) {
        if (key === 'vBitrate') {
            customSettings.vBitrate = Math.max(500, Math.min(8000, customSettings.vBitrate + delta));
            document.getElementById('vBitrateVal').textContent = customSettings.vBitrate + ' Kbps';
        } else if (key === 'aBitrate') {
            customSettings.aBitrate = Math.max(32, Math.min(320, customSettings.aBitrate + delta));
            document.getElementById('aBitrateVal').textContent = customSettings.aBitrate + ' Kbps';
        } else if (key === 'fps') {
            customSettings.fps = Math.max(15, Math.min(60, customSettings.fps + delta));
            document.getElementById('fpsVal').textContent = customSettings.fps + ' fps';
        } else if (key === 'buffer') {
            customSettings.buffer = Math.max(2, Math.min(30, customSettings.buffer + delta));
            document.getElementById('bufferVal').textContent = customSettings.buffer + 's';
        }
    }
    
    async function applySettings() {
        if (!isPlaying || !currentStreamId) {
            alert('⚠️ ابدأ البث أولاً!');
            return;
        }
        
        const btn = document.getElementById('startBtn');
        btn.disabled = true;
        btn.textContent = '⏳ جاري تطبيق الإعدادات...';
        
        try {
            await fetch('/stop_stream/' + currentStreamId);
            await startStream(true);
            
            btn.textContent = '✅ تم تطبيق الإعدادات!';
            setTimeout(() => {
                btn.textContent = '🔄 تبديل القناة';
                btn.disabled = false;
            }, 2000);
            
        } catch (err) {
            alert('❌ خطأ: ' + err.message);
            btn.disabled = false;
        }
    }
    
    async function restartStream() {
        if (!isPlaying || !currentStreamId) {
            alert('⚠️ لا يوجد بث يعمل!');
            return;
        }
        
        await stopStream();
        await startStream();
    }
    
    async function stopStream() {
        if (currentStreamId) {
            await fetch('/stop_stream/' + currentStreamId);
        }
        
        if (hls) {
            hls.destroy();
            hls = null;
        }
        
        document.getElementById('videoContainer').classList.remove('active');
        document.getElementById('networkStatus').classList.remove('active');
        document.getElementById('startBtn').textContent = '🚀 ابدأ البث المباشر';
        isPlaying = false;
        currentStreamId = null;
    }
    
    async function startStream(isRestart = false) {
        const type = document.getElementById('sourceType').value;
        let url = type === 'single' ? document.getElementById('singleUrl').value.trim() : selectedChannelUrl;
        
        if (!url) { alert('⚠️ اختر قناة أولاً'); return; }
        
        const btn = document.getElementById('startBtn');
        btn.disabled = true;
        btn.textContent = '⏳ جاري إعداد البث...';
        
        document.getElementById('progressContainer').style.display = 'block';
        updateProgress(10, 'تحليل المصدر...');
        
        try {
            const params = new URLSearchParams({
                url: url,
                quality: currentQuality
            });
            
            if (currentQuality === 'custom') {
                params.append('v_bitrate', customSettings.vBitrate);
                params.append('a_bitrate', customSettings.aBitrate);
                params.append('resolution', document.getElementById('customResolution').value);
                params.append('fps', customSettings.fps);
                params.append('buffer', customSettings.buffer);
            }
            
            updateProgress(30, 'إنشاء بث HLS...');
            
            const response = await fetch('/create_stream?' + params);
            const data = await response.json();
            
            if (!data.success) {
                throw new Error(data.error || 'Failed to create stream');
            }
            
            currentStreamId = data.stream_id;
            updateProgress(70, 'جاري تحميل المشغل...');
            
            await playStream(data.stream_id);
            
            updateProgress(100, '✅ جاهز!');
            isPlaying = true;
            
            setTimeout(() => {
                document.getElementById('progressContainer').style.display = 'none';
                btn.disabled = false;
                btn.textContent = '🔄 تبديل القناة';
            }, 1000);
            
        } catch (err) {
            alert('❌ خطأ: ' + err.message);
            btn.disabled = false;
            btn.textContent = isRestart ? '🔄 إعادة التشغيل' : '🚀 ابدأ البث المباشر';
            document.getElementById('progressContainer').style.display = 'none';
        }
    }
    
    function updateProgress(percent, status) {
        document.getElementById('progressFill').style.width = percent + '%';
        document.getElementById('progressStatus').textContent = status;
        document.getElementById('progressPercent').textContent = percent + '%';
    }
    
    async function playStream(streamId) {
        const video = document.getElementById('player');
        const container = document.getElementById('videoContainer');
        const hlsUrl = '/hls/' + streamId + '/master.m3u8';
        
        container.classList.add('active');
        document.getElementById('networkStatus').classList.add('active');
        
        if (Hls.isSupported()) {
            if (hls) { hls.destroy(); }
            
            hls = new Hls({
                maxBufferLength: customSettings.buffer || 4,
                maxMaxBufferLength: 60,
                liveSyncDurationCount: 2,
                liveMaxLatencyDurationCount: 5,
                enableWorker: true,
                lowLatencyMode: false,
                backBufferLength: 30,
                startLevel: -1,
                abrEwmaDefaultEstimate: 2000000,
                abrBandWidthFactor: 0.95,
                abrBandWidthUpFactor: 0.7,
                testBandwidth: true,
                progressive: false
            });
            
            hls.loadSource(hlsUrl);
            hls.attachMedia(video);
            
            hls.on(Hls.Events.MANIFEST_PARSED, function(event, data) {
                console.log('Manifest loaded, found ' + data.levels.length + ' quality levels');
                video.play().catch(e => console.log('Autoplay prevented:', e));
            });
            
            hls.on(Hls.Events.LEVEL_SWITCHED, function(event, data) {
                const level = hls.levels[data.level];
                if (level) {
                    document.getElementById('qualityBadge').textContent = level.height + 'p';
                }
            });
            
            hls.on(Hls.Events.ERROR, function(event, data) {
                if (data.fatal) {
                    switch(data.type) {
                        case Hls.ErrorTypes.NETWORK_ERROR:
                            console.log('Network error, trying to recover...');
                            hls.startLoad();
                            break;
                        case Hls.ErrorTypes.MEDIA_ERROR:
                            console.log('Media error, trying to recover...');
                            hls.recoverMediaError();
                            break;
                        default:
                            console.log('Fatal error, destroying...');
                            hls.destroy();
                            break;
                    }
                }
            });
            
            startNetworkMonitor();
            
        } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
            video.src = hlsUrl;
            video.play();
        } else {
            alert('❌ المتصفح لا يدعم HLS');
        }
    }
    
    function startNetworkMonitor() {
        if (networkMonitorInterval) clearInterval(networkMonitorInterval);
        
        networkMonitorInterval = setInterval(() => {
            if (!hls) return;
            
            const level = hls.levels[hls.currentLevel];
            
            if (level) {
                document.getElementById('qualityBadge').textContent = level.height + 'p';
                document.getElementById('qualityValue').textContent = level.height + 'p';
                document.getElementById('qualityBar').style.width = (level.height / 1080 * 100) + '%';
            }
            
            if (video.buffered.length > 0) {
                const buffered = video.buffered.end(0) - video.currentTime;
                document.getElementById('bufferValue').textContent = buffered.toFixed(1) + 's';
                document.getElementById('bufferBar').style.width = Math.min(buffered / 10 * 100, 100) + '%';
                
                if (buffered < 2) {
                    document.getElementById('networkBadge').textContent = '🟡 ضعيف';
                } else if (buffered > 5) {
                    document.getElementById('networkBadge').textContent = '🟢 ممتاز';
                } else {
                    document.getElementById('networkBadge').textContent = '🟢 جيد';
                }
            }
            
        }, 1000);
    }
    
    switchM3uMethod('link');
</script>

</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_INTERFACE)

@app.route('/parse_m3u_url')
def parse_m3u_url():
    m3u_url = request.args.get('url', '')
    if not m3u_url:
        return jsonify([])
    try:
        response = requests.get(m3u_url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        channels = parse_m3u_content(response.text)
    except Exception as e:
        print(f"[M3U ERROR] {e}")
        channels = []
    return jsonify(channels)

@app.route('/parse_m3u_file', methods=['POST'])
def parse_m3u_file():
    if 'file' not in request.files:
        return jsonify([])
    file = request.files['file']
    if file.filename == '':
        return jsonify([])
    try:
        content = file.read().decode('utf-8', errors='ignore')
        channels = parse_m3u_content(content)
    except Exception as e:
        print(f"[M3U FILE ERROR] {e}")
        channels = []
    return jsonify(channels)

@app.route('/create_stream')
def create_stream():
    """إنشاء بث HLS جديد مع إعدادات مخصصة"""
    url = request.args.get('url', '')
    quality = request.args.get('quality', 'auto')
    
    custom_settings = None
    if quality == 'custom':
        custom_settings = {
            'video_bitrate': int(request.args.get('v_bitrate', 2000)),
            'audio_bitrate': int(request.args.get('a_bitrate', 128)),
            'resolution': request.args.get('resolution', '1280x720'),
            'fps': int(request.args.get('fps', 25)),
            'buffer': int(request.args.get('buffer', 4)),
            'preset': 'medium',
            'tune': 'film',
            'gop': 50,
            'bufsize_mult': 3
        }
    
    if not url:
        return jsonify({"success": False, "error": "No URL provided"})
    
    try:
        stream_id = f"stream_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        stream_manager.create_stream(url, stream_id, quality, custom_settings)
        
        return jsonify({
            "success": True,
            "stream_id": stream_id,
            "quality": quality,
            "url": f"/hls/{stream_id}/master.m3u8"
        })
        
    except Exception as e:
        print(f"[CREATE STREAM ERROR] {e}")
        return jsonify({"success": False, "error": str(e)})

@app.route('/hls/<stream_id>/<path:filename>')
def serve_hls(stream_id, filename):
    """تقديم ملفات HLS"""
    stream_manager.update_access(stream_id)
    
    stream_dir = os.path.join(CACHE_DIR, stream_id)
    file_path = os.path.join(stream_dir, filename)
    
    if filename.endswith('.m3u8'):
        if not os.path.exists(file_path):
            if filename == 'master.m3u8':
                return Response("""#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
360p/playlist.m3u8
""", mimetype='application/vnd.apple.mpegurl')
        
        with open(file_path, 'r') as f:
            content = f.read()
        return Response(content, mimetype='application/vnd.apple.mpegurl')
    
    if os.path.exists(file_path):
        return send_from_directory(stream_dir, filename, mimetype='video/mp2t')
    
    return "File not found", 404

@app.route('/stream_status/<stream_id>')
def stream_status(stream_id):
    """الحصول على حالة البث"""
    status = stream_manager.get_stream_status(stream_id)
    if not status:
        return jsonify({"active": False, "error": "Stream not found"})
    return jsonify(status)

@app.route('/stop_stream/<stream_id>')
def stop_stream(stream_id):
    """إيقاف البث"""
    stream_manager._stop_stream(stream_id)
    stream_dir = os.path.join(CACHE_DIR, stream_id)
    if os.path.exists(stream_dir):
        shutil.rmtree(stream_dir, ignore_errors=True)
    return jsonify({"success": True})

if __name__ == '__main__':
    import os
    port = int(os.environ.get("PORT", 7860))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
