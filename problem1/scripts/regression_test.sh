#!/usr/bin/env bash
# =============================================================================
# 회귀 테스트: 파이프라인(gem5 -> glue 1 -> McPAT -> glue 2 -> HotSpot)이
# 각 testcase에서 제대로 된 결과를 내는지 검사한다. 코드를 바꿀 때마다 실행한다.
#
# 사용법:
#   regression_test.sh                     기본 testcase 전체, 빠른 모드
#   regression_test.sh blackscholes:4:4    지정한 testcase만 (<benchmark>:<코어 수>:<width>)
#   regression_test.sh --full [testcase]   gem5부터 다시 실행
#   regression_test.sh --update-baseline   지금 결과를 기준값으로 저장
#
# 옵션:
#   --full             gem5부터 다시 실행한다. gem5_config.py나 gem5 자체를 바꿨을 때 사용.
#                      오래 걸린다 (4개 동시 실행 기준 blackscholes 6~10분, canneal 약 35분,
#                      streamcluster 약 65분, freqmine 약 125분).
#   --update-baseline  검사를 통과한 testcase의 결과를 기준값(out/<이름>/baseline.json)으로 저장
#   -j N               동시에 실행할 testcase 수 (기본 4)
#   -v, --verbose      통과한 검사도 모두 출력
#   --list             기본 testcase 목록 출력
#
# 빠른 모드 (기본):
#   out/<이름>/gem5/의 기존 gem5 결과를 그대로 두고 glue 1 -> McPAT -> glue 2 -> HotSpot만
#   다시 실행한다 (testcase당 몇 초). glue code, 템플릿, HotSpot 설정을 바꿨을 때 쓴다.
#   gem5 결과가 아직 없는 testcase는 SKIP으로 표시한다.
#
# 검사 내용:
#   1. 기본 검사   : 파이프라인 종료 코드, 출력 파일, 코어 수/width가 요청대로인지,
#                    ROI만 측정했는지, 값의 범위, 단계 사이 값의 일치(전력 합계, 블록 이름,
#                    배치도 겹침/빈틈, 면적), 온도 범위
#   2. 기준값 비교 : 저장해 둔 결과와 비교. gem5 결과가 기준값 때와 같은 파일이면 거의 정확히
#                    같아야 하고(1e-6), gem5를 다시 실행했으면 FULL_TOL(기본 0.1 = 10%) 안이어야 한다.
#   3. 경향 검사   : 코어 수/width를 키웠을 때 면적, leakage가 늘어나는지 (FAIL),
#                    실행 시간, 전력이 예상 방향으로 움직이는지 (WARN)
#
# 종료 코드: FAIL이 하나라도 있으면 1, 없으면 0 (WARN, SKIP은 0)
# 실행 로그: out/regression/<이름>.log
# =============================================================================
set -uo pipefail

PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="$PROJ_DIR/out"
LOGDIR="$OUT/regression"
PIPELINE="$PROJ_DIR/problem1/scripts/run_whole_pipeline.py"

# 벤치마크 4개, 코어 수 1/4/8, width 2/4/8이 각각 한 번 이상 들어가도록 고른 목록
DEFAULT_CASES=(
    blackscholes:1:4
    canneal:1:4
    freqmine:1:4
    streamcluster:1:4
    blackscholes:4:4
    blackscholes:8:4
    blackscholes:1:2
    blackscholes:1:8
)

FULL=0; UPDATE=0; VERBOSE=0; JOBS=4; CASES=()
FULL_TOL="${FULL_TOL:-0.10}"

usage() { sed -n '2,/^# ====/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; }

while (( $# )); do
    case "$1" in
        --full)            FULL=1 ;;
        --update-baseline) UPDATE=1 ;;
        -v|--verbose)      VERBOSE=1 ;;
        -j)                JOBS="${2:-}"; shift ;;
        --list)            printf '%s\n' "${DEFAULT_CASES[@]}"; exit 0 ;;
        -h|--help)         usage; exit 0 ;;
        -*)                echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
        *)                 CASES+=("$1") ;;
    esac
    shift
