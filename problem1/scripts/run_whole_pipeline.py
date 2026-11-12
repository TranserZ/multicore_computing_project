#!/usr/bin/env python3
"""
전체 파이프라인 실행: gem5 -> glue 1 -> McPAT -> glue 2 -> HotSpot

한 가지 구성(프로그램, 코어 수, width ...)에 대해 성능, 전력, 온도를 구한다.

  1. gem5     : PARSEC 프로그램의 ROI를 O3 CPU로 시뮬레이션  -> stats.txt, config.json
  2. glue 1   : gem5 결과 -> McPAT 입력 XML
  3. McPAT    : 전력, 면적 계산
  4. glue 2   : McPAT 결과 -> HotSpot 입력 (.flp, .ptrace)
  5. HotSpot  : steady-state 온도 계산
  6. 요약     : result.json

출력 위치: out/<이름>/
  gem5/     gem5 출력 (stats.txt, config.json, gem5.log ...)
  mcpat/    mcpat_in.xml, mcpat_out.txt
  hotspot/  chip.flp, chip.ptrace, chip.steady, power.json
  result.json

사용 예:
  run_whole_pipeline.py --benchmark blackscholes --cores 4 --width 4
  run_whole_pipeline.py --benchmark blackscholes --cores 4 --width 4 --skip-gem5
      (이미 있는 gem5 결과로 McPAT, HotSpot만 다시 실행)

gem5, McPAT, HotSpot은 모두 setup.sh가 준비한 Docker 이미지 안에서 실행한다.
"""

import argparse
import json
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
sys.path.insert(0, os.path.join(PROJ_DIR, "problem1", "glue"))

import glue_code1  # noqa: E402
import glue_code2  # noqa: E402

DOCKER_IMAGE = os.environ.get(
    "GEM5_IMAGE", "ghcr.io/gem5/ubuntu-24.04_all-dependencies:v25-1"
)
# 컨테이너 안에서는 프로젝트 폴더가 /proj로 보인다.
C_PROJ = "/proj"
GEM5_BIN = f"{C_PROJ}/tools/gem5/build/X86/gem5.opt"
GEM5_CONFIG = f"{C_PROJ}/problem1/configs/gem5_config.py"
KERNEL = f"{C_PROJ}/tools/resources/vmlinux-4.19.83"
DISK = f"{C_PROJ}/tools/resources/parsec.img"
MCPAT_BIN = f"{C_PROJ}/tools/mcpat/mcpat"
HOTSPOT_BIN = f"{C_PROJ}/tools/hotspot/hotspot"
MCPAT_TEMPLATE = os.path.join(PROJ_DIR, "problem1", "configs", "mcpat_template.xml")
HOTSPOT_CONFIG = os.path.join(PROJ_DIR, "problem1", "configs", "hotspot.config")


def in_container(path):
    """호스트 경로 -> 컨테이너 안 경로"""
    rel = os.path.relpath(os.path.abspath(path), PROJ_DIR)
    if rel.startswith(".."):
        sys.exit(f"[pipeline] 프로젝트 폴더 밖의 경로는 쓸 수 없습니다: {path}")
    return f"{C_PROJ}/{rel}"


def docker_run(cmd, kvm=False, stdout=None, stderr=None):
    """Docker 이미지 안에서 명령을 실행한다 (현재 사용자 권한으로)."""
    base = ["docker", "run", "--rm",
            "-u", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
            "-v", f"{PROJ_DIR}:{C_PROJ}", "-w", C_PROJ]
    if kvm:
        base += ["--device", "/dev/kvm",
                 "--group-add", str(os.stat("/dev/kvm").st_gid)]
    return subprocess.run(base + [DOCKER_IMAGE] + cmd, stdout=stdout, stderr=stderr)


def step(n, msg):
    print(f"\n[pipeline] ({n}/6) {msg}", flush=True)


