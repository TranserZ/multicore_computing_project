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
│   │   ├── run_repeats.py               반복 실행: 구성마다 실행 횟수가 목표에 이를 때까지 돌림
│   │   ├── regression_test.sh           회귀 테스트: testcase별 결과 검사 (코드를 바꿀 때마다 실행)
│   │   ├── results_log.py               실행 기록 관리: runs.csv에 추가, 구성별 평균·표준편차 요약
│   │   ├── extract_details.py           실행별 상세 통계: out/ 원본에서 분석에 쓰는 값만 뽑아 run_details.csv로 저장
│   │   └── plot_results.py              보고서용 그래프와 요약 표 생성
│   ├── results/
│   │   ├── runs.csv                     실행 기록 (gem5 실행 한 번이 한 줄, 자동으로 쌓임)
│   │   ├── run_details.csv              실행별 상세 통계 (실행 한 번이 한 줄, 67개 값)
│   │   ├── out_raw_20261127.tar.gz      out/ 원본의 압축본 (실행 138개, 17 MB)
│   │   └── figures/                     그래프(fig1~fig6)와 그래프에 쓴 값의 표
│   └── changes/
│       └── mcpat_no_return_fixed.patch  McPAT 무한 루프 버그 수정
├── problem2/
├── tools/                               도구 설치 위치 (업로드 안 함)
├── out/                                 시뮬레이션 출력 (업로드 안 함)
└── .venv/                               그래프용 Python 가상환경 (업로드 안 함)
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
| `--no-record` | | `problem1/results/runs.csv`에 기록하지 않음 (디버그용 실행) |

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

## 실행 기록 (`problem1/results/runs.csv`)

같은 구성도 실행마다 ROI 시간이 최대 약 10% 달라지므로, 여러 번 실행한 값을 모아 평균을 냅니다.

- `run_whole_pipeline.py`가 끝날 때마다 한 줄이 자동으로 추가됩니다. `out/<이름>/`은 다시 실행하면 덮어써지지만 기록은 남습니다.
- gem5 실행 한 번이 한 줄입니다(`run_id` = `stats.txt`의 md5). 같은 gem5 결과를 `--skip-gem5`로 다시 처리하면 줄이 늘지 않고 전력·온도 값만 갱신됩니다.
- 한 줄의 내용: 구성(벤치마크, 코어 수, width, ROB 등), 결과(ROI 시간, IPC, 전력, 면적, 온도), 실행 시각, 돌린 사람(`runner`, git 사용자 이름), 돌린 컴퓨터의 CPU, 코드의 commit.
- 여러 사람이 같은 구성을 돌려도 줄이 겹치지 않습니다. 줄을 구분하는 것은 `run_id`이고, `out_name`(각자 컴퓨터의 출력 폴더 이름)은 참고용입니다.
- 이 파일은 GitHub에 올립니다. 각자 실행한 줄이 merge할 때 모두 남도록 `.gitattributes`에 설정되어 있습니다.

```bash
# 구성별 실행 횟수, 평균 ± 표준편차
python3 problem1/scripts/results_log.py summary

# 요약 표를 CSV로 저장
python3 problem1/scripts/results_log.py summary --out summary.csv
```

### 반복 실행

같은 구성을 여러 번 돌릴 때는 `run_repeats.py`를 씁니다. `runs.csv`에 이미 있는 횟수를 세어 모자란 만큼만 돌리므로, 중간에 끊겨도 같은 명령으로 이어서 진행됩니다.

```bash
# 구성마다 총 3회가 되도록, 4개씩 동시에 실행
python3 problem1/scripts/run_repeats.py -n 3 -j 4 canneal:1:4 canneal:4:4 canneal:8:4

# ROB / IQ / LQ / SQ를 바꾼 구성 (뒤에 네 값을 붙임)
python3 problem1/scripts/run_repeats.py -n 3 canneal:1:8:256:128:64:64

# 무엇을 돌릴지만 확인
python3 problem1/scripts/run_repeats.py -n 3 --dry-run canneal:1:4
```

