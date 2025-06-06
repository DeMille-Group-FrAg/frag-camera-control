import sys
import ctypes
import h5py
import time
import logging
import configparser
import numpy as np
from scipy import optimize
import PyQt5
import pyqtgraph as pg
import PyQt5.QtWidgets as qt
import os
import qdarkstyle # see https://github.com/ColinDuquesnoy/QDarkStyleSheet
import socket
import selectors
import struct
from collections import deque
from PyQt5.QtGui import QIcon
import vmbpy

from camera import Alvium
from widgets import NewSpinBox, NewDoubleSpinBox, NewComboBox, Scrollarea, imageWidget

window_icon_name = 'FVEY_Rosette.ico'

def gaussian(amp, x_mean, y_mean, x_width, y_width, offset):
    x_width = float(x_width)
    y_width = float(y_width)

    return lambda x, y: amp*np.exp(-0.5*((x-x_mean)/x_width)**2-0.5*((y-y_mean)/y_width)**2) + offset

# return a 2D gaussian fit
# generally a 2D gaussian fit can have 7 params, 6 of them are implemented here (the excluded one is an angle)
# codes adapted from https://scipy-cookbook.readthedocs.io/items/FittingData.html
def gaussianfit(data):
    # calculate moments for initial guess
    total = np.sum(data)
    X, Y = np.indices(data.shape)
    x_mean = np.sum(X*data)/total
    x_mean = np.clip(x_mean, 0, data.shape[0]-1) # coerce x_mean to data shape
    y_mean = np.sum(Y*data)/total
    y_mean = np.clip(y_mean, 0, data.shape[1]-1) # coerce y_mean to data shape
    col = data[:, int(y_mean)]
    x_width = np.sqrt(np.abs((np.arange(col.size)-x_mean)**2*col).sum()/col.sum())
    row = data[int(x_mean), :]
    y_width = np.sqrt(np.abs((np.arange(row.size)-y_mean)**2*row).sum()/row.sum())
    offset = (data[0, :].sum()+data[-1, :].sum()+data[:, 0].sum()+data[:, -1].sum())/np.sum(data.shape)/2
    amp = data.max() - offset

    # use optimize function to obtain 2D gaussian fit
    errorfunction = lambda p: np.ravel(gaussian(*p)(*np.indices(data.shape))-data)
    p, success = optimize.leastsq(errorfunction, (amp, x_mean, y_mean, x_width, y_width, offset))

    p_dict = {}
    p_dict["x_mean"] = p[1]
    p_dict["y_mean"] = p[2]
    p_dict["x_width"] = p[3]
    p_dict["y_width"] = p[4]
    p_dict["amp"] = p[0]
    p_dict["offset"] = p[5]
    p_dict["peak"] = np.max(data)

    return p_dict


# this thread handles TCP communication with another PC, it starts when this program starts
# code is from https://github.com/qw372/Digital-transfer-cavity-laser-lock/blob/8db28c2edd13c2c474d68c4b45c8f322f94f909d/main.py#L1385
class TcpThread(PyQt5.QtCore.QThread):
    update_signal = PyQt5.QtCore.pyqtSignal(dict)
    start_signal = PyQt5.QtCore.pyqtSignal()
    stop_signal = PyQt5.QtCore.pyqtSignal()

    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.data = bytes()
        self.length_get = False
        self.host = self.parent.defaults["tcp_connection"]["host_addr"]
        self.port = self.parent.defaults["tcp_connection"].getint("port")
        self.sel = selectors.DefaultSelector()

        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Avoid bind() exception: OSError: [Errno 48] Address already in use
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind((self.host, self.port))
        self.server_sock.listen()
        logging.info(f"listening on: {(self.host, self.port)}")
        self.server_sock.setblocking(False)
        self.sel.register(self.server_sock, selectors.EVENT_READ, data=None)

    def run(self):
        while self.parent.control.tcp_active:
            events = self.sel.select(timeout=0.1)
            for key, mask in events:
                if key.data is None:
                    # this event is from self.server_sock listening
                    self.accept_wrapper(key.fileobj)
                else:
                    s = key.fileobj
                    try:
                        data = s.recv(1024) # 1024 bytes should be enough for our data
                    except Exception as err:
                        logging.error(f"TCP connection error: \n{err}")
                        data = None
                    if data:
                        self.data += data
                        while len(self.data) > 0:
                            if (not self.length_get) and len(self.data) >= 4:
                                self.length = struct.unpack(">I", self.data[:4])[0]
                                self.length_get = True
                                self.data = self.data[4:]
                            elif self.length_get and len(self.data) >= self.length:
                                message = self.data.decode('utf-8')
                                # logging.info(message)
                                if message == "Status?":
                                    # if it's just a check in message to test connection
                                    re = "Running" if self.parent.control.active else "Idle"
                                    try:
                                        s.sendall(re.encode('utf-8'))
                                    except Exception as err:
                                        logging.error(f"(tcp thread) Failed to reply the message. \n{err}")
                                elif message == "Stop":
                                    # if it's to stop running
                                    self.stop_signal.emit()
                                else:
                                    # if it's a message about scan sequence
                                    with open(self.parent.defaults["scan_file_name"]["default"], "w") as f:
                                        f.write(message)

                                    # turn on the camera here
                                    self.start_signal.emit()
                                    time.sleep(0.2)

                                    try:
                                        s.sendall("Received".encode('utf-8'))
                                    except Exception as err:
                                        logging.error(f"(tcp thread) Failed to reply the message. \n{err}")
                                t = time.time()
                                time_string = time.strftime("%Y-%m-%d  %H:%M:%S.", time.localtime(t))
                                time_string += "{:1.0f}".format((t%1)*10) # get 0.1 s time resolution
                                return_dict = {"last write": time_string}
                                self.update_signal.emit(return_dict)
                                self.data = self.data[self.length:]
                                self.length_get = False
                            else:
                                break
                    else:
                        # empty data will be interpreted as the signal of client shutting down
                        logging.info("client shutting down...")
                        self.sel.unregister(s)
                        s.close()
                        self.length_get = False
                        self.data = bytes()

        self.sel.unregister(self.server_sock)
        self.server_sock.close()
        self.sel.close()

    def accept_wrapper(self, sock):
        conn, addr = sock.accept()  # Should be ready to read
        logging.info(f"accepted connection from: {addr}")
        conn.setblocking(False)
        self.sel.register(conn, selectors.EVENT_READ, data=123) # In this application, 'data' keyword can be anything but None
        return_dict = {"client addr": addr}
        self.update_signal.emit(return_dict)

