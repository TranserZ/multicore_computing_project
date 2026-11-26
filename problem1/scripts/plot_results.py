#!/usr/bin/env python3
"""
보고서용 그래프와 요약 표를 만든다 (입력: problem1/results/runs.csv).

  fig1_cores.png     코어 수 실험 (1/4/8코어, 4-wide): speedup, 전력, 최고 온도
  fig2_width.png     width 실험 (2/4/8-wide, 1코어): speedup, 전력, 최고 온도
  fig3_tradeoff.png  코어를 늘리는 방향과 폭을 넓히는 방향의 전력 대비 speedup
  fig4_window.png    8-wide에서 윈도우를 고정한 경우와 두 배로 키운 경우
  fig5_power_cores.png, fig6_power_width.png
                     전력이 어디로 가는지 (W): 부품별, dynamic과 leakage
  summary_table.csv  그래프에 쓴 값 전부 (구성별 평균, 표준편차, speedup)
  power_breakdown.csv  구성별 부품별 전력 (fig5, fig6에 쓴 값)

fig5, fig6은 실행별 상세 통계(problem1/results/run_details.csv)를 읽어 그린다.
이 파일은 extract_details.py가 out/ 의 원본에서 뽑아 만든다.

출력 폴더: problem1/results/figures/

matplotlib이 필요하다. 프로젝트 폴더의 가상환경으로 실행한다:
  .venv/bin/python problem1/scripts/plot_results.py
(가상환경 만들기: python3 -m venv .venv && .venv/bin/python -m pip install matplotlib)
"""

import csv
import math
import os
import statistics
import sys
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import extract_details  # noqa: E402
import results_log  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(results_log.RUNS_CSV), "figures")
BENCHES = ["blackscholes", "canneal", "freqmine", "streamcluster"]
AMBIENT_C = 45.0  # hotspot.config의 ambient (318.15 K)

# 색: 순서가 있는 값(코어 수, width)은 한 색의 밝기 단계로, 두 방향 비교는 두 색으로.
# 흰 바탕 기준으로 검증한 값이다.
RAMP3 = ["#86b6ef", "#2a78d6", "#104281"]
RAMP2 = ["#86b6ef", "#184f95"]
BLUE, ORANGE = "#2a78d6", "#eb6834"
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS = "#e1e0d9", "#c3c2b7"

# 그림은 보고서 본문 폭(약 7.2인치)에 맞춰 그린다. 넣을 때 줄이지 않으면 글자가 이 크기로 보인다.
FIG_W = 7.2
FS_LABEL, FS_TICK, FS_VALUE, FS_NOTE = 7.5, 7, 6.2, 6.2

# 부품별 전력: (열 이름, 범례 이름, 색). 색은 범주용 순서를 그대로 쓴다.
PARTS = [("core", "Core logic", "#2a78d6"), ("l1", "L1 caches", "#eb6834"),
         ("l2", "L2 cache", "#1baf7a"), ("bus", "Interconnect (bus)", "#eda100"),
         ("mc", "Memory controller", "#e87ba4")]
DYN_COLOR, LEAK_COLOR = "#6250d6", "#b5b4ad"

WINDOW_DEFAULT = ("128", "64", "32", "32")
WINDOW_HALF = ("64", "32", "16", "16")
WINDOW_DOUBLE = ("256", "128", "64", "64")


# ----------------------------------------------------------------------------
def load():
    """(benchmark, cores, width, window) -> 실행 목록. 기본 설정(warm-up 1회 등)만 쓴다."""
    groups = defaultdict(list)
    for r in results_log.read_rows():
        if (r["size"], r["l2"], r["clock"], r["warmup_runs"]) != ("simsmall", "2MiB", "3GHz", "1"):
            continue
        window = (r["rob"], r["iq"], r["lq"], r["sq"])
        groups[(r["benchmark"], int(r["cores"]), int(r["width"]), window)].append(r)
    return groups


