# 멀티테넌트 전환 설계안 (vetconsent — 공용 배포판)

> 목표: `lucid-consent` 를 fork 한 **별도 프로젝트 `vetconsent`** 로 여러 동물병원이 공동 사용하는 SaaS 를 운영.
> **lucid-consent 는 루시드 전용 단일병원 버전으로 계속 진화** (카카오 연동 등), vetconsent 는 현재 HEAD 스냅샷 기준으로 출발해 멀티테넌시 적용 후 안정 운영.
>
> - 로컬 폴더: `C:\vetconsent` (신규)
> - Repo: 별도 (GitHub repo 는 향후 생성)
> - Render: `vetconsent.onrender.com` (신규 서비스, 별도 persistent disk, 별도 환경변수)
> - 방식: **단일 배포 + `hospital_id` 기반 논리적 격리**, **설원장 승인제** 가입.
> - 과금: MVP 단계 — 과금 로직 없음, 구조만 준비.
>
> 루시드(lucid-consent) 와 공유하는 것: **초기 코드 스냅샷만**. 이후 카카오 연동 등 루시드 특화 기능은 vetconsent 로 역이식하지 않음.

---

## 1. 역할(Role) 체계

| 역할 | 누구 | 권한 |
|---|---|---|
| `super_admin` | 설원장 (루시드 최초 admin 1명만) | 병원 가입 신청 승인/거절, 전체 병원 조회, 초기 비밀번호 재발급 |
| `hospital_admin` | 각 병원 대표원장 | 자기 병원 범위 내 — 사용자 관리, 동의서 템플릿 편집, 의료진 등록, 동의서 삭제 |
| `vet` | 일반 수의사 | 자기 병원 범위 내 — 동의서 작성/서명요청/이력 조회 |

기존 루시드 계정 처리:
- `admin` → `super_admin` 로 승격 (루시드 소속)
- `seolwon`, `nowon` → `hospital_admin` 로 승격 (루시드 소속)

---

## 2. DB 스키마 변경

### 2.1 신규 테이블

```sql
CREATE TABLE hospitals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,              -- 정식명 "○○동물메디컬센터"
    short_name TEXT NOT NULL,        -- 약칭 "○○동물병원"
    address TEXT,
    phone TEXT,
    logo_path TEXT,                  -- static/uploads/logo_{id}.png 등
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE hospital_applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_name TEXT NOT NULL,
    short_name TEXT,
    address TEXT,
    phone TEXT,
    applicant_name TEXT NOT NULL,     -- 신청자(원장명)
    applicant_email TEXT NOT NULL,
    applicant_phone TEXT,
    memo TEXT,                         -- 신청 메모
    status TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected
    reject_reason TEXT,
    reviewed_by INTEGER,               -- users.id (super_admin)
    reviewed_at TEXT,
    created_hospital_id INTEGER,       -- 승인 후 생성된 hospitals.id
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE hospital_vets (            -- 병원별 의료진 명단 (동의서 서명란용)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    display_order INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (hospital_id) REFERENCES hospitals(id)
);
```

### 2.2 기존 테이블 변경

모두 `hospital_id INTEGER NOT NULL` 컬럼 추가 + FK:

| 테이블 | 변경 |
|---|---|
| `users` | `hospital_id` 추가. `UNIQUE(username)` → `UNIQUE(hospital_id, username)` |
| `surgeries` | `hospital_id` 추가. `UNIQUE(name)` → `UNIQUE(hospital_id, name)` |
| `hospitalizations` | 동일 |
| `imaging_exams` | 동일 |
| `consent_records` | `hospital_id` 추가. `token`은 전역 UNIQUE 유지(추측 공격/교차 침범 방지) |
| `hospital_template` | PK `CHECK(id=1)` → `PRIMARY KEY(hospital_id)` 로 변경 |

### 2.3 초기화 전략 (vetconsent 는 빈 DB 에서 시작)

`vetconsent` 는 루시드 데이터 이관 없음. 따라서 "마이그레이션" 이 아니라 **깨끗한 멀티테넌트 스키마로 `init_db()` 재작성**.

1. 모든 도메인 테이블에 처음부터 `hospital_id INTEGER NOT NULL` + `UNIQUE(hospital_id, name)` 로 생성
2. `hospitals` / `hospital_applications` / `hospital_vets` 신규 테이블 함께 생성
3. 최초 실행 시 **super_admin 1명만 시드** — 환경변수 `SUPER_ADMIN_USERNAME` / `SUPER_ADMIN_PASSWORD` / `SUPER_ADMIN_NAME` 기반. 병원 소속 없음(`hospital_id` NULL 허용하지 않고 별도 system 병원 id=0 사용 또는 `hospital_id` nullable)
4. `surgeries` / `imaging_exams` 의 seed_data 는 **승인 시 각 병원 DB에 복사** (공용 베이스 라이브러리로 활용)
5. 이미 생성된 DB 는 스키마 체크 후 누락 테이블/컬럼만 조건부 추가 — 재실행 안전

루시드 특화 하드코딩은 **fork 직후 일괄 제거**:
- `HOSPITAL_NAME`, `HOSPITAL_SHORT` 상수 삭제 → 동적 로드
- `default_templates.py` 의 루시드 특화 문구(카카오톡 안내 등) 제거, 범용 기본 템플릿으로 재작성
- 초기 시드 수의사(`설원장`, `노진희원장`) 제거 (승인 시 신청자 이름으로 생성)

---

## 3. 라우트 변경 방식

