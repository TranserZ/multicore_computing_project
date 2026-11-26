#!/usr/bin/env python3
"""
실행별 상세 통계: out/<이름>/ 의 원본에서 분석에 쓰는 값만 뽑아 CSV 한 줄로 만든다.

out/ 은 용량이 커서 GitHub에 올리지 않는다 (실행 하나에 2~5 MB, 대부분 gem5가 기록한
통계 수천 줄과 설정 파일). 그 가운데 분석에 실제로 쓴 값은 실행당 수십 개뿐이므로,
그 값들만 problem1/results/run_details.csv 에 모아 둔다. 실행 하나가 한 줄이다.

  - 어떤 실행을 뽑을지는 runs.csv가 정한다 (run_id = stats.txt의 md5로 원본과 맞춰 본다).
  - 원본이 이 컴퓨터에 없는 실행(다른 사람이 돌렸거나 지운 것)은 기존 줄을 그대로 둔다.
  - 그래프 스크립트(plot_results.py)의 전력 분해 그림은 이 파일을 읽어 그린다.

사용법:
  extract_details.py            out/ 에서 뽑아 run_details.csv 갱신
  extract_details.py --check    무엇이 새로 뽑히는지만 출력
"""

import argparse
import csv
import glob
import hashlib
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(PROJ_DIR, "problem1", "glue"))

import glue_code1  # noqa: E402
import glue_code2  # noqa: E402
import results_log  # noqa: E402

DETAILS_CSV = os.path.join(os.path.dirname(results_log.RUNS_CSV), "run_details.csv")

ID_COLS = ["run_id", "out_name", "benchmark", "cores", "width", "rob", "iq", "lq", "sq",
           "warmup_runs"]
# 전력을 나누는 부품 (McPAT 출력 기준). L1 캐시는 McPAT이 코어 안에 넣어 계산하므로 따로 꺼낸다.
POWER_PARTS = ["core", "l1", "l2", "bus", "mc"]
# 코어 안에서 따로 기록하는 유닛: (열 이름, McPAT 출력의 이름)
CORE_UNITS = [("ifu", "Instruction Fetch Unit"), ("rename", "Renaming Unit"),
              ("lsu", "Load Store Unit"), ("mmu", "Memory Management Unit"),
              ("exe", "Execution Unit"), ("scheduler", "Instruction Scheduler"),
              ("result_bus", "Results Broadcast Bus")]

COLUMNS = (
    ID_COLS
    # gem5: 실행한 양 (모든 코어의 합)
    + ["roi_ms", "insts", "user_insts", "uops", "fp_ops", "loads", "stores", "branches",
       "function_calls", "busy_cycles", "ipc"]
    # gem5: 코어가 ROI 중 실제로 동작한 비율(%)과 잠든 횟수
    + ["active_pct_mean", "active_pct_min", "active_pct_max", "times_idled"]
    # gem5: 파이프라인
    + ["decoded_uops", "branch_mispredicts", "rename_blocked_pct", "issue_idle_pct",
       "rob_full_events", "iq_full_events", "lq_full_events", "sq_full_events",
       "reg_full_events"]
    # gem5: 메모리 계층
    + ["l1i_misses", "l1d_misses", "l2_misses", "l2_miss_latency_ns", "itlb_misses",
       "dtlb_misses", "bus_packets", "mem_reads", "mem_writes"]
    # McPAT: 부품별 전력 (W), 코어 안 유닛의 dynamic 전력 (W, 모든 코어의 합)
    + [f"{p}_{t}_W" for p in POWER_PARTS for t in ("dynamic", "leakage")]
    + [f"{u}_dynamic_W" for u, _ in CORE_UNITS]
    + ["total_W", "area_mm2"]
    # HotSpot: 온도와 가장 뜨거운 실행 블록의 전력 밀도
    + ["peak_C", "peak_block", "avg_C", "l2_C", "exe_max_W_per_mm2"]
)


# ----------------------------------------------------------------------------
def find_run_dir(out_name):
    """out/<이름>/ 또는 out/<묶음>/<이름>/ 을 찾는다. 없으면 None."""
    for d in (glob.glob(os.path.join(PROJ_DIR, "out", out_name))
              + glob.glob(os.path.join(PROJ_DIR, "out", "*", out_name))):
        if os.path.isfile(os.path.join(d, "gem5", "stats.txt")):
            return d
    return None