# the thread called when the program starts to interface with camera and take images
# this thread waits unitl a new image is available and read it out from the camera
class CamThread(PyQt5.QtCore.QThread):
    signal = PyQt5.QtCore.pyqtSignal(dict)

    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.image_order = [x.strip() for x in self.parent.defaults["measurement"]["image_order"].split(',')]
        self.pixeltomm = self.parent.defaults["atomic"].getfloat('pixeltomm')
        self.cross_section = self.parent.defaults["atomic"].getfloat('cross_section')
        self.counter_limit = self.parent.control.num_img_to_take*len(self.image_order)
        self.counter = 0
        self.img_dict = {}
        
        if self.parent.control.control_mode == "record":
            self.signal_count_list = []
            self.img_ave = np.zeros((self.parent.device.image_shape["xmax"], self.parent.device.image_shape["ymax"]))
        elif self.parent.control.control_mode == "scan":
            self.signal_count_dict = {}

        self.scan_config = self.parent.control.scan_config
        self.ave_bkg = None
        self.bkg_counter = 0
        self.last_time = time.time()

    def run(self):
        with self.parent.device.start():
            while self.counter < self.counter_limit and self.parent.control.active:
                if self.parent.device.trigger_mode == "software":
                    self.parent.device.software_trigger() # software-ly trigger the camera
                    time.sleep(0.5)

                print(self.counter)
                while self.parent.device.num_images_available() == 0:
                    # wait until a new image is available,
                    # this step will block the thread, so it can't be in the main thread
                    time.sleep(0.001)

                if self.parent.control.active:
                    print('made it!')
                    image = self.parent.device.read_image().T
                    image_type = self.image_order[self.counter%2] # odd-numbered image is signal, even-numbered image is background
                    # convert the image data type to float, to avoid overflow
                    print(image_type)
                    image = image.astype("float")
                    # Software ROI - commented out; replace with calls to hardware ROI? (WEC 2025-04-21)
                    # xstart = int(image.shape[0]/2 - self.parent.device.image_shape['xmax']/2)
                    # ystart = int(image.shape[1]/2 - self.parent.device.image_shape['ymax']/2)
                    # image = image[xstart : xstart+self.parent.device.image_shape['xmax'],
                    #                 ystart : ystart+self.parent.device.image_shape['ymax']]

                    if image_type == "background":
                        self.image_bg = image
                        self.img_dict["type"] = "background"
                        self.img_dict["counter"] = self.counter
                        self.img_dict["image"] = image
                        print('Taking bkg')

                    elif image_type == "signal":
                        self.image_signal = image
                        self.img_dict["type"] = "signal"
                        self.img_dict["counter"] = self.counter
                        self.img_dict["image"] = image

                    else:
                        logging.warning("Measurement type not supported.")
                        return

                    num = int(self.counter/len(self.image_order)+1) #num is number of post-proceed image (ie signal - background)
                    if self.parent.control.control_mode == "scan":
                        # value of the scan parameter
                        scan_param = self.parent.control.scan_config[f"scan_value_{num-1}"][self.parent.control.scan_elem_name]
                        self.img_dict["scan_param"] = scan_param

                    if self.counter%2 == 1: #checking to see if this is the second image taken
                        if self.parent.control.meas_mode == "fluorescence":
                            image_post = self.image_signal - self.image_bg
                            image_post_roi = image_post[self.parent.control.roi["xmin"] : self.parent.control.roi["xmax"],
                                                            self.parent.control.roi["ymin"] : self.parent.control.roi["ymax"]]

                            sc = np.sum(image_post_roi) # signal count
                        elif self.parent.control.meas_mode == "absorption":
                            self.image_bg = np.clip(self.image_bg, 1, None)
                            self.image_signal = np.clip(self.image_signal, 1, None)
                            image_post = np.divide(self.image_signal, self.image_bg) #avoid divide by zero errors
                            image_post = -np.log(image_post)
                            image_post_roi = image_post[self.parent.control.roi["xmin"] : self.parent.control.roi["xmax"],
                                                            self.parent.control.roi["ymin"] : self.parent.control.roi["ymax"]]
                            sc = np.sum(image_post_roi)*(self.pixeltomm)**2/self.cross_section # signal count
                        else:
                            logging.warning("Measurement type not supported.")
                            return

                        #num = int(self.counter/len(self.image_order)+1)

                        self.img_dict["num_image"] = num
                        self.img_dict["image_post"] = image_post
                        self.img_dict["image_post_roi"] = image_post_roi
                        self.img_dict["signal_count"] = np.format_float_scientific(sc, precision=4)
                        self.img_dict["signal_count_raw"] = sc

                        if self.parent.control.control_mode == "record":
                            # a list to save signal count of every single image
                            self.signal_count_list.append(sc)
                            # the average image
                            self.img_ave = np.average(np.array([self.img_ave, self.img_dict["image_post"]]), axis=0, weights=[(num-1)/num, 1/num])
                            self.img_dict["image_ave"] = self.img_ave
                            # signal count statistics, mean and error of mean = stand. dev. / sqrt(image number)
                            self.img_dict["signal_count_ave"] = np.format_float_scientific(np.mean(self.signal_count_list), precision=4)
                            self.img_dict["signal_count_err"] = np.format_float_scientific(np.std(self.signal_count_list)/np.sqrt(num), precision=4)
                        elif self.parent.control.control_mode == "scan":
                            # a dictionary that saves values of scan parameters as keys and a list of signal counts of corresponding images as vals
                            if scan_param in self.signal_count_dict:
                                self.signal_count_dict[scan_param] = np.append(self.signal_count_dict[scan_param], sc)
                            else:
                                self.signal_count_dict[scan_param] = np.array([sc])
                            self.img_dict["signal_count_scan"] = self.signal_count_dict

                    # transfer saved data back to main thread by signal-slot mechanism
                    self.signal.emit(self.img_dict)

                    self.counter += 1
                    # If I call "update imge" function here to update images in main thread, it sometimes work but sometimes not.
                    # It may be because PyQt is not thread safe. A signal-slot way seemed to be preferred,
                    # e.g. https://stackoverflow.com/questions/54961905/real-time-plotting-using-pyqtgraph-and-threading

                    logging.info(f"image {self.counter}: "+"{:.5f} s".format(time.time()-self.last_time))

