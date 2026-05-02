# K8s-02. 인프라 서비스 k8s 배포

## 전체 인프라를 k8s로 이전합니다.

```
기존 서버          →  k8s Namespace: failure-prediction
─────────────────────────────────────────────────────
node2(18AFD226)
  VictoriaMetrics  →  StatefulSet (node1 PV)
  Telegraf         →  DaemonSet
  Grafana          →  Deployment
  Alertmanager     →  Deployment
  PostgreSQL       →  StatefulSet (node1 PV)

node1(18AFD201)
  VictoriaMetrics  →  (단일 인스턴스로 통합, 보존 기간 24개월)
  MinIO            →  StatefulSet (node1 PV)
  PostgreSQL       →  (위 PG에 통합)

node3(18AFD199)
  MLflow           →  Deployment
  FastAPI          →  Ray Serve로 대체
```

---

## 1. VictoriaMetrics StatefulSet

```yaml
# k8s/infra/victoria-metrics.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: pvc-victoria-metrics
  namespace: failure-prediction
spec:
  storageClassName: local-nvme
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 500Gi
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: victoria-metrics
  namespace: failure-prediction
spec:
  serviceName: victoria-metrics-svc
  replicas: 1
  selector:
    matchLabels:
      app: victoria-metrics
  template:
    metadata:
      labels:
        app: victoria-metrics
    spec:
      nodeSelector:
        kubernetes.io/hostname: node1    # NVMe 스토리지 노드
      containers:
        - name: victoria-metrics
          image: victoriametrics/victoria-metrics:latest
          args:
            - -storageDataPath=/storage
            - -retentionPeriod=24        # 24개월
            - -httpListenAddr=:8428
          ports:
            - containerPort: 8428
          volumeMounts:
            - name: storage
              mountPath: /storage
      volumes:
        - name: storage
          persistentVolumeClaim:
            claimName: pvc-victoria-metrics
---
apiVersion: v1
kind: Service
metadata:
  name: victoria-metrics-svc
  namespace: failure-prediction
spec:
  selector:
    app: victoria-metrics
  ports:
    - port: 8428
      targetPort: 8428
  type: ClusterIP
```

---

## 2. PostgreSQL StatefulSet

```yaml
# k8s/infra/postgresql.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: pvc-postgresql
  namespace: failure-prediction
spec:
  storageClassName: local-nvme
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 100Gi
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgresql
  namespace: failure-prediction
spec:
  serviceName: postgresql-svc
  replicas: 1
  selector:
    matchLabels:
      app: postgresql
  template:
    metadata:
      labels:
        app: postgresql
    spec:
      nodeSelector:
        kubernetes.io/hostname: node1
      containers:
        - name: postgresql
          image: postgres:15
          env:
            - name: POSTGRES_DB
              value: failure_pred
            - name: POSTGRES_USER
              value: hpcdev
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: pg-secret
                  key: password
          ports:
            - containerPort: 5432
          volumeMounts:
            - name: storage
              mountPath: /var/lib/postgresql/data
      volumes:
        - name: storage
          persistentVolumeClaim:
            claimName: pvc-postgresql
---
apiVersion: v1
kind: Service
metadata:
  name: postgresql-svc
  namespace: failure-prediction
spec:
  selector:
    app: postgresql
  ports:
    - port: 5432
      targetPort: 5432
```

---

## 3. MinIO StatefulSet

```yaml
# k8s/infra/minio.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: pvc-minio
  namespace: failure-prediction
spec:
  storageClassName: local-nvme
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 500Gi
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: minio
  namespace: failure-prediction
spec:
  serviceName: minio-svc
  replicas: 1
  selector:
    matchLabels:
      app: minio
  template:
    metadata:
      labels:
        app: minio
    spec:
      nodeSelector:
        kubernetes.io/hostname: node1
      containers:
        - name: minio
          image: minio/minio:latest
          command: ["minio", "server", "/data", "--console-address", ":9001"]
          env:
            - name: MINIO_ROOT_USER
              valueFrom:
                secretKeyRef:
                  name: minio-secret
                  key: access-key
            - name: MINIO_ROOT_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: minio-secret
                  key: secret-key
          ports:
            - containerPort: 9000   # API
            - containerPort: 9001   # Console
          volumeMounts:
            - name: storage
              mountPath: /data
      volumes:
        - name: storage
          persistentVolumeClaim:
            claimName: pvc-minio
---
apiVersion: v1
kind: Service
metadata:
  name: minio-svc
  namespace: failure-prediction
spec:
  selector:
    app: minio
  ports:
    - name: api
      port: 9000
      targetPort: 9000
    - name: console
      port: 9001
      targetPort: 9001
```

