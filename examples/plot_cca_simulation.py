# -*- coding: utf-8 -*-
"""
Contrast-CCA on simulated MEG data
=========================

This example shows how to simulate MEG data with individual differences and then extract the associations with CCA.
"""

# %%
# Import necessary libraries.

import logging
import warnings
import os

import mne
import numpy as np

import scipy.stats
from scipy.linalg import cholesky

import matplotlib.pyplot as plt 
import matplotlib as mpl

from sparsecca import cca_ipls
from sklearn.decomposition import PCA

# Suppress warnings and set plotting and logging properties
plt.rcParams.update({'font.size': 10.0})
logging.getLogger('mne').setLevel(logging.ERROR)
mne.viz.set_3d_backend('pyvista')

# %%
# Set up paths.

data_path = mne.datasets.sample.data_path()
subjects_dir = os.path.join(data_path, 'subjects')
subject = 'sample'

raw_fname = os.path.join(data_path, 'MEG', subject, 'sample_audvis_raw.fif')
fwd_fname = os.path.join(data_path, 'MEG', subject, 'sample_audvis-meg-oct-6-fwd.fif')

# %%
# Read raw, drop eeg data and resample.

raw = mne.io.Raw(raw_fname, preload=True)
picks = mne.pick_types(raw.info, meg=True)
raw.drop_channels([ch_name for ch_idx, ch_name in enumerate(raw.info['ch_names']) 
                   if ch_idx not in picks])
raw.resample(50)

# %%
# Define some variables needed later on.

info = raw.info
fwd = mne.read_forward_solution(fwd_fname)
src = fwd['src']

length = 50
sfreq = info['sfreq']
tstep = 1/sfreq
n_times = length / tstep
events = [[0, 0, 1]]
fmin, fmax = 1, 20
inv_method = 'dSPM'
jitter_factor = 0.01
n_perm = 500

# how many canonical correlations are computed
# and to which dimension to reduce the contrast data
n_cca_components = 1
n_contrast_components = 4

# penalties for CCA, use no penalty for now
penalty_behav_ratio=1.0
penalty_behav = 0.0
penalty_contrast_ratio = 0.0
penalty_contrast = 0.0

# first condition is from beginning to middle,
# while second is from middle to end
cond_1_ival = 0, length / 2
cond_2_ival = length / 2, length

# For reproducibility, fix the random state
rand_state = np.random.RandomState(23)

# %%
# Define function to get extended label spanning vertices around a label center.

def get_label(regexp, extent=5):
    """ get label by regexp
    """
    orig_label = mne.read_labels_from_annot(
        subject, regexp=regexp, subjects_dir=subjects_dir)[0]
    center = mne.label.select_sources(
        subject, orig_label, location='center', extent=0,
        subjects_dir=subjects_dir).vertices[0]
    label = mne.label.select_sources(
        subject, orig_label, location='center', extent=extent,
        subjects_dir=subjects_dir)
    return label, center

# %%
# Use the function to get labels for parietal, precentral and temporal areas in both hemis.

label_rh_par, center_rh_par = get_label('superiorparietal-rh')
label_lh_par, center_lh_par = get_label('superiorparietal-lh')
label_rh_prec, center_rh_prec = get_label('precentral-rh')
label_lh_prec, center_lh_prec = get_label('precentral-lh')
label_rh_temp, center_rh_temp = get_label('superiortemporal-rh')
label_lh_temp, center_lh_temp = get_label('superiortemporal-lh')

# %%
# Define function to simulate raw data.

