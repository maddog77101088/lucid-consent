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
    # surgeries 마이그레이션: post_op_notes (수술후 기본 안내)
    cur.execute("PRAGMA table_info(surgeries)")
    sg_cols = [c[1] for c in cur.fetchall()]
    if "post_op_notes" not in sg_cols:
        cur.execute("ALTER TABLE surgeries ADD COLUMN post_op_notes TEXT DEFAULT ''")
    # hospitalizations 마이그레이션: discharge_notes (내과 퇴원 기본 안내)
    cur.execute("PRAGMA table_info(hospitalizations)")
    hp_cols = [c[1] for c in cur.fetchall()]
    if "discharge_notes" not in hp_cols:
        cur.execute("ALTER TABLE hospitalizations ADD COLUMN discharge_notes TEXT DEFAULT ''")
    # 해피콜 테이블
    cur.execute("""
        CREATE TABLE IF NOT EXISTS happy_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_type TEXT NOT NULL,
            patient_name TEXT,
            guardian_name TEXT,
            guardian_phone TEXT,
            diagnosis TEXT,
            vet_name TEXT,
            assignee_id INTEGER,
            scheduled_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            call_memo TEXT,
            doc_body TEXT,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            completed_at TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_happycall_date ON happy_calls(scheduled_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_happycall_status ON happy_calls(status)")
    # happy_calls 마이그레이션: 카톡 워크플로 컬럼 추가
    cur.execute("PRAGMA table_info(happy_calls)")
    hc_cols = [c[1] for c in cur.fetchall()]
    for col, ddl in [
        ("draft_message", "ALTER TABLE happy_calls ADD COLUMN draft_message TEXT"),
        ("approved_message", "ALTER TABLE happy_calls ADD COLUMN approved_message TEXT"),
        ("approved_at", "ALTER TABLE happy_calls ADD COLUMN approved_at TEXT"),
        ("approved_by", "ALTER TABLE happy_calls ADD COLUMN approved_by INTEGER"),
        ("sent_at", "ALTER TABLE happy_calls ADD COLUMN sent_at TEXT"),
        ("sent_by", "ALTER TABLE happy_calls ADD COLUMN sent_by INTEGER"),
        ("reply_received_at", "ALTER TABLE happy_calls ADD COLUMN reply_received_at TEXT"),
    ]:
        if col not in hc_cols:
            cur.execute(ddl)
    con.commit()
    # 통합 환자 문서 테이블 (환자별·질환별 히스토리 + 미래 AI 추천용)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS patient_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_type TEXT NOT NULL,
            patient_chart_id TEXT,
            patient_name TEXT NOT NULL,
            species TEXT,
            breed TEXT,
            age TEXT,
            sex TEXT,
            guardian_name TEXT,
            guardian_phone TEXT,
            diagnosis TEXT,
            surgery_id INTEGER,
            hospitalization_id INTEGER,
            tags TEXT,
            title TEXT,
            body TEXT,
            structured_data TEXT,
            vet_name TEXT,
            related_consent_token TEXT,
            related_happycall_id INTEGER,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pd_chart ON patient_documents(patient_chart_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pd_name ON patient_documents(patient_name)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pd_doctype ON patient_documents(doc_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pd_created ON patient_documents(created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pd_diagnosis ON patient_documents(diagnosis)")
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
    # 해피콜: 오늘 예정 건 + 밀린 건 (지난 날짜 미완료)
    today_str = datetime.now().strftime("%Y-%m-%d")
    # 카톡 안부 stats
    hc_pending_draft = db.execute(
        "SELECT COUNT(*) FROM happy_calls WHERE status='pending_draft'"
    ).fetchone()[0]
    hc_drafted = db.execute(
        "SELECT COUNT(*) FROM happy_calls WHERE status='drafted'"
    ).fetchone()[0]
    hc_approved = db.execute(
        "SELECT COUNT(*) FROM happy_calls WHERE status='approved'"
    ).fetchone()[0]
    # 호환성을 위해 기존 필드도 유지 (legacy)
    hc_today = hc_pending_draft + hc_drafted
    hc_overdue = hc_approved
    return render_template("dashboard.html", counts=counts, total=sum(counts.values()),
                           pending_cnt=pending_cnt, signed_cnt=signed_cnt,
                           hc_today=hc_today, hc_overdue=hc_overdue,
                           hc_pending_draft=hc_pending_draft,
                           hc_drafted=hc_drafted,
                           hc_approved=hc_approved)


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
    "anesthesia_risk","estimated_cost","hospitalization","expected_duration","notes",
    "post_op_notes"]


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
                 estimated_cost,hospitalization,expected_duration,notes,post_op_notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
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
            expected_duration=?,notes=?,post_op_notes=?,updated_at=datetime('now') WHERE id=?""",
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
    _save_patient_document(
        "consent_imaging", data.get("patient_name", ""),
        species=data.get("species",""), breed=data.get("breed",""),
        age=data.get("age",""), sex=data.get("sex",""),
        guardian_name=data.get("guardian_name",""), guardian_phone=data.get("guardian_mobile",""),
        diagnosis=data.get("imaging_modalities", "") if "imaging_modalities" else "",
        title="영상검사 동의서",
        tags=data.get("imaging_modalities", ""),
        related_consent_token=token,
        structured_data=data,
    )

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


# ===================== 퇴원 요청 및 서약서 =====================
# 보호자 요청에 의한 조기퇴원용 (치료 포기) 서약서.

DISCHARGE_FIELDS = [
    "guardian_id", "guardian_name", "guardian_phone", "guardian_mobile",
    "guardian_address",
    "animal_id", "rfid", "patient_name", "species", "breed",
    "age", "sex", "color",
    "diagnosis", "discharge_reason",
]


def _render_discharge_print_from_data(data, db, signature_b64=None, signer_name=None,
                                      signed_at=None, show_sign_button=False,
                                      sign_interactive=False):
    """퇴원 요청 및 서약서 렌더링."""
    doc_title = "퇴원 요청 및 서약서"
    if signature_b64:
        data["signature_b64"] = signature_b64
        data["signer_name"] = signer_name or data.get("guardian_name", "")
        data["signed_at"] = signed_at or ""
    sign_form_fields = DISCHARGE_FIELDS + ["vet_name", "doc_date"]
    return render_template(
        "discharge_print.html",
        d=data, doc_title=doc_title,
        show_sign_button=show_sign_button and not signature_b64,
        sign_interactive=sign_interactive,
        sign_form_fields=sign_form_fields,
        sign_form_action=url_for("discharge_create_sign_link"),
        hospital_name=HOSPITAL_NAME,
    )


@app.route("/discharge/new", methods=["GET"])
@login_required
def discharge_new():
    return render_template(
        "discharge_new.html",
        today=datetime.now().strftime("%Y-%m-%d"),
        vet_name=session.get("display_name", ""),
    )


@app.route("/discharge/preview", methods=["POST"])
@login_required
def discharge_preview():
    db = get_db()
    data = {k: request.form.get(k, "") for k in DISCHARGE_FIELDS}
    data["vet_name"] = request.form.get("vet_name") or session.get("display_name", "")
    data["doc_date"] = request.form.get("doc_date") or datetime.now().strftime("%Y-%m-%d")
    data["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    return _render_discharge_print_from_data(data, db, show_sign_button=True)


@app.route("/discharge/create-sign-link", methods=["POST"])
@login_required
def discharge_create_sign_link():
    """퇴원 서약서 폼 → 서명 토큰 생성."""
    db = get_db()
    data = {k: request.form.get(k, "") for k in DISCHARGE_FIELDS}
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
        (token, "discharge", json.dumps(data, ensure_ascii=False),
         data.get("patient_name", ""), data.get("guardian_name", ""),
         data.get("vet_name", ""), expires_at, session.get("user_id", 0))
    )
    db.commit()
    _save_patient_document(
        "consent_discharge", data.get("patient_name", ""),
        species=data.get("species",""), breed=data.get("breed",""),
        age=data.get("age",""), sex=data.get("sex",""),
        guardian_name=data.get("guardian_name",""), guardian_phone=data.get("guardian_mobile",""),
        diagnosis=data.get("diagnosis", "") if "diagnosis" else "",
        title="퇴원 요청 및 서약서",
        tags="조기퇴원",
        related_consent_token=token,
        structured_data=data,
    )

    sign_url = f"{_sign_base_url()}/sign/{token}"
    return jsonify({
        "ok": True,
        "token": token,
        "url": sign_url,
        "qr_b64": _qr_base64(sign_url),
        "expires_at": expires_at,
        "ttl_hours": SIGN_LINK_TTL_HOURS,
        "doc_type": "discharge",
    })


# ===================== 치료비 미수금 지불 서약서 =====================
# 미납 진료비에 대한 납입 약속 서약서. 작성일 + 유예일수 → 납입기한 자동 계산.

PAYMENT_FIELDS = [
    "guardian_id", "guardian_name", "guardian_phone", "guardian_mobile",
    "guardian_address",
    "animal_id", "patient_name", "species", "breed",
    "unpaid_amount", "grace_days", "reason",
]


def _compute_due_date(doc_date_str, grace_days):
    """작성일(YYYY-MM-DD) + 유예일수 → 납입 기한 (date + 표시문자열)."""
    try:
        base = datetime.strptime(doc_date_str, "%Y-%m-%d")
        days = int(grace_days or 0)
        if days < 1: days = 0
        due = base + timedelta(days=days)
        weekday = ['월','화','수','목','금','토','일'][due.weekday()]
        return due.strftime("%Y-%m-%d"), f"{due.year}년 {due.month:02d}월 {due.day:02d}일 ({weekday})"
    except (ValueError, TypeError):
        return "", ""


def _render_payment_print_from_data(data, db, signature_b64=None, signer_name=None,
                                    signed_at=None, show_sign_button=False,
                                    sign_interactive=False):
    """치료비 미수금 지불 서약서 렌더링."""
    doc_title = "치료비 미수금 지불 서약서"
    # 납입기한 계산 (작성일 + grace_days)
    due_iso, due_disp = _compute_due_date(data.get("doc_date", ""), data.get("grace_days", 0))
    data["due_date_iso"] = due_iso
    data["due_date_display"] = due_disp
    if signature_b64:
        data["signature_b64"] = signature_b64
        data["signer_name"] = signer_name or data.get("guardian_name", "")
        data["signed_at"] = signed_at or ""
    sign_form_fields = PAYMENT_FIELDS + ["vet_name", "doc_date"]
    return render_template(
        "payment_print.html",
        d=data, doc_title=doc_title,
        show_sign_button=show_sign_button and not signature_b64,
        sign_interactive=sign_interactive,
        sign_form_fields=sign_form_fields,
        sign_form_action=url_for("payment_create_sign_link"),
        hospital_name=HOSPITAL_NAME,
    )


@app.route("/payment/new", methods=["GET"])
@login_required
def payment_new():
    return render_template(
        "payment_new.html",
        today=datetime.now().strftime("%Y-%m-%d"),
        vet_name=session.get("display_name", ""),
    )


@app.route("/payment/preview", methods=["POST"])
@login_required
def payment_preview():
    db = get_db()
    data = {k: request.form.get(k, "") for k in PAYMENT_FIELDS}
    data["vet_name"] = request.form.get("vet_name") or session.get("display_name", "")
    data["doc_date"] = request.form.get("doc_date") or datetime.now().strftime("%Y-%m-%d")
    data["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    return _render_payment_print_from_data(data, db, show_sign_button=True)


@app.route("/payment/create-sign-link", methods=["POST"])
@login_required
def payment_create_sign_link():
    """미수금 서약서 폼 → 서명 토큰 생성."""
    db = get_db()
    data = {k: request.form.get(k, "") for k in PAYMENT_FIELDS}
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
        (token, "payment", json.dumps(data, ensure_ascii=False),
         data.get("patient_name", ""), data.get("guardian_name", ""),
         data.get("vet_name", ""), expires_at, session.get("user_id", 0))
    )
    db.commit()
    _save_patient_document(
        "consent_payment", data.get("patient_name", ""),
        species=data.get("species",""), breed=data.get("breed",""),
        age=data.get("age",""), sex=data.get("sex",""),
        guardian_name=data.get("guardian_name",""), guardian_phone=data.get("guardian_mobile",""),
        diagnosis=data.get("", "") if "" else "",
        title="치료비 미수금 지불 서약서",
        tags="미수금",
        related_consent_token=token,
        structured_data=data,
    )

    sign_url = f"{_sign_base_url()}/sign/{token}"
    return jsonify({
        "ok": True,
        "token": token,
        "url": sign_url,
        "qr_b64": _qr_base64(sign_url),
        "expires_at": expires_at,
        "ttl_hours": SIGN_LINK_TTL_HOURS,
        "doc_type": "payment",
    })


# ===================== 수술후 안내문 (AI 자동생성) =====================
# B+C 방식: 수술 DB의 post_op_notes 기본값 + 환자 특이사항 → Claude 로 보호자용 안내문 작성.

POSTOP_PROMPT = """당신은 한국 동물병원의 수의사가 수술 후 퇴원하는 환자의 보호자에게 전달할 안내문을 작성하는 전문가입니다.

**톤과 양식 (매우 중요)**:
- 첫 줄: "[환자이름] 보호자님께"
- 둘째 줄(빈 줄)
- 셋째 줄: "[환자이름]는 오늘 [수술명]을 잘 마치고 퇴원합니다. 집에서 아래 사항들을 꼭 지켜주세요."
- 이후 각 섹션은 이모지 제목 + "-" 글머리 본문
- 부드럽고 안심되는 어투. 하지만 주의사항은 명확히.
- 전문 용어 대신 쉬운 말 (예: "경구 투여" → "먹이기", "식욕부진" → "밥을 잘 안 먹음")

**섹션 구성 (입원 기간에 따라 다름)**:

[당일 퇴원인 경우 — 마취 회복 내용 포함]
1. 📌 오늘~내일 (마취 여전히 남아있을 수 있음 → 조용한 곳에서 휴식, 비틀거림·혀 깨물기 주의)
2. 🍚 먹이기 (오늘 저녁: 평소의 1/4~1/3 소량 / 내일 아침: 절반 정도로 증량 / 물은 소량씩 자유)
3. 💊 약 복용
4. 🩹 상처 관리 & E-collar (입력된 소독·연고·붕대 지침을 정확히 반영)
5. 🐾 활동 제한
6. 🚨 응급 증상 시 즉시 연락
7. 📅 다음 내원 일정
8. 마무리

[입원 후 퇴원인 경우 — 마취 회복 내용 생략]
1. 📌 집에서의 관리 시작 (입원 중 잘 회복됨, 집에서 이어서 관리할 포인트)
2. 🍚 먹이기 (평소 식사로 복귀 or 조심할 점)
3. 💊 약 복용
4. 🩹 상처 관리 & E-collar (입력된 소독·연고·붕대 지침을 정확히 반영)
5. 🐾 활동 제한
6. 🚨 응급 증상 시 즉시 연락
7. 📅 다음 내원 일정
8. 마무리

**퇴원 당시 환자 상태에 따른 톤 조절 (매우 중요)**:
- "좋은 경과로 퇴원": 안심되는 부드러운 톤. "잘 회복했어요, 집에서도 잘 지낼 수 있을 거예요" 느낌.
  주의사항은 일반적인 수준으로 간결하게.
- "회복이 지연 중 퇴원": 차분하고 주의 깊은 톤. 평소보다 세심히 관찰해야 한다는 점을 자연스럽게 전달.
  응급 증상 섹션을 조금 더 자세히. 추가 연락 기준을 낮게 설정 (예: "조금이라도 이상하면").
- "상태 악화 중 퇴원": 정직하고 진지한 톤. 안심시키지 말 것. 보호자 주의가 매우 필요함을 명확히.
  응급 증상을 매우 구체적으로, 기준을 낮게 (예: "식욕 감소·활동량 감소만 있어도 즉시 연락").
  "언제든 병원으로 연락" 을 여러 번 강조.

**출력 규칙**:
- 마크다운 코드블록·헤더(#) 금지. 이모지 + 줄글만.
- 입력된 정보(약 이름·일수·날짜)는 정확히 그대로 반영.
- 입력에 없는 정보는 추측 금지. 일반적으로 통용되는 상식 수준만 추가.
- 응급 증상은 보호자가 즉시 알아차릴 수 있도록 구체적으로 (색·냄새·빈도 등).
- 마지막 줄: "궁금한 점이 있으시면 언제든 병원으로 연락 주세요. 02-941-7900 · 24시 루시드 동물병원"
"""


@app.route("/postop/new", methods=["GET"])
@login_required
def postop_new():
    db = get_db()
    surgeries = db.execute(
        "SELECT id,name,category FROM surgeries ORDER BY category,name"
    ).fetchall()
    return render_template("postop_new.html",
                           surgeries=surgeries,
                           today=datetime.now().strftime("%Y-%m-%d"),
                           vet_name=session.get("display_name", ""))


@app.route("/api/postop/generate", methods=["POST"])
@login_required
def api_postop_generate():
    """구조화 입력 + 수술 DB post_op_notes → Claude 로 보호자 안내문 생성."""
    if requests is None:
        return jsonify({"error": "'requests' 패키지가 설치되어 있지 않습니다."}), 500
    data = request.get_json() or {}
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY 환경변수 미설정"}), 400

    patient = (data.get("patient_name") or "").strip()
    guardian = (data.get("guardian_name") or "").strip()
    species = (data.get("species") or "").strip()
    age = (data.get("age") or "").strip()
    surgery_name = (data.get("surgery_name") or "").strip()
    diagnosis = (data.get("diagnosis") or "").strip()
    medications = (data.get("medications") or "").strip()
    med_days = (data.get("med_days") or "").strip()
    ecollar_days = (data.get("ecollar_days") or "").strip()
    activity_limit_days = (data.get("activity_limit_days") or "").strip()
    suture_remove_date = (data.get("suture_remove_date") or "").strip()
    followup_note = (data.get("followup_note") or "").strip()
    db_postop = (data.get("db_postop_notes") or "").strip()
    special_notes = (data.get("special_notes") or "").strip()
    hospitalization_days = (data.get("hospitalization_days") or "").strip()
    wound_disinfect = (data.get("wound_disinfect") or "").strip()
    wound_ointment = (data.get("wound_ointment") or "").strip()
    wound_ointment_name = (data.get("wound_ointment_name") or "").strip()
    wound_bandage = (data.get("wound_bandage") or "").strip()
    discharge_status = (data.get("discharge_status") or "good").strip()

    if not patient or not surgery_name:
        return jsonify({"error": "환자명과 수술명은 필수입니다."}), 400

    meta = []
    if patient: meta.append(f"- 환자 이름: {patient}")
    if species: meta.append(f"- 종: {species}")
    if age: meta.append(f"- 나이: {age}")
    if guardian: meta.append(f"- 보호자: {guardian}")
    if surgery_name: meta.append(f"- 수술명: {surgery_name}")
    if diagnosis: meta.append(f"- 진단명: {diagnosis}")

    extra = []
    if medications:
        extra.append(f"- 처방 약물: {medications}" + (f" (복용 기간: {med_days}일)" if med_days else ""))
    if ecollar_days:
        extra.append(f"- E-collar 착용 기간: {ecollar_days}일")
    if activity_limit_days:
        extra.append(f"- 활동 제한 기간: {activity_limit_days}일")
    if suture_remove_date:
        extra.append(f"- 실밥 제거 예정일: {suture_remove_date}")
    if followup_note:
        extra.append(f"- 추가 재진 안내: {followup_note}")

    # 입원 기간 (0 또는 빈값 = 당일 퇴원)
    try:
        hosp_days_int = int(hospitalization_days) if hospitalization_days else 0
    except (ValueError, TypeError):
        hosp_days_int = 0
    if hosp_days_int == 0:
        extra.append("- 입원 기간: 당일 퇴원 (수술 당일 집으로 귀가)")
    else:
        extra.append(f"- 입원 기간: 수술 후 {hosp_days_int}일 입원, 오늘 퇴원 (마취 회복 완료 상태)")

    # 퇴원 당시 환자 상태 (AI 톤 조절용)
    status_label = {
        "good": "좋은 경과로 퇴원 — 수술·회복이 순조로움. 일반적인 주의사항 위주로 안심 톤.",
        "delayed": "회복이 지연 중 퇴원 — 평소보다 주의 깊은 관찰 필요. 정상 회복 범위 벗어나는 징후를 보호자가 알아차릴 수 있게 구체적으로 안내.",
        "worsening": "상태 악화 중 퇴원 — 적극적 관찰 필요. 응급 증상 섹션을 더 자세히, 낮은 기준으로 병원 연락하도록 강조. 지나치게 안심시키지 말고 정직하되 부드럽게.",
    }.get(discharge_status, "")
    if status_label:
        extra.append(f"- 퇴원 당시 환자 상태: {status_label}")

    # 상처관리 구조화
    wound_parts = []
    disinfect_label = {
        "twice": "하루 두 번 소독",
        "daily": "하루 한 번 소독",
        "asneeded": "필요 시에만 소독",
        "none": "소독 불필요",
    }.get(wound_disinfect, "")
    if disinfect_label:
        wound_parts.append(f"  · 소독: {disinfect_label}")
    if wound_ointment == "yes":
        wound_parts.append(f"  · 연고 도포: {wound_ointment_name or '처방 연고'} 사용")
    bandage_label = {
        "daily": "매일 교체",
        "2-3d": "2~3일마다 교체",
        "waterproof": "방수 스프레이 도포 (교체 불필요)",
        "none": "붕대·드레싱 없음",
    }.get(wound_bandage, "")
    if bandage_label:
        wound_parts.append(f"  · 붕대/드레싱: {bandage_label}")
    if wound_parts:
        extra.append("- 상처 관리 지침:\n" + "\n".join(wound_parts))

    user_msg_parts = ["[환자 및 수술 정보]", "\n".join(meta)]
    if extra:
        user_msg_parts += ["\n[이번 환자의 처방·관리 조건]", "\n".join(extra)]
    if db_postop:
        user_msg_parts += ["\n[병원 DB의 해당 수술 기본 주의사항]", db_postop]
    if special_notes:
        user_msg_parts += ["\n[이번 환자 특이사항]", special_notes]
    user_msg_parts.append("\n위 정보를 모두 반영하여 보호자용 퇴원 안내문을 작성해주세요.")
    user_msg = "\n".join(user_msg_parts)

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
                "system": POSTOP_PROMPT,
                "messages": [{"role": "user", "content": user_msg}],
            },
            timeout=90,
        )
        if r.status_code != 200:
            return jsonify({"error": f"Claude API 오류 {r.status_code}: {r.text[:300]}"}), 500
        body = r.json()
        text_parts = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
        text = "\n".join(text_parts).strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith(("text","markdown","md")):
                text = text.split("\n", 1)[1] if "\n" in text else ""
            text = text.strip().rstrip("`").strip()
        # 통합 환자 문서 저장
        _save_patient_document(
            "postop", patient,
            species=species, age=age,
            guardian_name=guardian,
            diagnosis=(diagnosis or surgery_name),
            title=surgery_name,
            body=text,
            tags=surgery_name,
            structured_data={
                "surgery_name": surgery_name, "diagnosis": diagnosis,
                "medications": medications, "med_days": med_days,
                "ecollar_days": ecollar_days, "activity_limit_days": activity_limit_days,
                "suture_remove_date": suture_remove_date, "followup_note": followup_note,
                "hospitalization_days": hospitalization_days,
                "discharge_status": discharge_status,
                "wound_disinfect": wound_disinfect, "wound_ointment": wound_ointment,
                "wound_ointment_name": wound_ointment_name, "wound_bandage": wound_bandage,
                "special_notes": special_notes,
            },
        )
        return jsonify({"ok": True, "text": text})
    except Exception as e:
        return jsonify({"error": f"AI 요청 실패: {e}"}), 500


