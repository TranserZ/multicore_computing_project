#!/usr/bin/env python3
"""
실행 결과 기록: problem1/results/runs.csv

gem5 실행 한 번이 한 줄이다. 같은 구성을 여러 번 돌리면 줄이 계속 쌓이므로
(out/<이름>/은 덮어써져도 기록은 남는다) 나중에 평균과 편차를 낼 수 있다.

  - run_whole_pipeline.py가 끝날 때마다 자동으로 기록한다.
  - 줄은 gem5 결과(stats.txt)의 md5(run_id)로 구분한다. 같은 gem5 결과를
    --skip-gem5로 다시 처리하면 줄이 늘지 않고 전력, 온도 값만 갱신된다.
    out_name(각자 컴퓨터의 출력 폴더 이름)은 참고용이라 서로 겹쳐도 된다.
  - 누가 어디서 돌렸는지는 runner(git 사용자 이름)와 host_cpu 열로 구분한다.

사용 예:
  results_log.py summary                      구성별 실행 횟수, 평균, 표준편차
  results_log.py summary --out summary.csv    위 표를 CSV로도 저장
  results_log.py add out/blackscholes_c1_w4   이미 있는 결과 폴더를 기록에 추가
"""

import argparse
import csv
import fcntl
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
RUNS_CSV = os.path.join(PROJ_DIR, "problem1", "results", "runs.csv")

# 구성을 정하는 열. 이 값이 모두 같은 줄끼리 같은 구성으로 보고 평균을 낸다.
CONFIG_COLS = ["benchmark", "size", "cores", "width", "rob", "iq", "lq", "sq",
               "l2", "clock", "dcache_ports", "warmup_runs"]
METRIC_COLS = ["roi_ms", "insts", "ipc", "power_W", "dynamic_W", "leakage_W",
               "area_mm2", "peak_C", "avg_C"]
COLUMNS = (["run_id", "gem5_finished"] + CONFIG_COLS + METRIC_COLS
           + ["gem5_host_min", "host_cpu", "runner", "git_commit", "out_name"])
# summary에서 평균, 표준편차를 내는 값
SUMMARY_METRICS = ["roi_ms", "ipc", "power_W", "peak_C", "avg_C"]


def host_cpu():
    """gem5를 돌린 컴퓨터의 CPU 모델 이름."""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return " ".join(line.split(":", 1)[1].split())
    except OSError:
        pass
    return ""


def runner_name():
    """실험을 돌린 사람. 환경 변수 RESULTS_RUNNER가 있으면 그 값, 없으면 git 사용자 이름."""
    name = os.environ.get("RESULTS_RUNNER", "").strip()
    if not name:
        name = subprocess.run(["git", "-C", PROJ_DIR, "config", "user.name"],
                              capture_output=True, text=True).stdout.strip()
    return name


def git_commit():
    """현재 commit (짧은 해시). commit하지 않은 코드 변경이 있으면 -dirty를 붙인다."""
    def git(*a):
        return subprocess.run(["git", "-C", PROJ_DIR, *a], capture_output=True,
                              text=True).stdout.strip()
    head = git("rev-parse", "--short", "HEAD")
    if not head:
        return ""
    # 기록 파일 자체는 실행할 때마다 바뀌므로 변경 여부를 볼 때 뺀다.
    dirty = git("status", "--porcelain", "--untracked-files=no",
                "--", ".", ":!problem1/results")
    return head + ("-dirty" if dirty else "")


