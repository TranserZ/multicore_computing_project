#!/usr/bin/env python3
"""
glue code 2: McPAT 결과 -> HotSpot 입력 (.flp, .ptrace)

입력:
  - McPAT 출력 텍스트 (-print_level 5로 실행한 결과)
출력:
  - chip.flp        : 칩 배치도 (블록 이름, 폭, 높이, x, y / 단위 m)
  - chip.ptrace     : 블록별 평균 전력 (W). 블록 이름은 .flp와 동일
  - power.json      : 칩 전체/블록별 전력과 면적 요약

블록 구성:
  코어마다 3개   core{i}_FE  : Instruction Fetch Unit + Renaming Unit
                 core{i}_EXE : Execution Unit
                 core{i}_MEM : Load Store Unit(L1D 포함) + Memory Management Unit
  코어 바깥      L2          : 공유 L2 캐시 + 버스
                 MC          : 메모리 컨트롤러

블록 전력 = Runtime Dynamic + Subthreshold Leakage + Gate Leakage

배치:
  코어를 격자(1코어 1x1, 4코어 2x2, 8코어 4x2)로 놓고,
  그 아래 한 줄에 L2와 MC를 나란히 놓는다.

사용법:
  glue_code2.py --mcpat mcpat_out.txt --outdir <dir>
"""

import argparse
import json
import math
import os
import re
import sys

METRIC = re.compile(
    r"^\s*(Area|Peak Dynamic|Subthreshold Leakage|Gate Leakage|Runtime Dynamic)"
    r"\s*=\s*([-+0-9.eE]+|nan|inf)\s*(mm\^2|W)\s*$"
)

# 코어 안 McPAT 유닛 -> HotSpot 블록
UNIT_TO_BLOCK = {
    "Instruction Fetch Unit": "FE",
    "Renaming Unit": "FE",
    "Execution Unit": "EXE",
    "Load Store Unit": "MEM",
    "Memory Management Unit": "MEM",
}
CORE_BLOCKS = ["FE", "EXE", "MEM"]  # 코어 안에서 위에서 아래로 쌓는 순서


# ----------------------------------------------------------------------------
# McPAT 출력 읽기
# ----------------------------------------------------------------------------
def parse_mcpat(path):
    """
    McPAT 출력을 [(들여쓰기, 이름, {지표: 값})] 목록으로 읽는다.
    이름 줄 다음에 나오는 지표 줄들은 그 이름에 속한다.
    """
    nodes = []
    with open(path) as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("*"):
                continue
            m = METRIC.match(line)
            if m:
                if nodes:
                    nodes[-1][2].setdefault(m.group(1), float(m.group(2)))
                continue
            if "=" in line:  # 'Device Type= ...' 같은 설명 줄
                continue
            indent = len(line) - len(line.lstrip())
            nodes.append((indent, line.strip().rstrip(":").strip(), {}))
    return nodes


def power_of(m):
    """(dynamic, leakage) [W]"""
    return (
        m.get("Runtime Dynamic", 0.0),
        m.get("Subthreshold Leakage", 0.0) + m.get("Gate Leakage", 0.0),
    )


