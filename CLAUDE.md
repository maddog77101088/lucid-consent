# CLAUDE.md — 루시드 동의서 시스템 작업 컨텍스트

> 이 문서는 **Claude(Cowork)** 가 새 세션에서 이 프로젝트를 즉시 이해하기 위한 핸드오프 노트다.
> 설원장이 PC를 옮기거나 앱을 재설치한 후에도 이 파일만 보면 작업 맥락이 복원된다.
> 마지막 갱신: 2026-05-08

---

## 1. 프로젝트 한 줄 요약

**`lucid-consent`** = 24시루시드동물메디컬센터 전용 **수술/입원 동의서·진료안내문·소견서 자동화 웹앱** (Flask + SQLite + 카카오 알림톡 + Claude/OpenAI AI). Render에 배포되어 운영 중이며, 공개 URL은 `https://lucid-consent.onrender.com`.

오너: 설원장 (`maddog77101088`) — 외과수술 + 경영 담당. 부인 노진희 원장과 공동대표.

---

## 2. 기술 스택

| 레이어 | 사용 |
|---|---|
| 백엔드 | Flask 3.0.3 + Werkzeug 3.0.4, gunicorn 22.0.0 |
| DB | SQLite (`data/lucid.db` — 로컬 자동생성, Render 영속 디스크 `/var/data/lucid.db`) |
| 템플릿 | Jinja2 (templates/ 60+개 HTML) |
| 정적 | static/style.css, static/print.css |
| AI | Claude (anthropic) 우선 → 실패 시 OpenAI 자동 폴백 |
| 카카오 알림톡 | **Solapi** API (api.solapi.com) — 카카오 비즈니스 채널 |
| 라이브러리 | requests, qrcode[pil], openpyxl |
| 배포 | Render (Singapore region, starter $7 + disk 1GB $1 = 월 약 $8) |

---

## 3. Git 원격 — 작업 동기화의 단일 진실

```
origin: https://github.com/maddog77101088/lucid-consent.git
브랜치: main
```

**모든 코드는 GitHub에 100% 커밋되어 있다.** 다른 PC에서는 `git clone`만 하면 끝. 로컬 .git/logs/HEAD에는 약 113개 커밋 히스토리가 있고 origin/main과 완전 동기화 상태(2026-05-08 기준).

마지막 5개 커밋 (작업 흐름 파악용):
1. `5676d27` feat: 리퍼병원 일괄 업로드 (xlsx/csv) — 양식 다운로드, 헤더 자동인식, 중복처리 옵션
2. `e83966a` fix: 진료 소견서 — 수의사 전화번호 입력 제거(병원 대표번호 자동), OCR 성별 폼옵션 매핑 보정
3. `519dfe1` refactor: 진료 소견서 — 사인을 수의사 이름 옆 인라인 배치
4. `37cf89f` feat: 진료 소견서에 발행 목적 드롭다운(보험청구용/기타) 추가
5. `5c91eff` feat: 수의사 면허번호·사인 강제 입력 (/me/license) — 사인 패드 직접 그리기

---

## 4. 파일 구조 핵심

```
lucid-consent/
├── app.py                   # Flask 메인 (266KB, 단일파일 전체 라우트)
├── default_templates.py     # 루시드 표준 동의서 본문 (헤더/disclaimer/footer)
├── seed_data.py             # 17개 초기 수술 시드
├── requirements.txt
├── render.yaml              # Render 배포 설정
├── Procfile                 # gunicorn 진입점
├── PLAN.md                  # ★ 멀티테넌트 vetconsent 설계안 (다음 큰 작업)
├── DEPLOY.md                # Render 배포 절차
├── README.md                # 사용 안내
├── templates/               # 60+ 개 Jinja 템플릿
│   ├── base.html · login.html · dashboard.html
│   ├── consent_new/print/history.html      # 수술/입원 동의서
│   ├── surgery_list/edit.html              # 수술 DB
│   ├── imaging_*.html                      # 영상검사 동의서
│   ├── privacy_*.html                      # 개인정보 동의서 (NIMS 마약류용)
│   ├── euthanasia_*.html                   # 안락사 동의서
│   ├── discharge_*.html · payment_*.html   # 퇴원·미수금 서약서
│   ├── medical_opinion_*.html              # 진료 소견서 (보험청구용)
│   ├── ce_new/postop_new/imd_new.html      # 진료안내문(CE) / 수술후 / 내과
│   ├── happy_calls.html                    # 카톡 안부 워크플로
│   ├── notices_list/edit/view.html         # 안내문 관리
│   ├── referrals_list.html · referral_hospitals.html  # 리퍼병원 관리
│   ├── patients_list/detail.html · diagnoses_list/detail.html  # 통합 환자 문서
│   ├── sign_page.html · sign_complete/status.html      # 보호자 모바일 서명
│   ├── menu_admin/db/notices/consents.html # 대시보드 카테고리
│   └── ...
├── static/{style,print}.css
├── data/lucid.db            # ★ git 미포함 (.gitignore), Render 영속 디스크에 실데이터
└── .env                     # ★ git 미포함, API 키 보관 (현재 로컬엔 없음)
```

