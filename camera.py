import logging
import traceback

import pco

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