def extract_blocks(nodes):
    """
    McPAT 노드 목록에서 HotSpot 블록별 {area[mm^2], dynamic[W], leakage[W]}를 만든다.
    반환: (blocks: 순서 있는 dict, n_cores, processor 지표)
    """
    top = [i for i, (ind, _, _) in enumerate(nodes) if ind == 0]
    sections = {}  # 이름 -> [(시작, 끝)]
    for k, i in enumerate(top):
        end = top[k + 1] if k + 1 < len(top) else len(nodes)
        sections.setdefault(nodes[i][1], []).append((i, end))

    if "Processor" not in sections:
        sys.exit("[glue2] McPAT 출력에서 'Processor' 구간을 찾지 못했습니다.")
    proc = nodes[sections["Processor"][0][0]][2]

    n_cores = 1
    for _, name, _ in nodes:
        m = re.match(r"Total Cores:\s*(\d+)\s*cores", name)
        if m:
            n_cores = int(m.group(1))

    core_secs = sections.get("Core", [])
    if not core_secs:
        sys.exit("[glue2] McPAT 출력에서 'Core' 구간을 찾지 못했습니다. "
                 "-print_level 5로 실행했는지 확인하세요.")
    # homogeneous_cores=1이면 Core 구간이 하나만 나온다 -> 모든 코어에 같은 값 사용
    if len(core_secs) == 1 and n_cores > 1:
        core_secs = core_secs * n_cores
    if len(core_secs) != n_cores:
        sys.exit(f"[glue2] 코어 수 불일치: Total Cores {n_cores}, Core 구간 {len(core_secs)}")

    blocks = {}
    for ci, (start, end) in enumerate(core_secs):
        core = nodes[start][2]
        core_dyn, core_leak = power_of(core)
        unit_indent = nodes[start + 1][0] if start + 1 < end else 0
        part = {b: {"area": 0.0, "dynamic": 0.0, "leakage": 0.0} for b in CORE_BLOCKS}
        for ind, name, m in nodes[start + 1:end]:
            if ind != unit_indent:
                continue
            b = UNIT_TO_BLOCK.get(name)
            if b is None:
                print(f"[glue2] 경고: 알 수 없는 코어 유닛 '{name}' -> EXE에 포함")
                b = "EXE"
            dyn, leak = power_of(m)
            part[b]["area"] += m.get("Area", 0.0)
            part[b]["dynamic"] += dyn
            part[b]["leakage"] += leak

        # 코어 전체 값과 유닛 합의 차이(유닛에 속하지 않는 공통 부분)를
        # 면적 비율대로 나눠 넣어, 블록 합이 코어 전체와 같아지게 한다.
        a_sum = sum(p["area"] for p in part.values())
        d_sum = sum(p["dynamic"] for p in part.values())
        l_sum = sum(p["leakage"] for p in part.values())
        for b in CORE_BLOCKS:
            share = part[b]["area"] / a_sum if a_sum else 1.0 / len(CORE_BLOCKS)
            part[b]["dynamic"] += (core_dyn - d_sum) * share
            part[b]["leakage"] += (core_leak - l_sum) * share
            part[b]["area"] = core.get("Area", a_sum) * share
            blocks[f"core{ci}_{b}"] = part[b]

    def uncore(names):
        out = {"area": 0.0, "dynamic": 0.0, "leakage": 0.0}
        for n in names:
            for start, _ in sections.get(n, []):
                m = nodes[start][2]
                dyn, leak = power_of(m)
                out["area"] += m.get("Area", 0.0)
                out["dynamic"] += dyn
                out["leakage"] += leak
        return out

    blocks["L2"] = uncore(["L2", "BUSES"])
    blocks["MC"] = uncore(["Memory Controller"])
    for name in ("L2", "MC"):
        if blocks[name]["area"] <= 0:
            del blocks[name]
    return blocks, n_cores, proc


# ----------------------------------------------------------------------------
# 배치도 만들기
# ----------------------------------------------------------------------------
def grid_shape(n):
    """코어 격자 (열, 행). 1->1x1, 4->2x2, 8->4x2"""
    rows = max(1, int(math.floor(math.sqrt(n))))
    while n % rows:
        rows -= 1
    return n // rows, rows


