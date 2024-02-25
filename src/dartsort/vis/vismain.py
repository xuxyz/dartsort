import pickle
from pathlib import Path

import matplotlib.pyplot as plt
from tqdm.auto import tqdm

from ..util.analysis import DARTsortAnalysis
from ..util.data_util import DARTsortSorting
from . import scatterplots, unit
from .sorting import make_sorting_summary

try:
    from dredge import motion_util

    have_dredge = True
except ImportError:
    have_dredge = False


def visualize_sorting(
    recording,
    sorting,
    output_directory,
    motion_est=None,
    make_scatterplots=True,
    make_sorting_summaries=True,
    make_unit_summaries=True,
    gt_sorting=None,
    dpi=200,
    layout_max_height=4,
    layout_figsize=(11, 8.5),
    n_jobs=0,
    n_jobs_templates=0,
    overwrite=False,
):
    output_directory.mkdir(exist_ok=True, parents=True)

    if make_scatterplots:
        scatter_unreg = output_directory / "scatter_unreg.png"
        if overwrite or not scatter_unreg.exists():
            fig = plt.figure(figsize=layout_figsize)
            fig, axes, scatters = scatterplots.scatter_spike_features(
                sorting=sorting, figure=fig
            )
            if have_dredge and motion_est is not None:
                motion_util.plot_me_traces(motion_est, axes[2], color="r", lw=1)
            fig.savefig(scatter_unreg, dpi=dpi)
            plt.close(fig)

        scatter_reg = output_directory / "scatter_reg.png"
        if motion_est is not None and (overwrite or not scatter_reg.exists()):
            fig = plt.figure(figsize=layout_figsize)
            fig, axes, scatters = scatterplots.scatter_spike_features(
                sorting=sorting, motion_est=motion_est, registered=True, figure=fig
            )
            fig.savefig(scatter_reg, dpi=dpi)
            plt.close(fig)

    sorting_analysis = None
    if make_sorting_summaries and sorting.n_units > 1:
        sorting_summary = output_directory / "sorting_summary.png"
        if overwrite or not sorting_summary.exists():
            sorting_analysis = DARTsortAnalysis.from_sorting(
                recording=recording,
                sorting=sorting,
                motion_est=motion_est,
                name=output_directory.stem,
                n_jobs_templates=n_jobs_templates,
            )

            fig = make_sorting_summary(
                sorting_analysis,
                max_height=layout_max_height,
                figsize=layout_figsize,
                figure=None,
            )
            fig.savefig(sorting_summary, dpi=dpi)

    if make_unit_summaries and sorting.n_units > 1:
        unit_summary_dir = output_directory / "single_unit_summaries"
        summaries_done = not overwrite and unit.all_summaries_done(
            sorting.unit_ids, unit_summary_dir
        )

        unit_assignments_dir = output_directory / "template_assignments"
        do_assignments = "match" in output_directory.stem
        assignments_done = not overwrite and unit.all_summaries_done(
            sorting.unit_ids, unit_assignments_dir
        )

        do_something = (not summaries_done) or (do_assignments and not assignments_done)
        if sorting_analysis is None and do_something:
            sorting_analysis = DARTsortAnalysis.from_sorting(
                recording=recording,
                sorting=sorting,
                motion_est=motion_est,
                name=output_directory.stem,
                n_jobs_templates=n_jobs_templates,
            )

        if not summaries_done:
            unit.make_all_summaries(
                sorting_analysis,
                unit_summary_dir,
                channel_show_radius_um=50.0,
                amplitude_color_cutoff=15.0,
                max_height=layout_max_height,
                figsize=layout_figsize,
                dpi=dpi,
                n_jobs=n_jobs,
                show_progress=True,
                overwrite=overwrite,
            )

        if do_assignments and not assignments_done:
            unit.make_all_summaries(
                sorting_analysis,
                unit_assignments_dir,
                plots=unit.template_assignment_plots,
                channel_show_radius_um=50.0,
                amplitude_color_cutoff=15.0,
                dpi=dpi,
                n_jobs=n_jobs,
                show_progress=True,
                overwrite=overwrite,
            )


def visualize_all_sorting_steps(
    recording,
    dartsort_dir,
    visualizations_dir,
    make_scatterplots=True,
    make_sorting_summaries=True,
    make_unit_summaries=True,
    gt_sorting=None,
    step_dir_name_format="step{step:02d}_{step_name}",
    motion_est_pkl="motion_est.pkl",
    initial_sortings=("subtraction.h5", "initial_clustering.npz"),
    step_refinements=("split{step}.npz", "merge{step}.npz"),
    match_step_sorting="matching{step}.h5",
    layout_max_height=4,
    layout_figsize=(11, 8.5),
    dpi=200,
    n_jobs=0,
    n_jobs_templates=0,
    overwrite=False,
):
    dartsort_dir = Path(dartsort_dir)
    visualizations_dir = Path(visualizations_dir)

    step_paths = list(initial_sortings)
    n_match_steps = sum(
        1 for _ in dartsort_dir.glob(match_step_sorting.format(step="*"))
    )
    match_step_sortings = step_refinements + (match_step_sorting,)
    for step in range(n_match_steps):
        step_paths.extend(s.format(step=step) for s in match_step_sortings)

    motion_est_pkl = dartsort_dir / motion_est_pkl
    if motion_est_pkl.exists():
        with open(motion_est_pkl, "rb") as jar:
            motion_est = pickle.load(jar)

    for j, path in enumerate(tqdm(step_paths, desc="Sorting steps")):
        sorting_path = dartsort_dir / path
        step_name = sorting_path.name
        if sorting_path.name.endswith(".h5"):
            sorting = DARTsortSorting.from_peeling_hdf5(sorting_path)
            step_name = step_name.removesuffix(".h5")
        elif sorting_path.name.endswith(".npz"):
            sorting = DARTsortSorting.load(sorting_path)
            step_name = step_name.removesuffix(".npz")
        else:
            assert False

        step_dir_name = step_dir_name_format.format(step=j, step_name=step_name)
        visualize_sorting(
            recording=recording,
            sorting=sorting,
            output_directory=visualizations_dir / step_dir_name,
            motion_est=motion_est,
            make_scatterplots=make_scatterplots,
            make_sorting_summaries=make_sorting_summaries,
            make_unit_summaries=make_unit_summaries,
            gt_sorting=gt_sorting,
            dpi=dpi,
            layout_max_height=layout_max_height,
            layout_figsize=layout_figsize,
            n_jobs=n_jobs,
            n_jobs_templates=n_jobs_templates,
            overwrite=overwrite,
        )
