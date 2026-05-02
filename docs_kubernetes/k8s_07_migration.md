# K8s-07. 기존 데이터 이전 절차

## 이전 대상

| 기존 서버 | 데이터 | k8s 이전 위치 |
|---|---|---|
| node2/18AFD226 (10.100.230.72) | VictoriaMetrics 메트릭 (6개월) | k8s VictoriaMetrics PV |
| node1/18AFD201 (10.100.230.70) | VictoriaMetrics 메트릭 (장기) | k8s VictoriaMetrics PV (통합) |
| node1/18AFD201 (10.100.230.70) | MinIO 모델 아티팩트 | k8s MinIO PV |
| node2/18AFD226 (10.100.230.72) | PostgreSQL DB | k8s PostgreSQL PV |
| node3/18AFD199 (10.100.230.71) | MLflow 실험 기록 | k8s MLflow (MinIO 연동) |

---

## 1. VictoriaMetrics 데이터 이전

VictoriaMetrics는 스냅샷 기능을 제공합니다.

```bash
# 기존 node2에서 스냅샷 생성
curl http://10.100.230.72:8428/snapshot/create
# 응답: {"status":"ok","snapshot":"20250408T120000-xxxxxxxx"}

# 스냅샷 파일 위치
ssh root@10.100.230.72 \
  'ls /var/lib/victoriametrics/snapshots/'

# k8s node1으로 복사
rsync -avz root@10.100.230.72:/var/lib/victoriametrics/snapshots/ \
  root@10.100.230.6:/data/victoria-metrics/snapshots/

# 기존 node1(장기)도 동일하게
curl http://10.100.230.70:8429/snapshot/create
rsync -avz root@10.100.230.70:/var/lib/victoriametrics/snapshots/ \
  root@10.100.230.6:/data/victoria-metrics/snapshots/

# k8s VictoriaMetrics Pod에서 스냅샷 복원
kubectl exec -n failure-prediction \
  $(kubectl get pod -l app=victoria-metrics -n failure-prediction -o jsonpath='{.items[0].metadata.name}') \
  -- vmrestore -src=/storage/snapshots/<snapshot_name> -storageDataPath=/storage
```

---

## 2. MinIO 데이터 이전 (모델 아티팩트)

```bash
# 기존 node1(18AFD201)에서 k8s MinIO로 복사
# mc 클라이언트 설치 후
mc alias set old-minio http://10.100.230.70:9000 minioadmin minioadmin

# k8s MinIO 포트포워딩
kubectl port-forward svc/minio-svc 9000:9000 -n failure-prediction &

mc alias set new-minio http://localhost:9000 minioadmin minioadmin

# 버킷 생성
mc mb new-minio/mlflow-artifacts
mc mb new-minio/training-datasets

# 데이터 복사
mc mirror old-minio/mlflow-artifacts new-minio/mlflow-artifacts
mc mirror old-minio/training-datasets new-minio/training-datasets

# 확인
mc ls new-minio/mlflow-artifacts
```

---

## 3. PostgreSQL 데이터 이전

```bash
# 기존 node2(18AFD226)에서 덤프
ssh root@10.100.230.72 \
  'pg_dump -U hpcdev failure_pred > /tmp/failure_pred.sql'

# 로컬로 복사
scp root@10.100.230.72:/tmp/failure_pred.sql /tmp/

# k8s PostgreSQL에 복원
kubectl port-forward svc/postgresql-svc 5432:5432 -n failure-prediction &
psql -h localhost -U hpcdev -d failure_pred < /tmp/failure_pred.sql

# 확인
psql -h localhost -U hpcdev -d failure_pred -c '\dt'
```

---

## 4. 이전 완료 후 기존 서버 서비스 중단 순서

```bash
# 순서 중요: 검증 완료 후 중단

# 1단계: 기존 FastAPI 중단
ssh root@10.100.230.71 'pkill -f "uvicorn src.api.main"'

# 2단계: 기존 APScheduler 중단 (FastAPI와 함께 중단됨)

# 3단계: 기존 VictoriaMetrics 중단 (데이터 이전 후)
ssh root@10.100.230.72 'systemctl stop victoria-metrics'
ssh root@10.100.230.70 'systemctl stop victoria-metrics'

# 4단계: 기존 PostgreSQL 중단
ssh root@10.100.230.72 'systemctl stop postgresql'

# 5단계: 기존 MinIO 중단
ssh root@10.100.230.70 'systemctl stop minio'

# 6단계: 기존 MLflow 중단
ssh root@10.100.230.71 'systemctl stop mlflow'

# 7단계: 기존 Grafana 중단
ssh root@10.100.230.72 'systemctl stop grafana'
```

---

## 5. Grafana 대시보드 이전

```bash
# 기존 Grafana에서 대시보드 JSON 내보내기
curl -s http://admin:admin@10.100.230.72:3000/api/dashboards/home \
  | jq '.dashboard' > /tmp/dashboard_export.json

# k8s Grafana에 불러오기 (포트포워딩 후)
kubectl port-forward svc/grafana-svc 3001:3000 -n failure-prediction &
curl -X POST http://admin:admin@localhost:3001/api/dashboards/import \
  -H 'Content-Type: application/json' \
  -d @/tmp/dashboard_export.json
```

---

## 이전 검증 체크리스트

```
데이터 이전:
  [ ] VictoriaMetrics 스냅샷 복원 후 메트릭 조회 정상
  [ ] MinIO 버킷/아티팩트 복사 완료
  [ ] PostgreSQL failure_events, training_labels 테이블 확인
  [ ] MLflow 실험 기록 접근 가능

서비스 전환:
  [ ] Ray Serve /predict/all 정상 응답
  [ ] ESXi CronJob → Slack 알림 수신
  [ ] Grafana 대시보드 k8s 클러스터에서 표시
  [ ] 기존 서버 서비스 모두 중단 완료
```
