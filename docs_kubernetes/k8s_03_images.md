# K8s-03. 컨테이너 이미지 빌드

## 이미지 구성

```
10.100.230.6:5000/failure-pred:gpu-latest  ← Head Pod (node1, T4 GPU)
  base: rayproject/ray:2.9.0-gpu
  포함: Chronos, MOIRAI, Anomaly Transformer, PyTorch+CUDA

10.100.230.6:5000/failure-pred:cpu-latest  ← Worker Pod (node2~6)
  base: rayproject/ray:2.9.0
  포함: XGBoost, pyVmomi, paramiko, 수집기, 피처 계산
```

---

## Dockerfile.ray-gpu

```dockerfile
FROM rayproject/ray:2.9.0-gpu

USER root
WORKDIR /app

RUN apt-get update && apt-get install -y \
    ipmitool openssh-client curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements-ray-gpu.txt .
RUN pip install --no-cache-dir -r requirements-ray-gpu.txt

COPY src/ ./src/
COPY k8s/configmaps/ ./configs/

USER ray
```

## requirements-ray-gpu.txt

```
ray[serve]==2.9.0
torch==2.1.0+cu121 --extra-index-url https://download.pytorch.org/whl/cu121
chronos-forecasting==1.3.0
uni2ts==1.1.0
git+https://github.com/thuml/Anomaly-Transformer
fastapi==0.108.0
uvicorn==0.27.1
mlflow==2.9.0
boto3==1.34.0
pandas==2.1.0
numpy==1.26.0
scipy==1.11.0
shap==0.44.0
prometheus-client==0.19.0
pyyaml==6.0.1
psycopg2-binary==2.9.9
```

---

## Dockerfile.ray-cpu

```dockerfile
FROM rayproject/ray:2.9.0

USER root
WORKDIR /app

RUN apt-get update && apt-get install -y \
    ipmitool openssh-client curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements-ray-cpu.txt .
RUN pip install --no-cache-dir -r requirements-ray-cpu.txt

COPY src/ ./src/
COPY k8s/configmaps/ ./configs/

USER ray
```

## requirements-ray-cpu.txt

```
ray[serve]==2.9.0
xgboost==2.0.0
scikit-learn==1.3.0
imbalanced-learn==0.11.0
pyVmomi==8.0.1
paramiko==3.4.0
mlflow==2.9.0
boto3==1.34.0
pandas==2.1.0
numpy==1.26.0
scipy==1.11.0
optuna==3.5.0
apscheduler==3.10.4
prometheus-client==0.19.0
pyyaml==6.0.1
psycopg2-binary==2.9.9
python-dotenv==1.0.0
```

---

## 이미지 빌드 및 푸시

```bash
cd /opt/k8s_migration

# 기존 소스 복사
cp -r /opt/failure_prediction/src/collectors src/
cp -r /opt/failure_prediction/src/features   src/
cp -r /opt/failure_prediction/src/training   src/
cp -r /opt/failure_prediction/src/esxi       src/

# CPU 이미지
docker build -f docker/Dockerfile.ray-cpu \
  -t 10.100.230.6:5000/failure-pred:cpu-latest .
docker push 10.100.230.6:5000/failure-pred:cpu-latest

# GPU 이미지 (시간 오래 걸림 — CUDA 패키지)
docker build -f docker/Dockerfile.ray-gpu \
  -t 10.100.230.6:5000/failure-pred:gpu-latest .
docker push 10.100.230.6:5000/failure-pred:gpu-latest

# 확인
curl http://10.100.230.6:5000/v2/failure-pred/tags/list
```
