import contextlib
import gc
import h5py
import numpy as np
import signal
import time
import torch
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

from collections import namedtuple

try:
    from spikeglx import _geometry_from_meta, read_meta_data
except ImportError:
    try:
        from ibllib.io.spikeglx import _geometry_from_meta, read_meta_data
    except ImportError:
        raise ImportError("Can't find spikeglx...")
from pathlib import Path
from sklearn.decomposition import PCA
from tqdm.auto import tqdm

from . import denoise, detect, localize_index
from .spikeio import get_binary_length, read_data, read_waveforms_in_memory
from .waveform_utils import make_channel_index, make_contiguous_channel_index


def subtraction(
    standardized_bin,
    out_folder,
    geom=None,
    tpca_rank=8,
    n_sec_chunk=1,
    n_sec_pca=10,
    t_start=0,
    t_end=None,
    sampling_rate=30_000,
    thresholds=[12, 10, 8, 6, 5, 4],
    peak_sign="neg",
    nn_detect=False,
    denoise_detect=False,
    do_nn_denoise=True,
    neighborhood_kind="firstchan",
    extract_box_radius=200,
    extract_firstchan_n_channels=40,
    box_norm_p=np.inf,
    spike_length_samples=121,
    trough_offset=42,
    dedup_spatial_radius=70,
    enforce_decrease_kind="columns",
    nsync=0,
    n_jobs=1,
    device=None,
    save_residual=False,
    save_waveforms=False,
    do_clean=True,
    localization_kind="logbarrier",
    localize_radius=100,
    localize_firstchan_n_channels=20,
    loc_workers=4,
    overwrite=False,
    random_seed=0,
    binary_dtype=np.float32,
    dtype=np.float32,
):
    """Subtraction-based waveform extraction

    Runs subtraction pipeline, and optionally also the localization.

    Loads data from a binary file (standardized_bin), and loads geometry
    from the associated meta file if `geom=None`.

    Results are saved to `out_folder` in the following format:
     - residual_[dataset name]_[time region].bin
        - a binary file like the input binary
     - subtraction_[dataset name]_[time region].h5
        - An HDF5 file containing all of the resulting data.
          In detail, if N is the number of discovered waveforms,
          n_channels is the number of channels in the probe,
          T is `spike_len_samples`, C is the number of channels
          of the extracted waveforms (determined from `extract_box_radius`),
          then this HDF5 file contains the following datasets:

            geom : (n_channels, 2)
            start_sample : scalar
            end_sample : scalar
                First and last sample of time region considered
                (controlled by arguments `t_start`, `t_end`)
            channel_index : (n_channels, C)
                Array of channel indices. channel_index[c] contains the
                channels that a waveform with max channel `c` was extracted
                on.
            tpca_mean, tpca_components : arrays
                The fitted temporal PCA parameters.
            spike_index : (N, 2)
                The columns are (sample, max channel)
            subtracted_waveforms : (N, T, C)
                Waveforms that were subtracted
            cleaned_waveforms : (N, T, C)
                Final denoised waveforms, only computed/saved if
                `do_clean=True`
            localizations : (N, 5)
                Only computed/saved if `localization_kind` is `"logbarrier"`
                or `"original"`
                The columsn are: x, y, z, alpha, z relative to max channel
            maxptps : (N,)
                Only computed/saved if `localization_kind="logbarrier"`

    Returns
    -------
    out_h5 : path to output hdf5 file
    residual : path to residual if save_residual
    """
    if neighborhood_kind not in ("firstchan", "box", "circle"):
        raise ValueError(
            "Neighborhood kind", neighborhood_kind, "not understood."
        )
    if enforce_decrease_kind not in ("columns", "radial", "none"):
        raise ValueError(
            "Enforce decrease method", enforce_decrease_kind, "not understood."
        )
    if peak_sign not in ("neg", "both"):
        raise ValueError("peak_sign", peak_sign, "not understood.")

    standardized_bin = Path(standardized_bin)
    stem = standardized_bin.stem
    batch_len_samples = n_sec_chunk * sampling_rate

    # prepare output dir
    out_folder = Path(out_folder)
    out_folder.mkdir(exist_ok=True)
    batch_data_folder = out_folder / f"batches_{stem}"
    batch_data_folder.mkdir(exist_ok=True)
    out_h5 = out_folder / f"subtraction_{stem}_t_{t_start}_{t_end}.h5"
    if save_residual:
        residual_bin = out_folder / f"residual_{stem}_t_{t_start}_{t_end}.bin"
    try:
        if out_h5.exists():
            with h5py.File(out_h5, "r+") as _:
                pass
            del _
    except BlockingIOError as e:
        raise ValueError(
            f"Output HDF5 {out_h5} is currently in use by another program. "
            "Maybe a Jupyter notebook that's still running?"
        ) from e

    # pick device if it's not supplied
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        if device.type == "cuda":
            torch.cuda._lazy_init()
            torch.set_grad_enabled(False)
    else:
        device = torch.device(device)

    # if no geometry is supplied, try to load it from meta file
    if geom is None:
        geom = read_geom_from_meta(standardized_bin)
        if geom is None:
            raise ValueError(
                "Either pass `geom` or put meta file in folder with binary."
            )
    n_channels = geom.shape[0]
    ncols = len(np.unique(geom[:, 0]))

    # TODO: read this from meta.
    # right now it's just used to load NN detector and pick the enforce
    # decrease method if enforce_decrease_kind=="columns"
    probe = {4: "np1", 2: "np2"}.get(ncols, None)

    # figure out if we will use a NN detector, and if so which
    nn_detector_path = None
    if nn_detect:
        nn_detector_path = (
            Path(__file__).parent.parent / f"pretrained/detect_{probe}.pt"
        )
        print("Using pretrained detector for", probe, "from", nn_detector_path)
        detection_kind = "voltage->NN"
    elif denoise_detect:
        print("Using denoising NN detection")
        detection_kind = "denoised PTP"
    else:
        print("Using voltage detection")
        detection_kind = "voltage"

    # figure out length of data
    T_samples, T_sec = get_binary_length(
        standardized_bin,
        n_channels,
        sampling_rate,
        nsync=nsync,
        dtype=binary_dtype,
    )
    assert t_start >= 0 and (t_end is None or t_end <= T_sec)
    start_sample = int(np.floor(t_start * sampling_rate))
    end_sample = (
        T_samples if t_end is None else int(np.floor(t_end * sampling_rate))
    )
    portion_len_s = (end_sample - start_sample) / sampling_rate
    print(
        f"Running subtraction. Total recording length is {T_sec:0.2f} "
        f"s, running on portion of length {portion_len_s:0.2f} s. "
        f"Using {detection_kind} detection with thresholds: {thresholds}."
    )

    # compute helper data structures
    # channel indexes for extraction, NN detection, deduplication
    dedup_channel_index = make_channel_index(
        geom, dedup_spatial_radius, steps=2
    )
    nn_channel_index = make_channel_index(geom, dedup_spatial_radius, steps=1)
    if neighborhood_kind == "box":
        extract_channel_index = make_channel_index(
            geom, extract_box_radius, distance_order=False, p=box_norm_p
        )
        # use radius-based localization neighborhood
        loc_n_chans = None
        loc_radius = localize_radius
    elif neighborhood_kind == "circle":
        extract_channel_index = make_channel_index(
            geom, extract_box_radius, distance_order=False, p=2
        )
        # use radius-based localization neighborhood
        loc_n_chans = None
        loc_radius = localize_radius
    elif neighborhood_kind == "firstchan":
        extract_channel_index = make_contiguous_channel_index(
            n_channels, n_neighbors=extract_firstchan_n_channels
        )

        # use old firstchan style localization neighborhood
        loc_n_chans = localize_firstchan_n_channels
        loc_radius = None
    else:
        assert False

    # helper data structure for radial enforce decrease
    do_enforce_decrease = True
    radial_parents = None
    if enforce_decrease_kind == "radial":
        radial_parents = denoise.make_radial_order_parents(
            geom, extract_channel_index, n_jumps_per_growth=1, n_jumps_parent=3
        )
    elif enforce_decrease_kind == "columns":
        pass
    else:
        print("Skipping enforce decrease.")
        do_enforce_decrease = False

    # check localization arg
    if localization_kind in ("original", "logbarrier"):
        print("Using", localization_kind, "localization")
        do_localize = True
    else:
        print("No localization")
        do_localize = False

    # pre-fit temporal PCA
    tpca = None
    if n_sec_pca is not None:
        # try to load old TPCA if it's around
        if not overwrite and out_h5.exists():
            with h5py.File(out_h5, "r") as output_h5:
                tpca = tpca_from_h5(output_h5)

        # otherwise, train it
        if tpca is None:
            with timer("Training TPCA"), torch.no_grad():
                tpca = train_pca(
                    standardized_bin,
                    spike_length_samples,
                    extract_channel_index,
                    geom,
                    radial_parents,
                    T_samples,
                    sampling_rate,
                    dedup_channel_index,
                    thresholds,
                    nn_detector_path,
                    denoise_detect,
                    nn_channel_index,
                    nsync=nsync,
                    peak_sign=peak_sign,
                    do_nn_denoise=do_nn_denoise,
                    do_enforce_decrease=do_enforce_decrease,
                    probe=probe,
                    n_sec_pca=n_sec_pca,
                    rank=tpca_rank,
                    random_seed=random_seed,
                    device=device,
                    binary_dtype=binary_dtype,
                    dtype=dtype,
                )

            # try to free up some memory on GPU that might have been used above
            if device.type == "cuda":
                gc.collect()
                torch.cuda.empty_cache()

    # if we're on GPU, we can't use processes, since each process will
    # have it's own torch runtime and those will use all the memory
    if device.type == "cuda":
        pass
    else:
        if loc_workers > 1:
            print(
                "Setting number of localization workers to 1. (Since "
                "you're on CPU, use a large n_jobs for parallelism.)"
            )
            loc_workers = 1

    # parallel batches
    jobs = list(
        enumerate(
            range(
                start_sample,
                end_sample,
                batch_len_samples,
            )
        )
    )

    # -- initialize storage
    with get_output_h5(
        out_h5,
        start_sample,
        end_sample,
        geom,
        extract_channel_index,
        tpca,
        neighborhood_kind,
        spike_length_samples,
        save_waveforms=save_waveforms,
        do_clean=do_clean,
        do_localize=do_localize,
        overwrite=overwrite,
        dtype=dtype,
    ) as (output_h5, last_sample):

        spike_index = output_h5["spike_index"]
        subtracted_tpca_projs = output_h5["subtracted_tpca_projs"]
        if neighborhood_kind == "firstchan":
            firstchans = output_h5["first_channels"]
        if save_waveforms:
            subtracted_wfs = output_h5["subtracted_waveforms"]
            if do_clean:
                cleaned_wfs = output_h5["cleaned_waveforms"]
        if localization_kind in ("original", "logbarrier"):
            locs = output_h5["localizations"]
            maxptps = output_h5["maxptps"]
            peak_heights = output_h5["peak_heights"]
            trough_depths = output_h5["trough_depths"]
            widths = output_h5["widths"]
        N = len(spike_index)

        # if we're resuming, filter out jobs we already did
        jobs = [
            (batch_id, start)
            for batch_id, start in jobs
            if start >= last_sample
        ]
        n_batches = len(jobs)

        # residual binary file -- append if we're resuming
        if save_residual:
            residual_mode = "ab" if last_sample > 0 else "wb"
            residual = open(residual_bin, mode=residual_mode)

        # now run subtraction in parallel
        jobs = (
            (
                batch_id,
                batch_data_folder,
                s_start,
                batch_len_samples,
                standardized_bin,
                thresholds,
                tpca,
                trough_offset,
                dedup_channel_index,
                spike_length_samples,
                extract_channel_index,
                start_sample,
                end_sample,
                do_clean,
                radial_parents,
                localization_kind,
                loc_workers,
                geom,
                do_enforce_decrease,
                probe,
                loc_n_chans,
                loc_radius,
                peak_sign,
                nsync,
                binary_dtype,
                dtype,
            )
            for batch_id, s_start in jobs
        )

        # no-threading/multiprocessing execution for debugging if n_jobs == 0
        Executor = ProcessPoolExecutor if n_jobs else MockPoolExecutor
        context = multiprocessing.get_context("spawn")
        manager = context.Manager() if n_jobs else None
        id_queue = manager.Queue() if n_jobs else MockQueue()

        n_jobs = n_jobs or 1
        if n_jobs < 0:
            n_jobs = multiprocessing.cpu_count() - 1

        for id in range(n_jobs):
            id_queue.put(id)

        with Executor(
            max_workers=n_jobs,
            mp_context=context,
            initializer=_subtraction_batch_init,
            initargs=(
                device,
                nn_detector_path,
                nn_channel_index,
                denoise_detect,
                do_nn_denoise,
                id_queue,
            ),
        ) as pool:
            for result in tqdm(
                pool.map(_subtraction_batch, jobs),
                total=n_batches,
                desc="Batches",
                smoothing=0,
            ):
                with noint:
                    N_new = result.N_new

                    # write new residual
                    if save_residual:
                        np.load(result.residual).tofile(residual)
                    Path(result.residual).unlink()

                    # skip if nothing new
                    if not N_new:
                        continue

                    # grow arrays as necessary and write results
                    subtracted_tpca_projs.resize(N + N_new, axis=0)
                    subtracted_tpca_projs[N:] = np.load(
                        result.subtracted_tpca_projs
                    )

                    if save_waveforms:
                        subtracted_wfs.resize(N + N_new, axis=0)
                        subtracted_wfs[N:] = np.load(result.subtracted_wfs)

                        if do_clean:
                            cleaned_wfs.resize(N + N_new, axis=0)
                            cleaned_wfs[N:] = np.load(result.cleaned_wfs)

                    spike_index.resize(N + N_new, axis=0)
                    spike_index[N:] = np.load(result.spike_index)

                    if do_localize:
                        locs.resize(N + N_new, axis=0)
                        locs[N:] = np.load(result.localizations)
                        maxptps.resize(N + N_new, axis=0)
                        maxptps[N:] = np.load(result.maxptps)
                        trough_depths.resize(N + N_new, axis=0)
                        trough_depths[N:] = np.load(result.trough_depths)
                        peak_heights.resize(N + N_new, axis=0)
                        peak_heights[N:] = np.load(result.peak_heights)
                        widths.resize(N + N_new, axis=0)
                        widths[N:] = np.load(result.widths)

                    if neighborhood_kind == "firstchan":
                        firstchans.resize(N + N_new, axis=0)
                        firstchans[N:] = extract_channel_index[
                            spike_index[N:, 1], 0
                        ]

                    # delete original files
                    Path(result.subtracted_tpca_projs).unlink()
                    Path(result.subtracted_wfs).unlink()
                    if do_clean:
                        Path(result.cleaned_wfs).unlink()
                    Path(result.spike_index).unlink()
                    if do_localize:
                        Path(result.localizations).unlink()
                        Path(result.maxptps).unlink()
                        Path(result.trough_depths).unlink()
                        Path(result.peak_heights).unlink()
                        Path(result.widths).unlink()

                    # update spike count
                    N += N_new

    # -- done!
    if save_residual:
        residual.close()
    print("Done. Detected", N, "spikes")
    print("Results written to:")
    if save_residual:
        print(residual_bin)
    print(out_h5)
    try:
        batch_data_folder.rmdir()
    except OSError as e:
        print(e)
    return out_h5