# the class that places elements in UI and handles data processing
class Control(Scrollarea):
    def __init__(self, parent):
        super().__init__(parent, label="", type="vbox")
        self.setMaximumWidth(400)
        self.frame.setContentsMargins(0,0,0,0)

        # interpret data as fluorescence or optical density
        self.meas_mode = self.parent.defaults["measurement"].get("default")

        # number of pixels of the largest image we can do gaussian fit to in real time (i.e. updating in every experimental cycle)
        # it depends on CPU power and duration of experimental cycle
        self.cpu_limit = self.parent.defaults["gaussian_fit"].getint("cpu_limit")

        # number of images to take in each run
        self.num_img_to_take = self.parent.defaults["image_number"].getint("default")

        # image region of interest
        self.roi = {"xmin": self.parent.defaults["roi"].getint("xmin"),
                    "xmax": self.parent.defaults["roi"].getint("xmax"),
                    "ymin": self.parent.defaults["roi"].getint("ymin"),
                    "ymax": self.parent.defaults["roi"].getint("ymax")}

        # gaussian filter settings
        self.gaussian_fit = self.parent.defaults["gaussian_fit"].getboolean("default")
        self.gaussian_filter = self.parent.defaults["gaussian_filter"].getboolean("state")
        self.gaussian_filter_sigma = self.parent.defaults["gaussian_filter"].getfloat("sigma")

        self.img_save = self.parent.defaults["image_save"].getboolean("default")

        # boolean variable, turned on when the camera starts to take images
        self.active = False

        # control mode, can be "record" or "scan" in current implementation
        self.control_mode = None

        # boolean variable, turned on when the TCP thread is started
        self.tcp_active = False

        # save signal count
        self.signal_count_deque = deque([], maxlen=20)

        # places GUI elements
        self.place_recording()
        self.place_image_control()
        self.place_cam_control()
        self.place_tcp_control()
        self.place_save_load()

        # don't start tcp thread here, 
        # it will be started when the program load latest setting (using load_settings(latest=true))
        
        # self.tcp_start()

    # place recording gui elements
    def place_recording(self):
        record_box = qt.QGroupBox("Recording")
        record_box.setStyleSheet("QGroupBox {border: 1px solid #304249;}")
        record_box.setMaximumHeight(270)
        record_frame = qt.QGridLayout()
        record_box.setLayout(record_frame)
        self.frame.addWidget(record_box)

        self.record_bt = qt.QPushButton("Record")
        self.record_bt.clicked[bool].connect(lambda val, mode="record": self.start(mode))
        record_frame.addWidget(self.record_bt, 0, 0)
        self.record_bt.setEnabled(True)

        self.scan_bt = qt.QPushButton("Scan")
        self.scan_bt.clicked[bool].connect(lambda val, mode="scan": self.start(mode))
        record_frame.addWidget(self.scan_bt, 0, 1)
        self.scan_bt.setEnabled(False)

        self.stop_bt = qt.QPushButton("Stop")
        self.stop_bt.clicked[bool].connect(lambda val: self.stop())
        record_frame.addWidget(self.stop_bt, 0, 2)
        self.stop_bt.setEnabled(False)

        record_frame.addWidget(qt.QLabel("Measurement:"), 1, 0, 1, 1)
        self.meas_rblist = []
        meas_bg = qt.QButtonGroup(self.parent)
        op = [x.strip() for x in self.parent.defaults["measurement"]["options"].split(',')]
        for j, i in enumerate(op):
            meas_rb = qt.QRadioButton(i)
            meas_rb.setFixedHeight(30)
            meas_rb.setChecked(True if i == self.meas_mode else False)
            meas_rb.toggled[bool].connect(lambda val, rb=meas_rb: self.set_meas_mode(rb.text(), val))
            self.meas_rblist.append(meas_rb)
            meas_bg.addButton(meas_rb)
            record_frame.addWidget(meas_rb, 1, 1+j, 1, 1)

        # display signal count in real time
        record_frame.addWidget(qt.QLabel("Signal count:"), 2, 0, 1, 1)
        self.signal_count = qt.QLabel()
        self.signal_count.setText("0")
        self.signal_count.setStyleSheet("QLabel{background-color: gray; font: 20pt}")
        self.signal_count.setToolTip("Singal after bkg subtraction or OD")
        record_frame.addWidget(self.signal_count, 2, 1, 1, 2)

        # display mean of signal count in real time in "record" mode
        record_frame.addWidget(qt.QLabel("Signal mean:"), 3, 0, 1, 1)
        self.signal_count_mean = qt.QLabel()
        self.signal_count_mean.setText("0")
        self.signal_count_mean.setStyleSheet("QLabel{background-color: gray; font: 20pt}")
        self.signal_count_mean.setToolTip("Signal after bkg subtraction or OD")
        record_frame.addWidget(self.signal_count_mean, 3, 1, 1, 2)

        # display error of mean of signal count in real time in "record" mode
        record_frame.addWidget(qt.QLabel("Signal error:"), 4, 0, 1, 1)
        self.signal_count_err_mean = qt.QLabel()
        self.signal_count_err_mean.setText("0")
        self.signal_count_err_mean.setStyleSheet("QLabel{background-color: gray; font: 20pt}")
        self.signal_count_err_mean.setToolTip("Signal after bkg subtraction or OD")
        record_frame.addWidget(self.signal_count_err_mean, 4, 1, 1, 2)

    # place image control gui elements
    def place_image_control(self):
        img_ctrl_box = qt.QGroupBox("Image Control")
        img_ctrl_box.setStyleSheet("QGroupBox {border: 1px solid #304249;}")
        img_ctrl_frame = qt.QFormLayout()
        img_ctrl_box.setLayout(img_ctrl_frame)
        self.frame.addWidget(img_ctrl_box)

        # a spinbox to set number of images to take in next run
        num_img_upperlimit = self.parent.defaults["image_number"].getint("max")
        self.num_img_to_take_sb = NewSpinBox(range=(1, num_img_upperlimit), suffix=None)
        self.num_img_to_take_sb.setValue(self.num_img_to_take)
        self.num_img_to_take_sb.valueChanged[int].connect(lambda val: self.set_num_img(val))
        img_ctrl_frame.addRow("Num of image to take:", self.num_img_to_take_sb)

        # spinboxes to set image region of interest in x
        self.x_min_sb = NewSpinBox(range=(0, self.roi["xmax"]-1), suffix=None)
        self.x_min_sb.setValue(self.roi["xmin"])
        self.x_max_sb = NewSpinBox(range=(self.roi["xmin"]+1, self.parent.device.image_shape["xmax"]), suffix=None)
        self.x_max_sb.setValue(self.roi["xmax"])
        self.x_min_sb.valueChanged[int].connect(lambda val, text='xmin', sb=self.x_max_sb:
                                                self.set_roi(text, val, sb))
        self.x_max_sb.valueChanged[int].connect(lambda val, text='xmax', sb=self.x_min_sb:
                                                self.set_roi(text, val, sb))

        x_range_box = qt.QWidget()
        x_range_layout = qt.QHBoxLayout()
        x_range_layout.setContentsMargins(0,0,0,0)
        x_range_box.setLayout(x_range_layout)
        x_range_layout.addWidget(self.x_min_sb)
        x_range_layout.addWidget(self.x_max_sb)
        img_ctrl_frame.addRow("ROI X range:", x_range_box)

        # spinboxes to set image region of interest in y
        self.y_min_sb = NewSpinBox(range=(0, self.roi["ymax"]-1), suffix=None)
        self.y_min_sb.setValue(self.roi["ymin"])
        self.y_max_sb = NewSpinBox(range=(self.roi["ymin"]+1, self.parent.device.image_shape["ymax"]), suffix=None)
        self.y_max_sb.setValue(self.roi["ymax"])
        self.y_min_sb.valueChanged[int].connect(lambda val, text='ymin', sb=self.y_max_sb:
                                                self.set_roi(text, val, sb))
        self.y_max_sb.valueChanged[int].connect(lambda val, text='ymax', sb=self.y_min_sb:
                                                self.set_roi(text, val, sb))

        y_range_box = qt.QWidget()
        y_range_layout = qt.QHBoxLayout()
        y_range_layout.setContentsMargins(0,0,0,0)
        y_range_box.setLayout(y_range_layout)
        y_range_layout.addWidget(self.y_min_sb)
        y_range_layout.addWidget(self.y_max_sb)
        img_ctrl_frame.addRow("ROI Y range:", y_range_box)

        # display number of images that have been taken
        self.num_image = qt.QLabel()
        self.num_image.setText("0")
        self.num_image.setStyleSheet("background-color: gray;")
        img_ctrl_frame.addRow("Num of recorded images:", self.num_image)

        # set hdf group name and whether to save image to a hdf file
        self.run_name_le = qt.QLineEdit()
        default_run_name = self.parent.defaults["image_save"]["run_name"]
        self.run_name_le.setText(default_run_name)
        self.run_name_le.setToolTip("HDF group name/run name")
        self.img_save_chb = qt.QCheckBox()
        self.img_save_chb.setTristate(False)
        self.img_save_chb.setChecked(self.img_save)
        self.img_save_chb.setStyleSheet("QCheckBox::indicator {width: 15px; height: 15px;}")
        self.img_save_chb.stateChanged[int].connect(lambda state: self.set_img_save(state))
        img_save_box = qt.QWidget()
        img_save_layout = qt.QHBoxLayout()
        img_save_layout.setContentsMargins(0,0,0,0)
        img_save_box.setLayout(img_save_layout)
        img_save_layout.addWidget(self.run_name_le)
        img_save_layout.addWidget(self.img_save_chb)
        img_ctrl_frame.addRow("Image auto save:", img_save_box)

        img_ctrl_frame.addRow("------------------", qt.QWidget())

        # set whether to apply gaussian filter
        self.gauss_filter_chb = qt.QCheckBox()
        self.gauss_filter_chb.setTristate(False)
        self.gauss_filter_chb.setChecked(self.gaussian_filter)
        self.gauss_filter_chb.setStyleSheet("QCheckBox::indicator {width: 15px; height: 15px;}")
        self.gauss_filter_chb.stateChanged[int].connect(lambda val, param="state": self.set_gauss_filter(val, param))
        img_ctrl_frame.addRow("gaussian filter:", self.gauss_filter_chb)

        # spinboxes to set gaussian filter sigma
        self.gaussian_filter_sigma_dsb = NewDoubleSpinBox(range=(0.01, 10000), decimals=2, suffix=None)
        self.gaussian_filter_sigma_dsb.setValue(self.gaussian_filter_sigma)
        self.gaussian_filter_sigma_dsb.valueChanged[float].connect(lambda val, param="sigma": self.set_gauss_filter(val, param))
        img_ctrl_frame.addRow("gaussian filter sigma:", self.gaussian_filter_sigma_dsb)

        img_ctrl_frame.addRow("------------------", qt.QWidget())

        # set whether to do gaussian fit in real time
        self.gauss_fit_chb = qt.QCheckBox()
        self.gauss_fit_chb.setTristate(False)
        self.gauss_fit_chb.setChecked(self.gaussian_fit)
        self.gauss_fit_chb.setStyleSheet("QCheckBox::indicator {width: 15px; height: 15px;}")
        self.gauss_fit_chb.stateChanged[int].connect(lambda state: self.set_gauss_fit(state))
        self.gauss_fit_chb.setToolTip(f"Can only be enabled when image size less than {self.cpu_limit} pixels.")
        img_ctrl_frame.addRow("2D gaussian fit:", self.gauss_fit_chb)

        if (self.roi["xmax"]-self.roi["xmin"])*(self.roi["ymax"]-self.roi["ymin"]) > self.cpu_limit:
            # this line has to be after gauss_fit_chb's connect()
            self.gauss_fit_chb.setChecked(False)
            self.gauss_fit_chb.setEnabled(False)

        # display 2D gaussian fit results
        self.x_mean = qt.QLabel()
        self.x_mean.setMaximumWidth(90)
        self.x_mean.setText("0")
        self.x_mean.setStyleSheet("QWidget{background-color: gray;}")
        self.x_mean.setToolTip("x mean")
        self.x_stand_dev = qt.QLabel()
        self.x_stand_dev.setMaximumWidth(90)
        self.x_stand_dev.setText("0")
        self.x_stand_dev.setStyleSheet("QWidget{background-color: gray;}")
        self.x_stand_dev.setToolTip("x standard deviation")
        gauss_x_box = qt.QWidget()
        gauss_x_layout = qt.QHBoxLayout()
        gauss_x_layout.setContentsMargins(0,0,0,0)
        gauss_x_box.setLayout(gauss_x_layout)
        gauss_x_layout.addWidget(self.x_mean)
        gauss_x_layout.addWidget(self.x_stand_dev)
        img_ctrl_frame.addRow("2D gaussian fit (x):", gauss_x_box)

        self.y_mean = qt.QLabel()
        self.y_mean.setMaximumWidth(90)
        self.y_mean.setText("0")
        self.y_mean.setStyleSheet("QWidget{background-color: gray;}")
        self.y_mean.setToolTip("y mean")
        self.y_stand_dev = qt.QLabel()
        self.y_stand_dev.setMaximumWidth(90)
        self.y_stand_dev.setText("0")
        self.y_stand_dev.setStyleSheet("QWidget{background-color: gray;}")
        self.y_stand_dev.setToolTip("y standard deviation")
        gauss_y_box = qt.QWidget()
        gauss_y_layout = qt.QHBoxLayout()
        gauss_y_layout.setContentsMargins(0,0,0,0)
        gauss_y_box.setLayout(gauss_y_layout)
        gauss_y_layout.addWidget(self.y_mean)
        gauss_y_layout.addWidget(self.y_stand_dev)
        img_ctrl_frame.addRow("2D gaussian fit (y):", gauss_y_box)

        self.amp = qt.QLabel()
        self.amp.setText("0")
        self.amp.setStyleSheet("QWidget{background-color: gray;}")
        img_ctrl_frame.addRow("2D gaussian fit (amp.):", self.amp)

        self.offset = qt.QLabel()
        self.offset.setText("0")
        self.offset.setStyleSheet("QWidget{background-color: gray;}")
        img_ctrl_frame.addRow("2D gaussian fit (offset):", self.offset)
        
        self.peak = qt.QLabel()
        self.peak.setText("0")
        self.peak.setStyleSheet("QWidget{background-color: gray;}")
        img_ctrl_frame.addRow("Peak Signal:", self.peak)

    # place camera control gui elements
    def place_cam_control(self):
        self.cam_ctrl_box = qt.QGroupBox("Camera Control")
        self.cam_ctrl_box.setStyleSheet("QGroupBox {border: 1px solid #304249;}")
        cam_ctrl_frame = qt.QFormLayout()
        self.cam_ctrl_box.setLayout(cam_ctrl_frame)
        self.frame.addWidget(self.cam_ctrl_box)

        # set trigger mode
        self.trig_mode_rblist = []
        trig_bg = qt.QButtonGroup(self.parent)
        self.trig_box = qt.QWidget()
        self.trig_box.setMaximumWidth(200)
        trig_layout = qt.QHBoxLayout()
        trig_layout.setContentsMargins(0,0,0,0)
        self.trig_box.setLayout(trig_layout)
        op = [x.strip() for x in self.parent.defaults["trigger_mode"]["options"].split(',')]
        for i in op:
            trig_mode_rb = qt.QRadioButton(i)
            trig_mode_rb.setChecked(True if i == self.parent.device.trigger_mode else False)
            trig_mode_rb.toggled[bool].connect(lambda val, rb=trig_mode_rb: self.parent.device.set_trigger_mode(rb.text(), val))
            self.trig_mode_rblist.append(trig_mode_rb)
            trig_bg.addButton(trig_mode_rb)
            trig_layout.addWidget(trig_mode_rb)
        cam_ctrl_frame.addRow("Trigger mode:", self.trig_box)

        # set exposure time and unit
        expo_cf = self.parent.defaults["expo_time"]
        default_unit = self.parent.defaults["expo_unit"]["default"]
        default_unit_num = self.parent.defaults["expo_unit"].getfloat(default_unit)
        default_time = expo_cf.getfloat("default")/default_unit_num
        self.expo_dsb = NewDoubleSpinBox(range=(expo_cf.getfloat("min")/default_unit_num, expo_cf.getfloat("max")/default_unit_num), decimals=int(expo_cf.getint("decimals")+np.log10(default_unit_num)))
        self.expo_dsb.setValue(default_time)
        self.expo_unit_cb = NewComboBox()
        self.expo_unit_cb.setMaximumHeight(30)
        op = [x.strip() for x in self.parent.defaults["expo_unit"]["options"].split(',')]
        self.expo_unit_cb.addItems(op)
        self.expo_unit_cb.setCurrentText(default_unit)
        self.expo_dsb.valueChanged[float].connect(lambda val, cb=self.expo_unit_cb, type="time":
                                            self.set_expo_time(val, cb.currentText(), type))
        self.expo_unit_cb.currentTextChanged[str].connect(lambda val, dsb=self.expo_dsb, type="unit": self.set_expo_time(dsb.value(), val, type))
        expo_box = qt.QWidget()
        expo_box.setMaximumWidth(200)
        expo_layout = qt.QHBoxLayout()
        expo_layout.setContentsMargins(0,0,0,0)
        expo_box.setLayout(expo_layout)
        expo_layout.addWidget(self.expo_dsb)
        expo_layout.addWidget(self.expo_unit_cb)
        cam_ctrl_frame.addRow("Exposure time:", expo_box)

        # set binning
        self.bin_horizontal = NewSpinBox(range=self.parent.device.BIN_RANGE)
        self.bin_vertical = NewSpinBox(range=self.parent.device.BIN_RANGE)
        self.bin_horizontal.setValue(self.parent.device.binning["horizontal"])
        self.bin_vertical.setValue(self.parent.device.binning["vertical"])
        self.bin_horizontal.valueChanged[int].connect(lambda val, text="hori", cb=self.bin_vertical: self.set_binning(text, val, cb.value()))
        self.bin_vertical.valueChanged[int].connect(lambda val, text="vert", cb=self.bin_horizontal: self.set_binning(text, cb.value(), val))
        bin_box = qt.QWidget()
        bin_box.setMaximumWidth(200)
        bin_layout = qt.QHBoxLayout()
        bin_layout.setContentsMargins(0,0,0,0)
        bin_box.setLayout(bin_layout)
        bin_layout.addWidget(self.bin_horizontal)
        bin_layout.addWidget(self.bin_vertical)
        cam_ctrl_frame.addRow("Binning H x V:", bin_box)

    # place gui elements related to TCP connection
    def place_tcp_control(self):
        tcp_ctrl_box = qt.QGroupBox("TCP Control")
        tcp_ctrl_box.setStyleSheet("QGroupBox {border: 1px solid #304249;}")
        tcp_ctrl_frame = qt.QFormLayout()
        tcp_ctrl_box.setLayout(tcp_ctrl_frame)
        self.frame.addWidget(tcp_ctrl_box)

        server_host = self.parent.defaults["tcp_connection"]["host_addr"]
        server_port = self.parent.defaults["tcp_connection"]["port"]
        self.server_addr_la = qt.QLabel(server_host+" ("+server_port+")")
        self.server_addr_la.setStyleSheet("QLabel{background-color: gray;}")
        self.server_addr_la.setToolTip("server = this PC")
        tcp_ctrl_frame.addRow("Server/This PC address:", self.server_addr_la)

        self.client_addr_la = qt.QLabel("No connection")
        self.client_addr_la.setStyleSheet("QLabel{background-color: gray;}")
        tcp_ctrl_frame.addRow("Last client address:", self.client_addr_la)

        self.last_write_la = qt.QLabel("No connection")
        self.last_write_la.setStyleSheet("QLabel{background-color: gray;}")
        tcp_ctrl_frame.addRow("Last connection time:", self.last_write_la)

        self.restart_tcp_bt = qt.QPushButton("Restart Connection")
        self.restart_tcp_bt.clicked[bool].connect(lambda val: self.restart_tcp())
        tcp_ctrl_frame.addRow("Restart:", self.restart_tcp_bt)

    # place save/load program configuration gui elements
    def place_save_load(self):
        self.save_load_box = qt.QGroupBox("Save/Load Settings")
        self.save_load_box.setStyleSheet("QGroupBox {border: 1px solid #304249;}")
        save_load_frame = qt.QFormLayout()
        self.save_load_box.setLayout(save_load_frame)
        self.frame.addWidget(self.save_load_box)

        self.file_name_le = qt.QLineEdit()
        default_file_name = self.parent.defaults["setting_save"]["file_name"]
        self.file_name_le.setText(default_file_name)
        save_load_frame.addRow("File name to save:", self.file_name_le)

        self.date_time_chb = qt.QCheckBox()
        self.date_time_chb.setTristate(False)
        date = self.parent.defaults["setting_save"].getboolean("append_time")
        self.date_time_chb.setChecked(date)
        self.date_time_chb.setStyleSheet("QCheckBox::indicator {width: 15px; height: 15px;}")
        save_load_frame.addRow("Auto append time:", self.date_time_chb)

        self.save_settings_bt = qt.QPushButton("save settings")
        self.save_settings_bt.setMaximumWidth(200)
        self.save_settings_bt.clicked[bool].connect(lambda val: self.save_settings())
        save_load_frame.addRow("Save settings:", self.save_settings_bt)

        self.load_settings_bt = qt.QPushButton("load settings")
        self.load_settings_bt.setMaximumWidth(200)
        self.load_settings_bt.clicked[bool].connect(lambda val: self.load_settings())
        save_load_frame.addRow("Load settings:", self.load_settings_bt)

    # start to take images
    def start(self, mode="scan"):
        # self.control_mode = mode
        self.active = True

        # clear signal count QLabels
        self.signal_count.setText("0")
        self.signal_count_mean.setText("0")
        self.signal_count_err_mean.setText("0")
        self.num_image.setText("0")

        # clear images
        img = np.zeros((self.parent.device.image_shape["xmax"], self.parent.device.image_shape["ymax"]))
        for key, image_show in self.parent.image_win.imgs_dict.items():
            image_show.setImage(img, autoLevels=self.parent.image_win.auto_scale_state_dict[key])
        self.parent.image_win.x_plot_curve.setData(np.sum(img, axis=1))
        self.parent.image_win.y_plot_curve.setData(np.sum(img, axis=0))
        self.parent.image_win.ave_img.setImage(img, autoLevels=self.parent.image_win.ave_img_auto_scale_chb.isChecked())

        # clear gaussian fit QLabels
        self.amp.setText("0")
        self.offset.setText("0")
        self.x_mean.setText("0")
        self.x_stand_dev.setText("0")
        self.y_mean.setText("0")
        self.y_stand_dev.setText("0")
        self.peak.setText("0")

        # initialize a hdf group if image saving is required
        if self.img_save:
            # file name of the hdf file we save image to
            self.hdf_filename = self.parent.defaults["image_save"]["file_name"] + "_" + time.strftime("%Y%m%d") + ".hdf"

            os.makedirs(os.path.dirname(self.hdf_filename), exist_ok=True)
            with h5py.File(self.hdf_filename, "a") as hdf_file:
                self.hdf_group_name = self.run_name_le.text()+"_"+time.strftime("%Y%m%d_%H%M%S")
                hdf_file.create_group(self.hdf_group_name)

        self.scan_config = configparser.ConfigParser()
        self.scan_config.optionxform = str
        self.scan_config.read(self.parent.defaults["scan_file_name"]["default"])
        self.num_img_to_take_sb.setValue(self.scan_config["general"].getint("element_number"))
        # num = (self.scan_config["general"].getint("image_number") + self.scan_config["general"].getint("bkg_image_number")) * self.scan_config["general"].getint("sample_number")
        # self.num_img_to_take_sb.setValue(num)
        # self.num_img_to_take will be changed automatically

        self.scan_elem_name = self.scan_config["general"].get("scanned_devices_parameters")
        self.scan_elem_name = self.scan_elem_name.split(",")
        self.scan_elem_name = self.scan_elem_name[0].strip()
        if self.scan_elem_name:
            self.control_mode = "scan"
        else:
            self.control_mode = "record"

        if self.control_mode == "scan":
            self.signal_count_dict = {}
            self.parent.image_win.scan_plot_widget.setLabel("bottom", self.scan_elem_name)

            self.parent.image_win.ave_scan_tab.setCurrentIndex(1) # switch to scan plot tab

        # disable and gray out image/camera controls, in case of any accidental parameter change
        self.enable_widgets(False)

        if self.meas_mode == "fluorescence":
            self.parent.image_win.img_tab.setCurrentIndex(2) # switch to fluorescence plot tab
        elif self.meas_mode == "absorption":
            self.parent.image_win.img_tab.setCurrentIndex(3) # switch to absorption plot tab
        else:
            logging.warning("Measurement mode not supported.")
            return

        # initialize a image taking thread
        self.rec = CamThread(self.parent)
        self.rec.signal.connect(self.img_ctrl_update)
        self.rec.finished.connect(self.stop)
        self.rec.start() # start this thread

        # Another way to do this is to use QTimer() to trigger image readout (timer interval can be 0),
        # but in that case, the while loop that waits for the image is running in the main thread,
        # and blocks the main thread.

    # force to stop image taking
    def stop(self):
        if self.active:
            self.active = False
            try:
                self.rec.wait() #  wait until thread closed
            except AttributeError:
                pass

            # don't reset control_mode to None, bcause img_ctrl_update function for the last image may be called after this function being called
            # self.control_mode = None

            self.enable_widgets(True)

    # function that will be called in every experimental cycle to update GUI display
    @PyQt5.QtCore.pyqtSlot(dict)
    def img_ctrl_update(self, img_dict):
        img_type = img_dict["type"] # "image" or "bkg"
        if img_type == "background":
            img = img_dict["image"]
            # update background image
            self.parent.image_win.imgs_dict["Background"].setImage(img, autoLevels=self.parent.image_win.auto_scale_state_dict["Background"])
        elif img_type == "signal":
            # update signal images
            img = img_dict["image"]
            self.parent.image_win.imgs_dict["Raw Signal"].setImage(img, autoLevels=self.parent.image_win.auto_scale_state_dict["Raw Signal"])

        if img_dict['counter']%2 == 1: #Checking if second image
            self.num_image.setText(str(img_dict["num_image"]))

            img = img_dict["image_post"]
            if self.meas_mode == "fluorescence":
                self.parent.image_win.imgs_dict["Signal minus ave bkg"].setImage(img, autoLevels=self.parent.image_win.auto_scale_state_dict["Signal minus ave bkg"])
            elif self.meas_mode == "absorption":
                self.parent.image_win.imgs_dict["Optical density"].setImage(img, autoLevels=self.parent.image_win.auto_scale_state_dict["Optical density"])
            else:
                logging.warning("Measurement type not supported")
                return
            
            self.parent.image_win.x_plot_curve.setData(np.sum(img, axis=1))
            self.parent.image_win.y_plot_curve.setData(np.sum(img, axis=0))
            img_roi = img_dict["image_post"][self.roi["xmin"]:self.roi["xmax"], self.roi["ymin"]:self.roi["ymax"]]
            self.parent.image_win.x_plot_roi_curve.setData(np.sum(img_roi, axis=1))
            self.parent.image_win.y_plot_roi_curve.setData(np.sum(img_roi, axis=0))
            self.num_image.setText(str(img_dict["num_image"]))
            self.signal_count.setText(str(img_dict["signal_count"]))
            self.signal_count_deque.append(img_dict["signal_count_raw"])
            self.parent.image_win.sc_plot_curve.setData(np.array(self.signal_count_deque), symbol='o')

            if self.control_mode == "record":
                self.parent.image_win.ave_img.setImage(img_dict["image_ave"])
                self.signal_count_mean.setText(str(img_dict["signal_count_ave"]))
                self.signal_count_err_mean.setText(str(img_dict["signal_count_err"]))
            elif self.control_mode == "scan":
                x = np.array([])
                y = np.array([])
                err = np.array([])
                for i, (param, sc_list) in enumerate(img_dict["signal_count_scan"].items()):
                    x = np.append(x, float(param))
                    y = np.append(y, np.mean(sc_list))
                    err = np.append(err, np.std(sc_list)/np.sqrt(len(sc_list)))
                # sort data in order of value of the scan parameter
                order = x.argsort()
                x = x[order]
                y = y[order]
                err = err[order]
                # update "signal count vs scan parameter" plot
                self.parent.image_win.scan_plot_curve.setData(x, y, symbol='o')
                self.parent.image_win.scan_plot_errbar.setData(x=x, y=y, top=err, bottom=err, beam=(x[-1]-x[0])/len(x)*0.2, pen=pg.mkPen('w', width=1.2))
                
            # if img_dict["bkg_counter"] > 0:
            #     img = img_dict["image_post"]
            #     self.parent.image_win.imgs_dict["Signal minus ave bkg"].setImage(img, autoLevels=self.parent.image_win.auto_scale_state_dict["Signal minus ave bkg"])
            #     self.parent.image_win.x_plot_curve.setData(np.sum(img, axis=1))
            #     self.parent.image_win.y_plot_curve.setData(np.sum(img, axis=0))

            #     img_roi = img_dict["image_post_roi"]
            #     self.parent.image_win.x_plot_roi_curve.setData(np.sum(img_roi, axis=1))
            #     self.parent.image_win.y_plot_roi_curve.setData(np.sum(img_roi, axis=0))

            #     sc = img_dict["signal_count"]
            #     self.signal_count.setText(np.format_float_scientific(sc, precision=4))
            #     self.signal_count_deque.append(sc)
            #     self.parent.image_win.sc_plot_curve.setData(np.array(self.signal_count_deque), symbol='o')

            #     if self.control_mode == "record":
            #         self.parent.image_win.ave_img.setImage(img_dict["image_ave"], autoLevels=self.parent.image_win.ave_img_auto_scale_state)
            #         self.signal_count_mean.setText(np.format_float_scientific(img_dict["signal_count_ave"], precision=4))
            #         self.signal_count_err_mean.setText(np.format_float_scientific(img_dict["signal_count_err"], precision=4))
            #     elif self.control_mode == "scan":
            #         x = np.array([])
            #         y = np.array([])
            #         err = np.array([])
            #         for i, (param, sc_list) in enumerate(img_dict["signal_count_scan"].items()):
            #             x = np.append(x, float(param))
            #             y = np.append(y, np.mean(sc_list))
            #             err = np.append(err, np.std(sc_list)/np.sqrt(len(sc_list)))
            #         # sort data in order of value of the scan parameter
            #         order = x.argsort()
            #         x = x[order]
            #         y = y[order]
            #         err = err[order]
            #         # update "signal count vs scan parameter" plot
            #         self.parent.image_win.scan_plot_curve.setData(x, y, symbol='o')
            #         self.parent.image_win.scan_plot_errbar.setData(x=x, y=y, top=err, bottom=err, beam=(x[-1]-x[0])/len(x)*0.2, pen=pg.mkPen('w', width=1.2))


            if self.gaussian_fit:
                # do 2D gaussian fit and update GUI displays
                param = gaussianfit(img_dict["image_post_roi"])
                self.amp.setText("{:.2f}".format(param["amp"]))
                self.offset.setText("{:.2f}".format(param["offset"]))
                self.x_mean.setText("{:.2f}".format(param["x_mean"]+self.roi["xmin"]))
                self.x_stand_dev.setText("{:.2f}".format(param["x_width"]))
                self.y_mean.setText("{:.2f}".format(param["y_mean"]+self.roi["ymin"]))
                self.y_stand_dev.setText("{:.2f}".format(param["y_width"]))
                self.peak.setText("{:.2f}".format(param["peak"]))
                
                xy = np.indices((self.roi["xmax"]-self.roi["xmin"], self.roi["ymax"]-self.roi["ymin"]))
                fit = gaussian(param["amp"], param["x_mean"], param["y_mean"], param["x_width"], param["y_width"], param["offset"])(*xy)

                self.parent.image_win.x_plot_roi_fit_curve.setData(np.sum(fit, axis=1), pen=pg.mkPen('r'))
                self.parent.image_win.y_plot_roi_fit_curve.setData(np.sum(fit, axis=0), pen=pg.mkPen('r'))
            else:
                self.parent.image_win.x_plot_roi_fit_curve.setData(np.array([]))
                self.parent.image_win.y_plot_roi_fit_curve.setData(np.array([]))

        if self.img_save:
            # save imagees to local hdf file
            # in "record" mode, all images are save in the same group
            # in "scan" mode, images of the same value of scan parameter are saved in the same group
            with h5py.File(self.hdf_filename, "r+") as hdf_file:
                root = hdf_file.require_group(self.hdf_group_name)
                if self.control_mode == "scan":
                    root.attrs["scanned parameter"] = self.scan_elem_name
                    root.attrs["number of images"] = self.num_img_to_take
                    root = root.require_group(self.scan_elem_name+"_"+img_dict["scan_param"])
                    root.attrs["scanned parameter"] = self.scan_elem_name
                    root.attrs["scanned param value"] = img_dict["scan_param"]
                dset = root.create_dataset(
                                            name                 = "image" + "_{:06d}".format(img_dict["counter"]),
                                            data                 = img_dict["image"],
                                            shape                = img_dict["image"].shape,
                                            dtype                = "f",
                                            compression          = "gzip",
                                            compression_opts     = 4
                                        )
                # dset.attrs["signal count"] = img_dict["signal_count"]
                dset.attrs["measurement type"] = self.meas_mode
                dset.attrs["region of interest: xmin"] = self.roi["xmin"]
                dset.attrs["region of interest: xmax"] = self.roi["xmax"]
                dset.attrs["region of interest: ymin"] = self.roi["ymin"]
                dset.attrs["region of interest: ymax"] = self.roi["ymax"]

                # display as image in HDFView
                # https://support.hdfgroup.org/HDF5/doc/ADGuide/ImageSpec.html
                dset.attrs["CLASS"] = np.bytes_("IMAGE")
                dset.attrs["IMAGE_VERSION"] = np.bytes_("1.2")
                dset.attrs["IMAGE_SUBCLASS"] = np.bytes_("IMAGE_GRAYSCALE")
                dset.attrs["IMAGE_WHITE_IS_ZERO"] = 0

                if self.gaussian_fit and (img_type == "image"):
                    for key, val in param.items():
                        dset.attrs["2D gaussian fit"+key] = val

    def enable_widgets(self, arg):
        # enable/disable controls
        # self.stop_bt.setEnabled(not arg)
        # self.record_bt.setEnabled(arg)
        # self.scan_bt.setEnabled(arg)
        for rb in self.meas_rblist:
            rb.setEnabled(arg)

        self.num_img_to_take_sb.setEnabled(arg)
        # self.gauss_fit_chb.setEnabled(arg)
        self.img_save_chb.setEnabled(arg)
        self.run_name_le.setEnabled(arg)
        self.cam_ctrl_box.setEnabled(arg)
        self.save_load_box.setEnabled(arg)

        # enable/disable in image ROI selection
        # self.x_min_sb.setEnabled(arg)
        # self.x_max_sb.setEnabled(arg)
        # self.y_min_sb.setEnabled(arg)
        # self.y_max_sb.setEnabled(arg)
        # for key, roi in self.parent.image_win.img_roi_dict.items():
        #     roi.setEnabled(arg)
        # self.parent.image_win.x_plot_lr.setMovable(arg)
        # self.parent.image_win.y_plot_lr.setMovable(arg)

        # force GUI to respond now
        self.parent.app.processEvents()

    def set_num_img(self, val):
        self.num_img_to_take = val

    def set_roi(self, text, val, sb):
        if text == "xmin":
            sb.setMinimum(val+1)
        elif text == "xmax":
            sb.setMaximum(val-1)
        elif text == "ymin":
            sb.setMinimum(val+1)
        elif text == "ymax":
            sb.setMaximum(val-1)

        self.roi[text] = val

        # set in image ROI selection boxes position/size
        x_range = self.roi["xmax"]-self.roi["xmin"]
        y_range = self.roi["ymax"]-self.roi["ymin"]
        for key, roi in self.parent.image_win.img_roi_dict.items():
            roi.setPos(pos=(self.roi["xmin"], self.roi["ymin"]))
            roi.setSize(size=(x_range, y_range))
        self.parent.image_win.x_plot_lr.setRegion((self.roi["xmin"], self.roi["xmax"]))
        self.parent.image_win.y_plot_lr.setRegion((self.roi["ymin"], self.roi["ymax"]))

        # disable 2D gaussian fit if ROI is too larges
        if x_range*y_range > self.cpu_limit:
            if self.gauss_fit_chb.isEnabled():
                self.gauss_fit_chb.setChecked(False)
                self.gauss_fit_chb.setEnabled(False)
        else:
            if not self.gauss_fit_chb.isEnabled():
                self.gauss_fit_chb.setEnabled(True)

    def set_gauss_fit(self, state):
        self.gaussian_fit = bool(state)

    def set_gauss_filter(self, val, param):
        if param == "state":
            self.gaussian_filter = bool(val)
        elif param == "sigma":
            self.gaussian_filter_sigma = val
        else:
            logging.warning(f"Unsupported guassian filter setting: {param}.")

    def set_img_save(self, state):
        self.img_save = state

    def set_expo_time(self, time, unit, change_type):
        unit_num = self.parent.defaults["expo_unit"].getfloat(unit)
        minimum = self.parent.defaults["expo_time"].getfloat("min")
        maximum = self.parent.defaults["expo_time"].getfloat("max")
        d = self.parent.defaults["expo_time"].getint("decimals")
        if change_type == "unit":
            self.expo_dsb.setRange(minimum/unit_num, maximum/unit_num)
            self.expo_dsb.setDecimals(int(d+np.log10(unit_num)))
        elif change_type == "time":
            pass
        else:
            logging.warning("set_expo_time: invalid change_type")
            return

        t = time*unit_num
        t = max(t, minimum)
        t = min(t, maximum)
        self.parent.device.set_expo_time(t)

    def set_binning(self, text, bin_h, bin_v):
        self.parent.device.set_binning(bin_h, bin_v)

        # set bounds for ROI spinboxes
        if text == "hori":
            x_max = self.parent.device.image_shape["xmax"]
            self.x_max_sb.setMaximum(int(x_max))
        elif text == "vert":
            y_max = self.parent.device.image_shape["ymax"]
            self.y_max_sb.setMaximum(int(y_max))
        else:
            logging.warning("Binning type not supported.")

        # set boundaries for in image ROI selections
        for key, roi in self.parent.image_win.img_roi_dict.items():
            roi.setBounds(pos=[0,0], size=[self.parent.device.image_shape["xmax"], self.parent.device.image_shape["ymax"]])
        self.parent.image_win.x_plot_lr.setBounds([0, self.parent.device.image_shape["xmax"]])
        self.parent.image_win.y_plot_lr.setBounds([0, self.parent.device.image_shape["ymax"]])

    def tcp_start(self):
        self.tcp_active = True
        self.tcp_thread = TcpThread(self.parent)
        self.tcp_thread.update_signal.connect(self.tcp_widgets_update)
        self.tcp_thread.start_signal.connect(self.start)
        self.tcp_thread.stop_signal.connect(self.stop)
        self.tcp_thread.start()

    def tcp_stop(self):
        self.tcp_active = False
        try:
            self.tcp_thread.wait() # wait until closed
        except AttributeError as err:
            pass

    def restart_tcp(self):
        self.tcp_stop()
        self.tcp_start()

    @PyQt5.QtCore.pyqtSlot(dict)
    def tcp_widgets_update(self, dict):
        t = dict.get("last write")
        if t:
            self.last_write_la.setText(t)

        addr = dict.get("client addr")
        if addr:
            self.client_addr_la.setText(dict["client addr"][0]+" ("+str(dict["client addr"][1])+")")

    def save_settings(self, latest=False):
        if latest:
            file_name = "program_setting_latest.ini"
        else:
        # compile file name
            file_name = ""
            if self.file_name_le.text():
                file_name += self.file_name_le.text()
            if self.date_time_chb.isChecked():
                if file_name != "":
                    file_name += "_"
                file_name += time.strftime("%Y%m%d_%H%M%S")
            file_name += ".ini"
            file_name = r"saved_settings/"+file_name

            # check if the file name already exists
            if os.path.exists(file_name):
                overwrite = qt.QMessageBox.warning(self, 'File name exists',
                                                'File name already exists. Continue to overwrite it?',
                                                qt.QMessageBox.Yes | qt.QMessageBox.No,
                                                qt.QMessageBox.No)
                if overwrite == qt.QMessageBox.No:
                    return

        config = configparser.ConfigParser()
        config.optionxform = str

        config["record_control"] = {}
        config["record_control"]["meas_mode"] = self.meas_mode

        config["image_control"] = {}
        config["image_control"]["num_image"] = str(self.num_img_to_take_sb.value())
        config["image_control"]["xmin"] = str(self.x_min_sb.value())
        config["image_control"]["xmax"] = str(self.x_max_sb.value())
        config["image_control"]["ymin"] = str(self.y_min_sb.value())
        config["image_control"]["ymax"] = str(self.y_max_sb.value())
        config["image_control"]["2D_gaussian_fit"] = str(self.gaussian_fit)
        config["image_control"]["run_name"] = self.run_name_le.text()
        config["image_control"]["image_auto_save"] = str(self.img_save_chb.isChecked())
        config["image_control"]["gaussian_filter"] = str(self.gaussian_filter)
        config["image_control"]["gaussian_filter_sigma"] = str(self.gaussian_filter_sigma)
        for name in self.parent.image_win.imgs_name:
            config["image_control"][f"auto_scale_state_{name}"] = str(self.parent.image_win.auto_scale_state_dict[name])
        config["image_control"]["auto_scale_state_Average_image"] = str(self.parent.image_win.ave_img_auto_scale_state)
        
        config["camera_control"] = {}
        for i in self.trig_mode_rblist:
            if i.isChecked():
                t = i.text()
                break
        config["camera_control"]["trigger_mode"] = t
        config["camera_control"]["exposure_time"] = str(self.expo_dsb.value())
        config["camera_control"]["exposure_unit"] = self.expo_unit_cb.currentText()
        config["camera_control"]["binning_horizontal"] = str(self.bin_horizontal.value())
        config["camera_control"]["binning_vertical"] = str(self.bin_vertical.value())

        config["tcp_control"] = self.parent.defaults["tcp_connection"]

        configfile = open(file_name, "w")
        config.write(configfile)
        configfile.close()

    def load_settings(self, latest=False):
        if latest:
            try:
                config = configparser.ConfigParser()
                config.read("program_setting_latest.ini")
            except KeyError:
                # could not find file
                return
        else:
            # open a file dialog to choose a configuration file to load
            file_name, _ = qt.QFileDialog.getOpenFileName(self, "Load settigns", "saved_settings/", "All Files (*);;INI File (*.ini)")
            if not file_name:
                return

            config = configparser.ConfigParser()
            config.read(file_name)

        for i in self.meas_rblist:
            if i.text() == config["record_control"]["meas_mode"]:
                i.setChecked(True)
                break

        self.num_img_to_take_sb.setValue(config["image_control"].getint("num_image"))
        # the spinbox emits 'valueChanged' signal, and its connected function will be called
        self.x_min_sb.setValue(config["image_control"].getint("xmin"))
        self.x_max_sb.setValue(config["image_control"].getint("xmax"))
        self.y_min_sb.setValue(config["image_control"].getint("ymin"))
        self.y_max_sb.setValue(config["image_control"].getint("ymax"))
        # make sure image range is updated BEFORE gauss_fit_chb
        self.gauss_fit_chb.setChecked(config["image_control"].getboolean("2d_gaussian_fit"))
        # the combobox emits 'stateChanged' signal, and its connected function will be called
        self.img_save_chb.setChecked(config["image_control"].getboolean("image_auto_save"))
        self.run_name_le.setText(config["image_control"].get("run_name"))

        self.gauss_filter_chb.setChecked(config["image_control"].getboolean("gaussian_filter"))
        self.gaussian_filter_sigma_dsb.setValue(config["image_control"].getfloat("gaussian_filter_sigma"))

        for name in self.parent.image_win.imgs_name:
            self.parent.image_win.auto_scale_chb_dict[name].setChecked(config["image_control"].getboolean(f"auto_scale_state_{name}"))
        self.parent.image_win.ave_img_auto_scale_chb.setChecked(config["image_control"].getboolean("auto_scale_state_Average_image"))

        for i in self.trig_mode_rblist:
            if i.text() == config["camera_control"]["trigger_mode"]:
                i.setChecked(True)
                break

        # make sure expo_unit_cb changes before time, because it changes expo_dsb range
        self.expo_unit_cb.setCurrentText(config["camera_control"]["exposure_unit"])
        self.expo_dsb.setValue(config["camera_control"].getfloat("exposure_time"))

        self.bin_horizontal.setValue(int(config["camera_control"].get("binning_horizontal")))
        self.bin_vertical.setValue(int(config["camera_control"].get("binning_vertical")))

        self.tcp_stop()
        self.parent.defaults["tcp_connection"] = config["tcp_control"]
        server_host = self.parent.defaults["tcp_connection"]["host_addr"]
        server_port = self.parent.defaults["tcp_connection"]["port"]
        self.server_addr_la.setText(server_host+" ("+server_port+")")
        self.tcp_start()

    def set_meas_mode(self, text, checked):
        if checked:
            self.meas_mode = text

