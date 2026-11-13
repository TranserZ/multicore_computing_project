# Problem 1: gem5 → McPAT → HotSpot 파이프라인

## 폴더 구조

```
multicore_computing_project/
├── setup.sh                             공통 환경 설치 스크립트
├── PROBLEM1.md                          이 문서
├── PROBLEM2.md
├── problem1/
│   ├── glue/
│   │   ├── glue_code1.py                1st glue code: gem5 결과(stats.txt, config.json)를 McPAT 입력 XML로 변환
│   │   └── glue_code2.py                2nd glue code: McPAT 전력 결과를 HotSpot 입력(.ptrace, .flp)으로 변환
│   ├── configs/
│   │   ├── gem5_config.py               gem5 실행 설정 (코어 수, width 등)
│   │   ├── mcpat_template.xml           McPAT 기본 설정 (공정, 전압 등 gem5가 알려주지 않는 값)
│   │   └── hotspot.config               HotSpot 열 환경 설정 (방열판, 주변 온도 등)
│   ├── scripts/
│   │   ├── run_whole_pipeline.py        전체 실행: gem5(Docker) → glue 1 → McPAT → glue 2 → HotSpot
│   │   └── regression_test.sh           회귀 테스트: testcase별 결과 검사 (코드를 바꿀 때마다 실행)
│   └── changes/
│       └── mcpat_no_return_fixed.patch  McPAT 무한 루프 버그 수정
├── problem2/
├── tools/                               도구 설치 위치 (업로드 안 함)
└── out/                                 시뮬레이션 출력 (업로드 안 함)
```

## 실행 방법

한 가지 구성에 대해 gem5 → McPAT → HotSpot을 한 번에 실행합니다.

```bash
# 전체 실행 (gem5 포함, 수십 분 소요)
python3 problem1/scripts/run_whole_pipeline.py --benchmark blackscholes --cores 4 --width 4

# gem5는 건너뛰고 기존 gem5 결과로 McPAT, HotSpot만 다시 실행 (몇 초)
python3 problem1/scripts/run_whole_pipeline.py --benchmark blackscholes --cores 4 --width 4 --skip-gem5
```

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--benchmark` | (필수) | blackscholes, canneal, freqmine, streamcluster |
| `--cores` | 1 | 코어 수 (= PARSEC 스레드 수) |
| `--width` | 4 | fetch, decode, rename, dispatch, issue, commit width를 함께 설정 (writeback width는 gem5 기본값 8로 고정) |
| `--rob` `--iq` `--lq` `--sq` | 128, 64, 32, 32 | ROB, IQ, load queue, store queue 크기 |
| `--l2` | 2MiB | 공유 L2 크기 |
| `--dcache-ports` | 2 | McPAT에 넣을 데이터 캐시 포트 수 (전력·면적 계산에만 영향) |
| `--warmup-runs` | 1 | 측정 전에 KVM으로 같은 프로그램을 미리 실행하는 횟수 |
| `--skip-gem5` | | 기존 gem5 결과 재사용 |

### 처리 흐름

```
gem5 (KVM으로 부팅 → KVM으로 warm-up 실행 1회 → 다음 실행의 ROI에서 O3 CPU로 전환, ROI만 측정)
  │  stats.txt, config.json
  ▼
glue_code1.py   config.json의 구조 값과 stats.txt의 활동량을 mcpat_template.xml에 채움
  │  mcpat_in.xml
  ▼
McPAT           블록별 전력(dynamic, leakage)과 면적 계산
  │  mcpat_out.txt
  ▼
glue_code2.py   블록별 전력을 뽑아 .ptrace를 만들고, 면적으로 배치도 .flp를 생성
  │  chip.flp, chip.ptrace
  ▼
HotSpot         steady-state 온도 계산
  │  chip.steady
  ▼
