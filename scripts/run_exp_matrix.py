#!/usr/bin/env python3
import argparse
import json
import os
import pty
import queue
import re
import select
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
EXP_PATH = REPO_ROOT / "scripts" / "exp.py"
MOUNT_PATH = REPO_ROOT / "scripts" / "mount.py"
SIZE_PATH = REPO_ROOT / "scripts" / "size.py"
LOG_ROOT = REPO_ROOT / "log" / "exp_matrix"
STATUS_PATH = LOG_ROOT / "status.json"
SUMMARY_PATH = LOG_ROOT / "summary.tsv"


@dataclass(frozen=True)
class Experiment:
    name: str
    config_name: str
    log_dir_expr: str
    task_name: str
    interface_type: str
    expected_hours: float


EXPERIMENTS = [
    Experiment(
        name="01_conzone_raw",
        config_name="conzone",
        log_dir_expr='f"log/fio_results-raw/{CONFIG_NAME}"',
        task_name="task_d",
        interface_type="zoned",
        expected_hours=2.0,
    ),
    Experiment(
        name="02_block_raw",
        config_name="block",
        log_dir_expr='f"log/fio_results-raw/{CONFIG_NAME}"',
        task_name="task_d",
        interface_type="block",
        expected_hours=2.0,
    ),
    Experiment(
        name="03_conzone_fs",
        config_name="conzone",
        log_dir_expr='f"log/fio_results-fs/{CONFIG_NAME}"',
        task_name="task_c",
        interface_type="zoned",
        expected_hours=2.0,
    ),
    Experiment(
        name="04_block_fs",
        config_name="block",
        log_dir_expr='f"log/fio_results-fs/{CONFIG_NAME}"',
        task_name="task_c",
        interface_type="block",
        expected_hours=2.0,
    ),
]


def now_string():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def format_duration(seconds):
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def ensure_log_files():
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    if not SUMMARY_PATH.exists():
        SUMMARY_PATH.write_text(
            "name\tstatus\tstart_time\tend_time\telapsed\treturncode\tlog_path\n",
            encoding="utf-8",
        )


