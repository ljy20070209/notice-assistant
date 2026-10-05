# -*- coding: utf-8 -*-
"""
通知助手·学生端 v2.0
==============================
在 v1 基础上改进：
【修复】
1. 冲突检测：加入/编辑日程时正确排除"自身"，不再出现"与自己冲突"的误报
2. 烟花特效：多轮多方向粒子爆发（含星星形状），CDN 加载失败时自动降级为 CSS 动画
3. 文本框：提交后自动清空（clear_on_submit）
4. 提示消息：加入日程后的"成功/冲突警告"不再被 st.rerun() 吞掉（改为 flash 机制）

【新增】
5. 支持粘贴链接抓取通知正文、上传文档（txt/docx/pdf）解析通知
6. 图片中的二维码自动识别并保留内容；通知原图保存到本地，查看详情时可回看
7. 日程支持编辑（✏️）、逾期标红、按时间排序、显示倒计时
8. "MM-DD" 这类无年份日期自动推断为最近未来的日期，不再落到 1900 年
9. API Key 优先从环境变量 / st.secrets 读取，避免硬编码泄露
"""
import streamlit as st
import json
import os
import re
import io
import time
import base64
import hashlib
import requests
from datetime import datetime, timedelta

# ================= 配置 =================
def _load_key():
    key = os.environ.get("ZHIPU_API_KEY", "")
    if not key:
        try:
            key = st.secrets.get("ZHIPU_API_KEY", "")
        except Exception:
            pass
    return key

ZHIPU_API_KEY = _load_key() or ""
SCHEDULE_FILE = "schedules.json"
READ_FILE = "read_flags.json"
IMG_DIR = "notice_images"          # 通知原图留存目录
os.makedirs(IMG_DIR, exist_ok=True)
LLM_MODEL = "glm-4-flash"
VISION_MODEL = "glm-4v-flash"

# ================= 文件工具 =================
def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def short_id(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]

# ================= 日程数据管理 =================
def get_schedules():
    data = load_json(SCHEDULE_FILE, [])
    if not isinstance(data, list):
        return []
    out, max_sid, changed = [], 0, False
    for s in data:
        if isinstance(s, dict) and "title" in s:
            s.setdefault("completed", False)
            if not isinstance(s.get("sid"), int):
                max_sid += 1
                s["sid"] = max_sid
                changed = True
            else:
                max_sid = max(max_sid, s["sid"])
            out.append(s)
    if changed:
        save_schedules(out)
    return out

def save_schedules(data):
    save_json(SCHEDULE_FILE, data)

def next_sid(schedules):
    ids = [s.get("sid") for s in schedules if isinstance(s.get("sid"), int)]
    return (max(ids) + 1) if ids else 1

def parse_time(t):
    """多格式时间解析。
    - 完整日期：2026-09-25 14:00 / 2026年9月25日 / 2026/09/25
    - 无年份：09-25 14:00 / 09-25 → 自动取最近的未来日期（避免落到1900年）
    - 仅时刻：14:00 → 今天该时刻，已过则明天
    """
    if not t:
        return None
    t = str(t).strip()
    t = t.replace("年", "-").replace("月", "-").replace("日", "")
    t = t.replace("/", "-").replace(".", "-")
    t = re.sub(r"\s+", " ", t).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    m = re.match(r"^(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?$", t)
    if m:
        mo, d, h, mi = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
        now = datetime.now()
        try:
            dt = datetime(now.year, mo, d, int(h or 0), int(mi or 0))
        except ValueError:
            return None
        if dt < now - timedelta(days=1):          # 已过去超过1天 → 推到明年
            try:
                dt = dt.replace(year=now.year + 1)
            except ValueError:
                return None
        return dt
    m = re.match(r"^(\d{1,2}):(\d{2})$", t)
    if m:
        now = datetime.now()
        dt = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if dt < now:
            dt += timedelta(days=1)
        return dt
    return None

def fmt_countdown(start_str):
    """返回 (状态文本, 是否已过期)"""
    dt = parse_time(start_str)
    if dt is None:
        return "", False
    now = datetime.now()
    if dt < now:
        return "⚠️ 已开始/已过期", True
    days = (dt.date() - now.date()).days
    if days == 0:
        return f"⏰ 今天 {dt.strftime('%H:%M')}", False
    if days == 1:
        return "⏰ 明天", False
    return f"📆 还有 {days} 天", False