# -- subtraction routines


# the return type for `subtraction_batch` below
SubtractionBatchResult = namedtuple(
    "SubtractionBatchResult",
    [
        "N_new",
        "s_start",
        "s_end",
        "residual",
        "subtracted_wfs",
        "subtracted_tpca_projs",
        "cleaned_wfs",
        "spike_index",
        "batch_id",
        "localizations",
        "maxptps",
        "trough_depths",
        "peak_heights",
        "widths",
    ],
)


# Parallelism helpers
def _subtraction_batch(args):
    return subtraction_batch(
        *args,
        _subtraction_batch.device,
        _subtraction_batch.denoiser,
        _subtraction_batch.detector,
        _subtraction_batch.dn_detector,
    )


def _subtraction_batch_init(
    device,
    nn_detector_path,
    nn_channel_index,
    denoise_detect,
    do_nn_denoise,
    id_queue,
):
    """Thread/process initializer -- loads up neural nets"""
    rank = id_queue.get()

    torch.set_grad_enabled(False)
    if device.type == "cuda":
        if torch.cuda.device_count() > 1:
            device = torch.device(
                "cuda", index=rank % torch.cuda.device_count()
            )
            print(
                f"Worker {rank} using GPU {rank % torch.cuda.device_count()} "
                f"out of {torch.cuda.device_count()} available."
            )
        torch.cuda._lazy_init()
    _subtraction_batch.device = device

    time.sleep(rank)
    print(f"Worker {rank} init", flush=True)

    denoiser = None
    if do_nn_denoise:
        denoiser = denoise.SingleChanDenoiser()
        denoiser.load()
        denoiser.to(device)
    _subtraction_batch.denoiser = denoiser

    detector = None
    if nn_detector_path:
        detector = detect.Detect(nn_channel_index)
        detector.load(nn_detector_path)
        detector.to(device)
    _subtraction_batch.detector = detector

    dn_detector = None
    if denoise_detect:
        dn_detector = detect.DenoiserDetect(denoiser)
        dn_detector.to(device)
    _subtraction_batch.dn_detector = dn_detector


