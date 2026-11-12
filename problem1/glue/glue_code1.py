#!/usr/bin/env python3
"""
glue code 1: gem5 결과 -> McPAT 입력 XML

입력:
  - config.json : gem5가 기록한 시스템 구조 (코어 수, width, ROB, 캐시 크기 ...)
  - stats.txt   : gem5가 기록한 활동량 (명령어 수, 캐시 접근 횟수 ...), ROI 구간
  - 템플릿 XML  : gem5가 알려주지 않는 값(공정, 소자 종류 등)이 들어 있는 McPAT 입력 틀
출력:
  - McPAT 입력 XML

동작:
  템플릿의 core0을 코어 수만큼 복제(core0, core1, ...)하고,
  각 코어/캐시에 해당하는 gem5 값을 채운다. (homogeneous_cores = 0)

사용법:
  glue_code1.py --stats stats.txt --config config.json \
                --template mcpat_template.xml --out mcpat_in.xml [--dcache-ports 2]
"""

import argparse
import copy
import json
import re
import sys
import xml.etree.ElementTree as ET


DEFAULT_DCACHE_PORTS = 2  # --dcache-ports를 주지 않았을 때 쓰는 값

# ----------------------------------------------------------------------------
# gem5 출력 읽기
# ----------------------------------------------------------------------------
def read_stats(path):
    """stats.txt의 마지막 통계 블록을 {이름: 값}으로 읽는다."""
    blocks, cur = [], None
    with open(path) as f:
        for line in f:
            if "Begin Simulation Statistics" in line:
                cur = {}
            elif "End Simulation Statistics" in line:
                if cur is not None:
                    blocks.append(cur)
                cur = None
            elif cur is not None:
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        cur[parts[0]] = float(parts[1])
                    except ValueError:
                        pass
    if not blocks:
        sys.exit(f"[glue1] {path}: 통계 블록이 없습니다.")
    return blocks[-1]


def as_list(x):
    return x if isinstance(x, list) else [x]


class Stats:
    """접두사를 붙여서 통계를 꺼내는 도우미. 없는 통계는 0으로 본다."""

    def __init__(self, stats, prefix=""):
        self.s, self.p, self.missing = stats, prefix, set()

    def get(self, *names):
        """여러 이름을 주면 합을 돌려준다."""
        total = 0.0
        for n in names:
            key = self.p + n
            if key in self.s:
                total += self.s[key]
            else:
                self.missing.add(key)
        return total

    def has(self, name):
        return self.p + name in self.s


# ----------------------------------------------------------------------------
# McPAT XML 쓰기 도우미
# ----------------------------------------------------------------------------
def find_comp(parent, comp_id):
    for c in parent.iter("component"):
        if c.get("id") == comp_id:
            return c
    sys.exit(f"[glue1] 템플릿에 component '{comp_id}'가 없습니다.")


def set_val(comp, kind, name, value):
    """comp 바로 아래의 <param>/<stat> 값을 바꾼다. kind: 'param' | 'stat'"""
    for e in comp.findall(kind):
        if e.get("name") == name:
            if isinstance(value, float):
                value = int(round(value)) if value == int(value) or value > 10 else value
            e.set("value", str(value))
            return
    sys.exit(f"[glue1] 템플릿 {comp.get('id')}에 {kind} '{name}'가 없습니다.")


def get_val(comp, kind, name):
    for e in comp.findall(kind):
        if e.get("name") == name:
            return e.get("value")
    sys.exit(f"[glue1] 템플릿 {comp.get('id')}에 {kind} '{name}'가 없습니다.")


def set_cache_cfg(comp, name, size, block, assoc):
    """캐시 설정 문자열 'size,block,assoc,...'의 앞 3개만 바꾸고 나머지는 템플릿 값 유지."""
    old = [x.strip() for x in get_val(comp, "param", name).split(",")]
    old[0:3] = [str(size), str(block), str(assoc)]
    set_val(comp, "param", name, ",".join(old))


def rename_ids(comp, old, new):
    """component id의 'system.core0' -> 'system.coreN' 일괄 변경."""
    for c in comp.iter("component"):
        cid = c.get("id")
        if cid.startswith(old):
            c.set("id", new + cid[len(old):])
    comp.set("name", new.split(".")[-1])