def simulate(n_subjects, cond_1_deps, cond_2_deps):
    """ Generates raw data with alpha oscillations from two resting-state like conditions 
    around cortex modulated by behavioral variables
    """

    # make diagonal noise covariance and use it and fwd to make inverse operator
    cov = mne.make_ad_hoc_cov(info)
    inv = mne.minimum_norm.make_inverse_operator(info, fwd, cov)

    raws = []
    for subject_idx in range(n_subjects):

        source_simulator = mne.simulation.SourceSimulator(src, tstep=tstep)

        # let every subject have some subject-specific base activity
        base_amp = 100e-10 * np.exp(rand_state.randn()/10)

        # define helper functions to create alpha oscillations

        def alpha_wave(base_freq, length, phase):
            """ creates alpha wave with frequency and phase jitter """
            jitter_factor = 0.05
            return np.sin(2.0 * np.pi * 
                (base_freq * np.arange(length) * tstep +
                 np.cumsum(jitter_factor * rand_state.randn(int(length)))) + phase)

        def get_modulator():
            """ creates a boxcar-like random carrier wave """
            modulator = (np.array_split(np.ones(int(n_times / 2)), int(n_times/20)) + 
                         np.array_split(np.zeros(int(n_times / 2)), int(n_times/20)))
            rand_state.shuffle(modulator)
            modulator = np.concatenate(modulator, axis=0)
            return modulator

        # Add some base alpha activity to superior temporal areas, where both conditions
        # behave similarly
        source_signal = np.ones(int(n_times)) * alpha_wave(10, n_times, 0) * base_amp
        source_simulator.add_data(label_lh_temp, source_signal*get_modulator(), events)
        source_simulator.add_data(label_rh_temp, source_signal*get_modulator(), events)

        # Add activity that depends on the behav variables
        cond_1_factor = np.prod(np.exp(cond_1_deps[subject_idx]))
        cond_2_factor = np.prod(np.exp(cond_2_deps[subject_idx]))
        modulator = get_modulator()
        cond_1_signal = np.concatenate([np.ones(int(n_times/2)), np.zeros(int(n_times/2))])
        cond_1_signal = cond_1_signal * alpha_wave(10, n_times, 0) * base_amp * cond_1_factor
        cond_2_signal = np.concatenate([np.zeros(int(n_times/2)), np.ones(int(n_times/2))])
        cond_2_signal = cond_2_signal * alpha_wave(10, n_times, 0) * base_amp * cond_2_factor
        source_signal = cond_1_signal + cond_2_signal
        source_simulator.add_data(label_lh_par, source_signal*modulator, events)
        source_simulator.add_data(label_rh_par, source_signal*modulator, events)

        # Add activity that is independent of the behavioral variables but which differs
        # between the first and second condition.
        cond_1_factor, cond_2_factor = 2, 0.5
        modulator = get_modulator()
        cond_1_signal = np.concatenate([np.ones(int(n_times/2)), np.zeros(int(n_times/2))])
        cond_1_signal = cond_1_signal * alpha_wave(10, n_times, 0) * base_amp * cond_1_factor
        cond_2_signal = np.concatenate([np.zeros(int(n_times/2)), np.ones(int(n_times/2))])
        cond_2_signal = cond_2_signal * alpha_wave(10, n_times, 0) * base_amp * cond_2_factor
        source_signal = cond_1_signal + cond_2_signal
        source_simulator.add_data(label_lh_prec, source_signal*modulator, events)
        source_simulator.add_data(label_rh_prec, source_signal*modulator, events)

        # Simulate these sources to create raw object
        raw = mne.simulation.simulate_raw(info, source_simulator, forward=fwd)

        # Add some 1/f noise to sensors with spatial structure induced by cov.
        mne.simulation.add_noise(raw, cov, iir_filter=[0.2, -0.2, 0.04], random_state=rand_state)

        raws.append(raw)

    return raws, behav_data, inv

# %% 
# Now that there is a simulation function, simulate data.
# The simulation sets up three areas of activation (temporal, parietal and precentral),
# and two different conditions (0s to 25s, and 25s to 50s).
# In to temporal areas, we put an oscillatory dipole of 10Hz that oscillates 
# on some base amplitude, which varies subject by subject, and does not depend on the condition. 
# To precentral areas we put oscillatory dipole of 10Hz, that has amplitude of 2 * base amplitude
# in the first condition and 0.5 * base amplitude in the second condition, giving a "constant" 
# difference between the conditions.
# To parietal areas we put oscillatory dipole of 10Hz, which can be set to vary with respect to
# behavioral variables. Here we make it so that the first condition does not vary with the 
# behavioral variables, but the second condition is very correlated with the first
# behavioral variable.

