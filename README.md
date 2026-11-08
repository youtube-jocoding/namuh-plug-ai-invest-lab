# 나무플러그 AI 투자 실험실

**NAMUH Plug AI Invest Lab** · 후원: **NH투자증권 나무플러그**

NH투자증권 나무플러그의 후원을 받아 진행한 AI 투자 실험에서 출발한 독립 오픈소스 프로젝트입니다. NH투자증권의 공식 제품·공식 SDK는 아닙니다. 후원 정보와 별개로 실험의 한계와 검증 결과를 공개합니다.

여러 AI 모델의 투자 결정을 **같은 데이터·같은 체결 조건·같은 원장**으로 비교하는 공개 연구용 백테스트 도구입니다.

Python 3.11 이상에서 외부 패키지 없이 실행됩니다. 기본 데모는 인터넷·API 키·과금 없이 작동합니다. 실제 AI 비교에는 사용자가 선택한 OpenRouter 모델과 사용 권한이 있는 CSV 데이터가 필요합니다.

> 실거래 프로그램이 아닙니다. 증권사 로그인·계좌·주문 API가 없으며, 모든 거래는 독립된 SQLite 가상계좌에 기록합니다. 기본 데모의 여섯 참가자는 **AI 모델이 아닌 결정론적 예제 전략**입니다.

## 1. 1분 만에 실행

이 저장소 폴더에서 실행합니다.

```sh
python --version
python -m namuh_lab demo --out runs/demo
python -m namuh_lab audit --out runs/demo/run
```

생성된 `runs/demo/run/report.html`을 브라우저로 열면 순위·수익률 경로·원본 판단과 수량 근거를 확인할 수 있습니다. 같은 폴더에 `report.json`, `trades.csv`, `experiment.sqlite`도 생성됩니다. 보고서는 외부 폰트·CDN·분석 도구 없이 오프라인으로 열립니다.

설치형 명령이 필요하면 선택적으로 실행하세요.

```sh
python -m pip install -e .
namuh-lab demo --out runs/another-demo
```

이미 있는 출력 폴더를 덮어쓰지 않습니다. 두 번 실행하려면 다른 폴더 이름을 쓰세요.

## 2. 실제 데이터로 준비

1. [데이터 형식](docs/DATA.md)에 맞는 `sessions.csv`, `bars.csv`를 준비합니다. **판단 기간 앞에 최소 20개 완료 봉**이 더 필요합니다.
2. `examples/config.openrouter.json`을 복사합니다. 기간·시장별 자금·데이터 경로·수수료·모델 6개를 지정합니다. 비용 예시는 현행 세율이 아닙니다.
3. 모델 ID는 현재 카탈로그에서 찾습니다. 자동 모델 교체나 대체 공급자는 허용하지 않습니다.

```sh
python -m namuh_lab models --query deepseek
```

