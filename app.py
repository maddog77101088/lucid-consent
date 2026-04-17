"""루시드 동물병원 수술동의서 자동화 시스템."""
import os
import json
import base64
import sqlite3
from datetime import datetime
from functools import wraps
from flask import (
    Flask, render_template, render_template_string, request, redirect, url_for,
    session, jsonify, g, flash, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from default_templates import DEFAULT_HEADER, DEFAULT_DISCLAIMER, DEFAULT_FOOTER
try:
    from default_templates import DEFAULT_YOUTUBE_URL
except ImportError:
    DEFAULT_YOUTUBE_URL = ""

try:
    import requests
except ImportError:
    requests = None

try:
    import qrcode
    import io
except ImportError:
    qrcode = None


def _qr_base64(url):
    """URL을 QR PNG base64 문자열로."""
    if not qrcode or not url:
        return ""
    try:
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return ""

# .env 파일 자동 로드 (key=value 형식)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            _k = _k.strip()
            _v = _v.strip().strip('"').strip("'")
            if _k and _v and _k not in os.environ:
                os.environ[_k] = _v

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "lucid-dev-secret-change-in-prod")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "lucid.db"))

HOSPITAL_NAME = "24시루시드동물메디컬센터"
HOSPITAL_SHORT = "루시드 동물병원"
CATEGORIES = ["일반외과", "정형외과", "연부조직외과", "응급·기타"]
HOSP_CATEGORIES = ["내과", "외과 회복", "중환자·응급", "감염·예방", "기타"]