@app.route("/api/surgery/<int:sid>/postop", methods=["GET"])
@login_required
def api_surgery_postop(sid):
    """수술 DB 에서 post_op_notes 반환."""
    row = get_db().execute("SELECT post_op_notes FROM surgeries WHERE id=?", (sid,)).fetchone()
    if not row: return jsonify({"error": "not found"}), 404
    return jsonify({"post_op_notes": row["post_op_notes"] or ""})

# ===================== 내과 퇴원 안내문 (AI 자동생성) =====================
# 입원 DB의 discharge_notes + 환자 진단·처방·모니터링 → Claude 로 보호자용 내과 퇴원 안내문 작성.

IMD_PROMPT = """당신은 한국 동물병원의 수의사가 내과 입원 치료 후 퇴원하는 환자의 보호자에게 전달할 안내문을 작성하는 전문가입니다.

**톤과 양식 (매우 중요)**:
- 첫 줄: "[환자이름] 보호자님께"
- 둘째 줄(빈 줄)
- 셋째 줄: "[환자이름]는 [입원기간]일간 입원 치료 후 오늘 퇴원합니다. 집에서의 관리 안내드립니다."
- 이후 각 섹션은 이모지 제목 + "-" 글머리 본문
- 부드럽고 안심되는 어투. 보호자가 차근차근 따라할 수 있도록 구체적으로.
- 전문 용어 대신 쉬운 말 (예: "BUN 상승" → "신장 수치가 약간 높음", "경구 투여" → "먹이기")

**섹션 순서 (빠지지 않게)**:
1. 📌 진단 및 현재 상태 (한두 문장 요약, 보호자가 이해할 수 있게)
2. 💊 약 복용 (처방된 약 이름·횟수·기간 정확히. 만성투약은 "재진까지" "지속적으로" 등 자연스럽게 표현)
3. 🍽 처방식 / 식이 관리 (처방식 있으면 종류·양·횟수. 없으면 일반 식이 주의점)
4. 👀 집에서 관찰할 항목 (입력된 모니터링 항목들을 보호자가 매일 체크할 수 있게 풀어쓰기.
   각 항목별로 "정상 범위" 또는 "이렇게 하면 됨" 가이드 포함. 예: 호흡수 = "안정 시 1분간 30회 이하")
5. 🚨 즉시 연락할 응급 증상 (해당 진단명에 특화된 응급 신호. 보호자가 알아차릴 수 있도록 구체적으로.
   진단명별 예: 신부전→구토 지속·식욕 완전 X / 심장병→호흡곤란·잇몸 청색증 / 당뇨→떨림·의식저하 / 췌장염→심한 구토·복통)
6. 📅 다음 내원 일정 (입력된 1차/2차 재진 정확히 반영)
7. 마무리 (궁금한 점 언제든 연락 + 병원 전화번호)

**퇴원 당시 상태에 따른 톤 조절**:
- "좋은 경과로 퇴원": 안심되는 톤. "잘 회복했어요" 느낌.
- "회복 지연 중": 차분히 주의 깊게. 응급 증상 기준 낮게.
- "상태 악화 중": 정직하고 진지하게. 안심시키지 말고 적극적 관찰 강조.

**출력 규칙**:
- 마크다운 코드블록·헤더(#) 금지. 이모지 + 줄글만.
- 입력된 정보(약 이름·기간·날짜)는 정확히 반영.
- 입력에 없는 정보는 추측 금지. 일반적 상식 수준만 추가.
- 마지막 줄: "궁금한 점이 있으시면 언제든 병원으로 연락 주세요. 02-941-7900 · 24시 루시드 동물병원"
"""