result.json     실행 시간, IPC, 전력, 면적, 최고·평균 온도
```

### 출력 (`out/<benchmark>_c<코어 수>_w<width>/`)

| 위치 | 내용 |
|---|---|
| `gem5/` | `stats.txt`, `config.json`, `gem5.log` |
| `mcpat/` | `mcpat_in.xml`, `mcpat_out.txt` |
| `hotspot/` | `chip.flp`, `chip.ptrace`, `chip.steady`, `power.json` |
| `result.json` | 이 구성의 성능, 전력, 온도 요약 |

### HotSpot 블록 구성

- 코어마다 3개: `core{i}_FE`(fetch + rename), `core{i}_EXE`(실행 유닛), `core{i}_MEM`(load/store 유닛 + L1D + TLB)
- 코어 바깥: `L2`(공유 L2 + 버스), `MC`(메모리 컨트롤러)
- 배치: 코어를 격자(1코어 1×1, 4코어 2×2, 8코어 4×2)로 놓고 그 아래 한 줄에 L2와 MC를 둠

### 모델링에서 정한 것

| 항목 | 값 | 이유 |
|---|---|---|
| 공정 | 22nm, HP 소자 | McPAT이 지원하는 가장 최신 공정 |
| 데이터 캐시 포트 | `--dcache-ports`로 지정 (기본 2) | gem5의 메모리 유닛 수(한 사이클에 실행 가능한 load/store 수)와 McPAT의 캐시 포트 수(물리적 통로 수)는 뜻이 달라 자동으로 정하지 않음. 32KB 캐시 면적은 2개일 때 3.3mm², 4개일 때 11.8mm² |
| IQ 크기 | gem5 IQ를 정수용, FP용으로 절반씩 | gem5는 IQ 하나를 같이 쓰고 McPAT은 둘로 나눠 모델링함 |
| FPU 개수 | FP 덧셈 유닛 + FP 곱셈 유닛 | gem5의 SIMD 유닛은 x86 FP 연산에 쓰이지 않아 제외 |
| writeback width | 모든 구성에서 gem5 기본값 8 | `--width`와 함께 2로 줄이면 gem5 O3가 assertion으로 중단됨. McPAT 입력에는 들어가지 않는 값 |
| 열 환경 | HotSpot 예제 기본 패키지, 주변 온도 45°C | 모든 실험에서 고정 |

### warm-up 실행이 필요한 이유

warm-up 없이 바로 측정하면 ROI 안에 모든 코어가 쉬는 구간이 몇 ms씩 생깁니다. ROI에서 처음 쓰는 페이지를 읽어오느라 main 스레드가 장치 I/O를 기다리고, 그동안 스레드 생성이 늦어지기 때문입니다. simsmall의 ROI는 수십 ms로 짧아서 이 대기가 결과를 크게 왜곡합니다.

| blackscholes, 4-wide | warm-up 없음 | warm-up 1회 |
|---|---|---|
| 1코어 ROI | 30.08ms | 26.16ms |
| 4코어 ROI | 26.33ms, 20.17ms (실행마다 다름) | 8.32ms |
| 4코어 speedup | 1.14 | 3.14 |

## 회귀 테스트

코드를 바꾼 뒤 파이프라인 결과가 여전히 올바른지 확인합니다.

```bash
# 빠른 모드 (약 20초): 기존 gem5 결과로 glue 1 → McPAT → glue 2 → HotSpot만 다시 실행해 검사
problem1/scripts/regression_test.sh

# gem5부터 다시 실행 (gem5_config.py를 바꿨을 때. 벤치마크에 따라 수 분~2시간)
problem1/scripts/regression_test.sh --full blackscholes:1:4 blackscholes:4:4