def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = g._db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_db(_=None):
    db = getattr(g, "_db", None)
    if db:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'vet',
            must_change_password INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS surgeries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL, category TEXT NOT NULL,
            purpose_effect TEXT, procedure TEXT, complications TEXT,
            anesthesia_risk TEXT, estimated_cost TEXT, hospitalization TEXT,
            expected_duration TEXT, notes TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS hospitalizations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            category TEXT NOT NULL,
            purpose_effect TEXT,
            complications TEXT,
            estimated_cost TEXT,
            hospitalization TEXT,
            expected_duration TEXT,
            notes TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS hospital_template (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            header_html TEXT, disclaimer_html TEXT, footer_html TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    # 기존 DB 마이그레이션: youtube_url 컬럼 추가 + 빈 값이면 기본값으로 채우기
    cur.execute("PRAGMA table_info(hospital_template)")
    cols = [c[1] for c in cur.fetchall()]
    if "youtube_url" not in cols:
        cur.execute("ALTER TABLE hospital_template ADD COLUMN youtube_url TEXT DEFAULT ''")
    # 기존 row가 있고 URL이 비어있으면 기본값 주입
    cur.execute("UPDATE hospital_template SET youtube_url=? WHERE id=1 AND (youtube_url IS NULL OR youtube_url='')",
                (DEFAULT_YOUTUBE_URL,))
    # users 테이블 마이그레이션: must_change_password 컬럼
    cur.execute("PRAGMA table_info(users)")
    ucols = [c[1] for c in cur.fetchall()]
    if "must_change_password" not in ucols:
        cur.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
    con.commit()
    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        for u, d, r in [("admin","관리자","admin"),("seolwon","설원장","vet"),("nowon","노진희원장","vet")]:
            cur.execute("INSERT INTO users (username,password_hash,display_name,role,must_change_password) VALUES (?,?,?,?,1)",
                (u, generate_password_hash("lucid1234"), d, r))
    cur.execute("SELECT COUNT(*) FROM hospital_template")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO hospital_template (id,header_html,disclaimer_html,footer_html,youtube_url) VALUES (1,?,?,?,?)",
            (DEFAULT_HEADER, DEFAULT_DISCLAIMER, DEFAULT_FOOTER, DEFAULT_YOUTUBE_URL))
    else:
        # 자동 마이그레이션: 기존에 저장된 구버전 헤더를 새 버전(doc_title 변수화)으로 교체
        # 사용자가 직접 수정한 흔적이 없으면(기존 하드코딩 제목/안내문 그대로면) 덮어쓴다.
        cur.execute("SELECT header_html FROM hospital_template WHERE id=1")
        row = cur.fetchone()
        if row and row[0]:
            hh = row[0]
            if ("수술 및 입원 동의서" in hh and "{{ doc_title }}" not in hh) or \
               ("본 동의서는 차트에 저장되며" in hh):
                cur.execute("UPDATE hospital_template SET header_html=? WHERE id=1", (DEFAULT_HEADER,))
    cur.execute("SELECT COUNT(*) FROM surgeries")
    if cur.fetchone()[0] == 0:
        from seed_data import SEED_SURGERIES
        for s in SEED_SURGERIES:
            cur.execute("""INSERT INTO surgeries
                (name,category,purpose_effect,procedure,complications,anesthesia_risk,
                 estimated_cost,hospitalization,expected_duration,notes)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (s["name"], s["category"], s.get("purpose_effect"), s.get("procedure"),
                 s.get("complications"), s.get("anesthesia_risk"), s.get("estimated_cost"),
                 s.get("hospitalization"), s.get("expected_duration"), s.get("notes")))
    con.commit()
    con.close()


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        # 비번 강제 변경이 필요한 경우(/me/password 제외) 우회
        if session.get("must_change_password") and request.endpoint not in ("change_password", "logout", "static"):
            return redirect(url_for("change_password"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("role") != "admin":
            flash("관리자만 접근 가능합니다.", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapper


# ----- 비밀번호 정책 -----
COMMON_PASSWORDS = {
    "lucid1234", "password", "password1", "12345678", "qwerty123", "qwerty1234",
    "admin1234", "1q2w3e4r", "abcd1234", "letmein123", "iloveyou1", "1234567890",
    "lucidanimal", "hospital1234", "seoul1234", "강북1234",
}

def validate_password(pw, username="", display_name=""):
    """반환: (ok: bool, msg: str)"""
    if not isinstance(pw, str):
        return False, "비밀번호 형식이 올바르지 않습니다."
    if len(pw) < 10:
        return False, "비밀번호는 최소 10자 이상이어야 합니다."
    if len(pw) > 128:
        return False, "비밀번호가 너무 깁니다. (최대 128자)"
    classes = 0
    if any(c.islower() for c in pw): classes += 1
    if any(c.isupper() for c in pw): classes += 1
    if any(c.isdigit() for c in pw): classes += 1
    if any(not c.isalnum() for c in pw): classes += 1
    if classes < 3:
        return False, "영문 대문자·소문자·숫자·특수문자 중 3종류 이상 포함해야 합니다."
    low = pw.lower()
    if username and username.lower() in low:
        return False, "비밀번호에 아이디를 포함할 수 없습니다."
    if display_name and display_name.lower() in low:
        return False, "비밀번호에 이름을 포함할 수 없습니다."
    if low in COMMON_PASSWORDS:
        return False, "너무 흔한 비밀번호입니다. 다른 비밀번호를 사용해주세요."
    return True, ""


@app.context_processor
def inject_user():
    return {
        "current_user": {"id": session.get("user_id"), "name": session.get("display_name"), "role": session.get("role")},
        "hospital_name": HOSPITAL_NAME, "hospital_short": HOSPITAL_SHORT, "categories": CATEGORIES,
    }


@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username","").strip()
        p = request.form.get("password","")
        row = get_db().execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
        if row and check_password_hash(row["password_hash"], p):
            session["user_id"] = row["id"]; session["display_name"] = row["display_name"]
            session["role"] = row["role"]; session["username"] = row["username"]
            session["must_change_password"] = bool(row["must_change_password"]) if "must_change_password" in row.keys() else False
            if session["must_change_password"]:
                flash("보안을 위해 비밀번호를 변경해주세요.", "ok")
                return redirect(url_for("change_password"))
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("아이디 또는 비밀번호가 올바르지 않습니다.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/me/password", methods=["GET","POST"])
def change_password():
    if "user_id" not in session:
        return redirect(url_for("login"))
    if request.method == "POST":
        cur_pw = request.form.get("current_password","")
        new_pw = request.form.get("new_password","")
        new_pw2 = request.form.get("new_password2","")
        db = get_db()
        row = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if not row or not check_password_hash(row["password_hash"], cur_pw):
            flash("현재 비밀번호가 일치하지 않습니다.", "error")
        elif new_pw != new_pw2:
            flash("새 비밀번호 확인이 일치하지 않습니다.", "error")
        elif cur_pw == new_pw:
            flash("기존 비밀번호와 동일합니다. 다른 비밀번호를 사용해주세요.", "error")
        else:
            ok, msg = validate_password(new_pw, row["username"], row["display_name"])
            if not ok:
                flash(msg, "error")
            else:
                db.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                           (generate_password_hash(new_pw), session["user_id"]))
                db.commit()
                session["must_change_password"] = False
                flash("비밀번호가 변경되었습니다.", "ok")
                return redirect(url_for("dashboard"))
    return render_template("change_password.html", forced=session.get("must_change_password", False))


@app.route("/")
@login_required
def dashboard():
    db = get_db()
    counts = {c: db.execute("SELECT COUNT(*) FROM surgeries WHERE category=?", (c,)).fetchone()[0] for c in CATEGORIES}
    return render_template("dashboard.html", counts=counts, total=sum(counts.values()))


@app.route("/surgeries")
@login_required
def surgery_list():
    q = request.args.get("q","").strip()
    category = request.args.get("category","").strip()
    sql = "SELECT * FROM surgeries WHERE 1=1"; params = []
    if q: sql += " AND (name LIKE ? OR purpose_effect LIKE ?)"; params += [f"%{q}%", f"%{q}%"]
    if category: sql += " AND category = ?"; params.append(category)
    sql += " ORDER BY category, name"
    rows = get_db().execute(sql, params).fetchall()
    return render_template("surgery_list.html", surgeries=rows, q=q, category=category)


SURGERY_FIELDS = ["name","category","purpose_effect","procedure","complications",
    "anesthesia_risk","estimated_cost","hospitalization","expected_duration","notes"]


def _form_to_surgery():
    return {k: request.form.get(k,"").strip() for k in SURGERY_FIELDS}


@app.route("/surgeries/new", methods=["GET","POST"])
@login_required
def surgery_new():
    if request.method == "POST":
        d = _form_to_surgery()
        try:
            get_db().execute("""INSERT INTO surgeries
                (name,category,purpose_effect,procedure,complications,anesthesia_risk,
                 estimated_cost,hospitalization,expected_duration,notes)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                tuple(d[k] for k in SURGERY_FIELDS))
            get_db().commit()
            flash("수술이 DB에 추가되었습니다.", "ok")
            return redirect(url_for("surgery_list"))
        except sqlite3.IntegrityError:
            flash("이미 같은 이름의 수술이 있습니다.", "error")
    return render_template("surgery_edit.html", surgery=None)