@app.route("/imd/new", methods=["GET"])
@login_required
def imd_new():
    db = get_db()
    cases = db.execute(
        "SELECT id,name,category FROM hospitalizations ORDER BY category,name"
    ).fetchall()
    return render_template("imd_new.html",
                           cases=cases,
                           today=datetime.now().strftime("%Y-%m-%d"),
                           vet_name=session.get("display_name", ""))


@app.route("/api/imd/generate", methods=["POST"])
@login_required
def api_imd_generate():
    """내과 퇴원 안내문 AI 생성."""
    if requests is None:
        return jsonify({"error": "'requests' 패키지 미설치"}), 500
    data = request.get_json() or {}
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY 환경변수 미설정"}), 400

    patient = (data.get("patient_name") or "").strip()
    guardian = (data.get("guardian_name") or "").strip()
    species = (data.get("species") or "").strip()
    age = (data.get("age") or "").strip()
    diagnosis = (data.get("diagnosis") or "").strip()
    medications = (data.get("medications") or "").strip()
    med_duration = (data.get("med_duration") or "").strip()
    diet = (data.get("diet") or "").strip()
    monitoring_items = data.get("monitoring_items") or []
    if isinstance(monitoring_items, str):
        monitoring_items = [monitoring_items]
    followup1_date = (data.get("followup1_date") or "").strip()
    followup1_purpose = (data.get("followup1_purpose") or "").strip()
    followup2_date = (data.get("followup2_date") or "").strip()
    followup2_purpose = (data.get("followup2_purpose") or "").strip()
    hospitalization_days = (data.get("hospitalization_days") or "").strip()
    discharge_status = (data.get("discharge_status") or "good").strip()
    db_discharge_notes = (data.get("db_discharge_notes") or "").strip()
    special_notes = (data.get("special_notes") or "").strip()

    if not patient or not diagnosis:
        return jsonify({"error": "환자명과 진단명은 필수입니다."}), 400

    meta = []
    if patient: meta.append(f"- 환자 이름: {patient}")
    if species: meta.append(f"- 종: {species}")
    if age: meta.append(f"- 나이: {age}")
    if guardian: meta.append(f"- 보호자: {guardian}")
    meta.append(f"- 진단명: {diagnosis}")

    extra = []
    if hospitalization_days:
        extra.append(f"- 입원 기간: {hospitalization_days}일")
    status_label = {
        "good": "좋은 경과로 퇴원 — 회복 순조로움. 안심되는 톤.",
        "delayed": "회복 지연 중 — 평소보다 주의 깊은 관찰 필요. 응급 기준 낮게.",
        "worsening": "상태 악화 중 — 적극적 관찰 필요. 안심시키지 말고 정직하게.",
    }.get(discharge_status, "")
    if status_label:
        extra.append(f"- 퇴원 당시 환자 상태: {status_label}")

    if medications:
        extra.append(f"- 처방 약물: {medications}" + (f" (복용 기간: {med_duration})" if med_duration else ""))
    if diet:
        extra.append(f"- 처방식 / 식이 관리: {diet}")
    if monitoring_items:
        extra.append(f"- 보호자가 집에서 매일 모니터링할 항목: {', '.join(monitoring_items)}")
    if followup1_date or followup1_purpose:
        f1 = f"1차 재진: {followup1_date}" + (f" ({followup1_purpose})" if followup1_purpose else "")
        extra.append(f"- {f1}")
    if followup2_date or followup2_purpose:
        f2 = f"2차 재진: {followup2_date}" + (f" ({followup2_purpose})" if followup2_purpose else "")
        extra.append(f"- {f2}")

    user_msg_parts = ["[환자 및 진단 정보]", "\n".join(meta)]
    if extra:
        user_msg_parts += ["\n[이번 환자의 처방·관리 조건]", "\n".join(extra)]
    if db_discharge_notes:
        user_msg_parts += ["\n[병원 DB의 해당 진단 기본 퇴원 안내]", db_discharge_notes]
    if special_notes:
        user_msg_parts += ["\n[이번 환자 특이사항]", special_notes]
    user_msg_parts.append("\n위 정보를 모두 반영하여 보호자용 내과 퇴원 안내문을 작성해주세요.")
    user_msg = "\n".join(user_msg_parts)

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
                "system": IMD_PROMPT,
                "messages": [{"role": "user", "content": user_msg}],
            },
            timeout=90,
        )
        if r.status_code != 200:
            return jsonify({"error": f"Claude API 오류 {r.status_code}: {r.text[:300]}"}), 500
        body = r.json()
        text_parts = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
        text = "\n".join(text_parts).strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith(("text","markdown","md")):
                text = text.split("\n", 1)[1] if "\n" in text else ""
            text = text.strip().rstrip("`").strip()
        # 통합 환자 문서 저장
        _save_patient_document(
            "imd", patient,
            species=species, age=age,
            guardian_name=guardian,
            diagnosis=diagnosis,
            title=diagnosis,
            body=text,
            tags=diagnosis,
            structured_data={
                "diagnosis": diagnosis,
                "medications": medications, "med_duration": med_duration,
                "diet": diet, "monitoring_items": monitoring_items,
                "followup1_date": followup1_date, "followup1_purpose": followup1_purpose,
                "followup2_date": followup2_date, "followup2_purpose": followup2_purpose,
                "hospitalization_days": hospitalization_days,
                "discharge_status": discharge_status,
                "special_notes": special_notes,
            },
        )
        return jsonify({"ok": True, "text": text})
    except Exception as e:
        return jsonify({"error": f"AI 요청 실패: {e}"}), 500


