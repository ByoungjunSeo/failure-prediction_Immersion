# 01. 환경 구성

## 개발 서버 3대 상세 스펙

### node3 — AI 학습 서버 ★ Claude Code 실행 (18AFD199)

| 항목 | 내용 |
|---|---|
| Hostname | 18AFD199 |
| IP / BMC | 10.100.230.71 / 10.100.231.71 |
| 접속 | `ssh hpcdev@10.100.230.71` |
| OS | Rocky Linux 9.7 |
| CPU | Intel Xeon Gold 6140 × 2소켓 (36코어 72스레드) |
| GPU | NVIDIA A100 **80GB** HBM2 × 2장 (PCIe) |
| RAM | 64GB DDR4 × 8 = **512GB** |
| Disk | TOSHIBA 600GB × 2 + Micron NVMe 3.84TB × 1 |
| NIC | Intel X722 1GbE + 1GbE 2포트 |
| 서비스 | MLflow, FastAPI, PyTorch, XGBoost, Chronos, MOIRAI, Jupyter, Claude Code |

### node2 — 데이터 수집 및 모니터링 (18AFD226)

| 항목 | 내용 |
|---|---|
| Hostname | 18AFD226 |
| IP / BMC | 10.100.230.72 / 10.100.231.72 |
| 접속 | `ssh hpcdev@10.100.230.72` |
| OS | Rocky Linux 9.7 |
| CPU | Intel Xeon Gold 6140 × 2소켓 |
| RAM | 512GB (64GB × 8) |
| Disk | TOSHIBA 600GB × 2 + Micron NVMe 3.84TB × 1 |
| RAID | Broadcom / LSI MegaRAID SAS-3 3108 |
| 서비스 | Telegraf, VictoriaMetrics, Grafana, Alertmanager, PostgreSQL |

### node1 — 데이터 저장 및 백업 (18AFD201)

| 항목 | 내용 |
|---|---|
| Hostname | 18AFD201 |
| IP / BMC | 10.100.230.70 / 10.100.231.70 |
| 접속 | `ssh hpcdev@10.100.230.70` |
| OS | Rocky Linux 9.7 |
| CPU | Intel Xeon Gold 6140 × 2소켓 |
| RAM | 512GB (64GB × 8) |
| Disk | TOSHIBA 600GB × 2 + Micron NVMe 3.84TB × 1 |
| NIC | Intel X710 10GbE SFP+ (다른 노드 대비 추가) |
| 서비스 | VictoriaMetrics 장기보존, MinIO, PostgreSQL Replica |

---

## ESXi 호스트 (4대 운영)

> ✅ SSH 접속 허용 (읽기/모니터링 목적)
> 🚫 패키지 설치, 설정 파일 수정, 서비스 변경 절대 금지
> ❌ vmgnode17 (10.148.148.117): 접속 장애로 제외

| 호스트 | IP | BMC IP | ESXi 계정 | BMC 계정 |
|---|---|---|---|---|
| vmgnode18 | 10.148.148.118 | 172.31.201.118 | root / VMware!0 | admin / admin |
| vmgnode23 | 10.148.148.123 | 172.31.201.123 | root / VMware!0 | admin / admin |
| vmgnode26 | 10.148.148.126 | 172.31.201.126 | root / VMware!0 | admin / admin |
| vmgnode30 | 10.148.148.130 | 172.31.201.130 | root / VMware!0 | admin / admin |

### ESXi SSH로 가능한 추가 수집

```bash
# SSH로 가져올 수 있는 정보 (pyVmomi 대비 추가)
ssh root@10.148.148.118 'esxcli hardware memory get'
ssh root@10.148.148.118 'esxcli system stats memory get'
ssh root@10.148.148.118 'esxcli hardware ipmi fru list'   # DIMM 물리 정보
ssh root@10.148.148.118 'cat /var/log/vmkernel.log | grep -i "memory\|error" | tail -200'
ssh root@10.148.148.118 'vim-cmd hostsvc/hostsummary'
ssh root@10.148.148.118 'esxcli system syslog config get'
```

---

## 계정 초기 설정 (최초 1회, root로 실행)

```bash
# ── 3대 서버 모두 동일 실행 (node1, node2, node3) ──
useradd -m -s /bin/bash hpcdev
passwd hpcdev
usermod -aG wheel hpcdev
echo 'hpcdev ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers.d/hpcdev
chmod 440 /etc/sudoers.d/hpcdev
mkdir -p /opt/failure_prediction
chown -R hpcdev:hpcdev /opt/failure_prediction
```

```bash
# ── node3에서만: SSH 키 배포 ──
su - hpcdev
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ''
ssh-copy-id hpcdev@10.100.230.70   # node1
ssh-copy-id hpcdev@10.100.230.72   # node2

# 연결 확인
ssh hpcdev@10.100.230.70 'echo node1 ok'
ssh hpcdev@10.100.230.72 'echo node2 ok'
```

---

## node3 ML 스택 설치

```bash
conda create -n failure_pred python=3.11 -y
conda activate failure_pred

# PyTorch (CUDA 12.1)
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121

# 오픈소스 시계열 모델 (핵심)
pip install chronos-forecasting          # Amazon Chronos
pip install uni2ts                       # Salesforce MOIRAI
pip install git+https://github.com/thuml/Anomaly-Transformer  # Anomaly Transformer

# ML 패키지
pip install xgboost lightgbm scikit-learn imbalanced-learn shap optuna
pip install mlflow fastapi uvicorn
pip install pandas numpy scipy
pip install apscheduler prometheus-client
pip install pyVmomi paramiko pyyaml python-dotenv
pip install pytest pytest-httpserver

# GPU 확인
python -c "import torch; print(torch.cuda.device_count(), torch.cuda.get_device_name(0))"
# 기대: 2  NVIDIA A100 80GB PCIe
```

---

## configs/esxi.yaml

```yaml
# vCenter 미사용 — ESXi 직접 접근
# vmgnode17: 접속 장애로 제외
esxi_hosts:
  - id: vmgnode18
    ip: 10.148.148.118
    bmc_ip: 172.31.201.118
    username: root
    password: ${ESXI_PASSWORD}
  - id: vmgnode23
    ip: 10.148.148.123
    bmc_ip: 172.31.201.123
    username: root
    password: ${ESXI_PASSWORD}
  - id: vmgnode26
    ip: 10.148.148.126
    bmc_ip: 172.31.201.126
    username: root
    password: ${ESXI_PASSWORD}
  - id: vmgnode30
    ip: 10.148.148.130
    bmc_ip: 172.31.201.130
    username: root
    password: ${ESXI_PASSWORD}

collection:
  interval_seconds: 60
  timeout_seconds: 10
  retry_count: 3
  ssh_key_path: ~/.ssh/id_ed25519   # ESXi SSH 키 (선택)
```

## .env 파일

```bash
ESXI_PASSWORD=VMware!0
DB_PASSWORD=your_pg_password
SLACK_WEBHOOK_URL=https://hooks.slack.com/...
MLFLOW_TRACKING_URI=http://localhost:5000
MINIO_ENDPOINT=http://10.100.230.70:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
```
