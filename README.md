# ps_fix
Convert PostScript-format raw LPT outputs from retro windows versions to pdf.

## PSCRIPT.DRV — `0x14` 제어 바이트 버그 (Windows 9x/ME 한정)

**발견:** 2026-06-27, Windows ME + IE 5.5 인쇄 출력 (HP Color LaserJet 5/5M PS 드라이버)
**재확인:** 2026-07-16, Windows 98 + Franklin Planner 7.3 등록카드 인쇄 (동일 드라이버)

### 증상

- PS 파일 헤더: `%%DocumentData: Clean7Bit` 선언
- 실제 파일: **offset 4042 고정 위치**에 `0x14` 제어 바이트 삽입
- 오류 메시지: `ERROR: undefined / OFFENDING COMMAND: load / STACK: /setlinecap /Lc`
- Ghostscript/Okular: PS도 변환된 PDF도 모두 열기 실패 또는 내용 없음
- `ps2pdf` 실패

### 원인

Windows 9x/ME의 PSCRIPT.DRV(9x 계열)가 PS 연산자 약어 테이블 내부에
`0x14`를 삽입하면서 `Clean7Bit` 선언과 모순.
**Windows NT 4.0의 PSCRIPT.DRV(NT 계열)에서는 발생하지 않음** — 동일한
HP Color LaserJet 5/5M PS 드라이버라도 OS 계열에 따라 드라이버 구현이 다름.
Win98, WinME 모두 offset 4042 고정 위치에서 발생 확인.

삽입 위치 예: `/Lc/setl\x14inecap` — `setlinecap` 약어 정의 중간에 삽입됨.

### 오류 연쇄 구조

`/Lc/setl\x14inecap` → 토크나이저가 `setl` + `\x14` + `inecap` 으로 쪼갬
→ `\x14`를 연산자로 해석 시도 → 정의 없음 → 스택 오염
→ 이후 `Lc`(setlinecap 약어) 호출 시 `load` undefined로 표면화
→ `ERROR: undefined / OFFENDING COMMAND: load / STACK: /setlinecap /Lc`

즉 Ghostscript가 보고하는 `load` 오류는 `\x14` 직접 오류가 아니라
**스택 오염의 downstream 증상**임. 오류 메시지만 보고 `load` 관련 문제로
오해하지 말 것.

### ⚠️ 수정 방법 — 반드시 삭제, 공백 치환 금지

**공백(`\x20`)으로 치환하면 안 됨**: `setl inecap`으로 토큰이 분리되어
여전히 undefined 오류 발생.

**올바른 수정: 완전 삭제(`b''`)**
