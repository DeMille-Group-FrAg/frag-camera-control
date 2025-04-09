from collections import deque
from contextlib import ExitStack
import logging
import traceback

import pco

from vmbpy import VmbSystem

class Alvium:
    """
    Interface to Allied Vision's Alvium cameras, using the Vimba X SDK.

    Due to the supremely irritating structure of the SDK, almost all calls to the camera must be executed inside two
    with statements, one for the SDK singleton and one for the camera itself. In particular, leaving the camera's
    context manager block stops image acquisition. This makes it almost impossible to abstract away this implementation
    detail. Additionally, acquiring the vmbpy singleton is quite slow. Thanks, Allied Vision!

    To deal with this limitation, code that wants to acquire images needs to use the class this way:

    >>> cam = Alvium("Camera ID")
    >>> with cam.start():
    ...     image = cam.read_image()
    """

    def __init__(self, camera_id):
        self.trigger_mode = "software"
        self.sensor_format = ""

        self.frame_queue = deque()

        with VmbSystem.get_instance() as vmb:
            self.cam = vmb.get_camera_by_id(camera_id)

            with self.cam:
                self.get_image_shape()
                self.binning = {"horizontal": self.cam.BinningHorizontal.get(), "vertical": self.cam.BinningVertical.get()}

                self.cam.AcquisitionMode.set("Continuous")
                self.cam.TriggerMode.set("On")
                self.cam.TriggerSelector.set("FrameStart")
                self.cam.TriggerSource.set("Software")

    def start(self):
        # Set up an ExitStack to hold onto the vmbpy.VmbSystem and vmbpy.Camera contexts
        vmb_contexts = ExitStack()
        vmb_contexts.enter_context(VmbSystem.get_instance())
        vmb_contexts.enter_context(self.cam)

        self.cam.start_streaming(self.queue_frame)

        vmb_contexts.callback(self.cam.stop_streaming)

        return vmb_contexts

    def queue_frame(self, cam, stream, frame):
        self.frame_queue.append(frame.as_numpy_ndarray().copy())
        cam.queue_frame(frame)

    def set_sensor_format(self, arg):
        print(f"Set sensor format {arg}")

    # conversion factor, which is 1/gain or number of electrons/count
    def set_conv_factor(self, arg):
        print(f"Set conv factor {arg}")

    def set_trigger_mode(self, text, checked):
        if checked:
            self.trigger_mode = text
            if text == "software":
                self.cam.TriggerSource.set("Software")
            elif text == "external TTL":
                self.cam.TriggerSource.set("Line0")

    def set_expo_time(self, expo_time):
        with VmbSystem.get_instance(), self.cam:
            self.cam.ExposureTime.set(expo_time * 1e6)

    def get_image_shape(self):
        with VmbSystem.get_instance(), self.cam:
            self.image_shape = {"xmax": self.cam.Width.get(), "ymax": self.cam.Height.get()}

    def set_binning(self, bin_h, bin_v):
        if not bin_h in range(1, 9) and bin_v in range(1, 9):
            raise ValueError(f"Binning must be between 1 and 8, was ({bin_h}, {bin_v})")

        with VmbSystem.get_instance(), self.cam:
            self.cam.BinningHorizontal.set(bin_h)
            self.cam.BinningVertical.set(bin_v)

        self.get_image_shape()

    def num_images_available(self):
        return len(self.frame_queue)

    def software_trigger(self):
        with VmbSystem.get_instance(), self.cam:
            self.cam.TriggerSoftware.run()

    def stop(self):
        with VmbSystem.get_instance(), self.cam:
            self.cam.stop_streaming()

    def read_image(self):
        if len(self.frame_queue) == 0:
            raise RuntimeError("No images available")
        return self.frame_queue.popleft()

    def close(self):
        print("Close")

# the class that handles camera interface (except taking images) and configuration
class pixelfly:
    def __init__(self, parent):
        self.parent = parent

        try:
            # due to some unknow issues in computer IO and the way pco package is coded,
            # an explicit assignment to "interface" keyword is required
            self.cam = pco.Camera(interface='USB 2.0')
        except Exception as err:
            logging.error(traceback.format_exc())
            logging.error("Can't open camera")
            return

        # initialize camera
        self.set_sensor_format(self.parent.defaults["sensor_format"]["default"])
        self.set_clock_rate(self.parent.defaults["clock_rate"]["default"])
        self.set_conv_factor(self.parent.defaults["conv_factor"]["default"])
        self.set_trigger_mode(self.parent.defaults["trigger_mode"]["default"], True)
        self.set_expo_time(self.parent.defaults["expo_time"].getfloat("default"))
        self.set_binning(self.parent.defaults["binning"].getint("horizontal_default"),
                        self.parent.defaults["binning"].getint("vertical_default"))
        self.set_image_shape()
        self.set_record_mode()

    def set_sensor_format(self, arg):
        self.sensor_format = arg
        format_cam = self.parent.defaults["sensor_format"][arg]
        self.cam.sdk.set_sensor_format(format_cam)
        self.cam.sdk.arm_camera()
        # print(f"sensor format = {arg}")

    def set_clock_rate(self, arg):
        rate = self.parent.defaults["clock_rate"].getint(arg)
        self.cam.configuration = {"pixel rate": rate}
        # print(f"clock rate = {arg}")

    # conversion factor, which is 1/gain or number of electrons/count
    def set_conv_factor(self, arg):
        conv = self.parent.defaults["conv_factor"].getint(arg)
        self.cam.sdk.set_conversion_factor(conv)
        self.cam.sdk.arm_camera()
        # print(f"conversion factor = {arg}")

    def set_trigger_mode(self, text, checked):
        if checked:
            self.trigger_mode = text
            mode_cam = self.parent.defaults["trigger_mode"][text]
            self.cam.configuration = {"trigger": mode_cam}
            # print(f"trigger source = {arg}")

    def set_expo_time(self, expo_time):
        self.cam.configuration = {'exposure time': expo_time}
        # print(f"exposure time (in seconds) = {expo_time}")

    # 4*4 binning at most
    def set_binning(self, bin_h, bin_v):
        self.binning = {"horizontal": int(bin_h), "vertical": int(bin_v)}
        self.cam.configuration = {'binning': (self.binning["horizontal"], self.binning["vertical"])}
        # print(f"binning = {bin_h} (horizontal), {bin_v} (vertical)")

    # image size of camera returned image, depends on sensor format and binning
    def set_image_shape(self):
        format_str = self.sensor_format + " absolute_"
        self.image_shape = {"xmax": int(self.parent.defaults["sensor_format"].getint(format_str+"xmax")/self.binning["horizontal"]),
                            "ymax": int(self.parent.defaults["sensor_format"].getint(format_str+"ymax")/self.binning["vertical"])}

    def num_images_available(self):
        return self.cam.rec.get_status()["dwProcImgCount"]

    def software_trigger(self):
        self.cam.sdk.force_trigger()

    def set_record_mode(self):
        self.cam.record(number_of_images=4, mode="ring buffer") # number_of_images is buffer size in ring buffer mode, and has to be at least 4

    def stop(self):
        self.cam.stop()

    def read_latest_image(self):
        return self.cam.image(image_index=0xFFFFFFFF)

    def close(self):
        self.cam.close()