# ----------------------------------------------------------------------------
def run_gem5(args, gem5_dir):
    os.makedirs(gem5_dir, exist_ok=True)
    cmd = [GEM5_BIN, "-d", in_container(gem5_dir), GEM5_CONFIG,
           "--benchmark", args.benchmark, "--size", args.size,
           "--cores", str(args.cores), "--width", str(args.width),
           "--rob", str(args.rob), "--iq", str(args.iq),
           "--lq", str(args.lq), "--sq", str(args.sq),
           "--l2", args.l2, "--clock", args.clock,
           "--warmup-runs", str(args.warmup_runs),
           "--kernel", KERNEL, "--disk", DISK]
    log = os.path.join(gem5_dir, "gem5.log")
    print(f"[pipeline] 로그: {os.path.relpath(log, PROJ_DIR)} "
          "(오래 걸립니다. 진행 상황은 이 파일에서 '[ROI]'를 찾아보세요)")
    with open(log, "w") as f:
        r = docker_run(cmd, kvm=True, stdout=f, stderr=subprocess.STDOUT)
    text = open(log, errors="replace").read()
    if r.returncode != 0 or "[ROI] end" not in text:
        sys.exit(f"[pipeline] gem5 실패 (exit {r.returncode}). {log}를 확인하세요.")


def run_mcpat(xml_path, out_path):
    with open(out_path, "w") as f:
        r = docker_run([MCPAT_BIN, "-infile", in_container(xml_path),
                        "-print_level", "5"], stdout=f, stderr=subprocess.STDOUT)
    if r.returncode != 0 or "Processor:" not in open(out_path).read():
        sys.exit(f"[pipeline] McPAT 실패. {out_path}를 확인하세요.")


def run_hotspot(hs_dir):
    c = in_container(hs_dir)
    log = os.path.join(hs_dir, "hotspot.log")
    with open(log, "w") as f:
        r = docker_run([HOTSPOT_BIN,
                        "-c", in_container(HOTSPOT_CONFIG),
                        "-f", f"{c}/chip.flp", "-p", f"{c}/chip.ptrace",
                        "-model_type", "block",
                        "-steady_file", f"{c}/chip.steady",
                        "-o", f"{c}/chip.ttrace"],
                       stdout=f, stderr=subprocess.STDOUT)
    steady = os.path.join(hs_dir, "chip.steady")
    if r.returncode != 0 or not os.path.exists(steady):
        sys.exit(f"[pipeline] HotSpot 실패. {log}를 확인하세요.")
    return steady


def read_steady(steady_path, block_names):
    """HotSpot steady 파일에서 블록 온도만 읽어 섭씨로 돌려준다."""
    temps = {}
    with open(steady_path) as f:
        for line in f:
            p = line.split()
            if len(p) == 2 and p[0] in block_names:
                temps[p[0]] = float(p[1]) - 273.15
    return temps