@app.route("/surgeries/<int:sid>/edit", methods=["GET","POST"])
@login_required
def surgery_edit(sid):
    db = get_db()
    row = db.execute("SELECT * FROM surgeries WHERE id=?", (sid,)).fetchone()
    if not row: abort(404)
    if request.method == "POST":
        d = _form_to_surgery()
        db.execute("""UPDATE surgeries SET name=?,category=?,purpose_effect=?,procedure=?,
            complications=?,anesthesia_risk=?,estimated_cost=?,hospitalization=?,
            expected_duration=?,notes=?,updated_at=datetime('now') WHERE id=?""",
            tuple(d[k] for k in SURGERY_FIELDS) + (sid,))
        db.commit()
        flash("수정되었습니다.", "ok")
        return redirect(url_for("surgery_list"))
    return render_template("surgery_edit.html", surgery=row)


@app.route("/surgeries/<int:sid>/delete", methods=["POST"])
@login_required
@admin_required
def surgery_delete(sid):
    get_db().execute("DELETE FROM surgeries WHERE id=?", (sid,))
    get_db().commit()
    flash("삭제되었습니다.", "ok")
    return redirect(url_for("surgery_list"))


@app.route("/consent/new")
@login_required
def consent_new():
    db = get_db()
    surgeries = db.execute("SELECT id,name,category FROM surgeries ORDER BY category,name").fetchall()
    hospitalizations = db.execute("SELECT id,name,category FROM hospitalizations ORDER BY category,name").fetchall()
    return render_template("consent_new.html", surgeries=surgeries,
                           hospitalizations=hospitalizations,
                           hosp_categories=HOSP_CATEGORIES)


@app.route("/api/surgery/<int:sid>")
@login_required
def api_surgery_detail(sid):
    row = get_db().execute("SELECT * FROM surgeries WHERE id=?", (sid,)).fetchone()
    if not row: return jsonify({"error":"not found"}), 404
    return jsonify(dict(row))


