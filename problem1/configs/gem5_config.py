"""
PARSEC 3.0 full-system run on X86 for the gem5 -> McPAT -> HotSpot flow.

Flow:
  1. Boot Linux and run everything before the ROI on KVM cores (fast).
     The benchmark is first run --warmup-runs times entirely on KVM so that
     its code/data pages are in memory; only the last run is measured.
  2. At the PARSEC ROI begin hook (m5 work_begin -> WORKBEGIN),
     switch to O3 cores and reset stats.
  3. At the ROI end hook (WORKEND), dump stats and stop.

So stats.txt contains the ROI only.

Example:
  gem5.opt -d out/bs_1c gem5_config.py --benchmark blackscholes --cores 1
"""

import argparse
import time

import m5
from m5.objects import IQUnit

from gem5.components.boards.x86_board import X86Board
from gem5.components.cachehierarchies.classic.private_l1_shared_l2_walk_cache_hierarchy import (
    PrivateL1SharedL2WalkCacheHierarchy,
)
from gem5.components.memory import DualChannelDDR4_2400
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_switchable_processor import (
    SimpleSwitchableProcessor,
)
from gem5.isas import ISA
from gem5.resources.resource import DiskImageResource, KernelResource
from gem5.simulate.exit_event import ExitEvent
from gem5.simulate.simulator import Simulator
from gem5.utils.requires import requires

requires(isa_required=ISA.X86, kvm_required=True)

p = argparse.ArgumentParser()
p.add_argument(
    "--benchmark",
    required=True,
    choices=["blackscholes", "canneal", "freqmine", "streamcluster"],
)
p.add_argument("--size", default="simsmall")
p.add_argument("--cores", type=int, default=1)
# Superscalar width: fetch/decode/rename/dispatch/issue/wb/commit together.
p.add_argument("--width", type=int, default=4)
p.add_argument("--rob", type=int, default=128)
p.add_argument("--iq", type=int, default=64)
p.add_argument("--lq", type=int, default=32)
p.add_argument("--sq", type=int, default=32)
p.add_argument("--clock", default="3GHz")
p.add_argument("--l1i", default="32KiB")
p.add_argument("--l1d", default="32KiB")
p.add_argument("--l2", default="2MiB")
# 디버그용: ROI 동안 이 간격(마이크로초)마다 통계를 따로 기록한다. 0이면 끔.
# 켜면 stats.txt에 블록이 여러 개 생기므로 파이프라인 결과용으로는 쓰지 않는다.
p.add_argument("--stat-period-us", type=int, default=0)
# 디버그용: ROI 시작부터 켤 gem5 debug flag (쉼표로 구분), ROI 시작 후 멈출 시간(ms)
p.add_argument("--roi-debug-flags", default="")
p.add_argument("--roi-max-ms", type=float, default=0)
# 측정 전에 KVM으로 같은 프로그램을 미리 실행하는 횟수.
# 필요한 코드/데이터 페이지를 메모리에 올려, ROI 안에서 디스크 I/O를 기다리지 않게 한다.
p.add_argument("--warmup-runs", type=int, default=1)
p.add_argument("--kernel", required=True)
p.add_argument("--disk", required=True)
args = p.parse_args()

cache_hierarchy = PrivateL1SharedL2WalkCacheHierarchy(
    l1i_size=args.l1i, l1d_size=args.l1d, l2_size=args.l2
)
memory = DualChannelDDR4_2400(size="3GiB")

processor = SimpleSwitchableProcessor(
    starting_core_type=CPUTypes.KVM,
    switch_core_type=CPUTypes.O3,
    isa=ISA.X86,
    num_cores=args.cores,
)

# KVM cores: perf counters are often unavailable inside Docker.
for core in processor._switchable_cores["start"]:
    core.get_simobject().usePerf = False

# O3 cores: apply the pipeline configuration.
w = args.width
for core in processor._switchable_cores["switch"]:
    cpu = core.get_simobject()
    cpu.fetchWidth = w
    cpu.decodeWidth = w
    cpu.renameWidth = w
    cpu.dispatchWidth = w
    cpu.issueWidth = w
    cpu.wbWidth = w
    cpu.commitWidth = w
    cpu.squashWidth = w
    cpu.numROBEntries = args.rob
    cpu.instQueues = [IQUnit(numEntries=args.iq)]
    cpu.LQEntries = args.lq
    cpu.SQEntries = args.sq

board = X86Board(
    clk_freq=args.clock,
    processor=processor,
    memory=memory,
    cache_hierarchy=cache_hierarchy,
)

run_cmd = (
    f"parsecmgmt -a run -p {args.benchmark} -c gcc-hooks "
    f"-i {args.size} -n {args.cores};"
)
command = (
    "cd /home/gem5/parsec-benchmark;"
    "source env.sh;"
    + run_cmd * (args.warmup_runs + 1)
    + "sleep 5;"
    "m5 exit;"
)

board.set_kernel_disk_workload(
    kernel=KernelResource(local_path=args.kernel),
    disk_image=DiskImageResource(local_path=args.disk, root_partition="1"),
    readfile_contents=command,
)

roi = {}


def on_workbegin():
    for i in range(args.warmup_runs):
        print(f"[WARMUP] run {i + 1} ROI begin (KVM, not measured)")
        yield False
    print(f"[ROI] begin at tick {m5.curTick()}: switch KVM -> O3, reset stats")
    processor.switch()
    m5.stats.reset()
    if args.stat_period_us > 0:
        m5.stats.periodicStatDump(args.stat_period_us * 1_000_000)  # 1 us = 1e6 ticks
    for flag in filter(None, args.roi_debug_flags.split(",")):
        m5.debug.flags[flag].enable()
    if args.roi_max_ms > 0:
        m5.scheduleTickExitFromCurrent(int(args.roi_max_ms * 1e9))
    roi["begin"] = m5.curTick()
    roi["wall"] = time.time()
    yield False


def on_workend():
    for i in range(args.warmup_runs):
        print(f"[WARMUP] run {i + 1} ROI end")
        yield False
    roi["end"] = m5.curTick()
    print(f"[ROI] end at tick {roi['end']}: dump stats")
    m5.stats.dump()
    yield True


simulator = Simulator(
    board=board,
    on_exit_event={
        ExitEvent.WORKBEGIN: on_workbegin(),
        ExitEvent.WORKEND: on_workend(),
    },
)

t0 = time.time()
simulator.run()

print(f"[DONE] exit cause: {simulator.get_last_exit_event_cause()}")
if "end" in roi:
    print(f"[DONE] ROI simulated time: {(roi['end'] - roi['begin']) / 1e12:.6f} s")
    print(f"[DONE] ROI wallclock: {time.time() - roi['wall']:.1f} s")
print(f"[DONE] total wallclock: {time.time() - t0:.1f} s")