def subtraction_batch(
    batch_id,
    batch_data_folder,
    s_start,
    batch_len_samples,
    standardized_bin,
    thresholds,
    tpca,
    trough_offset,
    dedup_channel_index,
    spike_length_samples,
    extract_channel_index,
    start_sample,
    end_sample,
    do_clean,
    radial_parents,
    localization_kind,
    loc_workers,
    geom,
    do_enforce_decrease,
    probe,
    loc_n_chans,
    loc_radius,
    peak_sign,
    nsync,
    binary_dtype,
    dtype,
    device,
    denoiser,
    detector,
    dn_detector,
):
    """Runs subtraction on a batch

    This function handles the logic of loading data from disk
    (padding it with a buffer where necessary), running the loop
    over thresholds for `detect_and_subtract`, handling spikes
    that were in the buffer, and applying the denoising pipeline.

    A note on buffer logic:
     - We load a buffer of twice the spike length.
     - The outer buffer of size spike length is to ensure that
       spikes inside the inner buffer of size spike length can be
       loaded
     - We subtract spikes inside the inner buffer in `detect_and_subtract`
       to ensure consistency of the residual across batches.

    Arguments
    ---------
    batch_id : int
        Used when naming temporary batch result files saved to
        `batch_data_folder`. (Not used otherwise -- in particular
        this does not determine what data is loaded or processed.)
    batch_data_folder : string
        Where temporary results are being stored
    s_start : int
        The batch's starting time in samples
    batch_len_samples : int
        The length of a batch in samples
    standardized_bin : int
        The path to the standardized binary file
    thresholds : list of int
        Voltage thresholds for subtraction
    tpca : sklearn PCA object or None
        A pre-trained temporal PCA (or None in which case no PCA
        is applied)
    trough_offset : int
        42 in practice, the alignment of the max channel's trough
        in the extracted waveforms
    dedup_channel_index : int array (num_channels, num_neighbors)
        Spatial neighbor structure for deduplication
    spike_length_samples : int
        121 in practice, temporal length of extracted waveforms
    extract_channel_index : int array (num_channels, extract_channels)
        Channel neighborhoods for extracted waveforms
    device : string or torch.device
    start_sample, end_sample : int
        Temporal boundary of the region of the recording being
        considered (in samples)
    radial_parents
        Helper data structure for enforce_decrease
    localization_kind : str
        How should we run localization?
    loc_workers : int
        on how many threads?
    geom : array
        The probe geometry
    denoiser, detector : torch nns or None
    probe : string or None

    Returns
    -------
    res : SubtractionBatchResult
    """
    # we use a double buffer: inner buffer of length spike_length,
    # outer buffer of length spike_length
    #  - detections are restricted to the inner buffer
    #  - outer buffer allows detections on the border of the inner
    #    buffer to be loaded
    #  - using the inner buffer allows for subtracted residuals to be
    #    consistent (more or less) across batches
    #  - only the spikes in the center (i.e. not in either buffer)
    #    will be returned to the caller
    buffer = 2 * spike_length_samples

    # load raw data with buffer
    s_end = min(end_sample, s_start + batch_len_samples)
    n_channels = len(dedup_channel_index)
    load_start = max(start_sample, s_start - buffer)
    load_end = min(end_sample, s_end + buffer)
    residual = read_data(
        standardized_bin,
        binary_dtype,
        load_start,
        load_end,
        n_channels,
        nsync,
        out_dtype=dtype,
    )

    # 0 padding if we were at the edge of the data
    pad_left = pad_right = 0
    if load_start == start_sample:
        pad_left = buffer
    if load_end == end_sample:
        pad_right = buffer - (end_sample - s_end)
    if pad_left != 0 or pad_right != 0:
        residual = np.pad(
            residual, [(pad_left, pad_right), (0, 0)], mode="edge"
        )

    # now, no matter where we were, the data has the following shape
    assert residual.shape == (2 * buffer + s_end - s_start, n_channels)

    # main subtraction loop
    subtracted_wfs = []
    subtracted_tpca_projs = None
    if tpca is not None:
        subtracted_tpca_projs = []
    spike_index = []
    for threshold in thresholds:
        subwfs, subpcs, residual, spind = detect_and_subtract(
            residual,
            threshold,
            radial_parents,
            tpca,
            dedup_channel_index,
            extract_channel_index,
            peak_sign=peak_sign,
            detector=detector,
            denoiser=denoiser,
            denoiser_detector=dn_detector,
            trough_offset=trough_offset,
            spike_length_samples=spike_length_samples,
            device=device,
            do_enforce_decrease=do_enforce_decrease,
            probe=probe,
        )
        if len(spind):
            subtracted_wfs.append(subwfs)
            if tpca is not None:
                subtracted_tpca_projs.append(subpcs)
            spike_index.append(spind)

    # at this point, trough times in the spike index are relative
    # to the full buffer of length 2 * spike length

    # strip buffer from residual and remove spikes in buffer
    residual_singlebuf = residual[spike_length_samples:-spike_length_samples]
    if batch_data_folder is not None:
        residual = residual[buffer:-buffer]
        np.save(batch_data_folder / f"{batch_id:08d}_res.npy", residual)

    # return early if there were no spikes
    if batch_data_folder is None and not spike_index:
        # this return is used by `train_pca` as an early exit
        return spike_index, subtracted_wfs, residual_singlebuf
    elif not spike_index:
        return SubtractionBatchResult(
            N_new=0,
            s_start=s_start,
            s_end=s_end,
            residual=batch_data_folder / f"{batch_id:08d}_res.npy",
            subtracted_wfs=None,
            subtracted_tpca_projs=None,
            cleaned_wfs=None,
            spike_index=None,
            batch_id=batch_id,
            localizations=None,
            maxptps=None,
            peak_heights=None,
            trough_depths=None,
            widths=None,
        )

    subtracted_wfs = np.concatenate(subtracted_wfs, axis=0)
    if tpca is not None:
        subtracted_tpca_projs = np.concatenate(subtracted_tpca_projs, axis=0)
    spike_index = np.concatenate(spike_index, axis=0)

    # sort so time increases
    sort = np.argsort(spike_index[:, 0])
    subtracted_wfs = subtracted_wfs[sort]
    if tpca is not None:
        subtracted_tpca_projs = subtracted_tpca_projs[sort]
    spike_index = spike_index[sort]

    # get rid of spikes in the buffer
    # also, get rid of spikes too close to the beginning/end
    # of the recording if we are in the first or last batch
    spike_time_min = 0
    if s_start == start_sample:
        spike_time_min = trough_offset
    spike_time_max = s_end - s_start
    if load_end == end_sample:
        spike_time_max -= spike_length_samples - trough_offset

    minix = np.searchsorted(spike_index[:, 0], spike_time_min, side="left")
    maxix = np.searchsorted(spike_index[:, 0], spike_time_max, side="right")
    spike_index = spike_index[minix:maxix]
    subtracted_wfs = subtracted_wfs[minix:maxix]
    if tpca is not None:
        subtracted_tpca_projs = subtracted_tpca_projs[minix:maxix]

    # if caller passes None for the output folder, just return
    # the results now (eg this is used by train_pca)
    if batch_data_folder is None:
        return spike_index, subtracted_wfs, residual_singlebuf

    # get cleaned waveforms
    cleaned_wfs = None
    if do_clean:
        cleaned_wfs = subtracted_wfs
        if spike_index.size:
            cleaned_wfs = read_waveforms_in_memory(
                residual_singlebuf,
                spike_index,
                spike_length_samples,
                extract_channel_index,
                trough_offset=trough_offset,
                buffer=spike_length_samples,
            )
            cleaned_wfs = full_denoising(
                cleaned_wfs + subtracted_wfs,
                spike_index[:, 1],
                extract_channel_index,
                radial_parents,
                do_enforce_decrease=do_enforce_decrease,
                probe=probe,
                tpca=tpca,
                device=device,
                denoiser=denoiser,
            )

    # times relative to batch start
    # recall, these times were aligned to the double buffer, so we don't
    # need to adjust them according to the buffer at all.
    spike_index[:, 0] += s_start

    # save the results to disk to avoid memory issues
    N_new = len(spike_index)
    np.save(batch_data_folder / f"{batch_id:08d}_sub.npy", subtracted_wfs)
    np.save(
        batch_data_folder / f"{batch_id:08d}_subpc.npy", subtracted_tpca_projs
    )
    np.save(batch_data_folder / f"{batch_id:08d}_si.npy", spike_index)

    clean_file = None
    if do_clean:
        clean_file = batch_data_folder / f"{batch_id:08d}_clean.npy"
        np.save(clean_file, cleaned_wfs)

    localizations_file = (
        maxptps_file
    ) = peak_heights_file = trough_depths_file = widths_file = None
    if localization_kind in ("original", "logbarrier"):
        locwfs = cleaned_wfs if do_clean else subtracted_wfs
        locptps = locwfs.ptp(1)
        maxchans = np.nanargmax(locptps, axis=1)
        maxchan_traces = locwfs[np.arange(len(locwfs)), :, maxchans]
        trough_depths = maxchan_traces.min(1)
        peak_heights = maxchan_traces.max(1)
        first_peaks = maxchan_traces[:, :trough_offset].argmax(1)
        second_peaks = trough_offset + maxchan_traces[
            :, trough_offset:
        ].argmax(1)
        widths = second_peaks - first_peaks

        xs, ys, z_rels, z_abss, alphas = localize_index.localize_ptps_index(
            locptps,
            geom,
            spike_index[:, 1],
            extract_channel_index,
            n_channels=loc_n_chans,
            radius=loc_radius,
            n_workers=loc_workers,
            pbar=False,
            logbarrier=localization_kind == "logbarrier",
        )
        localizations_file = batch_data_folder / f"{batch_id:08d}_loc.npy"
        np.save(localizations_file, np.c_[xs, ys, z_abss, alphas, z_rels])
        maxptps_file = batch_data_folder / f"{batch_id:08d}_maxptp.npy"
        np.save(maxptps_file, np.nanmax(locptps, axis=1))
        trough_depths_file = batch_data_folder / f"{batch_id:08d}_tdepth.npy"
        np.save(trough_depths_file, trough_depths)
        peak_heights_file = batch_data_folder / f"{batch_id:08d}_pheight.npy"
        np.save(peak_heights_file, peak_heights)
        widths_file = batch_data_folder / f"{batch_id:08d}_width.npy"
        np.save(widths_file, widths)

    res = SubtractionBatchResult(
        N_new=N_new,
        s_start=s_start,
        s_end=s_end,
        residual=batch_data_folder / f"{batch_id:08d}_res.npy",
        subtracted_wfs=batch_data_folder / f"{batch_id:08d}_sub.npy",
        subtracted_tpca_projs=batch_data_folder / f"{batch_id:08d}_subpc.npy",
        cleaned_wfs=clean_file,
        spike_index=batch_data_folder / f"{batch_id:08d}_si.npy",
        batch_id=batch_id,
        localizations=localizations_file,
        maxptps=maxptps_file,
        peak_heights=peak_heights_file,
        trough_depths=trough_depths_file,
        widths=widths_file,
    )

    return res


