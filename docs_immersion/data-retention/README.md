# 데이터 보존 시스템

2026-06-22 구축. 액침서버 클러스터 모니터링 데이터의 장기 보존.

## 핵심 사양

| 시스템 | 용도 | Retention | PVC | StorageClass |
|--------|-----|-----------|-----|--------------|
| Prometheus | 인프라 메트릭 (CPU/GPU/NPU/메모리/디스크/네트워크) | 2년 (730d) | 500Gi | Longhorn |
| VictoriaMetrics | AI 예측 점수 + PUE 메트릭 | 24개월 | 500Gi | Longhorn |

이중 안전망: `retention.time` + `retentionSize` 400GB 상한 → 디스크 100% 사고 방지.

## 데이터 누적 일정

- 2026-06-22: 시작 (11일치 백업 복원 + 신규 누적)
- 2026-12-22: 6개월치
- 2027-06-22: 1년치
- 2028-06-22: 만 2년 도달 (이후 최신 2년 유지)

## 디렉토리

- [architecture.md](architecture.md): 시스템 구조 + retention 정책
- [backup-procedure.md](backup-procedure.md): 백업 절차
- [phase2-setup.md](phase2-setup.md): Prometheus PVC + retention 설정 가이드

## 관련

- PUE 데이터 추출 도구: [../../services/pue-data-exporter/README.md](../../services/pue-data-exporter/README.md)
