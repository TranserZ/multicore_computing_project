#!/usr/bin/env python3
"""
반복 실행: 구성마다 실행 횟수가 목표에 이를 때까지 파이프라인을 돌린다.

같은 구성도 실행마다 ROI 시간이 달라지므로 여러 번 돌려 평균을 낸다.
runs.csv에 이미 있는 실행 횟수를 세어 모자란 만큼만 돌리므로, 중간에 끊겨도
같은 명령을 다시 실행하면 이어서 진행된다.

  - 결과는 run_whole_pipeline.py가 problem1/results/runs.csv에 자동으로 기록한다.
  - 원본 출력은 out/<benchmark>_c<코어 수>_w<width>_r<번호>/ 에 따로 남는다.
  - 부팅 단계에서 멈춘 실행(일정 시간 안에 ROI에 들어가지 못한 실행)은 끊고 다시 돌린다.

testcase는 <benchmark>:<코어 수>:<width> 이고, 뒤에 :<ROB>:<IQ>:<LQ>:<SQ>를 붙이면
윈도우 크기를 바꾼 구성이 된다 (붙이지 않으면 기본값 128:64:32:32).

사용 예:
  run_repeats.py -n 5 blackscholes:1:4 blackscholes:4:4      구성마다 총 5회가 되도록
  run_repeats.py -n 5 --dry-run blackscholes:1:4             무엇을 돌릴지만 출력
  run_repeats.py -n 3 canneal:1:8:256:128:64:64              ROB 256, IQ 128, LQ 64, SQ 64
"""

import argparse
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
sys.path.insert(0, SCRIPT_DIR)

import results_log  # noqa: E402

PIPELINE = os.path.join(SCRIPT_DIR, "run_whole_pipeline.py")
OUT = os.path.join(PROJ_DIR, "out")
LOG_DIR = os.path.join(OUT, "repeats")
# 구성을 정하는 값 중 testcase(benchmark:cores:width)로 주지 않는 것의 기본값.
# run_whole_pipeline.py의 기본값과 같아야 한다.
DEFAULTS = {"size": "simsmall", "rob": "128", "iq": "64", "lq": "32", "sq": "32",
            "l2": "2MiB", "clock": "3GHz", "dcache_ports": "2", "warmup_runs": "1"}


WINDOW_COLS = ["rob", "iq", "lq", "sq"]


def count_runs(bench, cores, width, window):
    """runs.csv에서 이 구성(나머지는 기본값)의 실행 횟수를 센다."""
    want = dict(DEFAULTS, benchmark=bench, cores=str(cores), width=str(width), **window)
    return sum(all(r.get(k) == v for k, v in want.items())
               for r in results_log.read_rows())


def kill_container(name):
    """이 실행의 gem5 컨테이너를 찾아 끈다 (출력 폴더 경로로 찾는다)."""
    ps = subprocess.run(["docker", "ps", "--no-trunc", "--format", "{{.ID}} {{.Command}}"],
                        capture_output=True, text=True).stdout
    for line in ps.splitlines():
        cid, _, cmd = line.partition(" ")
        if f"/out/{name}/gem5 " in cmd:
            subprocess.run(["docker", "kill", cid], capture_output=True)


