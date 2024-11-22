import itertools
import warnings
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
import torch
from tqdm.auto import tqdm

from ..cluster import gaussian_mixture, stable_features
from ..util import spiketorch
from ..util.multiprocessing_util import (CloudpicklePoolExecutor,
                                         ThreadPoolExecutor, cloudpickle,
                                         get_pool)
from . import analysis_plots, gmm_helpers, layout
from .colors import glasbey1024
from .waveforms import geomplot

distance_cmap = plt.cm.plasma


class GMMPlot(layout.BasePlot):
    width = 1
    height = 1
    kind = "gmm"

    def draw(self, panel, gmm, unit_id):
        raise NotImplementedError


# -- summary plots


class ISIHistogram(GMMPlot):
    kind = "small"
    width = 2
    height = 1.5

    def __init__(self, bin_ms=0.1, max_ms=5):
        self.bin_ms = bin_ms
        self.max_ms = max_ms

    def draw(self, panel, gmm, unit_id):
        axis = panel.subplots()
        times_s = gmm.data.times_seconds[gmm.labels == unit_id]
        dt_ms = np.diff(times_s) * 1000
        bin_edges = np.arange(0, self.max_ms + self.bin_ms, self.bin_ms)
        counts, _ = np.histogram(dt_ms, bin_edges)
        axis.stairs(counts, bin_edges, color=glasbey1024[unit_id], fill=True)
        axis.set_xlabel("isi (ms)")
        axis.set_ylabel(f"count ({dt_ms.size+1} tot. sp.)")


class ChansHeatmap(GMMPlot):
    kind = "tall"
    width = 2
    height = 3

    def __init__(self, cmap=plt.cm.magma):
        self.cmap = cmap

    def draw(self, panel, gmm, unit_id):
        (in_unit_full,) = torch.nonzero(gmm.labels == unit_id, as_tuple=True)
        spike_chans = gmm.data.extract_channels[in_unit_full].numpy(force=True)
        ixs = spike_chans[spike_chans < gmm.data.n_channels]
        unique_ixs, counts = np.unique(ixs, return_counts=True)
        ax = panel.subplots()
        xy = gmm.data.prgeom.numpy(force=True)
        s = ax.scatter(*xy[unique_ixs].T, c=counts, lw=0, cmap=self.cmap)
        plt.colorbar(s, ax=ax, shrink=0.3, label="chan count")
        ax.scatter(
            *xy[gmm[unit_id].channels.numpy(force=True)].T,
            color="r",
            lw=1,
            fc="none",
        )
        ax.scatter(
            *xy[np.atleast_1d(gmm[unit_id].snr.argmax().numpy(force=True))].T,
            color="g",
            lw=0,
        )


class TextInfo(GMMPlot):
    kind = "aaatext"
    width = 2
    height = 1

    def draw(self, panel, gmm, unit_id):
        axis = panel.subplots()
        axis.axis("off")
        msg = f"unit {unit_id}\n"

        nspikes = (gmm.labels == unit_id).sum()
        msg += f"n spikes: {nspikes}\n"
        if gmm[unit_id].annotations:
            msg += "annots:\n"
            for k, v in gmm[unit_id].annotations.items():
                if torch.is_tensor(k):
                    k = k.numpy(force=True)
                    if k.size == 1:
                        k = k.item()
                if torch.is_tensor(v):
                    v = v.numpy(force=True)
                if isinstance(v, np.ndarray):
                    if not v.size:
                        v = "[]"
                    elif v.size == 1:
                        v = v.item()
                    elif v.ndim == 1:
                        vv = [str(v[0])]
                        for vvv in map(str, v[1:]):
                            if len(vv[-1]) > 16:
                                vv[-1] += "\n"
                                vv.append(vvv)
                                continue
                            vv[-1] += "," + vvv
                        v = "\n".join(vv)
                msg += f"{k}:\n{v}"

        axis.text(0, 0, msg, fontsize=6.5)


