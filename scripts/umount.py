import os
import sys

WORK_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOG_DIR = os.path.join(WORK_DIR, "log")
MOUNT_POINT = os.path.join(WORK_DIR, "mnt")

RMMOD_CMD = "sudo rmmod nvmev"
UMOUNT_CMD = f"sudo umount {MOUNT_POINT}"


def process(rawdevice=False):
    os.makedirs(LOG_DIR, exist_ok=True)

    if rawdevice == False:
        # umount
        print("\numount: %s" % (UMOUNT_CMD))
        ret = os.system(UMOUNT_CMD)

        if ret:
            print("Umount Error")
            return ret

    # rmmod
    print("\nrmmod: %s" % (RMMOD_CMD))
    os.system(RMMOD_CMD)
    os.system(f"sudo dmesg > {os.path.join(LOG_DIR, 'rmmod_dmesg')}")
    return 0


# Usage: python3 umount.py
if __name__ == "__main__":
    os.chdir(WORK_DIR)
    if len(sys.argv) > 1 and sys.argv[1] == "rawdevice":
        ret = process(rawdevice=True)
    else:
        ret = process()
    if ret == 0:
        print("Umount Success")
