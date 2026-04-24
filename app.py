"""루시드 동물병원 수술동의서 자동화 시스템."""
import os
import json
import base64
import secrets
import sqlite3
from datetime import datetime, timedelta
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
        CREATE TABLE IF NOT EXISTS imaging_exams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            category TEXT NOT NULL,
            modality TEXT,             -- "CT" / "MRI" / "CT,MRI"
            purpose_effect TEXT,
            procedure TEXT,
            complications TEXT,
            contrast_type TEXT,        -- "Iodine" / "Gadolinium" / "비사용"
            sedation_note TEXT,
            post_care TEXT,
            expected_duration TEXT,
            estimated_cost TEXT,
            notes TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS hospital_template (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            header_html TEXT, disclaimer_html TEXT, footer_html TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS consent_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            doc_type TEXT NOT NULL,           -- 'surgery' or 'imaging'
            form_data TEXT NOT NULL,          -- JSON (원본 폼 데이터)
            patient_name TEXT,
            guardian_name TEXT,
            vet_name TEXT,
            signature_data TEXT,              -- base64 PNG (서명 이미지)
            signer_name TEXT,                 -- 실제 서명한 보호자명 입력값
            signed_at TEXT,
            expires_at TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_consent_token ON consent_records(token);
        CREATE INDEX IF NOT EXISTS idx_consent_created ON consent_records(created_at);
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
    # consent_records 마이그레이션: checked_boxes 컬럼 (보호자 체크 상태 JSON)
    cur.execute("PRAGMA table_info(consent_records)")
    cr_cols = [c[1] for c in cur.fetchall()]
    if "checked_boxes" not in cr_cols:
        cur.execute("ALTER TABLE consent_records ADD COLUMN checked_boxes TEXT DEFAULT '[]'")
    # Soft delete용 컬럼 (admin 전용 삭제 기능)
    if "deleted_at" not in cr_cols:
        cur.execute("ALTER TABLE consent_records ADD COLUMN deleted_at TEXT")
    if "deleted_by" not in cr_cols:
        cur.execute("ALTER TABLE consent_records ADD COLUMN deleted_by INTEGER")
    if "delete_reason" not in cr_cols:
        cur.execute("ALTER TABLE consent_records ADD COLUMN delete_reason TEXT")
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
    cur.execute("SELECT COUNT(*) FROM imaging_exams")
    if cur.fetchone()[0] == 0:
        try:
            from seed_data import SEED_IMAGING
        except ImportError:
            SEED_IMAGING = []
        for s in SEED_IMAGING:
            cur.execute("""INSERT INTO imaging_exams
                (name,category,modality,purpose_effect,procedure,complications,
                 contrast_type,sedation_note,post_care,expected_duration,estimated_cost,notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (s["name"], s["category"], s.get("modality","CT"),
                 s.get("purpose_effect"), s.get("procedure"), s.get("complications"),
                 s.get("contrast_type"), s.get("sedation_note"), s.get("post_care"),
                 s.get("expected_duration"), s.get("estimated_cost"), s.get("notes")))
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
    pending_cnt = db.execute(
        "SELECT COUNT(*) FROM consent_records WHERE signed_at IS NULL "
        "AND deleted_at IS NULL "
        "AND datetime(expires_at) > datetime('now', 'localtime')"
    ).fetchone()[0]
    signed_cnt = db.execute(
        "SELECT COUNT(*) FROM consent_records WHERE signed_at IS NOT NULL AND deleted_at IS NULL"
    ).fetchone()[0]
    return render_template("dashboard.html", counts=counts, total=sum(counts.values()),
                           pending_cnt=pending_cnt, signed_cnt=signed_cnt)


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
- **기저질환(underlying)·복용약물은 절대 추출하지 마세요.** 주치의가 직접 기재합니다. 빈 문자열로 두세요.

**JSON 필드 전체 목록**: guardian_id, guardian_name, guardian_phone, guardian_mobile, guardian_address, guardian_rrn, animal_id, rfid, patient_name, species, breed, age, sex, coat_color, weight

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
        # 기저질환·복용약물은 OCR이 추출해도 절대 자동입력하지 않음 (주치의가 직접 기재)
        if isinstance(result, dict):
            result.pop("underlying", None)
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


def _apply_checked_boxes(html, checked_set):
    """렌더된 HTML 내 <span class="cb">☐</span> 들을 순서대로 찾아서,
    checked_set(int set)에 해당하는 인덱스는 ☑로 치환. ASA 표 등 '☑'가 이미 있는
    것은 class="cb"가 없거나 '☐'가 아니라 자동으로 건너뛴다."""
    import re as _re
    pattern = _re.compile(r'(<span[^>]*class="cb"[^>]*>)☐(</span>)')
    counter = [0]
    def rep(m):
        i = counter[0]
        counter[0] += 1
        if i in checked_set:
            return m.group(1) + '☑' + m.group(2)
        return m.group(0)
    return pattern.sub(rep, html)


def _strip_handwritten_signature(html):
    """전자서명이 있을 때 footer의 '보호자 또는 의뢰인: ___ (인)' 수기 서명란 제거."""
    import re as _re
    # DEFAULT_FOOTER 형태의 수기 서명란 + 선택적 <br> 까지 함께 제거
    html = _re.sub(
        r'보호자\s*또는\s*의뢰인\s*:\s*[_]+\s*\(인\)\s*(<br\s*/?>)?\s*',
        '',
        html
    )
    return html


def _render_consent_print_from_data(data, db, signature_b64=None, signer_name=None, signed_at=None,
                                    show_sign_button=False, sign_interactive=False,
                                    checked_boxes=None):
    """수술/입원 동의서 data dict → consent_print.html 렌더링.
    consent_preview와 sign 페이지에서 공통으로 사용. signature_b64가 있으면 서명 이미지 삽입.
    show_sign_button=True: 원내 수의사 미리보기에 "서명받기" 버튼 노출.
    sign_interactive=True: iframe 내부에서 체크박스 클릭 가능하게 JS 주입.
    checked_boxes: list of int (서명본 렌더링 시 체크할 인덱스들)."""
    tpl = db.execute("SELECT * FROM hospital_template WHERE id=1").fetchone()
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
    # 서명 이미지가 제공되면 data에 주입 (consent_print.html에서 표시)
    if signature_b64:
        data["signature_b64"] = signature_b64
        data["signer_name"] = signer_name or data.get("guardian_name", "")
        data["signed_at"] = signed_at or ""
    # 서명받기 버튼용 hidden form 필드 (렌더된 추가 필드는 제외)
    sign_form_fields = CONSENT_FIELDS + ["vet_name", "surgery_date"]
    html = render_template(
        "consent_print.html",
        d=data, r=rendered, doc_title=doc_title,
        show_sign_button=show_sign_button and not signature_b64,
        sign_interactive=sign_interactive,
        sign_form_fields=sign_form_fields,
        sign_form_action=url_for("consent_create_sign_link"),
    )
    # 서명 완료본 렌더링 후처리: 체크박스 반영 + 수기 서명란 제거
    if signature_b64:
        if checked_boxes:
            html = _apply_checked_boxes(html, set(checked_boxes))
        html = _strip_handwritten_signature(html)
    return html


@app.route("/consent/preview", methods=["POST"])
@login_required
def consent_preview():
    db = get_db()
    data = {k: request.form.get(k, "") for k in CONSENT_FIELDS}
    data["vet_name"] = request.form.get("vet_name") or session.get("display_name", "")
    data["surgery_date"] = request.form.get("surgery_date") or datetime.now().strftime("%Y-%m-%d")
    data["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    return _render_consent_print_from_data(data, db, show_sign_button=True)


# ===================== 보호자 모바일 서명 (QR 링크) =====================

SIGN_LINK_TTL_HOURS = 24  # QR 링크 유효시간 (시간 단위)


def _make_sign_token():
    """URL-safe 랜덤 토큰 32자."""
    return secrets.token_urlsafe(24)


def _sign_base_url():
    """외부에서 접근 가능한 기본 URL.
    Render/프록시 환경에서 X-Forwarded-Proto 고려."""
    # 우선 ENV에서 지정된 PUBLIC_BASE_URL 사용 (예: https://lucid-consent.onrender.com)
    base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base:
        return base
    # 없으면 요청 헤더 기반
    return request.url_root.rstrip("/")


@app.route("/consent/create-sign-link", methods=["POST"])
@login_required
def consent_create_sign_link():
    """수술동의서 폼 데이터로 서명 토큰 생성 → QR/URL 반환 (AJAX)."""
    db = get_db()
    # 기존 preview와 동일하게 폼 데이터 수집
    data = {k: request.form.get(k, "") for k in CONSENT_FIELDS}
    data["vet_name"] = request.form.get("vet_name") or session.get("display_name", "")
    data["surgery_date"] = request.form.get("surgery_date") or datetime.now().strftime("%Y-%m-%d")
    data["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    token = _make_sign_token()
    expires_at = (datetime.now() + timedelta(hours=SIGN_LINK_TTL_HOURS)).strftime("%Y-%m-%d %H:%M:%S")

    db.execute(
        """INSERT INTO consent_records
           (token, doc_type, form_data, patient_name, guardian_name, vet_name,
            expires_at, created_by)
           VALUES (?,?,?,?,?,?,?,?)""",
        (token, "surgery", json.dumps(data, ensure_ascii=False),
         data.get("patient_name", ""), data.get("guardian_name", ""),
         data.get("vet_name", ""), expires_at, session.get("user_id", 0))
    )
    db.commit()

    sign_url = f"{_sign_base_url()}/sign/{token}"
    return jsonify({
        "ok": True,
        "token": token,
        "url": sign_url,
        "qr_b64": _qr_base64(sign_url),
        "expires_at": expires_at,
        "ttl_hours": SIGN_LINK_TTL_HOURS,
        "doc_type": "surgery",
    })


@app.route("/imaging/consent/create-sign-link", methods=["POST"])
@login_required
def imaging_consent_create_sign_link():
    """영상검사 동의서 폼 데이터로 서명 토큰 생성 → QR/URL 반환 (AJAX)."""
    db = get_db()
    mods = request.form.getlist("imaging_modalities")
    data = {k: request.form.get(k, "") for k in CONSENT_IMG_FIELDS}
    data["imaging_modalities"] = ",".join(mods) if mods else request.form.get("imaging_modalities", "")
    data["vet_name"] = request.form.get("vet_name") or session.get("display_name", "")
    data["exam_date"] = request.form.get("exam_date") or datetime.now().strftime("%Y-%m-%d")
    data["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    token = _make_sign_token()
    expires_at = (datetime.now() + timedelta(hours=SIGN_LINK_TTL_HOURS)).strftime("%Y-%m-%d %H:%M:%S")

    db.execute(
        """INSERT INTO consent_records
           (token, doc_type, form_data, patient_name, guardian_name, vet_name,
            expires_at, created_by)
           VALUES (?,?,?,?,?,?,?,?)""",
        (token, "imaging", json.dumps(data, ensure_ascii=False),
         data.get("patient_name", ""), data.get("guardian_name", ""),
         data.get("vet_name", ""), expires_at, session.get("user_id", 0))
    )
    db.commit()

    sign_url = f"{_sign_base_url()}/sign/{token}"
    return jsonify({
        "ok": True,
        "token": token,
        "url": sign_url,
        "qr_b64": _qr_base64(sign_url),
        "expires_at": expires_at,
        "ttl_hours": SIGN_LINK_TTL_HOURS,
        "doc_type": "imaging",
    })


# ===================== 안락사 동의서 =====================
# 안락사 결정 환자용. 사후 장례 방법 선택 포함.

EUTHANASIA_FIELDS = [
    "guardian_id", "guardian_name", "guardian_phone", "guardian_mobile",
    "guardian_address", "guardian_rrn6",
    "animal_id", "rfid", "patient_name", "species", "breed",
    "age", "sex", "color",
    "funeral",
]


def _render_euthanasia_print_from_data(data, db, signature_b64=None, signer_name=None,
                                       signed_at=None, show_sign_button=False,
                                       sign_interactive=False):
    """안락사 동의서 렌더링."""
    doc_title = "안락사 동의서"
    if signature_b64:
        data["signature_b64"] = signature_b64
        data["signer_name"] = signer_name or data.get("guardian_name", "")
        data["signed_at"] = signed_at or ""
    sign_form_fields = EUTHANASIA_FIELDS + ["vet_name", "doc_date"]
    return render_template(
        "euthanasia_print.html",
        d=data, doc_title=doc_title,
        show_sign_button=show_sign_button and not signature_b64,
        sign_interactive=sign_interactive,
        sign_form_fields=sign_form_fields,
        sign_form_action=url_for("euthanasia_create_sign_link"),
        hospital_name=HOSPITAL_NAME,
    )


@app.route("/euthanasia/new", methods=["GET"])
@login_required
def euthanasia_new():
    return render_template(
        "euthanasia_new.html",
        today=datetime.now().strftime("%Y-%m-%d"),
        vet_name=session.get("display_name", ""),
    )


@app.route("/euthanasia/preview", methods=["POST"])
@login_required
def euthanasia_preview():
    db = get_db()
    data = {k: request.form.get(k, "") for k in EUTHANASIA_FIELDS}
    data["vet_name"] = request.form.get("vet_name") or session.get("display_name", "")
    data["doc_date"] = request.form.get("doc_date") or datetime.now().strftime("%Y-%m-%d")
    data["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    return _render_euthanasia_print_from_data(data, db, show_sign_button=True)


@app.route("/euthanasia/create-sign-link", methods=["POST"])
@login_required
def euthanasia_create_sign_link():
    """안락사 동의서 폼 → 서명 토큰 생성."""
    db = get_db()
    data = {k: request.form.get(k, "") for k in EUTHANASIA_FIELDS}
    data["vet_name"] = request.form.get("vet_name") or session.get("display_name", "")
    data["doc_date"] = request.form.get("doc_date") or datetime.now().strftime("%Y-%m-%d")
    data["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    token = _make_sign_token()
    expires_at = (datetime.now() + timedelta(hours=SIGN_LINK_TTL_HOURS)).strftime("%Y-%m-%d %H:%M:%S")

    db.execute(
        """INSERT INTO consent_records
           (token, doc_type, form_data, patient_name, guardian_name, vet_name,
            expires_at, created_by)
           VALUES (?,?,?,?,?,?,?,?)""",
        (token, "euthanasia", json.dumps(data, ensure_ascii=False),
         data.get("patient_name", ""), data.get("guardian_name", ""),
         data.get("vet_name", ""), expires_at, session.get("user_id", 0))
    )
    db.commit()

    sign_url = f"{_sign_base_url()}/sign/{token}"
    return jsonify({
        "ok": True,
        "token": token,
        "url": sign_url,
        "qr_b64": _qr_base64(sign_url),
        "expires_at": expires_at,
        "ttl_hours": SIGN_LINK_TTL_HOURS,
        "doc_type": "euthanasia",
    })


# ===================== 개인정보 수집·활용 동의서 =====================
# 마약류·향정신성의약품 원외처방 시 필수. 보호자가 모바일에서 주민번호 직접 입력.

PRIVACY_FIELDS = [
    "guardian_id", "guardian_name", "guardian_phone", "guardian_mobile",
    "guardian_address", "guardian_email",
    # 환자 식별용 (인쇄본 상단 한 줄)
    "animal_id", "patient_name", "species", "breed",
]


def _render_privacy_print_from_data(data, db, signature_b64=None, signer_name=None,
                                    signed_at=None, show_sign_button=False,
                                    sign_interactive=False, privacy_input=None):
    """개인정보 수집·활용 동의서 렌더링.
    privacy_input: 보호자가 모바일에서 입력한 {rrn, sms_ok, mail_ok, ads_ok, legal_name, legal_phone, legal_relation}
    """
    # 문서 타이틀
    doc_title = "개인정보 수집 및 활용 동의서"
    if signature_b64:
        data["signature_b64"] = signature_b64
        data["signer_name"] = signer_name or data.get("guardian_name", "")
        data["signed_at"] = signed_at or ""
    sign_form_fields = PRIVACY_FIELDS + ["vet_name", "doc_date"]
    html = render_template(
        "privacy_print.html",
        d=data, doc_title=doc_title,
        privacy=privacy_input or {},
        show_sign_button=show_sign_button and not signature_b64,
        sign_interactive=sign_interactive,
        sign_form_fields=sign_form_fields,
        sign_form_action=url_for("privacy_create_sign_link"),
        hospital_name=HOSPITAL_NAME,
    )
    return html


@app.route("/privacy/new", methods=["GET"])
@login_required
def privacy_new():
    return render_template(
        "privacy_new.html",
        today=datetime.now().strftime("%Y-%m-%d"),
        vet_name=session.get("display_name", ""),
    )


@app.route("/privacy/preview", methods=["POST"])
@login_required
def privacy_preview():
    db = get_db()
    data = {k: request.form.get(k, "") for k in PRIVACY_FIELDS}
    data["vet_name"] = request.form.get("vet_name") or session.get("display_name", "")
    data["doc_date"] = request.form.get("doc_date") or datetime.now().strftime("%Y-%m-%d")
    data["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    return _render_privacy_print_from_data(data, db, show_sign_button=True)


@app.route("/privacy/create-sign-link", methods=["POST"])
@login_required
def privacy_create_sign_link():
    """개인정보 동의서 폼 → 서명 토큰 생성."""
    db = get_db()
    data = {k: request.form.get(k, "") for k in PRIVACY_FIELDS}
    data["vet_name"] = request.form.get("vet_name") or session.get("display_name", "")
    data["doc_date"] = request.form.get("doc_date") or datetime.now().strftime("%Y-%m-%d")
    data["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    token = _make_sign_token()
    expires_at = (datetime.now() + timedelta(hours=SIGN_LINK_TTL_HOURS)).strftime("%Y-%m-%d %H:%M:%S")

    db.execute(
        """INSERT INTO consent_records
           (token, doc_type, form_data, patient_name, guardian_name, vet_name,
            expires_at, created_by)
           VALUES (?,?,?,?,?,?,?,?)""",
        (token, "privacy", json.dumps(data, ensure_ascii=False),
         data.get("patient_name", ""), data.get("guardian_name", ""),
         data.get("vet_name", ""), expires_at, session.get("user_id", 0))
    )
    db.commit()

    sign_url = f"{_sign_base_url()}/sign/{token}"
    return jsonify({
        "ok": True,
        "token": token,
        "url": sign_url,
        "qr_b64": _qr_base64(sign_url),
        "expires_at": expires_at,
        "ttl_hours": SIGN_LINK_TTL_HOURS,
        "doc_type": "privacy",
    })


# ----- 보호자 서명 페이지 (로그인 불필요, 토큰 기반) -----

def _load_sign_record(token):
    """토큰으로 consent_records 조회. 삭제된(soft-deleted) 레코드는 None 반환."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM consent_records WHERE token=? AND deleted_at IS NULL", (token,)
    ).fetchone()
    return row


def _sign_status(row):
    """서명 레코드 상태 판정: 'ok' / 'expired' / 'signed' / None(missing)."""
    if not row:
        return None
    if row["signed_at"]:
        return "signed"
    try:
        exp = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        exp = None
    if exp and datetime.now() > exp:
        return "expired"
    return "ok"


@app.route("/sign/<token>", methods=["GET"])
def sign_page(token):
    """보호자 서명 페이지 (모바일 최적화)."""
    row = _load_sign_record(token)
    status = _sign_status(row)
    if status is None:
        return render_template("sign_status.html",
                               status="missing",
                               hospital_name=HOSPITAL_SHORT), 404
    if status == "expired":
        return render_template("sign_status.html",
                               status="expired",
                               hospital_name=HOSPITAL_SHORT), 410
    if status == "signed":
        # 이미 서명 완료 → 열람(complete 페이지로)
        return redirect(url_for("sign_complete", token=token))

    data = json.loads(row["form_data"])
    doc_type = row["doc_type"]
    if doc_type == "imaging":
        mod_label = (data.get("imaging_modalities") or "").replace(",", "·")
        doc_title = f"영상 촬영 마취 동의서 ({mod_label})" if mod_label else "영상 촬영 마취 동의서"
    elif doc_type == "privacy":
        doc_title = "개인정보 수집 및 활용 동의서"
    elif doc_type == "euthanasia":
        doc_title = "안락사 동의서"
    else:
        pt = data.get("patient_type") or "surgery_hospital"
        if pt == "hospital_only":
            doc_title = "입원 동의서"
        elif pt == "surgery_daycare":
            doc_title = "수술 동의서"
        else:
            doc_title = "수술 및 입원 동의서"

    # JS에서 Date() 파싱 가능하도록 ISO 유사 포맷 (로컬시간)
    expires_at_iso = row["expires_at"].replace(" ", "T")

    return render_template("sign_page.html",
                           token=token,
                           doc_type=doc_type,
                           doc_title=doc_title,
                           patient_name=data.get("patient_name", ""),
                           guardian_name=data.get("guardian_name", ""),
                           guardian_email=data.get("guardian_email", ""),
                           hospital_name=HOSPITAL_SHORT,
                           expires_at_iso=expires_at_iso)


@app.route("/sign/<token>/preview", methods=["GET"])
def sign_doc_preview(token):
    """iframe용 동의서 본문 렌더링 (서명란 없는 원본) + 체크박스 활성화 JS."""
    row = _load_sign_record(token)
    status = _sign_status(row)
    if status is None:
        abort(404)
    if status == "expired":
        abort(410)

    data = json.loads(row["form_data"])
    db = get_db()
    if row["doc_type"] == "imaging":
        return _render_imaging_print_from_data(data, db, sign_interactive=True)
    if row["doc_type"] == "privacy":
        return _render_privacy_print_from_data(data, db, sign_interactive=True)
    if row["doc_type"] == "euthanasia":
        return _render_euthanasia_print_from_data(data, db, sign_interactive=True)
    return _render_consent_print_from_data(data, db, sign_interactive=True)


@app.route("/sign/<token>", methods=["POST"])
def sign_submit(token):
    """보호자 서명 제출. JSON body: {signer_name, signature(data URL)}."""
    row = _load_sign_record(token)
    status = _sign_status(row)
    if status is None:
        return jsonify({"ok": False, "error": "유효하지 않은 링크입니다."}), 404
    if status == "expired":
        return jsonify({"ok": False, "error": "링크가 만료되었습니다."}), 410
    if status == "signed":
        return jsonify({"ok": False, "error": "이미 서명이 완료된 동의서입니다."}), 409

    payload = request.get_json(silent=True) or {}
    signer_name = (payload.get("signer_name") or "").strip()
    signature = payload.get("signature") or ""
    checked_raw = payload.get("checked_boxes") or []
    if not signer_name:
        return jsonify({"ok": False, "error": "보호자 성함을 입력해주세요."}), 400
    if not signature.startswith("data:image/"):
        return jsonify({"ok": False, "error": "서명 이미지가 올바르지 않습니다."}), 400

    # "data:image/png;base64,XXXX" 에서 base64 부분만 저장
    try:
        _, b64 = signature.split(",", 1)
    except ValueError:
        return jsonify({"ok": False, "error": "서명 이미지 형식 오류."}), 400

    # 용량 제한 (대략 2MB)
    if len(b64) > 2_800_000:
        return jsonify({"ok": False, "error": "서명 이미지가 너무 큽니다."}), 400

    # checked_boxes: 정수 배열만 허용
    try:
        checked_boxes = sorted({int(x) for x in checked_raw if isinstance(x, (int, str)) and str(x).isdigit()})
    except (TypeError, ValueError):
        checked_boxes = []

    # privacy_input: doc_type='privacy'일 때 보호자가 모바일에서 입력한 정보
    # (주민번호, 동의체크, 법정대리인 등) → form_data에 _privacy_input 키로 병합
    privacy_input = payload.get("privacy_input") or {}
    # euthanasia_input: doc_type='euthanasia'일 때 보호자가 선택한 사후 장례 방법
    euthanasia_input = payload.get("euthanasia_input") or {}
    signed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db = get_db()

    if row["doc_type"] == "euthanasia" and euthanasia_input:
        # 장례 방법만 whitelist 로 업데이트
        try:
            form_data = json.loads(row["form_data"])
        except (ValueError, TypeError):
            form_data = {}
        funeral = (euthanasia_input.get("funeral") or "").strip()
        if funeral not in ("individual", "hospital", "take_home"):
            return jsonify({"ok": False, "error": "사후 장례 방법이 올바르지 않습니다."}), 400
        form_data["funeral"] = funeral
        db.execute(
            "UPDATE consent_records SET signature_data=?, signer_name=?, signed_at=?, "
            "checked_boxes=?, form_data=? WHERE token=?",
            (b64, signer_name, signed_at, json.dumps(checked_boxes),
             json.dumps(form_data, ensure_ascii=False), token)
        )
        db.commit()
        return jsonify({"ok": True, "redirect": url_for("sign_complete", token=token)})

    if row["doc_type"] == "privacy" and privacy_input:
        # 기존 form_data 읽어서 _privacy_input 병합 후 저장
        try:
            form_data = json.loads(row["form_data"])
        except (ValueError, TypeError):
            form_data = {}
        # 허용된 키만 추출 (whitelist)
        allowed = {"rrn", "email", "sms_ok", "mail_ok", "ads_ok",
                   "legal_name", "legal_phone", "legal_relation"}
        clean = {k: (v or "").strip() if isinstance(v, str) else v
                 for k, v in privacy_input.items() if k in allowed}
        # 주민번호 간단 검증 (13자리 숫자만)
        rrn = clean.get("rrn", "").replace("-", "")
        if rrn and (not rrn.isdigit() or len(rrn) != 13):
            return jsonify({"ok": False, "error": "주민등록번호 형식이 올바르지 않습니다 (13자리 숫자)."}), 400
        clean["rrn"] = rrn
        form_data["_privacy_input"] = clean
        db.execute(
            "UPDATE consent_records SET signature_data=?, signer_name=?, signed_at=?, "
            "checked_boxes=?, form_data=? WHERE token=?",
            (b64, signer_name, signed_at, json.dumps(checked_boxes),
             json.dumps(form_data, ensure_ascii=False), token)
        )
    else:
        db.execute(
            "UPDATE consent_records SET signature_data=?, signer_name=?, signed_at=?, checked_boxes=? WHERE token=?",
            (b64, signer_name, signed_at, json.dumps(checked_boxes), token)
        )
    db.commit()
    return jsonify({
        "ok": True,
        "redirect": url_for("sign_complete", token=token),
    })


@app.route("/sign/<token>/complete", methods=["GET"])
def sign_complete(token):
    """서명 완료 안내 + 서명본 PDF(브라우저 인쇄) 버튼."""
    row = _load_sign_record(token)
    if not row:
        abort(404)
    if not row["signed_at"]:
        # 미서명 상태면 서명 페이지로
        return redirect(url_for("sign_page", token=token))

    data = json.loads(row["form_data"])
    return render_template(
        "sign_complete.html",
        token=token,
        hospital_name=HOSPITAL_SHORT,
        patient_name=data.get("patient_name", ""),
        signer_name=row["signer_name"] or "",
        signed_at=row["signed_at"],
    )


@app.route("/sign/<token>/pdf", methods=["GET"])
def sign_pdf(token):
    """서명 이미지 삽입된 완성본 동의서 (브라우저 인쇄 → PDF 저장)."""
    row = _load_sign_record(token)
    if not row:
        abort(404)
    if not row["signed_at"]:
        abort(400)

    data = json.loads(row["form_data"])
    # checked_boxes JSON 파싱
    try:
        checked_boxes = json.loads(row["checked_boxes"] or "[]")
        if not isinstance(checked_boxes, list):
            checked_boxes = []
    except (ValueError, TypeError):
        checked_boxes = []

    db = get_db()
    if row["doc_type"] == "imaging":
        return _render_imaging_print_from_data(
            data, db,
            signature_b64=row["signature_data"],
            signer_name=row["signer_name"],
            signed_at=row["signed_at"],
            checked_boxes=checked_boxes,
        )
    if row["doc_type"] == "privacy":
        # form_data 안에 보호자가 모바일에서 입력한 정보가 병합되어 있음 (privacy_input)
        privacy_input = data.get("_privacy_input") or {}
        return _render_privacy_print_from_data(
            data, db,
            signature_b64=row["signature_data"],
            signer_name=row["signer_name"],
            signed_at=row["signed_at"],
            privacy_input=privacy_input,
        )
    if row["doc_type"] == "euthanasia":
        return _render_euthanasia_print_from_data(
            data, db,
            signature_b64=row["signature_data"],
            signer_name=row["signer_name"],
            signed_at=row["signed_at"],
        )
    return _render_consent_print_from_data(
        data, db,
        signature_b64=row["signature_data"],
        signer_name=row["signer_name"],
        signed_at=row["signed_at"],
        checked_boxes=checked_boxes,
    )


# ----- 서명 대기 리스트 / 동의서 이력 / QR 재조회 -----

def _doc_type_label(t):
    return "영상검사" if t == "imaging" else "수술·입원"


@app.route("/consents/pending", methods=["GET"])
@login_required
def consents_pending():
    """서명 대기 리스트: signed_at IS NULL, 미만료."""
    db = get_db()
    rows = db.execute("""
        SELECT cr.id, cr.token, cr.doc_type, cr.patient_name, cr.guardian_name,
               cr.vet_name, cr.expires_at, cr.created_at, cr.created_by,
               u.display_name AS creator_name
        FROM consent_records cr
        LEFT JOIN users u ON u.id = cr.created_by
        WHERE cr.signed_at IS NULL
          AND cr.deleted_at IS NULL
          AND datetime(cr.expires_at) > datetime('now', 'localtime')
        ORDER BY cr.created_at DESC
    """).fetchall()
    return render_template("consent_pending.html", rows=rows,
                           doc_type_label=_doc_type_label)


@app.route("/consents", methods=["GET"])
@login_required
def consents_history():
    """서명 완료 동의서 검색/이력."""
    q = (request.args.get("q") or "").strip()
    doc_type = request.args.get("type") or ""
    db = get_db()
    sql = """
        SELECT cr.id, cr.token, cr.doc_type, cr.patient_name, cr.guardian_name,
               cr.vet_name, cr.signer_name, cr.signed_at, cr.created_at,
               u.display_name AS creator_name
        FROM consent_records cr
        LEFT JOIN users u ON u.id = cr.created_by
        WHERE cr.signed_at IS NOT NULL
          AND cr.deleted_at IS NULL
    """
    args = []
    if q:
        sql += " AND (cr.patient_name LIKE ? OR cr.guardian_name LIKE ? OR cr.signer_name LIKE ? OR cr.vet_name LIKE ?)"
        like = f"%{q}%"
        args += [like, like, like, like]
    if doc_type in ("surgery", "imaging", "privacy", "euthanasia"):
        sql += " AND cr.doc_type=?"
        args.append(doc_type)
    sql += " ORDER BY cr.signed_at DESC LIMIT 300"
    rows = db.execute(sql, args).fetchall()
    return render_template("consent_history.html", rows=rows,
                           q=q, doc_type=doc_type,
                           doc_type_label=_doc_type_label)


@app.route("/api/consents/<int:cid>/qr", methods=["GET"])
@login_required
def api_consent_qr(cid):
    """기존 토큰의 QR을 다시 반환 (대기 리스트 → QR 팝업용)."""
    db = get_db()
    row = db.execute(
        "SELECT token, expires_at, signed_at FROM consent_records WHERE id=? AND deleted_at IS NULL", (cid,)
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "동의서를 찾을 수 없습니다."}), 404
    if row["signed_at"]:
        return jsonify({"ok": False, "error": "이미 서명이 완료되었습니다.",
                        "signed": True}), 409
    try:
        exp = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        exp = None
    if exp and datetime.now() > exp:
        return jsonify({"ok": False, "error": "링크가 만료되었습니다.",
                        "expired": True}), 410
    sign_url = f"{_sign_base_url()}/sign/{row['token']}"
    return jsonify({
        "ok": True,
        "token": row["token"],
        "url": sign_url,
        "qr_b64": _qr_base64(sign_url),
        "expires_at": row["expires_at"],
    })


