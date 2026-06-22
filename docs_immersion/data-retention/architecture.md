# 아키텍처 — 데이터 보존

## 역할 분리

두 개의 시계열 DB가 용도별로 분리되어 있다.

| | Prometheus | VictoriaMetrics |
|---|---|---|
| 네임스페이스 | `monitoring` | `failure-prediction` |
| 수집 대상 | 인프라 메트릭 (node-exporter, DCGM, furiosa NPU, kube-state) | AI 예측 점수(`failure_pred_*`) + PUE 메트릭(`pue_*`) |
| 시계열 규모 | 약 13만 (headSeries ≈ 124,668) | 17종 메트릭 |
| Retention | 2년 (730d) | 24개월 |
| PVC | 500Gi Longhorn | 500Gi Longhorn |
| 접근 | ClusterIP `monitoring-kube-prometheus-prometheus.monitoring:9090` | NodePort `victoria-metrics-svc:8428` (30171) |

## 데이터 흐름

```
node-exporter / DCGM / furiosa-exporter ──► Prometheus ──┐
                                                          ├─► Grafana (대시보드)
PUE 컨트롤러 / AI 예측 파이프라인 ──► VictoriaMetrics ─────┘
                                                          └─► PUE Data Exporter (CSV 추출)
```

## PVC 영구 보존 메커니즘

- 두 DB 모두 Longhorn 분산 블록 스토리지 위의 PVC 를 사용한다.
- Pod 가 재시작/재스케줄되어도 PVC 는 유지되므로 데이터가 보존된다.
- Prometheus 는 StatefulSet 의 `volumeClaimTemplate` 으로 PVC 가 고정 연결된다.

## Retention 이중 안전망

디스크가 100% 차서 DB 가 죽는 사고를 막기 위해 두 조건을 함께 건다.

1. **시간 기준** `--storage.tsdb.retention.time=730d` — 2년 지난 블록 삭제
2. **용량 기준** `--storage.tsdb.retention.size=400GB` — 500Gi PVC 의 약 80% 도달 시 오래된 블록부터 삭제

→ 둘 중 먼저 도달하는 조건이 적용된다. 데이터가 예상보다 빨리 늘어도 용량 상한이 PVC 포화를 선제 차단한다.

## Longhorn 디스크 용량 계획

- Longhorn 노드 5대, 노드당 약 2.9TB 가용.
- Prometheus 500Gi + VictoriaMetrics 500Gi = 논리 1TB. Longhorn 복제본까지 고려해도 노드당 가용량 대비 충분한 여유.
- `storage-over-provisioning-percentage=100`, `storage-minimal-available-percentage=25` 기본값 유지.

## 이전 문제 — Prometheus 시한폭탄 해소

- **이전 상태**: Prometheus 에 PVC 가 없었다. 데이터가 스케줄된 노드의 루트 파일시스템(`/dev/mapper/rl-root`, emptyDir)에 있어 **Pod 재시작 시 전손** 위험이 있었다. retention 도 10일에 불과했다.
- **조치(Phase 2)**: 500Gi Longhorn PVC 연결 + retention 2년 + 용량 상한 400GB. 6/22 시점 백업에서 11일치 데이터를 복원했다.
- 자세한 절차는 [phase2-setup.md](phase2-setup.md) 참고.