@app.route("/api/hospitalization/<int:hid>/discharge", methods=["GET"])
@login_required
def api_hospitalization_discharge(hid):
    """입원 DB 의 discharge_notes 만 반환 (imd 폼에서 케이스 선택 시)."""
    row = get_db().execute("SELECT discharge_notes FROM hospitalizations WHERE id=?", (hid,)).fetchone()
    if not row: return jsonify({"error": "not found"}), 404
    return jsonify({"discharge_notes": row["discharge_notes"] or ""})

# ===================== 해피콜 관리 =====================
# CE · 수술후 · 내과 퇴원 안내문 생성 시 자동 등록되는 follow-up 통화 리스트.

HAPPYCALL_DEFAULT_DAYS = {"ce": 1, "postop": 2, "imd": 3}
HAPPYCALL_DOC_LABELS = {"ce": "진료안내문(CE)", "postop": "수술후 안내문", "imd": "내과 퇴원 안내문"}


@app.route("/api/happy-calls/create", methods=["POST"])
@login_required
def api_happy_call_create():
    """안내문 생성 시 자동으로 해피콜 등록. JSON body.
    필수: doc_type, patient_name
    선택: guardian_name, guardian_phone, diagnosis, vet_name, doc_body, days_offset
    """
    data = request.get_json() or {}
    doc_type = (data.get("doc_type") or "").strip()
    if doc_type not in ("ce", "postop", "imd"):
        return jsonify({"ok": False, "error": "doc_type invalid"}), 400
    patient = (data.get("patient_name") or "").strip()
    if not patient:
        return jsonify({"ok": False, "error": "환자명 누락"}), 400

    days_offset = data.get("days_offset")
    try:
        days_offset = int(days_offset) if days_offset is not None else HAPPYCALL_DEFAULT_DAYS.get(doc_type, 1)
    except (ValueError, TypeError):
        days_offset = HAPPYCALL_DEFAULT_DAYS.get(doc_type, 1)

    scheduled = (datetime.now() + timedelta(days=days_offset)).strftime("%Y-%m-%d")

    db = get_db()
    cur = db.execute(
        """INSERT INTO happy_calls
           (doc_type, patient_name, guardian_name, guardian_phone, diagnosis,
            vet_name, assignee_id, scheduled_date, status, doc_body, created_by)
           VALUES (?,?,?,?,?,?,?,?,'pending_draft',?,?)""",
        (doc_type,
         patient,
         (data.get("guardian_name") or "").strip(),
         (data.get("guardian_phone") or "").strip(),
         (data.get("diagnosis") or "").strip(),
         (data.get("vet_name") or session.get("display_name", "")).strip(),
         session.get("user_id"),
         scheduled,
         (data.get("doc_body") or "").strip(),
         session.get("user_id", 0))
    )
    db.commit()
    hc_id = cur.lastrowid
    _save_patient_document(
        "happycall", patient,
        guardian_name=(data.get("guardian_name") or "").strip(),
        guardian_phone=(data.get("guardian_phone") or "").strip(),
        diagnosis=(data.get("diagnosis") or "").strip(),
        title=f"해피콜 ({HAPPYCALL_DOC_LABELS.get(doc_type, doc_type)})",
        body=(data.get("doc_body") or "").strip(),
        tags=(data.get("diagnosis") or "").strip(),
        related_happycall_id=hc_id,
        structured_data={"scheduled_date": scheduled, "source_doc_type": doc_type},
    )
    return jsonify({"ok": True, "id": hc_id, "scheduled_date": scheduled})