class MStep(GMMPlot):
    kind = "mstep"
    width = 5
    height = 4
    alpha = 0.05

    def __init__(self, n_waveforms_show=64):
        self.n_waveforms_show = n_waveforms_show

    def draw(self, panel, gmm, unit_id, axes=None):
        if self.with_covs:
            panel_top, panel_bottom = panel.subfigures(nrows=2, height_ratios=[1, 1])
        else:
            panel_top = panel
        ax = panel_top.subplots()
        ax.axis("off")

        # get spike data and determine channel set by plotting
        sp = gmm.random_spike_data(
            unit_id, max_size=self.n_waveforms_show, with_reconstructions=True
        )
        maa = sp.waveforms.abs().nan_to_num().max()
        geomplot_kw = dict(
            max_abs_amp=maa,
            geom=gmm.data.prgeom.numpy(force=True),
            show_zero=False,
            return_chans=True,
        )
        lines, chans = geomplot(
            sp.waveforms,
            channels=sp.channels,
            color="k",
            alpha=self.alpha,
            ax=ax,
            **geomplot_kw,
        )
        chans = torch.tensor(list(chans))
        tup = gaussian_mixture.to_full_probe(
            sp, weights=None, n_channels=gmm.data.n_channels, storage=None
        )
        features_full, weights_full, count_data, weights_normalized = tup
        feats = features_full[:, :, chans]
        n, r, c = feats.shape
        emp_mean = torch.nanmean(feats, dim=0)
        emp_mean = gmm.data.tpca.force_reconstruct(emp_mean.nan_to_num_())
        model_mean = gmm[unit_id].mean[:, chans]
        model_mean = gmm.data.tpca.force_reconstruct(model_mean)

        geomplot(
            np.stack([emp_mean, model_mean], axis=0),
            channels=chans[None].broadcast_to(2, *chans.shape).numpy(force=True),
            max_abs_amp=maa,
            geom=gmm.data.prgeom.numpy(force=True),
            colors=["k", glasbey1024[unit_id]],
            show_zero=False,
            ax=ax,
        )
        ax.axis("off")
        ax.set_title("reconstructed mean and example inputs")