# -- temporal PCA


def train_pca(
    standardized_bin,
    spike_length_samples,
    extract_channel_index,
    geom,
    radial_parents,
    len_recording_samples,
    sampling_rate,
    dedup_channel_index,
    thresholds,
    nn_detector_path,
    denoise_detect,
    nn_channel_index,
    subtraction_tpca=None,
    peak_sign="neg",
    do_nn_denoise=True,
    do_enforce_decrease=True,
    probe=None,
    n_sec_pca=10,
    rank=7,
    nsync=0,
    random_seed=0,
    device="cpu",
    trough_offset=42,
    binary_dtype=np.float32,
    dtype=np.float32,
):
    """Pre-train temporal PCA

    Extracts several random seconds of data by subtraction
    with no PCA, and trains a temporal PCA on the resulting
    waveforms.

    This same function is used to fit the subtraction TPCA and the
    collision-cleaned TPCA.
    """
    n_seconds = len_recording_samples // sampling_rate
    starts = sampling_rate * np.random.default_rng(random_seed).choice(
        n_seconds, size=min(n_sec_pca, n_seconds), replace=False
    )

    denoiser = None
    if do_nn_denoise:
        denoiser = denoise.SingleChanDenoiser().load().to(device)

    detector = None
    if nn_detector_path:
        detector = detect.Detect(nn_channel_index)
        detector.load(nn_detector_path)
        detector.to(device)

    dn_detector = None
    if denoise_detect:
        assert do_nn_denoise
        dn_detector = detect.DenoiserDetect(denoiser)
        dn_detector.to(device)

    # do a mini-subtraction with no PCA, just NN denoise and enforce_decrease
    spike_indices = []
    waveforms = []
    residuals = []
    for s_start in tqdm(starts, "PCA training subtraction"):
        spind, wfs, residual_singlebuf = subtraction_batch(
            0,
            None,
            s_start,
            sampling_rate,
            standardized_bin,
            thresholds,
            subtraction_tpca,
            trough_offset,
            dedup_channel_index,
            spike_length_samples,
            extract_channel_index,
            0,
            len_recording_samples,
            False,
            radial_parents,
            False,
            None,
            None,
            do_enforce_decrease,
            probe,
            None,
            None,
            peak_sign,
            nsync,
            binary_dtype,
            dtype,
            device,
            denoiser,
            detector,
            dn_detector,
        )
        spike_indices.append(spind)
        waveforms.append(wfs)
        residuals.append(residual_singlebuf)

    try:
        # this can raise value error
        spike_index = np.concatenate(spike_indices, axis=0)
        waveforms = np.concatenate(waveforms, axis=0)

        # but we can also be here...
        if waveforms.size == 0:
            raise ValueError
    except ValueError:
        raise ValueError(
            f"No waveforms found in the whole {n_sec_pca} training "
            "batches for TPCA, so we could not train it. Maybe you "
            "can increase n_sec_pca, but also maybe there are data "
            "or threshold issues?"
        )

    N, T, C = waveforms.shape
    print("Fitting PCA on", N, "waveforms from mini-subtraction")

    # get subtracted or collision-cleaned waveforms
    if subtraction_tpca is None:
        # we're fitting to subtracted wfs
        pass
    else:
        # otherwise, we are fitting the collision-cleaned TPCA,
        # so add back the residual
        waveforms += np.concatenate(
            [
                read_waveforms_in_memory(
                    res,
                    spike_index,
                    spike_length_samples,
                    extract_channel_index,
                    trough_offset=trough_offset,
                    buffer=spike_length_samples,
                )
                for res, spind in zip(residuals, spike_indices)
            ],
            axis=0,
        )

    # fit TPCA
    tpca = PCA(rank, random_state=random_seed)
    # extract waveforms for real channels
    wfs_in_probe = waveforms.transpose(0, 2, 1)
    in_probe_index = extract_channel_index < extract_channel_index.shape[0]
    wfs_in_probe = wfs_in_probe[in_probe_index[spike_index[:, 1]]]
    tpca.fit(wfs_in_probe)

    return tpca


