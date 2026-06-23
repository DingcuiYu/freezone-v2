import argparse
import glob
import os
import re

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import matplotlib.pyplot as plt

DIR = "log/fio_results-raw"
TASK = "D"

DEFAULT_RESULT_DIRS = {
    "block": f"{DIR}/block",
    "conzone": f"{DIR}/conzone",
}

SCHEME_COLORS = {
    "block": ["#08306b", "#2171b5", "#6baed6", "#c6dbef"],
    "conzone": ["#7f2704", "#d94801", "#fd8d3c", "#fdd0a2"],
}

THREAD_MARKERS = {
    1: "o",
    2: "s",
    4: "^",
    8: "D",
}


def parse_queue_depths(queue_depths_str):
    """Parse comma-separated queue depths such as 1,2,4,8."""
    if queue_depths_str is None:
        return None

    queue_depths = []
    seen = set()
    for item in queue_depths_str.split(","):
        item = item.strip()
        if not item:
            continue

        queue_depth = int(item)
        if queue_depth <= 0:
            raise ValueError(f"Queue depth must be positive: {queue_depth}")
        if queue_depth not in seen:
            seen.add(queue_depth)
            queue_depths.append(queue_depth)

    if not queue_depths:
        raise ValueError("At least one queue depth must be provided")
    return queue_depths


def parse_iops(iops_str):
    """Convert fio IOPS strings such as 1811, 86.0k, or 1.2M to numeric IOPS."""
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)([kKmM]?)\s*", iops_str)
    if not match:
        raise ValueError(f"Unsupported IOPS value: {iops_str}")

    value = float(match.group(1))
    suffix = match.group(2).lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return value


def parse_fio_result(file_path):
    """Return one parsed C-task fio result as (threads, queue_depth, iops)."""
    filename = os.path.basename(file_path)
    match = f"{TASK}_t(\d+)_qd(\d+)_fio_result\.txt"
    name_match = re.fullmatch(match, filename)
    if not name_match:
        return None

    threads = int(name_match.group(1))
    queue_depth = int(name_match.group(2))

    with open(file_path, "r", encoding="utf-8") as fio_file:
        content = fio_file.read()

    iops_match = re.search(r"^\s*write:\s+IOPS=([^,\s]+)", content, re.MULTILINE)
    if not iops_match:
        raise ValueError(f"Could not find write IOPS in {file_path}")

    return threads, queue_depth, parse_iops(iops_match.group(1))


def collect_iops_data(result_dirs=DEFAULT_RESULT_DIRS):
    """Collect C-task IOPS data as data[scheme][threads][queue_depth] = iops."""
    data = {}
    for scheme, result_dir in result_dirs.items():
        scheme_data = {}
        pattern = os.path.join(result_dir, f"{TASK}_t*_qd*_fio_result.txt")
        for file_path in glob.glob(pattern):
            parsed = parse_fio_result(file_path)
            if parsed is None:
                continue

            threads, queue_depth, iops = parsed
            scheme_data.setdefault(threads, {})[queue_depth] = iops

        data[scheme] = scheme_data
    return data


def plot_iops_by_queue_depth(
    result_dirs=DEFAULT_RESULT_DIRS,
    output_path="log/fio_results/task_c_iops.png",
    font_size=28,
    queue_depths_to_plot=None,
):
    """Plot 4K sequential direct-write IOPS by queue depth for block and conzone."""
    data = collect_iops_data(result_dirs)
    if queue_depths_to_plot is None:
        queue_depths_to_plot = sorted(
            {qd for scheme in data.values() for qds in scheme.values() for qd in qds}
        )
    else:
        queue_depths_to_plot = list(queue_depths_to_plot)

    qd_positions = {qd: index for index, qd in enumerate(queue_depths_to_plot)}

    plt.rcParams.update(
        {
            "font.family": "Times New Roman",
            "font.size": font_size,
            "axes.labelsize": font_size,
            "axes.titlesize": font_size,
            "xtick.labelsize": font_size,
            "ytick.labelsize": font_size,
            "legend.fontsize": max(font_size - 6, 1),
        }
    )

    fig, ax = plt.subplots(figsize=(18, 12))

    for scheme, threads_data in data.items():
        colors = SCHEME_COLORS.get(scheme, ["#333333"])
        for index, threads in enumerate(sorted(threads_data)):
            qd_iops = threads_data[threads]
            queue_depths = [qd for qd in queue_depths_to_plot if qd in qd_iops]
            if not queue_depths:
                continue
            x_positions = [qd_positions[qd] for qd in queue_depths]
            iops_values = [qd_iops[qd] for qd in queue_depths]
            color = colors[index % len(colors)]
            marker = THREAD_MARKERS.get(threads, "o")

            ax.plot(
                x_positions,
                iops_values,
                marker=marker,
                markersize=12,
                linewidth=3,
                color=color,
                label=f"{scheme} - {threads} thread{'s' if threads > 1 else ''}",
            )

    ax.set_xlabel("Queue Depth")
    ax.set_ylabel("IOPS")
    ax.set_title("4K Sequential Direct Write IOPS")
    ax.set_xticks(range(len(queue_depths_to_plot)))
    ax.set_xticklabels(queue_depths_to_plot)
    ax.grid(True, linestyle="--", linewidth=1, alpha=0.45)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=2, frameon=False)
    fig.tight_layout(rect=[0, 0.12, 1, 1])

    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Plot fio C-task IOPS by queue depth for block and conzone."
    )
    parser.add_argument("--font-size", type=int, default=28)
    parser.add_argument(
        "--output",
        default=f"{DIR}/task_iops.png",
        help="Output image path.",
    )
    parser.add_argument(
        "--queue-depths",
        default=None,
        help="Comma-separated queue depths to plot, for example: 1,2,4,8.",
    )
    args = parser.parse_args()

    output_path = plot_iops_by_queue_depth(
        output_path=args.output,
        font_size=args.font_size,
        queue_depths_to_plot=parse_queue_depths(args.queue_depths),
    )
    print(f"Saved figure to: {output_path}")


if __name__ == "__main__":
    main()