def make_floorplan(blocks, n_cores):
    """
    반환: [(이름, 폭, 높이, x, y)] 단위 m
    좌표 원점은 칩의 왼쪽 아래.
    """
    mm = 1e-3
    core_area = max(
        sum(blocks[f"core{i}_{b}"]["area"] for b in CORE_BLOCKS) for i in range(n_cores)
    )
    side = math.sqrt(core_area)  # 코어는 정사각형 [mm]
    cols, rows = grid_shape(n_cores)
    chip_w = cols * side

    uncore = [n for n in ("L2", "MC") if n in blocks]
    uncore_area = sum(blocks[n]["area"] for n in uncore)
    uncore_h = uncore_area / chip_w if uncore else 0.0

    flp = []
    x = 0.0
    for n in uncore:  # 아래쪽 한 줄
        w = blocks[n]["area"] / uncore_h
        flp.append((n, w * mm, uncore_h * mm, x * mm, 0.0))
        x += w

    for i in range(n_cores):
        cx = (i % cols) * side
        cy = uncore_h + (rows - 1 - i // cols) * side
        a = sum(blocks[f"core{i}_{b}"]["area"] for b in CORE_BLOCKS)
        y = cy + side
        for b in CORE_BLOCKS:  # 위에서 아래로 가로 띠 모양으로 쌓는다
            h = side * blocks[f"core{i}_{b}"]["area"] / a
            y -= h
            flp.append((f"core{i}_{b}", side * mm, h * mm, cx * mm, y * mm))
    return flp, chip_w, uncore_h + rows * side


# ----------------------------------------------------------------------------
def convert(mcpat_out, outdir, verbose=True):
    nodes = parse_mcpat(mcpat_out)
    blocks, n_cores, proc = extract_blocks(nodes)
    flp, chip_w, chip_h = make_floorplan(blocks, n_cores)
    names = [f[0] for f in flp]

    os.makedirs(outdir, exist_ok=True)
    flp_path = os.path.join(outdir, "chip.flp")
    ptrace_path = os.path.join(outdir, "chip.ptrace")
    json_path = os.path.join(outdir, "power.json")

    with open(flp_path, "w") as f:
        f.write("# <name>\t<width>\t<height>\t<left-x>\t<bottom-y>   (m)\n")
        for name, w, h, x, y in flp:
            f.write(f"{name}\t{w:.9f}\t{h:.9f}\t{x:.9f}\t{y:.9f}\n")

    power = {n: blocks[n]["dynamic"] + blocks[n]["leakage"] for n in names}
    with open(ptrace_path, "w") as f:
        f.write("\t".join(names) + "\n")
        f.write("\t".join(f"{power[n]:.6f}" for n in names) + "\n")

    dyn = sum(blocks[n]["dynamic"] for n in names)
    leak = sum(blocks[n]["leakage"] for n in names)
    area = sum(blocks[n]["area"] for n in names)
    mcpat_dyn, mcpat_leak = power_of(proc)
    summary = {
        "n_cores": n_cores,
        "total_power_W": dyn + leak,
        "dynamic_power_W": dyn,
        "leakage_power_W": leak,
        "area_mm2": area,
        "chip_width_mm": chip_w,
        "chip_height_mm": chip_h,
        "mcpat_processor": {
            "dynamic_power_W": mcpat_dyn,
            "leakage_power_W": mcpat_leak,
            "area_mm2": proc.get("Area", 0.0),
        },
        "blocks": {
            n: {
                "power_W": power[n],
                "dynamic_W": blocks[n]["dynamic"],
                "leakage_W": blocks[n]["leakage"],
                "area_mm2": blocks[n]["area"],
            }
            for n in names
        },
    }
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2)

    if verbose:
        print(f"[glue2] 블록 {len(names)}개, 칩 {chip_w:.2f} x {chip_h:.2f} mm "
              f"({area:.2f} mm^2)")
        print(f"[glue2] 전력 {dyn + leak:.3f} W "
              f"(dynamic {dyn:.3f} + leakage {leak:.3f})")
        diff = (dyn + leak) - (mcpat_dyn + mcpat_leak)
        if abs(diff) > 0.01 * (mcpat_dyn + mcpat_leak):
            print(f"[glue2] 경고: 블록 합계가 McPAT Processor 합계와 {diff:+.3f} W 차이")
        print(f"[glue2] -> {flp_path}, {ptrace_path}, {json_path}")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mcpat", required=True, help="McPAT 출력 텍스트 (-print_level 5)")
    ap.add_argument("--outdir", required=True)
    a = ap.parse_args()
    convert(a.mcpat, a.outdir)