# -- denoising / detection helpers


def detect_and_subtract(
    raw,
    threshold,
    radial_parents,
    tpca,
    dedup_channel_index,
    extract_channel_index,
    *,
    peak_sign="neg",
    detector=None,
    denoiser=None,
    denoiser_detector=None,
    nn_switch_threshold=4,
    trough_offset=42,
    spike_length_samples=121,
    device="cpu",
    do_enforce_decrease=True,
    probe=None,
):
    """Detect and subtract

    For a fixed voltage threshold, detect spikes, denoise them,
    and subtract them from the recording.

    This function is the core of the subtraction routine.

    Returns
    -------
    waveforms, subtracted_raw, spike_index
    """
    device = torch.device(device)

    kwargs = dict(nn_detector=None, nn_denoiser=None, denoiser_detector=None)
    kwargs["denoiser_detector"] = denoiser_detector
    if detector is not None and threshold <= nn_switch_threshold:
        kwargs["nn_detector"] = detector
        kwargs["nn_denoiser"] = denoiser

    start = spike_length_samples
    end = -spike_length_samples
    if denoiser_detector is not None:
        start = start - 42
        end = end + 79

    # the full buffer has length 2 * spike len on both sides,
    # but this spike index only contains the spikes inside
    # the inner buffer of length spike len
    # times are relative to the *inner* buffer
    spike_index = detect.detect_and_deduplicate(
        raw[start:end].copy(),
        threshold,
        dedup_channel_index,
        buffer_size=spike_length_samples,
        device=device,
        peak_sign=peak_sign,
        spike_length_samples=spike_length_samples,
        **kwargs,
    )
    if not spike_index.size:
        return [], [], raw, []

    # -- read waveforms
    padded_raw = np.pad(raw, [(0, 0), (0, 1)], constant_values=np.nan)
    # get times relative to trough + buffer
    # currently, times are trough times relative to spike_length_samples,
    # but they also *start* there
    # thus, they are actually relative to the full buffer
    # of length 2 * spike_length_samples
    time_range = np.arange(
        2 * spike_length_samples - trough_offset,
        3 * spike_length_samples - trough_offset,
    )
    time_ix = spike_index[:, 0, None] + time_range[None, :]
    chan_ix = extract_channel_index[spike_index[:, 1]]
    waveforms = padded_raw[time_ix[:, :, None], chan_ix[:, None, :]]

    # -- denoising
    waveforms, tpca_proj = full_denoising(
        waveforms,
        spike_index[:, 1],
        extract_channel_index,
        radial_parents,
        do_enforce_decrease=do_enforce_decrease,
        probe=probe,
        tpca=tpca,
        device=device,
        denoiser=denoiser,
        return_tpca_embedding=True,
    )

    # -- the actual subtraction
    # have to use subtract.at since -= will only subtract once in the overlaps,
    # subtract.at will subtract multiple times where waveforms overlap
    np.subtract.at(
        padded_raw,
        (time_ix[:, :, None], chan_ix[:, None, :]),
        waveforms,
    )
    # remove the NaN padding
    subtracted_raw = padded_raw[:, :-1]

    return waveforms, tpca_proj, subtracted_raw, spike_index