class Stat:
    """한 구성의 평균과 표준편차."""

    def __init__(self, rows):
        self.n = len(rows)
        for col in results_log.METRIC_COLS:
            vals = [float(r[col]) for r in rows]
            setattr(self, col, statistics.mean(vals))
            setattr(self, col + "_sd", statistics.stdev(vals) if len(vals) > 1 else 0.0)


def speedup(base, s):
    """기준 구성 대비 speedup과 그 표준편차 (두 평균의 상대 표준편차를 합친 값)."""
    sp = base.roi_ms / s.roi_ms
    rel = math.hypot(base.roi_ms_sd / base.roi_ms, s.roi_ms_sd / s.roi_ms)
    return sp, (0.0 if s is base else sp * rel)


# ----------------------------------------------------------------------------
def style_axis(ax, ylabel):
    ax.set_ylabel(ylabel, color=INK2, fontsize=FS_LABEL)
    ax.yaxis.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(axis="both", colors=MUTED, labelsize=FS_TICK, length=0, pad=2)
    for lab in ax.get_xticklabels():
        lab.set_color(INK2)


def rounded_bar(ax, x, height, width, color, bottom=0.0):
    """위쪽만 둥근 막대. 반지름은 축 비율에 맞춰 화면에서 같은 크기로 보이게 한다."""
    if height <= 0:
        return
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    bbox = ax.get_window_extent()
    rx = 4 / bbox.width * (x1 - x0)   # 화면 4px(220dpi 기준 약 1.3pt)에 해당하는 데이터 길이
    ry = 4 / bbox.height * (y1 - y0)
    r = min(rx, width / 2)
    scale = ry / rx
    if height / scale <= 2 * r:   # 모서리를 둥글릴 만큼 높지 않으면 그냥 사각형
        ax.bar(x, height, width, bottom=bottom, color=color, linewidth=0)
        return
    # FancyBboxPatch는 한 반지름만 받으므로 y축을 x축 단위로 바꿔 그린 뒤 되돌린다.
    # pad만큼 사방으로 커지므로 상자는 r씩 줄여서 준다 (막대 끝이 정확히 값에 오도록).
    patch = FancyBboxPatch((x - width / 2 + r, bottom / scale + r),
                           width - 2 * r, height / scale - 2 * r,
                           boxstyle=f"round,pad={r},rounding_size={r}",
                           linewidth=0, facecolor=color,
                           transform=matplotlib.transforms.Affine2D().scale(1, scale) + ax.transData)
    ax.add_patch(patch)
    # 아래쪽은 기준선에 맞춰 각지게 덮는다.
    ax.bar(x, min(height, 2 * ry), width, bottom=bottom, color=color, linewidth=0)


def nice_top(vmax):
    """축 위쪽 끝: 가장 큰 값보다 10% 이상 여유를 두고 2 또는 5 단위로 올린다."""
    step = 2 if vmax < 20 else 5
    return math.ceil(vmax * 1.12 / step) * step


def grouped_bars(ax, values, errors, colors, ylabel, ylim, bottom=0.0, labels=None,
                 bar_w=0.22):
    """벤치마크별 묶음 막대. values[series][bench]."""
    n = len(values)
    gap = 0.03
    ax.set_xlim(-0.6, len(BENCHES) - 0.4)
    ax.set_ylim(*ylim)
    ax.set_xticks(range(len(BENCHES)))
    ax.set_xticklabels(BENCHES, rotation=25, ha="right", rotation_mode="anchor")
    style_axis(ax, ylabel)
    ax.figure.canvas.draw()  # 막대 모서리 반지름 계산에 축 크기가 필요하다
    for i in range(n):
        for b in range(len(BENCHES)):
            x = b + (i - (n - 1) / 2) * (bar_w + gap)
            v = values[i][b]
            rounded_bar(ax, x, v - bottom, bar_w, colors[i], bottom)
            e = errors[i][b]
            if e > 0:
                ax.errorbar(x, v, yerr=e, color=INK, linewidth=0.7, capsize=1.5, capthick=0.7)
            if labels and labels[i][b]:
                ax.annotate(labels[i][b], (x, v + e), xytext=(0, 2), textcoords="offset points",
                            ha="center", va="bottom", fontsize=FS_VALUE, color=INK2)