@app.route("/api/surgery/quick-add", methods=["POST"])
@login_required
def api_surgery_quick_add():
    """수술 upsert: 이름이 있으면 업데이트, 없으면 새로 추가."""
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name: return jsonify({"error":"수술명 필수"}), 400
    db = get_db()
    existing = db.execute("SELECT id FROM surgeries WHERE name=?", (name,)).fetchone()
    if existing:
        db.execute("""UPDATE surgeries SET category=?, purpose_effect=?, procedure=?,
            complications=?, anesthesia_risk=?, estimated_cost=?, hospitalization=?,
            expected_duration=?, notes=?, updated_at=datetime('now') WHERE id=?""",
            (data.get("category") or "응급·기타",
             data.get("purpose_effect",""), data.get("procedure",""),
             data.get("complications",""), data.get("anesthesia_risk",""),
             data.get("estimated_cost",""), data.get("hospitalization",""),
             data.get("expected_duration",""),
             data.get("notes","AI 자동채움으로 업데이트됨"),
             existing["id"]))
        db.commit()
        return jsonify({"id": existing["id"], "existed": True, "updated": True})
    cur = db.execute("""INSERT INTO surgeries
        (name,category,purpose_effect,procedure,complications,anesthesia_risk,
         estimated_cost,hospitalization,expected_duration,notes)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (name, data.get("category") or "응급·기타",
         data.get("purpose_effect",""), data.get("procedure",""),
         data.get("complications",""), data.get("anesthesia_risk",""),
         data.get("estimated_cost",""), data.get("hospitalization",""),
         data.get("expected_duration",""),
         data.get("notes","AI 자동채움으로 추가됨")))
    db.commit()
    return jsonify({"id": cur.lastrowid, "existed": False, "updated": False})


AI_SYSTEM_PROMPT = """당신은 한국의 동물병원(소동물 수의학) 전문가입니다. 주어진 수술명에 대해 보호자에게 설명할 수술동의서 내용을 작성합니다.

**검색 절차**:
1. web_search로 해당 수술의 사망률·치명적 합병증·재수술률을 찾으세요.
2. 사망률(mortality), 재수술률(reoperation), 주요 합병증(major complications)을 수치로 수집하세요.

**출력 규칙** (매우 중요):
- 검색·추론이 끝나면 **최종 출력은 순수 JSON 객체 하나만**입니다.
- 서두 문장·요약·마크다운·코드블록·설명·이모지 등을 포함하지 마세요.
- 첫 문자는 반드시 '{' 이어야 하고, 마지막 문자는 '}' 이어야 합니다.
- JSON 내부 텍스트에 줄바꿈이 필요하면 \\n 이스케이프를 쓰세요.

**JSON 필드**:
- purpose_effect: 수술의 목적 및 기대효과
- procedure: 수술 방법 개요
- complications: 예상 합병증 및 예후. 사망률·재수술률·주요 합병증을 수치와 함께 구체적으로 서술. 경미 → 중증 → 치명적 순.
- anesthesia_risk: 마취 관련 위험
- estimated_cost: 대한민국 원화 범위 (예: "100~200만원")
- hospitalization: 입원기간, 통원치료·재진, 모니터링 방법
- expected_duration: 예시 "60~90분"
- category: "일반외과" / "정형외과" / "연부조직외과" / "응급·기타" 중 하나

보호자가 이해할 수 있는 평이한 한국어로, 그러나 치명적 위험은 명확히 고지하세요.
다시 강조: 최종 응답은 JSON 객체 하나만. 검색 결과 요약·서두 설명 절대 금지."""


@app.route("/api/surgery/ai-generate", methods=["POST"])
@login_required
def api_surgery_ai_generate():
    """Claude API로 수술 정보 자동 생성."""
    if requests is None:
        return jsonify({"error": "'requests' 패키지가 설치되어 있지 않습니다. pip install requests"}), 500
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "수술명을 입력하세요."}), 400
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY 환경변수가 설정되어 있지 않습니다. README 참고."}), 400
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 8000,
                "system": AI_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": f"수술명: {name}\n\n웹검색으로 사망률·치명적 합병증·재수술률을 확인하세요. 모든 검색·추론이 끝나면 JSON 객체 하나만 출력하세요. 요약·설명·서두 문장 금지. 첫 문자는 '{{' 이어야 합니다."}],
                "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
            },
            timeout=120,
        )
        if r.status_code != 200:
            return jsonify({"error": f"Claude API 오류 {r.status_code}: {r.text[:300]}"}), 500
        body = r.json()
        # 여러 content 블록 중 text 블록만 합치기 (server tool use 블록 제외)
        text_parts = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
        text = "\n".join(text_parts).strip()
        # JSON 부분만 추출
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip().rstrip("`").strip()
        # JSON 객체 시작~끝 범위만 뽑기 (서두 설명이 섞인 경우 대비)
        if not text.startswith("{"):
            s = text.find("{"); e = text.rfind("}")
            if s >= 0 and e > s:
                text = text[s:e+1]
        result = json.loads(text)
        return jsonify({"ok": True, "data": result})
    except json.JSONDecodeError as e:
        return jsonify({"error": f"AI 응답 파싱 실패: {e}. 원문: {text[:400]}"}), 500
    except Exception as e:
        return jsonify({"error": f"AI 요청 실패: {e}"}), 500


OCR_SYSTEM_PROMPT = """이미지는 한국 동물병원 PMS365(우리엔) 차트 스크린샷입니다. 상단 바와 헤더에서 환자·보호자 정보를 추출하세요.

**⚠️ 절대 원칙 (매우 중요)**:
1. **이미지에 실제로 적혀있는 글자만 그대로 옮기세요.** 추측·유사한 이름으로 변경·한글 이름 "자연스럽게" 바꾸기 절대 금지.
2. **글자가 흐려서 확실하지 않으면 빈 문자열("")로 두세요.** 비슷한 다른 이름을 쓰느니 빈 값이 낫습니다.
3. **이름은 1글자씩 정확히 읽으세요.** 예: "양순"은 "양순"이지 "금뭉이·영순·야옹이" 가 아닙니다.

**출력 규칙**: JSON 객체 하나만. 서두·설명·코드블록·마크다운 금지. 첫 문자 '{', 마지막 '}'. 없는 필드는 빈 문자열("").

**PMS365 레이아웃 매핑** (이미지에 보이는 텍스트 그대로):
- 좌측 상단 번호 → guardian_id
- 보호자 이름 → guardian_name (이미지 문자 그대로)
- 보호자 휴대폰(010-xxxx-xxxx) → guardian_mobile
- "ID: xxxx" (두번째 ID) → animal_id
- 동물 이름 → patient_name
  · 괄호 안 상태표기(사망/입원/호텔 등)는 **제거**하되 이름 자체는 **이미지 그대로** 유지
  · 예: "양순(사망)" → patient_name="양순" (절대 "금뭉이" 같은 다른 이름으로 바꾸지 마세요)
- 종 "고양이" → species="Feline (고양이)"; "개" → species="Canine (개)"; 그 외 "기타"
- 품종은 이미지 그대로. 단 명백한 영문 오타만 교정 (예: "Korean Shot Hair" → "Korean Shorthair")
- 성별 텍스트 매핑:
  · "중성화 Female" or "Spayed" → "암컷(중성화)"
  · "Female" or "여" or "암" → "암컷"
  · "중성화 Male" or "Castrated" or "Neutered" → "수컷(중성화)"
  · "Male" or "남" or "수" → "수컷"
- 생년월일 괄호 안 나이(예: 15y1m2d) → "15년 1개월" 로 변환
- 체중 "X.X Kg (BSA:...)" → weight="X.X" (숫자만)
- RFID·주소·주민번호·피모색이 화면에 없으면 빈 문자열
- 기저질환은 Problem List/차트에 보이면 기재, 없으면 빈 문자열

**JSON 필드 전체 목록**: guardian_id, guardian_name, guardian_phone, guardian_mobile, guardian_address, guardian_rrn, animal_id, rfid, patient_name, species, breed, age, sex, coat_color, weight, underlying

다시 강조: **patient_name과 guardian_name은 이미지의 실제 문자만 사용. 추측으로 "그럴듯한" 한국이름 생성 금지.**"""


@app.route("/api/chart-ocr", methods=["POST"])
@login_required
def api_chart_ocr():
    if requests is None:
        return jsonify({"error": "requests 패키지가 설치되어 있지 않습니다."}), 500
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY 환경변수가 필요합니다."}), 400
    if "image" not in request.files:
        return jsonify({"error": "image 파일이 없습니다."}), 400
    img = request.files["image"]
    img_bytes = img.read()
    if not img_bytes:
        return jsonify({"error": "빈 이미지."}), 400
    media_type = img.mimetype or "image/png"
    if media_type not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
        media_type = "image/png"
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 2000,
                "system": OCR_SYSTEM_PROMPT,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                        {"type": "text", "text": "차트에서 보호자·동물 정보를 추출해 JSON만 출력하세요."}
                    ]
                }],
            },
            timeout=60,
        )
        if r.status_code != 200:
            return jsonify({"error": f"Claude API 오류 {r.status_code}: {r.text[:300]}"}), 500
        body = r.json()
        text_parts = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
        text = "\n".join(text_parts).strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip().rstrip("`").strip()
        if not text.startswith("{"):
            s = text.find("{"); e = text.rfind("}")
            if s >= 0 and e > s:
                text = text[s:e+1]
        result = json.loads(text)
        return jsonify({"ok": True, "data": result})
    except json.JSONDecodeError as e:
        return jsonify({"error": f"OCR 응답 파싱 실패: {e}. 원문: {text[:400]}"}), 500
    except Exception as e:
        return jsonify({"error": f"OCR 요청 실패: {e}"}), 500


CONSENT_FIELDS = ["guardian_id","guardian_name","guardian_phone","guardian_mobile",
    "guardian_address","guardian_rrn","guardian_relation","animal_id","rfid",
    "species","breed","patient_name","age","sex","coat_color","weight","underlying",
    "surgery_name","surgery_category","surgery_side","asa_grade","purpose_effect",
    "procedure","complications","estimated_cost","hospitalization","anesthesia_risk",
    "expected_duration","extra_note",
    # 입원/당일퇴원 분기용
    "patient_type",       # surgery_daycare / surgery_hospital / hospital_only
    "discharge_type",     # "당일퇴원" or "입원"
    "hospital_days",      # 수술 후 입원 일수 (숫자 문자열)
    "hospital_name",      # 입원만일 때 병증명
    "hospital_category",  # 입원만일 때 분류
    # 입원만(hospital_only) 전용 필드
    "h_purpose_effect", "h_complications", "h_estimated_cost",
    "h_hospitalization", "h_expected_duration",
    "h_admit_date", "h_extra_note",
    ]


@app.route("/consent/preview", methods=["POST"])
@login_required
def consent_preview():
    db = get_db()
    tpl = db.execute("SELECT * FROM hospital_template WHERE id=1").fetchone()
    data = {k: request.form.get(k,"") for k in CONSENT_FIELDS}
    data["vet_name"] = request.form.get("vet_name") or session.get("display_name","")
    data["surgery_date"] = request.form.get("surgery_date") or datetime.now().strftime("%Y-%m-%d")
    data["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    # YouTube URL / QR 코드 (DB가 비어있으면 기본값 fallback)
    try:
        yt_url = tpl["youtube_url"] if "youtube_url" in tpl.keys() else ""
    except Exception:
        yt_url = ""
    if not yt_url:
        yt_url = DEFAULT_YOUTUBE_URL
    data["youtube_url"] = yt_url or ""
    data["video_qr_b64"] = _qr_base64(yt_url) if yt_url else ""
    # 환자 유형별 문서 타이틀 (상단 h1 + 페이지 title 공용)
    _pt = data.get("patient_type") or "surgery_hospital"
    if _pt == "hospital_only":
        doc_title = "입원 동의서"
    elif _pt == "surgery_daycare":
        doc_title = "수술 동의서"
    else:
        doc_title = "수술 및 입원 동의서"
    rendered = {
        "header": render_template_string(tpl["header_html"] or "", d=data, doc_title=doc_title),
        "disclaimer": render_template_string(tpl["disclaimer_html"] or "", d=data, doc_title=doc_title),
        "footer": render_template_string(tpl["footer_html"] or "", d=data, doc_title=doc_title),
    }
    # 입원만 환자: disclaimer의 "알려드립니다!" 섹션(수술 관련 공지) 자동 제거
    if data.get("patient_type") == "hospital_only":
        import re as _re
        rendered["disclaimer"] = _re.sub(
            r'<h4[^>]*>\s*알려드립니다!?\s*</h4>\s*<ol[^>]*class="notice-list"[^>]*>.*?</ol>',
            '', rendered["disclaimer"], flags=_re.DOTALL
        )
    # QR 박스를 '보호자의 약속' 6번 항목 직후(promise-list </ol> 다음)에 주입
    # surgery_daycare(당일퇴원)는 입원 전 영상이 부적합이라 제외
    if data.get("video_qr_b64") and data.get("patient_type") != "surgery_daycare":
        import re as _re2
        qr_html = (
            '<div class="yt-qr-box" style="margin-top:8pt; padding:8pt 10pt;'
            ' border:1.5pt solid #2563eb; border-radius:4pt; display:flex;'
            ' gap:12pt; align-items:center;">'
            f'<img src="data:image/png;base64,{data["video_qr_b64"]}"'
            ' style="width:90px; height:90px; flex-shrink:0;">'
            '<div style="flex:1;">'
            '<strong>📺 입원 전 주의사항 영상 (필수 시청)</strong>'
            '<p class="small" style="margin:3pt 0 0;">QR 코드를 스마트폰 카메라로'
            ' 스캔하여 영상을 시청해주세요.</p>'
            f'<p class="small" style="margin:2pt 0 0; color:#666; word-break:break-all;">{data.get("youtube_url","")}</p>'
            '</div></div>'
        )
        # promise-list 닫는 </ol> 바로 뒤에 삽입
        rendered["disclaimer"] = _re2.sub(
            r'(<ol[^>]*class="promise-list"[^>]*>.*?</ol>)',
            r'\1' + qr_html,
            rendered["disclaimer"], count=1, flags=_re2.DOTALL
        )
    return render_template("consent_print.html", d=data, r=rendered, doc_title=doc_title)


@app.route("/template", methods=["GET","POST"])
@login_required
@admin_required
def template_edit():
    db = get_db()
    if request.method == "POST":
        db.execute("""UPDATE hospital_template SET header_html=?,disclaimer_html=?,
            footer_html=?,youtube_url=?,updated_at=datetime('now') WHERE id=1""",
            (request.form.get("header_html",""),
             request.form.get("disclaimer_html",""),
             request.form.get("footer_html",""),
             request.form.get("youtube_url","").strip()))
        db.commit()
        flash("병원 기본양식이 저장되었습니다.", "ok")
        return redirect(url_for("template_edit"))
    tpl = db.execute("SELECT * FROM hospital_template WHERE id=1").fetchone()
    return render_template("template_edit.html", tpl=tpl)


@app.route("/users", methods=["GET","POST"])
@login_required
@admin_required
def users():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            uname = request.form["username"].strip()
            dname = request.form["display_name"].strip()
            pw = request.form["password"]
            ok, msg = validate_password(pw, uname, dname)
            if not ok:
                flash(msg, "error")
            else:
                try:
                    db.execute("INSERT INTO users (username,password_hash,display_name,role,must_change_password) VALUES (?,?,?,?,1)",
                        (uname, generate_password_hash(pw), dname, request.form.get("role","vet")))
                    db.commit(); flash("사용자가 추가되었습니다. 첫 로그인 시 비밀번호를 변경하게 됩니다.", "ok")
                except sqlite3.IntegrityError:
                    flash("이미 존재하는 아이디입니다.", "error")
        elif action == "reset_pw":
            uid = int(request.form["user_id"])
            pw = request.form["password"]
            target = db.execute("SELECT username, display_name FROM users WHERE id=?", (uid,)).fetchone()
            if not target:
                flash("대상 사용자를 찾을 수 없습니다.", "error")
            else:
                ok, msg = validate_password(pw, target["username"], target["display_name"])
                if not ok:
                    flash(msg, "error")
                else:
                    db.execute("UPDATE users SET password_hash=?, must_change_password=1 WHERE id=?",
                               (generate_password_hash(pw), uid))
                    db.commit(); flash("비밀번호가 초기화되었습니다. 해당 사용자는 다음 로그인 시 변경해야 합니다.", "ok")
        elif action == "delete":
            uid = int(request.form["user_id"])
            if uid == session.get("user_id"):
                flash("자기 자신은 삭제할 수 없습니다.", "error")
            else:
                db.execute("DELETE FROM users WHERE id=?", (uid,))
                db.commit(); flash("삭제되었습니다.", "ok")
        return redirect(url_for("users"))
    rows = db.execute("SELECT * FROM users ORDER BY role, display_name").fetchall()
    return render_template("users.html", users=rows)


# ===================== 입원 DB (hospitalizations) =====================

HOSP_FIELDS = ["name","category","purpose_effect","complications",
               "estimated_cost","hospitalization","expected_duration","notes"]


@app.route("/hospitalizations")
@login_required
def hospitalization_list():
    q = request.args.get("q","").strip()
    category = request.args.get("category","").strip()
    sql = "SELECT * FROM hospitalizations WHERE 1=1"; params = []
    if q: sql += " AND (name LIKE ? OR purpose_effect LIKE ?)"; params += [f"%{q}%", f"%{q}%"]
    if category: sql += " AND category = ?"; params.append(category)
    sql += " ORDER BY category, name"
    rows = get_db().execute(sql, params).fetchall()
    return render_template("hospitalization_list.html", items=rows, q=q, category=category,
                           hosp_categories=HOSP_CATEGORIES)


def _form_to_hosp():
    return {k: request.form.get(k,"").strip() for k in HOSP_FIELDS}


@app.route("/hospitalizations/new", methods=["GET","POST"])
@login_required
def hospitalization_new():
    if request.method == "POST":
        d = _form_to_hosp()
        try:
            get_db().execute("""INSERT INTO hospitalizations
                (name,category,purpose_effect,complications,
                 estimated_cost,hospitalization,expected_duration,notes)
                VALUES (?,?,?,?,?,?,?,?)""",
                tuple(d[k] for k in HOSP_FIELDS))
            get_db().commit()
            flash("입원 케이스가 DB에 추가되었습니다.", "ok")
            return redirect(url_for("hospitalization_list"))
        except sqlite3.IntegrityError:
            flash("이미 같은 이름의 입원 케이스가 있습니다.", "error")
    return render_template("hospitalization_edit.html", item=None,
                           hosp_categories=HOSP_CATEGORIES)


@app.route("/hospitalizations/<int:hid>/edit", methods=["GET","POST"])
@login_required
def hospitalization_edit(hid):
    db = get_db()
    row = db.execute("SELECT * FROM hospitalizations WHERE id=?", (hid,)).fetchone()
    if not row: abort(404)
    if request.method == "POST":
        d = _form_to_hosp()
        db.execute("""UPDATE hospitalizations SET name=?,category=?,purpose_effect=?,
            complications=?,estimated_cost=?,hospitalization=?,expected_duration=?,
            notes=?,updated_at=datetime('now') WHERE id=?""",
            tuple(d[k] for k in HOSP_FIELDS) + (hid,))
        db.commit()
        flash("수정되었습니다.", "ok")
        return redirect(url_for("hospitalization_list"))
    return render_template("hospitalization_edit.html", item=row,
                           hosp_categories=HOSP_CATEGORIES)


@app.route("/hospitalizations/<int:hid>/delete", methods=["POST"])
@login_required
@admin_required
def hospitalization_delete(hid):
    get_db().execute("DELETE FROM hospitalizations WHERE id=?", (hid,))
    get_db().commit()
    flash("삭제되었습니다.", "ok")
    return redirect(url_for("hospitalization_list"))


@app.route("/api/hospitalization/<int:hid>")
@login_required
def api_hospitalization_detail(hid):
    row = get_db().execute("SELECT * FROM hospitalizations WHERE id=?", (hid,)).fetchone()
    if not row: return jsonify({"error":"not found"}), 404
    return jsonify(dict(row))


@app.route("/api/hospitalization/quick-add", methods=["POST"])
@login_required
def api_hospitalization_quick_add():
    """입원 케이스 upsert."""
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name: return jsonify({"error":"병증명 필수"}), 400
    db = get_db()
    existing = db.execute("SELECT id FROM hospitalizations WHERE name=?", (name,)).fetchone()
    if existing:
        db.execute("""UPDATE hospitalizations SET category=?, purpose_effect=?,
            complications=?, estimated_cost=?, hospitalization=?, expected_duration=?,
            notes=?, updated_at=datetime('now') WHERE id=?""",
            (data.get("category") or "기타",
             data.get("purpose_effect",""), data.get("complications",""),
             data.get("estimated_cost",""), data.get("hospitalization",""),
             data.get("expected_duration",""),
             data.get("notes","AI 자동채움으로 업데이트됨"),
             existing["id"]))
        db.commit()
        return jsonify({"id": existing["id"], "existed": True, "updated": True})
    cur = db.execute("""INSERT INTO hospitalizations
        (name,category,purpose_effect,complications,
         estimated_cost,hospitalization,expected_duration,notes)
        VALUES (?,?,?,?,?,?,?,?)""",
        (name, data.get("category") or "기타",
         data.get("purpose_effect",""), data.get("complications",""),
         data.get("estimated_cost",""), data.get("hospitalization",""),
         data.get("expected_duration",""),
         data.get("notes","AI 자동채움으로 추가됨")))
    db.commit()
    return jsonify({"id": cur.lastrowid, "existed": False, "updated": False})


HOSP_AI_SYSTEM_PROMPT = """당신은 한국의 동물병원(소동물 수의학) 전문가입니다. 주어진 '입원 케이스(병증명)'에 대해 보호자에게 설명할 입원 동의서 내용을 작성합니다.

**검색 절차**:
1. web_search로 해당 병증의 주요 합병증·사망률·입원기간을 찾으세요.
2. 수치가 있으면 반드시 포함하세요.

**출력 규칙** (매우 중요):
- 검색·추론이 끝나면 **최종 출력은 순수 JSON 객체 하나만**입니다.
- 서두 문장·요약·마크다운·코드블록·설명·이모지 등을 포함하지 마세요.
- 첫 문자는 반드시 '{' 이어야 하고, 마지막 문자는 '}' 이어야 합니다.
- JSON 내부 텍스트에 줄바꿈이 필요하면 \\n 이스케이프를 쓰세요.

**JSON 필드**:
- purpose_effect: 입원의 목적 및 기대효과 (어떤 치료·모니터링을 왜 하는지)
- complications: 예상 합병증 및 예후. 사망률·주요 합병증 수치를 구체적으로. 경미 → 중증 → 치명적 순.
- estimated_cost: 대한민국 원화 범위 (예: "일 15~30만원, 총 80~200만원")
- hospitalization: 통원치료·재진 여부, 퇴원 후 집에서의 모니터링 방법
- expected_duration: 예시 "3~5일", "7~14일"
- category: "내과" / "외과 회복" / "중환자·응급" / "감염·예방" / "기타" 중 하나

보호자가 이해할 수 있는 평이한 한국어로, 그러나 치명적 위험은 명확히 고지하세요.
다시 강조: 최종 응답은 JSON 객체 하나만. 검색 결과 요약·서두 설명 절대 금지."""


@app.route("/api/hospitalization/ai-generate", methods=["POST"])
@login_required
def api_hospitalization_ai_generate():
    """Claude API로 입원 케이스 정보 자동 생성."""
    if requests is None:
        return jsonify({"error": "'requests' 패키지가 설치되어 있지 않습니다."}), 500
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "병증명을 입력하세요."}), 400
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY 환경변수가 설정되어 있지 않습니다."}), 400
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 8000,
                "system": HOSP_AI_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": f"병증/입원 사유: {name}\n\n웹검색으로 주요 합병증·입원기간·사망률을 확인하세요. 검색·추론이 끝나면 JSON 객체 하나만 출력하세요. 첫 문자는 '{{' 이어야 합니다."}],
                "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
            },
            timeout=120,
        )
        if r.status_code != 200:
            return jsonify({"error": f"Claude API 오류 {r.status_code}: {r.text[:300]}"}), 500
        body = r.json()
        text_parts = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
        text = "\n".join(text_parts).strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip().rstrip("`").strip()
        if not text.startswith("{"):
            s = text.find("{"); e = text.rfind("}")
            if s >= 0 and e > s:
                text = text[s:e+1]
        result = json.loads(text)
        return jsonify({"ok": True, "data": result})
    except json.JSONDecodeError as e:
        return jsonify({"error": f"AI 응답 파싱 실패: {e}. 원문: {text[:400]}"}), 500
    except Exception as e:
        return jsonify({"error": f"AI 요청 실패: {e}"}), 500


@app.cli.command("init-db")
def init_db_cmd():
    init_db(); print("DB 초기화 완료:", DB_PATH)


with app.app_context():
    init_db()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
