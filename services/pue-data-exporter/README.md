# PUE Data Exporter

PUE 측정/분석용 raw 메트릭 데이터 추출 도구.

## 사용 방법

### 방법 1: Grafana 통한 접근 (권장)

1. `http://10.100.230.130:31618` 접속
2. Self-Monitoring 폴더 → **"데이터 추출 (Data Export)"** 대시보드
3. 우상단 시간 범위 설정 (예: Last 1 hour)
4. **"데이터 추출 페이지 열기"** 링크 클릭 → 새 탭 (현재 시간 범위 자동 전달)
5. 메트릭 그룹 선택 + 다운로드

### 방법 2: 직접 URL

`http://10.100.230.130:31620` → 시간 미지정 시 최근 1시간

## 메트릭 그룹

| 그룹 | 내용 | 노드 |
|-----|------|-----|
| GPU | 사용률/전력/메모리/온도 | node1~4 |
| NPU | 사용률/전력/메모리/온도 | node5 |
| CPU | 사용률/Load Average | node1~5 |
| 메모리 | 사용량/사용률/총량 | node1~5 |
| 디스크 | 사용량/사용률 (`/`, 루트) | node1~5 |
| 네트워크 | 송신/수신 (MB/s) | node1~5 |

## 출력 형식

tar.gz 풀면:

- 메트릭별 **wide-format CSV** (`datetime` + node 컬럼)
- `README.txt` (단위/노드 정보/Excel 팁)
- `MANIFEST.txt` (시간 범위/행 수)

특징: UTF-8 BOM (Excel 한글 호환), datetime 은 **KST**, 단위는 파일명·README 에 명시.

예시 `gpu_power_w.csv`:

```
datetime,node1,node2,node3,node4
2026-06-22 15:15:00,10.79,145.91,145.21,287.31
...
```

계산형/단위변환은 PromQL 표현식으로 처리한다:
CPU 사용률 = `100 - avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[rate_window])) * 100`,
메모리/디스크 사용률, 네트워크 MB/s(rate), GPU MiB→GB, NPU bytes→MB 등.
NPU 사용률은 8코어 평균, NPU 온도는 `peak` 라벨, 네트워크는 물리 NIC(`device=~"en.*"`)만 합산.

## 아키텍처

- 백엔드: FastAPI (Python) — `app.py`(UI + `/export` + `/healthz`), `exporter.py`(추출 로직)
- Pod: **ConfigMap 마운트 방식** — 공유 이미지 `failure-pred:gpu-latest` 에 fastapi/uvicorn/requests 가
  이미 있어 별도 이미지 빌드 없이 코드만 주입 (`Dockerfile` 은 독립 빌드용으로 보존)
- Service: NodePort 31620
- 데이터 소스: Prometheus(인프라) + VictoriaMetrics(PUE/AI). URL 은 `pue-data-exporter-config` ConfigMap 에서 env 주입

## 배포

매니페스트: [`deploy/exporter/`](../../deploy/exporter/), 대시보드: [`k8s/grafana/dashboards/data-export.json`](../../k8s/grafana/dashboards/data-export.json)

```bash
# 코드 ConfigMap (app.py + exporter.py 만)
kubectl -n failure-prediction create configmap pue-data-exporter-app \
  --from-file=app.py=services/pue-data-exporter/app.py \
  --from-file=exporter.py=services/pue-data-exporter/exporter.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f deploy/exporter/configmap.yaml
kubectl apply -f deploy/exporter/deployment.yaml
kubectl apply -f deploy/exporter/service.yaml
```

## 코드 수정 시

`app.py` 또는 `exporter.py` 수정 후 ConfigMap 재생성 + Pod 재시작:

```bash
kubectl -n failure-prediction create configmap pue-data-exporter-app \
  --from-file=app.py=services/pue-data-exporter/app.py \
  --from-file=exporter.py=services/pue-data-exporter/exporter.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n failure-prediction rollout restart deployment pue-data-exporter
```

## Grafana 패널 메모

Grafana Text 패널은 기본적으로 HTML 을 sanitize 해 iframe 을 제거한다(`disable_sanitize_html=false`).
전역 보안 설정 변경 대신 **markdown 링크** 방식을 사용해 `${__from:date:iso}`/`${__to:date:iso}` 시간 변수를 exporter URL 에 전달한다.

## 트러블슈팅

- 외부 접근 안 됨 → 방화벽 확인 (31620 포트)
- 응답 timeout → 큰 시간 범위는 step 키우기 (1m, 5m)
- CSV 에 빈 셀 → 그 시각 노드 데이터 없음 (스크레이프 누락)
