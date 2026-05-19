# 워크스테이션 이전 핸드오프 — 2026-05-18

## 프로젝트 한 줄 요약
HPC K8s 클러스터(node1~5)의 **자원사용률 실시간 가시화 + AI 앙상블 기반 장애예측** (5 predictor: Chronos / MOIRAI / AnomalyTransformer / XGBoost / LLMEmbedding(NPU)). 기존 ESXi 4대 외부 모니터링 → 신규 클러스터 자체 모니터링으로 전환 중.

---

## 신규 클러스터 5 노드 (작업 대상)

| 노드 | IP | 역할 | OS | 가속기 |
|---|---|---|---|---|
| node1 | 10.100.230.130 | control-plane + GPU worker | Rocky 9.7 | RTX 5060 Ti 16GB |
| node2 | 10.100.230.131 | GPU worker | Rocky 9.7 | RTX 5060 Ti 16GB |
| node3 | 10.100.230.132 | GPU worker | Rocky 9.7 | RTX 5060 Ti 16GB |
| node4 | 10.100.230.133 | GPU worker | Rocky 9.7 | RTX 5080 16GB |
| node5 | 10.100.230.134 | NPU worker | Ubuntu 22.04 | Furiosa RNGD |

K8s v1.35.4, containerd 2.2.3. 모든 노드에 2.7TB SATA SSD `/dev/sdb`(Longhorn 3-replica용). 같은 사내망(10.100.230.0/24).

접속: `ssh root@10.100.230.130` (key: `~/.ssh/id_ed25519_newcluster`). 비번 백업: `Uniwide1!`.

---

## 진행 상태

### Phase 0~2 완료 (GitHub 커밋됨)

- **Phase 0** (`f0d01df`) — Furiosa NPU exporter, smartctl-exporter, DCGM ServiceMonitor, NPU 4B 모델 업그레이드 (PVC 10→20GB)
- **Phase 1** (`3a2a3db`) — Grafana 3종 대시보드 (Node Exporter Full / DCGM / Furiosa NPU), NodePort 31300 노출
- **Phase 2** (`e47f92a`) — ensemble_app.py 확장 (`/predict/node/*` 5 노드), self-pred-push CronJob(1분), VM datasource, AI 예측 점수 대시보드
- **Phase 0c** — NPU 모델 0.6B → 4B 업그레이드 (embed dim 1024→2560, DRAM 1.5GB→8GB)

### Phase 3 완료 (2026-05-18)

- `k8s/monitoring/alerts/failure-pred-rules.yaml` PrometheusRule (4 group / 9 hardware+pipeline rules) — 클러스터 apply 완료
- `k8s/monitoring/alerts/alertmanager-slack.yaml` AlertmanagerConfig (slack-warning, slack-critical 두 receiver) — apply 완료, 채널: `#액침서버_장애예측_알람`
- `k8s/monitoring/alerts/vmalert.yaml` vmalert Deployment (score 기반 3 rule, `--external.label=namespace=monitoring`) — apply 완료
- Slack webhook 실 URL 양 NS(monitoring, failure-prediction) secret 적용 완료
- ESXi CronJob 4개 (esxi-collector/edac/response, ce-simulator) **suspend=true** — yaml + 클러스터 모두 반영
- self-pred-push CronJob에서 `/predict/esxi/all` 호출 제거 완료
- AI 예측 점수 대시보드 node-only 로 재구성

**종단 검증 결과 (2026-05-18):**
- vmalert→Alertmanager: `alerts_sent_total=69, send_errors=0`
- Alertmanager→Slack: `notifications_total{slack}=6, failed{slack}=0`
- Slack `#액침서버_장애예측_알람` 채널에서 CRITICAL 알림 수신 확인
- null receiver 문제: `--external.label=namespace=monitoring`으로 해결 (operator가 AlertmanagerConfig에 implicit namespace matcher 주입하는 것이 원인이었음)

### 핵심 진단 기록
- **vmalert 의 올바른 API path**: `/api/v1/rules`, `/api/v1/alerts`. `/api/v1/groups` 는 Prometheus 용이고 vmalert 가 거부함 ("unsupported path requested") — 이전에 빈 응답 → JSON decode 실패로 가짜 traceback 만들었음. 운영 영향 없음.
- **Ray head pod 의 Python traceback** 으로 보이던 것은 사실 진단 셸 파이프에서 `wget` 없는 컨테이너의 빈 stdout 을 `python3 -c "json.load"` 가 받아서 호스트(node1)의 python3.9 가 띄운 가짜 traceback. Ray Serve 5 deployment 는 HEALTHY 유지 중.

---

## 운영 URL/포트