def stats_md5(run_dir):
    with open(os.path.join(run_dir, "gem5", "stats.txt"), "rb") as f:
        return hashlib.md5(f.read()).hexdigest()[:12]


def clock_hz(text):
    m = re.fullmatch(r"([0-9.]+)\s*([GMk]?)Hz", text)
    return float(m.group(1)) * {"G": 1e9, "M": 1e6, "k": 1e3, "": 1}[m.group(2)]


def gem5_values(run_dir, clock):
    s = glue_code1.read_stats(os.path.join(run_dir, "gem5", "stats.txt"))
    cores = glue_code1.indexed_prefixes(s, r"board\.processor\.switch(\d*)\.core\.")

    def total(name):          # 모든 코어의 합
        return sum(s.get(p + name, 0.0) for p in cores)

    def caches(kind, name):   # L1 캐시 (코어마다 하나)의 합
        return sum(v for k, v in s.items()
                   if re.fullmatch(rf"board\.cache_hierarchy\.{kind}\d*\.{name}", k))

    roi = s["simSeconds"]
    cycles = total("numCycles")
    # ROI의 사이클 수: gem5가 기록한 클럭 주기(tick)를 쓴다. 3GHz는 실제로 333 ps라 3.003 GHz다.
    period = s.get("board.clk_domain.clock")
    roi_cycles = s["simTicks"] / period if period else roi * clock
    active = [s.get(p + "numCycles", 0.0) / roi_cycles * 100 for p in cores]
    l2 = "board.cache_hierarchy.l2cache."
    return {
        "roi_ms": roi * 1e3,
        "insts": total("commitStats0.numInsts"),
        "user_insts": total("commitStats0.numUserInsts"),
        "uops": total("commitStats0.numOps"),
        "fp_ops": total("commitStats0.numFpInsts"),
        "loads": total("commitStats0.numLoadInsts"),
        "stores": total("commitStats0.numStoreInsts"),
        "branches": total("executeStats0.numBranches"),
        "function_calls": total("commitStats0.functionCalls"),
        "busy_cycles": cycles,
        "ipc": total("commitStats0.numInsts") / cycles if cycles else 0.0,
        "active_pct_mean": sum(active) / len(active),
        "active_pct_min": min(active),
        "active_pct_max": max(active),
        "times_idled": total("timesIdled"),
        "decoded_uops": total("decode.decodedInsts"),
        "branch_mispredicts": total("commit.branchMispredicts"),
        "rename_blocked_pct": total("rename.status::Blocked") / cycles * 100 if cycles else 0.0,
        "issue_idle_pct": total("numIssuedDist::0") / cycles * 100 if cycles else 0.0,
        "rob_full_events": total("rename.ROBFullEvents"),
        "iq_full_events": total("rename.IQFullEvents"),
        "lq_full_events": total("rename.LQFullEvents"),
        "sq_full_events": total("rename.SQFullEvents"),
        "reg_full_events": total("rename.fullRegistersEvents"),
        "l1i_misses": caches("l1icaches", "overallMisses::total"),
        "l1d_misses": caches("l1dcaches", "overallMisses::total"),
        "l2_misses": s.get(l2 + "overallMisses::total", 0.0),
        "l2_miss_latency_ns": s.get(l2 + "overallAvgMissLatency::total", 0.0) / 1e3,  # tick = 1 ps
        "itlb_misses": total("mmu.itb.rdMisses") + total("mmu.itb.wrMisses")
                       + total("mmu.itb.exMisses"),
        "dtlb_misses": total("mmu.dtb.rdMisses") + total("mmu.dtb.wrMisses"),
        "bus_packets": s.get("board.cache_hierarchy.l2bus.pktCount::total", 0.0),
        "mem_reads": sum(v for k, v in s.items()
                         if re.fullmatch(r"board\.memory\.mem_ctrl\d*\.readReqs", k)),
        "mem_writes": sum(v for k, v in s.items()
                          if re.fullmatch(r"board\.memory\.mem_ctrl\d*\.writeReqs", k)),
    }