class CovarianceResidual(GMMPlot):
    kind = "mstep"
    width = 7
    height = 5

    def draw(self, panel, gmm, unit_id):
        sp = gmm.random_spike_data(unit_id)
        weights = gmm.get_fit_weights(
            unit_id, sp.indices, getattr(gmm, "log_liks", None)
        )

        achans = gaussian_mixture.occupied_chans(sp, gmm.noise.n_channels)
        if weights is None:
            weights = sp.features.new_ones(len(sp))
        afeats, aweights = stable_features.pad_to_chans(
            sp, achans, gmm.noise.n_channels, weights=weights, pad_value=torch.nan
        )
        aweights_sum = torch.nansum(aweights, 0)
        aweights_norm = aweights / aweights_sum

        mean = torch.linalg.vecdot(
            aweights_norm.unsqueeze(1).nan_to_num(), afeats.nan_to_num(), dim=0
        )
        afeatsc = afeats - mean

        emp_cov = spiketorch.nancov(
            afeatsc.view(len(sp), -1), weights=weights, correction=0, force_posdef=True
        )
        # emp_cov = torch.cov(afeatsc.view(len(sp), -1).nan_to_num().T)
        noise_cov = gmm.noise.marginal_covariance(achans).to_dense()
        residual = emp_cov - noise_cov

        mmT = mean.view(-1, 1) @ mean.view(1, -1)
        scale = (mmT * residual).sum() / mmT.square().sum()
        if scale < 0:
            warnings.warn(f"mmT {scale=:0.3f} negative, clipping.")
            scale = 0.0
        mmT_cov = noise_cov + scale * mmT
        mmT_residual = emp_cov - mmT_cov

        emp_eigs = torch.linalg.eigvalsh(emp_cov)
        noise_eigs = torch.linalg.eigvalsh(noise_cov)
        residual_eigs, residual_vecs = torch.linalg.eigh(residual)
        try:
            mmT_residual_eigs = torch.linalg.eigvalsh(mmT_residual)
        except Exception as e:
            warnings.warn(f"mmT residual. {e}")
            mmT_residual_eigs = torch.zeros(mmT_residual.shape[0])

        rank1 = (residual_vecs[:, -1:] * residual_eigs[-1:]) @ residual_vecs[:, -1:].T
        rank1_model = noise_cov + rank1
        rank1_residual = emp_cov - rank1_model
        rank1_residual_eigs = torch.linalg.eigvalsh(rank1_residual)

        signal = gmm[unit_id].marginal_covariance(channels=achans, signal_only=True)
        signal = signal.to_dense()
        modelcov = gmm[unit_id].marginal_covariance(channels=achans)
        modelcov = modelcov.to_dense()
        model_residual = emp_cov - modelcov
        model_residual_eigs = torch.linalg.eigvalsh(model_residual)

        top, bot = panel.subfigures(nrows=2, height_ratios=[5, 2])
        axes = top.subplots(nrows=3, ncols=3, sharex=True, sharey=True)
        # ax_eig, ax_r2 = bot.subplots(ncols=2)
        ax_eig = bot.subplots()

        # vm = 0.9 * emp_cov.abs().max()
        imk = dict(cmap=plt.cm.seismic, interpolation="none")

        covs = dict(
            emp=emp_cov,
            noise=noise_cov,
            noise_resid=residual,
            mmT=scale * mmT,
            mmT_model=mmT_cov,
            mmT_resid=mmT_residual,
            rank1=rank1,
            rank1_model=rank1_model,
            rank1_resid=rank1_residual,
            model_signal=signal,
            model=modelcov,
            model_residual=model_residual,
        )
        colors = dict(
            emp="k",
            noise="g",
            noise_resid="r",
            mmT="gray",
            mmT_model="gray",
            mmT_resid="orange",
            rank1="gray",
            rank1_model="gray",
            rank1_resid="fuchsia",
            model_signal="gray",
            model="gray",
            model_resid="purple",
        )

        eigs = dict(
            emp=emp_eigs,
            noise=noise_eigs,
            noise_resid=residual_eigs,
            mmT_resid=mmT_residual_eigs,
            rank1_resid=rank1_residual_eigs,
            model_resid=model_residual_eigs,
        )
        for (name, cov), ax in zip(covs.items(), axes.flat):
            color = colors[name]
            vm = cov.abs().max() * 0.9
            mimk = imk | dict(vmax=vm, vmin=-vm)
            # if name == "mmT":
            #     vm = cov.abs().max() * 0.9
            #     mimk = dict(
            #         vmin=-vm, vmax=vm, cmap=plt.cm.seismic, interpolation="none"
            #     )
            # else:
            #     mimk = imk
            im = ax.imshow(cov, **mimk)
            cb = plt.colorbar(im, ax=ax, shrink=0.2)
            cb.outline.set_visible(False)
            title = name
            if name == "mmT":
                title = title + f" (scale={scale:.2f})"
            ax.set_title(title, color=color)
            if name in eigs:
                ax_eig.plot(eigs[name].flip(0), color=color, lw=1)
                # r2 = (eigs['emp'].sum() - F.relu(eigs[name].flip(0)).cumsum(0)) / eigs['emp'].sum()
                # ax_r2.plot(r2, color=color, lw=1)

        for ax in axes.flat[len(covs) :]:
            ax.axis("off")
        ax_eig.set_xlabel("eig index")
        ax_eig.set_ylabel("eigenvalues")
        ax_eig.axhline(0, color="k", lw=0.8)
        # ax_r2.set_ylabel('1-R^2')
        # ax_eig.axhline([0, 1], color='k', lw=0.8)


