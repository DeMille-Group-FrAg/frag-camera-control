import numpy as np
from scipy import optimize
from scipy import stats
import uncertainties.unumpy as unp
import uncertainties as unc
import matplotlib.pyplot as plt
import matplotlib as mpl
import h5py
from scipy.constants import k, u
import addcopyfighandler

def gaussian(amp, x_mean, y_mean, x_width, y_width, offset):
    x_width = float(x_width)
    y_width = float(y_width)

    return lambda x, y: amp*np.exp(-0.5*((x-x_mean)/x_width)**2-0.5*((y-y_mean)/y_width)**2) + offset

# return a 2D gaussian fit
# generally a 2D gaussian fit can have 7 params, 6 of them are implemented here (the excluded one is an angle)
# codes adapted from https://scipy-cookbook.readthedocs.io/items/FittingData.html
def gaussianfit(data, roi, showimg=False):
    # calculate moments for initial guess
    data = data[roi["xmin"]:roi["xmax"], roi["ymin"]:roi["ymax"]]
    if showimg:
        plt.imshow(data, cmap = 'viridis')
        plt.show()

    total = np.sum(data)
    X, Y = np.indices(data.shape)
    x_mean = np.sum(X*data)/total
    y_mean = np.sum(Y*data)/total
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

    return p_dict

