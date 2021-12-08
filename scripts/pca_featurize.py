import argparse
import h5py
import numpy as np
from sklearn.decomposition import IncrementalPCA
from tqdm.auto import trange

from spike_psvae import localization, point_source_centering


ap = argparse.ArgumentParser()

ap.add_argument("input_h5")
ap.add_argument("input_dataset")

ap.add_argument("output_h5")

ap.add_argument("--n_components", type=int, default=5, required=False)
ap.add_argument("--batch_size", type=int, default=8192, required=False)
ap.add_argument("--maxchans_key", default="max_channels", required=False)
ap.add_argument("--n_workers", type=int, default=1, required=False)
ap.add_argument("--relocate_dims", type=str, default="yza", required=False)

args = ap.parse_args()
batch_size = args.batch_size

K = args.n_components
ipca_orig = IncrementalPCA(n_components=K)
ipca_reloc = IncrementalPCA(n_components=K)

input_h5 = h5py.File(args.input_h5, "r+")
waveforms = input_h5[args.input_dataset]
if args.input_dataset + "_relocated" not in input_h5:
    relocated_waveforms = input_h5.create_dataset_like(
        args.input_dataset + "_relocated", waveforms
    )
else:
    relocated_waveforms = input_h5[args.input_dataset + "_relocated"]
spike_index = input_h5["spike_index"][:]
maxchans_key = None
if args.maxchans_key in input_h5:
    maxchans = input_h5[args.maxchans_key][:]
geom = input_h5["geom"][:]
N, T, C = waveforms.shape
assert C < geom.shape[0]
geomkind = "standard" if (C // 2) % 2 else "updown"
firstchans = None
if "first_channels" in input_h5:
    firstchans = input_h5["first_channels"]
    geomkind = "firstchan"
print("geomkind is", geomkind)

channel_radius = C // 2
print(N, T, C)
xs, ys, z_rels, z_abss, alphas = localization.localize_waveforms_batched(
    waveforms,
    geom,
    maxchans=maxchans,
    channel_radius=channel_radius,
    n_workers=args.n_workers,
    jac=False,
    firstchans=firstchans,
    geomkind=geomkind,
    batch_size=1024,
)

output_h5 = h5py.File(args.output_h5, "w-")
output_h5.create_dataset("geom", data=geom)
output_h5.create_dataset("x", data=xs)
output_h5.create_dataset("y", data=ys)
output_h5.create_dataset("z_rel", data=z_rels)
output_h5.create_dataset("z_abs", data=z_abss)
output_h5.create_dataset("alpha", data=alphas)
output_h5.create_dataset("spike_index", data=spike_index)
maxptp = output_h5.create_dataset("maxptp", shape=xs.shape, dtype=np.float64)

for b in trange(N // batch_size, desc="fit"):
    start = b * batch_size
    end = min(N, (b + 1) * batch_size)

    wfs_orig = waveforms[start:end]
    maxptp[start:end] = wfs_orig.ptp(1).ptp(1)
    B, _, _ = wfs_orig.shape
    wfs_reloc, r, q = point_source_centering.relocate_simple(
        wfs_orig,
        geom,
        maxchans[start:end],
        xs[start:end],
        ys[start:end],
        z_rels[start:end],
        alphas[start:end],
        channel_radius=channel_radius,
        firstchans=firstchans[start:end] if firstchans else None,
        geomkind=geomkind,
        relocate_dims=args.relocate_dims,
        interp_xz=False,
    )
    relocated_waveforms[start:end] = wfs_reloc

    ipca_orig.partial_fit(wfs_orig.reshape(B, -1))
    ipca_reloc.partial_fit(wfs_reloc.reshape(B, -1))

loadings_orig = np.empty((N, K))
loadings_reloc = np.empty((N, K))
for b in trange(N // batch_size, desc="project"):
    start = b * batch_size
    end = min(N, (b + 1) * batch_size)

    wfs_orig = waveforms[start:end].reshape(end - start, -1)
    wfs_reloc = relocated_waveforms[start:end].reshape(end - start, -1)

    loadings_orig[start:end] = ipca_orig.transform(wfs_orig)
    loadings_reloc[start:end] = ipca_reloc.transform(wfs_reloc)

output_h5.create_dataset("loadings_orig", data=loadings_orig)
output_h5.create_dataset(
    "pcs_orig", data=ipca_orig.components_.reshape(K, T, C)
)
output_h5.create_dataset("loadings_reloc", data=loadings_reloc)
output_h5.create_dataset(
    "pcs_reloc", data=ipca_reloc.components_.reshape(K, T, C)
)