n_subjects = 10

# Generate behav data from multivariate normal distribution
behav_mean = [0, 0]
behav_cov = [[1, 0],
             [0, 1]]
behav_data = []
for idx in range(n_subjects):
    behav_data.append(rand_state.multivariate_normal(behav_mean, behav_cov))
behav_data = np.array(behav_data)

# Make second condition positively correlated with the first behav variable
cond_1_deps = np.zeros((n_subjects, 2))
cond_2_deps = np.array([behav_data[:, 0], np.zeros(n_subjects)]).T

raws, behavs, inv = simulate(n_subjects, cond_1_deps, cond_2_deps)

# %%
# Take a look at the first raw.

raws[0].plot()

# %%
# Plot how the simulated data looks as a PSD averaged over channels.

fig, (ax_1, ax_2) = plt.subplots(2)
ax_1.set_title('Cond 1')
ax_2.set_title('Cond 2')
fig.suptitle('PSDs in sensor space')
for raw in raws:
    psds, freqs = mne.time_frequency.psd_welch(
        raw, fmin=fmin, fmax=fmax, n_fft=int(sfreq),
        tmin=cond_1_ival[0], tmax=cond_1_ival[1])
    ax_1.plot(freqs, np.mean(psds, axis=0))

    psds, freqs = mne.time_frequency.psd_welch(
        raw, fmin=fmin, fmax=fmax, n_fft=int(sfreq),
        tmin=cond_2_ival[0], tmax=cond_2_ival[1])
    ax_2.plot(freqs, np.mean(psds, axis=0))

fig.tight_layout()
plt.show()

# %%
# Define functions for computing activation maps, on which contrast maps are based on.
# Contrast maps are spatial maps, computed by subtracting spatial 
# alpha power of first condition from spatial alpha power of second condition.

def compute_activation_maps(raw, inv):

    tmin, tmax = cond_1_ival[0], cond_1_ival[1]
    cond_1_psd = mne.minimum_norm.compute_source_psd(
        raw.copy().crop(tmin+1, tmax-1), inv, method=inv_method, 
        fmin=fmin, fmax=fmax,
        n_fft=sfreq, pick_ori=None, dB=False)
    
    tmin, tmax = cond_2_ival[0], cond_2_ival[1]
    cond_2_psd = mne.minimum_norm.compute_source_psd(
        raw.copy().crop(tmin+1, tmax-1), inv, method=inv_method, 
        fmin=fmin, fmax=fmax,
        n_fft=sfreq, pick_ori=None, dB=False)

    freqs = cond_1_psd.times

    # compute averages over alpha frequency band in the two conditions
    cond_1_act = np.mean(cond_1_psd._data[:, (freqs >= 7) & (freqs <= 13)], axis=1)
    cond_2_act = np.mean(cond_2_psd._data[:, (freqs >= 7) & (freqs <= 13)], axis=1)

    vertices = cond_1_psd.vertices

    return cond_1_act, cond_2_act, cond_1_psd, cond_2_psd, freqs, vertices

# %%
# Compute contrast maps using the function.

contrast_maps = []
cond_1_psds = []
cond_2_psds = []
for raw in raws:
    result = compute_activation_maps(raw, inv)
    contrast_maps.append(result[1] - result[0])
    cond_1_psds.append(result[2])
    cond_2_psds.append(result[3])
    freqs = result[4]
    vertices = result[5]

# %% 
# Plot the PSDs that the contrast maps are based on.

fig, (ax_1, ax_2) = plt.subplots(2)
fig.suptitle('PSDs in source space')
ax_1.set_title('Cond 1')
for psd in cond_1_psds:
    ax_1.plot(freqs, np.mean(psd.data, axis=0))

ax_2.set_title('Cond 2')
for psd in cond_2_psds:
    ax_2.plot(freqs, np.mean(psd.data, axis=0))