---

## 5. 환경변수 — `.env` 또는 Render 대시보드에 설정 필수

`app.py`에서 사용하는 전체 목록:

| 키 | 용도 | 비고 |
|---|---|---|
| `SECRET_KEY` | Flask 세션 서명 | Render에서 `generateValue: true`로 자동 |
| `DB_PATH` | DB 경로 | Render: `/var/data/lucid.db`, 로컬: `data/lucid.db` |
| `ANTHROPIC_API_KEY` | Claude API (AI 자동채움/소견서/안내문/카톡 안부) | 1순위 |
| `OPENAI_API_KEY` | ChatGPT (Claude 다운 시 폴백) | 2순위 |
| `AI_PROVIDER` | "auto"(기본)/"anthropic"/"openai" | 강제 지정 시 |
| `SOLAPI_API_KEY`, `SOLAPI_API_SECRET` | 카카오 알림톡 발송 (api.solapi.com) | 솔라피 콘솔에서 발급 |
| `KAKAO_PFID` | 카카오 비즈니스 채널 ID | 솔라피 → 채널 등록 후 |
| `KAKAO_SENDER` | 발신 휴대폰번호 | 미설정 시 `HOSPITAL_PHONE` |
| `KAKAO_TEMPLATE_ID_CONSENT` | 동의서 사본 발송 템플릿 | |
| `KAKAO_TEMPLATE_ID_NOTICE` | 안내문 도착 알림 (기본) | |
| `KAKAO_TEMPLATE_ID_NOTICE_CE` | 진료안내문(CE) 전용 | doc_type별 분리 |
| `KAKAO_TEMPLATE_ID_NOTICE_POSTOP` | 수술후 안내문 전용 | |
| `KAKAO_TEMPLATE_ID_NOTICE_IMD` | 내과 퇴원 안내문 전용 | |
| `KAKAO_TEMPLATE_ID_HOSPITAL_VIDEO` | 입원전 안내영상 카톡 (서명 시 자동) | |
| `KAKAO_TEMPLATE_ID_MEDICAL_OPINION` | 진료 소견서 보호자 발송 | |
| `KAKAO_TEMPLATE_ID_VET_REFERRAL` | 리퍼병원 통보 | |
| `KAKAO_TEMPLATE_ID_DOCTOR_ALERT` | 주치의 응답 알림 | |
| `KAKAO_TEMPLATE_ID` | (legacy 폴백) | |
| `HOSPITAL_NAME` | "24시루시드동물메디컬센터" | 기본값 있음 |
| `HOSPITAL_SHORT` | "루시드 동물병원" | |
| `HOSPITAL_PHONE` | "02-941-7900" | |
| `HOSPITAL_BRANCH` | 지점 표시 (선택) | |
| `PUBLIC_BASE_URL` | 보호자 모바일 서명 링크 베이스 (예: `https://lucid-consent.onrender.com`) | QR/카톡 링크 생성 시 필요 |
| `FLASK_DEBUG` | "0" 운영 / "1" 개발 | |
| `PORT` | 기본 5000 (Render는 자동) | |

**현재 로컬에 `.env` 파일이 없음.** 다른 PC로 옮길 때는 Render Dashboard → Environment 탭에서 값을 그대로 복사해 와서 `.env` 만들거나, 로컬 개발용 키만 `.env`에 두고 운영은 Render 환경변수만 사용.

---

## 6. 데이터베이스 — 실데이터 위치

- **운영**: Render persistent disk `/var/data/lucid.db` (Render Shell로만 접근)
- **로컬**: `data/lucid.db` (없으면 `app.py` 첫 실행 시 자동 생성, seed 17개 수술 + 기본 admin/seolwon/nowon 계정 자동)
- **백업**: Render Shell → `cp /var/data/lucid.db /var/data/lucid.db.$(date +%Y%m%d).bak`
- 동의서 1건당 수 KB → 1GB 디스크에 수만 건 저장 가능

기본 계정 (최초 1회 비번 강제 변경됨):

| 아이디 | 권한 | 비고 |
|---|---|---|
| `admin` | 관리자 (super) | 사용자관리·DB편집·삭제 권한 |
| `seolwon` | 수의사 (설원장) | 외과수술 |
| `nowon` | 수의사 (노진희원장) | 고양이·줄기세포 |

---

## 7. 새 PC에서 개발환경 부팅 절차

```bash
# 1) 코드 가져오기
git clone https://github.com/maddog77101088/lucid-consent.git
cd lucid-consent

# 2) 가상환경 + 의존성
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# 3) .env 만들기 (최소)
echo SECRET_KEY=dev-secret-change-me > .env
echo ANTHROPIC_API_KEY=sk-ant-... >> .env
# 카카오 발송 테스트 안 할 거면 SOLAPI_*는 생략 가능

# 4) 실행
python app.py    # http://127.0.0.1:8000
```

API 키 없어도 기본 기능(동의서 작성/인쇄, DB CRUD, 보호자 서명)은 모두 작동. AI 버튼 누를 때만 오류.

---

