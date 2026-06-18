# PUE 부하 제어 스크립트

한 줄로 GPU/NPU 부하 단계를 변경하는 운영 자동화 스크립트.

## 빠른 시작

```bash
./scripts/pue/start_30.sh   # 30% 부하
./scripts/pue/start_50.sh   # 50% 부하
./scripts/pue/start_90.sh   # 90% 부하
./scripts/pue/start_99.sh   # 99% 부하 (주의: 추론 성능 저하 가능)
./scripts/pue/stop_all.sh   # 전체 정지
./scripts/pue/reset_all.sh  # 재부팅/사고 후 전체 복구
./scripts/pue/_status.sh    # 현재 상태 확인
```

## 사전 요구사항

- `ssh newcluster-master` 설정 완료 (비밀번호 없이 접속)
- `kubectl` 접근 가능 (master를 통해)
- IPMI 자격증명 (`reset_all.sh`에서 cold cycle 시):
  ```bash
  export IPMI_HOST=10.100.231.130
  export IPMI_USER=admin
  export IPMI_PASS=<password>
  ```

## 부하 단계별 실측 (검증 완료)

| 스크립트 | GPU 의도 | GPU 실측 | NPU 실측 | 추론 응답 | 비고 |
|---------|---------|---------|---------|----------|------|
| start_30 | 30% | ~30% (7분 수렴) | 73~84W (~30%) | 3.1초 | 저부하 |
| start_50 | 50% | ~50% | 95~102W (~50%) | 3.3초 | 중부하 |
| start_90 | 90% | ~88% (85% 클램프) | 155~161W (풀가동) | 3.1초 (간헐 6초) | 고부하 |
| start_99 | 99% | ~88% (85% 클램프) | 154~156W (풀가동) | 3.3초 (간헐 6초) | 최대 |
| stop_all | OFF | 0% | 45W (idle) | 3.3초 | 정지 |

**GPU 85% 클램프 (의도된 안전장치)**: PI 컨트롤러 내부 안전 제한으로 target이
85%로 클램프됨. GPU 사망 사고 2회 후 추가된 보호 장치. start_90과 start_99는
GPU 측면에서 사실상 동일 부하 (~88%). 이 제한은 의도적이며 유지함.
99% 부하가 정말 필요하면 안전 검토 후 수동 ConfigMap 조정 필요:
```bash
kubectl -n failure-prediction patch cm pue-gpu-load-config \
  --type merge -p '{"data":{"MAX_GPU_UTIL":"99"}}'
```

**NPU 90/99 동일**: NPU는 점유(occupancy) 모델 특성상 90%와 99%의 실측 전력 차이가
거의 없음 (155W vs 156W). 실질적 NPU 구분은 30%(84W) / 50%(102W) / 풀가동(155W+) 3단계.

NPU TDP: ~160W. GPU TDP: 180W (RTX 5060 Ti).

## 안전장치

- **GPU 폭주 방지**: batch 상한 72, VRAM 가드 85%, 온도 83°C
- **추론 watchdog**: 응답 5초 초과 3회 연속 시 PUE GPU 부하 자동 정지 + Slack 알림
- **node1 GPU 영구 제외**: PCIe 사망 이력 2회 (Xid 79). GPU 부하는 node2-4만 대상
- **사전 체크**: 스크립트 시작 시 클러스터 Ready 및 추론 정상 여부 확인

## 스크립트 구조

```
scripts/pue/
├── _common.sh      # 공통 함수 (로깅, ssh, 클러스터 체크, NPU 매핑)
├── _start.sh       # 부하 시작 내부 헬퍼
├── _status.sh      # 상태 확인 (GPU/NPU util, 전력, 온도, 추론 응답)
├── start_30.sh     # 30% 부하
├── start_50.sh     # 50% 부하
├── start_90.sh     # 90% 부하
├── start_99.sh     # 99% 부하
├── stop_all.sh     # 전체 정지
├── reset_all.sh    # 전체 복구 (인터랙티브)
└── README.md
```

## 환경 변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `PUE_MASTER_HOST` | `newcluster-master` | SSH 접속할 마스터 호스트 |
| `IPMI_HOST` | - | node1 BMC IP (reset_all.sh) |
| `IPMI_USER` | - | IPMI 사용자명 |
| `IPMI_PASS` | - | IPMI 비밀번호 |

## 트러블슈팅

- **부하가 목표에 안 닿음**: GPU는 목표 ±10% 정상. NPU는 요청 빈도 기반이므로 전력 기준으로 판단
- **추론 응답 5초+**: watchdog이 자동 정지함. 원인 파악 후 `start_XX.sh` 재실행
- **node1 GPU 메트릭 없음**: `reset_all.sh` 실행 후 cold cycle 진행
- **Registry 비정상**: `reset_all.sh` Step 4에서 자동 재생성 옵션 제공