def write_status(exp, status, start_time, elapsed=0, returncode=None, note=""):
    STATUS_PATH.write_text(
        json.dumps(
            {
                "experiment": exp.name if exp else None,
                "status": status,
                "start_time": start_time,
                "updated_at": now_string(),
                "elapsed": format_duration(elapsed),
                "returncode": returncode,
                "note": note,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def append_summary(exp, status, start_time, end_time, elapsed, returncode, log_path):
    with SUMMARY_PATH.open("a", encoding="utf-8") as f:
        f.write(
            "\t".join(
                [
                    exp.name,
                    status,
                    start_time,
                    end_time,
                    format_duration(elapsed),
                    "" if returncode is None else str(returncode),
                    str(log_path.relative_to(REPO_ROOT)),
                ]
            )
            + "\n"
        )


def backup_exp_py():
    backup = EXP_PATH.with_suffix(".py.matrix.bak")
    if not backup.exists():
        backup.write_text(EXP_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[{now_string()}] Backed up scripts/exp.py to {backup.relative_to(REPO_ROOT)}")


def backup_mount_py():
    backup = MOUNT_PATH.with_suffix(".py.matrix.bak")
    if not backup.exists():
        backup.write_text(MOUNT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[{now_string()}] Backed up scripts/mount.py to {backup.relative_to(REPO_ROOT)}")


def replace_main_task_call(text, task_name):
    pattern = re.compile(
        r"(?P<prefix>^def main\(\):\n.*?^\s*# Execute all tasks\n)"
        r"(?P<indent>\s*)task_[a-z]\(\)\s*$",
        flags=re.MULTILINE | re.DOTALL,
    )
    new_text, count = pattern.subn(
        lambda match: f"{match.group('prefix')}{match.group('indent')}{task_name}()",
        text,
        count=1,
    )
    if count != 1:
        raise RuntimeError("Could not find the task call inside exp.py main()")
    return new_text


def configure_exp_py(exp):
    text = EXP_PATH.read_text(encoding="utf-8")
    text = re.sub(
        r'^CONFIG_NAME\s*=\s*["\'].*?["\']\s*$',
        f'CONFIG_NAME = "{exp.config_name}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r"^LOG_DIR\s*=.*$",
        f"LOG_DIR = {exp.log_dir_expr}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    text = replace_main_task_call(text, exp.task_name)
    EXP_PATH.write_text(text, encoding="utf-8")
    print(
        f"[{now_string()}] Configured exp.py: "
        f"CONFIG_NAME={exp.config_name}, LOG_DIR={exp.log_dir_expr}, main={exp.task_name}()"
    )


def extract_insmod_command(output):
    matches = re.findall(r"sudo\s+insmod\s+\./nvmev\.ko[^\r\n]*", output)
    if not matches:
        raise RuntimeError("Could not find the Insmod Command in size.py output")
    return matches[-1].strip()


def configure_mount_py(insmod_cmd):
    text = MOUNT_PATH.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r'^INSMOD_CMD\s*=\s*f?["\'].*?["\']\s*$',
        f'INSMOD_CMD = f"{insmod_cmd}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise RuntimeError("Could not find INSMOD_CMD in scripts/mount.py")
    MOUNT_PATH.write_text(new_text, encoding="utf-8")
    print(f"[{now_string()}] Configured mount.py: INSMOD_CMD={insmod_cmd}")


def automate_size_py(interface_type, log_path):
    prompts = [
        ("Confirm overwrite", "y\n"),
        ("Select prototype", "conzone\n"),
        ("Memmap start address", "\n"),
        ("Flash type", "\n"),
        ("Interface type (block/zoned)", f"{interface_type}\n"),
        ("Block size", "\n"),
        ("Planes per Superblock", "\n"),
        ("DIES_PER_ZONE", "\n"),
        ("PLNS_PER_LUN", "\n"),
        ("pSLC superblocks for data", "\n"),
        ("Data namespace size", "\n"),
        ("Meta namespace size", "\n"),
        ("Meta OP ratio", "\n"),
    ]

    print(f"[{now_string()}] Running size.py with interface={interface_type}")
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, str(SIZE_PATH.relative_to(REPO_ROOT))],
        cwd=REPO_ROOT,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        text=False,
        close_fds=True,
    )
    os.close(slave_fd)

    pending = ""
    output_chunks = []
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"# {now_string()} size.py interface={interface_type}\n")
        while True:
            if proc.poll() is not None:
                break
            ready, _, _ = select.select([master_fd], [], [], 1.0)
            if ready:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                chunk = data.decode(errors="replace")
                print(chunk, end="", flush=True)
                log.write(chunk)
                log.flush()
                output_chunks.append(chunk)
                pending += chunk
                pending = pending[-2000:]

                for pattern, response in prompts:
                    if pattern in pending:
                        os.write(master_fd, response.encode())
                        log.write(response)
                        log.flush()
                        pending = ""
                        break
            elif time.monotonic() - started > 300:
                proc.terminate()
                raise TimeoutError("size.py did not finish within 5 minutes")

        try:
            while True:
                data = os.read(master_fd, 4096)
                if not data:
                    break
                chunk = data.decode(errors="replace")
                print(chunk, end="", flush=True)
                log.write(chunk)
                output_chunks.append(chunk)
        except OSError:
            pass

    os.close(master_fd)
    returncode = proc.wait()
    if returncode != 0:
        raise RuntimeError(f"size.py failed with return code {returncode}; see {log_path}")
    insmod_cmd = extract_insmod_command("".join(output_chunks))
    print(f"[{now_string()}] size.py finished; insmod command: {insmod_cmd}")
    return insmod_cmd


def reader_thread(stream, out_queue):
    try:
        for line in iter(stream.readline, ""):
            out_queue.put(line)
    finally:
        stream.close()


def run_experiment(exp, monitor_interval, timeout_hours):
    log_path = LOG_ROOT / f"{exp.name}.log"
    expected_seconds = exp.expected_hours * 3600
    timeout_seconds = timeout_hours * 3600 if timeout_hours else None
    start_time = now_string()
    start = time.monotonic()
    write_status(exp, "running", start_time)

    cmd = [sys.executable, "-u", str(EXP_PATH.relative_to(REPO_ROOT))]
    print(f"[{now_string()}] Starting {exp.name}; expected about {exp.expected_hours:.1f}h")
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"# {start_time} {' '.join(cmd)}\n")
        proc = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        out_queue = queue.Queue()
        thread = threading.Thread(target=reader_thread, args=(proc.stdout, out_queue), daemon=True)
        thread.start()
        last_report = start

        try:
            while proc.poll() is None:
                try:
                    line = out_queue.get(timeout=1.0)
                except queue.Empty:
                    line = None

                if line is not None:
                    print(line, end="", flush=True)
                    log.write(line)
                    log.flush()

                elapsed = time.monotonic() - start
                if elapsed - (last_report - start) >= monitor_interval:
                    over = elapsed - expected_seconds
                    if over > 0:
                        msg = (
                            f"[{now_string()}] {exp.name} running for "
                            f"{format_duration(elapsed)}; over expected time by {format_duration(over)}"
                        )
                    else:
                        msg = (
                            f"[{now_string()}] {exp.name} running for "
                            f"{format_duration(elapsed)}; expected remaining about "
                            f"{format_duration(expected_seconds - elapsed)}"
                        )
                    print(msg)
                    log.write(msg + "\n")
                    log.flush()
                    write_status(exp, "running", start_time, elapsed)
                    last_report = time.monotonic()

                if timeout_seconds and elapsed > timeout_seconds:
                    msg = f"[{now_string()}] Timeout reached; terminating {exp.name}"
                    print(msg)
                    log.write(msg + "\n")
                    proc.terminate()
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break

            while True:
                try:
                    line = out_queue.get_nowait()
                except queue.Empty:
                    break
                print(line, end="", flush=True)
                log.write(line)
        except KeyboardInterrupt:
            msg = f"[{now_string()}] Interrupted; terminating {exp.name}"
            print(msg)
            log.write(msg + "\n")
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise

    returncode = proc.wait()
    elapsed = time.monotonic() - start
    end_time = now_string()
    status = "ok" if returncode == 0 else "failed"
    write_status(exp, status, start_time, elapsed, returncode)
    append_summary(exp, status, start_time, end_time, elapsed, returncode, log_path)
    print(
        f"[{now_string()}] Finished {exp.name}: status={status}, "
        f"elapsed={format_duration(elapsed)}, log={log_path.relative_to(REPO_ROOT)}"
    )
    if returncode != 0:
        raise RuntimeError(f"{exp.name} failed with return code {returncode}")


def select_experiments(start_at, only):
    selected = EXPERIMENTS
    if only:
        wanted = set(only)
        selected = [exp for exp in selected if exp.name in wanted or exp.name[:2] in wanted]
    if start_at:
        index = None
        for i, exp in enumerate(selected):
            if exp.name == start_at or exp.name[:2] == start_at:
                index = i
                break
        if index is None:
            raise ValueError(f"Unknown start experiment: {start_at}")
        selected = selected[index:]
    return selected


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run the four freezone experiment configurations in order."
    )
    parser.add_argument(
        "--start-at",
        help="Start at an experiment name or number, e.g. 03 or 03_conzone_fs.",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        help="Run only selected experiment names or numbers, e.g. 01 04_block_fs.",
    )
    parser.add_argument(
        "--monitor-interval",
        type=int,
        default=300,
        help="Seconds between progress reports while exp.py is running.",
    )
    parser.add_argument(
        "--timeout-hours",
        type=float,
        default=0,
        help="Optional per-experiment timeout. 0 means monitor only, do not kill.",
    )
    parser.add_argument(
        "--skip-size",
        action="store_true",
        help="Do not run scripts/size.py before each experiment.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Configure files for the selected experiments, but do not launch exp.py.",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Print the planned sequence without modifying files or running experiments.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    selected = select_experiments(args.start_at, args.only)
    if not selected:
        print("No experiments selected.")
        return 1

    print("Experiment plan:")
    for exp in selected:
        print(
            f"  {exp.name}: CONFIG_NAME={exp.config_name}, "
            f"interface={exp.interface_type}, {exp.task_name}(), LOG_DIR={exp.log_dir_expr}"
        )

    if args.plan_only:
        return 0

    if not args.dry_run and os.geteuid() != 0:
        print("Please run this script as root, for example: sudo python3 scripts/run_exp_matrix.py")
        return 1

    ensure_log_files()
    backup_exp_py()
    backup_mount_py()

    signal.signal(signal.SIGTERM, lambda signum, frame: (_ for _ in ()).throw(KeyboardInterrupt))

    try:
        for exp in selected:
            size_log = LOG_ROOT / f"{exp.name}_size.log"
            configure_exp_py(exp)
            if not args.skip_size:
                insmod_cmd = automate_size_py(exp.interface_type, size_log)
                configure_mount_py(insmod_cmd)
            if args.dry_run:
                print(f"[{now_string()}] Dry run configured {exp.name}; skipping exp.py launch")
                continue
            run_experiment(exp, args.monitor_interval, args.timeout_hours)
    except KeyboardInterrupt:
        write_status(None, "interrupted", now_string(), note="Interrupted by user")
        print(f"[{now_string()}] Stopped by user.")
        return 130
    except Exception as exc:
        print(f"[{now_string()}] ERROR: {exc}")
        return 1

    if args.dry_run:
        write_status(None, "configured", now_string(), note="Dry run configured selected experiments")
        print(f"[{now_string()}] Dry run finished; selected experiments were configured but not launched.")
    else:
        write_status(None, "complete", now_string(), note="All selected experiments completed")
        print(f"[{now_string()}] All selected experiments completed.")
        print(f"Summary: {SUMMARY_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