# ----------------------------------------------------------------------------
# 코어 하나 채우기
# ----------------------------------------------------------------------------
def fu_count(fu_list, op_class):
    """해당 연산을 처리할 수 있는 functional unit 개수."""
    return sum(
        fu["count"]
        for fu in fu_list
        if any(op["opClass"] == op_class for op in fu["opList"])
    )


def fill_core(comp, cfg, st, ic_cfg, dc_cfg, ic, dc, clock_mhz, block, dcache_ports):
    """
    comp : McPAT core component
    cfg  : config.json의 O3 CPU 설정
    st   : 이 코어의 Stats, ic/dc : 이 코어의 L1I/L1D Stats
    """
    iq = as_list(cfg["instQueues"])[0]
    fu_list = iq["fuPool"]["FUList"]
    width = cfg["issueWidth"]

    # ---- 구조 (param) ----
    P = lambda n, v: set_val(comp, "param", n, v)
    P("clock_rate", clock_mhz)
    P("x86", 1)
    P("machine_type", 0)  # 0 = out-of-order
    P("number_hardware_threads", cfg.get("numThreads", 1))
    P("fetch_width", cfg["fetchWidth"])
    P("decode_width", cfg["decodeWidth"])
    P("issue_width", width)
    P("peak_issue_width", width)
    P("commit_width", cfg["commitWidth"])
    # FP 연산을 실행하는 유닛: FP 덧셈 유닛 + FP 곱셈/나눗셈 유닛
    n_fpu = fu_count(fu_list, "FloatAdd") + fu_count(fu_list, "FloatMult")
    P("fp_issue_width", max(1, min(width, n_fpu)))
    P("ALU_per_core", max(1, fu_count(fu_list, "IntAlu")))
    P("MUL_per_core", max(1, fu_count(fu_list, "IntMult")))
    P("FPU_per_core", max(1, n_fpu))
    # gem5는 정수/FP가 IQ 하나를 같이 쓰지만 McPAT은 둘로 나눠 모델링한다.
    # 전체 엔트리 수가 같도록 절반씩 나눈다.
    P("instruction_window_size", max(1, iq["numEntries"] // 2))
    P("fp_instruction_window_size", max(1, iq["numEntries"] // 2))
    P("ROB_size", cfg["numROBEntries"])
    P("phy_Regs_IRF_size", cfg["numPhysIntRegs"])
    P("phy_Regs_FRF_size", cfg["numPhysFloatRegs"])
    P("store_buffer_size", cfg["SQEntries"])
    P("load_buffer_size", cfg["LQEntries"])
    # McPAT은 memory_ports만큼 데이터 캐시에 읽기/쓰기 포트(물리적 통로)를 단다.
    # gem5의 메모리 유닛 수(한 사이클에 실행 가능한 load/store 수)와는 뜻이 달라서
    # 자동으로 정하지 않고 사용자가 --dcache-ports로 준 값을 그대로 쓴다.
    # (참고: 32KB 캐시 면적은 포트 2개일 때 3.3mm^2, 4개일 때 11.8mm^2)
    P("memory_ports", dcache_ports)
    bp = cfg["branchPred"]
    P("RAS_size", bp["ras"]["numEntries"])

    # ---- 활동량 (stat) ----
    S = lambda n, v: set_val(comp, "stat", n, v)
    g = st.get
    busy = g("numCycles")
    idle = g("quiesceCycles")
    total = busy + idle

    # 파이프라인을 지나간 명령어(µop) 수. 잘못 예측한 경로의 명령어도 포함.
    total_insts = g("decode.decodedInsts")
    c_ops = g("commitStats0.numOps")
    c_fp = g("commitStats0.numFpInsts", "commitStats0.numVecInsts")
    fp_frac = min(1.0, c_fp / c_ops) if c_ops else 0.0
    S("total_instructions", total_insts)
    S("fp_instructions", total_insts * fp_frac)
    S("int_instructions", total_insts * (1 - fp_frac))
    S("committed_instructions", c_ops)
    S("committed_fp_instructions", c_ops * fp_frac)
    S("committed_int_instructions", c_ops * (1 - fp_frac))
    S("branch_instructions", g("branchPred.lookups_0::total"))
    S("branch_mispredictions", g("branchPred.mispredicted_0::total"))
    S("load_instructions", g("executeStats0.numLoadInsts"))
    S("store_instructions", g("executeStats0.numStoreInsts"))

    S("total_cycles", total)
    S("idle_cycles", idle)
    S("busy_cycles", busy)
    # 동작 중인 사이클 가운데 파이프라인이 실제로 채워진 비율
    ipc = c_ops / busy if busy else 0.0
    S("pipeline_duty_cycle", round(min(1.0, ipc / width), 4))

    S("ROB_reads", g("rob.reads"))
    S("ROB_writes", g("rob.writes"))

    int_lk = g("rename.intLookups")
    fp_lk = g("rename.fpLookups", "rename.vecLookups")
    renamed = g("rename.renamedOperands")
    lk = int_lk + fp_lk
    S("rename_reads", int_lk)
    S("fp_rename_reads", fp_lk)
    S("rename_writes", renamed * int_lk / lk if lk else 0)
    S("fp_rename_writes", renamed * fp_lk / lk if lk else 0)

    S("inst_window_reads", g("intInstQueueReads"))
    S("inst_window_writes", g("intInstQueueWrites"))
    S("inst_window_wakeup_accesses", g("intInstQueueWakeupAccesses"))
    S("fp_inst_window_reads", g("fpInstQueueReads", "vecInstQueueReads"))
    S("fp_inst_window_writes", g("fpInstQueueWrites", "vecInstQueueWrites"))
    S(
        "fp_inst_window_wakeup_accesses",
        g("fpInstQueueWakeupAccesses", "vecInstQueueWakeupAccesses"),
    )

    S("int_regfile_reads", g("executeStats0.numIntRegReads"))
    S("int_regfile_writes", g("executeStats0.numIntRegWrites"))
    S("float_regfile_reads", g("executeStats0.numFpRegReads"))
    S("float_regfile_writes", g("executeStats0.numFpRegWrites"))

    S("function_calls", g("commitStats0.functionCalls"))
    S("context_switches", 0)

    ialu = g("intAluAccesses")
    fpu = g("fpAluAccesses", "vecAluAccesses")
    mul = g("issuedInstType_0::IntMult", "issuedInstType_0::IntDiv")
    S("ialu_accesses", ialu)
    S("fpu_accesses", fpu)
    S("mul_accesses", mul)
    S("cdb_alu_accesses", ialu)
    S("cdb_fpu_accesses", fpu)
    S("cdb_mul_accesses", mul)

    # ---- 분기 예측기 / BTB ----
    btb = find_comp(comp, comp.get("id") + ".BTB")
    old = [x.strip() for x in get_val(btb, "param", "BTB_config").split(",")]
    old[0] = str(bp["btb"]["numEntries"])
    set_val(btb, "param", "BTB_config", ",".join(old))
    set_val(btb, "stat", "read_accesses", g("branchPred.BTBLookups"))
    set_val(btb, "stat", "write_accesses", g("branchPred.BTBUpdates"))

    cbp = bp.get("conditionalBranchPred", {})
    if cbp.get("type") == "TournamentBP":
        pred = find_comp(comp, comp.get("id") + ".predictor")
        set_val(pred, "param", "local_predictor_entries", cbp["localPredictorSize"])
        set_val(pred, "param", "global_predictor_entries", cbp["globalPredictorSize"])
        set_val(pred, "param", "global_predictor_bits", cbp["globalCtrBits"])
        set_val(pred, "param", "chooser_predictor_entries", cbp["choicePredictorSize"])
        set_val(pred, "param", "chooser_predictor_bits", cbp["choiceCtrBits"])

    # ---- TLB ----
    itlb = find_comp(comp, comp.get("id") + ".itlb")
    dtlb = find_comp(comp, comp.get("id") + ".dtlb")
    set_val(itlb, "param", "number_entries", cfg["mmu"]["itb"]["size"])
    set_val(dtlb, "param", "number_entries", cfg["mmu"]["dtb"]["size"])
    set_val(itlb, "stat", "total_accesses",
            g("mmu.itb.rdAccesses", "mmu.itb.wrAccesses", "mmu.itb.exAccesses"))
    set_val(itlb, "stat", "total_misses",
            g("mmu.itb.rdMisses", "mmu.itb.wrMisses", "mmu.itb.exMisses"))
    set_val(dtlb, "stat", "total_accesses",
            g("mmu.dtb.rdAccesses", "mmu.dtb.wrAccesses", "mmu.dtb.exAccesses"))
    set_val(dtlb, "stat", "total_misses",
            g("mmu.dtb.rdMisses", "mmu.dtb.wrMisses", "mmu.dtb.exMisses"))

    # ---- L1 캐시 ----
    icache = find_comp(comp, comp.get("id") + ".icache")
    set_cache_cfg(icache, "icache_config", ic_cfg["size"], block, ic_cfg["assoc"])
    set_val(icache, "stat", "read_accesses", ic.get("overallAccesses::total"))
    set_val(icache, "stat", "read_misses", ic.get("overallMisses::total"))

    dcache = find_comp(comp, comp.get("id") + ".dcache")
    set_cache_cfg(dcache, "dcache_config", dc_cfg["size"], block, dc_cfg["assoc"])
    wr_acc = dc.get("WriteReq.accesses::total", "LockedRMWWriteReq.accesses::total")
    wr_miss = dc.get("WriteReq.misses::total", "LockedRMWWriteReq.misses::total")
    set_val(dcache, "stat", "write_accesses", wr_acc)
    set_val(dcache, "stat", "write_misses", wr_miss)
    set_val(dcache, "stat", "read_accesses", dc.get("overallAccesses::total") - wr_acc)
    set_val(dcache, "stat", "read_misses", dc.get("overallMisses::total") - wr_miss)

    return total, idle, busy


# ----------------------------------------------------------------------------
# 전체 변환
# ----------------------------------------------------------------------------
def indexed_prefixes(stats, pattern):
    """
    'board.processor.switch{i}.core.' 같은 접두사를 인덱스 순서로 찾는다.
    gem5는 객체가 1개면 인덱스를 붙이지 않고, 여러 개면 0, 1, 2...를 붙인다.
    pattern: 인덱스 자리에 (\\d*)를 넣은 정규식, 접두사 전체를 그룹 0으로 매칭.
    """
    found = {}
    rx = re.compile(pattern)
    for k in stats:
        m = rx.match(k)
        if m:
            found[int(m.group(1) or 0)] = m.group(0)
    return [found[i] for i in sorted(found)]


def convert(stats_path, config_path, template_path, out_path,
            dcache_ports=DEFAULT_DCACHE_PORTS, verbose=True):
    stats = read_stats(stats_path)
    with open(config_path) as f:
        board = json.load(f)["board"]

    tree = ET.parse(template_path)
    root = tree.getroot()
    system = find_comp(root, "system")

    core_cfgs = [c["core"] for c in as_list(board["processor"]["switch"])]
    n = len(core_cfgs)
    ch = board["cache_hierarchy"]
    ic_cfgs, dc_cfgs = as_list(ch["l1icaches"]), as_list(ch["l1dcaches"])
    l2_cfg = ch["l2cache"]
    block = board["cache_line_size"]
    clock_mhz = int(round(1e6 / board["clk_domain"]["clock"][0]))  # tick = 1 ps

    core_pre = indexed_prefixes(stats, r"board\.processor\.switch(\d*)\.core\.")
    ic_pre = indexed_prefixes(stats, r"board\.cache_hierarchy\.l1icaches(\d*)\.")
    dc_pre = indexed_prefixes(stats, r"board\.cache_hierarchy\.l1dcaches(\d*)\.")
    if not (len(core_pre) == len(ic_pre) == len(dc_pre) == n):
        sys.exit(
            f"[glue1] 코어 수 불일치: config {n}, stats 코어 {len(core_pre)}, "
            f"L1I {len(ic_pre)}, L1D {len(dc_pre)}"
        )

    # ---- 코어: 템플릿 core0을 n개로 복제 ----
    template_core = find_comp(system, "system.core0")
    pos = list(system).index(template_core)
    system.remove(template_core)
    all_missing = set()
    max_total = 0
    for i in range(n):
        core = copy.deepcopy(template_core)
        rename_ids(core, "system.core0", f"system.core{i}")
        st, ic, dc = Stats(stats, core_pre[i]), Stats(stats, ic_pre[i]), Stats(stats, dc_pre[i])
        total, idle, busy = fill_core(
            core, core_cfgs[i], st, ic_cfgs[i], dc_cfgs[i], ic, dc, clock_mhz, block,
            dcache_ports
        )
        max_total = max(max_total, total)
        system.insert(pos + i, core)
        all_missing |= st.missing | ic.missing | dc.missing

    # ---- 시스템 전체 ----
    set_val(system, "param", "number_of_cores", n)
    set_val(system, "param", "homogeneous_cores", 0)
    set_val(system, "param", "number_of_L2s", 1)
    set_val(system, "param", "Private_L2", 0)  # 모든 코어가 L2 하나를 공유
    set_val(system, "param", "number_of_L3s", 0)
    set_val(system, "param", "number_cache_levels", 2)
    set_val(system, "param", "target_core_clockrate", clock_mhz)
    set_val(system, "stat", "total_cycles", max_total)
    set_val(system, "stat", "idle_cycles", 0)
    set_val(system, "stat", "busy_cycles", max_total)

    # ---- 공유 L2 ----
    l2s = Stats(stats, "board.cache_hierarchy.l2cache.")
    l2 = find_comp(system, "system.L20")
    set_cache_cfg(l2, "L2_config", l2_cfg["size"], block, l2_cfg["assoc"])
    set_val(l2, "param", "clockrate", clock_mhz)
    l2_wr = 0.0
    for req in ("WritebackDirty", "WritebackClean"):
        if l2s.has(f"{req}.accesses::total"):
            l2_wr += l2s.get(f"{req}.accesses::total")
    set_val(l2, "stat", "write_accesses", l2_wr)
    set_val(l2, "stat", "write_misses", 0)
    set_val(l2, "stat", "read_accesses", l2s.get("overallAccesses::total"))
    set_val(l2, "stat", "read_misses", l2s.get("overallMisses::total"))
    set_val(l2, "stat", "duty_cycle", 1.0)

    # ---- L1-L2 사이 버스 ----
    noc = find_comp(system, "system.NoC0")
    set_val(noc, "param", "clockrate", clock_mhz)
    bus = Stats(stats, "board.cache_hierarchy.l2bus.")
    set_val(noc, "stat", "total_accesses", bus.get("pktCount::total"))

    # ---- 메모리 컨트롤러 ----
    mc = find_comp(system, "system.mc")
    rd = sum(v for k, v in stats.items()
             if re.fullmatch(r"board\.memory\.mem_ctrl\d*\.readReqs", k))
    wr = sum(v for k, v in stats.items()
             if re.fullmatch(r"board\.memory\.mem_ctrl\d*\.writeReqs", k))
    set_val(mc, "stat", "memory_reads", rd)
    set_val(mc, "stat", "memory_writes", wr)
    set_val(mc, "stat", "memory_accesses", rd + wr)

    all_missing |= l2s.missing | bus.missing
    tree.write(out_path, encoding="utf-8", xml_declaration=True)

    if verbose:
        print(f"[glue1] 코어 {n}개, {clock_mhz} MHz, "
              f"width {core_cfgs[0]['issueWidth']}, ROB {core_cfgs[0]['numROBEntries']}, "
              f"L2 {l2_cfg['size'] // 1024} KiB")
        print(f"[glue1] 전체 사이클 {int(max_total)} -> {out_path}")
        if all_missing:
            print(f"[glue1] 경고: stats.txt에 없어 0으로 처리한 통계 {len(all_missing)}개:")
            for k in sorted(all_missing)[:10]:
                print(f"         {k}")
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stats", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--template", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dcache-ports", type=int, default=DEFAULT_DCACHE_PORTS,
                    help="McPAT에 넣을 데이터 캐시 포트 수 (기본 %(default)s)")
    a = ap.parse_args()
    if a.dcache_ports < 1:
        ap.error("--dcache-ports는 1 이상이어야 합니다.")
    convert(a.stats, a.config, a.template, a.out, dcache_ports=a.dcache_ports)
