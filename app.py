import os
import subprocess
import requests
import re
import time
import threading
from flask import Flask, Response, request, render_template_string, jsonify
from urllib.parse import unquote

app = Flask(__name__)

def parse_m3u_content(content):
    channels = []
    lines = content.split('\n')
    current_name = None
    for line in lines:
        line = line.strip()
        if line.startswith('#EXTINF:'):
            name_match = re.search(r',([^,]+)$', line)
            if name_match:
                current_name = name_match.group(1).strip()
            else:
                current_name = "قناة بدون اسم"
        elif line.startswith('http://') or line.startswith('https://'):
            if current_name:
                channels.append({"name": current_name, "url": line})
                current_name = None
            else:
                channels.append({"name": line.split('/')[-1], "url": line})
    return channels

HTML_INTERFACE = '''
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>📺 IPTV Render</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        :root {
            --primary: #58a6ff;
            --secondary: #238636;
            --danger: #da3633;
            --warning: #d29922;
            --bg-dark: #0d1117;
            --bg-card: #161b22;
            --border: #30363d;
            --text: #c9d1d9;
            --text-muted: #8b949e;
        }
        body { 
            background-color: var(--bg-dark); 
            color: var(--text); 
            font-family: 'Segoe UI', system-ui, sans-serif;
            min-height: 100vh;
        }
        .container { max-width: 800px; margin: 0 auto; padding: 15px; }
        
        .header {
            text-align: center;
            padding: 20px;
            background: var(--bg-card);
            border-radius: 12px;
            margin-bottom: 15px;
            border: 1px solid var(--border);
        }
        .header h1 { color: var(--primary); font-size: 24px; }
        .header p { color: var(--text-muted); font-size: 13px; margin-top: 5px; }
        
        .render-badge {
            background: rgba(88,166,255,0.1);
            border: 1px solid var(--primary);
            color: var(--primary);
            padding: 5px 12px;
            border-radius: 15px;
            font-size: 11px;
            display: inline-block;
            margin-top: 10px;
        }
        
        .card {
            background: var(--bg-card);
            border-radius: 10px;
            padding: 15px;
            margin-bottom: 15px;
            border: 1px solid var(--border);
        }
        .card-title {
            color: var(--primary);
            font-size: 15px;
            margin-bottom: 12px;
            font-weight: 600;
        }
        
        .form-group { margin-bottom: 12px; }
        label { display: block; font-size: 12px; margin-bottom: 5px; font-weight: 600; }
        input, select {
            width: 100%;
            padding: 10px;
            background: var(--bg-dark);
            border: 1px solid var(--border);
            border-radius: 6px;
            color: #fff;
            font-size: 13px;
        }
        
        .btn {
            padding: 10px 16px;
            border: none;
            border-radius: 6px;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
        }
        .btn-primary { background: var(--primary); color: #fff; }
        .btn-success { background: var(--secondary); color: #fff; }
        .btn-danger { background: var(--danger); color: #fff; }
        .btn:hover { transform: translateY(-1px); }
        
        .preset-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 8px;
            margin-bottom: 10px;
        }
        .preset-btn {
            padding: 12px;
            border: 2px solid var(--border);
            border-radius: 8px;
            background: var(--bg-dark);
            color: var(--text);
            cursor: pointer;
            text-align: center;
            transition: all 0.2s;
        }
        .preset-btn:hover { border-color: var(--primary); }
        .preset-btn.active {
            border-color: var(--secondary);
            background: rgba(35,134,54,0.1);
        }
        .preset-btn h4 { font-size: 13px; margin-bottom: 3px; }
        .preset-btn p { font-size: 11px; color: var(--text-muted); }
        
        .video-box {
            background: #000;
            border-radius: 10px;
            overflow: hidden;
            border: 2px solid var(--primary);
            display: none;
            margin-top: 15px;
        }
        .video-box.active { display: block; }
        video { width: 100%; height: auto; max-height: 400px; display: block; }
        
        .status-bar {
            display: flex;
            justify-content: space-between;
            padding: 8px 12px;
            background: rgba(0,0,0,0.8);
            font-size: 11px;
        }
        
        .hidden { display: none !important; }
        
        .sleep-warning {
            background: rgba(210,153,34,0.1);
            border: 1px solid var(--warning);
            padding: 10px;
            border-radius: 8px;
            margin-bottom: 15px;
            font-size: 12px;
        }
    </style>
</head>
<body>

<div class="container">
    <div class="header">
        <h1>📺 IPTV Render</h1>
        <p>يعمل على Render.com - موارد أفضل</p>
        <span class="render-badge">⚡ Render Free Tier</span>
    </div>
    
    <div class="sleep-warning">
        ⚠️ <strong>تنبيه:</strong> Render Free يدخل Sleep بعد 15 دقيقة. سأضيف Keep-Alive تلقائي.
    </div>

    <div class="card">
        <div class="card-title">📡 مصدر البث</div>
        
        <div class="form-group">
            <select id="sourceType" onchange="handleSourceChange()">
                <option value="single">🔗 رابط مباشر</option>
                <option value="m3u">📁 ملف M3U</option>
            </select>
        </div>
        
        <div id="singleArea">
            <input type="text" id="singleUrl" value="http://ugeen.live:8080/Ugeen_VIPmS3NcQ/qQPQWj/4540" placeholder="رابط البث">
        </div>
        
        <div id="m3uArea" class="hidden">
            <div style="display:flex; gap:8px; margin-bottom:8px;">
                <button class="btn btn-primary" onclick="switchM3uMethod('link')" style="flex:1;">🔗 رابط</button>
                <button class="btn btn-primary" onclick="switchM3uMethod('file')" style="flex:1;">📁 ملف</button>
            </div>
            <div id="m3uUrlInput"><input type="text" id="m3uUrl" placeholder="https://example.com/playlist.m3u"></div>
            <div id="m3uFileInput" class="hidden"><input type="file" id="m3uFile" accept=".m3u,.m3u8"></div>
            <button class="btn btn-primary" onclick="processM3U()" style="width:100%; margin-top:8px;">🔄 تحليل</button>
        </div>
    </div>

    <div class="card hidden" id="channelsCard">
        <div class="card-title">📋 القنوات</div>
        <input type="text" id="channelSearch" placeholder="🔍 بحث..." oninput="filterChannels()" style="margin-bottom:8px;">
        <div id="channelsList" style="max-height:250px; overflow-y:auto;"></div>
    </div>

    <div class="card">
        <div class="card-title">🎯 الجودة (Render أقوى!)</div>
        
        <div class="preset-grid">
            <div class="preset-btn active" onclick="selectPreset('low')" id="p-low">
                <h4>📉 منخفض</h4>
                <p>480p @ 800K<br>CPU ~40%</p>
            </div>
            
            <div class="preset-btn" onclick="selectPreset('medium')" id="p-medium">
                <h4>📊 متوسطة</h4>
                <p>720p @ 1500K<br>CPU ~60%</p>
            </div>
            
            <div class="preset-btn" onclick="selectPreset('high')" id="p-high">
                <h4>🎬 عالية</h4>
                <p>720p @ 2000K<br>CPU ~80%</p>
            </div>
            
            <div class="preset-btn" onclick="selectPreset('custom')" id="p-custom">
                <h4>⚙️ مخصص</h4>
                <p>تحكم يدوي</p>
            </div>
        </div>
        
        <div id="customSettings" class="hidden" style="margin-top:10px; padding:10px; background:var(--bg-dark); border-radius:6px;">
            <div class="form-group">
                <label>الدقة:</label>
                <select id="customRes">
                    <option value="640x360">360p</option>
                    <option value="854x480" selected>480p</option>
                    <option value="1280x720">720p</option>
                </select>
            </div>
            <div class="form-group">
                <label>معدل البت (Kbps):</label>
                <input type="number" id="customBitrate" value="800" min="300" max="3000" step="100">
            </div>
        </div>
    </div>

    <div class="card" style="text-align:center;">
        <button class="btn btn-success" onclick="startStream()" id="startBtn" style="font-size:16px; padding:14px 30px;">▶️ بدء البث</button>
        <button class="btn btn-danger" onclick="stopStream()" style="margin-right:10px;">⏹️ إيقاف</button>
    </div>

    <div class="video-box" id="videoBox">
        <div class="status-bar">
            <span id="statusText">🔴 جاري التحميل...</span>
            <span id="qualityText">480p</span>
        </div>
        <video id="player" controls autoplay playsinline></video>
    </div>
</div>

<script>
    let currentPreset = 'low';
    let currentM3uMethod = 'link';
    let allChannels = [];
    let selectedChannelUrl = "";
    let isPlaying = false;
    
    // Keep-Alive لمنع Sleep
    setInterval(() => {
        if (isPlaying) {
            fetch('/ping').catch(() => {});
        }
    }, 60000); // كل دقيقة
    
    function handleSourceChange() {
        const type = document.getElementById('sourceType').value;
        document.getElementById('singleArea').classList.toggle('hidden', type !== 'single');
        document.getElementById('m3uArea').classList.toggle('hidden', type !== 'm3u');
    }
    
    function switchM3uMethod(method) {
        currentM3uMethod = method;
        document.getElementById('m3uUrlInput').classList.toggle('hidden', method !== 'link');
        document.getElementById('m3uFileInput').classList.toggle('hidden', method !== 'file');
    }
    
    function processM3U() {
        if (currentM3uMethod === 'link') {
            const url = document.getElementById('m3uUrl').value.trim();
            if (!url) return;
            fetch('/parse_m3u_url?url=' + encodeURIComponent(url))
                .then(r => r.json())
                .then(data => showChannels(data));
        } else {
            const file = document.getElementById('m3uFile').files[0];
            if (!file) return;
            const formData = new FormData();
            formData.append('file', file);
            fetch('/parse_m3u_file', { method: 'POST', body: formData })
                .then(r => r.json())
                .then(data => showChannels(data));
        }
    }
    
    function showChannels(data) {
        allChannels = data || [];
        document.getElementById('channelsCard').classList.remove('hidden');
        filterChannels();
    }
    
    function filterChannels() {
        const query = document.getElementById('channelSearch').value.toLowerCase();
        const container = document.getElementById('channelsList');
        container.innerHTML = '';
        allChannels.forEach(ch => {
            if (ch.name.toLowerCase().includes(query)) {
                const item = document.createElement('div');
                item.style.cssText = 'padding:8px; cursor:pointer; border-bottom:1px solid var(--border);';
                item.innerHTML = `<span>${ch.name}</span>`;
                item.onclick = () => { selectedChannelUrl = ch.url; document.querySelectorAll('#channelsList div').forEach(el => el.style.background=''); item.style.background='rgba(35,134,54,0.2)'; };
                container.appendChild(item);
            }
        });
    }
    
    function selectPreset(p) {
        currentPreset = p;
        document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
        document.getElementById('p-' + p).classList.add('active');
        document.getElementById('customSettings').classList.toggle('hidden', p !== 'custom');
    }
    
    function getSettings() {
        const presets = {
            'low': { resolution: '854x480', v_bitrate: '800', a_bitrate: '96' },
            'medium': { resolution: '1280x720', v_bitrate: '1500', a_bitrate: '128' },
            'high': { resolution: '1280x720', v_bitrate: '2000', a_bitrate: '128' },
            'custom': { 
                resolution: document.getElementById('customRes').value,
                v_bitrate: document.getElementById('customBitrate').value,
                a_bitrate: '96'
            }
        };
        return presets[currentPreset];
    }
    
    async function startStream() {
        const type = document.getElementById('sourceType').value;
        let url = type === 'single' ? document.getElementById('singleUrl').value.trim() : selectedChannelUrl;
        if (!url) return alert('اختر قناة');
        
        const settings = getSettings();
        const params = new URLSearchParams({ url: url, ...settings });
        
        document.getElementById('videoBox').classList.add('active');
        document.getElementById('qualityText').textContent = settings.resolution;
        document.getElementById('statusText').textContent = '🔴 جاري التحميل...';
        
        const player = document.getElementById('player');
        player.src = '/video_feed?' + params.toString();
        player.load();
        player.play().catch(e => console.log('Autoplay blocked'));
        
        isPlaying = true;
    }
    
    function stopStream() {
        const player = document.getElementById('player');
        player.pause();
        player.src = '';
        document.getElementById('videoBox').classList.remove('active');
        isPlaying = false;
    }
    
    switchM3uMethod('link');
</script>

</body>
</html>
'''