class Job:
    def __init__(self, bench, cores, width, window, name):
        self.bench, self.cores, self.width, self.name = bench, cores, width, name
        self.window = window
        self.proc, self.start, self.tries = None, None, 0

    def launch(self):
        self.tries += 1
        self.start = time.time()
        log = open(os.path.join(LOG_DIR, f"{self.name}.log"), "w")
        self.proc = subprocess.Popen(
            [sys.executable, PIPELINE, "--benchmark", self.bench,
             "--cores", str(self.cores), "--width", str(self.width), "--name", self.name]
            + [x for k in WINDOW_COLS for x in (f"--{k}", self.window[k])],
            stdout=log, stderr=subprocess.STDOUT)

    def in_roi(self):
        """gem5가 부팅과 warm-up을 마치고 ROI에 들어갔는가."""
        try:
            with open(os.path.join(OUT, self.name, "gem5", "gem5.log"), errors="replace") as f:
                return "[ROI] begin" in f.read()
        except OSError:
            return False

    def elapsed_min(self):
        return (time.time() - self.start) / 60


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cases", nargs="+",
                    help="<benchmark>:<코어 수>:<width>[:<ROB>:<IQ>:<LQ>:<SQ>]")
    ap.add_argument("-n", "--target", type=int, default=5,
                    help="구성마다 채울 총 실행 횟수 (기본 %(default)s). 이미 있는 실행도 센다")
    ap.add_argument("-j", "--jobs", type=int, default=4,
                    help="동시에 돌릴 개수 (기본 %(default)s). 호스트 부하가 KVM 구간에 "
                         "영향을 주므로 반복 실행끼리는 같은 값으로 맞춘다")
    ap.add_argument("--boot-timeout-min", type=float, default=10,
                    help="이 시간 안에 ROI에 들어가지 못하면 멈춘 것으로 보고 다시 실행 "
                         "(기본 %(default)s분)")
    ap.add_argument("--stagger-sec", type=float, default=20,
                    help="실행을 시작하는 간격 (기본 %(default)s초). 동시에 부팅하지 않게 한다")
    ap.add_argument("--dry-run", action="store_true", help="무엇을 돌릴지만 출력")
    args = ap.parse_args()

    # 구성별로 모자란 횟수만큼 작업을 만든다. 폴더 이름은 겹치지 않는 번호를 쓴다.
    per_case = []
    for c in args.cases:
        try:
            bench, cores, width, *win = c.split(":")
            cores, width = int(cores), int(width)
            if len(win) not in (0, len(WINDOW_COLS)):
                raise ValueError
            window = {k: str(int(v)) for k, v in zip(WINDOW_COLS, win)} or \
                     {k: DEFAULTS[k] for k in WINDOW_COLS}
        except ValueError:
            sys.exit(f"testcase 형식이 잘못됨: {c} "
                     "(예: blackscholes:4:4 또는 canneal:1:8:256:128:64:64)")
        # 윈도우 크기가 기본값과 다르면 폴더 이름과 출력에 표시한다.
        tag = "".join(f"_{k}{window[k]}" for k in WINDOW_COLS if window[k] != DEFAULTS[k])
        have = count_runs(bench, cores, width, window)
        need = max(0, args.target - have)
        jobs, k = [], 1
        while len(jobs) < need:
            name = f"{bench}_c{cores}_w{width}{tag}_r{k}"
            if not os.path.exists(os.path.join(OUT, name)):
                jobs.append(Job(bench, cores, width, window, name))
            k += 1
        per_case.append(jobs)
        print(f"  {bench:14s} {cores}코어 {width}-wide{tag.replace('_', ' ')}: "
              f"기록 {have}회, 추가 {need}회")
    # 구성을 번갈아 가며 돌린다 (한 구성의 반복이 같은 시간대에 몰리지 않게).
    queue = [j for group in zip(*[g + [None] * (max(map(len, per_case)) - len(g))
                                  for g in per_case]) for j in group if j] if per_case else []
    print(f"추가 실행 {len(queue)}개, 동시 실행 {args.jobs}개")
    if args.dry_run or not queue:
        return

    os.makedirs(LOG_DIR, exist_ok=True)
    running, done, failed, last_launch, t0 = [], [], [], 0.0, time.time()
    while queue or running:
        if queue and len(running) < args.jobs and time.time() - last_launch >= args.stagger_sec:
            job = queue.pop(0)
            job.launch()
            running.append(job)
            last_launch = time.time()
        for job in list(running):
            rc = job.proc.poll()
            if rc is None:
                if job.elapsed_min() > args.boot_timeout_min and not job.in_roi():
                    print(f"  [멈춤] {job.name}: {args.boot_timeout_min:g}분 안에 ROI에 "
                          f"들어가지 못함. 끊고 {'다시 실행' if job.tries < 3 else '포기'}",
                          flush=True)
                    job.proc.kill()
                    kill_container(job.name)
                    job.proc.wait()
                    running.remove(job)
                    (queue if job.tries < 3 else failed).append(job)
                continue
            running.remove(job)
            if rc == 0:
                done.append(job)
                print(f"  [완료 {len(done)}/{len(done) + len(queue) + len(running)}] "
                      f"{job.name} ({job.elapsed_min():.1f}분)", flush=True)
            else:
                failed.append(job)
                print(f"  [실패] {job.name} (exit {rc}). "
                      f"로그: out/repeats/{job.name}.log", flush=True)
        time.sleep(2)

    print(f"\n끝: 완료 {len(done)}, 실패 {len(failed)}, {(time.time() - t0) / 60:.1f}분")
    if failed:
        print("실패한 실행:", ", ".join(j.name for j in failed))
    print("요약: python3 problem1/scripts/results_log.py summary")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