def check_time_conflict(new_start, new_end, exclude_sid=None, exclude_title_start=None):
    """检测与已有【未完成】日程的重叠。
    - exclude_sid：编辑时排除自身
    - exclude_title_start：新增时排除"同标题+同起始"的自身
    时间无法解析时返回 []（不误报）。"""
    new_s = parse_time(new_start)
    if new_s is None:
        return []
    new_e = parse_time(new_end) if new_end else new_s
    if new_e is None or new_e < new_s:
        new_e = new_s
    conflicts = []
    for s in get_schedules():
        if s.get("completed"):
            continue
        if exclude_sid is not None and s.get("sid") == exclude_sid:
            continue
        if exclude_title_start is not None:
            title, start = exclude_title_start
            if s.get("title") == title and s.get("start") == start:
                continue
        s_s = parse_time(s.get("start"))
        if s_s is None:
            continue
        s_e = parse_time(s.get("end")) if s.get("end") else s_s
        if s_e is None or s_e < s_s:
            s_e = s_s
        if new_s < s_e and s_s < new_e:      # 半开区间重叠
            conflicts.append((s.get("title", ""), s.get("start", ""), s.get("end", "")))
    return conflicts

def add_schedule(title, start, end, loc):
    scheds = get_schedules()
    for s in scheds:                          # 完全重复才禁止
        if s.get("title") == title and s.get("start") == start:
            return False, "该日程已存在（同标题 + 同起始时间），未重复添加"
    conflicts = check_time_conflict(start, end, exclude_title_start=(title, start))
    warning_msg = ""
    if conflicts:
        names = "、".join(c[0] for c in conflicts)
        detail = "\n".join(f"· 「{c[0]}」{c[1]} ~ {c[2]}" for c in conflicts)
        warning_msg = f"⚠️ 与已有日程时间重叠：{names}\n{detail}\n（已强制添加）"
    scheds.append({"sid": next_sid(scheds), "title": title, "start": start,
                   "end": end, "loc": loc, "completed": False})
    save_schedules(scheds)
    return True, warning_msg

def update_schedule(sid, title, start, end, loc):
    scheds = get_schedules()
    for s in scheds:
        if s.get("sid") == sid:
            s.update({"title": title, "start": start, "end": end, "loc": loc})
            save_schedules(scheds)
            return True
    return False

def toggle_schedule(sid):
    scheds = get_schedules()
    for s in scheds:
        if s.get("sid") == sid:
            s["completed"] = not s.get("completed", False)
            save_schedules(scheds)
            return s["completed"]
    return None

def delete_schedule(sid):
    save_schedules([s for s in get_schedules() if s.get("sid") != sid])

# ================= 已读记录管理 =================
def get_read_flags():
    return load_json(READ_FILE, {})

def save_read_flags(data):
    save_json(READ_FILE, data)

def upsert_read(key, text, res, img_path=None, qr_list=None):
    rf = get_read_flags()
    rf[key] = {"text": text, "res": res, "read": True,
               "end": (res or {}).get("截止时间", ""),
               "img": img_path, "qr": qr_list or []}
    save_read_flags(rf)


# ================= 非图片输入：链接 / 文档 =================
def fetch_url_text(url):
    """抓取网页正文（去标签），适用于通知链接。失败抛异常由调用方处理。"""
    if not re.match(r"^https?://", url):
        url = "https://" + url
    r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    r.encoding = r.apparent_encoding or r.encoding
    html = r.text
    html = re.sub(r"(?is)<script.*?</script>|<style.*?</style>|<!--.*?-->", " ", html)
    text = re.sub(r"(?s)<[^>]+>", "\n", html)
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()[:6000]

def extract_docx(data):
    """用标准库解析 docx 正文（无需第三方依赖）"""
    import zipfile
    from xml.etree import ElementTree as ET
    z = zipfile.ZipFile(io.BytesIO(data))
    xml = z.read("word/document.xml")
    ns_t = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
    ns_p = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
    tree = ET.fromstring(xml)
    paras = ["".join(t.text or "" for t in p.iter(ns_t)) for p in tree.iter(ns_p)]
    return "\n".join(p for p in paras if p.strip())