def legend(fig, names, colors, title):
    handles = [matplotlib.patches.Patch(facecolor=c, label=n) for n, c in zip(names, colors)]
    if title:  # 고정한 조건은 범례 맨 앞에 글자로만 적는다
        handles.insert(0, matplotlib.patches.Patch(alpha=0, label=title))
    leg = fig.legend(handles=handles, loc="upper center", ncol=len(handles),
                     frameon=False, fontsize=FS_LABEL, bbox_to_anchor=(0.5, 1.0),
                     handlelength=1.2, handleheight=0.9, columnspacing=1.4, labelcolor=INK2)
    if title:
        leg.get_texts()[0].set_color(INK)


def save(fig, name):
    path = os.path.join(OUT_DIR, name)
    fig.savefig(path, dpi=300, facecolor="white")
    plt.close(fig)
    print("  " + os.path.relpath(path))


def three_metric_figure(stats, configs, names, base_idx, legend_title, speedup_label, fname):
    """speedup, 전력, 최고 온도를 세로로 나란히 그린다. configs[i][bench] = Stat."""
    fig, axes = plt.subplots(1, 3, figsize=(FIG_W, 2.55), gridspec_kw={"width_ratios": [2, 1, 1]})
    fig.subplots_adjust(left=0.055, right=0.995, top=0.89, bottom=0.2, wspace=0.27)
    sp = [[speedup(configs[base_idx][b], configs[i][b]) for b in range(4)] for i in range(len(configs))]
    sp_v = [[x[0] for x in row] for row in sp]
    sp_e = [[x[1] for x in row] for row in sp]
    top = max(v + e for rv, re in zip(sp_v, sp_e) for v, e in zip(rv, re))
    grouped_bars(axes[0], sp_v, sp_e, RAMP3, speedup_label, (0, math.ceil(top + 0.6)),
                 labels=[["" if i == base_idx else f"{v:.2f}" for v in row]
                         for i, row in enumerate(sp_v)], bar_w=0.27)
    pw = [[c[b].power_W for b in range(4)] for c in configs]
    pw_e = [[c[b].power_W_sd for b in range(4)] for c in configs]
    grouped_bars(axes[1], pw, pw_e, RAMP3, "Average power (W)", (0, nice_top(max(map(max, pw)))))
    tc = [[c[b].peak_C for b in range(4)] for c in configs]
    tc_e = [[c[b].peak_C_sd for b in range(4)] for c in configs]
    grouped_bars(axes[2], tc, tc_e, RAMP3, "Peak steady-state temp. (°C)",
                 (AMBIENT_C, math.ceil(max(map(max, tc)) / 2 + 0.5) * 2), bottom=AMBIENT_C)
    axes[2].annotate(f"axis starts at ambient ({AMBIENT_C:g} °C)", (0.02, 0.985),
                     xycoords="axes fraction", va="top", fontsize=FS_NOTE, color=MUTED)
    legend(fig, names, RAMP3, legend_title)
    save(fig, fname)