fig.tight_layout()
plt.show()

# %%
# Define function for plotting contrast maps.

def plot_contrast_map(contrast_map, vertices):

    contrast_map = mne.SourceEstimate(contrast_map, vertices, 
                                      tmin=0, tstep=1, subject='sample')
    brain = contrast_map.plot(
        'sample', 
        subjects_dir=subjects_dir, 
        hemi='both', alpha=1.0,
        size=600, title='Contrast map',
        colorbar=False,
        time_viewer=False)

    # add centers corresponding to the labels defined earlier
    brain.add_foci(center_rh_par, coords_as_verts=True, hemi='rh')
    brain.add_foci(center_lh_par, coords_as_verts=True, hemi='lh')
    brain.add_foci(center_rh_prec, coords_as_verts=True, hemi='rh')
    brain.add_foci(center_lh_prec, coords_as_verts=True, hemi='lh')
    brain.add_foci(center_rh_temp, coords_as_verts=True, hemi='rh')
    brain.add_foci(center_lh_temp, coords_as_verts=True, hemi='lh')

    brain.show_view(view={'azimuth': 0, 'elevation': 0, 'distance': 550,
                          'focalpoint': [0, 0, 0]})

# %%
# Plot average contrast map over all subjects.

plot_contrast_map(np.mean(contrast_maps, axis=0), vertices)

# %%
# Note that the activation is seen especially in the precentral area, where we 
# set up "constant" difference. It is not seen in the temporal areas, 
# as there the conditions do not differ. It is also not seen in the 
# parietal areas, as the differences cancel out there.
# Next, define function for CCA computation.

def compute_cca(contrast_data, behav_data, n_contrast_components, n_cca_components):

    # rank transform and standardize behav variables
    behav_data = np.array([scipy.stats.rankdata(elem) for elem in np.array(behav_data).T]).T
    behav_wh = (behav_data - np.mean(behav_data, axis=0)) / np.std(behav_data, axis=0)

    # rank transform and reduce dimensionality for contrast data
    contrast_data = np.array([scipy.stats.rankdata(elem) for elem in np.array(contrast_data).T]).T
    contrast_pca = PCA(
        n_components=n_contrast_components, whiten=True, random_state=rand_state)
    contrast_wh = contrast_pca.fit_transform(contrast_data)
    contrast_mixing = contrast_pca.components_

    # use partial least squares based CCA from Mai et al (2019).
    cca_contrast_weights, cca_behav_weights = cca_ipls(
        contrast_wh, behav_wh, 
        alpha_lambda_ratio=penalty_behav_ratio,
        alpha_lambda=penalty_behav, 
        beta_lambda=penalty_contrast, 
        beta_lambda_ratio=penalty_contrast_ratio,
        standardize=False,
        n_pairs=n_cca_components, glm_impl='pyglmnet')

    return cca_contrast_weights, cca_behav_weights, contrast_mixing, contrast_wh, behav_wh

# %%
# Use the function to compute the CCA.

cca_contrast_weights, cca_behav_weights, contrast_mixing, contrast_wh, behav_wh = compute_cca(
    contrast_maps, behavs, n_contrast_components=n_contrast_components, 
    n_cca_components=n_cca_components)

# %% 
# Define function for plotting canonical weights of behavioral variables.

def plot_behav_weights(comp_idx, cca_behav_weights):

    behav_weights = cca_behav_weights[:, comp_idx]

    fig, ax = plt.subplots()
    behav_vars = ['Var ' + str(behav_idx+1) for behav_idx in range(len(behav_weights))]
    ax.bar(behav_vars, behav_weights, align='center', alpha=1.0, width=0.5)
    ax.axhline(0)
    ax.set_ylabel('Weight')
    ax.set_xlabel('Behavioral variable')
    plt.show()

# %% 
# Define function for plotting canonical weights of contrast variables.

def plot_contrast_weights(comp_idx, cca_contrast_weights, contrast_mixing):

    contrast_weights = np.dot(cca_contrast_weights[:, comp_idx], contrast_mixing)
    plot_contrast_map(contrast_weights, vertices)

