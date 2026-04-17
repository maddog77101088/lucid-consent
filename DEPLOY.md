# 배포 가이드 (Render 기준)

루시드 동물병원 수술동의서 시스템을 인터넷에 올려서 다른 수의사들이 접속할 수 있게 하는 절차입니다. 소요 시간 약 30분, 월 비용 약 $8 (≈ 11,000원).

## 1. 사전 준비 — 반드시 먼저 할 것

### 1.1 노출된 API 키 폐기

기존 `.env`의 `ANTHROPIC_API_KEY`는 외부에 노출된 적이 있으므로 반드시 폐기합니다.

1. https://console.anthropic.com/settings/keys 접속
2. 기존 키 오른쪽 점 3개 메뉴 → **Delete**
3. **Create Key** → 이름 "lucid-consent-prod" → 발급받은 값을 **안전하게** 별도 메모장에 복사

### 1.2 기본 비밀번호 변경 준비

로컬 배포 시점에 기본 계정 3개가 모두 `lucid1234`로 생성되며, 최초 로그인 시 시스템이 강제로 비밀번호 변경 화면을 띄웁니다. 미리 다음 양식의 비밀번호를 준비하세요:
- 최소 10자 이상
- 영문 대문자·소문자·숫자·특수문자 중 3종류 이상
- 아이디·이름 포함 금지
- 흔한 비밀번호(`lucid1234`, `password` 등) 금지

예: `StrongClin!c2026`

## 2. GitHub 리포지토리 준비

Render는 GitHub 리포와 연동해서 자동 배포합니다.

```powershell
cd C:\Users\maddo\Downloads\lucid_consent_v10e\lucid-consent
git init
git add .
git commit -m "Initial commit: 루시드 동의서 시스템"
```

`.gitignore`에 `.env`가 포함돼 있으므로 API 키는 GitHub에 올라가지 않습니다. 혹시 모르니 `git status`로 `.env`가 목록에 없는지 한 번 더 확인하세요.

GitHub에서 새 비공개 리포지토리 생성(예: `lucid-consent`) 후:

```powershell
git remote add origin https://github.com/<YOUR_USERNAME>/lucid-consent.git
git branch -M main
git push -u origin main
```

## 3. Render 배포

### 3.1 서비스 생성

1. https://render.com/ 가입 / 로그인 (GitHub 계정으로 로그인 편함)
2. **New +** → **Blueprint** 선택
3. GitHub 리포 연결 → `lucid-consent` 선택
4. Render가 자동으로 `render.yaml`을 감지합니다.
5. Blueprint 이름 입력 후 **Apply**
6. 첫 배포 중 **"Secret ANTHROPIC_API_KEY is missing"** 경고가 뜨면 다음 단계로

### 3.2 환경변수 등록

서비스 페이지 → **Environment** 탭 → **Add Environment Variable**

| Key | Value |
|---|---|
| `ANTHROPIC_API_KEY` | 1.1에서 새로 발급한 키 |

(SECRET_KEY, DB_PATH 등은 render.yaml이 자동 설정)

저장 후 **Manual Deploy → Deploy latest commit**.

### 3.3 배포 확인

- 서비스 페이지 상단에 URL이 표시됩니다 (예: `https://lucid-consent.onrender.com`)
- **Logs** 탭에서 `Running on http://0.0.0.0:10000` 같은 메시지가 뜨면 OK
- 브라우저로 해당 URL 접속 → 로그인 페이지가 뜨면 성공

## 4. 첫 로그인 & 보안 초기 설정

### 4.1 admin 계정

- URL: `https://lucid-consent.onrender.com/login`
- 아이디: `admin`
- 비밀번호: `lucid1234`
- → 로그인하면 **비밀번호 변경** 화면이 강제로 뜸
- → 1.2에서 준비한 강한 비밀번호로 변경

### 4.2 seolwon, nowon 계정

같은 방식으로 각자 직접 로그인해서 비밀번호 변경. 또는 admin으로 로그인한 뒤 **사용자 관리** 페이지에서 각자 비밀번호를 임시로 재설정해주면 해당 사용자는 첫 로그인 시 자동으로 변경 화면을 만나게 됩니다.

### 4.3 새 수의사 계정 추가

admin 로그인 → **사용자 관리** → "새 사용자 추가" 섹션
- 아이디: 영문 (예: `drkim`)
- 이름: 표시명 (예: `김수의사 원장`)
- 임시 비밀번호: 10자+, 3종 조합
- 역할: 수의사 / 관리자
→ **추가**

전달 방법: 해당 수의사에게 아이디와 임시 비밀번호를 안전한 경로(카카오 개인톡, SMS)로 전달. 본인이 첫 로그인 시 강제로 변경하게 됩니다.

## 5. 운영 체크리스트

### 자주 하는 것
- Render 대시보드 **Logs** 에서 오류·이상 접근 확인
- 정기적으로(월 1회) 사용자 목록 점검 → 퇴사자 계정 삭제

### 가끔 하는 것
- DB 백업: Render 서비스 페이지 → **Shell** → `cp /var/data/lucid.db /var/data/lucid.db.$(date +%Y%m%d).bak`
- 또는 로컬로 내려받기: `Shell` → `cat /var/data/lucid.db` 복사해서 저장 (작은 DB면 가능)

### 문제 생겼을 때
- 배포 후 500 에러: Logs 탭에서 traceback 확인
- DB 깨졌을 때: 백업 파일로 복원 (`cp /var/data/lucid.db.백업 /var/data/lucid.db` 후 서비스 Restart)
- admin 비번 잊었을 때: Shell에서
  ```
  python -c "import sqlite3; from werkzeug.security import generate_password_hash; c=sqlite3.connect('/var/data/lucid.db'); c.execute('UPDATE users SET password_hash=?, must_change_password=1 WHERE username=?', (generate_password_hash('TempPass-2026!'), 'admin')); c.commit()"
  ```
  → `admin / TempPass-2026!` 로 로그인하면 다시 변경 화면이 뜸

## 6. 비용

| 항목 | 월 비용 |
|---|---|
| Render Starter Web Service | $7 |
| Persistent Disk 1GB | $1 |
| **합계** | **약 $8 (11,000원)** |

DB가 1GB 차면 disk sizeGB를 render.yaml에서 올리면 됩니다. 동의서 1건당 수 KB이므로 1GB는 수만 건 저장 가능.

## 7. 보안 권장

- **접속 IP 제한이 필요한 경우**: Render Starter는 Cloudflare 연동으로 IP 제한 가능. 필요 시 별도 안내.
- **정기 비밀번호 변경**: 현재는 강제 정책 없음. 필요하면 `password_changed_at` 컬럼 추가 후 90일 정책 구현 가능.
- **2차 인증(2FA)**: 현재 미지원. 병원 공용 PC 환경에서는 물리적 보안(문 잠금, 화면 잠금)으로 보완.
- **감사 로그**: 누가 언제 동의서를 만들었는지 남기는 기능은 현재 없음. 필요하면 `consent_log` 테이블 추가.

## 8. 커스텀 도메인 (선택)

`consent.lucidanimal.co.kr` 같이 병원 도메인으로 붙이고 싶으면:
1. Render 서비스 페이지 → **Settings** → **Custom Domains** → **Add**
2. 도메인 입력 → Render가 알려주는 CNAME 레코드를 도메인 DNS에 추가
3. SSL은 Render가 자동 발급

---

문제 생기면 Render 서비스 페이지의 **Logs**·**Events** 탭이 1차 진단 지점입니다.