@app.route("/api/consents/<int:cid>/cancel", methods=["POST"])
@login_required
def api_consent_cancel(cid):
    """대기 중인 서명 요청 취소 (만료 시각을 과거로 설정)."""
    db = get_db()
    row = db.execute(
        "SELECT signed_at FROM consent_records WHERE id=? AND deleted_at IS NULL", (cid,)
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "없습니다."}), 404
    if row["signed_at"]:
        return jsonify({"ok": False, "error": "이미 서명이 완료된 건은 취소할 수 없습니다."}), 409
    db.execute("UPDATE consent_records SET expires_at='2000-01-01 00:00:00' WHERE id=?", (cid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/consents/<int:cid>/delete", methods=["POST"])
@login_required
@admin_required
def api_consent_delete(cid):
    """[관리자 전용] 서명 완료 동의서 soft delete.
    보안: 환자명 재입력 + 사유 필수. DB에 deleted_at/deleted_by/delete_reason 기록."""
    db = get_db()
    row = db.execute(
        "SELECT id, patient_name, signed_at, deleted_at FROM consent_records WHERE id=?",
        (cid,)
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "동의서를 찾을 수 없습니다."}), 404
    if row["deleted_at"]:
        return jsonify({"ok": False, "error": "이미 삭제된 동의서입니다."}), 410

    payload = request.get_json(silent=True) or {}
    confirm_name = (payload.get("patient_name") or "").strip()
    reason = (payload.get("reason") or "").strip()

    # 환자명 정확 일치 확인 (공백 무시)
    actual = (row["patient_name"] or "").strip()
    if not confirm_name:
        return jsonify({"ok": False, "error": "환자명을 입력해주세요."}), 400
    if confirm_name != actual:
        return jsonify({"ok": False,
                        "error": f"환자명이 일치하지 않습니다. 정확히 '{actual}'을(를) 입력해주세요."}), 400
    if not reason or len(reason) < 3:
        return jsonify({"ok": False, "error": "삭제 사유를 3자 이상 입력해주세요."}), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "UPDATE consent_records SET deleted_at=?, deleted_by=?, delete_reason=? WHERE id=?",
        (now, session.get("user_id", 0), reason, cid)
    )
    db.commit()
    return jsonify({"ok": True, "deleted_at": now})


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