def mcpat_values(run_dir):
    """부품별 (dynamic, leakage)와 코어 안 유닛의 dynamic 전력."""
    nodes = glue_code2.parse_mcpat(os.path.join(run_dir, "mcpat", "mcpat_out.txt"))
    top = [i for i, (ind, _, _) in enumerate(nodes) if ind == 0]
    part = {p: [0.0, 0.0] for p in POWER_PARTS}
    unit = {u: 0.0 for u, _ in CORE_UNITS}
    unit_name = {name: u for u, name in CORE_UNITS}

    def add(key, metrics, sign=1):
        dyn, leak = glue_code2.power_of(metrics)
        part[key][0] += sign * dyn
        part[key][1] += sign * leak

    for k, i in enumerate(top):
        name, metrics = nodes[i][1], nodes[i][2]
        end = top[k + 1] if k + 1 < len(top) else len(nodes)
        if name == "Core":
            add("core", metrics)
            for _, sub, m in nodes[i + 1:end]:
                if sub in ("Instruction Cache", "Data Cache"):
                    add("l1", m)
                    add("core", m, -1)
                if sub in unit_name:
                    unit[unit_name[sub]] += m.get("Runtime Dynamic", 0.0)
        elif name in ("L2", "BUSES", "Memory Controller"):
            add({"L2": "l2", "BUSES": "bus", "Memory Controller": "mc"}[name], metrics)
    out = {f"{p}_{t}_W": part[p][j] for p in POWER_PARTS for j, t in enumerate(("dynamic", "leakage"))}
    out.update({f"{u}_dynamic_W": v for u, v in unit.items()})
    out["total_W"] = sum(a + b for a, b in part.values())
    return out


def thermal_values(run_dir):
    with open(os.path.join(run_dir, "result.json")) as f:
        temp = json.load(f)["temperature_C"]
    with open(os.path.join(run_dir, "hotspot", "power.json")) as f:
        power = json.load(f)
    exe = [b["power_W"] / b["area_mm2"] for n, b in power["blocks"].items() if n.endswith("_EXE")]
    return {"area_mm2": power["area_mm2"], "peak_C": temp["peak"],
            "peak_block": temp["peak_block"], "avg_C": temp["average"],
            "l2_C": temp["blocks"].get("L2", ""), "exe_max_W_per_mm2": max(exe)}


def fmt(v):
    if isinstance(v, float):
        return str(int(v)) if v == int(v) and abs(v) >= 1000 else f"{v:.6g}"
    return str(v)


def extract(row):
    """runs.csv의 한 줄 -> 상세 통계 한 줄. 원본이 없거나 다른 실행이면 None."""
    run_dir = find_run_dir(row["out_name"])
    if run_dir is None or stats_md5(run_dir) != row["run_id"]:
        return None
    d = {c: row[c] for c in ID_COLS}
    d.update(gem5_values(run_dir, clock_hz(row["clock"])))
    d.update(mcpat_values(run_dir))
    d.update(thermal_values(run_dir))
    return {c: fmt(d[c]) for c in COLUMNS}


def read_details(path=DETAILS_CSV):
    """{run_id: 줄}"""
    if not os.path.exists(path):
        return {}
    with open(path, newline="") as f:
        return {r["run_id"]: r for r in csv.DictReader(f)}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="파일을 쓰지 않고 결과만 출력")
    args = ap.parse_args()

    runs = results_log.read_rows()
    old = read_details()
    rows, fresh, kept, missing = [], 0, 0, []
    for r in sorted(runs, key=results_log.sort_key):
        d = extract(r)
        if d is not None:
            fresh += 1
        elif r["run_id"] in old:
            d = {c: old[r["run_id"]].get(c, "") for c in COLUMNS}
            kept += 1
        else:
            missing.append(r["out_name"])
            continue
        rows.append(d)

    print(f"runs.csv {len(runs)}건: 원본에서 뽑음 {fresh}, 기존 줄 유지 {kept}, "
          f"원본도 기존 줄도 없음 {len(missing)}")
    if missing:
        print("  없는 실행:", ", ".join(missing[:8]) + (" ..." if len(missing) > 8 else ""))
    if args.check:
        return
    with open(DETAILS_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"-> {os.path.relpath(DETAILS_CSV, PROJ_DIR)} ({len(rows)}줄, {len(COLUMNS)}열, "
          f"{os.path.getsize(DETAILS_CSV) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