def make_row(out_dir, commit=""):
    """결과 폴더(out/<이름>/) 하나를 기록 한 줄로 만든다."""
    stats = os.path.join(out_dir, "gem5", "stats.txt")
    with open(os.path.join(out_dir, "result.json")) as f:
        r = json.load(f)
    with open(stats, "rb") as f:
        run_id = hashlib.md5(f.read()).hexdigest()[:12]
    perf, power, temp = r["performance"], r["power"], r["temperature_C"]
    row = {
        "run_id": run_id,
        "gem5_finished": time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(os.path.getmtime(stats))),
        "roi_ms": round(perf["roi_seconds"] * 1e3, 6),
        "insts": int(perf["committed_insts"]),
        "ipc": round(perf["ipc"], 4),
        "power_W": round(power["total_power_W"], 4),
        "dynamic_W": round(power["dynamic_power_W"], 4),
        "leakage_W": round(power["leakage_power_W"], 4),
        "area_mm2": round(power["area_mm2"], 3),
        "peak_C": round(temp["peak"], 2),
        "avg_C": round(temp["average"], 3),
        "gem5_host_min": round(perf.get("host_seconds", 0) / 60, 1),
        "host_cpu": host_cpu(),
        "runner": runner_name(),
        "git_commit": commit,
        "out_name": r.get("name", os.path.basename(os.path.normpath(out_dir))),
    }
    row.update({k: r["config"][k] for k in CONFIG_COLS})
    return {k: str(row[k]) for k in COLUMNS}


def read_rows(path=RUNS_CSV):
    """기록을 읽는다. run_id가 같은 줄이 여러 개면 마지막 것만 남긴다."""
    if not os.path.exists(path):
        return []
    rows = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("run_id"):
                rows[row["run_id"]] = row
    return list(rows.values())


def sort_key(row):
    """같은 구성의 실행이 붙어 있도록: 벤치마크, 코어 수, width, 나머지 설정, 실행 시각 순."""
    def num(v):
        return (0, float(v), "") if v.replace(".", "", 1).isdigit() else (1, 0.0, v)
    rest = [k for k in CONFIG_COLS if k not in ("benchmark", "cores", "width")]
    return ([(1, 0.0, row["benchmark"]), num(row["cores"]), num(row["width"])]
            + [num(row[k]) for k in rest]
            + [(1, 0.0, row["gem5_finished"]), (1, 0.0, row["run_id"])])


def record(out_dir, gem5_ran, commit=None, path=RUNS_CSV):
    """
    결과 폴더 하나를 기록에 넣는다. 반환: (줄, 'added' 또는 'updated')

    gem5_ran: 방금 gem5를 실행했으면 True. 이때만 현재 commit을 적는다
              (기존 gem5 결과는 어느 commit의 코드로 돌렸는지 알 수 없으므로).
    """
    if commit is None:
        commit = git_commit() if gem5_ran else ""
    new = make_row(out_dir, commit)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # 여러 실행이 동시에 끝나도 줄이 섞이지 않게 파일을 잠그고 다시 쓴다.
    with open(path, "a+", newline="") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        rows = read_rows(path)
        old = next((r for r in rows if r["run_id"] == new["run_id"]), None)
        if old:
            # 같은 gem5 실행: gem5와 관련된 정보는 처음 기록을 유지하고 나머지만 갱신
            for k in ("gem5_finished", "host_cpu", "gem5_host_min", "runner"):
                new[k] = old.get(k) or new[k]
            new["git_commit"] = old.get("git_commit") or new["git_commit"]
            rows[rows.index(old)] = new
        else:
            rows.append(new)
        rows.sort(key=sort_key)
        f.seek(0)
        f.truncate()
        w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in COLUMNS})
    return new, ("updated" if old else "added")


# ----------------------------------------------------------------------------
def summarize(rows):
    """구성별로 묶어 실행 횟수, 평균, 표준편차를 낸다."""
    groups = {}
    for r in rows:
        groups.setdefault(tuple(r[k] for k in CONFIG_COLS), []).append(r)

    def key(cfg):
        d = dict(zip(CONFIG_COLS, cfg))
        return (d["benchmark"], int(d["cores"]), int(d["width"]), cfg)

    out = []
    for cfg in sorted(groups, key=key):
        g = groups[cfg]
        s = dict(zip(CONFIG_COLS, cfg))
        s["n"] = len(g)
        for m in SUMMARY_METRICS:
            v = [float(r[m]) for r in g]
            s[m + "_mean"] = statistics.mean(v)
            s[m + "_std"] = statistics.stdev(v) if len(v) > 1 else None
        s["roi_spread_pct"] = ((max(float(r["roi_ms"]) for r in g)
                                - min(float(r["roi_ms"]) for r in g))
                               / s["roi_ms_mean"] * 100) if len(g) > 1 else None
        s["hosts"] = len({(r["host_cpu"], r.get("runner", "")) for r in g})
        out.append(s)
    return out