### 3.1 패턴
- 로그인 성공 시 `session['hospital_id']` 저장
- 헬퍼 함수 `current_hospital_id()` 도입
- 모든 SELECT/INSERT/UPDATE/DELETE 에 `WHERE hospital_id = ?` 일괄 적용
- `@login_required` 내부에서 `g.hospital_id = session['hospital_id']` 주입

### 3.2 예외 처리
- `/sign/<token>` (공개 라우트): `token` 으로 `consent_records` 조회 → 해당 행의 `hospital_id` 로 템플릿/병원정보 로드 (로그인 세션과 무관)
- `/apply` (공개 라우트): 가입 신청 — 로그인/hospital_id 불필요
- `/super/*` 라우트: `@super_admin_required` — hospital_id 필터 대신 전체 조회

### 3.3 교차 침범 방어
- 상세/편집/삭제 라우트는 항상 `id = ? AND hospital_id = ?` 조건. 다른 병원의 id를 URL로 넣어도 404.
- `/api/consents/<id>/qr`, `/api/consents/<id>/cancel`, `/api/consents/<id>/delete` 동일 적용.

---

## 4. 가입/승인 플로우

```
[공개] /apply                          → hospital_applications INSERT (status=pending)
[super_admin] /super/applications      → pending 목록 조회
[super_admin] /super/applications/<id>/approve
    → hospitals INSERT
    → users INSERT (role=hospital_admin, 임시 비번 발급)
    → hospital_template INSERT (DEFAULT_* 복사)
    → hospital_vets INSERT (신청자명 1명)
    → 승인 완료 화면에 ID/임시비번 노출 (이메일 발송은 Phase 2)
[hospital_admin] 첫 로그인 시 must_change_password=1 → 비밀번호 변경 강제
```

---

## 5. 커스터마이징 범위 (요구사항 반영)

| 항목 | 위치 | 편집자 |
|---|---|---|
| 병원명/주소/전화/로고 | `/hospital/settings` | hospital_admin |
| 수의사 목록 | `/hospital/settings` (hospital_vets) | hospital_admin |
| 동의서 header/disclaimer/footer | `/template` (기존 페이지 재활용) | hospital_admin |
| 차트 OCR / AI 자동채움 프롬프트 | 공용 (코드 상수) | 공유, 편집 불가 — Phase 2 고도화 대상 |

---

## 6. UI/브랜딩 동적화

- `app.context_processor` 로 `current_hospital` 객체를 모든 템플릿에 주입
- `base.html`, `dashboard.html`, `login.html` 의 하드코딩된 "루시드 동물병원" → `{{ hospital.short_name }}` 치환
- `sign_page.html` 의 `hospital_name=HOSPITAL_SHORT` → `consent_records.hospital_id` 기반 동적 로드
- `default_templates.py` 의 `{{ doc_title }}` 외 루시드 특화 문구(카카오톡 안내 등)는 초기 템플릿으로만 사용, 이후 각 병원이 `/template` 에서 직접 편집

---

## 7. 배포 영향

- **lucid-consent (루시드 전용)**: 변경 없음. 기존 서비스/데이터 그대로 유지. 향후 카카오 연동 등 계속 개발.
- **vetconsent (공용)**: 신규 Render 서비스. 별도 persistent disk, 별도 DB(`/var/data/vetconsent.db`), 별도 `ANTHROPIC_API_KEY` 환경변수.
- 초기 DB 는 빈 상태로 시작 (루시드 데이터 이관 없음). 초기 유일 계정 = `super_admin` (설원장, 환경변수 `SUPER_ADMIN_*` 로 첫 시드).
- URL: `vetconsent.onrender.com` (공용 병원들이 여기로 접속)
- DNS/도메인 변경 없음

---

## 8. 작업 순서 (TaskList와 동일)

1. **설계안 확정** (본 문서)
2. **DB 스키마 확장 + 마이그레이션 스크립트**
3. **인증/세션에 hospital_id 주입 + 역할 분리**
4. **모든 라우트에 hospital_id 필터 적용** (~35개)
5. **hospital_template 다중 행 + 설정 페이지**
6. **가입 신청 + 승인 페이지**
7. **UI/브랜딩 동적화**
8. **최종 검증 + 로컬 테스트**

각 단계마다 로컬 커밋 → 확인 후 main 푸시(=Render 자동 배포).

---

## 9. 리스크 & 완화책

| 리스크 | 완화 |
|---|---|
| 마이그레이션 실패로 기존 루시드 데이터 손상 | 실행 전 `/var/data/lucid.db` → `lucid.db.bak.{timestamp}` 자동 백업 |
| UNIQUE 제약 재생성 중 SQLite 잠금 | `init_db()` 시작 시 한 번만 실행, 완료 플래그 `schema_version` 관리 |
| QR 토큰 교차 침범 | `token` 전역 UNIQUE + 조회 시 내부적으로 hospital_id 로드 (세션 hospital_id와 비교하지 않음 — 보호자는 로그인 안 함) |
| 승인 전 계정 생성 방지 | `hospital_applications.status='approved'` 트랜잭션 내에서만 `users` INSERT |
| 공용 AI/OCR 프롬프트 남용 | Phase 2에서 병원별 API 사용량 기록 테이블 추가 |

---

## 10. Phase 2 (본 작업 범위 외)

- 이메일 발송(SMTP 또는 SES): 승인 알림, 비밀번호 초기화
- 병원별 AI 사용량 기록 / 월 한도
- 서브도메인(`lucid.○○병원.com`) 옵션
- 과금(Stripe 등)
- 각 병원 전용 로고 파일 업로드 저장소 외부화(S3 등)
