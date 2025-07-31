"""
------------------------------------------------------------------------------------------------------

                                               ,@@@@@@
                                            @@@@@@@@@@@            @@@
                                          @@@@@@@@@@@@      @@@@@@@@@@@
                                        @@@@@@@@@@@@@   @@@@@@@@@@@@@@
                                      @@@@@@@/         ,@@@@@@@@@@@@@
                                         /@@@@@@@@@@@@@@@  @@@@@@@@
                                    @@@@@@@@@@@@@@@@@@@@@@@@ @@@@@
                                @@@@@@@@                @@@@@
                              ,@@@                        @@@@&
                                             @@@@@@.       @@@@
                                   @@@     @@@@@@@@@/      @@@@@
                                   ,@@@.     @@@@@@((@     @@@@(
                                   //@@@        ,,  @@@@  @@@@@
                                   @@@(                @@@@@@@
                                   @@@  @          @@@@@@@@#
                                       @@@@@@@@@@@@@@@@@
                                      @@@@@@@@@@@@@(

Copyright (c) 2025 EyeTrackVR <3
LICENSE: Babble Software Distribution License 1.0
------------------------------------------------------------------------------------------------------
"""

import cv2
import numpy as np
import queue
import serial
import serial.tools.list_ports
import threading
import time
from colorama import Fore
from config import EyeTrackCameraConfig, EyeTrackSettingsConfig
from enum import Enum
from utils.misc_utils import get_camera_index_by_name, list_camera_names, COM_PORTS
import psutil, os
import sys


process = psutil.Process(os.getpid())  # set process priority to low
try:
    sys.getwindowsversion()
except AttributeError:
    process.nice(10)  # UNIX: 0 low 10 high
    process.nice()
else:
    process.nice(psutil.HIGH_PRIORITY_CLASS)  # Windows
    process.nice()
    # See https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getpriorityclass#return-value for values

WAIT_TIME = 0.1
# Serial communication protocol:
# header-begin (2 bytes)
# header-type (2 bytes)
# packet-size (2 bytes)
# packet (packet-size bytes)
ETVR_HEADER = b"\xff\xa0"
ETVR_HEADER_FRAME = b"\xff\xa1"
ETVR_HEADER_LEN = 6


class CameraState(Enum):
    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2