class atomnumanalysis:
    def __init__(self, fname, gname, detuning):
        # resonant_cross_section in mm^2, linewidth in MHz
        param = {"pixeltomm": 99*2/(27)*4*1e-3, "kB": k, "m": 109*u, "confidence_band": 0.95, "resonant_cross_section": 5/3*(328e-6)**2/(2*np.pi), "linewidth":23.4}

        atom_num = self.readhdf(fname, gname, param, detuning)

        # atom_num = np.random.rand(10)+3

        mpl.style.use("seaborn-v0_8")
        self.fig, self.ax = plt.subplots()
        self.plot(atom_num, param=param)
        self.ax.set_xlabel("No. of images")
        self.ax.set_ylabel("Atom number")
        self.ax.set_title(gname)
        self.ax.legend()
        # plt.hist(atom_num, bins=40)
        plt.show()

    def readhdf(self, fname, gname, param, detuning):
        with h5py.File(fname, "r") as f:
            group = f[gname]
            atom_num = np.array([])
            cross_section = param["resonant_cross_section"]/(1+4*(detuning/param["linewidth"])**2)

            test = []
            for img in group.keys():
                test.append(img)

            for i in range (1, int(len(group)/2)):
                roi = {"xmin":0, "xmax":400, "ymin":0, "ymax":400} # choose a braod roi for the first fit trial



                img_data = np.divide((np.array(group[test[i*2]])),((np.array(group[test[i*2+1]]))))
                img_data = -1*np.log(img_data)


                #img_data = np.divide(np.array(group[test[i*2]]) - np.full((348, 260), 200), np.array(group[test[2*i+1]])-np.full((348, 260), 200))
               # img_data = -1*np.log(img_data)
                data = img_data[roi["xmin"]:roi["xmax"], roi["ymin"]:roi["ymax"]]
                if True:
                    plt.imshow(data, cmap = 'viridis')
                    plt.grid(None)
                    plt.show()
                # plt.figure()
                # plt.imshow(img_data, cmap='viridis')
                # plt.show()

                #roi = {"xmin":70, "xmax":110, "ymin":40, "ymax":80} # choose a braod roi for the first fit trial for extend pixel range
                roi = {"xmin":180, "xmax":220, "ymin":80, "ymax":120} # choose a braod roi for the first fit trial
                #roi = {"xmin":0, "xmax":250, "ymin":100, "ymax":300} # choose a braod roi for the first fit trial

                new_roi = roi
                fitresult = gaussianfit(img_data, roi, showimg = False)
                print(fitresult)

                new_roi = {} # calculate a new roi based on the first fit result (use +/-3sigma region)
                new_roi["xmin"] = int(np.maximum(roi["xmin"]+fitresult["x_mean"]-3*fitresult["x_width"], 0))
                new_roi["xmax"] = int(np.minimum(roi["xmin"]+fitresult["x_mean"]+3*fitresult["x_width"], img_data.shape[0]))
                new_roi["ymin"] = int(np.maximum(roi["ymin"]+fitresult["y_mean"]-3*fitresult["y_width"], 0))
                new_roi["ymax"] = int(np.minimum(roi["ymin"]+fitresult["y_mean"]+3*fitresult["y_width"], img_data.shape[1]))

                fitresult = gaussianfit(img_data, new_roi, showimg=False) # make a second fit using the new roi
                roi = new_roi
                new_roi = {} # calculate a new roi based on the second fit result (use +/-3sigma region)
                new_roi["xmin"] = int(np.maximum(roi["xmin"]+fitresult["x_mean"]-3*fitresult["x_width"], 0))
                new_roi["xmax"] = int(np.minimum(roi["xmin"]+fitresult["x_mean"]+3*fitresult["x_width"], img_data.shape[0]))
                new_roi["ymin"] = int(np.maximum(roi["ymin"]+fitresult["y_mean"]-3*fitresult["y_width"], 0))
                new_roi["ymax"] = int(np.minimum(roi["ymin"]+fitresult["y_mean"]+3*fitresult["y_width"], img_data.shape[1]))



                sc = np.sum(img_data[new_roi["xmin"]:new_roi["xmax"], new_roi["ymin"]:new_roi["ymax"]]) # signal count
                print(sc)
                atom_num_summing = np.append(atom_num, sc*(param["pixeltomm"]**2)/cross_section)
                print("Atom number summing: " + str(atom_num_summing[0]/1e6))
                signal = fitresult["amp"] * 2*np.pi * fitresult["x_width"]  * fitresult["y_width"]
                atom_num = np.append(atom_num, signal*(param["pixeltomm"]**2)/cross_section)


                density = atom_num/((2*np.pi)**1.5*(fitresult["x_width"] * fitresult["x_width"] * fitresult["y_width"] *(param["pixeltomm"]**3) *1e-3 *1e-3*1e-3))/1e6/1e10
                print("Atom Number: " + str(np.mean(atom_num/1e6)))
                print('Density:' + str(np.mean(density)))

        return atom_num

    def plot(self, atom_num, param={}):
        color = 'C1'
        self.ax.plot(atom_num, marker='o', mfc=color, markeredgewidth=0.8, markeredgecolor='k', linestyle='')

        c = stats.norm.ppf((1+param["confidence_band"])/2) # 95% confidence level gives critical value c=1.96
        mean = np.mean(atom_num)
        std = np.std(atom_num)/np.sqrt(len(atom_num))
        x = np.arange(len(atom_num))
        self.ax.plot(x, np.ones(len(atom_num))*mean, color, label="Atom number: "+np.format_float_scientific(mean, precision=2)+"("+ np.format_float_scientific(std, precision=1)+")")
        self.ax.fill_between(x, np.ones(len(atom_num))*(mean-c*std), np.ones(len(atom_num))*(mean+c*std), color=color, alpha=0.2, label="{:.0f}% confidence band".format(param["confidence_band"]*100))



filepath = "C:/Users/13128/jmd/pixelfly-python-control/saved_images/"
filename = "images_20240730.hdf"
filename = "images_20250905.hdf"
fname = filepath + filename
fname=r"C:\Users\frage\Documents\FrAg Code\alvium-python-control\saved_images\images_20250512.hdf"
gname = "DetuningPowerDependence" + "_20250512_155408"
detuning = 19 # in MHz

# calculate and plot temperature, inital rms radius, reduced \chi^2, 1-CDF(\chi^2).
# indicate uncertainties at "confidence_band" confidence level
# plot pointwise confident band at "confidence_band" level
tof = atomnumanalysis(fname, gname, detuning)