@torch.no_grad()
def full_denoising(
    waveforms,
    maxchans,
    extract_channel_index,
    radial_parents=None,
    do_enforce_decrease=True,
    probe=None,
    tpca=None,
    device=None,
    denoiser=None,
    batch_size=1024,
    align=False,
    return_tpca_embedding=False,
):
    """Denoising pipeline: neural net denoise, temporal PCA, enforce_decrease"""
    num_channels = len(extract_channel_index)
    N, T, C = waveforms.shape
    assert not align  # still working on that

    # in new pipeline, some channels are off the edge of the probe
    # those are filled with NaNs, which will blow up PCA. so, here
    # we grab just the non-NaN channels.

    in_probe_channel_index = extract_channel_index < num_channels
    in_probe_index = in_probe_channel_index[maxchans]
    waveforms = waveforms.transpose(0, 2, 1)
    wfs_in_probe = waveforms[in_probe_index]

    # Apply NN denoiser (skip if None) #doesn't matter if wf on channels or everywhere
    if denoiser is not None:
        results = []
        for bs in range(0, wfs_in_probe.shape[0], batch_size):
            be = min(bs + batch_size, N * C)
            results.append(
                denoiser(
                    torch.as_tensor(
                        wfs_in_probe[bs:be], device=device, dtype=torch.float
                    )
                )
                .cpu()
                .numpy()
            )
        wfs_in_probe = np.concatenate(results, axis=0)
        del results

    # everyone to numpy now, if we were torch
    if torch.is_tensor(waveforms):
        waveforms = np.array(waveforms.cpu())

    # Temporal PCA while we are still transposed
    if tpca is not None:
        wfs_in_probe = tpca.inverse_transform(tpca.transform(wfs_in_probe))

    # back to original shape
    waveforms[in_probe_index] = wfs_in_probe
    waveforms = waveforms.transpose(0, 2, 1)

    # enforce decrease
    if radial_parents is None and probe is not None:
        rel_maxchans = maxchans - extract_channel_index[maxchans, 0]
        if probe == "np1":
            for i in range(N):
                denoise.enforce_decrease_np1(
                    waveforms[i], max_chan=rel_maxchans[i], in_place=True
                )
        elif probe == "np2":
            for i in range(N):
                denoise.enforce_decrease(
                    waveforms[i], max_chan=rel_maxchans[i], in_place=True
                )
        else:
            assert False
    elif radial_parents is not None:
        denoise.enforce_decrease_shells(
            waveforms, maxchans, radial_parents, in_place=True
        )
    else:
        # no enforce decrease
        pass

    if return_tpca_embedding and tpca is not None:
        tpca_embeddings = np.empty(
            (N, C, tpca.n_components), dtype=waveforms.dtype
        )
        # run tpca only on channels that matter!
        tpca_embeddings[in_probe_index] = tpca.transform(
            waveforms.transpose(0, 2, 1)[in_probe_index]
        )
        return waveforms, tpca_embeddings.transpose(0, 2, 1)
    elif return_tpca_embedding:
        return waveforms, None

    return waveforms