class Likelihoods(GMMPlot):
    kind = "widescatter"
    width = 4
    height = 2

    def __init__(self, viol_ms=1.0):
        self.viol_ms = viol_ms

    def draw(self, panel, gmm, unit_id, axes=None):
        ax_time, ax_noise, ax_dist = panel.subplots(
            ncols=3, width_ratios=[3, 2, 1], sharey=True
        )
        ax_time.set_ylabel("log likelihood")
        ax_time.set_xlabel("time (s)")
        ax_noise.set_xlabel("noise loglik")
        ax_dist.set_xlabel("count")
        (in_unit,) = torch.nonzero(gmm.labels == unit_id, as_tuple=True)
        if not in_unit.numel():
            return
        if hasattr(gmm, "log_liks"):
            liks_ = gmm.log_liks[:, in_unit][[unit_id]].tocoo()
            inds_ = None
            if liks_.nnz:
                inds_ = in_unit
                liks = np.full(in_unit.shape, -np.inf, dtype=np.float32)
                liks[liks_.coords[1]] = liks_.data
                liks = torch.from_numpy(liks)
        else:
            inds_, liks = gmm.unit_log_likelihoods(unit_id, spike_indices=in_unit)
        if inds_ is None:
            return
        assert torch.equal(inds_, in_unit)
        nliks = gmm.noise_log_likelihoods()[1][in_unit]
        t = gmm.data.times_seconds[in_unit]
        dt_ms = np.diff(t) * 1000
        small = dt_ms <= self.viol_ms

        c = glasbey1024[unit_id]
        ax_time.scatter(t, liks, s=3, lw=0, color=c)
        if small.any():
            small = np.logical_or(
                np.pad(small, (1, 0), constant_values=False),
                np.pad(small, (0, 1), constant_values=False),
            )
            ax_time.scatter(t[small], liks[small], s=3, lw=0, color="k")
        ax_noise.scatter(nliks, liks, s=3, lw=0, color=c)
        histk = dict(histtype="step", orientation="horizontal")
        n, bins, _ = ax_dist.hist(
            liks[torch.isfinite(liks)], color=c, label="unit", bins=64, **histk
        )
        ax_dist.hist(nliks, color="k", label="noise", bins=bins, **histk)
        ax_dist.legend(
            loc="lower right",
            borderpad=0.1,
            frameon=False,
            borderaxespad=0.1,
            handletextpad=0.3,
            handlelength=1.0,
        )


class Amplitudes(GMMPlot):
    kind = "widescatter"
    width = 4
    height = 2

    def __init__(self, viol_ms=1.0):
        self.viol_ms = viol_ms

    def draw(self, panel, gmm, unit_id, axes=None):
        (in_unit,) = torch.nonzero(gmm.labels == unit_id, as_tuple=True)
        gmm_helpers.amp_double_scatter(
            gmm, in_unit, panel, unit_id=unit_id, labels=None, viol_ms=self.viol_ms
        )


# -- split-oriented plots