def extract_pdf(data):
    """解析 PDF；未安装 pypdf 时返回 None"""
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            return None
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)

def extract_document(file_bytes, filename):
    name = filename.lower()
    if name.endswith(".txt"):
        return file_bytes.decode("utf-8", errors="ignore")
    if name.endswith(".docx"):
        return extract_docx(file_bytes)
    if name.endswith(".pdf"):
        return extract_pdf(file_bytes)
    return None

def detect_qr(image_bytes):
    """识别图片中的二维码，返回内容列表（没装 opencv 时返回 []）"""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return []
    try:
        arr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return []
        det = cv2.QRCodeDetector()
        ok, decoded, _, _ = det.detectAndDecodeMulti(img)
        if ok:
            return [d for d in decoded if d]
        data, _, _ = det.detectAndDecode(img)
        return [data] if data else []
    except Exception:
        return []

# ================= AI 能力 =================
def extract_json(text):
    if not text:
        return None
    text = str(text).strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e > s:
        try:
            return json.loads(text[s:e + 1])
        except Exception:
            return None
    return None

FIELDS = ("适用对象、核心动作、起始时间、截止时间、涉及系统/地点、所需材料、补充提醒、白话版")

def _no_key_result():
    return {"适用对象": "未配置Key", "核心动作": "请配置Key", "起始时间": "", "截止时间": "",
            "涉及系统/地点": "", "所需材料": "", "补充提醒": "", "白话版": "请通过环境变量 ZHIPU_API_KEY 或 st.secrets 配置真实智谱API Key"}