@app.route("/happy-calls", methods=["GET"])
@login_required
def happy_calls_list():
    """해피콜 목록 (필터·정렬)."""
    status_filter = (request.args.get("status") or "active").strip()
    doc_type_filter = (request.args.get("doc_type") or "").strip()
    q = (request.args.get("q") or "").strip()

    sql = """SELECT hc.*, u.display_name AS assignee_name
             FROM happy_calls hc
             LEFT JOIN users u ON u.id = hc.assignee_id
             WHERE 1=1"""
    args = []
    if status_filter in ("pending_draft", "drafted", "approved", "sent", "replied", "done", "noreply", "canceled"):
        sql += " AND hc.status=?"
        args.append(status_filter)
    elif status_filter == "active":
        sql += " AND hc.status IN ('pending_draft', 'drafted', 'approved', 'sent')"
    elif status_filter == "all":
        pass
    if doc_type_filter in ("ce", "postop", "imd"):
        sql += " AND hc.doc_type=?"
        args.append(doc_type_filter)
    if q:
        sql += " AND (hc.patient_name LIKE ? OR hc.guardian_name LIKE ? OR hc.diagnosis LIKE ?)"
        like = f"%{q}%"
        args += [like, like, like]

    # 정렬: 진행 중인 것은 오래된 것부터 (먼저 처리), 나머지는 최근 순
    if status_filter in ("active", "pending_draft", "drafted", "approved", "sent"):
        sql += " ORDER BY hc.scheduled_date ASC, hc.created_at ASC"
    else:
        sql += " ORDER BY hc.scheduled_date DESC, hc.created_at DESC"
    sql += " LIMIT 500"

    rows = get_db().execute(sql, args).fetchall()
    today_str = datetime.now().strftime("%Y-%m-%d")
    return render_template("happy_calls.html",
                           rows=rows, today=today_str,
                           status_filter=status_filter,
                           doc_type_filter=doc_type_filter, q=q,
                           doc_labels=HAPPYCALL_DOC_LABELS)


