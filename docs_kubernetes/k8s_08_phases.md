# K8s-08. Phase별 Claude Code 지시문

> Claude Code는 기존 node3(18AFD199/10.100.230.71)에서 실행
> kubectl로 k8s 클러스터(node1, 10.100.230.6) 원격 제어

---

## K-P0 — k8s 환경 셋업 (Day 1)

```
다음을 순서대로 실행해줘:

1. kubectl 원격 설정:
   mkdir -p ~/.kube
   scp root@10.100.230.6:/etc/kubernetes/admin.conf ~/.kube/config
   chmod 600 ~/.kube/config
   kubectl get nodes -o wide

2. 클러스터 상태 확인:
   kubectl get pods --all-namespaces
   kubectl describe node node1 | grep -i "gpu\|nvidia\|tesla\|capacity" -A2

3. GPU 확인 (node1에 T4×4 인식 여부):
   kubectl get nodes -o json | \
     jq '.items[] | select(.metadata.name=="node1") | .status.capacity'
   # "nvidia.com/gpu": "4" 필수

4. KubeRay Operator 설치:
   helm repo add kuberay https://ray-project.github.io/kuberay-helm/
   helm repo update
   helm install kuberay-operator kuberay/kuberay-operator \
     --namespace ray-system --create-namespace --version 1.1.0
   kubectl get pods -n ray-system -w

5. Namespace 생성:
   kubectl create namespace failure-prediction

6. node1에 로컬 레지스트리 설치:
   ssh root@10.100.230.6 \
     "docker run -d -p 5000:5000 --restart=always --name registry registry:2"
   curl http://10.100.230.6:5000/v2/_catalog

7. node1 스토리지 디렉토리 준비:
   ssh root@10.100.230.6 \
     'mkdir -p /data/victoria-metrics /data/postgresql /data/minio /data/mlflow'

8. 프로젝트 디렉토리 생성 및 기존 코드 복사:
   mkdir -p /opt/k8s_migration/{src/ray_serve,k8s/{storage,infra,configmaps,cronjobs},docker,docs}
   cp -r /opt/failure_prediction/src/collectors /opt/k8s_migration/src/
   cp -r /opt/failure_prediction/src/features   /opt/k8s_migration/src/
   cp -r /opt/failure_prediction/src/training   /opt/k8s_migration/src/
   cp -r /opt/failure_prediction/src/esxi       /opt/k8s_migration/src/

완료 기준:
- kubectl get nodes에서 node1~6 모두 Ready
- node1 nvidia.com/gpu: 4 확인
- kuberay-operator Pod Running
- 로컬 레지스트리 http://10.100.230.6:5000 응답
```

---

## K-P1 — 스토리지 & 인프라 배포 (Day 2~3)

```
docs/k8s_01_environment.md, k8s_02_infra.md를 참고해서:

1. StorageClass 및 PV/PVC 생성:
   k8s/storage/storageclass-local.yaml 작성 후 apply
   kubectl get pv

2. Secret 생성 (실제 값으로):
   kubectl create secret generic esxi-credentials \
     --from-literal=password=VMware!0 -n failure-prediction
   kubectl create secret generic slack-secret \
     --from-literal=webhook-url=<실제URL> -n failure-prediction
   kubectl create secret generic minio-secret \
     --from-literal=access-key=minioadmin \
     --from-literal=secret-key=minioadmin -n failure-prediction
   kubectl create secret generic pg-secret \
     --from-literal=password=pgpassword -n failure-prediction

3. ConfigMap 생성:
   k8s/configmaps/servers-config.yaml 작성 후 apply

4. 인프라 서비스 배포 (순서대로):
   kubectl apply -f k8s/infra/postgresql.yaml
   kubectl apply -f k8s/infra/minio.yaml
   kubectl apply -f k8s/infra/victoria-metrics.yaml
   kubectl apply -f k8s/infra/mlflow.yaml
   kubectl apply -f k8s/infra/grafana.yaml
   kubectl get all -n failure-prediction

5. MinIO 버킷 생성:
   kubectl exec -n failure-prediction \
     $(kubectl get pod -l app=minio -n failure-prediction -o jsonpath='{.items[0].metadata.name}') \
     -- sh -c 'mc alias set local http://localhost:9000 minioadmin minioadmin && \
               mc mb local/mlflow-artifacts && mc mb local/training-datasets'

완료 기준:
- postgresql, minio, victoria-metrics, mlflow, grafana Pod 모두 Running
- MinIO 버킷 2개 생성 확인
- MLflow UI 접근: kubectl port-forward svc/mlflow-svc 5000:5000 -n failure-prediction
```

---

## K-P2 — 기존 데이터 이전 (Day 3~4)

```
docs/k8s_07_migration.md를 참고해서:

1. VictoriaMetrics 스냅샷 이전:
   curl http://10.100.230.72:8428/snapshot/create
   curl http://10.100.230.70:8429/snapshot/create
   rsync 명령으로 k8s node1(/data/victoria-metrics/)으로 복사

2. MinIO 데이터 이전:
   kubectl port-forward svc/minio-svc 9000:9000 -n failure-prediction &
   mc mirror old-minio/mlflow-artifacts new-minio/mlflow-artifacts
   mc mirror old-minio/training-datasets new-minio/training-datasets

3. PostgreSQL 데이터 이전:
   pg_dump -h 10.100.230.72 -U hpcdev failure_pred > /tmp/failure_pred.sql
   kubectl port-forward svc/postgresql-svc 5432:5432 -n failure-prediction &
   psql -h localhost -U hpcdev -d failure_pred < /tmp/failure_pred.sql

4. 이전 검증:
   kubectl port-forward svc/victoria-metrics-svc 8428:8428 -n failure-prediction &
   curl 'http://localhost:8428/api/v1/query?query=up' | jq '.data.result | length'
   psql -h localhost -U hpcdev -d failure_pred -c 'SELECT COUNT(*) FROM training_labels;'

완료 기준:
- VictoriaMetrics에 기존 메트릭 데이터 조회 가능
- PostgreSQL training_labels 건수 이전 전과 동일
- MLflow에서 기존 실험 기록 확인 가능
```

