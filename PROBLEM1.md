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
│   │   └── run_whole_pipeline.py        전체 실행: gem5(Docker) → glue 1 → McPAT → glue 2 → HotSpot
│   └── changes/
│       └── mcpat_no_return_fixed.patch  McPAT 무한 루프 버그 수정
├── problem2/
├── tools/                               도구 설치 위치 (업로드 안 함)
└── out/                                 시뮬레이션 출력 (업로드 안 함)
```

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