---

## 4. MLflow Deployment

```yaml
# k8s/infra/mlflow.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mlflow
  namespace: failure-prediction
spec:
  replicas: 1
  selector:
    matchLabels:
      app: mlflow
  template:
    metadata:
      labels:
        app: mlflow
    spec:
      containers:
        - name: mlflow
          image: ghcr.io/mlflow/mlflow:v2.9.0
          command:
            - mlflow
            - server
            - --backend-store-uri
            - postgresql+psycopg2://hpcdev:$(POSTGRES_PASSWORD)@postgresql-svc:5432/failure_pred
            - --default-artifact-root
            - s3://mlflow-artifacts/
            - --host
            - "0.0.0.0"
            - --port
            - "5000"
          env:
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: pg-secret
                  key: password
            - name: MLFLOW_S3_ENDPOINT_URL
              value: http://minio-svc:9000
            - name: AWS_ACCESS_KEY_ID
              valueFrom:
                secretKeyRef:
                  name: minio-secret
                  key: access-key
            - name: AWS_SECRET_ACCESS_KEY
              valueFrom:
                secretKeyRef:
                  name: minio-secret
                  key: secret-key
          ports:
            - containerPort: 5000
---
apiVersion: v1
kind: Service
metadata:
  name: mlflow-svc
  namespace: failure-prediction
spec:
  selector:
    app: mlflow
  ports:
    - port: 5000
      targetPort: 5000
```

---

## 5. Grafana Deployment

```yaml
# k8s/infra/grafana.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: grafana
  namespace: failure-prediction
spec:
  replicas: 1
  selector:
    matchLabels:
      app: grafana
  template:
    metadata:
      labels:
        app: grafana
    spec:
      containers:
        - name: grafana
          image: grafana/grafana:latest
          env:
            - name: GF_SECURITY_ADMIN_PASSWORD
              value: admin
          ports:
            - containerPort: 3000
          volumeMounts:
            - name: grafana-storage
              mountPath: /var/lib/grafana
      volumes:
        - name: grafana-storage
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: grafana-svc
  namespace: failure-prediction
spec:
  selector:
    app: grafana
  ports:
    - port: 3000
      targetPort: 3000
  type: NodePort    # 외부 접근용
  # NodePort를 통해 http://10.100.230.6:3xxxx 로 접근 가능
```

---

## 6. Telegraf DaemonSet (k8s 노드 메트릭 수집)

```yaml
# k8s/infra/telegraf-daemonset.yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: telegraf
  namespace: failure-prediction
spec:
  selector:
    matchLabels:
      app: telegraf
  template:
    metadata:
      labels:
        app: telegraf
    spec:
      hostNetwork: true
      hostPID: true
      containers:
        - name: telegraf
          image: telegraf:1.29
          securityContext:
            privileged: true
          volumeMounts:
            - name: telegraf-config
              mountPath: /etc/telegraf/telegraf.conf
              subPath: telegraf.conf
            - name: host-proc
              mountPath: /rootfs/proc
              readOnly: true
            - name: host-sys
              mountPath: /rootfs/sys
              readOnly: true
      volumes:
        - name: telegraf-config
          configMap:
            name: telegraf-config
        - name: host-proc
          hostPath:
            path: /proc
        - name: host-sys
          hostPath:
            path: /sys
```

---

## 전체 인프라 배포 순서

```bash
# 1. Storage
kubectl apply -f k8s/storage/

# 2. 인프라 서비스 (순서 중요)
kubectl apply -f k8s/infra/postgresql.yaml
kubectl apply -f k8s/infra/minio.yaml
kubectl apply -f k8s/infra/victoria-metrics.yaml

# MinIO 버킷 생성 (Pod 뜬 후)
kubectl exec -n failure-prediction \
  $(kubectl get pod -l app=minio -n failure-prediction -o jsonpath='{.items[0].metadata.name}') \
  -- mc alias set local http://localhost:9000 minioadmin minioadmin
kubectl exec ... -- mc mb local/mlflow-artifacts local/training-datasets

# 3. MLflow (MinIO, PG 준비 후)
kubectl apply -f k8s/infra/mlflow.yaml

# 4. Grafana
kubectl apply -f k8s/infra/grafana.yaml

# 5. Telegraf
kubectl apply -f k8s/infra/telegraf-daemonset.yaml

# 전체 확인
kubectl get all -n failure-prediction
```