@app.route('/')
def index():
    return render_template_string(HTML_INTERFACE)

@app.route('/ping')
def ping():
    """Keep-Alive endpoint"""
    return jsonify({"status": "alive"})

@app.route('/parse_m3u_url')
def parse_m3u_url():
    m3u_url = request.args.get('url', '')
    if not m3u_url:
        return jsonify([])
    try:
        response = requests.get(m3u_url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        channels = parse_m3u_content(response.text)
    except Exception as e:
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
        channels = []
    return jsonify(channels)

@app.route('/video_feed')
def video_feed():
    target_url = unquote(request.args.get('url', ''))
    resolution = request.args.get('resolution', '854x480')
    v_bitrate = request.args.get('v_bitrate', '800')
    a_bitrate = request.args.get('a_bitrate', '96')
    
    if not target_url:
        return "Missing URL", 400
    
    print(f"[STREAM] Render: {resolution} @ {v_bitrate}K")
    
    ffmpeg_cmd = [
        'ffmpeg',
        '-hide_banner',
        '-loglevel', 'error',
        '-fflags', '+discardcorrupt',
        '-reconnect', '1',
        '-reconnect_streamed', '1',
        '-reconnect_delay_max', '3',
        '-timeout', '5000000',
        '-thread_queue_size', '512',
        '-i', target_url,
        '-c:v', 'libx264',
        '-b:v', f'{v_bitrate}k',
        '-maxrate', f'{v_bitrate}k',
        '-bufsize', f'{int(v_bitrate)}k',
        '-s', resolution,
        '-r', '25',
        '-preset', 'ultrafast',
        '-tune', 'zerolatency',
        '-g', '100',
        '-keyint_min', '100',
        '-sc_threshold', '0',
        '-refs', '1',
        '-bf', '0',
        '-c:a', 'aac',
        '-b:a', f'{a_bitrate}k',
        '-ar', '44100',
        '-ac', '1',
        '-async', '1',
        '-vsync', 'cfr',
        '-max_muxing_queue_size', '512',
        '-f', 'mp4',
        '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
        '-'
    ]
    
    process = subprocess.Popen(
        ffmpeg_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    def generate():
        empty_count = 0
        max_empty = 300
        chunk_size = 8 * 1024
        
        try:
            while True:
                data = process.stdout.read(chunk_size)
                if not data:
                    empty_count += 1
                    if empty_count >= max_empty:
                        break
                    time.sleep(0.1)
                    continue
                
                empty_count = 0
                yield data
                
        except GeneratorExit:
            print("[STREAM] Client disconnected")
        except Exception as e:
            print(f"[STREAM] Error: {e}")
        finally:
            try:
                process.stdout.close()
            except: pass
            try:
                process.stderr.close()
            except: pass
            try:
                process.terminate()
                process.wait(timeout=2)
            except:
                try:
                    process.kill()
                    process.wait(timeout=1)
                except: pass
    
    return Response(
        generate(),
        mimetype='video/mp4',
        headers={
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
            'Connection': 'keep-alive'
        }
    )

if __name__ == '__main__':
    import os
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
