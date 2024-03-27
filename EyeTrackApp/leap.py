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

LEAP by: Prohurtz
Algorithm App Implementation By: Prohurtz

Copyright (c) 2023 EyeTrackVR <3
------------------------------------------------------------------------------------------------------
"""
#  LEAP = Lightweight Eyelid And Pupil
import os

os.environ["OMP_NUM_THREADS"] = "1"
import onnxruntime
import numpy as np
import cv2
import time
import math
from queue import Queue
import threading
from one_euro_filter import OneEuroFilter
import psutil, os
import sys
from utils.misc_utils import resource_path
import platform

frames = 0


def run_model(input_queue, output_queue, session):
    while True:
        frame = input_queue.get()
        if frame is None:
            break

        img_np = np.array(frame)
        img_np = img_np.astype(np.float32) / 255.0
        img_np = np.transpose(img_np, (2, 0, 1))
        img_np = np.expand_dims(img_np, axis=0)
        ort_inputs = {session.get_inputs()[0].name: img_np}
        pre_landmark = session.run(None, ort_inputs)

        pre_landmark = pre_landmark[1]
        pre_landmark = np.reshape(pre_landmark, (12, 2))
        output_queue.put((frame, pre_landmark))


class LEAP_C(object):
    def __init__(self):
        onnxruntime.disable_telemetry_events()
        # Config variables
        self.num_threads = 4  # Number of python threads to use (using ~1 more than needed to achieve wanted fps yields lower cpu usage)
        self.queue_max_size = 1  # Optimize for best CPU usage, Memory, and Latency. A maxsize is needed to not create a potential memory leak.
        if platform.system() == "Darwin":
            self.model_path = resource_path("Models/leap123023.onnx")  # funny MacOS files issues :P
        else:
            self.model_path = resource_path("Models\leap123023.onnx")
        self.interval = 1  # FPS print update rate
        self.low_priority = (
            False  # set process priority to low (may cause issues when unfocusing? reported by one, not reproducable)
        )
        self.low_priority = (
            True  # set process priority to low (may cause issues when unfocusing? reported by one, not reproducable)
        )
        self.print_fps = False
        # Init variables
        self.frames = 0
        self.queues = []
        self.threads = []
        self.model_output = np.zeros((12, 2))
        self.output_queue = Queue(maxsize=self.queue_max_size)
        self.start_time = time.time()

        for _ in range(self.num_threads):
            self.queue = Queue(maxsize=self.queue_max_size)
            self.queues.append(self.queue)

        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.optimized_model_filepath = ""

        if self.low_priority:
            try:
                process = psutil.Process(os.getpid())  # set process priority to low
                try:
                    sys.getwindowsversion()
                except AttributeError:
                    process.nice(0)  # UNIX: 0 low 10 high
                    process.nice()
                else:
                    process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)  # Windows
                    process.nice()
            except:
                pass
                # See https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getpriorityclass#return-value for values
        else:
            pass
        #    process = psutil.Process(os.getpid())  # set process priority to low
        #   try:
        #      sys.getwindowsversion()
        # except AttributeError:
        #    process.nice(10)  # UNIX: 0 low 10 high
        # else:
        #    process.nice(psutil.HIGH_PRIORITY_CLASS)  # Windows
        # See https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getpriorityclass#return-value for values

        min_cutoff = 0.1
        beta = 15.0
        # print(np.random.rand(22, 2))
        # noisy_point = np.array([1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1])
        self.one_euro_filter = OneEuroFilter(np.random.rand(12, 2), min_cutoff=min_cutoff, beta=beta)
        # self.one_euro_filter_open = OneEuroFilter(
        #   np.random.rand(1, 2), min_cutoff=0.01, beta=0.04
        # )
        self.dmax = 0
        self.dmin = 0
        self.openlist = []
        self.x = 0
        self.y = 0
        self.maxlist = []
        self.minlist = []

        self.ort_session1 = onnxruntime.InferenceSession(self.model_path, opts, providers=["CPUExecutionProvider"])
        # ort_session1 = onnxruntime.InferenceSession("C:/Users/beaul/PycharmProjects/EyeTrackVR/EyeTrackApp/Models/mommy062023.onnx", opts, providers=['DmlExecutionProvider'])
        threads = []
        for i in range(self.num_threads):
            thread = threading.Thread(
                target=run_model,
                args=(self.queues[i], self.output_queue, self.ort_session1),
                name=f"Thread {i}",
            )
            threads.append(thread)
            thread.start()

    def to_numpy(self, tensor):
        return tensor.detach().cpu().numpy() if tensor.requires_grad else tensor.cpu().numpy()

    def run_onnx_model(self, queues, session, frame):
        for i in range(len(queues)):
            if not queues[i].full():
                queues[i].put(frame)
                break

    def leap_run(self):

        img = self.current_image_gray_clean.copy()

        img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
        # img = imutils.rotate(img, angle=320)
        img_height, img_width = img.shape[:2]  # Move outside the loop

        frame = cv2.resize(img, (112, 112))
        imgvis = self.current_image_gray.copy()
        self.run_onnx_model(self.queues, self.ort_session1, frame)

        if not self.output_queue.empty():

            frame, pre_landmark = self.output_queue.get()
            pre_landmark = self.one_euro_filter(pre_landmark)
            # frame = cv2.resize(frame, (112, 112))

            for point in pre_landmark:
                x, y = point
                cv2.circle(imgvis, (int(x * img_width), int(y * img_height)), 2, (0, 0, 50), -1)
            cv2.circle(
                imgvis,
                tuple(int(x * img_width) for x in pre_landmark[2]),
                1,
                (255, 255, 0),
                -1,
            )
            #   cv2.circle(img, tuple(int(x*112) for x in pre_landmark[2]), 1, (255, 255, 0), -1)
            cv2.circle(
                imgvis,
                tuple(int(x * img_width) for x in pre_landmark[4]),
                1,
                (255, 255, 255),
                -1,
            )
            #   cv2.circle(img, tuple(int(x * 112) for x in pre_landmark[4]), 1, (255, 255, 255), -1)
            #    print(pre_landmark)

            x1, y1 = pre_landmark[0]
            x2, y2 = pre_landmark[6]
            #  euclidean_dist_width = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)

            x1, y1 = pre_landmark[1]
            x2, y2 = pre_landmark[3]

            x3, y3 = pre_landmark[4]
            x4, y4 = pre_landmark[2]

            # d = area / euclidean_dist_width
            #  print(area)
            # eyesize_dist = math.dist(pre_landmark[0], pre_landmark[6])
            # distance = math.dist(pre_landmark[1], pre_landmark[3])
            #  d = distance / eyesize_dist

            d1 = math.dist(pre_landmark[1], pre_landmark[3])

            d2 = math.dist(pre_landmark[2], pre_landmark[4])
            d = (d1 + d2) / 2
            # by averaging both sets we can get less error? i think part of why 1 eye is better than the other is because we only considered one offset points.
            # considering both should smooth things out between eyes

            try:
                if d >= np.percentile(
                    self.openlist, 80
                ):  # an aditional approach could be using the place where on average it is most stable, denoting what distance is the most stable "open"
                    self.maxlist.append(d)

                if d <= np.percentile(
                    self.openlist, 2
                ):  # an aditional approach could be using the place where on average it is most stable, denoting what distance is the most stable "open"
                    self.minlist.append(d)

                if len(self.maxlist) > 2000:
                    self.maxlist.pop(0)

                if len(self.minlist) > 2000:
                    self.minlist.pop(0)
                # this should be the average most open value, the average of top 200 values in rolling calibration
                # with this we can use it as the "openstate" (0.7, for expanded squeeze)

                # (x * weight_x + y * weight_y) / (weight_x + weight_y)

                normal_open = ((sum(self.maxlist) / len(self.maxlist)) * 0.90 + max(self.openlist) * 0.10) / (
                    0.95 + 0.15
                )
                normal_close = ((sum(self.minlist) / len(self.minlist)) * 0.05 + min(self.openlist) * 0.95) / (
                    0.05 + 0.95
                )

            except:
                normal_open = 0.8
                normal_close = 0.1
            # print(self.maxlist)

            if len(self.openlist) < 5000:  # TODO expose as setting?
                self.openlist.append(d)
            else:
                #  if d >= np.percentile(self.openlist, 99) or d <= np.percentile(
                #    self.openlist, 1
                # ):
                #    pass
                # else:
                self.openlist.pop(0)
                self.openlist.append(d)

            try:
                per = (d - normal_open) / (min(self.openlist) - normal_open)

                oldper = (d - max(self.openlist)) / (min(self.openlist) - max(self.openlist))

                per = 1 - per
                per = per - 0.2  # allow for eye widen? might require a more legit math way but this makes sense.
                per = min(per, 1.0)

                # if per <= 0.18:  # this should be tuned, i could make this auto calib based on min from a list of per values.
                #   per = 0.0
                print("new: ", per, "vs old: ", oldper)

            except:
                per = 0.8
                pass

            #    print(d, per)
            x = pre_landmark[6][0]
            y = pre_landmark[6][1]

            #  per = d - 0.1
            self.last_lid = per
            # pera = np.array([per, per])
            # self.one_euro_filter_open(pera)
            if per <= 0.2:  # TODO: EXPOSE AS SETTING
                per == 0.0
            # print(per)
            return imgvis, float(x), float(y), per

        imgvis = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        return imgvis, 0, 0, 0


class External_Run_LEAP(object):
    def __init__(self):
        self.algo = LEAP_C()

    def run(self, current_image_gray, current_image_gray_clean):
        self.algo.current_image_gray = current_image_gray
        self.algo.current_image_gray_clean = current_image_gray_clean
        img, x, y, per = self.algo.leap_run()
        return img, x, y, per