---

## K-P3 — Docker 이미지 빌드 (Day 4~5)

```
docs/k8s_03_images.md를 참고해서:

1. Dockerfile 파일 작성:
   docker/Dockerfile.ray-gpu 생성
   docker/Dockerfile.ray-cpu 생성
   requirements-ray-gpu.txt 생성
   requirements-ray-cpu.txt 생성

2. src/ray_serve/ensemble_app.py 작성:
   docs/k8s_04_rayserve.md 참고

3. CPU 이미지 빌드 먼저 (GPU보다 빠름):
   cd /opt/k8s_migration
   docker build -f docker/Dockerfile.ray-cpu \
     -t 10.100.230.6:5000/failure-pred:cpu-latest .
   docker push 10.100.230.6:5000/failure-pred:cpu-latest

4. GPU 이미지 빌드:
   docker build -f docker/Dockerfile.ray-gpu \
     -t 10.100.230.6:5000/failure-pred:gpu-latest .
   docker push 10.100.230.6:5000/failure-pred:gpu-latest

5. 모든 k8s 노드에서 이미지 pull 확인:
   for ip in 10.100.230.41 10.100.230.42 10.100.230.43; do
     ssh root@$ip "docker pull 10.100.230.6:5000/failure-pred:cpu-latest"
   done

완료 기준:
- 레지스트리에 cpu, gpu 태그 모두 확인
- k8s 노드에서 pull 성공
```

---

## K-P4 — Ray Serve 배포 (Day 5~6)

```
docs/k8s_04_rayserve.md를 참고해서:

1. RayService YAML 작성:
   k8s/rayservice.yaml 작성

2. RayService 배포:
   kubectl apply -f k8s/rayservice.yaml
   kubectl get rayservice -n failure-prediction -w
   kubectl get pods -n failure-prediction -w

3. Ray 대시보드 접근:
   kubectl port-forward svc/failure-pred-cluster-head-svc \
     8265:8265 -n failure-prediction &
   # http://localhost:8265 에서 Deployment 상태 확인

4. API 테스트:
   kubectl port-forward svc/anomaly-service-serve-svc \
     8000:8000 -n failure-prediction &
   curl http://localhost:8000/health
   curl http://localhost:8000/predict/vmgnode18
   curl http://localhost:8000/predict/all

완료 기준:
- RayService Running/Healthy 상태
- /predict/all 응답에 vmgnode18~30 결과 4개 포함
- model_scores 4개 모두 포함 (chronos, moirai, xgboost, anomaly_t)
- 응답 시간 < 500ms
```

---

## K-P5 — CronJob 배포 (Day 6~7)

```
docs/k8s_05_data_collection.md, k8s_06_esxi.md를 참고해서:

1. 수집기 Job 스크립트 작성:
   src/collectors/esxi_collector_job.py 작성
   src/esxi/response_job.py 작성

2. CronJob YAML 작성 및 배포:
   kubectl apply -f k8s/cronjobs/esxi-collector.yaml
   kubectl apply -f k8s/cronjobs/esxi-response.yaml
   kubectl apply -f k8s/cronjobs/retrain-cronjob.yaml
   kubectl apply -f k8s/cronjobs/weekly-tune.yaml

3. 수동 테스트 (1분 기다리지 않고 즉시 실행):
   kubectl create job esxi-collect-test \
     --from=cronjob/esxi-collector -n failure-prediction
   kubectl logs job/esxi-collect-test -n failure-prediction -f

   kubectl create job esxi-response-test \
     --from=cronjob/esxi-response -n failure-prediction
   kubectl logs job/esxi-response-test -n failure-prediction -f

완료 기준:
- esxi-collector: VictoriaMetrics에 메트릭 전송 확인
- esxi-response: Slack 알림 수신 확인
- audit_log DB에 대응 이력 기록 확인
```

---

## K-P6 — 기존 서비스 중단 및 전환 완료 (Day 7~8)

```
docs/k8s_07_migration.md를 참고해서:

1. 최종 검증 (기존 서비스 중단 전):
   - k8s Ray Serve API 정상 응답 확인
   - k8s CronJob 1분 주기 정상 동작 확인
   - k8s Grafana 대시보드 접근 확인
   - k8s PostgreSQL 데이터 정상 확인

2. 기존 서비스 중단 (순서대로):
   ssh root@10.100.230.71 'pkill -f "uvicorn src.api.main" || true'
   ssh root@10.100.230.72 'systemctl stop victoria-metrics grafana postgresql || true'
   ssh root@10.100.230.70 'systemctl stop victoria-metrics minio || true'
   ssh root@10.100.230.71 'systemctl stop mlflow || true'

3. Grafana 대시보드 이전:
   기존 Grafana에서 JSON 내보내기 → k8s Grafana에 import

4. Telegraf DaemonSet 배포:
   k8s/infra/telegraf-daemonset.yaml 작성 후 apply
   kubectl get pods -l app=telegraf -n failure-prediction

5. CLAUDE.md 업데이트:
   현재 Phase: K-P6 완료
   기존 서버: 이전 완료, 서비스 중단

완료 기준:
- 기존 서버 3대 서비스 모두 중단
- k8s Ray Serve 단독 운영 확인
- Slack CronJob 알림 정상
- Grafana 대시보드 k8s에서 표시
- kubectl get all -n failure-prediction 전체 Running
```