# -- HDF5 initialization / resuming old job logic


@contextlib.contextmanager
def get_output_h5(
    out_h5,
    start_sample,
    end_sample,
    geom,
    extract_channel_index,
    tpca,
    neighborhood_kind,
    spike_length_samples,
    do_clean=True,
    do_localize=True,
    save_waveforms=True,
    overwrite=False,
    chunk_len=4096,
    dtype=np.float32,
):
    h5_exists = Path(out_h5).exists()
    last_sample = 0
    if h5_exists and not overwrite:
        output_h5 = h5py.File(out_h5, "r+")
        h5_exists = True
        if len(output_h5["spike_index"]) > 0:
            last_sample = output_h5["spike_index"][-1, 0]
    else:
        if overwrite:
            print("Overwriting previous results, if any.")
        output_h5 = h5py.File(out_h5, "w")

        # initialize datasets
        output_h5.create_dataset("geom", data=geom)
        output_h5.create_dataset("start_sample", data=start_sample)
        output_h5.create_dataset("end_sample", data=end_sample)
        output_h5.create_dataset("channel_index", data=extract_channel_index)
        if tpca is not None:
            output_h5.create_dataset("tpca_mean", data=tpca.mean_)
            output_h5.create_dataset("tpca_components", data=tpca.components_)

        # resizable datasets so we don't fill up space
        extract_channels = extract_channel_index.shape[1]
        output_h5.create_dataset(
            "subtracted_tpca_projs",
            shape=(0, tpca.components_.shape[0], extract_channels),
            chunks=(chunk_len, tpca.components_.shape[0], extract_channels),
            maxshape=(None, tpca.components_.shape[0], extract_channels),
            dtype=dtype,
        )
        if save_waveforms:
            output_h5.create_dataset(
                "subtracted_waveforms",
                shape=(0, spike_length_samples, extract_channels),
                chunks=(chunk_len, spike_length_samples, extract_channels),
                maxshape=(None, spike_length_samples, extract_channels),
                dtype=dtype,
            )
        output_h5.create_dataset(
            "spike_index",
            shape=(0, 2),
            chunks=(chunk_len, 2),
            maxshape=(None, 2),
            dtype=np.int64,
        )
        if neighborhood_kind == "firstchan":
            output_h5.create_dataset(
                "first_channels",
                shape=(0,),
                chunks=(chunk_len,),
                maxshape=(None,),
                dtype=np.int64,
            )
        if save_waveforms and do_clean:
            output_h5.create_dataset(
                "cleaned_waveforms",
                shape=(0, spike_length_samples, extract_channels),
                chunks=(chunk_len, spike_length_samples, extract_channels),
                maxshape=(None, spike_length_samples, extract_channels),
                dtype=dtype,
            )
        if do_localize:
            output_h5.create_dataset(
                "localizations",
                shape=(0, 5),
                chunks=(chunk_len, 5),
                maxshape=(None, 5),
                dtype=dtype,
            )
            output_h5.create_dataset(
                "maxptps",
                shape=(0,),
                chunks=(chunk_len,),
                maxshape=(None,),
                dtype=dtype,
            )
            output_h5.create_dataset(
                "peak_heights",
                shape=(0,),
                chunks=(chunk_len,),
                maxshape=(None,),
                dtype=dtype,
            )
            output_h5.create_dataset(
                "trough_depths",
                shape=(0,),
                chunks=(chunk_len,),
                maxshape=(None,),
                dtype=dtype,
            )
            output_h5.create_dataset(
                "widths",
                shape=(0,),
                chunks=(chunk_len,),
                maxshape=(None,),
                dtype=dtype,
            )

    done_percent = (
        100 * (last_sample - start_sample) / (end_sample - start_sample)
    )
    if h5_exists and not overwrite:
        print(f"Resuming previous job, which was {done_percent:.0f}% done")
    elif h5_exists and overwrite:
        last_sample = 0
    else:
        print("No previous output found, starting from scratch.")

    # try/finally ensures we close `output_h5` if job is interrupted
    # docs.python.org/3/library/contextlib.html#contextlib.contextmanager
    try:
        yield output_h5, last_sample
    finally:
        output_h5.close()


