"""Initial clustering

Before template matching and before split/merge, we need to initialize
unit labels. Here, we implement methods for initializing the unit labels
inside shorter chunks (`cluster_chunk`) and across groups of shorter
chunks (`cluster_across_chunks`).

These functions expect inputs which are the HDF5 files that come from
running a BasePeeler on one or more chunks. So, they are expected to be
combined with calls to `main.subtract()`, as implemented in the
`main.initial_clustering` function (TODO!).
"""
import h5py
from dartsort.util.data_util import DARTsortSorting

from . import cluster_util


def cluster_chunk(
    peeling_hdf5_filename,
    motion_est=None,
    strategy="closest_registered_channels",
    chunk_size_s=300,
):
    """Cluster spikes from a single segment

    Arguments
    ---------
    peeling_hdf5_filename : str or Path
    motion_est : optional dredge.motion_util.MotionEstimate
    strategy : one of "closest_registered_channels" or other choices tba

    Returns
    -------
    sorting : DARTsortSorting
    """
    assert strategy in ("closest_registered_channels","hdbscan","ensembling_hdbscan",)
    

    if strategy == "closest_registered_channels":
        with h5py.File(peeling_hdf5_filename, "r") as h5:
            times_samples = h5["times_samples"][:]
            channels = h5["channels"][:]
            times_s = h5["times_seconds"][:]
            xyza = h5["point_source_localizations"][:]
            amps = h5["denoised_amplitudes"][:]
            geom = h5["geom"][:]
        labels = cluster_util.closest_registered_channels(
            times_s, xyza[:, 0], xyza[:, 2], geom, motion_est
        )
        sorting = DARTsortSorting(
            times_samples=times_samples,
            channels=channels,
            labels=labels,
            extra_features=dict(
                point_source_localizations=xyza,
                denoised_amplitudes=amps,
                times_seconds=times_s,
            ),
        )
    elif strategy == "hdbscan":
        with h5py.File(peeling_hdf5_filename, "r") as h5:
            times_samples = h5["times_samples"][:]
            channels = h5["channels"][:]
            times_s = h5["times_seconds"][:]
            xyza = h5["point_source_localizations"][:]
            amps = h5["denoised_amplitudes"][:]
            geom = h5["geom"][:]
        labels = cluster_util.hdbscan_clustering(
            times_s, xyza[:, 0], xyza[:, 2], geom, amps, motion_est
        )
        sorting = DARTsortSorting(
            times_samples=times_samples,
            channels=channels,
            labels=labels,
            extra_features=dict(
                point_source_localizations=xyza,
                denoised_amplitudes=amps,
                times_seconds=times_s,
            ),
        )
    elif strategy == "ensembling_hdbscan":
        with h5py.File(peeling_hdf5_filename, "r") as h5:
            times_samples = h5["times_samples"][:]
            channels = h5["channels"][:]
            times_s = h5["times_seconds"][:]
            xyza = h5["point_source_localizations"][:]
            amps = h5["denoised_amplitudes"][:]
            geom = h5["geom"][:]
        labels = cluster_util.ensembling_hdbscan(
            times_s, xyza[:, 0], xyza[:, 2], geom, amps, motion_est, chunk_size_s,
        )
        sorting = DARTsortSorting(
            times_samples=times_samples,
            channels=channels,
            labels=labels,
            extra_features=dict(
                point_source_localizations=xyza,
                denoised_amplitudes=amps,
                times_seconds=times_s,
            ),
        )
    else:
        raise ValueError
    return sorting