# the class that places images and plots
class ImageWin(Scrollarea):
    def __init__(self, parent):
        super().__init__(parent, label="Images", type="grid")
        self.frame.setColumnStretch(0,7)
        self.frame.setColumnStretch(1,4)
        self.frame.setRowStretch(0,1)
        self.frame.setRowStretch(1,1)
        self.frame.setRowStretch(2,1)
        self.frame.setContentsMargins(0,0,0,0)
        self.imgs_dict = {}
        self.img_roi_dict = {}
        self.auto_scale_chb_dict = {}
        self.auto_scale_state_dict = {}
        self.imgs_name = ["Background", "Raw Signal", "Signal minus ave bkg", "Optical density"]

        for name in self.imgs_name:
            self.auto_scale_state_dict[name] = self.parent.defaults.getboolean("image_auto_scale", name)
        self.ave_img_auto_scale_state = self.parent.defaults.getboolean("image_auto_scale", "Average image")

        # place images and plots
        self.place_sgn_imgs()
        self.place_axis_plots()

        self.ave_scan_tab = qt.QTabWidget()
        self.frame.addWidget(self.ave_scan_tab, 2, 0)
        self.place_ave_image()
        self.place_scan_plot()

        self.place_sc_plot()

    # place background and signal images
    def place_sgn_imgs(self):
        self.img_tab = qt.QTabWidget()
        self.frame.addWidget(self.img_tab, 0, 0, 2, 1)
        for i, name in enumerate(self.imgs_name):
            imgwidget = imageWidget(parent=self, name=name, include_ROI=True, colorname="viridis", 
                                    dummy_data_xmax=self.parent.device.image_shape["xmax"],
                                    dummy_data_ymax=self.parent.device.image_shape["ymax"],
                                    )

            # add the widget to the front panel
            self.img_tab.addTab(imgwidget.graphlayout, " "+name+" ")

            # config ROI
            imgwidget.img_roi.setPos(pos=(self.parent.defaults["roi"].getint("xmin"), self.parent.defaults["roi"].getint("ymin")))
            imgwidget.img_roi.setSize(size=(self.parent.defaults["roi"].getint("xmax")-self.parent.defaults["roi"].getint("xmin"),
                                            self.parent.defaults["roi"].getint("ymax")-self.parent.defaults["roi"].getint("ymin")))
            imgwidget.img_roi.sigRegionChanged.connect(lambda roi=imgwidget.img_roi: self.img_roi_update(roi))
            imgwidget.img_roi.setBounds(pos=[0,0], size=[self.parent.device.image_shape["xmax"], self.parent.device.image_shape["ymax"]])

            imgwidget.chb.setChecked(self.auto_scale_state_dict[name])
            imgwidget.chb.stateChanged[int].connect(lambda val, param=name: self.set_auto_scale(val, param))

            self.img_roi_dict[name] = imgwidget.img_roi
            self.imgs_dict[name] = imgwidget.img
            self.auto_scale_chb_dict[name] = imgwidget.chb
        
        self.starting_data = imgwidget.dummy_data

        self.img_tab.setCurrentIndex(2) # make tab #2 (count from 0) to show as default

    # place plots of signal_count along one axis
    def place_axis_plots(self):
        tickstyle = {"showValues": False}

        self.curve_tab = qt.QTabWidget()
        self.frame.addWidget(self.curve_tab, 0, 1, 2, 1)

        # place plot of signal_count along x axis
        x_data = np.sum(self.starting_data, axis=1)
        graphlayout = pg.GraphicsLayoutWidget(parent=self, border=True)
        self.curve_tab.addTab(graphlayout, " Full Frame Signal ")
        x_plot = graphlayout.addPlot(title="Signal count v.s. X")
        x_plot.showGrid(True, True)
        x_plot.setLabel("top")
        # x_plot.getAxis("top").setTicks([])
        x_plot.getAxis("top").setStyle(**tickstyle)
        x_plot.setLabel("right")
        # x_plot.getAxis("right").setTicks([])
        x_plot.getAxis("right").setStyle(**tickstyle)
        self.x_plot_curve = x_plot.plot(x_data)

        # add ROI selection
        self.x_plot_lr = pg.LinearRegionItem([self.parent.defaults["roi"].getint("xmin"),
                                                self.parent.defaults["roi"].getint("xmax")], swapMode="block")
        # no "snap" option for LinearRegion item?
        self.x_plot_lr.setBounds([0, self.parent.device.image_shape["xmax"]])
        x_plot.addItem(self.x_plot_lr)
        self.x_plot_lr.sigRegionChanged.connect(self.x_plot_lr_update)

        graphlayout.nextRow()

        # place plot of signal_count along y axis
        y_data = np.sum(self.starting_data, axis=0)
        y_plot = graphlayout.addPlot(title="Signal count v.s. Y")
        y_plot.showGrid(True, True)
        y_plot.setLabel("top")
        y_plot.getAxis("top").setStyle(**tickstyle)
        y_plot.setLabel("right")
        y_plot.getAxis("right").setStyle(**tickstyle)
        self.y_plot_curve = y_plot.plot(y_data)

        # add ROI selection
        self.y_plot_lr = pg.LinearRegionItem([self.parent.defaults["roi"].getint("ymin"),
                                                self.parent.defaults["roi"].getint("ymax")], swapMode="block")
        self.y_plot_lr.setBounds([0, self.parent.device.image_shape["ymax"]])
        y_plot.addItem(self.y_plot_lr)
        self.y_plot_lr.sigRegionChanged.connect(self.y_plot_lr_update)

        graphlayout = pg.GraphicsLayoutWidget(parent=self, border=True)
        self.curve_tab.addTab(graphlayout, " Signal in ROI ")

        x_plot = graphlayout.addPlot(title="Signal count v.s. X")
        x_plot.showGrid(True, True)
        x_plot.setLabel("top")
        # x_plot.getAxis("top").setTicks([])
        x_plot.getAxis("top").setStyle(**tickstyle)
        x_plot.setLabel("right")
        # x_plot.getAxis("right").setTicks([])
        x_plot.getAxis("right").setStyle(**tickstyle)
        data_roi = self.starting_data[self.parent.defaults["roi"].getint("xmin"):self.parent.defaults["roi"].getint("xmax"),
                                        self.parent.defaults["roi"].getint("ymin"):self.parent.defaults["roi"].getint("ymax")]
        x_data = np.sum(data_roi, axis=1)
        self.x_plot_roi_curve = x_plot.plot(x_data)
        self.x_plot_roi_fit_curve = x_plot.plot(np.array([]))

        graphlayout.nextRow()

        # place plot of signal_count along y axis
        y_plot = graphlayout.addPlot(title="Signal count v.s. Y")
        y_plot.showGrid(True, True)
        y_plot.setLabel("top")
        y_plot.getAxis("top").setStyle(**tickstyle)
        y_plot.setLabel("right")
        y_plot.getAxis("right").setStyle(**tickstyle)
        y_data = np.sum(data_roi, axis=0)
        self.y_plot_roi_curve = y_plot.plot(y_data)
        self.y_plot_roi_fit_curve = y_plot.plot(np.array([]))

    # place averaged image
    def place_ave_image(self):
        name = "Average image"
        imgwidget = imageWidget(parent=self, name=name, include_ROI=False, colorname="viridis", 
                                dummy_data_xmax=self.parent.device.image_shape["xmax"],
                                dummy_data_ymax=self.parent.device.image_shape["ymax"],
                                )

        self.ave_scan_tab.addTab(imgwidget.graphlayout, " "+name+" ")
        self.ave_img = imgwidget.img
        self.ave_img_auto_scale_chb = imgwidget.chb
        self.ave_img_auto_scale_chb.setChecked(self.ave_img_auto_scale_state)
        self.ave_img_auto_scale_chb.stateChanged[int].connect(lambda val, param="Average image": self.set_auto_scale(val, param))

    # place scan plots
    def place_scan_plot(self):
        tickstyle = {"showValues": False}

        self.scan_plot_widget = pg.PlotWidget(title="Signal count v.s. Scan param.")
        self.scan_plot_widget.showGrid(True, True)
        self.scan_plot_widget.setLabel("top")
        self.scan_plot_widget.getAxis("top").setStyle(**tickstyle)
        self.scan_plot_widget.setLabel("right")
        self.scan_plot_widget.getAxis("right").setStyle(**tickstyle)
        fontstyle = {"color": "#919191", "font-size": "11pt"}
        self.scan_plot_widget.setLabel("bottom", "Scan parameter", **fontstyle)
        self.scan_plot_widget.getAxis("bottom").enableAutoSIPrefix(False)
        self.scan_plot_curve = self.scan_plot_widget.plot()

        # place error bar
        self.scan_plot_errbar = pg.ErrorBarItem()
        self.scan_plot_widget.addItem(self.scan_plot_errbar)

        self.ave_scan_tab.addTab(self.scan_plot_widget, " Scan Plot ")

    # place a plot showing running signal count
    def place_sc_plot(self):
        tickstyle = {"showValues": False}

        self.sc_plot_widget = pg.PlotWidget(title="Signal count")
        self.sc_plot_widget.showGrid(True, True)
        self.sc_plot_widget.setLabel("top")
        self.sc_plot_widget.getAxis("top").setStyle(**tickstyle)
        self.sc_plot_widget.setLabel("right")
        self.sc_plot_widget.getAxis("right").setStyle(**tickstyle)
        self.sc_plot_curve = self.sc_plot_widget.plot()

        self.frame.addWidget(self.sc_plot_widget, 2, 1)

    # set ROI in background/signal imgaes
    def img_roi_update(self, roi):
        x_min = roi.pos()[0]
        y_min = roi.pos()[1]
        x_max = x_min + roi.size()[0]
        y_max = y_min + roi.size()[1]

        self.parent.control.x_min_sb.setValue(round(x_min))
        self.parent.control.x_max_sb.setValue(round(x_max))
        self.parent.control.y_min_sb.setValue(round(y_min))
        self.parent.control.y_max_sb.setValue(round(y_max))

    # set ROI in the plot of signal count along x-axis
    def x_plot_lr_update(self):
        x_min = self.x_plot_lr.getRegion()[0]
        x_max = self.x_plot_lr.getRegion()[1]

        self.parent.control.x_min_sb.setValue(round(x_min))
        self.parent.control.x_max_sb.setValue(round(x_max))

    # set ROI in the plot of signal count along y-axis
    def y_plot_lr_update(self):
        y_min = self.y_plot_lr.getRegion()[0]
        y_max = self.y_plot_lr.getRegion()[1]

        self.parent.control.y_min_sb.setValue(round(y_min))
        self.parent.control.y_max_sb.setValue(round(y_max))

    def set_auto_scale(self, val, param):
        # logging.info(str(val))
        if param == "Average image":
            self.ave_img_auto_scale_state = bool(val)
        elif param in self.imgs_name:
            self.auto_scale_state_dict[param] = bool(val)
        else:
            logging.warning(f"Unsupported auto scale param: {param}.")