# ===================== 영상검사 DB (imaging_exams) =====================

IMG_CATEGORIES = ["신경", "복부", "흉부", "근골격", "치과", "종양", "혈관", "기타"]
IMG_FIELDS = ["name","category","modality","purpose_effect","procedure","complications",
              "contrast_type","sedation_note","post_care",
              "expected_duration","estimated_cost","notes"]


@app.route("/imaging")
@login_required
def imaging_list():
    q = request.args.get("q","").strip()
    category = request.args.get("category","").strip()
    sql = "SELECT * FROM imaging_exams WHERE 1=1"; params = []
    if q: sql += " AND (name LIKE ? OR purpose_effect LIKE ?)"; params += [f"%{q}%", f"%{q}%"]
    if category: sql += " AND category = ?"; params.append(category)
    sql += " ORDER BY category, name"
    rows = get_db().execute(sql, params).fetchall()
    return render_template("imaging_list.html", items=rows, q=q, category=category,
                           img_categories=IMG_CATEGORIES)


def _form_to_img():
    return {k: request.form.get(k,"").strip() for k in IMG_FIELDS}


@app.route("/imaging/new", methods=["GET","POST"])
@login_required
def imaging_new_exam():
    if request.method == "POST":
        d = _form_to_img()
        try:
            get_db().execute("""INSERT INTO imaging_exams
                (name,category,modality,purpose_effect,procedure,complications,
                 contrast_type,sedation_note,post_care,expected_duration,estimated_cost,notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                tuple(d[k] for k in IMG_FIELDS))
            get_db().commit()
            flash("영상검사가 DB에 추가되었습니다.", "ok")
            return redirect(url_for("imaging_list"))
        except sqlite3.IntegrityError:
            flash("이미 같은 이름의 영상검사가 있습니다.", "error")
    return render_template("imaging_edit.html", item=None,
                           img_categories=IMG_CATEGORIES)


@app.route("/imaging/<int:iid>/edit", methods=["GET","POST"])
@login_required
def imaging_edit(iid):
    db = get_db()
    row = db.execute("SELECT * FROM imaging_exams WHERE id=?", (iid,)).fetchone()
    if not row: abort(404)
    if request.method == "POST":
        d = _form_to_img()
        db.execute("""UPDATE imaging_exams SET name=?,category=?,modality=?,purpose_effect=?,
            procedure=?,complications=?,contrast_type=?,sedation_note=?,post_care=?,
            expected_duration=?,estimated_cost=?,notes=?,updated_at=datetime('now')
            WHERE id=?""",
            tuple(d[k] for k in IMG_FIELDS) + (iid,))
        db.commit()
        flash("수정되었습니다.", "ok")
        return redirect(url_for("imaging_list"))
    return render_template("imaging_edit.html", item=row,
                           img_categories=IMG_CATEGORIES)


@app.route("/imaging/<int:iid>/delete", methods=["POST"])
@login_required
@admin_required
def imaging_delete(iid):
    get_db().execute("DELETE FROM imaging_exams WHERE id=?", (iid,))
    get_db().commit()
    flash("삭제되었습니다.", "ok")
    return redirect(url_for("imaging_list"))


@app.route("/api/imaging/<int:iid>")
@login_required
def api_imaging_detail(iid):
    row = get_db().execute("SELECT * FROM imaging_exams WHERE id=?", (iid,)).fetchone()
    if not row: return jsonify({"error":"not found"}), 404
    return jsonify(dict(row))


@app.route("/api/imaging/quick-add", methods=["POST"])
@login_required
def api_imaging_quick_add():
    """영상검사 upsert."""
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name: return jsonify({"error":"검사명 필수"}), 400
    db = get_db()
    existing = db.execute("SELECT id FROM imaging_exams WHERE name=?", (name,)).fetchone()
    if existing:
        db.execute("""UPDATE imaging_exams SET category=?, modality=?, purpose_effect=?,
            procedure=?, complications=?, contrast_type=?, sedation_note=?, post_care=?,
            expected_duration=?, estimated_cost=?, notes=?, updated_at=datetime('now')
            WHERE id=?""",
            (data.get("category") or "기타", data.get("modality") or "CT",
             data.get("purpose_effect",""), data.get("procedure",""),
             data.get("complications",""), data.get("contrast_type",""),
             data.get("sedation_note",""), data.get("post_care",""),
             data.get("expected_duration",""), data.get("estimated_cost",""),
             data.get("notes","AI 자동채움으로 업데이트됨"),
             existing["id"]))
        db.commit()
        return jsonify({"id": existing["id"], "existed": True, "updated": True})
    cur = db.execute("""INSERT INTO imaging_exams
        (name,category,modality,purpose_effect,procedure,complications,
         contrast_type,sedation_note,post_care,expected_duration,estimated_cost,notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (name, data.get("category") or "기타", data.get("modality") or "CT",
         data.get("purpose_effect",""), data.get("procedure",""),
         data.get("complications",""), data.get("contrast_type",""),
         data.get("sedation_note",""), data.get("post_care",""),
         data.get("expected_duration",""), data.get("estimated_cost",""),
         data.get("notes","AI 자동채움으로 추가됨")))
    db.commit()
    return jsonify({"id": cur.lastrowid, "existed": False, "updated": False})


IMG_AI_SYSTEM_PROMPT = """당신은 한국의 동물병원(소동물 수의학) 전문가입니다. 주어진 '영상검사(CT/MRI 등)'에 대해 보호자에게 설명할 동의서 내용을 작성합니다.

**검색 절차**:
1. web_search로 해당 검사의 일반적 마취 위험, 조영제 부작용 빈도, 소요시간, 비용(수의영상학 기준)을 찾으세요.
2. 수치가 있으면 반드시 포함하세요.

**출력 규칙** (매우 중요):
- 검색·추론이 끝나면 **최종 출력은 순수 JSON 객체 하나만**입니다.
- 서두 문장·요약·마크다운·코드블록·설명·이모지 등을 포함하지 마세요.
- 첫 문자는 반드시 '{' 이어야 하고, 마지막 문자는 '}' 이어야 합니다.
- JSON 내부 텍스트에 줄바꿈이 필요하면 \\n 이스케이프를 쓰세요.

**JSON 필드**:
- modality: "CT" / "MRI" / "CT,MRI" 중 하나
- purpose_effect: 검사의 목적·기대효과 (무엇을 평가하고 왜 하는지)
- procedure: 검사 방법 (마취·조영제 사용·소요시간·자세)
- complications: 예상 합병증 및 리스크. 마취 위험, 조영제 부작용, 방사선 피폭(CT) 또는 자기장 관련 금기(MRI, 체내 금속 이식물)
- contrast_type: "Iodine" (CT 조영) / "Gadolinium" (MRI 조영) / "비사용" / "선택적"
- sedation_note: 진정·마취 관련 주의사항 (금식 시간·사전 검사 필요성)
- post_care: 검사 후 주의사항 (마취 회복, 조영제 배출을 위한 수분 섭취 등)
- expected_duration: 예시 "15~30분", "30~60분"
- estimated_cost: 대한민국 원화 범위 (예: "60~120만원")
- category: "신경" / "복부" / "흉부" / "근골격" / "치과" / "종양" / "혈관" / "기타" 중 하나

보호자가 이해할 수 있는 평이한 한국어로, 그러나 치명적 위험은 명확히 고지하세요.
다시 강조: 최종 응답은 JSON 객체 하나만. 검색 결과 요약·서두 설명 절대 금지."""


@app.route("/api/imaging/ai-generate", methods=["POST"])
@login_required
def api_imaging_ai_generate():
    """Claude API로 영상검사 정보 자동 생성."""
    if requests is None:
        return jsonify({"error": "'requests' 패키지가 설치되어 있지 않습니다."}), 500
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "검사명을 입력하세요."}), 400
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
                "system": IMG_AI_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": f"영상검사명: {name}\n\n웹검색으로 해당 검사의 마취 위험·조영제 부작용·소요시간·비용을 확인하세요. 검색·추론이 끝나면 JSON 객체 하나만 출력하세요. 첫 문자는 '{{' 이어야 합니다."}],
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


