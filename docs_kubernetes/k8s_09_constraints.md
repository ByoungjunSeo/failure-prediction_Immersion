# K8s-09. 제약 사항 및 검증 체크리스트

## 절대 금지 사항

| 구분 | 금지 내용 |
|---|---|
| 🚫 ESXi SSH | 패키지 설치, 설정 변경, 파일 수정 |
| 🚫 k8s 직접 수정 | 노드에 직접 접속해 k8s 설정 변경 (kubectl만 사용) |
| 🚫 kube-system | kube-system namespace 직접 수정 |
| 🚫 하드코딩 | Secret 값을 코드/YAML에 직접 기재 |
| 🚫 print() | logging 모듈 사용, print() 금지 |
| 🚫 Accuracy | 평가지표로 Accuracy 사용 금지 |

---

## Ray Serve 코드 규칙

```
✅ @serve.deployment 데코레이터 필수
✅ 모델 초기화는 __init__에서만 (요청마다 재로드 금지)
✅ predict 메서드는 async def
✅ num_gpus / num_cpus 명시
✅ GPU 배치: nodeSelector: node1
✅ 환경변수로 서비스 URL 주입 (코드 내 IP 하드코딩 금지)
```

---

## GPU 할당 (node1 Tesla T4 × 4)

```
T4 GPU 0,1 공유: Chronos(0.5) + MOIRAI(0.5) + AnomalyT(0.5) = 1.5장
T4 GPU 3      : CronJob 재학습 (새벽 2시, CUDA_VISIBLE_DEVICES=3)
T4 GPU 2      : 예비 / 대규모 재학습 시 투입
```

---

## Phase별 완료 기준

| Phase | 기준 |
|---|---|
| K-P0 환경 셋업 | node1~6 Ready / GPU 4장 / KubeRay Running / 레지스트리 동작 |
| K-P1 인프라 배포 | PG/MinIO/VM/MLflow/Grafana 모두 Running / MinIO 버킷 생성 |
| K-P2 데이터 이전 | VictoriaMetrics 메트릭 조회 / PG 건수 동일 / MLflow 기록 확인 |
| K-P3 이미지 빌드 | cpu/gpu 이미지 레지스트리 Push / k8s 노드 Pull 성공 |
| K-P4 Ray Serve | RayService Healthy / /predict/all 4대 응답 / < 500ms |
| K-P5 CronJob | 3개 CronJob 정상 / Slack 수신 / VictoriaMetrics 메트릭 유입 |
| K-P6 전환 완료 | 기존 서버 서비스 중단 / k8s 단독 운영 / Grafana 표시 |

---

## 유용한 kubectl 명령어

```bash
# 전체 상태
kubectl get all -n failure-prediction

# Ray Serve 상태
kubectl get rayservice -n failure-prediction

# Pod 로그
kubectl logs -l app=anomaly-service -n failure-prediction --tail=100

# CronJob 수동 실행
kubectl create job <name>-test --from=cronjob/<name> -n failure-prediction

# 리소스 사용량
kubectl top pods -n failure-prediction
kubectl top nodes

# Ray 대시보드
kubectl port-forward svc/failure-pred-cluster-head-svc 8265:8265 -n failure-prediction

# API 접근
kubectl port-forward svc/anomaly-service-serve-svc 8000:8000 -n failure-prediction

# Grafana 접근
kubectl port-forward svc/grafana-svc 3000:3000 -n failure-prediction

# MLflow 접근
kubectl port-forward svc/mlflow-svc 5000:5000 -n failure-prediction
```

---

## 전체 서비스 내부 URL (k8s 클러스터 내부)

```
VictoriaMetrics : http://victoria-metrics-svc:8428
PostgreSQL      : postgresql-svc:5432
MinIO API       : http://minio-svc:9000
MLflow          : http://mlflow-svc:5000
Grafana         : http://grafana-svc:3000
Ray Serve API   : http://anomaly-service-serve-svc:8000
```
