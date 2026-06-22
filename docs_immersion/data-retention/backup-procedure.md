# 백업 절차

2026-06-22 실제 수행한 클러스터 스냅샷 백업 매뉴얼.

## 대상 및 백업 위치

| 대상 | 방식 | 결과 크기 |
|------|------|-----------|
| VictoriaMetrics | Pod 내부 `/storage` tar → `kubectl cp` | ~17M |
| Prometheus | 라이브 TSDB tar 스트리밍 | ~3.8G |
| Grafana | ConfigMap(대시보드/데이터소스) + PVC 데이터 + Deployment/Service | ~21M |

백업 위치: `node1:/home/backup/cluster-snapshot-YYYYMMDD-HHMM/`

## 사전 준비

```bash
BACKUP_DIR="/home/backup/cluster-snapshot-$(date +%Y%m%d-%H%M)"
mkdir -p "$BACKUP_DIR"
df -h /home   # 여유 공간 확인
# (선택) 데이터 변경 최소화를 위해 부하 생성기 일시 정지
kubectl -n failure-prediction scale deployment pue-gpu-load npu-load-generator --replicas=0
```

## VictoriaMetrics (PVC 있음 — 안전)

```bash
VM=$(kubectl -n failure-prediction get pod -l app=victoria-metrics --no-headers | awk '{print $1}' | head -1)
kubectl -n failure-prediction exec $VM -- tar czf /tmp/vm-backup.tar.gz -C /storage .
kubectl -n failure-prediction cp $VM:/tmp/vm-backup.tar.gz "$BACKUP_DIR/victoria_metrics_$(date +%Y%m%d).tar.gz"
kubectl -n failure-prediction exec $VM -- rm -f /tmp/vm-backup.tar.gz
```

## Prometheus (PVC 없던 시점 — 라이브 tar)

snapshot API(`/api/v1/admin/tsdb/snapshot`)는 `--web.enable-admin-api` 가 꺼져 있으면 사용할 수 없다(500/404).
이 경우 라이브 TSDB 디렉토리를 직접 tar 로 떠서 **node1 로 직접 스트리밍**한다(컨테이너/노드 루트에 임시파일을 만들지 않아 디스크 압박 회피).

```bash
PROM=prometheus-monitoring-kube-prometheus-prometheus-0
kubectl -n monitoring exec $PROM -c prometheus -- tar czf - -C /prometheus . \
  > "$BACKUP_DIR/prometheus_$(date +%Y%m%d).tar.gz" 2>/tmp/prom_tar_stderr.log
```

- 라이브 tar 는 복원 시 WAL 재생으로 정상 복구된다(표준 cold-backup).
- tar stderr 에 "file changed as we read it" 경고가 없으면 깨끗한 백업.

## Grafana

```bash
mkdir -p "$BACKUP_DIR/grafana"
for ns in monitoring failure-prediction; do
  kubectl -n $ns get configmap -l grafana_dashboard=1  -o yaml > "$BACKUP_DIR/grafana/dashboards-${ns}.yaml"
  kubectl -n $ns get configmap -l grafana_datasource=1 -o yaml > "$BACKUP_DIR/grafana/datasources-${ns}.yaml"
done
kubectl -n monitoring get deployment monitoring-grafana -o yaml > "$BACKUP_DIR/grafana/grafana-deployment.yaml"
kubectl -n monitoring get svc        monitoring-grafana -o yaml > "$BACKUP_DIR/grafana/grafana-svc.yaml"
# PVC 데이터 (활성 Pod 명시 타겟, 스트리밍)
GRAF=$(kubectl -n monitoring get pod -l app.kubernetes.io/name=grafana --field-selector=status.phase=Running -o name | head -1)
kubectl -n monitoring exec ${GRAF#pod/} -c grafana -- tar czf - -C /var/lib/grafana . > "$BACKUP_DIR/grafana/grafana-data.tar.gz"
( cd "$BACKUP_DIR" && tar czf "grafana_$(date +%Y%m%d).tar.gz" grafana/ && rm -rf grafana/ )
```

## 무결성 검증

```bash
cd "$BACKUP_DIR"
for f in *.tar.gz; do
  echo "$f: $(tar tzf "$f" >/dev/null 2>&1 && echo OK || echo FAIL) / $(tar tzf "$f" 2>/dev/null | wc -l) entries"
done
sha256sum *.tar.gz > checksums.sha256
```

Prometheus 백업은 환원 불가 데이터이므로 내용까지 확인 권장: ULID 블록 디렉토리 + `wal/` + `chunks_head/` 존재 여부.

## MANIFEST.txt 양식

```
백업 일자: <UTC ISO8601>
클러스터: failure-prediction
백업 위치: node1:<BACKUP_DIR>

[포함]
- victoria_metrics_*.tar.gz : VM 데이터, Retention 24개월
- prometheus_*.tar.gz       : Prometheus TSDB, Retention 2년
- grafana_*.tar.gz          : 대시보드 + 데이터소스 + PVC

[복원 방법]
- VM:        tar xzf victoria_metrics_*.tar.gz -C /storage 후 Pod 재시작
- Prometheus: TSDB 디렉토리에 풀고 Pod 재시작 (WAL 재생)
- Grafana:   ConfigMap apply + grafana-data 를 /var/lib/grafana 에 풀기
```

> 복원(Prometheus PVC 마이그레이션 포함) 절차는 [phase2-setup.md](phase2-setup.md) 참고.