## 8. 다음 큰 작업 — `vetconsent` 멀티테넌트 fork

**`PLAN.md` 전문 참조.** 요약:

- `lucid-consent`는 루시드 전용 단일병원 버전으로 계속 진화(카카오 연동 등)
- 별도 신규 프로젝트 `C:\vetconsent` 를 fork 하여 **여러 동물병원이 공동 사용하는 SaaS**로 전개
- Render 신규 서비스: `vetconsent.onrender.com` (별도 디스크/DB/API키)
- 단일 배포 + `hospital_id` 기반 논리적 격리
- **설원장 승인제** 가입 — `/apply` 공개 신청 → 설원장(`super_admin`)이 `/super/applications`에서 승인
- 역할 3단: `super_admin`(설원장 1명) / `hospital_admin`(각 병원 대표) / `vet`(수의사)
- DB 스키마 변경: 모든 도메인 테이블에 `hospital_id` 컬럼 추가, 신규 테이블 3개(`hospitals`, `hospital_applications`, `hospital_vets`)
- 작업 순서(PLAN §8): 설계확정 → DB스키마 → 인증/세션 → 라우트필터(~35개) → 템플릿 다중행 → 가입승인 페이지 → UI동적화 → 검증

---

## 9. 도메인 지식 — 시스템이 다루는 7가지 동의서/안내문

1. **수술/입원 동의서** (`/consent/new`) — 수술 DB 17개 시드 + AI 자동채움, 보호자 QR 모바일 서명
2. **영상검사 동의서** (`/imaging/new`) — CT/MRI 등
3. **개인정보 수집·활용 동의서** (`/privacy/new`) — NIMS 마약류 원외처방용
4. **안락사 동의서** (`/euthanasia/new`) — 사후 장례방법은 보호자 서명 시 선택
5. **퇴원 요청 및 서약서** (`/discharge/new`) — 보호자 요청 조기퇴원
6. **치료비 미수금 지불 서약서** (`/payment/new`) — 금액·납입기한 자동계산
7. **진료 소견서** (`/medical_opinion/new`) — 보험청구용/기타, 면허번호도장 자동삽입

추가 안내문 시스템:
- **CE(진료안내문)** — D+1 카톡 안부 자동등록
- **수술후 안내문(postop)** — D+2 카톡 안부 자동등록
- **내과 퇴원 안내문(imd)** — D+3 카톡 안부 자동등록
- **카톡 안부 워크플로** — AI 초안 → 주치의 첨삭 → 코디 발송 → 답장 분류(상태악화/약부작용/일반)

기타:
- **리퍼병원 관리** — 의뢰병원 DB, CE 의뢰병원 모드 검색·자동매칭
- **통합 환자 문서** (`patient_documents`) — 환자별/진단별 모든 문서 자동 저장
- **해피콜 시스템** — 자동 등록·만족도 설문

---

## 10. 코드 베이스 특이사항 — Claude가 알아두면 좋을 것

- `app.py`가 **단일 파일 266KB** — blueprint 분리 안 됨. 검색은 `grep`으로.
- AI 호출 11개 지점이 모두 **Claude → OpenAI 자동 폴백** 통합됨 (`f757f91` 커밋)
- **압존법 규칙** 강제 — AI 카톡 안부 프롬프트에 "반려동물 존대 금지" 명시 (`348670d` 커밋)
- 보호자 모바일 서명: `/sign/<token>` 공개 라우트 + token 전역 UNIQUE
- 모바일 반응형: 640px 미만 1열 자동 전환
- soft-delete 정책: 동의서/안내문/카톡안부 모두 admin 전용, 환자명 재입력 + 사유 필수
- `__pycache__/`는 .gitignore 미포함이지만 무시해도 됨

---

## 11. Cowork 작업 시 권장 워크플로

1. 작은 변경: 직접 Edit → 로컬 테스트 → `git add` / `git commit` / `git push`
2. 큰 변경: `PLAN.md` 같은 설계 문서 먼저 갱신 후 단계별 커밋
3. Render 배포: `main` 브랜치 push 시 자동 배포됨 (별도 명령 불필요)
4. DB 스키마 변경 시: `init_db()` 안에 조건부 ALTER TABLE 추가 (재실행 안전)
5. 카카오 템플릿 변경: 솔라피 검수 통과 필요 → 환경변수 템플릿 ID 갱신

---

## 12. 알려진 제약

- **2FA 없음** — 병원 공용 PC 환경에서는 물리 보안으로 보완
- **감사 로그 없음** — 누가 언제 동의서 만들었는지 별도 기록 없음 (필요 시 `consent_log` 테이블 추가 예정)
- **이메일 발송 없음** — 비밀번호 초기화는 admin이 직접 임시비번 발급해서 전달 (Phase 2)
- **AI 비용 모니터링 없음** — `vetconsent` 멀티테넌트에서 추가 예정

---

이 문서를 읽었다면 작업을 시작하기 전에 사용자에게 **"오늘 어느 부분을 작업할까요?"** 정도만 가볍게 물어본 후 진행. PLAN.md / DEPLOY.md / README.md 도 참고.