| 서비스 | URL | 비고 |
|---|---|---|
| Grafana | http://10.100.230.130:**31300** | admin / `eGiVhmpCwSAXIZMLxHkwu9a3v3LS8e4KmYrvIkTb` |
| Prometheus (in-cluster) | monitoring-kube-prometheus-prometheus.monitoring:9090 | |
| Alertmanager (in-cluster) | monitoring-kube-prometheus-alertmanager.monitoring:9093 | |
| VictoriaMetrics | http://10.100.230.130:**30171** (NodePort) | failure-prediction NS, AI 점수 저장소 |
| Ray Serve API | http://10.100.230.130:**31494** | `/predict/node/all`, `/predict/esxi/all`, `/health` |
| NPU embedding | http://npu-embed-svc.failure-prediction:8000 | OpenAI 호환 `/v1/embeddings` (Qwen3-Embedding-4B) |
| vmalert | http://vmalert.failure-prediction:8880 | `/api/v1/rules`, `/api/v1/alerts` |
| Container Registry | 10.100.230.130:**5000** | hostPath `/home/registry` (13G), standalone Pod — **정전 시 수동 복구 필요** (project_power_outage_state.md 참조) |
| 구 Grafana | http://10.100.230.72:3000 | datasource 만 신규 VM 가리킴 |

---

## TODO 목록

**pending**
- GPU 4대 균등 분산 (predictor replicas 4 + RayCluster worker 4)
- Registry standalone Pod → Deployment + Longhorn PVC 전환 (정전 회복력)
- Phase 4 (옵션): 자기-클러스터 데이터로 Chronos/MOIRAI fine-tune 백그라운드 CronJob

**완료**
- #21 Phase 3: Alertmanager rules + Slack routing — 종단 검증 통과 (2026-05-18)
- #16~20, #22 (설계 + Phase 0/1/2 + NPU 4B 업그레이드)

---

## 새 워크스테이션 정보 + 첫 작업

- 호스트: **10.100.250.103**, 계정 **mlcommons**, 작업 디렉토리 `/mlcommons_cm/failure_prediction`
- 기존 워크스테이션: 10.100.230.6 (node1 of OLD cluster = 신규 클러스터 master 와 IP 다름), root 계정, `/opt/failure_prediction`
- mlcommons 계정이므로 sudo 필요할 수 있음

**새 워크스테이션 첫 작업 순서:**

1. `/tmp/handoff-*.tgz` 압축 풀어 SSH 키 + .gitconfig 복원
   ```bash
   mkdir -p ~/.ssh && tar -xzf /tmp/handoff-*.tgz -C ~/ ; chmod 600 ~/.ssh/id_*
   ```
2. GitHub 저장소 clone:
   ```bash
   cd /mlcommons_cm && git clone git@github.com:ByoungjunSeo/failure-prediction.git failure_prediction
   cd failure_prediction
   git log --oneline -5    # 27d5b3b 까지 확인
   ```
3. 이 문서 (`docs/HANDOFF_TO_NEW_WORKSTATION.md`) 읽기
4. 신규 클러스터 SSH 키 확인 (`~/.ssh/id_ed25519_newcluster`) — handoff 패키지에서 복원되었어야 함
5. ssh config 에 `Host newcluster-master` 항목 확인 (id_ed25519_newcluster 가리키는지). 없으면 추가:
   ```
   Host newcluster-master
     HostName 10.100.230.130
     User root
     IdentityFile ~/.ssh/id_ed25519_newcluster
     IdentitiesOnly yes
     StrictHostKeyChecking accept-new
   ```
6. 연결 테스트: `ssh newcluster-master 'kubectl get nodes'`
7. **Phase 3 마무리 이어가기** — 위 "Phase 3 in_progress > 남은 작업" 의 1, 2번부터 진행

---

## 주요 파일 경로 (저장소 내)
```
docs/
  HANDOFF_TO_NEW_WORKSTATION.md      ← 이 문서
  16_npu_and_gpu_distribution_design.md
  17_self_monitoring_design.md
k8s/
  cronjobs/
    self-pred-push.yaml              (ESXi 호출 라인 주석 처리됨, 1분 주기 5 노드 push)
    esxi-edac.yaml                   (suspend: true 추가됨)
    ce-simulator.yaml                (suspend cluster patch 만, yaml 미반영)
    retrain-xgboost.yaml             (XGBoost 일일 재학습, 정상 가동)
  docker/Dockerfile.npu
  grafana/dashboards/
    ai-prediction-scores.json        (node-only 재구성됨)
    node-exporter-full.json
    dcgm-exporter.json
    npu-furiosa.json
  infra/
    npu-embed.yaml                   (ARTIFACT_DIR=/artifacts/qwen3-embed-4b)
    pvc-npu-artifacts.yaml           (Longhorn 10Gi, 클러스터에선 20Gi 로 확장됨)
  jobs/
    build-npu-artifact.yaml
  monitoring/
    alerts/
      failure-pred-rules.yaml        (PrometheusRule, hardware + pipeline 9 rules)
      alertmanager-slack.yaml        (AlertmanagerConfig, slack-warning/slack-critical)
      vmalert.yaml                   (vmalert Deployment, score 3 rules)
    dcgm-exporter-servicemonitor.yaml
    furiosa-metrics-servicemonitor.yaml
    grafana-vm-datasource.yaml
    smartctl-exporter-values.yaml
  rayserve/
    ensemble_app.py                  (5 predictor, /predict/{esxi,node}/* endpoints)
    raycluster.yaml                  (head + cpu-workers 3 replica, GPU 1 on head)
scripts/
  build_npu_artifact.py
  npu_serve_entrypoint.sh
  retrain_xgboost.py
```