def performance(stats_path):
    """gem5 stats.txt에서 성능 지표를 읽는다."""
    s = glue_code1.read_stats(stats_path)
    cores = glue_code1.indexed_prefixes(s, r"board\.processor\.switch(\d*)\.core\.")
    insts = sum(s.get(p + "commitStats0.numInsts", 0) for p in cores)
    cycles = sum(s.get(p + "numCycles", 0) for p in cores)
    return {
        "roi_seconds": s["simSeconds"],
        "committed_insts": insts,
        "busy_cycles": cycles,
        "ipc": insts / cycles if cycles else 0.0,  # 동작 사이클 기준, 전 코어 평균
        "host_seconds": s.get("hostSeconds", 0.0),
    }


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--benchmark", required=True,
                    choices=["blackscholes", "canneal", "freqmine", "streamcluster"])
    ap.add_argument("--size", default="simsmall")
    ap.add_argument("--cores", type=int, default=1)
    ap.add_argument("--width", type=int, default=4,
                    help="fetch/decode/rename/dispatch/issue/commit width "
                         "(writeback width는 gem5 기본값 8로 고정)")
    ap.add_argument("--rob", type=int, default=128)
    ap.add_argument("--iq", type=int, default=64)
    ap.add_argument("--lq", type=int, default=32)
    ap.add_argument("--sq", type=int, default=32)
    ap.add_argument("--l2", default="2MiB")
    ap.add_argument("--clock", default="3GHz")
    ap.add_argument("--dcache-ports", type=int, default=glue_code1.DEFAULT_DCACHE_PORTS,
                    help="McPAT에 넣을 데이터 캐시 포트 수 (기본 %(default)s)")
    ap.add_argument("--warmup-runs", type=int, default=1,
                    help="측정 전에 KVM으로 같은 프로그램을 미리 실행하는 횟수. "
                         "ROI 안에서 디스크 I/O를 기다리는 구간을 없앤다")
    ap.add_argument("--name", help="출력 폴더 이름 (기본: <benchmark>_c<cores>_w<width>)")
    ap.add_argument("--outroot", default=os.path.join(PROJ_DIR, "out"))
    ap.add_argument("--skip-gem5", action="store_true",
                    help="gem5를 다시 돌리지 않고 기존 결과를 사용")
    args = ap.parse_args()

    name = args.name or f"{args.benchmark}_c{args.cores}_w{args.width}"
    out = os.path.join(os.path.abspath(args.outroot), name)
    gem5_dir, mcpat_dir, hs_dir = (os.path.join(out, d) for d in ("gem5", "mcpat", "hotspot"))
    for d in (mcpat_dir, hs_dir):
        os.makedirs(d, exist_ok=True)
    stats, config = os.path.join(gem5_dir, "stats.txt"), os.path.join(gem5_dir, "config.json")
    t0 = time.time()
    print(f"[pipeline] 구성: {name}  ->  {os.path.relpath(out, PROJ_DIR)}/")

    step(1, "gem5 시뮬레이션")
    if args.skip_gem5:
        if not (os.path.exists(stats) and os.path.exists(config)):
            sys.exit(f"[pipeline] --skip-gem5: {gem5_dir}에 gem5 결과가 없습니다.")
        print("[pipeline] 건너뜀 (기존 결과 사용)")
    else:
        run_gem5(args, gem5_dir)
    perf = performance(stats)
    print(f"[pipeline] ROI {perf['roi_seconds'] * 1e3:.3f} ms, IPC {perf['ipc']:.3f}, "
          f"gem5 실행 {perf['host_seconds'] / 60:.1f}분")

    step(2, "glue 1: gem5 -> McPAT 입력")
    xml = os.path.join(mcpat_dir, "mcpat_in.xml")
    glue_code1.convert(stats, config, MCPAT_TEMPLATE, xml, dcache_ports=args.dcache_ports)

    step(3, "McPAT")
    mcpat_out = os.path.join(mcpat_dir, "mcpat_out.txt")
    run_mcpat(xml, mcpat_out)

    step(4, "glue 2: McPAT -> HotSpot 입력")
    power = glue_code2.convert(mcpat_out, hs_dir)

    step(5, "HotSpot")
    temps = read_steady(run_hotspot(hs_dir), set(power["blocks"]))
    hottest = max(temps, key=temps.get)
    total_area = sum(b["area_mm2"] for b in power["blocks"].values())
    avg_temp = sum(temps[n] * power["blocks"][n]["area_mm2"] for n in temps) / total_area

    step(6, "요약")
    result = {
        "name": name,
        "config": {k: getattr(args, k) for k in
                   ("benchmark", "size", "cores", "width", "rob", "iq", "lq", "sq", "l2", "clock",
                    "dcache_ports", "warmup_runs")},
        "performance": perf,
        "power": {k: power[k] for k in
                  ("total_power_W", "dynamic_power_W", "leakage_power_W", "area_mm2")},
        "temperature_C": {
            "peak": temps[hottest],
            "peak_block": hottest,
            "average": avg_temp,  # 면적 가중 평균
            "blocks": temps,
        },
    }
    with open(os.path.join(out, "result.json"), "w") as f:
        json.dump(result, f, indent=2)

    print(f"  실행 시간(ROI)   {perf['roi_seconds'] * 1e3:10.3f} ms   (IPC {perf['ipc']:.3f})")
    print(f"  평균 전력        {power['total_power_W']:10.3f} W    "
          f"(dynamic {power['dynamic_power_W']:.3f} + leakage {power['leakage_power_W']:.3f})")
    print(f"  칩 면적          {power['area_mm2']:10.3f} mm^2")
    print(f"  최고 온도        {temps[hottest]:10.2f} C    ({hottest})")
    print(f"  평균 온도        {avg_temp:10.2f} C")
    print(f"[pipeline] 완료 ({(time.time() - t0) / 60:.1f}분) -> "
          f"{os.path.relpath(os.path.join(out, 'result.json'), PROJ_DIR)}")


if __name__ == "__main__":
    main()