def cmd_summary(args):
    rows = read_rows(args.csv)
    if not rows:
        sys.exit(f"기록이 없습니다: {args.csv}")
    summ = summarize(rows)
    default = {"size": "simsmall", "rob": "128", "iq": "64", "lq": "32", "sq": "32",
               "l2": "2MiB", "clock": "3GHz", "dcache_ports": "2", "warmup_runs": "1"}

    def pm(s, m, digits):
        mean, std = s[m + "_mean"], s[m + "_std"]
        return f"{mean:.{digits}f}" + (f" ±{std:.{digits}f}" if std is not None else "")

    print(f"{'benchmark':14s}{'cores':>5s}{'width':>6s}{'n':>3s}  {'ROI(ms)':>18s}"
          f"{'범위':>7s}  {'IPC':>13s}  {'전력(W)':>15s}  {'최고(C)':>12s}  기본값과 다른 설정")
    for s in summ:
        other = ", ".join(f"{k}={s[k]}" for k in default if s[k] != default[k])
        spread = f"{s['roi_spread_pct']:.1f}%" if s["roi_spread_pct"] is not None else "-"
        if s["hosts"] > 1:
            other = (other + ", " if other else "") + f"실행 환경 {s['hosts']}곳 섞임"
        print(f"{s['benchmark']:14s}{s['cores']:>5s}{s['width']:>6s}{s['n']:>3d}  "
              f"{pm(s, 'roi_ms', 3):>18s}{spread:>8s}  {pm(s, 'ipc', 3):>13s}  "
              f"{pm(s, 'power_W', 3):>14s}  {pm(s, 'peak_C', 2):>12s}  {other}")
    print(f"\n실행 {len(rows)}개, 구성 {len(summ)}개  (± 는 표준편차, "
          "범위 = (최대 - 최소) / 평균. 1회만 실행한 구성은 표시 없음)")
    if args.out:
        cols = (CONFIG_COLS + ["n"]
                + [m + suf for m in SUMMARY_METRICS for suf in ("_mean", "_std")]
                + ["roi_spread_pct", "hosts"])
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
            w.writeheader()
            for s in summ:
                w.writerow({k: ("" if s[k] is None else
                                round(s[k], 6) if isinstance(s[k], float) else s[k])
                            for k in cols})
        print(f"저장: {args.out}")


def cmd_add(args):
    for d in args.out_dirs:
        try:
            row, how = record(d, gem5_ran=False, commit=args.commit, path=args.csv)
        except (OSError, KeyError, ValueError) as e:
            print(f"[건너뜀] {d}: {e}")
            continue
        print(f"[{'추가' if how == 'added' else '갱신'}] {row['out_name']}  "
              f"run_id {row['run_id']}  ROI {row['roi_ms']} ms")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default=RUNS_CSV, help="기록 파일 (기본: %(default)s)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("summary", help="구성별 실행 횟수, 평균, 표준편차")
    p.add_argument("--out", help="요약 표를 이 CSV 파일로도 저장")
    p.set_defaults(func=cmd_summary)
    p = sub.add_parser("add", help="이미 있는 결과 폴더를 기록에 추가")
    p.add_argument("out_dirs", nargs="+", help="out/<이름> 폴더")
    p.add_argument("--commit", default="",
                   help="그 gem5 실행에 쓴 코드의 commit을 알고 있으면 적는다")
    p.set_defaults(func=cmd_add)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