class KMeansSplit(GMMPlot):
    kind = "friend"
    width = 6.5
    height = 9

    def __init__(self, layout="vert"):
        self.layout = layout

    def draw(self, panel, gmm, unit_id, split_info=None):
        if split_info is None:
            split_info = gmm.kmeans_split_unit(unit_id, debug=True)
        failed = not split_info or "reas_labels" not in split_info
        if failed:
            ax = panel.subplots()
            if not split_info:
                msg = "no channels!"
            else:
                msg = "split abandoned"
            ax.text(0.5, 0.5, msg, ha="center", transform=ax.transAxes)
            ax.axis("off")
            return

        split_labels = split_info["reas_labels"]
        split_ids = np.unique(split_labels)

        kw = dict(nrows=4, height_ratios=[1, 1, 2, 2])
        if self.layout == "horz":
            kw = dict(ncols=4, width_ratios=[1, 1, 1, 1])
        amps_row, centroids_row, mcmeans_row, modes_row = panel.subfigures(**kw)
        fig_chans, fig_dists = modes_row.subfigures(ncols=2)
        fig_dist, fig_bimods = fig_dists.subfigures(nrows=2)
        panel.suptitle("kmeans split info")

        # subunit amplitudes
        gmm_helpers.amp_double_scatter(
            gmm, split_info["sp"].indices, amps_row, labels=split_labels
        )

        # distance matrix
        ax_dist = analysis_plots.distance_matrix_dendro(
            fig_dist,
            split_info["distances"],
            # unit_ids=split_ids,
            dendrogram_linkage=None,
            show_unit_labels=True,
            vmax=1.0,
            image_cmap=distance_cmap,
            show_values=True,
        )
        normstr = ", noisenormed" if gmm.distance_noise_normalized else ""
        ax_dist.set_title(f"{gmm.distance_metric}{normstr}", fontsize="small")

        # bimodality matrix
        ax_bimod = analysis_plots.distance_matrix_dendro(
            fig_bimods,
            split_info["bimodalities"],
            # unit_ids=split_ids,
            dendrogram_linkage=None,
            show_unit_labels=True,
            vmax=0.5,
            image_cmap=distance_cmap,
            show_values=True,
        )
        ax_bimod.set_title("bimodality", fontsize="small")

        # subunit means on the unit main channel, where possible
        ax_centroids, ax_mycentroids = centroids_row.subplots(ncols=2, sharey=True)
        ax_centroids.set_ylabel("orig unit main chan")
        ax_mycentroids.set_ylabel("split unit main chan")
        for ax in (ax_centroids, ax_mycentroids):
            ax.set_xticks([])
            ax.axhline(0, color="k", lw=0.8)
            sns.despine(ax=ax, left=False, right=True, bottom=True, top=True)
        mainchan = gmm[unit_id].snr.argmax()
        for subid, subunit in zip(split_ids, split_info["units"]):
            subm = subunit.mean[:, mainchan]
            subm = gmm.data.tpca._inverse_transform_in_probe(subm[None])[0]
            ax_centroids.plot(subm, color=glasbey1024[subid])

            subm = subunit.mean[:, subunit.snr.argmax()]
            subm = gmm.data.tpca._inverse_transform_in_probe(subm[None])[0]
            ax_mycentroids.plot(subm, color=glasbey1024[subid])

        # subunit multichan means
        chans = torch.cdist(gmm.data.prgeom[mainchan[None]], gmm.data.prgeom)
        chans = chans.view(-1)
        (chans,) = torch.nonzero(chans <= gmm.data.core_radius, as_tuple=True)
        if len(split_ids) < len(split_info["units"]):
            split_info["units"] = [split_info["units"][j] for j in split_ids]
        gmm_helpers.plot_means(
            mcmeans_row,
            gmm.data.prgeom,
            gmm.data.tpca,
            chans,
            split_info["units"],
            split_ids,
            title=None,
        )

        # subunit channels histogram
        chan_bins = torch.unique(split_info["sp"].channels)
        chan_bins = chan_bins[chan_bins < gmm.data.n_channels]
        chan_bins = torch.arange(chan_bins.min(), chan_bins.max() + 1)
        unit_chans = []
        for j in split_ids:
            uc = split_info["sp"].channels[split_labels == j]
            unit_chans.append(uc[uc < gmm.data.n_channels])
        ax_chans = fig_chans.subplots()
        ax_chans.hist(
            unit_chans,
            histtype="bar",
            bins=chan_bins,
            color=glasbey1024[split_ids],
            stacked=True,
        )
        ax_chans.set_xlabel("channel")
        ax_chans.set_ylabel("chan count in subunit")


# -- merge-oriented plots


class NeighborMeans(GMMPlot):
    kind = "merge"
    width = 4
    height = 3

    def __init__(self, n_neighbors=5):
        self.n_neighbors = n_neighbors

    def draw(self, panel, gmm, unit_id):
        neighbors = gmm_helpers.get_neighbors(gmm, unit_id)
        units = [gmm[u] for u in reversed(neighbors)]
        labels = neighbors[::-1]

        # means on core channels
        chans = gmm[unit_id].snr.argmax()
        chans = torch.cdist(gmm.data.prgeom[chans[None]], gmm.data.prgeom)
        chans = chans.view(-1)
        (chans,) = torch.nonzero(chans <= gmm.data.core_radius, as_tuple=True)

        gmm_helpers.plot_means(
            panel, gmm.data.prgeom, gmm.data.tpca, chans, units, labels
        )