def analyze_notice(text):
    """智谱 GLM 文本解析通知"""
    if not ZHIPU_API_KEY or "你的智谱" in ZHIPU_API_KEY:
        return _no_key_result()
    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    headers = {"Authorization": f"Bearer {ZHIPU_API_KEY}", "Content-Type": "application/json"}
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = f"""你是学校通知解析助手。请提取通知中的关键要素，只返回严格 JSON，禁止任何多余文字、解释或 markdown 代码块标记。
提取规则：
1. 日期时间统一写成「YYYY-MM-DD HH:MM」（如 2026-09-25 14:00）；只有日期没有时间则写「YYYY-MM-DD」。
2. 原文中的相对时间（如"明天""下周一""10号"）请结合今天日期 {today} 换算成具体日期。
3. 所有字段必须来自原文，原文没有提到的字段填空字符串，禁止编造。
4. 白话版用轻松口语，说清「要做什么 + 截止时间 + 地点/系统」，不超过 60 字。
必须包含的字段：{FIELDS}
通知内容：
{text}"""
    data = {"model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1}
    for i in range(2):
        try:
            r = requests.post(url, headers=headers, json=data, timeout=15)
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                obj = extract_json(content)
                if obj is not None:
                    return obj
                if i < 1:
                    data["messages"].append({"role": "assistant", "content": content})
                    data["messages"].append({"role": "user", "content": "只输出严格 JSON，不要任何其他内容。"})
                    continue
            elif i == 1:
                return {"核心动作": "解析失败", "白话版": f"接口返回异常：{r.status_code}"}
        except Exception as e:
            if i == 1:
                return {"核心动作": "解析失败", "白话版": f"调用失败：{str(e)[:80]}"}
        time.sleep(0.5)
    return {"核心动作": "解析失败", "白话版": "大模型返回异常"}

def analyze_image_notice(image_bytes):
    """图片识别+解析一步完成，返回 (原文, 解析JSON, 二维码列表)"""
    if not ZHIPU_API_KEY or "你的智谱" in ZHIPU_API_KEY:
        return "", _no_key_result(), []
    qr_list = detect_qr(image_bytes)
    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    headers = {"Authorization": f"Bearer {ZHIPU_API_KEY}", "Content-Type": "application/json"}
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    today = datetime.now().strftime("%Y-%m-%d")
    payload = {
        "model": VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": f"""这是一张学校通知的截图，请完成两个任务：
1. 先完整识别图中所有文字，保持原文段落顺序。
2. 再从通知文字中提取关键要素，返回严格JSON格式。
提取规则：
- 日期时间统一写成「YYYY-MM-DD HH:MM」；相对时间结合今天日期 {today} 换算。
- 字段：{FIELDS}
- 白话版用口语化表达，不超过60字。
请按以下格式返回，不要多余内容：
【原文】
(识别出的完整原文)
【JSON】
(严格的JSON对象，不要代码块)"""},
            ],
        }],
        "temperature": 0.1,
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code == 200:
            content = r.json()["choices"][0]["message"]["content"].strip()
            text_part, json_part = "", None
            if "【原文】" in content and "【JSON】" in content:
                text_part = content.split("【原文】")[1].split("【JSON】")[0].strip()
                json_part = extract_json(content.split("【JSON】")[1].strip())
            if json_part is None:
                json_part = extract_json(content)
                text_part = content.replace("```json", "").replace("```", "").strip()
            return text_part, (json_part or {"核心动作": "解析失败", "白话版": "图片解析失败"}), qr_list
        return "", {"核心动作": "识别失败", "白话版": f"接口返回异常：{r.status_code}"}, qr_list
    except Exception as e:
        return "", {"核心动作": "识别失败", "白话版": f"调用失败：{str(e)[:80]}"}, qr_list

# ================= 烟花特效（增强版 + CSS 降级 + 全屏覆盖修复） =================
# 【关键修复】st.components.v1.html 渲染出的 iframe 默认只有设置的高度（以前设了0），
# confetti 画布画在 iframe 内部，0 高度视口里什么都看不见。
# 解法：iframe 占位高度设 0（父容器自动折叠不留空白），再用 CSS 把 srcdoc 里
# 带有 KIMI_CONFETTI_OVERLAY 标记的 iframe 放大到全屏 fixed 覆盖层，透明且鼠标可穿透。
OVERLAY_CSS = """
<style>
iframe[srcdoc*="KIMI_CONFETTI_OVERLAY"] {
    position: fixed !important;
    top: 0 !important;
    left: 0 !important;
    width: 100vw !important;
    height: 100vh !important;
    z-index: 999999 !important;
    border: none !important;
    background: transparent !important;
    pointer-events: none !important;
}
</style>
"""

CONFETTI_HTML = """
<!--KIMI_CONFETTI_OVERLAY-->
<style>html,body{margin:0;padding:0;background:transparent;overflow:hidden;}</style>
<script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.9.3/dist/confetti.browser.min.js"></script>
<script>
(function () {
    var colors = ['#ff6b6b', '#4ecdc4', '#ffe66d', '#45b7d1', '#ff9f43', '#a8e6cf', '#c780fa'];
    function cssFallback() {
        // CDN 不可用时的 CSS 降级方案：生成 40 片彩色纸屑从上往下飘落
        for (var i = 0; i < 40; i++) {
            var p = document.createElement('div');
            var size = 6 + Math.random() * 8;
            p.style.cssText = 'position:fixed;top:-20px;left:' + (Math.random() * 100) +
                'vw;width:' + size + 'px;height:' + size + 'px;background:' +
                colors[i % colors.length] + ';z-index:9999;border-radius:' +
                (Math.random() > 0.5 ? '50%' : '2px') +
                ';animation:fall ' + (2 + Math.random() * 1.5) + 's linear ' +
                (Math.random() * 0.8) + 's forwards;pointer-events:none;';
            document.body.appendChild(p);
        }
        var st = document.createElement('style');
        st.textContent = '@keyframes fall{to{transform:translateY(110vh) rotate(' +
            (360 + Math.random() * 720) + 'deg);opacity:0.6;}}';
        document.head.appendChild(st);
        setTimeout(function(){ document.querySelectorAll('div[style*="fall"]').forEach(function(e){e.remove();}); }, 5000);
    }
    function boom() {
        if (typeof confetti === 'undefined') { cssFallback(); return; }
        confetti({ particleCount: 180, spread: 120, startVelocity: 45,
                   origin: { x: 0.5, y: 0.6 }, colors: colors, ticks: 220, gravity: 1.0, scalar: 1.1 });
        setTimeout(function () {   // 左侧彩带炮
            confetti({ particleCount: 90, angle: 60, spread: 60, startVelocity: 55,
                       origin: { x: 0, y: 0.75 }, colors: colors });
        }, 200);
        setTimeout(function () {   // 右侧彩带炮
            confetti({ particleCount: 90, angle: 120, spread: 60, startVelocity: 55,
                       origin: { x: 1, y: 0.75 }, colors: colors });
        }, 380);
        setTimeout(function () {   // 金色星星收尾
            confetti({ particleCount: 70, spread: 130, startVelocity: 32,
                       origin: { x: 0.5, y: 0.35 }, shapes: ['star'],
                       colors: ['#ffd700', '#ffec8b', '#ffffff'], scalar: 1.5, ticks: 260 });
        }, 550);
    }
    if (typeof confetti === 'undefined') {
        // 等待 CDN 加载，最多等 1.5 秒
        var waited = 0;
        var timer = setInterval(function () {
            waited += 100;
            if (typeof confetti !== 'undefined' || waited >= 1500) {
                clearInterval(timer);
                boom();
            }
        }, 100);
    } else {
        boom();
    }
})();
</script>
"""

def fire_confetti():
    # 占位高度给 0：父容器折叠不留空白，实际显示尺寸由 OVERLAY_CSS 控制为全屏
    st.components.v1.html(CONFETTI_HTML, height=0, width=0)


# ================= 页面 =================
def flash(level, msg):
    """设置一次性提示消息（rerun 后仍能显示）"""
    st.session_state["_flash"] = (level, msg)

def show_flash():
    if "_flash" in st.session_state:
        level, msg = st.session_state.pop("_flash")
        if level == "success":
            st.success(msg)
        elif level == "warning":
            st.warning(msg)
        elif level == "error":
            st.error(msg)
        else:
            st.info(msg)

def save_uploaded_image(file_bytes, ext=".png"):
    fname = short_id(str(time.time()) + str(len(file_bytes))) + ext
    path = os.path.join(IMG_DIR, fname)
    with open(path, "wb") as f:
        f.write(file_bytes)
    return path

def main():
    st.set_page_config(
    page_title="通知助手·学生端",
    page_icon="📢",
    layout="centered",
    initial_sidebar_state="collapsed"
)

    # 注入覆盖层 CSS：让烟花 iframe 全屏显示（必须每次运行都注入，保证样式常在对页面无副作用）
    st.markdown(OVERLAY_CSS, unsafe_allow_html=True)

    # 顶部：烟花 + 一次性消息
    if st.session_state.pop("play_confetti", False):
        fire_confetti()
        st.toast("🎉 操作成功！")
    show_flash()

    if "view_item" not in st.session_state:
        st.session_state.view_item = None
    if "pending_text" not in st.session_state:
        st.session_state.pending_text = ""
    if "img_uploader_key" not in st.session_state:
        st.session_state.img_uploader_key = 0
    if "doc_uploader_key" not in st.session_state:
        st.session_state.doc_uploader_key = 0

    col_left, col_right = st.columns([1, 1.6])

    # ================= 左栏 =================
    with col_left:
        st.markdown("## ✏️ 输入通知")
        tab_text, tab_img, tab_link, tab_doc = st.tabs(
            ["📝 文本", "🖼️ 图片", "🔗 链接", "📎 文档"])

        # ---- 文本 ----
        with tab_text:
            with st.form("notice_form", clear_on_submit=True):   # 提交后自动清空
                txt_in = st.text_area("粘贴通知内容...", height=120)
                st.caption("💡 输入完成后按 `Ctrl+Enter` 即可分析")
                submitted = st.form_submit_button("🔍 分析通知", type="primary", use_container_width=True)
            if submitted and txt_in.strip():
                st.session_state.pending_text = txt_in
                st.rerun()

        # ---- 图片（OCR+解析+二维码，一步到位）----
        with tab_img:
            uploaded = st.file_uploader(
                "上传通知截图（JPG/PNG）",
                type=["jpg", "jpeg", "png"],
                key=f"img_up_{st.session_state.img_uploader_key}")
            if st.button("📷 识别并分析", type="primary", use_container_width=True):
                if uploaded is None:
                    st.warning("请先上传图片")
                else:
                    file_bytes = uploaded.read()
                    with st.spinner("正在识别并解析图片..."):
                        ocr_text, res, qr_list = analyze_image_notice(file_bytes)
                    if not ocr_text and res.get("核心动作") == "识别失败":
                        flash("error", res.get("白话版", "识别失败"))
                    else:
                        img_path = save_uploaded_image(file_bytes, os.path.splitext(uploaded.name)[1] or ".png")
                        key = ocr_text.strip()[:200] if ocr_text.strip() else f"图片通知_{int(time.time())}"
                        upsert_read(key, ocr_text, res, img_path=img_path, qr_list=qr_list)
                        st.session_state.view_item = key
                        st.session_state.play_confetti = True
                        st.session_state.img_uploader_key += 1   # 重置上传框
                        st.rerun()

        # ---- 链接 ----
        with tab_link:
            url_in = st.text_input("粘贴通知链接（公众号文章、通知页等）", placeholder="https://...")
            if st.button("🌐 抓取并分析", type="primary", use_container_width=True):
                if not url_in.strip():
                    st.warning("请先粘贴链接")
                else:
                    with st.spinner("正在抓取网页内容..."):
                        try:
                            page_text = fetch_url_text(url_in.strip())
                        except Exception as e:
                            page_text = ""
                            flash("error", f"链接抓取失败：{str(e)[:80]}")
                    if page_text:
                        with st.spinner("正在调用大模型分析..."):
                            res = analyze_notice(page_text)
                        upsert_read(url_in.strip(), page_text, res)
                        st.session_state.view_item = url_in.strip()
                        st.session_state.play_confetti = True
                        st.rerun()

        # ---- 文档 ----
        with tab_doc:
            doc_up = st.file_uploader("上传通知文件（txt / docx / pdf）",
                                      type=["txt", "docx", "pdf"],
                                      key=f"doc_up_{st.session_state.doc_uploader_key}")
            if st.button("📄 解析并分析", type="primary", use_container_width=True):
                if doc_up is None:
                    st.warning("请先上传文件")
                else:
                    file_bytes = doc_up.read()
                    doc_text = extract_document(file_bytes, doc_up.name)
                    if doc_text is None:
                        flash("error", "PDF 解析需要安装 pypdf：pip install pypdf")
                    elif not doc_text.strip():
                        flash("warning", "未能从文件中提取到文字（可能是扫描件，请改用图片识别）")
                    else:
                        with st.spinner("正在调用大模型分析..."):
                            res = analyze_notice(doc_text)
                        upsert_read(f"文档_{doc_up.name}_{int(time.time())}", doc_text, res)
                        st.session_state.view_item = f"文档_{doc_up.name}_{int(time.time())}"
                        st.session_state.play_confetti = True
                        st.session_state.doc_uploader_key += 1
                        st.rerun()

        # ================= 待办日程 =================
        st.markdown("## 📅 待办日程")
        all_scheds = get_schedules()
        pending = sorted([s for s in all_scheds if not s.get("completed", False)],
                         key=lambda s: (parse_time(s.get("start")) is None, parse_time(s.get("start")) or datetime.max))
        completed = [s for s in all_scheds if s.get("completed", False)]

        if not all_scheds:
            st.caption("暂无日程")

        if pending:
            st.caption("⏳ 待办中")
            for s in pending:
                sid = s.get("sid")
                cd_text, overdue = fmt_countdown(s.get("start"))
                c1, c2, c3, c4 = st.columns([0.1, 0.62, 0.16, 0.12])
                with c1:
                    checked = st.checkbox("完成", key=f"sch_cb_{sid}", label_visibility="collapsed")
                with c2:
                    st.markdown(f"**{s['title']}**")
                    loc = s.get("loc") or ""
                    cap = f"{s.get('start','')} ~ {s.get('end','')}" + (f" | {loc}" if loc else "")
                    if overdue:
                        st.caption(f":red[{cap}　{cd_text}]")
                    else:
                        st.caption(f"{cap}　{cd_text}")
                with c3:
                    if st.button("✏️ 编辑", key=f"sch_edit_{sid}"):
                        st.session_state.editing_sid = sid
                        st.rerun()
                with c4:
                    if st.button("🗑", key=f"sch_del_{sid}"):
                        delete_schedule(sid)
                        flash("info", "日程已删除")
                        st.rerun()
                if checked:
                    toggle_schedule(sid)
                    st.session_state.pop(f"sch_cb_{sid}", None)
                    st.session_state.play_confetti = True
                    st.rerun()

        # ---- 编辑表单 ----
        editing_sid = st.session_state.get("editing_sid")
        if editing_sid:
            target = next((s for s in all_scheds if s.get("sid") == editing_sid), None)
            if target is None:
                st.session_state.editing_sid = None
            else:
                st.markdown("---")
                with st.form("edit_schedule_form"):
                    st.markdown("##### ✏️ 编辑日程")
                    et = st.text_input("标题", value=target.get("title", ""))
                    ec1, ec2 = st.columns(2)
                    es = ec1.text_input("起始", value=target.get("start", ""))
                    ee = ec2.text_input("截止", value=target.get("end", ""))
                    el = st.text_input("地点/系统", value=target.get("loc", ""))
                    bc1, bc2 = st.columns(2)
                    save_btn = bc1.form_submit_button("💾 保存", type="primary", use_container_width=True)
                    cancel_btn = bc2.form_submit_button("取消", use_container_width=True)
                if cancel_btn:
                    st.session_state.editing_sid = None
                    st.rerun()
                if save_btn:
                    if not et.strip() or not es.strip():
                        flash("error", "标题和起始时间不能为空")
                    else:
                        s_dt, e_dt = parse_time(es.strip()), parse_time(ee.strip())
                        if e_dt and s_dt and e_dt < s_dt:
                            flash("error", "截止时间早于起始时间，请检查")
                        else:
                            conf = check_time_conflict(es.strip(), ee.strip(), exclude_sid=editing_sid)
                            update_schedule(editing_sid, et.strip(), es.strip(), ee.strip(), el.strip())
                            st.session_state.editing_sid = None
                            if conf:
                                names = "、".join(c[0] for c in conf)
                                flash("warning", f"已保存，但请注意：与「{names}」时间重叠")
                            else:
                                flash("success", "✅ 日程已更新")
                            st.rerun()

        if completed:
            st.markdown("---")
            st.caption("✅ 已完成")
            for s in completed:
                sid = s.get("sid")
                c1, c2, c3 = st.columns([0.1, 0.75, 0.15])
                with c1:
                    checked = st.checkbox("恢复", key=f"sch_done_{sid}", value=True, label_visibility="collapsed")
                with c2:
                    st.markdown(f"~~{s['title']}~~")
                    st.caption(f":gray[{s.get('start','')} ~ {s.get('end','')}]")
                with c3:
                    if st.button("🗑", key=f"sch_del_done_{sid}"):
                        delete_schedule(sid)
                        flash("info", "日程已删除")
                        st.rerun()
                if not checked:
                    toggle_schedule(sid)
                    st.session_state.pop(f"sch_done_{sid}", None)
                    st.rerun()

        # ================= 已读记录 =================
        st.markdown("## ✅ 已读记录")
        read_flags = get_read_flags()
        if not read_flags:
            st.caption("暂无已读记录")
        for idx, (k, v) in enumerate(list(read_flags.items())):
            if not isinstance(v, dict):
                continue
            is_read = bool(v.get("read", True))
            kid = short_id(k)
            c1, c2, c3 = st.columns([0.1, 0.75, 0.15])
            with c1:
                checked = st.checkbox("已读", value=is_read, key=f"cb_r_{kid}", label_visibility="collapsed")
            with c2:
                label = k[:15] + "..." if len(k) > 15 else k
                if not is_read:
                    label = f"**{label}**"
                if st.button(label, key=f"view_{kid}"):
                    st.session_state.view_item = k
                    st.rerun()
            with c3:
                if st.button("🗑", key=f"del_r_{kid}"):
                    del read_flags[k]
                    save_read_flags(read_flags)
                    if st.session_state.view_item == k:
                        st.session_state.view_item = None
                    flash("info", "记录已删除")
                    st.rerun()
            if checked != is_read:
                read_flags[k]["read"] = checked
                save_read_flags(read_flags)
                st.rerun()

    # ================= 右栏 =================
    with col_right:
        st.markdown("## 📢 通知助手·学生端")
        st.caption("粘贴一条学校通知，自动提取关键要素，帮你快速看懂并安排进日程")

        # 文本输入的分析流程
        if st.session_state.pending_text:
            txt = st.session_state.pending_text
            with st.spinner("正在调用智谱大模型分析..."):
                res = analyze_notice(txt)
            upsert_read(txt.strip(), txt, res)
            st.session_state.view_item = txt.strip()
            st.session_state.pending_text = ""
            st.rerun()

        view_key = st.session_state.view_item
        rf_now = get_read_flags()
        txt, res = "", {}
        item = rf_now.get(view_key) if view_key else None
        if item:
            txt = item.get("text", "")
            res = item.get("res", {})
            st.info("📂 正在查看历史已读通知")
        else:
            st.info("在左侧输入通知并点击分析")
            st.stop()

        kid = short_id(view_key)
        res = res or {}
        res.setdefault("白话版", "")

        # ---- 关键要素 ----
        st.markdown("### 📋 关键要素")
        c1, c2 = st.columns(2)
        def show(col, t, val):
            col.markdown(f"**{t}**：{val or '未提及'}")
        with c1:
            show(c1, "适用对象", res.get("适用对象"))
            show(c1, "起始时间", res.get("起始时间"))
            show(c1, "涉及系统/地点", res.get("涉及系统/地点"))
            show(c1, "补充提醒", res.get("补充提醒"))
        with c2:
            show(c2, "核心动作", res.get("核心动作"))
            show(c2, "截止时间", res.get("截止时间"))
            show(c2, "所需材料", res.get("所需材料"))

        with st.expander("🗣 白话版通知（一键看懂）", expanded=True, key=f"exp_bh_{kid}"):
            st.write(res.get("白话版", ""))
        with st.expander("📄 原通知全文", expanded=False, key=f"exp_orig_{kid}"):
            st.text_area("", txt, height=150, label_visibility="collapsed", key=f"orig_{kid}")

        # ---- 通知原图回看（图片输入时保留）----
        img_path = item.get("img")
        if img_path and os.path.exists(img_path):
            with st.expander("🖼️ 通知原图（已留存）", expanded=False, key=f"exp_img_{kid}"):
                st.image(img_path, use_container_width=True)

        # ---- 二维码内容保留 ----
        qr_list = item.get("qr") or []
        if qr_list:
            with st.expander(f"🔳 识别到 {len(qr_list)} 个二维码（内容已保留）", expanded=True, key=f"exp_qr_{kid}"):
                for i, q in enumerate(qr_list):
                    st.code(q, language=None)
                    st.caption("👆 长按/全选复制，微信等 App 中识别加群")

        # ---- 加入个人日程 ----
        st.markdown("### ➕ 加入个人日程")
        title_in = st.text_input("标题", value=res.get("核心动作", "未命名日程"), key=f"t_{kid}")
        cs, ce = st.columns(2)
        with cs:
            start_in = st.text_input("起始", value=res.get("起始时间", ""), key=f"s_{kid}")
        with ce:
            end_in = st.text_input("截止", value=res.get("截止时间", ""), key=f"e_{kid}")
        loc_in = st.text_input("地点/系统", value=res.get("涉及系统/地点", ""), key=f"l_{kid}")

        # 实时冲突预览（排除自身，仅提醒不阻止）
        if start_in.strip() and title_in.strip():
            live = check_time_conflict(start_in.strip(), end_in.strip(),
                                       exclude_title_start=(title_in.strip(), start_in.strip()))
            for c_title, c_start, c_end in live:
                st.warning(f"⚠️ 与已有日程「{c_title}」（{c_start} ~ {c_end}）时间重叠")

        if st.button("加入日程", use_container_width=True, type="primary"):
            if not title_in.strip() or not start_in.strip():
                flash("error", "标题和起始时间不能为空")
            else:
                s_dt, e_dt = parse_time(start_in.strip()), parse_time(end_in.strip())
                if e_dt and s_dt and e_dt < s_dt:
                    flash("error", "截止时间早于起始时间，请检查")
                else:
                    ok, msg = add_schedule(title_in.strip(), start_in.strip(), end_in.strip(), loc_in.strip())
                    if ok:
                        st.session_state.play_confetti = True
                        if msg:
                            flash("warning", msg)
                        else:
                            flash("success", "✅ 已加入待办！")
                    else:
                        flash("error", f"❌ {msg}")
                st.rerun()

if __name__ == "__main__":
    main()