# ----------------------------------------------------------------------------
# 전력 분해
# ----------------------------------------------------------------------------
def collect_breakdown(groups, keys, csv_path):
    """
    구성별 부품별 전력의 평균. {(bench, cores, width): {부품: (dyn, leak)}}.
    실행별 값은 run_details.csv에서 읽고, 구성별 평균은 csv_path에 표로 저장한다.
    """
    details = extract_details.read_details()
    result = {}
    for key in keys:
        runs = [details[r["run_id"]] for r in groups[key + (WINDOW_DEFAULT,)]
                if r["run_id"] in details]
        if not runs:
            sys.exit(f"run_details.csv에 {key} 구성의 실행이 없습니다. "
                     "먼저 problem1/scripts/extract_details.py를 실행하세요.")
        result[key] = {k: tuple(statistics.mean(float(r[f"{k}_{t}_W"]) for r in runs)
                                for t in ("dynamic", "leakage"))
                       for k, _, _ in PARTS}
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["benchmark", "cores", "width", "total_W", "dynamic_W", "leakage_W"]
                   + [f"{k}_{t}_W" for k, _, _ in PARTS for t in ("dynamic", "leakage")]
                   + [f"{k}_share_pct" for k, _, _ in PARTS])
        for key in keys:
            p = result[key]
            dyn = sum(v[0] for v in p.values())
            leak = sum(v[1] for v in p.values())
            w.writerow([*key, f"{dyn + leak:.3f}", f"{dyn:.3f}", f"{leak:.3f}"]
                       + [f"{p[k][j]:.4f}" for k, _, _ in PARTS for j in (0, 1)]
                       + [f"{sum(p[k]) / (dyn + leak) * 100:.2f}" for k, _, _ in PARTS])
    return result


def breakdown_figure(bd, configs, tick_names, fname):
    """
    전력이 어디로 가는지를 W 단위로 쌓아 그린다. 막대 하나가 구성 하나.
    왼쪽: 부품별 (코어 로직, L1 캐시, 공유 부분), 오른쪽: dynamic과 static(leakage).
    비율이 아니라 크기로 그려서, 구성이 커질 때 어느 부분이 늘고 어느 부분이 그대로인지 보인다.
    configs: [(cores, width)] 3개, tick_names: 막대 아래에 적을 이름 3개.
    """
    fig, axes = plt.subplots(1, 2, figsize=(FIG_W, 2.75))
    fig.subplots_adjust(left=0.06, right=0.995, top=0.88, bottom=0.17, wspace=0.17)
    xs, labels = [], []
    for bi in range(len(BENCHES)):
        for ci in range(len(configs)):
            xs.append(bi * (len(configs) + 0.9) + ci)
            labels.append(tick_names[ci])
    keys = [(b, c, w) for b in BENCHES for c, w in configs]
    totals = [sum(sum(v) for v in bd[k].values()) for k in keys]
    top = nice_top(max(totals) * 0.98)   # 막대 위 숫자가 들어갈 만큼만 여유를 둔다

    def setup(ax):
        ax.set_xlim(xs[0] - 0.8, xs[-1] + 0.8)
        ax.set_ylim(0, top)
        ax.set_xticks(xs)
        ax.set_xticklabels(labels)
        style_axis(ax, "Average power (W)")
        for bi, b in enumerate(BENCHES):   # 벤치마크 이름은 묶음 가운데 아래에
            mid = statistics.mean(xs[bi * len(configs):(bi + 1) * len(configs)])
            ax.annotate(b, (mid, 0), xytext=(0, -19), textcoords="offset points",
                        ha="center", va="top", fontsize=FS_TICK, color=INK2)

    def stack(ax, series, top_labels, note):
        """series: [(이름, 색, [막대별 값])]. 막대 사이와 조각 사이는 흰 틈으로 나눈다."""
        bottom = [0.0] * len(xs)
        for name, color, vals in series:
            ax.bar(xs, vals, 0.82, bottom=bottom, color=color, edgecolor="white",
                   linewidth=0.7, label=name)
            bottom = [b + v for b, v in zip(bottom, vals)]
        for x, t, text in zip(xs, totals, top_labels):
            ax.annotate(text, (x, t), xytext=(0, 2), textcoords="offset points",
                        ha="center", va="bottom", fontsize=FS_VALUE, color=INK2)
        ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.02), ncol=len(series), frameon=False,
                  fontsize=FS_TICK, labelcolor=INK2, handlelength=1.1, handleheight=0.8,
                  columnspacing=1.2, borderaxespad=0)
        ax.annotate(note, (0.012, 0.985), xycoords="axes fraction", va="top",
                    fontsize=FS_NOTE, color=MUTED)

    # 버스와 메모리 컨트롤러는 1 W에 못 미쳐 따로 그리면 보이지 않으므로 L2와 묶는다.
    groups = [("Core logic", ["core"], PARTS[0][2]), ("L1 caches", ["l1"], PARTS[1][2]),
              ("Shared: L2, bus, mem. controller", ["l2", "bus", "mc"], PARTS[2][2])]
    setup(axes[0])
    stack(axes[0],
          [(name, color, [sum(sum(bd[k][p]) for p in parts) for k in keys])
           for name, parts, color in groups],
          [f"{t:.0f}" if t >= 10 else f"{t:.1f}" for t in totals], "labels: total power (W)")

    setup(axes[1])
    dyn = [sum(v[0] for v in bd[k].values()) for k in keys]
    leak = [sum(v[1] for v in bd[k].values()) for k in keys]
    stack(axes[1], [("Dynamic", DYN_COLOR, dyn), ("Static (leakage)", LEAK_COLOR, leak)],
          [f"{lk / t * 100:.0f}%" for lk, t in zip(leak, totals)], "labels: static share of total")
    save(fig, fname)