class NeighborDistances(GMMPlot):
    kind = "merge"
    width = 4
    height = 2

    def __init__(self, n_neighbors=5, dist_vmax=1.0):
        self.n_neighbors = n_neighbors
        self.dist_vmax = dist_vmax

    def draw(self, panel, gmm, unit_id):
        neighbors = gmm_helpers.get_neighbors(gmm, unit_id)
        distances = gmm.distances(
            units=[gmm[u] for u in neighbors], show_progress=False
        )
        ax = analysis_plots.distance_matrix_dendro(
            panel,
            distances,
            unit_ids=neighbors,
            dendrogram_linkage=None,
            show_unit_labels=True,
            vmax=self.dist_vmax,
            image_cmap=distance_cmap,
            show_values=True,
        )
        normstr = ", noisenormed" if gmm.distance_noise_normalized else ""
        ax.set_title(f"nearby {gmm.distance_metric}{normstr}", fontsize="small")


class NeighborBimodalities(GMMPlot):
    kind = "bim"
    width = 4
    height = 9

    def __init__(self, n_neighbors=5):
        self.n_neighbors = n_neighbors

    def draw(self, panel, gmm, unit_id):
        neighbors = gmm_helpers.get_neighbors(gmm, unit_id)
        assert neighbors[0] == unit_id
        if hasattr(gmm, "log_liks"):
            neighbors_plus_noiseunit = np.concatenate(
                (neighbors, [gmm.log_liks.shape[0] - 1])
            )
            log_liks = gmm.log_liks[neighbors_plus_noiseunit]
        else:
            log_liks = gmm.log_likelihoods(unit_ids=neighbors)
        labels, spikells, log_liks = gaussian_mixture.loglik_reassign(
            log_liks, has_noise_unit=True
        )
        kept = np.logical_and(labels >= 0, labels < len(neighbors))
        labels_ = np.full_like(labels, -1)
        labels_[kept] = neighbors[labels[kept]]
        labels = labels_

        others = neighbors[1:]
        axes = panel.subplots(nrows=len(others), ncols=2)
        histkw = dict(density=True, histtype="step", bins=128)
        no_leg_yet = True
        for j, (axes_row, other_id) in enumerate(zip(axes, others)):
            bimod_info = gmm.unit_pair_bimodality(
                unit_id,
                other_id,
                log_liks,
                loglik_ix_a=0,
                loglik_ix_b=j + 1,
                debug=True,
            )

            scatter_ax, bimod_ax = axes_row
            if j == 0:
                scatter_ax.set_title("loglik pair scatter", fontsize="small")
                bimod_ax.set_title("bimodality computation", fontsize="small")

            if "in_pair_kept" not in bimod_info:
                scatter_ax.text(
                    0.5,
                    0.5,
                    f"too few spikes",
                    transform=scatter_ax.transAxes,
                    ha="center",
                    va="center",
                )
                continue
            else:
                c = np.atleast_2d(glasbey1024[labels[bimod_info["in_pair_kept"]]])
                scatter_ax.scatter(bimod_info["xi"], bimod_info["xj"], s=3, lw=0, c=c)
                scatter_ax.set_ylabel(unit_id, color=glasbey1024[unit_id])
                scatter_ax.set_xlabel(other_id.item(), color=glasbey1024[other_id])

            if "samples" not in bimod_info:
                bimod_ax.text(
                    0.5,
                    0.5,
                    f"too-small\nkept prop {bimod_info['keep_prop']:.2f}",
                    transform=bimod_ax.transAxes,
                    ha="center",
                    va="center",
                )
                bimod_ax.axis("off")
                continue

            bimod_ax.hist(bimod_info["samples"], color="gray", label="hist", **histkw)
            bimod_ax.hist(
                bimod_info["samples"],
                weights=bimod_info["sample_weights"],
                color="k",
                label="whist",
                **histkw,
            )
            bimod_ax.axvline(bimod_info["cut"], color="k", lw=0.8, ls=":")
            bimod_ax.plot(
                bimod_info["domain"],
                bimod_info["alternative_density"],
                color="r",
                label="alt",
            )
            bimod_ax.plot(
                bimod_info["domain"], bimod_info["uni_density"], color="b", label="null"
            )
            info = f"{bimod_info['score_kind']}{bimod_info['score']:.3f} ll({unit_id})-ll({other_id.item()})"
            bimod_ax.set_xlabel(info)
            bimod_ax.set_yticks([])
            if no_leg_yet:
                bimod_ax.legend(
                    loc="upper right",
                    borderpad=0.1,
                    frameon=False,
                    borderaxespad=0.1,
                    handletextpad=0.3,
                    handlelength=1.0,
                    markerfirst=False,
                )
                no_leg_yet = False


