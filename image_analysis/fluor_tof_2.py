import numpy as np
from scipy import optimize
from scipy import stats
import uncertainties.unumpy as unp
import uncertainties as unc
import matplotlib.pyplot as plt
import matplotlib as mpl
import h5py
from scipy.constants import k, u
import os
import addcopyfighandler

def gaussian(xdata, amp, x_mean, y_mean, x_width, y_width, offset):
    x, y = xdata
    x_width = float(x_width)
    y_width = float(y_width)

    return amp*np.exp(-0.5*((x-x_mean)/x_width)**2-0.5*((y-y_mean)/y_width)**2) + offset

# return a 2D gaussian fit
# generally a 2D gaussian fit can have 7 params, 6 of them are implemented here (the excluded one is an angle)
# codes adapted from https://scipy-cookbook.readthedocs.io/items/FittingData.html
def gaussianfit(data, roi, showimg=False):
    # calculate moments for initial guess
    data = data[roi["xmin"]:roi["xmax"], roi["ymin"]:roi["ymax"]]
    if showimg:
        plt.imshow(data, cmap='viridis')
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

    xdata = np.vstack((X.ravel(), Y.ravel()))
    ydata = np.ravel(data)
    p, pcov = optimize.curve_fit(gaussian, xdata, ydata, p0=(amp, x_mean, y_mean, x_width, y_width, offset))
    pstd = np.sqrt(np.diag(pcov))

    p_dict = {}
    p_dict["x_mean"] = p[1]
    p_dict["y_mean"] = p[2]
    p_dict["x_width"] = p[3]
    p_dict["y_width"] = p[4]
    p_dict["amp"] = p[0]
    p_dict["offset"] = p[5]

    # print(p)
    # print(pstd)

    return p_dict

def linear(x, slope, offset):
    return slope*x+offset

def linearfit(x, y, yerr):
    ind = np.argsort(x)
    x_sorted = x[ind]
    y_sorted = y[ind]
    slope = (y_sorted[-1]-y_sorted[0])/(x_sorted[-1]-x_sorted[0])
    offset = (y_sorted[0]*x_sorted[-1]-y_sorted[-1]*x_sorted[0])/(x_sorted[-1]-x_sorted[0])

    popt, pcov = optimize.curve_fit(linear, x, y, p0=(slope, offset), sigma=yerr)

    return (popt, pcov)