`model`은 정확한 OpenRouter ID, `provider`는 공급자 slug, `provider_name`은 응답에 표시되는 공급자 이름입니다. OpenRouter 모델 페이지와 [provider routing 문서](https://openrouter.ai/docs/guides/routing/provider-selection)를 확인하세요. 공급자가 응답에서 달라지면 회차 전체를 중단합니다.

4. 데이터와 설정을 검증합니다. 이 명령은 모델을 호출하지 않습니다.

```sh
python -m namuh_lab validate --config my-config.json
```

## 3. OpenRouter 실행

API 키는 파일에 넣거나 커밋하지 말고 환경변수로 전달합니다. 이 프로그램은 상위 폴더나 `.env` 파일을 자동으로 읽지 않습니다.

PowerShell:

```powershell
$env:OPENROUTER_API_KEY = Read-Host 'OpenRouter API key' -MaskInput
python -m namuh_lab run --config my-config.json --out runs/experiment-01 --allow-paid
```

`-MaskInput`이 없는 Windows PowerShell 5.1에서는 PowerShell 7을 쓰거나 OS의 환경변수 설정 기능을 사용하세요. 셸 기록에 실제 키를 직접 입력하는 방식은 피하세요.

bash/zsh:

```sh
read -rs OPENROUTER_API_KEY
export OPENROUTER_API_KEY
python -m namuh_lab run --config my-config.json --out runs/experiment-01 --allow-paid
unset OPENROUTER_API_KEY
```

`--allow-paid` 없이는 모델 호출이 시작되지 않습니다. `max_api_budget_usd`는 실행별 예산, `credit_reserve_usd`는 최소 회차 예약액입니다. 입력 바이트 수와 카탈로그 단가로 계산한 보수적 예상액이 더 크면 그 값을 사용합니다. 이는 **비용 예약 추정**이지 결제 금액 보증이 아닙니다. 추론 토큰·공급자 과금 방식·다른 프로그램의 동시 사용에 따라 달라질 수 있습니다.

각 회차 전에 계정 잔액과 키 한도를 확인합니다. 그래도 호출 중 402·인증·속도 제한·공급자 장애·통신 장애가 발생하면 **다른 참가자까지 포함한 회차 전체를 체결 전에 취소하고 실행을 중단**합니다. 성공한 다른 모델의 호출 비용은 남을 수 있습니다.

## 4. 중단 후 재개

```sh
python -m namuh_lab report --out runs/experiment-01
python -m namuh_lab audit --out runs/experiment-01
python -m namuh_lab run --config my-config.json --out runs/experiment-01 --resume --allow-paid
```

- 이미 처리했거나 중단된 회차는 다시 호출하지 않습니다. 재개는 다음 미처리 회차부터 진행합니다.
- 코드·설정·입력 데이터 해시가 바뀌면 재개를 거부합니다. 수정 실험은 새로운 폴더에서 시작합니다.
- 중단 때문에 잃은 판단 기회를 “복원했다”고 해석하지 않습니다. 결과에 취소 기록이 남습니다.
- 호출 비용이 불명확하면 추가 유료 호출을 차단합니다. 공급자 청구 기록을 확인한 뒤 해당 요청에 비용을 별도 등록해야 합니다.

```sh
python -m namuh_lab resolve-cost --out runs/experiment-01 --round ROUND_ID --participant Model1 --usd 0 --note "Provider billing confirmed no charge"
```

위 `0`은 사용 예시일 뿐입니다. 실제 청구액을 확인하세요. 원본 응답·오류는 유지되고 비용 대조 기록만 추가됩니다. 회차 ID와 참가자 이름은 `report.json` 및 SQLite의 `requests`에서 확인할 수 있습니다.

## 비교 규칙

| 항목 | 동작 |
|---|---|
| 판단 입력 | 실행 봉 이전에 공개된 완료 봉만 사용. 같은 시장 데이터와 해시를 모두에게 전달 |
| 개인 입력 | 자기 현금·보유 수량·최근 자기 응답 4개만 추가 |
| 주문 | BUY / SELL / 빈 주문 목록(HOLD), 정수 주식, 종목·시점·수량 이유 필수 |
| 체결 | 다음 정규 세션 시가 + 고정 슬리피지·수수료, 과거 거래량 기준 참여 한도 |
| 체결 한도 | 각 가상계좌에 같은 유동성 한도. 모델끼리 실제 호가를 경쟁 소모하지 않음 |
| 현금·수량 | 차입·공매도·환전·소수점 수량 없음. 갭으로 예산 초과 시 해당 모델 주문 묶음 거절 |
| 공통 장애 | 모든 참가자 회차 취소. 가격을 본 뒤 일부 모델만 재호출하지 않음 |
| 모델 출력 오류 | 해당 모델의 회차 미거래. 원본 응답과 오류 저장 |
| 재시작 | 파일 잠금 + 트랜잭션 + 회차별 고유 키. 잠재적으로 과금된 요청 자동 재호출 금지 |
| 평가 | 시장별 초기 자금 대비 수익률의 동일 가중 평균, 관측 최대 낙폭, 체결 수·비용 별도 |

장중 실시간 시세 연결을 작성할 때 사용할 수 있는 `namuh_lab/quotes.py`도 포함합니다. 이 모듈은 **마감 다음 초까지 기다린 뒤 공통 호가를 수집**하므로 첫 조회 종목의 초 단위 시각을 소수점 마감보다 과거로 오인하는 문제를 예방합니다. 데이터 공급자와 네트워크 타임아웃을 연결하는 작업은 별도이며, 이 모듈만으로 실시간 대회가 실행되지는 않습니다.

## 지원 범위와 정직한 한계

- 과거 데이터에 대한 **현재 LLM의 답변**입니다. 모델이 이후 사건을 학습했을 수 있어, 진짜 전진 검증이나 미래를 모르는 투자자의 성과와 동일하지 않습니다.
- 일봉 시가 체결은 가정입니다. 실제 호가·체결 대기열·시장 충격·매도 대금 결제 대기·환전은 구현하지 않습니다.
- 기본 후보군은 고정입니다. 상장폐지·신규상장과 당시 시장 편입 이력이 반영되지 않으면 생존편향이 생깁니다. 현재 버전은 데이터 누락을 자동으로 메우지 않고 중단합니다.
- 분할·병합은 발견 시 중단합니다. 조정주가와 원주가를 섞거나 주식 수를 추정하지 않습니다. 배당은 [명시적인 순현금 지급 이벤트](docs/DATA.md)만 지원합니다.
- DART·EDGAR·일반 뉴스 자동 수집은 없습니다. 당시 이용 가능했던 공시 원문 아카이브 없이 현재 문서를 과거 판단에 넣으면 정보 누출이 됩니다.
- 기본 데모와 오프라인 테스트가 통과해도 특정 외부 공급자의 미래 가용성·응답 호환성까지 보장하지는 않습니다. 실제 유료 모델 연결은 사용자 키와 모델 권한으로 검증해야 합니다.
- 원본 응답은 공개 가능한 설명이며 모델 내부의 완전한 사고 과정이 아닙니다. 긴 설명 자체를 투자 실력으로 채점하지 마세요.

## 검증과 공개

```sh
python -m unittest discover -s tests -v
python scripts/check_public.py
```

GitHub Actions는 Windows·macOS·Linux, Python 3.11~3.13 조합을 실행하도록 구성했습니다. 실제 실행 결과는 해당 저장소 CI에서 확인하세요.

이 소스에는 실험자의 계좌·키·이메일·개인 서버 주소·실제 대회 거래 DB·실제 모델 응답이 포함되지 않습니다. 생성된 `runs/`는 기본적으로 Git에서 제외합니다. **사용자 실행 결과에는 입력한 종목명·모델 응답이 들어가므로 직접 검토한 뒤 공개하세요.**

- [데이터 계약과 예제](docs/DATA.md)
- [설계와 장애 정책](docs/DESIGN.md)
- [개발·기여](CONTRIBUTING.md)
- [보안 안내](SECURITY.md)

MIT License. 투자 수익이나 무중단 운영을 보장하지 않는 연구용 도구입니다.