# main class, parent of other classes
class CameraGUI(qt.QMainWindow):
    def __init__(self, app):
        super().__init__()
        time.sleep(0.1) #It appears this sleep is necessary to run on python 3.11.2. Not sure why
        
        self.setWindowIcon(QIcon(window_icon_name))

        self.setStyleSheet("QWidget{font: 10pt;}")
        # self.setStyleSheet("QToolTip{background-color: black; color: white; font: 10pt;}")
        self.app = app
        logging.getLogger().setLevel("INFO")

        # read default settings from a local .ini file
        self.defaults = configparser.ConfigParser()
        self.defaults.read('defaults.ini')

        self.setWindowTitle(f"Alvium - {self.defaults['camera']['id']}")

        # instantiate other classes
        self.device = Alvium(self.defaults["camera"]["id"])
        self.control = Control(self)
        self.image_win = ImageWin(self)

        # load latest settings
        self.control.load_settings(latest=True)

        self.splitter = qt.QSplitter()
        self.splitter.setOrientation(PyQt5.QtCore.Qt.Horizontal)
        self.setCentralWidget(self.splitter)
        self.splitter.addWidget(self.image_win)
        self.splitter.addWidget(self.control)

        self.resize(1600, 900)
        self.show()

    def closeEvent(self, event):
        if not self.control.active:
            self.control.save_settings(latest=True)
            super().closeEvent(event)

        else:
            # ask if continue to close
            ans = qt.QMessageBox.warning(self, 'Program warning',
                                'Warning: the program is running. Conitnue to close the program?',
                                qt.QMessageBox.Yes | qt.QMessageBox.No,
                                qt.QMessageBox.No)
            if ans == qt.QMessageBox.Yes:
                self.control.save_settings(latest=True)
                super().closeEvent(event)
            else:
                event.ignore()


if __name__ == '__main__':
    app = qt.QApplication(sys.argv)
    app.setStyleSheet(qdarkstyle.load_stylesheet_pyqt5())
    
    # This is for making the window's icon
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("frag.cameras.alvium")
    app.setWindowIcon(QIcon(window_icon_name))

    main_window = CameraGUI(app)

    try:
        # All interaction with the camera needs to do within the context manager of both the Vimba SDK and the camera.
        # Entering the camera context manager is slow as hell, so do it once at the top level of the program. Doing it
        # this way does mean that other programs (e.g. vimba viewer) are locked out of the camera while this program is
        # running, but that's a worthwhile tradeoff.
        with vmbpy.VmbSystem.get_instance(), main_window.device.cam:
            app.exec_()
            sys.exit(0)
    except SystemExit:
        print("\nApp is closing...")