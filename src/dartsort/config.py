"""Configuration classes

Users should not edit this file!

Rather, make your own custom configs by instantiating new
config objects, for example, to turn off neural net denoising
in the featurization pipeline you can make:

```
featurization_config = FeaturizationConfig(do_nn_denoise=False)
```

This will use all the other parameters' default values. This
object can then be passed into the high level functions like
`subtract(...)`.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

repo_root = Path(__file__).parent.parent.parent


@dataclass(frozen=True)
class FeaturizationConfig:
    """Featurization and denoising configuration

    Parameters for a featurization and denoising pipeline
    which has the flow:
    [input waveforms]
        -> [featurization of input waveforms]
        -> [denoising]
        -> [featurization of output waveforms]

    The flags below allow users to control which features
    are computed for the input waveforms, what denoising
    operations are applied, and what features are computed
    for the output (post-denoising) waveforms.
    """

    # -- denoising configuration
    do_nn_denoise: bool = True
    do_tpca_denoise: bool = True
    do_enforce_decrease: bool = True
    # turn off features below
    denoise_only: bool = False

    # -- featurization configuration
    save_input_waveforms: bool = False
    save_input_tpca_projs: bool = True
    save_output_waveforms: bool = False
    save_output_tpca_projs: bool = False
    save_amplitudes: bool = True
    # localization runs on output waveforms
    do_localization: bool = True
    localization_radius: float = 100.0
    # these are saved always if do_localization
    save_amplitude_vectors: bool = True

    # -- further info about denoising
    # in the future we may add multi-channel or other nns
    nn_denoiser_class_name: str = "SingleChannelWaveformDenoiser"
    nn_denoiser_pretrained_path: str = str(
        repo_root / "pretrained" / "single_chan_denoiser.pt"
    )
    # optionally restrict how many channels TPCA are fit on
    tpca_fit_radius: Optional[float] = None
    tpca_rank: int = 8

    # used when naming datasets saved to h5 files
    input_waveforms_name: str = "collisioncleaned"
    output_waveforms_name: str = "denoised"

    def to_class_names_and_kwargs(self):
        """Convert this config into a list of waveform transformer classes and arguments

        Used by WaveformPipeline.from_config(...) to construct WaveformPipelines
        from FeaturizationConfig objects.
        """
        class_names_and_kwargs = []

        do_feats = not self.denoise_only

        if do_feats and self.save_input_waveforms:
            class_names_and_kwargs.append(
                ("Waveform", {"name_prefix": self.input_waveforms_name})
            )
        if do_feats and self.save_input_tpca_projs:
            class_names_and_kwargs.append(
                (
                    "TemporalPCAFeaturizer",
                    {
                        "rank": self.tpca_rank,
                        "name_prefix": self.input_waveforms_name,
                    },
                )
            )
        if self.do_nn_denoise:
            class_names_and_kwargs.append(
                (
                    self.nn_denoiser_class_name,
                    {"pretrained_path": self.nn_denoiser_pretrained_path},
                )
            )
        if self.do_tpca_denoise:
            class_names_and_kwargs.append(
                (
                    "TemporalPCADenoiser",
                    {
                        "rank": self.tpca_rank,
                        "fit_radius": self.tpca_fit_radius,
                    },
                )
            )
        if self.do_enforce_decrease:
            class_names_and_kwargs.append(("EnforceDecrease", {}))
        if do_feats and self.save_output_waveforms:
            class_names_and_kwargs.append(
                (
                    "Waveform",
                    {"name_prefix": self.output_waveforms_name},
                )
            )
        if do_feats and self.save_output_tpca_projs:
            class_names_and_kwargs.append(
                (
                    "TemporalPCAFeaturizer",
                    {
                        "rank": self.tpca_rank,
                        "name_prefix": self.output_waveforms_name,
                    },
                )
            )
        if do_feats and (self.do_localization or self.save_amplitude_vectors):
            class_names_and_kwargs.append(
                (
                    "AmplitudeVector",
                    {"name_prefix": self.output_waveforms_name},
                )
            )
        if do_feats and self.save_amplitudes:
            class_names_and_kwargs.append(
                (
                    "MaxAmplitude",
                    {"name_prefix": self.output_waveforms_name},
                )
            )

        return class_names_and_kwargs


@dataclass(frozen=True)
class SubtractionConfig:
    trough_offset_samples: int = 42
    spike_length_samples: int = 121
    detection_thresholds: List[int] = (12, 10, 8, 6, 5, 4)
    chunk_length_samples: int = 30_000
    peak_sign: str = "neg"
    spatial_dedup_radius: float = 150.0
    extract_radius: float = 200.0
    n_chunks_fit: int = 40
    fit_subsampling_random_state: int = 0
    residnorm_decrease_threshold: float = 3.162  # sqrt(10)

    # how will waveforms be denoised before subtraction?
    # users can also save waveforms/features during subtraction
    subtraction_denoising_config: FeaturizationConfig = FeaturizationConfig(
        denoise_only=True,
        input_waveforms_name="raw",
        output_waveforms_name="subtracted",
    )