done
(( ${#CASES[@]} )) || CASES=("${DEFAULT_CASES[@]}")

[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || { echo "-j 값은 1 이상의 정수여야 합니다." >&2; exit 2; }
for c in "${CASES[@]}"; do
    [[ "$c" =~ ^(blackscholes|canneal|freqmine|streamcluster):[1-9][0-9]*:[1-9][0-9]*$ ]] \
        || { echo "testcase 형식이 틀렸습니다: '$c' (예: blackscholes:4:4)" >&2; exit 2; }
done
for f in tools/gem5/build/X86/gem5.opt tools/mcpat/mcpat tools/hotspot/hotspot; do
    [[ -x "$PROJ_DIR/$f" ]] || { echo "$f 가 없습니다. 먼저 ./setup.sh를 실행하세요." >&2; exit 2; }
done
docker info >/dev/null 2>&1 || { echo "Docker를 사용할 수 없습니다." >&2; exit 2; }

mkdir -p "$LOGDIR"
T0=$SECONDS
if (( FULL )); then mode="전체 (gem5부터 다시 실행)"; else mode="빠른 모드 (기존 gem5 결과 사용)"; fi
echo "회귀 테스트: testcase ${#CASES[@]}개, $mode, 동시 실행 $JOBS개"

# -----------------------------------------------------------------------------
# 1단계: testcase마다 파이프라인 실행. 결과는 $LOGDIR/<이름>.rc 에 남긴다.
#   숫자     파이프라인 종료 코드
#   nogem5   gem5 결과가 아직 없음 (빠른 모드에서 SKIP)
#   gem5fail 이전 gem5 실행이 실패해서 쓸 수 있는 결과가 없음
# -----------------------------------------------------------------------------
run_case() {
    local bench cores width
    IFS=: read -r bench cores width <<< "$1"
    local name="${bench}_c${cores}_w${width}"
    local d="$OUT/$name" t=$SECONDS
    local args=(--benchmark "$bench" --cores "$cores" --width "$width")
    rm -f "$LOGDIR/$name.rc" "$LOGDIR/$name.log"

    if (( ! FULL )); then
        if [[ -s "$d/gem5/stats.txt" && -s "$d/gem5/config.json" ]] \
            && grep -q 'End Simulation Statistics' "$d/gem5/stats.txt"; then
            args+=(--skip-gem5)
        elif [[ -e "$d/gem5/gem5.log" ]]; then
            echo gem5fail > "$LOGDIR/$name.rc"; return
        else
            echo nogem5 > "$LOGDIR/$name.rc"; return
        fi
    fi
    python3 "$PIPELINE" "${args[@]}" > "$LOGDIR/$name.log" 2>&1
    echo $? > "$LOGDIR/$name.rc"
    echo "  실행 끝: $name ($(( SECONDS - t ))초)"
}

for c in "${CASES[@]}"; do
    while (( $(jobs -rp | wc -l) >= JOBS )); do wait -n || true; done
    run_case "$c" &
done
wait

# -----------------------------------------------------------------------------
# 2단계: 결과 검사
# -----------------------------------------------------------------------------
COLOR=0; [[ -t 1 ]] && COLOR=1
python3 - "$PROJ_DIR" "$VERBOSE" "$UPDATE" "$FULL_TOL" "$COLOR" "${CASES[@]}" <<'PYEOF'
import hashlib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

proj = sys.argv[1]
verbose, update = sys.argv[2] == "1", sys.argv[3] == "1"
full_tol, color = float(sys.argv[4]), sys.argv[5] == "1"
case_args = sys.argv[6:]

OUT = os.path.join(proj, "out")
LOGDIR = os.path.join(OUT, "regression")
EXACT_TOL = 1e-6          # 같은 gem5 결과로 다시 계산했을 때 허용하는 상대 오차
MAX_TEMP_K = 398.15       # 이보다 뜨거우면 모델링이 잘못된 것으로 본다 (125 C)
CORE_BLOCKS = ("FE", "EXE", "MEM")
WIDTH_KEYS = ("fetchWidth", "decodeWidth", "renameWidth",
              "dispatchWidth", "issueWidth", "commitWidth")
METRIC_LABEL = {"roi_ms": "ROI 시간(ms)", "ipc": "IPC", "power_W": "전력(W)",
                "dynamic_W": "dynamic(W)", "leakage_W": "leakage(W)",
                "area_mm2": "면적(mm^2)", "peak_C": "최고 온도(C)", "avg_C": "평균 온도(C)"}


def paint(s, code):
    return f"\033[{code}m{s}\033[0m" if color else s


TAG = {"PASS": paint("[PASS]", "32"), "FAIL": paint("[FAIL]", "31"),
       "SKIP": paint("[SKIP]", "33"), "WARN": paint("[WARN]", "33")}


def as_list(x):
    return x if isinstance(x, list) else [x]


def close(a, b, rel, abs_tol=0.0):
    return abs(a - b) <= max(rel * max(abs(a), abs(b)), abs_tol)


def read_stats(text):
    """stats.txt 마지막 블록을 {이름: 값}으로."""
    s = {}
    last = text.rfind("Begin Simulation Statistics")
    for line in text[max(last, 0):].splitlines():
        p = line.split()
        if len(p) >= 2:
            try:
                s[p[0]] = float(p[1])
            except ValueError:
                pass
    return s


def failure_reason(path):
    """로그에서 실패 원인으로 보이는 줄 하나를 찾는다."""
    try:
        lines = open(path, errors="replace").read().splitlines()
    except OSError:
        return ""
    for pat in (r"Assertion .* failed|panic:|fatal:|Segmentation fault|Traceback", r"실패|Error|error"):
        for ln in lines:
            if re.search(pat, ln):
                return ln.strip()[:220]
    return lines[-1].strip()[:220] if lines else ""


class Case:
    def __init__(self, arg):
        self.bench, cores, width = arg.split(":")
        self.cores, self.width = int(cores), int(width)
        self.name = f"{self.bench}_c{self.cores}_w{self.width}"
        self.dir = os.path.join(OUT, self.name)
        self.checks = []      # (PASS|FAIL|WARN, 설명, 실패 내용)
        self.skip = ""        # SKIP 사유
        self.metrics = None
        self.note = ""

    def p(self, *parts):
        return os.path.join(self.dir, *parts)

    def ok(self, cond, label, detail=""):
        self.checks.append(("PASS" if cond else "FAIL", label, "" if cond else str(detail)))
        return bool(cond)

    def warn(self, cond, label, detail=""):
        self.checks.append(("PASS" if cond else "WARN", label, "" if cond else str(detail)))

    def group(self, title, fn):
        """검사 묶음 하나를 실행. 파일이 깨져 예외가 나면 그 묶음을 FAIL로 기록."""
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            self.ok(False, title, f"검사 중 오류: {type(e).__name__}: {e}")

    @property
    def n_fail(self):
        return sum(c[0] == "FAIL" for c in self.checks)

    @property
    def n_warn(self):
        return sum(c[0] == "WARN" for c in self.checks)

    @property
    def status(self):
        return "SKIP" if self.skip else "FAIL" if self.n_fail else "PASS"


# -----------------------------------------------------------------------------
# testcase 하나 검사
# -----------------------------------------------------------------------------
def check_case(c):
    ctx = {}
    try:
        rc = open(os.path.join(LOGDIR, c.name + ".rc")).read().strip()
    except OSError:
        rc = "?"
    if rc == "nogem5":
        c.skip = "gem5 결과 없음 (--full로 만들 수 있음)"
        return
    if rc == "gem5fail":
        c.ok(False, "gem5 결과", "이전 gem5 실행이 실패해 쓸 수 있는 결과가 없음: "
             + failure_reason(c.p("gem5", "gem5.log")))
        return
    if rc != "0":
        log = os.path.join(LOGDIR, c.name + ".log")
        detail = f"exit {rc}: {failure_reason(log)}"
        if "gem5 실패" in detail:
            detail += " / gem5: " + failure_reason(c.p("gem5", "gem5.log"))
        c.ok(False, "파이프라인 실행", detail)
        return
    c.ok(True, "파이프라인 실행")

    files = ["gem5/stats.txt", "gem5/config.json", "mcpat/mcpat_in.xml", "mcpat/mcpat_out.txt",
             "hotspot/chip.flp", "hotspot/chip.ptrace", "hotspot/chip.steady", "result.json"]
    missing = [f for f in files if not (os.path.exists(c.p(f)) and os.path.getsize(c.p(f)) > 0)]
    if not c.ok(not missing, "출력 파일 8개", "없거나 비어 있음: " + ", ".join(missing)):
        return

    # ---- gem5 ----
    def gem5():
        text = open(c.p("gem5", "stats.txt")).read()
        ctx["stats_md5"] = hashlib.md5(text.encode()).hexdigest()
        n_blk = text.count("Begin Simulation Statistics")
        c.ok(n_blk == 1, "gem5: 통계 블록이 1개 (ROI 구간만 기록)", f"{n_blk}개")
        if os.path.exists(c.p("gem5", "gem5.log")):
            log = open(c.p("gem5", "gem5.log"), errors="replace").read()
            c.ok("[ROI] begin" in log and "[ROI] end" in log,
                 "gem5: ROI 시작/끝 hook에 도달", "gem5.log에 [ROI] begin/end가 없음")
        s = read_stats(text)
        sim = s.get("simSeconds", 0.0)
        c.ok(sim > 0, "gem5: ROI 시간 > 0", sim)

        board = json.load(open(c.p("gem5", "config.json")))["board"]
        cores = [x["core"] for x in as_list(board["processor"]["switch"])]
        c.ok(len(cores) == c.cores, "gem5: 코어 수가 요청과 같음", f"{len(cores)} != {c.cores}")
        types = sorted({x.get("type", "?") for x in cores})
        c.ok(all("O3" in t for t in types), "gem5: 측정 구간 CPU가 out-of-order", types)
        bad = sorted({f"{k}={x[k]}" for x in cores for k in WIDTH_KEYS if x[k] != c.width})
        c.ok(not bad, f"gem5: fetch~commit width가 모두 {c.width}", ", ".join(bad))

        pre = {}
        for k in s:
            m = re.match(r"(board\.processor\.switch(\d*)\.core\.)numCycles$", k)
            if m:
                pre[int(m.group(2) or 0)] = m.group(1)
        insts = [s.get(pre[i] + "commitStats0.numInsts", 0.0) for i in sorted(pre)]
        cycles = [s[pre[i] + "numCycles"] for i in sorted(pre)]
        c.ok(len(pre) == c.cores, "gem5: 코어별 통계 개수", f"{len(pre)} != {c.cores}")
        idle = [i for i, v in enumerate(insts) if v <= 0]
        c.ok(not idle, "gem5: 모든 코어가 ROI에서 명령어를 실행", f"실행 0인 코어: {idle}")
        ipc = sum(insts) / sum(cycles) if sum(cycles) else 0.0
        c.ok(0 < ipc <= c.width, f"gem5: 0 < IPC <= width({c.width})", f"IPC {ipc:.3f}")
        ctx.update(sim=sim, ipc=ipc, roi_cycles=s["simTicks"] / board["clk_domain"]["clock"][0])

    c.group("gem5 결과 읽기", gem5)

    # ---- glue 1 결과 (McPAT 입력) ----
    def mcpat_in():
        root = ET.parse(c.p("mcpat", "mcpat_in.xml")).getroot()
        system = next(x for x in root.iter("component") if x.get("id") == "system")
        par = lambda comp: {e.get("name"): e.get("value") for e in comp.findall("param")}
        stat = lambda comp: {e.get("name"): e.get("value") for e in comp.findall("stat")}
        cores = [x for x in system.findall("component")
                 if re.fullmatch(r"system\.core\d+", x.get("id", ""))]
        n = int(par(system)["number_of_cores"])
        c.ok(n == c.cores and len(cores) == c.cores, "McPAT 입력: 코어 수",
             f"number_of_cores={n}, core 항목 {len(cores)}개, 요청 {c.cores}")
        bad = sorted({f"{k}={par(x)[k]}" for x in cores
                      for k in ("fetch_width", "issue_width", "commit_width")
                      if int(par(x)[k]) != c.width})
        c.ok(not bad, f"McPAT 입력: fetch/issue/commit width가 {c.width}", ", ".join(bad))

        bad = []
        for comp in root.iter("component"):
            for e in comp.findall("stat"):
                try:
                    v = float(e.get("value"))
                    if not (v >= 0 and v != float("inf")):
                        raise ValueError
                except (TypeError, ValueError):
                    bad.append(f"{comp.get('id')}.{e.get('name')}={e.get('value')}")
        c.ok(not bad, "McPAT 입력: 모든 활동량이 0 이상의 유한한 수", ", ".join(bad[:5]))
        zero = [x.get("id") for x in cores if float(stat(x)["committed_instructions"]) <= 0]
        c.ok(not zero, "McPAT 입력: 코어마다 committed_instructions > 0", zero)
        if "roi_cycles" in ctx:
            tc = float(stat(system)["total_cycles"])
            c.ok(close(tc, ctx["roi_cycles"], 1e-3), "McPAT 입력: 실행 시간(total_cycles)이 ROI 길이와 같음",
                 f"total_cycles {tc:.0f}, ROI {ctx['roi_cycles']:.0f} 사이클")

    c.group("McPAT 입력 읽기", mcpat_in)

    # ---- McPAT 출력 ----
    def mcpat_out():
        txt = open(c.p("mcpat", "mcpat_out.txt")).read()
        bad = re.findall(r"^.*=\s*-?(?:nan|inf)\b.*$", txt, re.M | re.I)
        c.ok(not bad, "McPAT 출력: nan/inf 없음", f"{len(bad)}곳, 예: {bad[0].strip() if bad else ''}")
        blk = re.search(r"^Processor:\s*\n(.*?)\n\s*\n", txt, re.S | re.M).group(1)
        v = {m.group(1).strip(): float(m.group(2)) for m in
             re.finditer(r"^\s*([A-Za-z ]+?)\s*=\s*([-+0-9.eE]+|nan|inf)\s", blk, re.M)}
        dyn = v["Runtime Dynamic"]
        leak = v["Subthreshold Leakage"] + v["Gate Leakage"]
        c.ok(v["Area"] > 0 and dyn > 0 and leak > 0, "McPAT 출력: 면적, dynamic, leakage > 0",
             f"면적 {v['Area']}, dynamic {dyn}, leakage {leak}")
        n = int(re.search(r"Total Cores:\s*(\d+)", txt).group(1))
        n_sec = len(re.findall(r"^Core:", txt, re.M))
        c.ok(n == c.cores and n_sec == c.cores, "McPAT 출력: 코어 수",
             f"Total Cores {n}, Core 구간 {n_sec}개, 요청 {c.cores}")
        c.warn(dyn <= v["Peak Dynamic"], "McPAT 출력: Runtime Dynamic <= Peak Dynamic",
               f"runtime {dyn} > peak {v['Peak Dynamic']} (활동량이 McPAT의 최대 가정보다 큼)")
        ctx.update(m_dyn=dyn, m_leak=leak, m_area=v["Area"])

    c.group("McPAT 출력 읽기", mcpat_out)

    # ---- glue 2 결과 (HotSpot 입력) ----
    def hotspot_in():
        flp = []
        for ln in open(c.p("hotspot", "chip.flp")):
            if ln.strip() and not ln.startswith("#"):
                f = ln.split()
                flp.append((f[0],) + tuple(float(x) for x in f[1:5]))
        names = [f[0] for f in flp]
        expected = {f"core{i}_{b}" for i in range(c.cores) for b in CORE_BLOCKS} | {"L2", "MC"}
        c.ok(set(names) == expected and len(names) == len(expected),
             f"HotSpot 입력: 블록 {len(expected)}개 (코어당 3개 + L2 + MC)",
             f"없음 {sorted(expected - set(names))}, 남음 {sorted(set(names) - expected)}, 총 {len(names)}개")

        pt = open(c.p("hotspot", "chip.ptrace")).read().splitlines()
        pt_names, power = pt[0].split(), [float(x) for x in pt[1].split()]
        c.ok(pt_names == names, "HotSpot 입력: .flp와 .ptrace의 블록 이름과 순서가 같음")
        c.ok(len(power) == len(pt_names) and all(x > 0 for x in power),
             "HotSpot 입력: 모든 블록 전력 > 0",
             [n for n, x in zip(pt_names, power) if x <= 0])
        ctx["ptrace_sum"] = sum(power)
        if "m_dyn" in ctx:
            total = ctx["m_dyn"] + ctx["m_leak"]
            c.ok(close(sum(power), total, 0.01), "HotSpot 입력: 블록 전력 합 = McPAT 칩 전체 전력 (1% 이내)",
                 f"블록 합 {sum(power):.4f} W, McPAT {total:.4f} W")

        c.ok(all(w > 0 and h > 0 for _, w, h, _, _ in flp), "배치도: 모든 블록의 폭, 높이 > 0")
        eps, overlap = 1e-8, []
        for i in range(len(flp)):
            for j in range(i + 1, len(flp)):
                (a, aw, ah, ax, ay), (b, bw, bh, bx, by) = flp[i], flp[j]
                if (min(ax + aw, bx + bw) - max(ax, bx) > eps
                        and min(ay + ah, by + bh) - max(ay, by) > eps):
                    overlap.append(f"{a}/{b}")
        c.ok(not overlap, "배치도: 블록이 서로 겹치지 않음", ", ".join(overlap[:5]))
        area = sum(w * h for _, w, h, _, _ in flp)
        bbox = ((max(x + w for _, w, _, x, _ in flp) - min(x for _, _, _, x, _ in flp))
                * (max(y + h for _, _, h, _, y in flp) - min(y for _, _, _, _, y in flp)))
        c.ok(close(area, bbox, 1e-3), "배치도: 빈틈 없이 직사각형을 채움",
             f"블록 면적 합 {area * 1e6:.3f} mm^2, 외곽 {bbox * 1e6:.3f} mm^2")
        if "m_area" in ctx:
            c.ok(close(area * 1e6, ctx["m_area"], 0.01), "배치도: 면적 합 = McPAT 칩 면적 (1% 이내)",
                 f"배치도 {area * 1e6:.3f}, McPAT {ctx['m_area']:.3f} mm^2")
        ctx["names"] = names

    c.group("HotSpot 입력 읽기", hotspot_in)

    # ---- HotSpot 출력 ----
    def hotspot_out():
        steady = {}
        for ln in open(c.p("hotspot", "chip.steady")):
            f = ln.split()
            if len(f) == 2:
                steady[f[0]] = float(f[1])
        names = ctx.get("names", [])
        lost = [n for n in names if n not in steady]
        c.ok(names and not lost, "HotSpot 출력: 모든 블록의 온도가 있음", lost)
        cfg = open(os.path.join(proj, "problem1", "configs", "hotspot.config")).read()
        amb = float(re.search(r"^\s*-ambient\s+([\d.]+)", cfg, re.M).group(1))
        temps = {n: steady[n] for n in names if n in steady}
        bad = [f"{n}={t - 273.15:.1f}C" for n, t in temps.items() if not amb <= t <= MAX_TEMP_K]
        c.ok(not bad, f"HotSpot 출력: 주변 온도({amb - 273.15:.0f}C) <= 블록 온도 <= {MAX_TEMP_K - 273.15:.0f}C",
             ", ".join(bad[:5]))
        ctx["temps"] = temps

    c.group("HotSpot 출력 읽기", hotspot_out)

    # ---- 요약 파일 ----
    def result():
        r = json.load(open(c.p("result.json")))
        perf, pw, tc, cfg = r["performance"], r["power"], r["temperature_C"], r["config"]
        c.ok((cfg["benchmark"], cfg["cores"], cfg["width"]) == (c.bench, c.cores, c.width),
             "result.json: 구성이 요청과 같음", cfg)
        c.ok(close(pw["total_power_W"], pw["dynamic_power_W"] + pw["leakage_power_W"], 1e-9),
             "result.json: 전체 전력 = dynamic + leakage")
        if "ptrace_sum" in ctx:
            c.ok(close(pw["total_power_W"], ctx["ptrace_sum"], 0, 1e-4),
                 "result.json: 전력이 HotSpot에 넣은 전력 합과 같음",
                 f"{pw['total_power_W']} vs {ctx['ptrace_sum']}")
        if "sim" in ctx:
            c.ok(close(perf["roi_seconds"], ctx["sim"], 1e-9), "result.json: 실행 시간이 gem5 ROI 시간과 같음")
        if ctx.get("temps"):
            peak = max(ctx["temps"].values()) - 273.15
            c.ok(close(tc["peak"], peak, 0, 0.011), "result.json: 최고 온도가 HotSpot 출력의 최댓값과 같음",
                 f"{tc['peak']} vs {peak}")
            c.ok(tc["average"] <= tc["peak"] + 1e-9, "result.json: 평균 온도 <= 최고 온도")
        c.metrics = {
            "roi_ms": perf["roi_seconds"] * 1e3, "ipc": perf["ipc"],
            "power_W": pw["total_power_W"], "dynamic_W": pw["dynamic_power_W"],
            "leakage_W": pw["leakage_power_W"], "area_mm2": pw["area_mm2"],
            "peak_C": tc["peak"], "avg_C": tc["average"],
        }

    c.group("result.json 읽기", result)

    # ---- 기준값 ----
    def baseline():
        path = c.p("baseline.json")
        if update:
            if c.n_fail == 0 and c.metrics:
                with open(path, "w") as f:
                    json.dump({"saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "gem5_stats_md5": ctx.get("stats_md5"),
                               "metrics": c.metrics}, f, indent=2)
                c.note = "기준값 저장"
            return
        if not c.metrics:
            return
        if not os.path.exists(path):
            c.note = "기준값 없음"
            return
        base = json.load(open(path))
        same = base.get("gem5_stats_md5") == ctx.get("stats_md5")
        tol = EXACT_TOL if same else full_tol
        diffs = []
        for k, new in c.metrics.items():
            old = base["metrics"].get(k)
            # 출력하는 %와 같은 기준(기준값 대비)으로 판정한다.
            if old is None or abs(new - old) > tol * abs(old):
                pct = f" ({(new - old) / old * 100:+.2f}%)" if old else ""
                diffs.append(f"{METRIC_LABEL[k]} {old} -> {new}{pct}")
        how = "같은 gem5 결과, 허용 오차 1e-6" if same else f"gem5 재실행됨, 허용 오차 {full_tol * 100:g}%"
        c.ok(not diffs, f"기준값과 일치 ({how})", "; ".join(diffs))

    c.group("기준값 비교", baseline)


# -----------------------------------------------------------------------------
# 구성 사이의 경향 검사
# -----------------------------------------------------------------------------
def check_trends(cases):
    """반환: [(PASS|FAIL|WARN, 설명, 내용)]"""
    out = []
    good = [c for c in cases if c.metrics and c.status == "PASS"]

    def pairs(key, order):
        groups = {}
        for c in good:
            groups.setdefault(key(c), []).append(c)
        for g in groups.values():
            g.sort(key=order)
            for a, b in zip(g, g[1:]):
                if order(a) != order(b):
                    yield a, b

    def rule(level, a, b, k, cond, what):
        va, vb = a.metrics[k], b.metrics[k]
        out.append(("PASS" if cond(va, vb) else level,
                    f"{a.name} -> {b.name}: {what}",
                    f"{METRIC_LABEL[k]} {va:.4g} -> {vb:.4g}"))

    for a, b in pairs(lambda c: (c.bench, c.width), lambda c: c.cores):     # 코어 수 증가
        rule("FAIL", a, b, "area_mm2", lambda x, y: y > x, "코어가 늘면 면적 증가")
        rule("FAIL", a, b, "leakage_W", lambda x, y: y > x, "코어가 늘면 leakage 증가")
        rule("WARN", a, b, "power_W", lambda x, y: y > x, "코어가 늘면 전체 전력 증가")
        rule("WARN", a, b, "roi_ms", lambda x, y: y < x, "코어가 늘면 실행 시간 감소")
    for a, b in pairs(lambda c: (c.bench, c.cores), lambda c: c.width):     # width 증가
        rule("FAIL", a, b, "area_mm2", lambda x, y: y > x, "width가 늘면 면적 증가")
        rule("FAIL", a, b, "leakage_W", lambda x, y: y > x, "width가 늘면 leakage 증가")
        rule("WARN", a, b, "roi_ms", lambda x, y: y <= x * 1.02, "width가 늘어도 실행 시간이 늘지 않음")
    return out


# -----------------------------------------------------------------------------
cases = [Case(a) for a in case_args]
for c in cases:
    check_case(c)

print("\n== testcase별 검사 ==")
for c in cases:
    if c.skip:
        print(f"{TAG['SKIP']} {c.name:24s} {c.skip}")
        continue
    n = len(c.checks)
    info = f"검사 {n}개 통과" if not c.n_fail else f"검사 {n}개 중 {c.n_fail}개 실패"
    if c.n_warn:
        info += f", 경고 {c.n_warn}개"
    if c.note:
        info += f"  ({c.note})"
    print(f"{TAG[c.status]} {c.name:24s} {info}")
    for st, label, detail in c.checks:
        if st == "PASS" and not verbose:
            continue
        mark = {"PASS": "  ok  ", "FAIL": paint(" FAIL ", "31"), "WARN": paint(" WARN ", "33")}[st]
        print(f"      {mark} {label}" + (f"\n             -> {detail}" if detail else ""))

trends = check_trends(cases)
print("\n== 구성 사이의 경향 ==")
if not trends:
    print("  비교할 수 있는 testcase 쌍이 없음")
else:
    n_bad = sum(t[0] != "PASS" for t in trends)
    print(f"  검사 {len(trends)}개 중 {len(trends) - n_bad}개 통과")
    for st, label, detail in trends:
        if st != "PASS" or verbose:
            tag = TAG[st] if st != "PASS" else "  ok  "
            print(f"  {tag} {label}\n         {detail}")

print("\n== 결과 요약 ==")
print(f"{'testcase':24s} {'상태':4s} {'ROI(ms)':>9s} {'IPC':>6s} {'speedup':>8s} "
      f"{'전력(W)':>8s} {'면적(mm2)':>9s} {'최고(C)':>7s} {'평균(C)':>7s}")
ref = {c.bench: c.metrics["roi_ms"] for c in cases
       if c.metrics and (c.cores, c.width) == (1, 4)}
for c in cases:
    m = c.metrics
    if not m:
        print(f"{c.name:24s} {c.status:4s} {'-':>9s}")
        continue
    sp = f"{ref[c.bench] / m['roi_ms']:.2f}" if c.bench in ref else "-"
    print(f"{c.name:24s} {c.status:4s} {m['roi_ms']:9.3f} {m['ipc']:6.3f} {sp:>8s} "
          f"{m['power_W']:8.3f} {m['area_mm2']:9.2f} {m['peak_C']:7.2f} {m['avg_C']:7.2f}")
print("  speedup: 같은 벤치마크의 1코어, 4-wide 구성 대비")

count = {s: sum(c.status == s for c in cases) for s in ("PASS", "FAIL", "SKIP")}
t_fail = sum(t[0] == "FAIL" for t in trends)
n_warn = sum(c.n_warn for c in cases) + sum(t[0] == "WARN" for t in trends)
print(f"\ntestcase: PASS {count['PASS']}, FAIL {count['FAIL']}, SKIP {count['SKIP']}"
      f" / 경향 검사 FAIL {t_fail} / 경고 {n_warn}")
if not update and any(c.note == "기준값 없음" for c in cases):
    print("기준값이 없는 testcase가 있습니다. 지금 결과가 맞다면 --update-baseline으로 저장하세요.")
sys.exit(1 if count["FAIL"] or t_fail else 0)
PYEOF
RC=$?
echo "소요 시간: $(( SECONDS - T0 ))초, 실행 로그: ${LOGDIR#"$PROJ_DIR"/}/"
exit $RC