- testcase는 `<benchmark>:<코어 수>:<width>` 형식입니다. 출력은 `out/<benchmark>_c<코어 수>_w<width>_r<번호>/`에 따로 남습니다.
- 동시 실행 개수(`-j`)는 호스트 부하를 바꿔 시작 상태에 영향을 주므로, 비교할 실험끼리는 같은 값(지금까지 4)으로 맞춥니다.

## 실행별 상세 통계 (`problem1/results/run_details.csv`)

`out/`은 실행 하나에 2~5 MB라 GitHub에 올리지 않습니다. 그 대부분은 gem5가 기록한 통계 수천 줄과 설정 파일이고, 분석에 실제로 쓰는 값은 실행당 수십 개입니다. 그 값만 뽑아 실행 하나를 한 줄로 저장한 것이 이 파일입니다.

```bash
# out/ 의 원본에서 뽑아 갱신 (실험을 더 돌린 뒤 실행)
python3 problem1/scripts/extract_details.py
```

| 묶음 | 열 | 출처 |
|---|---|---|
| 구성 | `run_id`, `out_name`, 벤치마크, 코어 수, width, ROB 등 | `runs.csv` |
| 실행한 양 | ROI 시간, 명령어 수(전체, 사용자 모드), µop 수, FP 연산 수, load, store, 분기, 함수 호출, IPC | gem5 `stats.txt` (모든 코어의 합) |
| 코어 동작 | 코어가 ROI 중 동작한 비율(평균, 최소, 최대), 잠든 횟수 | gem5 `stats.txt` |
| 파이프라인 | decode한 µop 수, 분기 예측 실패, rename이 막힌 비율, 아무것도 issue하지 못한 비율, ROB·IQ·LQ·SQ·레지스터가 가득 찬 횟수 | gem5 `stats.txt` |
| 메모리 계층 | L1I·L1D·L2 miss, L2 miss 평균 지연, TLB miss, 버스 패킷 수, 메모리 읽기·쓰기 | gem5 `stats.txt` |
| 전력 | 부품별(코어 로직, L1, L2, 버스, 메모리 컨트롤러) dynamic과 leakage, 코어 안 유닛별 dynamic, 합계, 면적 | McPAT `mcpat_out.txt` |
| 온도 | 최고 온도와 그 블록, 평균 온도, L2 온도, 가장 뜨거운 실행 블록의 전력 밀도 | HotSpot 결과 |

- `runs.csv`의 줄과 `run_id`로 이어집니다. 원본의 `stats.txt`가 그 `run_id`와 일치할 때만 뽑습니다.
- 원본이 이 컴퓨터에 없는 실행(다른 사람이 돌렸거나 지운 것)은 기존 줄을 그대로 둡니다. 각자 자기 실행을 뽑아 올리면 한 파일에 모입니다.
- L1 캐시는 McPAT이 코어 안에 넣어 계산하므로, 명령어 캐시와 데이터 캐시를 코어에서 꺼내 따로 적었습니다.
- 뽑아 두지 않은 값이 필요해지면 `extract_details.py`의 `COLUMNS`에 열을 추가하고 다시 실행합니다. 원본이 있는 실행만 새 값이 채워집니다.

### 원본 압축본 (`problem1/results/out_raw_20261127.tar.gz`)

`run_details.csv`에 없는 값이 필요하거나 glue code를 고친 뒤 전력·온도를 다시 계산해야 할 때를 위해, `out/`의 원본을 추려서 압축해 두었습니다. 2026-11-27까지의 실행 138개가 모두 들어 있습니다.

```bash
# 프로젝트 폴더에서 풀면 out/<이름>/ 으로 복원됨 (같은 이름의 파일은 덮어씀)
tar -xzf problem1/results/out_raw_20261127.tar.gz
```

| | 내용 |
|---|---|
| 들어 있는 것 | 실행마다 gem5 `stats.txt`, `config.json`, `gem5.log`, 게스트 실행 명령, McPAT 입출력, HotSpot 입출력, `result.json` |
| 뺀 것 | 파이프라인 실행 로그, `config.json`과 같은 내용의 다른 형식(`config.ini`, `config.dot` 등), 게스트 콘솔 출력, 주기 통계 실행(`out/debug/`), 회귀 테스트의 기준값 |

