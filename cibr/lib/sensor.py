import mne
import numpy as np 

from mne.channels.layout import _merge_grad_data
from mne.channels.layout import find_layout
from mne.channels.layout import _pair_grad_sensors


def plot_sensor_topomap(data, info, ax, cmap='RdBu_r', factor=1.0):
    """
    """

    data = data.copy()

    picks, pos = _pair_grad_sensors(info, find_layout(info))
    data = _merge_grad_data(data[picks], method='mean').reshape(-1)

    # if np.max(data) >= 0:
    #     pos_limit = np.percentile(data[data >= 0], 70)
    #     data[(data >= 0) & (data < pos_limit)] = 0
    #     data[(data >= 0) & (data >= pos_limit)] -= pos_limit
    # if np.min(data) <= 0:
    #     neg_limit = np.percentile(data[data <= 0], 30)
    #     data[(data <= 0) & (data > neg_limit)] = 0
    #     data[(data <= 0) & (data <= neg_limit)] -= neg_limit

    vmax = np.max(np.abs(data)) / factor
    vmin = -vmax

    pos = pos[:, :2]
    pos[:, 0] = (pos[:, 0] - np.mean(pos[:, 0])) / 4.5
    pos[:, 1] = (pos[:, 1] - np.mean(pos[:, 1])) / 4.5

    mne.viz.topomap.plot_topomap(data, pos, axes=ax, vmin=vmin, vmax=vmax,
                                 cmap=cmap, extrapolate='local', contours=0,
                                 image_interp='nearest', res=64)

