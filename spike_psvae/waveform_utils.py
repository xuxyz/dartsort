import numpy as np


def relativize_z(z_abs, maxchans, geom):
    """Take absolute z coords -> relative to max channel z."""
    return z_abs - geom[maxchans.astype(int), 1]


def maxchan_from_firstchan(firstchan, wf):
    return firstchan + wf.ptp(0).argmax()


def temporal_align(waveforms, offset=42):
    N, T, C = waveforms.shape
    maxchans = waveforms.ptp(1).argmax(1)
    offsets = waveforms[np.arange(N), :, maxchans].argmin(1)
    rolls = offset - offsets
    out = np.empty_like(waveforms)
    pads = [(0, 0), (0, 0)]
    for i, roll in enumerate(rolls):
        if roll > 0:
            pads[0] = (roll, 0)
            start, end = 0, T
        elif roll < 0:
            pads[0] = (0, -roll)
            start, end = -roll, T - roll
        else:
            out[i] = waveforms[i]
            continue

        pwf = np.pad(waveforms[i], pads, mode="linear_ramp")
        out[i] = pwf[start:end, :]

    return out


def get_local_chans(geom, firstchan, n_channels):
    """Gets indices of channels around the maxchan"""
    G, d = geom.shape
    assert d == 2
    assert not n_channels % 2

    # Deal with edge cases
    low = firstchan
    high = firstchan + n_channels
    assert low >= 0
    assert high <= G

    return low, high


def get_local_geom(
    geom,
    firstchan,
    maxchan,
    n_channels,
    return_z_maxchan=False,
):
    """Gets the geometry of some neighborhood of chans near maxchan"""
    low, high = get_local_chans(geom, firstchan, n_channels)
    local_geom = geom[low:high].copy()
    z_maxchan = geom[int(maxchan), 1]
    local_geom[:, 1] -= z_maxchan

    if return_z_maxchan:
        return local_geom, z_maxchan
    return local_geom


def relativize_waveforms(
    wfs, firstchans_orig, z, geom, maxchans_orig=None, feat_chans=18
):
    """
    Extract fewer channels.
    """
    chans_down = feat_chans // 2
    chans_down -= chans_down % 2

    stdwfs = np.zeros(
        (wfs.shape[0], wfs.shape[1], feat_chans), dtype=wfs.dtype
    )

    firstchans_std = firstchans_orig.copy().astype(int)
    maxchans_std = np.zeros(firstchans_orig.shape, dtype=int)
    if z is not None:
        z_rel = np.zeros_like(z)

    for i in range(wfs.shape[0]):
        wf = wfs[i]
        if maxchans_orig is None:
            mcrel = wf.ptp(0).argmax()
        else:
            mcrel = maxchans_orig[i] - firstchans_orig[i]
        mcrix = mcrel - mcrel % 2
        if z is not None:
            z_rel[i] = z[i] - geom[firstchans_orig[i] + mcrel, 1]

        low, high = mcrix - chans_down, mcrix + feat_chans - chans_down
        if low < 0:
            low, high = 0, feat_chans
        if high > wfs.shape[2]:
            low, high = wfs.shape[2] - feat_chans, wfs.shape[2]

        firstchans_std[i] += low
        stdwfs[i] = wf[:, low:high]
        maxchans_std[i] = firstchans_std[i] + stdwfs[i].ptp(0).argmax()

    if z is not None:
        return stdwfs, firstchans_std, maxchans_std, z_rel, chans_down
    else:
        return stdwfs, firstchans_std, maxchans_std, chans_down