# ----------------------------------------------------------------------------
def main():
    groups = load()

    def stat(bench, cores, width, window=WINDOW_DEFAULT):
        rows = groups.get((bench, cores, width, window))
        if not rows:
            sys.exit(f"runs.csv에 기록이 없음: {bench} {cores}코어 {width}-wide {window}")
        return Stat(rows)

    os.makedirs(OUT_DIR, exist_ok=True)
    print("그래프:")

    # 1) 코어 수 실험
    cores = [[stat(b, c, 4) for b in BENCHES] for c in (1, 4, 8)]
    three_metric_figure(None, cores, ["1 core", "4 cores", "8 cores"], 0, "4-wide core,",
                        "Speedup (normalized to 1 core)", "fig1_cores.png")

    # 2) width 실험
    widths = [[stat(b, 1, w) for b in BENCHES] for w in (2, 4, 8)]
    three_metric_figure(None, widths, ["2-wide", "4-wide", "8-wide"], 0, "1 core,",
                        "Speedup (normalized to 2-wide)", "fig2_width.png")

    # 3) 전력 대비 speedup: 기준은 두 실험이 공유하는 1코어 4-wide.
    #    비율이므로 로그 축을 쓰고, 벤치마크끼리 비교할 수 있게 축 범위를 맞춘다.
    fig, axes = plt.subplots(1, 4, figsize=(FIG_W, 2.4), sharex=True, sharey=True)
    fig.subplots_adjust(left=0.065, right=0.995, top=0.8, bottom=0.17, wspace=0.07)
    lo, hi = 0.4, 9.0
    ticks = [0.5, 1, 2, 4, 8]
    # 라벨을 선과 겹치지 않는 쪽에 둔다: (x 오프셋, y 오프셋, 가로 정렬, 세로 정렬)
    place = {"4c": (-4, 3, "right", "bottom"), "8c": (5, 0, "left", "center"),
             "2w": (5, -2, "left", "top"), "8w": (5, -1, "left", "center")}
    for bi, (ax, b) in enumerate(zip(axes, BENCHES)):
        base = cores[0][bi]
        paths = [
            ("more cores", BLUE, "o", [(cores[i][bi], t) for i, t in ((1, "4c"), (2, "8c"))]),
            ("wider core", ORANGE, "s", [(widths[i][bi], t) for i, t in ((0, "2w"), (2, "8w"))]),
        ]
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlim(lo, hi)
        ax.set_ylim(lo, hi)
        ax.set_aspect("equal", adjustable="box")
        for axis in (ax.xaxis, ax.yaxis):
            axis.set_major_locator(matplotlib.ticker.FixedLocator(ticks))
            axis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}"))
            axis.set_minor_locator(matplotlib.ticker.NullLocator())
        style_axis(ax, "Speedup vs 1 core, 4-wide" if bi == 0 else "")
        ax.xaxis.grid(True, color=GRID, linewidth=0.6)
        ax.set_title(b, fontsize=FS_LABEL + 0.5, color=INK, loc="left", pad=4)
        ax.plot([lo, hi], [lo, hi], color=AXIS, linewidth=0.8)  # 전력이 늘어난 만큼 빨라지는 선
        if bi == 0:
            ax.annotate("speedup = power\n(equal energy)", (0.95, 1.75), rotation=45,
                        fontsize=FS_NOTE, color=MUTED, ha="center", va="bottom",
                        rotation_mode="anchor", linespacing=1.1)
        for name, color, marker, items in paths:
            if name == "wider core":   # 2-wide -> 기준 -> 8-wide 순서로 잇는다
                order = [items[0], (base, None), items[1]]
            else:
                order = [(base, None)] + items
            xs = [s.power_W / base.power_W for s, _ in order]
            ys = [base.roi_ms / s.roi_ms for s, _ in order]
            ax.plot(xs, ys, color=color, linewidth=1.6, solid_capstyle="round", zorder=3)
            for s, text in items:
                x, y = s.power_W / base.power_W, base.roi_ms / s.roi_ms
                ax.plot(x, y, marker=marker, markersize=5.5, color=color,
                        markeredgecolor="white", markeredgewidth=1, zorder=4, label=name)
                dx, dy, ha, va = place[text]
                ax.annotate(text, (x, y), xytext=(dx, dy), textcoords="offset points",
                            fontsize=FS_TICK, color=INK2, ha=ha, va=va)
        ax.plot(1, 1, marker="o", markersize=4.5, color=INK2,
                markeredgecolor="white", markeredgewidth=1, zorder=5)
    fig.supxlabel("Power relative to 1 core, 4-wide", color=INK2, fontsize=FS_LABEL, y=0.01)
    handles = [matplotlib.lines.Line2D([], [], color=BLUE, marker="o", markersize=5.5, linewidth=1.6,
                                       markeredgecolor="white", label="more cores (4c, 8c at 4-wide)"),
               matplotlib.lines.Line2D([], [], color=ORANGE, marker="s", markersize=5.5, linewidth=1.6,
                                       markeredgecolor="white", label="wider core (2w, 8w at 1 core)"),
               matplotlib.lines.Line2D([], [], color=INK2, marker="o", markersize=4.5, linewidth=0,
                                       markeredgecolor="white", label="baseline: 1 core, 4-wide")]
    fig.legend(handles=handles, loc="upper center", ncol=3, frameon=False, fontsize=FS_LABEL,
               bbox_to_anchor=(0.5, 1.0), labelcolor=INK2, columnspacing=1.8)
    save(fig, "fig3_tradeoff.png")

    # 4) 윈도우 크기: 8-wide speedup, 고정 vs 두 배 (각각의 2-wide 기준)
    half = [stat(b, 1, 2, WINDOW_HALF) for b in BENCHES]
    double = [stat(b, 1, 8, WINDOW_DOUBLE) for b in BENCHES]
    fixed = [speedup(widths[0][i], widths[2][i]) for i in range(4)]
    scaled = [speedup(half[i], double[i]) for i in range(4)]
    fig, axes = plt.subplots(1, 2, figsize=(FIG_W, 2.5))
    fig.subplots_adjust(left=0.055, right=0.995, top=0.89, bottom=0.2, wspace=0.17)
    vals = [[x[0] for x in fixed], [x[0] for x in scaled]]
    errs = [[x[1] for x in fixed], [x[1] for x in scaled]]
    grouped_bars(axes[0], vals, errs, RAMP2, "8-wide speedup (normalized to 2-wide)",
                 (0, math.ceil(max(map(max, vals)) + 0.6)),
                 labels=[[f"{v:.2f}" for v in row] for row in vals], bar_w=0.3)
    pw = [[widths[2][i].power_W for i in range(4)], [double[i].power_W for i in range(4)]]
    pw_e = [[widths[2][i].power_W_sd for i in range(4)], [double[i].power_W_sd for i in range(4)]]
    grouped_bars(axes[1], pw, pw_e, RAMP2, "8-wide average power (W)",
                 (0, nice_top(max(map(max, pw))), ),
                 labels=[[f"{v:.1f}" for v in row] for row in pw], bar_w=0.3)
    legend(fig, ["fixed: ROB 128 / IQ 64 / LQ 32 / SQ 32",
                 "scaled with width: ROB 256 / IQ 128 / LQ 64 / SQ 64"], RAMP2, None)
    save(fig, "fig4_window.png")

    # 5, 6) 전력이 어디로 가는지
    bd_keys = ([(b, c, 4) for b in BENCHES for c in (1, 4, 8)]
               + [(b, 1, w) for b in BENCHES for w in (2, 8)])
    bd = collect_breakdown(groups, bd_keys, os.path.join(OUT_DIR, "power_breakdown.csv"))
    breakdown_figure(bd, [(1, 4), (4, 4), (8, 4)], ["1c", "4c", "8c"], "fig5_power_cores.png")
    breakdown_figure(bd, [(1, 2), (1, 4), (1, 8)], ["2w", "4w", "8w"], "fig6_power_width.png")

    # 요약 표: 그래프에 쓴 값 전부
    table = os.path.join(OUT_DIR, "summary_table.csv")
    with open(table, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["experiment", "benchmark", "cores", "width", "rob", "iq", "lq", "sq", "runs",
                    "roi_ms", "roi_ms_sd", "speedup", "speedup_sd", "speedup_base", "ipc",
                    "power_W", "power_W_sd", "dynamic_W", "leakage_W", "area_mm2",
                    "peak_C", "peak_C_sd", "avg_C"])

        def row(exp, b, c, wd, window, s, base, base_name):
            sp, sd = speedup(base, s)
            w.writerow([exp, b, c, wd, *window, s.n, f"{s.roi_ms:.3f}", f"{s.roi_ms_sd:.3f}",
                        f"{sp:.3f}", f"{sd:.3f}", base_name, f"{s.ipc:.3f}",
                        f"{s.power_W:.3f}", f"{s.power_W_sd:.3f}", f"{s.dynamic_W:.3f}",
                        f"{s.leakage_W:.3f}", f"{s.area_mm2:.2f}", f"{s.peak_C:.2f}",
                        f"{s.peak_C_sd:.2f}", f"{s.avg_C:.2f}"])

        for bi, b in enumerate(BENCHES):
            for ci, c in enumerate((1, 4, 8)):
                row("cores", b, c, 4, WINDOW_DEFAULT, cores[ci][bi], cores[0][bi], "1 core 4-wide")
        for bi, b in enumerate(BENCHES):
            for wi, wd in enumerate((2, 4, 8)):
                row("width", b, 1, wd, WINDOW_DEFAULT, widths[wi][bi], widths[0][bi], "1 core 2-wide")
        for bi, b in enumerate(BENCHES):
            row("width_scaled_window", b, 1, 2, WINDOW_HALF, half[bi], half[bi], "1 core 2-wide ROB 64")
            row("width_scaled_window", b, 1, 4, WINDOW_DEFAULT, widths[1][bi], half[bi], "1 core 2-wide ROB 64")
            row("width_scaled_window", b, 1, 8, WINDOW_DOUBLE, double[bi], half[bi], "1 core 2-wide ROB 64")
    print("표:\n  " + os.path.relpath(table)
          + "\n  " + os.path.relpath(os.path.join(OUT_DIR, "power_breakdown.csv")))


if __name__ == "__main__":
    main()