@app.route("/api/happy-calls/<int:hc_id>/update", methods=["POST"])
@login_required
def api_happy_call_update(hc_id):
    """상태·메모·연락처·예정일 업데이트."""
    data = request.get_json() or {}
    db = get_db()
    row = db.execute("SELECT * FROM happy_calls WHERE id=?", (hc_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404

    fields = []
    args = []
    for k in ("status", "call_memo", "guardian_phone", "scheduled_date"):
        if k in data:
            val = data[k]
            if k == "status" and val not in ("pending", "done", "noreply", "canceled"):
                continue
            fields.append(f"{k}=?")
            args.append((val or "").strip() if isinstance(val, str) else val)
    if not fields:
        return jsonify({"ok": False, "error": "업데이트할 필드 없음"}), 400

    # status 가 done/noreply/canceled 면 completed_at 설정
    if data.get("status") in ("done", "noreply", "canceled"):
        fields.append("completed_at=?")
        args.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    elif data.get("status") == "pending":
        fields.append("completed_at=NULL")  # 되돌림

    args.append(hc_id)
    # NULL 재할당 처리
    sql = "UPDATE happy_calls SET " + ", ".join(fields) + " WHERE id=?"
    sql = sql.replace("completed_at=NULL, ", "completed_at=NULL, ").replace(", completed_at=NULL WHERE", " WHERE")
    # 위 치환은 단순화 — completed_at=NULL 은 args 불필요
    if "completed_at=NULL" in sql:
        # args 에서 해당 슬롯 제거
        # 안전하게 재구성
        clean_fields = []
        clean_args = []
        for i, f in enumerate(fields):
            if f == "completed_at=NULL":
                clean_fields.append(f)
            else:
                clean_fields.append(f)
                clean_args.append(args[i])
        clean_args.append(hc_id)
        sql = "UPDATE happy_calls SET " + ", ".join(clean_fields) + " WHERE id=?"
        db.execute(sql, clean_args)
    else:
        db.execute(sql, args)
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/happy-calls/<int:hc_id>", methods=["GET"])
@login_required
def api_happy_call_detail(hc_id):
    """단건 상세 (doc_body 포함) - 모달에서 사용."""
    row = get_db().execute("SELECT * FROM happy_calls WHERE id=?", (hc_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, "data": dict(row)})

# ===================== 카톡 안부 (해피콜 카톡 워크플로) =====================
# pending_draft → drafted → approved → sent → replied → done
# 주치의가 AI초안 만들어 첨삭 후 승인 → 코디가 카톡으로 발송 → 답장 기록

KAKAO_HC_PROMPT = """당신은 한국 24시루시드 동물병원의 코디네이터가 보호자에게 보낼 카톡 안부 메시지를 작성하는 전문가입니다.

**톤과 양식 (매우 중요)**:
- 친근하고 따뜻한 카톡 톤. 너무 길지 않게 (5~7줄).
- 이모지 적절히 사용 (과하지 않게, 1~3개 정도).
- 첫 줄: "🐾 [환자]이 보호자님, 24시루시드 동물병원입니다." (조사는 받침에 맞춰서)
- 마지막 줄: "조금이라도 걱정되는 점 있으시면 이 채팅으로 답장 주시거나 02-941-7900 으로 전화 주세요. 언제든 도와드리겠습니다. 🙏"

**메시지 구성**:
1. 인사 + 병원 소개
2. [수술명/진단명] 후 안부 (한 줄)
3. 보호자가 점검할 핵심 항목 3가지 (환자 상황에 맞춤, 이모지 포함)
4. 답장/문의 안내

**환자 상태별 톤**:
- "좋은 경과로 퇴원": 안심되는 따뜻한 톤
- "회복이 지연 중 퇴원": 차분히 주의 깊게, 작은 변화도 알려달라는 부탁
- "상태 악화 중 퇴원": 진지하고 적극적, "조금이라도 이상하면 즉시 연락"

**출력 규칙**:
- 마크다운·코드블록·헤더 금지. 일반 카톡 메시지처럼 자연스럽게.
- 입력된 진단/수술/처방 정확히 반영. 추측 금지.
- 7줄 이내로 간결하게.
"""


@app.route("/api/happy-calls/<int:hc_id>/generate-draft", methods=["POST"])
@login_required
def api_happy_call_generate_draft(hc_id):
    """주치의가 클릭 → AI가 환자 정보 기반 카톡 안부 메시지 초안 생성."""
    if requests is None:
        return jsonify({"ok": False, "error": "'requests' 패키지 미설치"}), 500
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return jsonify({"ok": False, "error": "ANTHROPIC_API_KEY 미설정"}), 400

    db = get_db()
    row = db.execute("SELECT * FROM happy_calls WHERE id=?", (hc_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404

    # 환자 정보
    patient = row["patient_name"] or ""
    guardian = row["guardian_name"] or ""
    diagnosis = row["diagnosis"] or ""
    doc_type = row["doc_type"]
    doc_body = row["doc_body"] or ""

    # patient_documents 에서 관련 history 추가 조회 (있으면 reference)
    history_rows = db.execute(
        """SELECT doc_type, diagnosis, structured_data FROM patient_documents
           WHERE patient_name=? ORDER BY created_at DESC LIMIT 5""", (patient,)
    ).fetchall()
    history_summary = []
    for h in history_rows:
        sd = {}
        if h["structured_data"]:
            try: sd = json.loads(h["structured_data"])
            except: pass
        meds = sd.get("medications", "")
        status = sd.get("discharge_status", "")
        if meds or status:
            history_summary.append(f"- {h['doc_type']}: {h['diagnosis'] or ''} (약: {meds}, 퇴원상태: {status})")

    user_msg_parts = [
        f"[환자 정보]",
        f"- 환자 이름: {patient}",
        f"- 보호자: {guardian}",
        f"- 진단/수술명: {diagnosis}",
        f"- 안내문 유형: {HAPPYCALL_DOC_LABELS.get(doc_type, doc_type)}",
    ]
    if history_summary:
        user_msg_parts += ["\n[이 환자의 최근 history]"] + history_summary
    if doc_body:
        # 안내문 본문 첫 800자만
        body_excerpt = doc_body[:800] + ("..." if len(doc_body) > 800 else "")
        user_msg_parts += ["\n[보낸 안내문 요약]", body_excerpt]
    user_msg_parts.append("\n위 정보를 바탕으로 보호자에게 보낼 카톡 안부 메시지를 작성해주세요.")
    user_msg = "\n".join(user_msg_parts)

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1500,
                "system": KAKAO_HC_PROMPT,
                "messages": [{"role": "user", "content": user_msg}],
            },
            timeout=60,
        )
        if r.status_code != 200:
            return jsonify({"ok": False, "error": f"Claude API 오류 {r.status_code}: {r.text[:300]}"}), 500
        body = r.json()
        text_parts = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
        text = "\n".join(text_parts).strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith(("text","markdown","md")):
                text = text.split("\n", 1)[1] if "\n" in text else ""
            text = text.strip().rstrip("`").strip()
        # DB 에 draft_message 저장 + status 변경
        db.execute(
            "UPDATE happy_calls SET draft_message=?, status='drafted' WHERE id=?",
            (text, hc_id)
        )
        db.commit()
        return jsonify({"ok": True, "draft_message": text})
    except Exception as e:
        return jsonify({"ok": False, "error": f"AI 요청 실패: {e}"}), 500


@app.route("/api/happy-calls/<int:hc_id>/approve", methods=["POST"])
@login_required
def api_happy_call_approve(hc_id):
    """주치의 승인: 첨삭한 최종 메시지 저장 + 상태=approved."""
    data = request.get_json() or {}
    final_message = (data.get("approved_message") or "").strip()
    if not final_message:
        return jsonify({"ok": False, "error": "메시지 내용 누락"}), 400
    db = get_db()
    row = db.execute("SELECT id FROM happy_calls WHERE id=?", (hc_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    db.execute(
        """UPDATE happy_calls SET approved_message=?, status='approved',
           approved_at=?, approved_by=? WHERE id=?""",
        (final_message, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         session.get("user_id"), hc_id)
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/happy-calls/<int:hc_id>/mark-sent", methods=["POST"])
@login_required
def api_happy_call_mark_sent(hc_id):
    """코디 발송 완료."""
    db = get_db()
    db.execute(
        "UPDATE happy_calls SET status='sent', sent_at=?, sent_by=? WHERE id=?",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), session.get("user_id"), hc_id)
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/happy-calls/<int:hc_id>/mark-replied", methods=["POST"])
@login_required
def api_happy_call_mark_replied(hc_id):
    """답장 받음 + 메모 입력."""
    data = request.get_json() or {}
    memo = (data.get("call_memo") or "").strip()
    db = get_db()
    db.execute(
        """UPDATE happy_calls SET status='replied', call_memo=?, reply_received_at=?,
           completed_at=? WHERE id=?""",
        (memo, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         datetime.now().strftime("%Y-%m-%d %H:%M:%S"), hc_id)
    )
    db.commit()
    return jsonify({"ok": True})



# ===================== 통합 환자 문서 시스템 =====================
# 모든 동의서/안내문/해피콜을 환자별·질환별로 조회하기 위한 통합 저장소.

PD_DOC_TYPE_LABELS = {
    "consent_surgery":   "📄 수술·입원 동의서",
    "consent_imaging":   "🩻 영상검사 동의서",
    "consent_privacy":   "🔐 개인정보 동의서",
    "consent_euthanasia":"🕊 안락사 동의서",
    "consent_discharge": "🚪 퇴원 서약서",
    "consent_payment":   "🧾 미수금 서약서",
    "ce":      "💬 진료안내문(CE)",
    "postop":  "🏥 수술후 안내문",
    "imd":     "🩺 내과 퇴원 안내문",
    "happycall":"📞 해피콜",
}


def _save_patient_document(doc_type, patient_name, *,
                           patient_chart_id="", species="", breed="", age="", sex="",
                           guardian_name="", guardian_phone="",
                           diagnosis="", surgery_id=None, hospitalization_id=None,
                           tags="", title="", body="", structured_data=None,
                           vet_name="", related_consent_token="", related_happycall_id=None):
    """모든 문서를 patient_documents 에 통합 저장. 실패해도 raise 안 함."""
    try:
        if not patient_name:
            return None
        if structured_data is not None and not isinstance(structured_data, str):
            structured_data = json.dumps(structured_data, ensure_ascii=False)
        db = get_db()
        cur = db.execute(
            """INSERT INTO patient_documents
               (doc_type, patient_chart_id, patient_name, species, breed, age, sex,
                guardian_name, guardian_phone, diagnosis, surgery_id, hospitalization_id,
                tags, title, body, structured_data, vet_name,
                related_consent_token, related_happycall_id, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc_type, (patient_chart_id or "").strip(), patient_name.strip(),
             species or "", breed or "", age or "", sex or "",
             guardian_name or "", guardian_phone or "",
             diagnosis or "", surgery_id, hospitalization_id,
             tags or "", title or "", body or "", structured_data or "",
             vet_name or session.get("display_name", ""),
             related_consent_token or "", related_happycall_id,
             session.get("user_id", 0))
        )
        db.commit()
        return cur.lastrowid
    except Exception as _e:
        # 저장 실패해도 본 작업은 진행 (best-effort)
        return None


@app.route("/patients", methods=["GET"])
@login_required
def patients_list():
    """환자 검색 + 환자 목록 (최근 활동순). 차트ID 또는 환자명으로 그룹핑."""
    q = (request.args.get("q") or "").strip()
    db = get_db()
    sql = """
        SELECT
            COALESCE(NULLIF(patient_chart_id, ''), 'NAME:' || patient_name) AS group_key,
            patient_chart_id,
            patient_name,
            MAX(species) AS species,
            MAX(breed) AS breed,
            MAX(guardian_name) AS guardian_name,
            COUNT(*) AS doc_count,
            MAX(created_at) AS last_activity,
            GROUP_CONCAT(DISTINCT diagnosis) AS diagnoses
        FROM patient_documents
        WHERE 1=1
    """
    args = []
    if q:
        sql += " AND (patient_name LIKE ? OR patient_chart_id LIKE ? OR diagnosis LIKE ? OR guardian_name LIKE ?)"
        like = f"%{q}%"
        args += [like, like, like, like]
    sql += " GROUP BY group_key ORDER BY last_activity DESC LIMIT 200"
    rows = db.execute(sql, args).fetchall()
    return render_template("patients_list.html", rows=rows, q=q)


@app.route("/patients/<path:patient_key>", methods=["GET"])
@login_required
def patient_detail(patient_key):
    """환자 상세 — 모든 문서 시간순. patient_key 는 차트ID 또는 'NAME:이름'."""
    db = get_db()
    if patient_key.startswith("NAME:"):
        name = patient_key[5:]
        rows = db.execute(
            """SELECT * FROM patient_documents
               WHERE patient_name=? AND (patient_chart_id IS NULL OR patient_chart_id='')
               ORDER BY created_at DESC""", (name,)
        ).fetchall()
        chart_id = ""
        patient_name = name
    else:
        rows = db.execute(
            "SELECT * FROM patient_documents WHERE patient_chart_id=? ORDER BY created_at DESC",
            (patient_key,)
        ).fetchall()
        chart_id = patient_key
        patient_name = rows[0]["patient_name"] if rows else "(이름 없음)"
    if not rows:
        abort(404)
    return render_template("patient_detail.html",
                           rows=rows, patient_name=patient_name,
                           chart_id=chart_id, patient_key=patient_key,
                           doc_labels=PD_DOC_TYPE_LABELS)


@app.route("/diagnoses", methods=["GET"])
@login_required
def diagnoses_list():
    """진단·질환별 그룹 (자유 텍스트 진단명을 그대로 그룹키로 사용)."""
    q = (request.args.get("q") or "").strip()
    db = get_db()
    sql = """
        SELECT
            diagnosis,
            COUNT(*) AS doc_count,
            COUNT(DISTINCT COALESCE(NULLIF(patient_chart_id,''), 'NAME:'||patient_name)) AS patient_count,
            MAX(created_at) AS last_activity,
            GROUP_CONCAT(DISTINCT doc_type) AS doc_types
        FROM patient_documents
        WHERE diagnosis IS NOT NULL AND TRIM(diagnosis) <> ''
    """
    args = []
    if q:
        sql += " AND diagnosis LIKE ?"
        args.append(f"%{q}%")
    sql += " GROUP BY diagnosis ORDER BY doc_count DESC, last_activity DESC LIMIT 300"
    rows = db.execute(sql, args).fetchall()
    return render_template("diagnoses_list.html", rows=rows, q=q)


@app.route("/diagnoses/<path:diagnosis>", methods=["GET"])
@login_required
def diagnosis_detail(diagnosis):
    """특정 진단명의 모든 환자 케이스."""
    db = get_db()
    rows = db.execute(
        """SELECT * FROM patient_documents
           WHERE diagnosis=? ORDER BY created_at DESC""",
        (diagnosis,)
    ).fetchall()
    return render_template("diagnosis_detail.html",
                           rows=rows, diagnosis=diagnosis,
                           doc_labels=PD_DOC_TYPE_LABELS)









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
    _save_patient_document(
        "consent_euthanasia", data.get("patient_name", ""),
        species=data.get("species",""), breed=data.get("breed",""),
        age=data.get("age",""), sex=data.get("sex",""),
        guardian_name=data.get("guardian_name",""), guardian_phone=data.get("guardian_mobile",""),
        diagnosis=data.get("", "") if "" else "",
        title="안락사 동의서",
        tags="안락사",
        related_consent_token=token,
        structured_data=data,
    )

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
    _save_patient_document(
        "consent_privacy", data.get("patient_name", ""),
        species=data.get("species",""), breed=data.get("breed",""),
        age=data.get("age",""), sex=data.get("sex",""),
        guardian_name=data.get("guardian_name",""), guardian_phone=data.get("guardian_mobile",""),
        diagnosis=data.get("", "") if "" else "",
        title="개인정보 수집·활용 동의서",
        tags="",
        related_consent_token=token,
        structured_data=data,
    )

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
    elif doc_type == "discharge":
        doc_title = "퇴원 요청 및 서약서"
    elif doc_type == "payment":
        doc_title = "치료비 미수금 지불 서약서"
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
    if row["doc_type"] == "discharge":
        return _render_discharge_print_from_data(data, db, sign_interactive=True)
    if row["doc_type"] == "payment":
        return _render_payment_print_from_data(data, db, sign_interactive=True)
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
    if row["doc_type"] == "discharge":
        return _render_discharge_print_from_data(
            data, db,
            signature_b64=row["signature_data"],
            signer_name=row["signer_name"],
            signed_at=row["signed_at"],
        )
    if row["doc_type"] == "payment":
        return _render_payment_print_from_data(
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
    if doc_type in ("surgery", "imaging", "privacy", "euthanasia", "discharge", "payment"):
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
               "estimated_cost","hospitalization","expected_duration","notes",
               "discharge_notes"]


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
                 estimated_cost,hospitalization,expected_duration,notes,discharge_notes)
                VALUES (?,?,?,?,?,?,?,?,?)""",
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
            notes=?,discharge_notes=?,updated_at=datetime('now') WHERE id=?""",
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
        # 통합 환자 문서 저장 (보호자 모드만)
        if mode == "guardian" and patient_name:
            _save_patient_document(
                "ce", patient_name,
                guardian_name=guardian_name,
                title="진료안내문",
                body=text,
                structured_data={"mode": mode, "ref_vet_name": ref_vet_name},
            )
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