# 지금 결과를 기준값으로 저장 (의도한 변경으로 결과가 달라졌을 때)
problem1/scripts/regression_test.sh --update-baseline
```

- testcase는 `<benchmark>:<코어 수>:<width>` 형식이며, 생략하면 기본 8개(벤치마크 4개, 코어 수 1/4/8, width 2/4/8을 한 번 이상 포함)를 검사합니다.
- 검사 내용: 요청한 코어 수와 width가 반영됐는지, ROI만 측정했는지, 단계 사이의 값(전력 합계, 블록 이름, 면적)이 일치하는지, 배치도에 겹침이나 빈틈이 없는지, 온도 범위, 기준값과의 차이, 코어 수·width에 따른 경향.
- 기준값은 `out/<이름>/baseline.json`에 저장되며 각자의 gem5 결과에 묶여 있어 GitHub에는 올리지 않습니다.
- FAIL이 하나라도 있으면 종료 코드 1을 돌려줍니다. 자세한 옵션은 `regression_test.sh --help`.

## GitHub에 올리는 것 ✅

| 분류 | 파일 | 이유 |
|---|---|---|
| glue code | `problem1/glue/*` | 과제의 핵심이자 제출물 |
| 도구 설정 | `problem1/configs/*` | 모두가 같은 조건으로 실험해야 함 |
| 실행 스크립트 | `problem1/scripts/*` | 실행 방법을 하나로 통일함 |
| 도구 수정 사항 | `problem1/changes/*` | 모두가 같은 수정본을 써야 함 |
| 설치 스크립트 | `setup.sh` | 각자 같은 버전의 도구를 설치할 수 있음 |
| 결과 요약 | 결과표(CSV), 그래프 (예정) | 용량이 작고 보고서 작성에 필요 |
| 문서 | `README.md`, `PROBLEM1.md`, `PROBLEM2.md` | |

## GitHub에 올리면 안 되는 것 ❌

| 분류 | 위치 | 이유 |
|---|---|---|
| 도구 본체 | `tools/gem5`, `tools/mcpat`, `tools/hotspot` | 용량이 큼 (gem5 실행 파일만 약 1GB). 각자 설치 |
| PARSEC 디스크 이미지, 커널 | `tools/resources/` | 26GB라 GitHub 용량 제한(파일당 100MB)을 넘음 |
| 시뮬레이션 출력 | `out/` | 실행할 때마다 수십 MB씩 생김. 필요한 결과만 요약해서 올림 |
| Python 캐시 | `__pycache__/`, `*.pyc` | 자동 생성 파일 |

위 항목은 `.gitignore`에 등록되어 있어서 `git add .`를 해도 올라가지 않습니다.

## 각자 준비해야 하는 환경

### 필요 조건
- Linux (KVM 필요. Mac/Windows 불가)
- Docker (사용자가 `docker` 그룹에 포함되어 있어야 함)
- `/dev/kvm` 접근 권한
- 디스크 여유 공간 약 60GB

### 설치 방법
```bash
git clone <repo 주소>
cd multicore_computing_project
./setup.sh
```
- `tools/` 아래에 gem5, McPAT, HotSpot, PARSEC 리소스가 아래 버전으로 설치됩니다.
- gem5 빌드에 30분~1시간 이상, PARSEC 디스크 이미지 다운로드(약 8GB)에 시간이 걸립니다.
- 이미 설치된 항목은 건너뛰므로 여러 번 실행해도 됩니다.
- 메모리가 부족해 빌드가 실패하면 병렬도를 낮춥니다: `JOBS=2 ./setup.sh`

### 사용하는 도구 버전

| 도구 | 버전 |
|---|---|
| gem5 | v25.1.0.1 (X86 빌드) |
| gem5 빌드·실행 환경 | Docker `ghcr.io/gem5/ubuntu-24.04_all-dependencies:v25-1` |
| McPAT | v1.3 (GitHub `HewlettPackard/mcpat`, commit `74d4759`) + `mcpat_no_return_fixed.patch` |
| HotSpot | GitHub `uvahotspot/HotSpot`, commit `f18831e` |
| PARSEC | gem5-resources `x86-parsec` v1.0.0, 커널 `x86-linux-kernel-4.19.83` |
