# 루시드 동물병원 수술 및 입원 동의서 시스템

24시루시드동물메디컬센터 전용 자동화 웹 애플리케이션. 
원장님들이 브라우저에서 접속해 보호자·동물 정보를 입력하고 수술을 선택하면, 
루시드 병원 표준 양식(수술및입원 동의서 본문 + 3개 설명확인 + 5개 추가검사 동의 + 6개 보호자약속 + 2개 알림 + 수의사법 조항)이
그대로 포함된 동의서를 인쇄·PDF로 출력할 수 있습니다.

## 기능

1. **간단 로그인** : 원장별 계정 (관리자/수의사)
2. **수술 DB** (17개 시드 - 일반외과 5 / 정형외과 4 / 연부조직 5 / 응급·기타 3)
   각 수술마다: 수술방법 / 예상합병증 및 예후 / 수술의 목적 및 기대효과 / 예상비용 / 입원기간·통원·재진·모니터링 / 마취위험 / 예상소요시간
3. **동의서 작성**
   - 보호자 정보: ID / 이름 / 주민등록번호 / 전화·휴대전화 / 주소
   - 동물 정보: ID / RFID / 종 / 품종 / 이름 / 나이 / 성별 / 피모색 / 체중 / 기저질환
   - 수술 선택 → DB 내용 자동 로딩 (수정 가능)
   - DB에 없는 수술은 직접 입력 후 "수술DB에 저장" 한 번으로 재사용
4. **인쇄/PDF 출력** : 루시드 표준 양식 그대로 (체크박스 27개 포함). 브라우저 "PDF로 저장" → 보호자 수기 서명
5. **병원 기본양식 편집** : 관리자가 루시드 양식 본문을 언제든 수정 가능 (HTML)
6. **사용자 관리** : 원장 계정 CRUD

## 빠른 시작

```bash
pip install -r requirements.txt
python app.py
# http://127.0.0.1:8000
```

기본 계정:
| 아이디 | 비밀번호 | 권한 |
|--------|----------|------|
| admin | lucid1234 | 관리자 |
| seolwon | lucid1234 | 수의사 (설원장) |
| nowon | lucid1234 | 수의사 (노진희원장) |

**첫 로그인 후 반드시 비밀번호 변경**

## 클라우드 배포 (Render 추천, 무료플랜 가능)

1. GitHub에 이 폴더 푸시
2. Render.com → New + Blueprint → 리포 연결 → `render.yaml` 자동 감지 → 배포
3. 발급된 URL로 병원 PC에서 접속

## 파일 구조

```
lucid-consent/
├── app.py                  # Flask 메인 (라우트·인증·DB)
├── default_templates.py    # 루시드 양식 기본 HTML (헤더/본문/서명란)
├── seed_data.py            # 초기 수술 17건
├── requirements.txt
├── Procfile · render.yaml  # 클라우드 배포
├── templates/              # Jinja2 템플릿
│   ├── base.html
│   ├── login.html · dashboard.html
│   ├── surgery_list.html · surgery_edit.html
│   ├── consent_new.html    # ★ 동의서 작성
│   ├── consent_print.html  # ★ 인쇄·PDF 뷰
│   ├── template_edit.html
│   └── users.html
├── static/
│   ├── style.css · print.css
└── data/lucid.db           # SQLite (자동 생성)
```

## 루시드 양식 본문 (기본 문구)

`default_templates.py`에 루시드 수술 및 입원 동의서 전문이 그대로 들어있어
처음부터 정상 양식으로 출력됩니다. 
- 헤더: 3개 설명확인 질문 (Q1/Q2/Q3) + 집도의 자동 표시
- 본문: 5개 추가검사 동의/거절 + 6개 보호자 약속 + 2개 알림 + 수의사법 제13조의2
- 서명란: 수술 예정일 자동 + 보호자 (인) + 병원 귀하

문구 수정은 관리자 로그인 → [기본양식 편집]에서 가능.

## AI 자동채움 (DB에 없는 수술 처리)

DB에 없는 수술은 직접 입력칸에 수술명만 입력하고 **[🤖 AI로 자동채움]** 버튼을 누르면
Claude가 수술방법·합병증·예상비용·입원기간 등을 자동으로 작성해줍니다.
내용은 수의사가 검토 후 수정하여 인쇄하고, 필요시 **[수술DB에 저장]**으로 재활용.

### Claude API 키 발급·설정
1. [console.anthropic.com](https://console.anthropic.com) 가입 → API Keys → Create Key → `sk-ant-...` 복사
2. 서버 실행 전에 환경변수 설정:

**Windows (PowerShell)**
```powershell
$env:ANTHROPIC_API_KEY="sk-ant-api03-XXXXX"
python app.py
```

**Windows (cmd)**
```cmd
set ANTHROPIC_API_KEY=sk-ant-api03-XXXXX
python app.py
```

**Render (클라우드)**
Dashboard → Environment → Add Environment Variable → Key: `ANTHROPIC_API_KEY`, Value: `sk-ant-api03-XXXXX`

비용은 1회 자동채움 당 약 $0.005 (약 7원) — 한 달 500번 사용해도 3,500원 수준.

API 키 없이 써도 기본 기능은 모두 작동 (AI 버튼 누를 때만 오류 메시지 뜸).

## 수술 부위·방향 선택

십자인대·슬개골·골절 등 좌/우 구분이 필요한 수술은 동의서 작성 시
"수술 부위 / 방향" 드롭다운에서 **좌측 / 우측 / 양측** 을 선택하면
인쇄된 동의서에 강조 표시됩니다.

## 백업
모든 데이터는 `data/lucid.db` 한 파일. 주기적 복사로 백업.

## 보안
- 기본 비밀번호 즉시 변경
- `SECRET_KEY` 환경변수 배포 시 반드시 설정
- 작성된 동의서는 서버에 저장되지 않고 인쇄용 HTML로만 렌더 (개인정보 노출 최소화)

## 확장 예정
- [ ] 카카오 알림톡 연동 (동의서 PDF 전송)
- [ ] 마취 동의서·입원 동의서·치과 동의서 확장
- [ ] PMS365 연동 (환자정보 자동 불러오기)
