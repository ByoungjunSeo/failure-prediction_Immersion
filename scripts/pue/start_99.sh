#!/bin/bash
## PUE 부하 99% — 최대 부하 측정용 (주의: 추론 성능 저하 가능)
exec "$(dirname "$0")/_start.sh" 99
