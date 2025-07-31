import os
import typing
import sys
import serial
import serial.tools.list_ports
import glob
import platform
import cv2
import subprocess
import contextlib

from pathlib import Path
from typing import Union

# Keep lower cased!
COM_PORTS = ("com", "/dev/cu", "/dev/tty")  # Windows, MacOS, Linux

os_type = platform.system() # TODO: Replace is_nt's with this?
is_nt = True if os.name == "nt" else False


def PlaySound(*args, **kwargs):
    pass


SND_FILENAME = SND_ASYNC = 1

if is_nt:
    import winsound
    from pygrabber.dshow_graph import FilterGraph
    graph = FilterGraph()

    PlaySound = winsound.PlaySound
    SND_FILENAME = winsound.SND_FILENAME
    SND_ASYNC = winsound.SND_ASYNC


def clamp(x, low, high):
    return max(low, min(x, high))


def lst_median(lst, ordered=False):
    # https://github.com/emilianavt/OpenSeeFace/blob/6f24efc4f58eb7cca47ec2146d934eabcc207e46/remedian.py
    assert lst, "median needs a non-empty list"
    n = len(lst)
    p = q = n // 2
    if n < 3:
        p, q = 0, n - 1
    else:
        lst = lst if ordered else sorted(lst)
        if not n % 2:  # for even-length lists, use mean of mid 2 nums
            q = p - 1
    return lst[p] if p == q else (lst[p] + lst[q]) / 2


class FastMedian:
    # https://github.com/emilianavt/OpenSeeFace/blob/6f24efc4f58eb7cca47ec2146d934eabcc207e46/remedian.py
    # Initialization
    def __init__(self, inits: typing.Optional[typing.Sequence] = [], k=64):  # after some experimentation, 64 works ok
        self.all, self.k = [], k
        self.more, self.__median = None, None
        if inits is not None:
            [self + x for x in inits]

    # When full, push the median of current values to next list, then reset.
    def __add__(self, x):
        self.__median = None
        self.all.append(x)  # It would be faster to pre-allocate an array and assign it by index.
        if len(self.all) == self.k:
            self.more = self.more or FastMedian(k=self.k)
            self.more + self.__medianPrim(self.all)
            # It's going to be slower because of the re-allocation.
            self.all = []  # reset

    #  If there is a next list, ask its median. Else, work it out locally.
    def median(self):
        return self.more.median() if self.more else self.__medianPrim(self.all)

    # Only recompute median if we do not know it already.
    def __medianPrim(self, all):
        if self.__median is None:
            self.__median = lst_median(all, ordered=False)
        return self.__median

def resource_path(relative_path: Union[str, Path]) -> str:
    """
    Get absolute path to resource, works for dev and for PyInstaller
    """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = Path(sys._MEIPASS)
    except AttributeError:
        base_path = Path(".")

    return str(base_path / relative_path)

@contextlib.contextmanager
def suppress_stderr():
    """Context manager to suppress stderr (used for OpenCV warnings)."""
    with open(os.devnull, 'w') as devnull:
        old_stderr_fd = os.dup(2)
        os.dup2(devnull.fileno(), 2)
        try:
            yield
        finally:
            os.dup2(old_stderr_fd, 2)
            os.close(old_stderr_fd)

def list_cameras_opencv():
    """Use OpenCV to check available cameras by index (fallback for Linux/macOS)"""
    index = 0
    arr = []
    with suppress_stderr():  # tell OpenCV not to throw "cannot find camera" while we probe for cameras
        while True:
            cap = cv2.VideoCapture(index)
            if not cap.read()[0]:
                cap.release()
                break
            else:
                arr.append(f"/dev/video{index}")
                cap.release()
            index += 1
    return arr

def is_uvc_device(device):
    """Check if the device is a UVC video device (not metadata)"""
    try:
        result = subprocess.run(
            ["v4l2-ctl", f"--device={device}", "--all"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        output = result.stdout.decode("utf-8")

        # Check if "UVC Payload Header Metadata" is in the output
        if "UVC Payload Header Metadata" in output:
            return False  # It's metadata, not actual video
        return True  # It's a valid video device
    except Exception:
        return False


def list_linux_uvc_devices():
    """List UVC video devices on Linux (excluding metadata devices)"""
    try:
        # v4l2-ctl --list-devices breaks if video devices are non-sequential.
        # So this might be better?
        result = glob.glob("/dev/video*")
        devices = []
        for line in result:
            if is_uvc_device(line):
                devices.append(
                    line
                )  # We return the path like '/dev/video0'

        return devices

    except Exception as e:
        return [f"Error listing UVC devices on Linux: {str(e)}"]

def list_camera_names():
    """Cross-platform function to list camera names"""

    if is_nt:
        # On Windows, use pygrabber to list devices
        return graph.get_input_devices() + list_serial_ports()

    elif os_type == "Linux":
        # On Linux, return UVC device paths like '/dev/video0'
        return list_linux_uvc_devices() + list_serial_ports()

    elif os_type == "Darwin":
        # On macOS, fallback to OpenCV (device names aren't fetched)
        return list_cameras_opencv() + list_serial_ports()

    else:
        return ["Unsupported operating system"]

def list_serial_ports():
    #print("DEBUG: Listed Serial Ports")
    """ Lists serial port names

        :raises EnvironmentError:
            On unsupported or unknown platforms
        :returns:
            A list of the serial ports available on the system
    """
    if not sys.platform.startswith(("win", "linux", "cygwin", "darwin")):
        raise EnvironmentError("Unsupported platform")

    try:
        ports = [s.device for s in serial.tools.list_ports.comports()]
    except (AttributeError, OSError, serial.SerialException):
        pass
    return sorted(ports)

def get_camera_index_by_name(name):
    """Cross-platform function to get the camera index by its name or path"""
    cam_list = list_camera_names()

    # On Linux, we use device paths like '/dev/video0' and match directly
    # OpenCV expects the actual /dev/video#, not the offset into the device list
    if os_type == "Linux":
        if (name.startswith("/dev/ttyACM")):
            return int(str.replace(name,"/dev/ttyACM",""))
        else:
            return int(str.replace(name,"/dev/video",""))

    # On Windows, match by camera name
    elif os_type == 'Windows':
        for i, device_name in enumerate(cam_list):
            if device_name == name:
                return i

    # On macOS or other systems, fallback to OpenCV device index
    elif os_type == "Darwin":
        for i, device in enumerate(cam_list):
            if device == name:
                return i

    return None