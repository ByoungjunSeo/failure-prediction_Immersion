# Phase 2 — Prometheus PVC + Retention 2년 설정

2026-06-22 수행. PVC 없던 Prometheus 에 Longhorn PVC 를 연결하고 retention 을 2년으로 확장하며,
6/22 백업(11일치)을 복원한 절차.

> ⚠ 사전 조건: 반드시 [백업](backup-procedure.md)을 먼저 완료하고 무결성 검증까지 끝낸 뒤 진행한다.
> patch 순간 기존 emptyDir 데이터는 사라지고 백업에서 복원하게 된다.

## 1. 사전 점검

```bash
PROM_OBJ=$(kubectl -n monitoring get prometheus -o jsonpath='{.items[0].metadata.name}')
kubectl -n monitoring get prometheus $PROM_OBJ -o jsonpath='retention={.spec.retention} storage={.spec.storage}{"\n"}'
kubectl get sc | grep longhorn                 # 기본 StorageClass 확인
kubectl get nodes.longhorn.io -n longhorn-system   # 노드 스케줄 가능/용량 확인
```

## 2. Prometheus 객체 patch

prometheus-operator 가 patch 를 감지해 StatefulSet/Pod 를 재생성하고 PVC 를 프로비저닝한다.

```bash
kubectl -n monitoring patch prometheus $PROM_OBJ --type=merge -p '{
  "spec": {
    "retention": "730d",
    "retentionSize": "400GB",
    "storage": {
      "volumeClaimTemplate": {
        "spec": {
          "storageClassName": "longhorn",
          "accessModes": ["ReadWriteOnce"],
          "resources": {"requests": {"storage": "500Gi"}}
        }
      }
    }
  }
}'
```

- `retentionSize: "400GB"` 는 Prometheus 가 `400GiB` 로 정규화한다(의도된 ~80% 상한).
- PVC `...-db-...-0` 이 Bound 되고 새 Pod 가 Ready 될 때까지 대기:

```bash
kubectl -n monitoring wait --for=condition=ready pod -l app.kubernetes.io/name=prometheus --timeout=300s
kubectl -n monitoring get pvc | grep prometheus
```

## 3. 백업 데이터 복원

빈 PVC 로 Ready 된 새 Pod 에 백업을 복원한다.

```bash
NEW_PROM=prometheus-monitoring-kube-prometheus-prometheus-0
# 백업을 PVC(/prometheus, 여유 충분)에 직접 복사 — 컨테이너 /tmp 사용 금지(아래 트러블슈팅 참고)
kubectl -n monitoring cp /home/backup/cluster-snapshot-YYYYMMDD-HHMM/prometheus_YYYYMMDD.tar.gz \
  monitoring/$NEW_PROM:/prometheus/restore.tar.gz -c prometheus

# 추출: 빈 TSDB 파일을 백업으로 덮어쓰고 tar 제거
kubectl -n monitoring exec $NEW_PROM -c prometheus -- sh -c '
  cd /prometheus
  tar xzf restore.tar.gz
  rm -f restore.tar.gz
'

# WAL stray 세그먼트 정리 (아래 트러블슈팅 참고) 후 재시작
kubectl -n monitoring delete pod $NEW_PROM
kubectl -n monitoring wait --for=condition=ready pod -l app.kubernetes.io/name=prometheus --timeout=300s
```

## 4. 검증

```bash
FINAL=prometheus-monitoring-kube-prometheus-prometheus-0
kubectl -n monitoring exec $FINAL -c prometheus -- wget -qO- http://localhost:9090/-/healthy
# retention 플래그
kubectl -n monitoring exec $FINAL -c prometheus -- wget -qO- http://localhost:9090/api/v1/status/flags \
  | grep -oE '"storage.tsdb.retention.(time|size)":"[^"]+"'
# headSeries (복원 전과 일치하는지)
kubectl -n monitoring exec $FINAL -c prometheus -- wget -qO- http://localhost:9090/api/v1/status/tsdb \
  | python3 -c "import json,sys;print('headSeries=',json.load(sys.stdin)['data']['headStats']['numSeries'])"
# 과거 시점 데이터 존재 확인
kubectl -n monitoring exec $FINAL -c prometheus -- wget -qO- \
  "http://localhost:9090/api/v1/query?query=up&time=$(date -u -d '10 days ago' +%s)"
```

기대값: healthy, `retention.time=2y`, `retention.size=400GiB`, headSeries ≈ 124,668, 과거 시점 시계열 존재.
복원 시작 시점은 백업이 담고 있던 가장 오래된 데이터(구 10일 retention 기준)까지다.

## 트러블슈팅 (실제 겪은 것)

1. **`kubectl exec -i ... 'cat>'` stdin 스트리밍 실패**
   3.8GB 전송 중 apiserver 연결 리셋(`connection reset by peer`)으로 ~320KB 에서 끊김.
   → 대용량은 `kubectl cp` 사용(내부적으로 tar 청크 스트리밍, 안정적). 복사 후 원본/대상 바이트 크기 일치 확인.

2. **컨테이너 `/tmp` 가 read-only**
   백업을 컨테이너 `/tmp` 에 둘 수 없다. → 여유 큰 **PVC(`/prometheus`)** 를 스테이징으로 사용.

3. **WAL stray 세그먼트**
   빈 Pod 가 잠깐 떠 있는 동안 만든 `wal/00000000` 이 백업의 `checkpoint.00000NNN` 보다 낮은 번호로 남으면
   재시작 시 replay 가 꼬일 수 있다. → 재시작 전 stray 세그먼트 삭제하여 백업 WAL 세트만 남긴다.

   ```bash
   kubectl -n monitoring exec $NEW_PROM -c prometheus -- ls -la /prometheus/wal/
   kubectl -n monitoring exec $NEW_PROM -c prometheus -- rm -f /prometheus/wal/00000000
   ```

4. **복원 실패 fallback**
   CrashLoop 등으로 복원이 실패하면 빈 PVC 로 깨끗이 시작해도 운영엔 문제없다(백업은 `/home/backup` 에 보존).
   `rm -rf /prometheus/*` 후 Pod 재시작.