# %% 
# Define function for scatter plot to visualize canonical correlation.

def plot_cca_scatter(comp_idx, contrast_wh, behav_wh, cca_contrast_weights, cca_behav_weights):

    X = np.dot(contrast_wh, cca_contrast_weights[:, comp_idx])
    Y = np.dot(behav_wh, cca_behav_weights[:, comp_idx])

    fig, ax = plt.subplots()
    ax.scatter(X, Y, s=100)

    left = np.min(X) - np.max(np.abs(X))*0.1
    right = np.max(X) + np.max(np.abs(X))*0.1

    a, b = np.polyfit(X, Y, 1)
    ax.plot(np.linspace(left, right, 2), a*np.linspace(left, right, 2) + b)

    ax.set_xlim(left, right)
    ax.set_ylim(np.min(Y) - np.max(np.abs(Y))*0.4,
                np.max(Y) + np.max(np.abs(Y))*0.4)

    ax.set_ylabel('Behavioral correlate')
    ax.set_xlabel('Brain corralate')

    plt.show()

# %%
# With the functions defined, plot behav weights.

plot_behav_weights(0, cca_behav_weights)

# %%
# Plot contrast weights.

plot_contrast_weights(0, cca_contrast_weights, contrast_mixing)

# %%
# As we see, now the activity is localized to the parietal areas. This is because
# that is the only place where activity depends on the behav variables.
# Plot scatter plot.

plot_cca_scatter(0, contrast_wh, behav_wh, cca_contrast_weights, cca_behav_weights)

# %%
# Define function for running permuted versions of cca.

def permutations(contrast_wh, behav_wh, cca_contrast_weights, cca_behav_weights, n_perm):
    perm_stats = []
    for perm_idx, ordering in enumerate([rand_state.permutation(behav_wh.shape[0]) 
                                         for _ in range(n_perm)]):

        # use contrast variables as is is but permutate behav variables
        contrast_perm = contrast_wh.copy()
        behav_perm = behav_wh[ordering, :]

        cca_contrast_weights_perm, cca_behav_weights_perm = cca_ipls(
            contrast_perm, behav_perm, 
            alpha_lambda_ratio=penalty_behav_ratio,
            alpha_lambda=penalty_behav, 
            beta_lambda=penalty_contrast, 
            beta_lambda_ratio=penalty_contrast_ratio,
            standardize=False,
            n_pairs=n_cca_components, glm_impl='pyglmnet')

        # if n_cca_components > 1, use best coef as it might not 
        # always be the first due penalties
        corrcoefs = []
        for comp_idx in range(n_cca_components):
            X = np.dot(contrast_perm, cca_contrast_weights_perm[:, comp_idx])
            Y = np.dot(behav_perm, cca_behav_weights_perm[:, comp_idx])
            corrcoefs.append(np.corrcoef(X, Y)[0, 1])
        perm_stats.append(np.max(corrcoefs))

    # The first canonical correlation using the weights computed previously.
    X = np.dot(contrast_wh, cca_contrast_weights[:, 0])
    Y = np.dot(behav_wh, cca_behav_weights[:, 0])
    sample_stat = np.corrcoef(X, Y)[0, 1]

    # Compute fraction of coefs from permutations that are higher than the
    # sample coefficient.
    pvalue = len(list(filter(bool, perm_stats > sample_stat))) / n_perm
    print("Corrcoef for first component: " + str(round(sample_stat, 4)) + 
          " (pvalue " + str(pvalue) + ")")

# %%
# Run permutations.

permutations(contrast_wh, behav_wh, cca_contrast_weights, cca_behav_weights, n_perm)

# %%
# Let's experiment a bit and try increasing n_subjects to 30.

n_subjects = 30

behav_data = []
for idx in range(n_subjects):
    behav_data.append(rand_state.multivariate_normal(behav_mean, behav_cov))
behav_data = np.array(behav_data)