class NeighborInfoCriteria(GMMPlot):
    kind = "bim"
    width = 4
    height = 9

    def __init__(self, n_neighbors=5, fit_by_avg=False):
        self.n_neighbors = n_neighbors
        self.fit_by_avg = fit_by_avg

    def draw(self, panel, gmm, unit_id):
        neighbors = gmm_helpers.get_neighbors(gmm, unit_id)
        assert neighbors[0] == unit_id
        others = neighbors[1:]
        axes = panel.subplots(nrows=len(others), ncols=1)
        histkw = dict(density=True, histtype="step", bins=128, log=True)
        astr = "AICfull/merged: {aic_full:0.1f} / {aic_merged:0.1f}"
        bstr = "BICfull/merged: {bic_full:0.1f} / {bic_merged:0.1f}"
        lstr = "LLfull/merged: {full_loglik:0.1f} / {unit_loglik:0.1f}"
        cstr = f"{astr}\n{bstr}\n{lstr}\n"
        bbox = dict(facecolor="w", alpha=0.5, edgecolor="none")
        for ax, other_id in zip(axes, others):
            uids = [unit_id, other_id]
            res = gmm.unit_group_criterion(uids, gmm.log_liks, debug=True)
            sll = res["subunit_logliks"]
            if not torch.is_tensor(sll):
                sll = sll.tocsr()
            for row, uid in enumerate(uids):
                if not torch.is_tensor(sll):
                    rowsll = sll[[row]].data
                else:
                    rowsll = sll[row]
                rowsll = rowsll[torch.isfinite(rowsll)]
                if rowsll.numel():
                    ax.hist(rowsll, color=glasbey1024[uid], **histkw)
            ull = res["unit_logliks"]
            if ull is not None:
                ax.hist(ull[torch.isfinite(ull)], color="k", **histkw)
            s = f"other={other_id}\n" + cstr.format_map(res)
            aic_merge = res["aic_merged"] < res["aic_full"]
            bic_merge = res["bic_merged"] < res["bic_full"]
            ll_merge = res["unit_loglik"] > res["full_loglik"]
            aicdif = res["aic_full"] - res["aic_merged"]
            bicdif = res["bic_full"] - res["bic_merged"]
            lldif = res["full_loglik"] - res["unit_loglik"]
            s += f"aic: {aicdif:0.1f}, " + ("merge!" if aic_merge else "nope.") + "\n"
            s += f"bic: {bicdif:0.1f}, " + ("merge!" if bic_merge else "nope.") + "\n"
            s += f"ll: {lldif:0.1f}, " + ("merge!" if ll_merge else "nope.")
            ax.text(0.05, 0.95, s, transform=ax.transAxes, va="top", bbox=bbox)
            ax.set_xlabel("log lik")
            sns.despine(ax=ax, left=True, right=True, top=True)
            # ax.set_yticks([])


# -- main api

default_gmm_plots = (
    TextInfo(),
    ISIHistogram(),
    ISIHistogram(bin_ms=1, max_ms=50),
    ChansHeatmap(),
    MStep(with_covs=False),
    CovarianceResidual(),
    Likelihoods(),
    Amplitudes(),
    KMeansSplit(),
    NeighborMeans(),
    NeighborDistances(),
    NeighborBimodalities(),
    NeighborInfoCriteria(),
)


def make_unit_gmm_summary(
    gmm,
    unit_id,
    plots=default_gmm_plots,
    max_height=9,
    figsize=(15, 11),
    hspace=0.1,
    figure=None,
    **other_global_params,
):
    # notify plots of global params
    for p in plots:
        p.notify_global_params(
            **other_global_params,
        )

    figure = layout.flow_layout(
        plots,
        max_height=max_height,
        figsize=figsize,
        hspace=hspace,
        figure=figure,
        gmm=gmm,
        unit_id=unit_id,
    )

    return figure