# ===================== 영상검사 동의서 폼 / 미리보기 =====================

CONSENT_IMG_FIELDS = [
    "guardian_id","guardian_name","guardian_phone","guardian_mobile",
    "guardian_address","guardian_rrn","guardian_relation",
    "animal_id","rfid","species","breed","patient_name","age","sex","coat_color","weight",
    # 영상 촬영 마취 동의서 필드 (본문은 고정, 입력만 받음)
    "imaging_modalities",     # "CT", "MRI", "CT,MRI" 등 체크박스 결합
    "exam_name","exam_category","exam_side","exam_date","vet_name",
    "estimated_cost",
    "asa_grade","extra_note",
]


@app.route("/imaging/consent/new", methods=["GET"])
@login_required
def imaging_consent_new():
    exams = get_db().execute("SELECT id,name,category,modality FROM imaging_exams ORDER BY category,name").fetchall()
    return render_template("imaging_new.html", exams=exams,
                           img_categories=IMG_CATEGORIES,
                           vet_name=session.get("display_name",""),
                           today=datetime.now().strftime("%Y-%m-%d"))


def _render_imaging_print_from_data(data, db, signature_b64=None, signer_name=None, signed_at=None,
                                    show_sign_button=False, sign_interactive=False,
                                    checked_boxes=None):
    """영상검사 마취 동의서 data dict → imaging_print.html 렌더링.
    imaging_consent_preview와 sign 페이지에서 공통으로 사용."""
    tpl = db.execute("SELECT * FROM hospital_template WHERE id=1").fetchone()
    mod_label = (data.get("imaging_modalities") or "").replace(",", "·")
    if mod_label:
        doc_title = f"영상 촬영 마취 동의서 ({mod_label})"
    else:
        doc_title = "영상 촬영 마취 동의서"
    rendered = {
        "header": render_template_string(tpl["header_html"] or "", d=data, doc_title=doc_title),
        "footer": render_template_string(tpl["footer_html"] or "", d=data, doc_title=doc_title),
    }
    if signature_b64:
        data["signature_b64"] = signature_b64
        data["signer_name"] = signer_name or data.get("guardian_name", "")
        data["signed_at"] = signed_at or ""
    sign_form_fields = CONSENT_IMG_FIELDS + ["vet_name", "exam_date"]
    html = render_template(
        "imaging_print.html",
        d=data, r=rendered, doc_title=doc_title,
        show_sign_button=show_sign_button and not signature_b64,
        sign_interactive=sign_interactive,
        sign_form_fields=sign_form_fields,
        sign_form_action=url_for("imaging_consent_create_sign_link"),
    )
    if signature_b64:
        if checked_boxes:
            html = _apply_checked_boxes(html, set(checked_boxes))
        html = _strip_handwritten_signature(html)
    return html