cond_1_deps = np.zeros((n_subjects, 2))
cond_2_deps = np.array([behav_data[:, 0], np.zeros(n_subjects)]).T

raws, behavs, inv = simulate(n_subjects, cond_1_deps, cond_2_deps)

contrast_maps = []
for raw in raws:
    result = compute_activation_maps(raw, inv)
    contrast_maps.append(result[1] - result[0])

cca_contrast_weights, cca_behav_weights, contrast_mixing, contrast_wh, behav_wh = compute_cca(
    contrast_maps, behavs, n_contrast_components=n_contrast_components, 
    n_cca_components=n_cca_components)

# %%
# Plot behav weights.

plot_behav_weights(0, cca_behav_weights)

# %%
# Plot contrast weights.

plot_contrast_weights(0, cca_contrast_weights, contrast_mixing)

# %%
# Show the scatter plot.

plot_cca_scatter(0, contrast_wh, behav_wh, cca_contrast_weights, cca_behav_weights)

# %% 
# And the permutation test result.

permutations(contrast_wh, behav_wh, cca_contrast_weights, cca_behav_weights, n_perm)

# %%
# Let's next try making the correlation much weaker, from 1.0 to 0.6, 
# keeping the same n_subjects.
# First define a function that when given a vector generates another with prespecified correlation to the first one.

def generate_correlated(x, corr):
    Y = np.random.randn(len(x), 2)
    C = cholesky([[1, corr], [corr, 1]])
    Y[:, 0] = x
    return np.matmul(Y, C)[:, 1]

# %%
# Use the function for dependency structure.

cond_1_deps = np.zeros((n_subjects, 2))
cond_2_deps = np.array([generate_correlated(behav_data[:, 0], 0.6), np.zeros(n_subjects)]).T

# %%
# And go on to simulate.

raws, behavs, inv = simulate(n_subjects, cond_1_deps, cond_2_deps)

contrast_maps = []
for raw in raws:
    result = compute_activation_maps(raw, inv)
    contrast_maps.append(result[1] - result[0])

cca_contrast_weights, cca_behav_weights, contrast_mixing, contrast_wh, behav_wh = compute_cca(
    contrast_maps, behavs, n_contrast_components=n_contrast_components, 
    n_cca_components=n_cca_components)

# %%
# Plot behav weights.

plot_behav_weights(0, cca_behav_weights)

# %%
# Plot contrast weights.

plot_contrast_weights(0, cca_contrast_weights, contrast_mixing)

# %%
# Show the scatter plot.

plot_cca_scatter(0, contrast_wh, behav_wh, cca_contrast_weights, cca_behav_weights)

# %% 
# And the permutation test result.

permutations(contrast_wh, behav_wh, cca_contrast_weights, cca_behav_weights, n_perm)

# %% 
# For curiosity, let's see what happens if there is a two-way dependency, that is,
# the second condition is positively correlated with the first behav variable and
# negatively correlated with the second behav variable.

cond_1_deps = np.zeros((n_subjects, 2))
cond_2_deps = np.array([behav_data[:, 0], -behav_data[:, 1]]).T

# %%
# And go on to simulate.

raws, behavs, inv = simulate(n_subjects, cond_1_deps, cond_2_deps)

contrast_maps = []
for raw in raws:
    result = compute_activation_maps(raw, inv)
    contrast_maps.append(result[1] - result[0])

cca_contrast_weights, cca_behav_weights, contrast_mixing, contrast_wh, behav_wh = compute_cca(
    contrast_maps, behavs, n_contrast_components=n_contrast_components, 
    n_cca_components=n_cca_components)

# %%
# Plot behav weights.

plot_behav_weights(0, cca_behav_weights)

# %%
# Plot contrast weights.

plot_contrast_weights(0, cca_contrast_weights, contrast_mixing)

# %%
# Show the scatter plot.

plot_cca_scatter(0, contrast_wh, behav_wh, cca_contrast_weights, cca_behav_weights)

# %% 
# And the permutation test result.

permutations(contrast_wh, behav_wh, cca_contrast_weights, cca_behav_weights, n_perm)

