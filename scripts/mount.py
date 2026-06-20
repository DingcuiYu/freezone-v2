import os
import sys

WORK_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOG_DIR = os.path.join(WORK_DIR, "log")
MOUNT_POINT = os.path.join(WORK_DIR, "mnt")

META_DEVICE = "/dev/nvme2n1"
DATA_DEVICE = "/dev/nvme2n2"

# make -j `nproc`

## 1056 MiB zone ( 32 GiB )
## zoned sudo insmod ./nvmev.ko memmap_start=102G memmap_size=49105M cpus=37,39
## block sudo insmod ./nvmev.ko memmap_start=102G memmap_size=45937M cpus=37,39
INSMOD_CMD = f"sudo insmod ./nvmev.ko memmap_start=102G memmap_size=49105M cpus=37,39"
MKFS_DIR = os.path.join(WORK_DIR, "f2fs-tools-1.14.0", "mkfs")
# block sudo ./mkfs.f2fs -f -c /dev/nvme2n2 /dev/nvme2n1
# zone sudo ./mkfs.f2fs -f -m -c /dev/nvme2n2 /dev/nvme2n1
MKFS_CMD = f"sudo ./mkfs.f2fs -f -m -c {DATA_DEVICE} {META_DEVICE}"

# MOUNT_CMD = "mount -o age_extent_cache,discard /dev/nvme2n1 mnt"
MOUNT_CMD = f"sudo mount {META_DEVICE} {MOUNT_POINT}"

RAWDEVICE = 0
MOUNT = 1
ALL = 2


def prepare():
    # compile
    os.chdir(WORK_DIR)
    ret = os.system("sudo make -j `nproc`")
    if ret:
        print("Compile Error")
        return ret
    return 0


def process(step=2):
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(MOUNT_POINT, exist_ok=True)

    if step != MOUNT:
        # insmod
        print(f"\ninsmod: {INSMOD_CMD}")
        ret = os.system(INSMOD_CMD)
        if ret:
            print("Insmod Error")
            return ret
        os.system(f"sudo dmesg > {os.path.join(LOG_DIR, 'insmod_dmesg')}")
        os.system("sudo dmesg -c > /dev/zero")

    if step == RAWDEVICE:
        return 0

    # mkfs.f2fs & mnt
    os.chdir(MKFS_DIR)
    print(f"\nmkfs: {MKFS_CMD}")
    ret = os.system(f"{MKFS_CMD} > {os.path.join(LOG_DIR, 'mkfs_dmesg')} 2>&1")
    if ret:
        print("mkfs.f2fs Error")
        return ret
    os.chdir(WORK_DIR)

    print(f"\nmount: {MOUNT_CMD}")
    ret = os.system(MOUNT_CMD)
    if ret:
        print("mnt Error")
        return ret
    os.system(f"sudo dmesg > {os.path.join(LOG_DIR, 'mount_dmesg')}")
    os.system("sudo dmesg -c > /dev/zero")
    return 0


# Usage: python3 mount.py
if __name__ == "__main__":
    os.chdir(WORK_DIR)
    ret = 0
    step = ALL
    if len(sys.argv) > 1:
        if sys.argv[1] == "rawdevice":
            step = RAWDEVICE
        elif sys.argv[1] == "mount":
            step = MOUNT
    if step != MOUNT:
        ret = prepare()
    if ret == 0:
        process(step=step)