@app.route("/imaging/consent/preview", methods=["POST"])
@login_required
def imaging_consent_preview():
    db = get_db()
    # imaging_modalities는 체크박스 복수 선택이라 getlist
    mods = request.form.getlist("imaging_modalities")
    data = {k: request.form.get(k, "") for k in CONSENT_IMG_FIELDS}
    data["imaging_modalities"] = ",".join(mods) if mods else request.form.get("imaging_modalities", "")
    data["vet_name"] = request.form.get("vet_name") or session.get("display_name", "")
    data["exam_date"] = request.form.get("exam_date") or datetime.now().strftime("%Y-%m-%d")
    data["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    return _render_imaging_print_from_data(data, db, show_sign_button=True)


# ===================== CE 보호자안내 생성 =====================

CE_GUARDIAN_PROMPT = """당신은 한국 동물병원 원장의 진료 경과를 보호자에게 설명하는 안내문을 작성하는 전문가입니다.
아래 제공되는 차트 내용(수의사가 작성한 진료 기록/수치/처치)을 근거로,
보호자가 이해하기 쉬운 "-" 글머리 기호 줄글 안내문을 작성하세요.

**톤과 양식 (매우 중요, 샘플을 그대로 따라갈 것)**:
- **환자 이름이 제공되면 첫 줄을 "[환자이름] 보호자님." 으로 인사**, 한 줄 공백 후 본문 시작
- 본문 시작: "[환자이름]는/은 금일 ~ 증상으로 내원하였습니다."
- 친절하고 설명적 — 어려운 용어는 괄호로 풀어서 설명
- 약물은 한글 설명명으로 기재 (예: "위장관보호제, 항구토제, 항생제")
- 질병·검사는 한글+영문 병기 허용: "신세포암종(Renal cell carcinoma)", "조혈촉진제(DPO)"
- 검사 수치는 정상 범위 함께 표기: "Creatinine이 2.3(정상 0.8~1.6)으로 상승"
- 집에서의 관리 방법(음수량, 약 급여 시점)과 다음 단계 안내(추가 검사, 재진)를 자연스럽게 포함
- 마지막 항목은 "~ 결정되시면 병원으로 연락 부탁 드립니다." 같은 후속 조치 안내

**출력 규칙**:
- "-" 글머리 기호 줄글만 사용 (헤더·인사·코드블록 금지)
- 차트에 없는 의학적 사실은 추측·추가 금지
- 정상 수치 범위는 차트에 명시가 있거나 일반 수의학 기준에서 확실한 값만 병기
- 첫 줄부터 바로 본문, 마무리 인사 없이 내용만

**참고 예시 (출력 형식을 이 톤/길이/구조로 맞추세요)**:
샤미 보호자님.

- 샤미는 금일 구토, 식욕부진 증상으로 내원하였습니다.
- 복부 방사선 및 복부 초음파 검사 상 좌측 신장에서 종양 의심 소견 관찰되었습니다.
- 고양이 신장에서 가장 흔하게 발생하는 악성 종양은 신세포암종(Renal cell carcinoma)와 신장 림프종이며, 두 종양 모두 예후가 짧은 편입니다.
- 신세포암종의 경우 수술적으로 좌측 신장 제거 진행하게 되며, 림프종의 경우 주사 항암 치료를 진행하게 됩니다.
- 두 종양을 감별하기 위해서는 세침흡인 검사가 필요하며, 진행 원하시는 경우에는 병원으로 예약 연락 부탁 드립니다.
- 혈액 검사 상 신장 수치 중 Creatinine이 2.3(정상 0.8~1.6)으로 상승된 것이 확인되었습니다. 탈수 지속되는 경우 신장 수치 상승 심화될 수 있어 원내에서 피하수액 진행하였습니다. 집에서는 물, 습식캔 등을 통해서 음수량 채울 수 있도록 해주세요.
- 추가적으로 복부 초음파 검사 상 소장 근층 전반적으로 비후되어 있는 것이 확인되었습니다. 만성 장염을 우선 고려할 수 있는 소견으로, 회복 돕기 위해 내복약(위장관보호제, 항구토제, 항생제) 처방하였습니다. 오늘 저녁약부터 급여해주세요.
- 고양이에서는 식욕 부진 지속되면 탈수, 빈혈 심화 뿐만 아니라 지방간으로 인해 간기능 저하 발생할 수 있습니다. 세침흡인 검사 진행 또는 스테로이드 처방 결정되시면 병원으로 연락 부탁 드립니다."""



CE_VET_PROMPT = """당신은 한국 동물병원의 수의사가 다른 의뢰 병원 원장님(수의사)에게 보내는 진료 경과 보고 메시지를 작성하는 전문가입니다.
아래 차트 내용을 근거로, 동료 수의사에게 간결·전문적으로 전달하는 메시지를 작성하세요.

**톤과 양식 (매우 중요, 샘플을 그대로 따라갈 것)**:
- 첫 줄: "안녕하세요 원장님." (의뢰 원장님 성명이 주어지면 "안녕하세요 [성명]원장님.")
- 둘째 줄: "의뢰주신 [보호자]님 [환자이름] 진료 경과 관련하여 연락 드립니다."
- 이후 "-" 글머리 기호 줄글
- **동료 수의사 대상이므로 영문 약어·약물명 적극 사용**:
  · 검사/수치: BUN, P, Crea, ALT, AST, HCT, CBC, CRP, FNA, PCR, US, Rad 등
  · 약물: AMC, Metro, Famo, Cerenia, PDS, DPO, EPO, Omep, Ursodeoxy 등
  · 질병/소견: CKD, HCM, DKA, IMHA, FIP, Lymphoma, RCC 등
- 검사 수치는 정상 범위 병기 불필요 (동료는 이미 알고 있음)
- 마지막 줄: "- 소중한 환자 의뢰해주셔서 감사합니다. 최선을 다해 진료 하도록 하겠습니다."

**출력 규칙**:
- 차트에 없는 의학적 사실은 추측·추가 금지
- 첫 두 줄(인사·의뢰 안내) → "-" 글머리 본문 → 마무리 인사 순서 엄수

**참고 예시 (출력 형식을 이 톤/길이/구조로 맞추세요)**:
안녕하세요 원장님. 의뢰주신 김하은님 샤미 진료 경과 관련하여 연락 드립니다.
- 샤미는 복부 방사선 및 복부 초음파 검사 상 좌측 신장에서 종양 의심 소견 관찰되었으며 혈액 검사 상 BUN, P 수치는 정상, Creatinine은 2.3으로 증가 확인되었습니다. 종양의 양상을 고려 시 신세포암종(Renal cell carcinoma) 혹은 신장 림프종 가능성 우선 고려되는 점 안내드렸으며, 두 종양을 감별하기 위해 세침흡인 검사 추천 드렸습니다.
- 보호자님 고민해보시고 결정하시기로 해서 FNA는 우선 보류하였습니다.
- 초음파 상 소장 근층 전반적으로 비후되어 있는 것이 확인되어 우선 장염 치료에 대해 AMC, Metro, Famo, Cerenia 3일분 처방하였으며, 3일간 고민해보신 후 결정되시면 재내원해주시기로 했습니다.
- 혹시 FNA 진행 안하시는 쪽으로 결정하시게 되면 호스피스 관리 차원에서 PDS 처방 고려할 예정입니다.
- 추가적으로 빈혈 수치 28.7%로 경도의 빈혈 확인되어 조혈촉진제(DPO), 철분제, 코발라민(비타민 B12) 주사 진행하였습니다.
- 소중한 환자 의뢰해주셔서 감사합니다. 최선을 다해 진료 하도록 하겠습니다."""


@app.route("/ce/new", methods=["GET"])
@login_required
def ce_new():
    return render_template("ce_new.html")


@app.route("/api/ce/generate", methods=["POST"])
@login_required
def api_ce_generate():
    """차트 내용으로 보호자안내 또는 의뢰병원용 CE 메시지 생성."""
    if requests is None:
        return jsonify({"error": "'requests' 패키지가 설치되어 있지 않습니다."}), 500
    data = request.get_json() or {}
    chart = (data.get("chart") or "").strip()
    mode = (data.get("mode") or "guardian").strip()  # "guardian" / "vet"
    ref_vet_name = (data.get("ref_vet_name") or "").strip()
    guardian_name = (data.get("guardian_name") or "").strip()
    patient_name = (data.get("patient_name") or "").strip()
    if not chart:
        return jsonify({"error": "차트 내용을 입력하세요."}), 400

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY 환경변수가 설정되어 있지 않습니다."}), 400

    system = CE_VET_PROMPT if mode == "vet" else CE_GUARDIAN_PROMPT
    meta_lines = []
    if guardian_name: meta_lines.append(f"보호자 성명: {guardian_name}")
    if patient_name:  meta_lines.append(f"환자 이름: {patient_name}")
    if mode == "vet" and ref_vet_name:
        meta_lines.append(f"의뢰 원장님 성명: {ref_vet_name}")
    meta_block = ("\n".join(meta_lines) + "\n\n") if meta_lines else ""

    user_msg = f"{meta_block}다음 차트 내용을 바탕으로 안내문을 작성해주세요.\n\n---\n{chart}\n---"
    if mode == "guardian" and patient_name:
        user_msg += f"\n\n(첫 줄은 '{patient_name} 보호자님.'으로 인사하고, 한 줄 띄운 뒤 본문을 '-' 글머리 줄글로 작성하세요.)"
    if mode == "vet":
        greeting = f"안녕하세요 {ref_vet_name}원장님." if ref_vet_name else "안녕하세요 원장님."
        g = guardian_name or "보호자"
        p = patient_name or "환자"
        user_msg += f"\n\n(첫 줄은 정확히 '{greeting}'으로, 둘째 줄은 '의뢰주신 {g}님 {p} 진료 경과 관련하여 연락 드립니다.'로 시작하세요.)"

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
                "max_tokens": 4000,
                "system": system,
                "messages": [{"role": "user", "content": user_msg}],
            },
            timeout=90,
        )
        if r.status_code != 200:
            return jsonify({"error": f"Claude API 오류 {r.status_code}: {r.text[:300]}"}), 500
        body = r.json()
        text_parts = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
        text = "\n".join(text_parts).strip()
        # 마크다운 코드블록 제거
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith(("text", "markdown", "md")):
                text = text.split("\n", 1)[1] if "\n" in text else ""
            text = text.strip().rstrip("`").strip()
        return jsonify({"ok": True, "text": text, "mode": mode})
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