def make_all_gmm_summaries(
    gmm,
    save_folder,
    plots=default_gmm_plots,
    max_height=9,
    figsize=(15, 11),
    hspace=0.1,
    dpi=200,
    image_ext="png",
    n_jobs=0,
    show_progress=True,
    overwrite=False,
    unit_ids=None,
    use_threads=False,
    n_units=None,
    seed=0,
    **other_global_params,
):
    save_folder = Path(save_folder)
    if unit_ids is None:
        unit_ids = gmm.unit_ids()
    if n_units is not None and n_units < len(unit_ids):
        rg = np.random.default_rng(seed)
        unit_ids = rg.choice(unit_ids, size=n_units, replace=False)
    if not overwrite and all_summaries_done(unit_ids, save_folder, ext=image_ext):
        return

    save_folder.mkdir(exist_ok=True)

    global_params = dict(
        **other_global_params,
    )

    ispar = n_jobs > 0
    cls = CloudpicklePoolExecutor
    if use_threads:
        cls = ThreadPoolExecutor
    n_jobs, Executor, context = get_pool(n_jobs, cls=cls)

    initargs = (
        gmm,
        plots,
        max_height,
        figsize,
        hspace,
        dpi,
        save_folder,
        image_ext,
        overwrite,
        global_params,
    )
    if ispar and not use_threads:
        initargs = (cloudpickle.dumps(initargs),)
    with Executor(
        max_workers=n_jobs,
        mp_context=context,
        initializer=_summary_init,
        initargs=initargs,
    ) as pool:
        results = pool.map(_summary_job, unit_ids)
        if show_progress:
            results = tqdm(
                results,
                desc="GMM summaries",
                smoothing=0,
                total=len(unit_ids),
            )
        for res in results:
            pass


def all_summaries_done(unit_ids, save_folder, ext="png"):
    return save_folder.exists() and all(
        (save_folder / f"unit{unit_id:04d}.{ext}").exists() for unit_id in unit_ids
    )


class SummaryJobContext:
    def __init__(
        self,
        gmm,
        plots,
        max_height,
        figsize,
        hspace,
        dpi,
        save_folder,
        image_ext,
        overwrite,
        global_params,
    ):
        self.gmm = gmm
        self.plots = plots
        self.max_height = max_height
        self.figsize = figsize
        self.hspace = hspace
        self.dpi = dpi
        self.save_folder = save_folder
        self.image_ext = image_ext
        self.overwrite = overwrite
        self.global_params = global_params


_summary_job_context = None


def _summary_init(*args):
    global _summary_job_context
    if len(args) == 1:
        from cloudpickle import loads

        args = loads(args[0])
    _summary_job_context = SummaryJobContext(*args)


def _summary_job(unit_id):
    # handle resuming/overwriting
    try:
        ext = _summary_job_context.image_ext
        tmp_out = _summary_job_context.save_folder / f"tmp_unit{unit_id:04d}.{ext}"
        final_out = _summary_job_context.save_folder / f"unit{unit_id:04d}.{ext}"
        if tmp_out.exists():
            tmp_out.unlink()
        if not _summary_job_context.overwrite and final_out.exists():
            return
        if _summary_job_context.overwrite and final_out.exists():
            final_out.unlink()

        fig = plt.figure(
            figsize=_summary_job_context.figsize,
            layout="constrained",
            # dpi=_summary_job_context.dpi,
        )
        make_unit_gmm_summary(
            _summary_job_context.gmm,
            unit_id,
            hspace=_summary_job_context.hspace,
            plots=_summary_job_context.plots,
            max_height=_summary_job_context.max_height,
            figsize=_summary_job_context.figsize,
            figure=fig,
            **_summary_job_context.global_params,
        )

        # the save is done sort of atomically to help with the resuming and avoid
        # half-baked image files
        fig.savefig(tmp_out, dpi=_summary_job_context.dpi)
        tmp_out.rename(final_out)
        plt.close(fig)
    except Exception:
        import traceback

        print("// error in unit", unit_id)
        print(traceback.format_exc())
        raise
    finally:
        if tmp_out.exists():
            tmp_out.unlink()