- 풀고 나면 `extract_details.py`, `run_whole_pipeline.py --skip-gem5`를 그대로 쓸 수 있습니다.
- 새로 돌린 실행은 이 파일에 들어가지 않습니다. 압축본은 덮어쓰지 말고 날짜를 바꿔 새로 만듭니다(git은 바뀐 압축 파일을 통째로 다시 저장합니다).

## 그래프와 요약 표 (`problem1/results/figures/`)

`plot_results.py`가 `runs.csv`와 `run_details.csv`를 읽어 보고서용 그래프를 만듭니다. `out/`이 없어도 그릴 수 있습니다.

```bash
# 처음 한 번: 그래프용 가상환경 만들기 (matplotlib)
python3 -m venv .venv && .venv/bin/python -m pip install matplotlib

# 그래프와 표 만들기
.venv/bin/python problem1/scripts/plot_results.py
```

| 파일 | 내용 |
|---|---|
| `fig1_cores.png` | 코어 수 실험(1/4/8코어, 4-wide): speedup, 평균 전력, 최고 온도 |
| `fig2_width.png` | width 실험(2/4/8-wide, 1코어): speedup, 평균 전력, 최고 온도 |
| `fig3_tradeoff.png` | 코어를 늘리는 방향과 폭을 넓히는 방향의 전력 대비 speedup (기준: 1코어 4-wide) |
| `fig4_window.png` | 8-wide에서 ROB/IQ/LQ/SQ를 고정한 경우와 두 배로 키운 경우 |
| `fig5_power_cores.png`, `fig6_power_width.png` | 전력이 어디로 가는지(W): 부품별, dynamic과 leakage |
| `summary_table.csv` | fig1~fig4에 쓴 값 (구성별 평균, 표준편차, speedup, 면적 등) |
| `power_breakdown.csv` | fig5, fig6에 쓴 값 (구성별 부품별 전력과 비율) |

- 모든 값은 같은 구성을 여러 번 돌린 평균이고, 오차 막대는 표준편차입니다.
- 기본 설정(warm-up 1회)의 실행만 씁니다. ROB/IQ/LQ/SQ를 바꾼 실행은 fig4에만 씁니다.
- speedup의 기준은 실험마다 가장 작은 구성입니다(코어 수 실험은 1코어, width 실험은 2-wide).
- 온도 그래프의 세로축은 주변 온도(45°C)에서 시작합니다.
- `python3 -m venv`가 실패하면(`ensurepip` 없음) `python3 -m venv --without-pip .venv`로 만든 뒤 `get-pip.py`로 pip를 설치합니다.

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
| 실행 기록 | `problem1/results/runs.csv` | 여러 번 실행한 값을 모아 평균을 내는 데 필요 |
| 실행별 상세 통계 | `problem1/results/run_details.csv` | `out/` 원본 대신 올리는 요약. 분석과 그래프에 쓰는 값 |
| 원본 압축본 | `problem1/results/out_raw_20261127.tar.gz` | 다시 만들 수 없는 gem5 통계의 보관용 (17 MB) |
| 그래프와 표 | `problem1/results/figures/*` | 용량이 작고 보고서 작성에 필요 |
| 문서 | `README.md`, `PROBLEM1.md`, `PROBLEM2.md` | |

## GitHub에 올리면 안 되는 것 ❌

| 분류 | 위치 | 이유 |
|---|---|---|
| 도구 본체 | `tools/gem5`, `tools/mcpat`, `tools/hotspot` | 용량이 큼 (gem5 실행 파일만 약 1GB). 각자 설치 |
| PARSEC 디스크 이미지, 커널 | `tools/resources/` | 26GB라 GitHub 용량 제한(파일당 100MB)을 넘음 |
| 시뮬레이션 출력 | `out/` | 실행 하나에 2~5 MB씩 생김. 필요한 값은 `run_details.csv`로 뽑아서 올림 |
| 그래프용 가상환경 | `.venv/` | 각자 만드는 도구 환경 (약 180 MB) |
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