class tofanalysis:
    def __init__(self, fname, gname):
        param = {"pixeltomm": 99*2/(27)*4*1e-3, "m": 108.904755778*u, "confidence_band": 0.95}

        time_sq, axial_width_sq, axial_width_sq_err, radial_width_sq, radial_width_sq_err = self.readhdf(fname, gname, param)
        print(time_sq)
        print(radial_width_sq)
        print(radial_width_sq_err)

        order = time_sq.argsort()
        time_sq = time_sq[order]
        axial_width_sq = axial_width_sq[order]
        axial_width_sq_err = axial_width_sq_err[order]
        radial_width_sq = radial_width_sq[order]
        radial_width_sq_err = radial_width_sq_err[order]

        # ind = np.arange(0, 4)
        # time_sq = time_sq[ind]
        # axial_width_sq = axial_width_sq[ind]
        # axial_width_sq_err = axial_width_sq_err[ind]
        # radial_width_sq = radial_width_sq[ind]
        # radial_width_sq_err = radial_width_sq_err[ind]

        mpl.style.use("seaborn-v0_8")
        self.fig, self.ax = plt.subplots()
        self.plot(time_sq, radial_width_sq, radial_width_sq_err, type="radial", param=param)
        self.plot(time_sq, axial_width_sq, axial_width_sq_err, type="axial", param=param)
        self.ax.set_xlabel("time of flight$^2$ [ms$^2$]")
        self.ax.set_ylabel("cloud rms radius$^2$ [mm$^2$]")
        self.ax.set_title(gname)
        self.ax.legend()
        plt.show()

    def readhdf(self, fname, gname, param):
        with h5py.File(fname, "r") as f:
            group = f[gname]
            time_sq = np.array([])
            axial_width_sq = np.array([])
            axial_width_sq_err = np.array([])
            radial_width_sq = np.array([])
            radial_width_sq_err = np.array([])
            counter = 0

            print("Reading group ", group)
            for subg in group.keys(): #Cycle through subfolders (ie TOF expansion)
                print("Reading subgroup ", subg)
                image_list = []
                for img in group[subg].keys():
                    image_list.append(img)
                x_width_sq = []
                y_width_sq = []

                # print(counter)
                # counter += 1
                # if counter == 11.56:
                #      break

                for i in range(int(len(image_list)/2)): #Cycle through number of subtracted images in each subfolder. A subtracted image consists of 2 images
                    #img_data = np.array(group[subg][image_list[int(2*i)]]) - np.array(group[subg][image_list[(2*i+1)]]) #Signal - Background
                    img_data = np.divide(np.array(group[subg][image_list[int(2*i)]]), np.array(group[subg][image_list[int(2*i+1)]])) #Signal / Background
                    img_data = -1*np.log(img_data)


                    roi = {"xmin":150, "xmax":250, "ymin":100, "ymax":140} # choose a braod roi for the first fit trial
                    new_roi = roi
                    fitresult = gaussianfit(img_data, roi, showimg = False)
                    print('tset')
                    new_roi = {} # calculate a new roi based on the first fit result (use +/-3sigma region)
                    new_roi["xmin"] = int(np.maximum(roi["xmin"]+fitresult["x_mean"]-3*fitresult["x_width"], 0))
                    new_roi["xmax"] = int(np.minimum(roi["xmin"]+fitresult["x_mean"]+3*fitresult["x_width"], img_data.shape[0]))
                    new_roi["ymin"] = int(np.maximum(roi["ymin"]+fitresult["y_mean"]-3*fitresult["y_width"], 0))
                    new_roi["ymax"] = int(np.minimum(roi["ymin"]+fitresult["y_mean"]+3*fitresult["y_width"], img_data.shape[1]))

                    fitresult = gaussianfit(img_data, new_roi, showimg=False) # make a second fit using the new roi
                    x_width_sq.append((fitresult["x_width"]*param["pixeltomm"])**2)
                    y_width_sq.append((fitresult["y_width"]*param["pixeltomm"])**2)
                    print(fitresult)
                    #y_width_sq =np.append(y_width_sq, (fitresult["y_width"]*param["pixeltomm"])**2)

                axial_width_sq = np.append(axial_width_sq, np.mean(y_width_sq))
                axial_width_sq_err = np.append(axial_width_sq_err, np.std(y_width_sq)/np.sqrt(len(y_width_sq)))
                #axial_width_sq_err = np.append(axial_width_sq_err,.1)
                radial_width_sq = np.append(radial_width_sq, np.mean(x_width_sq))
                radial_width_sq_err = np.append(radial_width_sq_err, np.std(x_width_sq)/np.sqrt(len(x_width_sq)))
                #radial_width_sq_err = np.append(radial_width_sq_err, .1)
                time_sq = np.append(time_sq, (float(subg.split("_")[-1])/1e6+0.25)**2) # convert ns to ms

        return (time_sq, axial_width_sq, axial_width_sq_err, radial_width_sq, radial_width_sq_err)

    def plot(self, time_sq, width_sq, width_sq_err, type="", param={}):
        if type == "radial":
            color = 'C1'
        elif type == "axial":
            color = 'C2'
        else:
            print("Plot type not supported.")
            return

        popt, pcov = linearfit(time_sq, width_sq, width_sq_err)
        fit_chisq = np.sum(((linear(time_sq, *popt)-width_sq)/width_sq_err)**2)
        reduced_chisq = fit_chisq/(len(time_sq)-2)
        # gof = 100*(1 - stats.chi2.cdf(fit_chisq, len(time_sq)-2)) # in percent, goodness of fit, see https://faculty1.coloradocollege.edu/~sburns/toolbox/DataFitting.html
        self.ax.errorbar(time_sq, width_sq, yerr=width_sq_err, marker='o', mfc=color, markeredgewidth=0.8, markeredgecolor='k', ecolor=color, linestyle='')

        x = np.linspace(0, np.amax(time_sq), 200)
        width_sq_fit = linear(x, *popt)
        c = stats.norm.ppf((1+param["confidence_band"])/2) # 95% confidence level gives critical value c=1.96
        perr = np.sqrt(np.diag(pcov)) # gives the standard deviation of fitting parameters
        temp = popt[0]*param["m"]/1.380649e-23*1e6 # convert to uK
        temp_err = perr[0]*param["m"]/1.380649e-23*1e6
        radius = np.sqrt(popt[1])
        radius_err = 0.5*perr[1]/np.sqrt(popt[1])
        label = type + ": {:.0f}({:.0f}) uK, {:.2f}({:.2f}) mm, $\chi^2_\\nu$: {:.2f}".format(temp, temp_err, radius, radius_err, reduced_chisq)
        self.ax.plot(x, width_sq_fit, color, label=label)

        k, b = unc.correlated_values(popt, pcov)
        py = k*x+b
        nom = unp.nominal_values(py)
        std = unp.std_devs(py)
        self.ax.fill_between(x, nom-c*std, nom+c*std, color=color, alpha=0.2, label="{:.0f}% confidence band".format(param["confidence_band"]*100))


filepath = "C:/Users/13128/jmd/pixelfly-python-control/saved_images/"
filename = "images_20240909.hdf"
fname = filepath + filename
gname = "DetuningPowerDependence" + "_20240909_154515"
detuning = 0 # in MHz

# calculate and plot temperature, inital rms radius, reduced \chi^2, 1-CDF(\chi^2)
# indicate uncertainties at "confidence_band" confidence level
# plot pointwise confident band at "confidence_band" level
tof = tofanalysis(fname, gname)