class Camera:
    def __init__(
        self,
        config: EyeTrackCameraConfig,
        camera_index: int,
        cam_changed: "threading.Event",
        cancellation_event: "threading.Event",
        capture_event: "threading.Event",
        camera_status_outgoing: "queue.Queue[CameraState]",
        camera_output_outgoing: "queue.Queue(maxsize=5)",
        settings: EyeTrackSettingsConfig,
    ):

        self.camera_status = CameraState.CONNECTING
        self.config = config
        self.settings = settings
        self.camera_index = camera_index
        self.cam_changed = cam_changed
        self.camera_address = config.capture_source
        self.camera_status_outgoing = camera_status_outgoing
        self.camera_output_outgoing = camera_output_outgoing
        self.capture_event = capture_event
        self.cancellation_event = cancellation_event
        self.current_capture_source = config.capture_source
        self.cv2_camera: "cv2.VideoCapture" = None

        self.serial_connection = None
        self.last_frame_time = time.time()
        self.frame_number = 0
        self.fps = 0
        self.bps = 0
        self.start = True
        self.buffer = b""
        self.pf_fps = 0
        self.prevft = 0
        self.newft = 0
        self.fl = [0]



        self.error_message = f"{Fore.YELLOW}[WARN] Capture source {{}} not found, retrying...{Fore.RESET}"

    def __del__(self):
        self.close_cams()

    def set_output_queue(self, camera_output_outgoing: "queue.Queue"):
        self.camera_output_outgoing = camera_output_outgoing

    def run(self):
        should_push = False # False: Dry run the loop without "pushing" the 1st image for processing ( Also set "False" for new capture sources so we set CONNECTED status correctly! )

        while True:
            if self.cancellation_event.is_set():
                print(f"{Fore.CYAN}[INFO] Exiting Capture thread{Fore.RESET}")
                # openCV won't switch to a new source if provided with one
                # so, we have to manually release the camera on exit
                break
            # If things aren't open, retry until they are. Don't let read requests come in any earlier
            # than this, otherwise we can deadlock ourselves.
            if self.config.capture_source not in (None, ""):
                if self.camera_status is not CameraState.CONNECTED or self.cam_changed.is_set():
                    # Limit reconnect attempts to 2 second intervals
                    if self.current_capture_source is None and self.cancellation_event.wait(2):
                        continue
                    # Change camera status here so user has a second to see the DISCONNECTED state
                    if self.camera_status is not CameraState.CONNECTING:
                        self.camera_status = CameraState.CONNECTING
                    if self.cam_changed.is_set():
                        self.cam_changed.clear()
                    self.frame_number = 0
                    self.close_cams()

                    cap_source = self.config.capture_source
                    cap_source_l = str(cap_source).casefold()
                    is_int = isinstance(cap_source, int)
                    # Serial
                    if not is_int and cap_source_l.startswith(COM_PORTS):
                        self.current_capture_source = cap_source
                        if self.start_serial_connection(cap_source):
                            self.current_capture_source = None  # Always set to "None" on fail condition!
                            continue
                        should_push = False # False: Dry run the loop without "pushing" the 1st image for processing ( Also set "False" for new capture sources so we set CONNECTED status correctly! )
                    # Camera, Stream, Video files, etc.
                    else:
                        # This requires a wait, otherwise we can error and possible screw up the camera firmware. Fickle things.
                        if self.cancellation_event.wait(WAIT_TIME):
                            continue
                        if not is_int and cap_source_l in [c.casefold() for c in list_camera_names()]:
                            self.current_capture_source = get_camera_index_by_name(cap_source)
                        else:
                            self.current_capture_source = cap_source
                        if self.current_capture_source is None:
                            print(self.error_message.format(cap_source))
                            continue

                        self.cv2_camera = cv2.VideoCapture()
                        self.cv2_camera.setExceptionMode(True)
                        try:
                            # HW acceleration is slower with our use-case. For most systems I'd recommend against using HW accelerated decoding. # TODO: Add gui_use_gpu_decoder var to settings tab
                            self.cv2_camera.open(self.current_capture_source, cv2.CAP_ANY, (cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY) if self.settings.gui_use_gpu_decoder else (cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_NONE))
                        except cv2.error:
                            self.close_cams()
                        if self.cv2_camera is None or not self.cv2_camera.isOpened():
                            print(self.error_message.format(cap_source))
                            self.current_capture_source = None  # Always set to "None" on fail condition!
                            continue
                        should_push = False # False: Dry run the loop without "pushing" the 1st image for processing ( Also set "False" for new capture sources so we set CONNECTED status correctly! )
            else:
                # We don't have a capture source to try yet, wait for one to show up in the GUI.
                if self.cam_changed.wait(3):
                    self.cam_changed.clear()
                continue
            # Assuming we can access our capture source, wait for another thread to request a capture.
            # Cycle every so often to see if our cancellation token has fired. This basically uses a python event as a context-less, resettable one-shot channel.
            # Using a higher timeout here suppresses "Cropping Mode" backpressure warnings, but masks underlying issues.. ( For debugging use: 0.008-0.02 )
            if should_push and not self.capture_event.wait(0.05):
                pass
            if self.current_capture_source is not None:
                if self.cv2_camera is not None:
                    self.get_cv2_camera_picture(should_push)
                elif self.serial_connection is not None:
                    self.get_serial_camera_picture(should_push)
                if self.camera_status is CameraState.DISCONNECTED:
                    self.current_capture_source = None  # Always set to "None" on fail condition!
                    continue
                if not should_push:
                    # if we get all the way down here, consider ourselves connected
                    self.camera_status = CameraState.CONNECTED
                    should_push = True
        # Set status 'n close open cameras before returning
        self.current_capture_source = None
        self.camera_status = CameraState.CONNECTING
        self.close_cams()

    def get_cv2_camera_picture(self, should_push):
        try:
            ret, image = self.cv2_camera.read()
            if not ret:
                raise RuntimeError("Problem while getting frame")
            height, width = image.shape[:2]  # Calculate the aspect ratio
            if int(width) > 680:
                aspect_ratio = float(width) / float(height)  # Determine the new height based on the desired maximum width
                new_height = int(680 / aspect_ratio)
                image = cv2.resize(image, (680, new_height))

            if should_push:
                self.frame_number = int(self.cv2_camera.get(cv2.CAP_PROP_POS_FRAMES))
                # Calculate FPS
                current_frame_time = time.time()    # Should be using "time.perf_counter()", not worth ~3x cycles?
                delta_time = current_frame_time - self.last_frame_time
                self.last_frame_time = current_frame_time
                current_fps = 1 / delta_time if delta_time > 0 else 0
                # Exponential moving average (EMA). ~1100ns savings, delicious..
                self.fps = 0.02 * current_fps + 0.98 * self.fps
                # Fake compressed size, since .nbytes returns the uncompressed size in memory
                self.bps = 0.0384 * image.nbytes * self.fps

                self.push_image_to_queue(image, self.frame_number, self.fps)
        except:
            print(
                f"{Fore.YELLOW}[WARN] Capture source problem, assuming camera disconnected, waiting for reconnect.{Fore.RESET}"
            )
            self.camera_status = CameraState.DISCONNECTED

    def get_next_packet_bounds(self):
        beg = -1
        while beg == -1:
            self.buffer += self.serial_connection.read(2048)
            beg = self.buffer.find(ETVR_HEADER + ETVR_HEADER_FRAME)
        # Discard any data before the frame header.
        if beg > 0:
            self.buffer = self.buffer[beg:]
            beg = 0
        # We know exactly how long the jpeg packet is
        end = int.from_bytes(self.buffer[4:6], signed=False, byteorder="little")
        self.buffer += self.serial_connection.read(end - len(self.buffer))
        return beg, end

    def get_next_jpeg_frame(self):
        beg, end = self.get_next_packet_bounds()
        jpeg = self.buffer[beg + ETVR_HEADER_LEN : end + ETVR_HEADER_LEN]
        self.buffer = self.buffer[end + ETVR_HEADER_LEN :]
        return jpeg



    def get_serial_camera_picture(self, should_push):
        conn = self.serial_connection
        try:
            if conn.in_waiting:
                jpeg = self.get_next_jpeg_frame()
                if jpeg:
                    # Create jpeg frame from byte string
                    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
                    if image is None:
                        print(f"{Fore.YELLOW}[WARN] Frame drop. Corrupted JPEG.{Fore.RESET}")
                        return

                    if should_push:
                        # Calculate FPS
                        current_frame_time = time.time()    # Should be using "time.perf_counter()", not worth ~3x cycles?
                        delta_time = current_frame_time - self.last_frame_time
                        self.last_frame_time = current_frame_time
                        current_fps = 1 / delta_time if delta_time > 0 else 0
                        # Exponential moving average (EMA). ~1100ns savings, delicious..
                        self.fps = 0.02 * current_fps + 0.98 * self.fps
                        self.bps = len(jpeg) * self.fps

                        self.push_image_to_queue(image, self.frame_number + 1, self.fps)
                # Discard the serial buffer. This is due to the fact that it,
                # may build up some outdated frames. A bit of a workaround here tbh.
                # Do this at the end to give buffer time to refill.
                if self.serial_connection.in_waiting >= 32768:
                    print(f"{Fore.CYAN}[INFO] Discarding the serial buffer ({conn.in_waiting} bytes){Fore.RESET}")
                    conn.reset_input_buffer()
                    self.buffer = b""
        except Exception:
            print(
                f"{Fore.YELLOW}[WARN] Serial capture source problem, assuming camera disconnected, waiting for reconnect.{Fore.RESET}"
            )
            self.camera_status = CameraState.DISCONNECTED

    def start_serial_connection(self, port):
        try:
            rate = 115200 if sys.platform == "darwin" else 3000000  # Higher baud rate not working on macOS
            conn = serial.Serial(baudrate=rate, port=port, xonxoff=False, dsrdtr=False, rtscts=False)
            # Set explicit buffer size for serial.
            if sys.platform == "win32":
                buffer_size = 32768
                conn.set_buffer_size(rx_size=buffer_size, tx_size=buffer_size)

            print(f"{Fore.CYAN}[INFO] ETVR Serial Tracker device connected on {port}{Fore.RESET}")
            self.serial_connection = conn
            self.camera_status = CameraState.CONNECTED
            return False
        except Exception:
            print(f"{Fore.CYAN}[INFO] Failed to connect on {port}{Fore.RESET}")
            self.camera_status = CameraState.DISCONNECTED
            return True

    def close_cams(self):
        if self.cv2_camera is not None:
            self.cv2_camera.release()
            self.cv2_camera = None
        if self.serial_connection is not None:
            self.serial_connection.close()
            self.serial_connection = None

    def push_image_to_queue(self, image, frame_number, fps):
        # If there's backpressure, just yell. We really shouldn't have this unless we start getting
        # some sort of capture event conflict though.
        qsize = self.camera_output_outgoing.qsize()
        if qsize > 1:
            print(
                f"{Fore.YELLOW}[WARN] CAPTURE QUEUE BACKPRESSURE OF {qsize}. CHECK FOR CRASH OR TIMING ISSUES IN ALGORITHM.{Fore.RESET}"
            )
        self.camera_output_outgoing.put((image, frame_number, fps))
        self.capture_event.clear()