def tpca_from_h5(h5):
    tpca = None
    if "tpca_mean" in h5:
        tpca_mean = h5["tpca_mean"][:]
        tpca_components = h5["tpca_components"][:]
        if (tpca_mean == 0).all():
            print("H5 exists but TPCA params == 0, re-fit.")
        else:
            tpca = PCA(tpca_components.shape[0])
            tpca.mean_ = tpca_mean
            tpca.components_ = tpca_components
            print("Loaded TPCA from h5")
    return tpca


# -- data loading helpers


def read_geom_from_meta(bin_file):
    meta = Path(bin_file.parent) / (bin_file.stem + ".meta")
    if not meta.exists():
        raise ValueError("Expected", meta, "to exist.")
    header = _geometry_from_meta(read_meta_data(meta))
    geom = np.c_[header["x"], header["y"]]
    return geom


# -- utils


class MockPoolExecutor:
    """A helper class for turning off concurrency when debugging."""

    def __init__(
        self,
        max_workers=None,
        mp_context=None,
        initializer=None,
        initargs=None,
    ):
        initializer(*initargs)
        self.map = map

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return


class MockQueue:
    """Another helper class for turning off concurrency when debugging."""

    def __init__(self):
        self.q = []
        self.put = self.q.append
        self.get = lambda: self.q.pop(0)


class timer:
    def __init__(self, name="timer"):
        self.name = name

    def __enter__(self):
        self.start = time.time()
        return self

    def __exit__(self, *args):
        self.t = time.time() - self.start
        print(self.name, "took", self.t, "s")


class NoKeyboardInterrupt:
    """A context manager that we use to avoid ending up in invalid states."""

    def handler(self, *sig):
        if self.sig:
            signal.signal(signal.SIGINT, self.old_handler)
            sig, self.sig = self.sig, None
            self.old_handler(*sig)
        self.sig = sig

    def __enter__(self):
        self.old_handler = signal.signal(signal.SIGINT, self.handler)
        self.sig = None

    def __exit__(self, type, value, traceback):
        signal.signal(signal.SIGINT, self.old_handler)
        if self.sig:
            self.old_handler(*self.sig)


noint = NoKeyboardInterrupt()
